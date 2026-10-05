"""Deterministic campaign/compliance engine.

No LLM here — rules are plain code so results are reproducible and auditable.
A campaign profile:

    {
      "id": "brand-x",
      "name": "Brand X S&K",
      "strictness": "standard",              # loose | standard | strict
      "duration_s": {"min": 15, "max": 60},
      "banned_exact": ["brand lain"],
      "banned_topics": [{"topic": "judi", "regex": ["\\bjudi\\b", "slot"]}],
      "caption_required": {"all_of": ["#brandx"], "any_of": ["#fyp", "#viral"]},
      "human_gates": [{"id": "sensitive", "text": "Cek topik sensitif",
                       "trigger": "llm_flag:sensitive"}],
      "curation_note": "keep it upbeat"
    }

Verdict levels (worst wins): info < pass < warn < fail.
Key principle: AI risk flags and human gates can NEVER produce a hard `fail`;
only deterministic rules (duration / banned_exact) can. The LLM flags, humans
decide.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app import config

log = logging.getLogger("spectre.campaign")

CAMPAIGNS_DIR = config.DATA_DIR / "campaigns"

LEVEL_RANK = {"info": 0, "pass": 1, "warn": 2, "fail": 3}
VALID_STRICTNESS = {"loose", "standard", "strict"}


class CampaignError(ValueError):
    pass


@dataclass
class Verdict:
    level: str  # info | pass | warn | fail
    code: str
    message: str
    evidence: str = ""


@dataclass
class CampaignProfile:
    id: str
    name: str = ""
    strictness: str = "standard"
    duration_s: dict = field(default_factory=dict)
    banned_exact: list[str] = field(default_factory=list)
    banned_topics: list[dict] = field(default_factory=list)
    caption_required: dict = field(default_factory=dict)
    human_gates: list[dict] = field(default_factory=list)
    curation_note: str = ""
    raw: dict = field(default_factory=dict)


# ---- Text helpers ---------------------------------------------------------

def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


# ---- Validation -----------------------------------------------------------

def validate_profile(raw: dict) -> dict:
    """Validate + normalize a profile. Raises CampaignError on bad input."""
    if not isinstance(raw, dict):
        raise CampaignError("profil harus berupa object JSON")
    strictness = str(raw.get("strictness", "standard")).lower()
    if strictness not in VALID_STRICTNESS:
        raise CampaignError(f"strictness tidak valid: {strictness!r}")

    dur = raw.get("duration_s") or {}
    if not isinstance(dur, dict):
        raise CampaignError("duration_s harus object {min,max}")
    for key in ("min", "max"):
        if key in dur and dur[key] is not None:
            try:
                float(dur[key])
            except (TypeError, ValueError):
                raise CampaignError(f"duration_s.{key} bukan angka")

    for topic in raw.get("banned_topics") or []:
        if not isinstance(topic, dict):
            raise CampaignError("banned_topics item harus object")
        for pattern in topic.get("regex") or []:
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise CampaignError(f"regex tidak valid {pattern!r}: {exc}")

    for gate in raw.get("human_gates") or []:
        if not isinstance(gate, dict) or not gate.get("id"):
            raise CampaignError("human_gate butuh 'id'")

    return raw


# ---- Load / save ----------------------------------------------------------

def _load_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def list_campaigns() -> list[dict]:
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for path in sorted(CAMPAIGNS_DIR.glob("*.json")):
        try:
            raw = _load_raw(path)
            out.append({"id": raw.get("id", path.stem),
                        "name": raw.get("name", path.stem),
                        "strictness": raw.get("strictness", "standard")})
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Skipping bad campaign %s: %s", path.name, exc)
    return out


def load_profile(campaign_id: str) -> Optional[CampaignProfile]:
    if not campaign_id or campaign_id in ("none", "bebas"):
        return None
    path = CAMPAIGNS_DIR / f"{campaign_id}.json"
    if not path.is_file():
        return None
    raw = _load_raw(path)
    return CampaignProfile(
        id=raw.get("id", campaign_id),
        name=raw.get("name", campaign_id),
        strictness=str(raw.get("strictness", "standard")).lower(),
        duration_s=raw.get("duration_s") or {},
        banned_exact=[str(x) for x in (raw.get("banned_exact") or [])],
        banned_topics=[dict(x) for x in (raw.get("banned_topics") or [])],
        caption_required=raw.get("caption_required") or {},
        human_gates=[dict(x) for x in (raw.get("human_gates") or [])],
        curation_note=raw.get("curation_note", ""),
        raw=raw,
    )


def save_profile(raw: dict) -> str:
    validate_profile(raw)
    cid = re.sub(r"[^a-z0-9_-]", "-", str(raw.get("id", "")).strip().lower())
    cid = re.sub(r"-{2,}", "-", cid).strip("-_")
    if not cid:
        raise CampaignError("campaign id wajib diisi")
    raw["id"] = cid
    CAMPAIGNS_DIR.mkdir(parents=True, exist_ok=True)
    path = CAMPAIGNS_DIR / f"{cid}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return cid


# ---- Evaluation -----------------------------------------------------------

def _evidence(text: str, term: str, width: int = 60) -> str:
    idx = normalize_text(text).find(normalize_text(term))
    if idx < 0:
        return ""
    start = max(0, idx - width)
    return normalize_text(text)[start: idx + len(term) + width]


def evaluate(
    profile: Optional[CampaignProfile],
    *,
    start: float,
    end: float,
    text: str,
    caption: str = "",
    risk_flags: Iterable[str] = (),
) -> list[Verdict]:
    """Evaluate one clip. Deterministic rules can `fail`; AI flags only `warn`."""
    if profile is None:
        return []

    verdicts: list[Verdict] = []
    duration = max(0.0, end - start)
    norm = normalize_text(text)

    # Duration (hard).
    dmin = profile.duration_s.get("min")
    dmax = profile.duration_s.get("max")
    if dmin and duration < float(dmin):
        verdicts.append(Verdict("fail", "duration_min",
                                f"Durasi {duration:.0f}s < minimum {dmin}s"))
    if dmax and duration > float(dmax):
        verdicts.append(Verdict("fail", "duration_max",
                                f"Durasi {duration:.0f}s > maksimum {dmax}s"))

    # Banned exact words (hard).
    for term in profile.banned_exact:
        pattern = r"(?<!\w)" + re.escape(normalize_text(term)) + r"(?!\w)"
        if re.search(pattern, norm):
            verdicts.append(Verdict("fail", "banned_exact",
                                    f"Kata terlarang: '{term}'",
                                    evidence=_evidence(text, term)))

    # Banned topics (regex; warn only — negation/elision is common in language).
    for topic in profile.banned_topics:
        for pattern in topic.get("regex") or []:
            m = re.search(str(pattern), norm)
            if m:
                verdicts.append(Verdict("warn", "banned_topics",
                                        f"Topik sensitif '{topic.get('topic','?')}' terdeteksi",
                                        evidence=_evidence(text, m.group(0))))
                break

    # Caption requirements (warn — correctable post-edit).
    req = profile.caption_required or {}
    all_of = req.get("all_of") or []
    any_of = req.get("any_of") or []
    cap_norm = normalize_text(caption)
    missing_all = [t for t in all_of if normalize_text(t) not in cap_norm]
    if missing_all:
        verdicts.append(Verdict("warn", "caption_required",
                                "Caption kurang: " + ", ".join(missing_all)))
    if any_of and not any(normalize_text(t) in cap_norm for t in any_of):
        verdicts.append(Verdict("warn", "caption_required",
                                "Caption harus memuat salah satu: " + ", ".join(any_of)))

    # AI risk flags (warn only — the LLM has no veto).
    for flag in risk_flags:
        verdicts.append(Verdict("warn", "llm_flag", f"AI menandai risiko: {flag}"))

    # Human gates triggered by specific flags.
    for gate in profile.human_gates:
        trigger = str(gate.get("trigger", ""))
        if trigger.startswith("llm_flag:"):
            wanted = trigger.split(":", 1)[1]
            if wanted in set(risk_flags):
                verdicts.append(Verdict("warn", "human_gate",
                                        gate.get("text", "Perlu konfirmasi manusia")))

    # Strictness escalates soft warnings in `strict` mode.
    if profile.strictness == "strict":
        verdicts = [Verdict("fail" if v.level == "warn" else v.level, v.code,
                            v.message, v.evidence) for v in verdicts]

    if not verdicts:
        verdicts.append(Verdict("pass", "ok", "Lolos semua aturan"))
    return verdicts


def worst_level(verdicts: Iterable[Verdict]) -> str:
    level = "pass"
    for v in verdicts:
        if LEVEL_RANK.get(v.level, 0) > LEVEL_RANK.get(level, 0):
            level = v.level
    return level


def summarize(verdicts: list[Verdict]) -> str:
    return "; ".join(v.message for v in verdicts if v.level != "pass")
