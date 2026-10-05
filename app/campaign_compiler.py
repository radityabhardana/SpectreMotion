"""Campaign compiler: turn free-text terms-of-service into a structured profile.

Uses the LLMPort (so CI can inject a fake). The compiler is deliberately
defensive: it asks for strict JSON, strips fences, and validates the result
with the same `validate_profile` the runtime uses — so a hallucinated rule
can never silently enter the engine.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app import campaign
from app.errors import ValidationError
from app.ports import LLMPort

log = logging.getLogger("spectre.compiler")


SYSTEM_PROMPT = (
    "You convert terms-of-service / brand rules into a STRICT JSON compliance "
    "profile. Output JSON only, no prose, no markdown fences. "
    "Use exactly the schema given. If unsure, include the clause in "
    "'unresolved' rather than inventing a rule."
)

USER_TEMPLATE = """Convert the following terms-of-service into a compliance profile.

Return JSON exactly:
{{
  "id": "kebab-case-id",
  "name": "Human readable name",
  "strictness": "loose | standard | strict",
  "duration_s": {{"min": <int|null>, "max": <int|null>}},
  "banned_exact": ["word or phrase"],
  "banned_topics": [{{"topic": "label", "regex": ["lowercase pattern"]}}],
  "caption_required": {{"all_of": ["#tag"], "any_of": ["#tag"]}},
  "human_gates": [{{"id": "gate_id", "text": "what to confirm",
                    "trigger": "llm_flag:some_flag"}}],
  "curation_note": "short guidance",
  "unresolved": ["clauses you could not map to a rule"]
}}

Rules:
- `banned_exact` = hard-banned literal words (proper nouns, competitor names).
- `banned_topics.regex` = lowercase regex patterns, WARN only.
- Duration in seconds.
- Keep every regex simple and safe (no nested quantifiers).

Terms of service:
---
{text}
---
"""


def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def _slug(raw_id: str, fallback: str = "campaign") -> str:
    cid = re.sub(r"[^a-z0-9_-]", "-", str(raw_id or "").strip().lower())
    cid = re.sub(r"-{2,}", "-", cid).strip("-_")
    return cid or fallback


def _sanitize(draft: dict, name_hint: str = "") -> dict:
    """Coerce an LLM draft into a validator-safe profile shape."""
    profile = {
        "id": _slug(draft.get("id") or name_hint),
        "name": str(draft.get("name") or name_hint or "Campaign").strip(),
        "strictness": str(draft.get("strictness", "standard")).lower(),
        "duration_s": draft.get("duration_s") or {},
        "banned_exact": [str(x).strip() for x in (draft.get("banned_exact") or []) if str(x).strip()],
        "banned_topics": [],
        "caption_required": {},
        "human_gates": [],
        "curation_note": str(draft.get("curation_note", "")).strip(),
        "unresolved": [str(x) for x in (draft.get("unresolved") or [])],
    }
    if profile["strictness"] not in campaign.VALID_STRICTNESS:
        profile["strictness"] = "standard"

    for topic in draft.get("banned_topics") or []:
        if isinstance(topic, dict) and topic.get("topic"):
            regex = topic.get("regex") or []
            if isinstance(regex, str):
                regex = [regex]
            profile["banned_topics"].append(
                {"topic": str(topic["topic"]),
                 "regex": [str(r).lower() for r in regex]}
            )

    cap = draft.get("caption_required") or {}
    if isinstance(cap, dict):
        profile["caption_required"] = {
            "all_of": [str(t) for t in (cap.get("all_of") or [])],
            "any_of": [str(t) for t in (cap.get("any_of") or [])],
        }

    for gate in draft.get("human_gates") or []:
        if isinstance(gate, dict) and gate.get("id"):
            profile["human_gates"].append({
                "id": str(gate["id"]),
                "text": str(gate.get("text", "Perlu konfirmasi")),
                "trigger": str(gate.get("trigger", "always")),
            })
    return profile


def compile_profile(tos_text: str, *, llm: LLMPort, name_hint: str = "") -> dict:
    """Compile ToS text into a validated profile dict (not yet saved)."""
    if not tos_text or len(tos_text.strip()) < 20:
        raise ValidationError("Teks S&K terlalu pendek untuk dikompilasi")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(text=tos_text[:8000])},
    ]
    content = llm.chat(messages, json_mode=True)
    try:
        draft = _extract_json(content)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"LLM tidak mengembalikan JSON valid: {exc}") from exc
    profile = _sanitize(draft, name_hint)
    campaign.validate_profile(profile)  # hard gate
    return profile


def compile_and_save(tos_text: str, *, llm: LLMPort, name_hint: str = "") -> str:
    profile = compile_profile(tos_text, llm=llm, name_hint=name_hint)
    return campaign.save_profile(profile)
