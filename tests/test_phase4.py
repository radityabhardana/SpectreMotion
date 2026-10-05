"""Phase 4 tests: campaign policy depth, compiler (fake LLM), audit trail.
Hermetic — temp dirs, no network.
"""

import json

import pytest

from app import audit, campaign, campaign_compiler
from app.errors import ValidationError


@pytest.fixture()
def ca(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "CAMPAIGNS_DIR", tmp_path)
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    return tmp_path


# ---- Policy depth ---------------------------------------------------------

def _profile(**kw):
    return campaign.CampaignProfile(id="t", **kw)


def test_banned_topics_is_warn_not_fail(ca):
    p = _profile(banned_topics=[{"topic": "judi", "regex": [r"\bjudi\b"]}])
    vs = campaign.evaluate(p, start=0, end=30, text="ini soal judi online")
    codes = {v.code: v.level for v in vs}
    assert codes.get("banned_topics") == "warn"


def test_human_gate_triggers_on_flag(ca):
    p = _profile(human_gates=[{"id": "g", "text": "cek", "trigger": "llm_flag:sensitive"}])
    vs = campaign.evaluate(p, start=0, end=30, text="aman", risk_flags=["sensitive"])
    assert any(v.code == "human_gate" for v in vs)


def test_human_gate_not_triggered_without_flag(ca):
    p = _profile(human_gates=[{"id": "g", "text": "cek", "trigger": "llm_flag:sensitive"}])
    vs = campaign.evaluate(p, start=0, end=30, text="aman")
    assert not any(v.code == "human_gate" for v in vs)


def test_ai_flag_never_fails_even_in_standard(ca):
    p = _profile()
    vs = campaign.evaluate(p, start=0, end=30, text="aman", risk_flags=["x"])
    assert all(v.level != "fail" for v in vs)


def test_strictness_strict_escalates_warnings(ca):
    p = _profile(strictness="strict",
                 caption_required={"all_of": ["#brand"]})
    vs = campaign.evaluate(p, start=0, end=30, text="aman", caption="tanpa tag")
    assert any(v.code == "caption_required" and v.level == "fail" for v in vs)


def test_caption_any_of(ca):
    p = _profile(caption_required={"any_of": ["#fyp", "#viral"]})
    ok = campaign.evaluate(p, start=0, end=30, text="x", caption="hai #fyp")
    assert not any(v.code == "caption_required" for v in ok)
    bad = campaign.evaluate(p, start=0, end=30, text="x", caption="hai")
    assert any(v.code == "caption_required" for v in bad)


def test_validate_rejects_bad_regex(ca):
    with pytest.raises(campaign.CampaignError):
        campaign.validate_profile({"id": "x", "banned_topics": [{"topic": "t", "regex": ["("]}]})


def test_validate_rejects_bad_strictness(ca):
    with pytest.raises(campaign.CampaignError):
        campaign.validate_profile({"id": "x", "strictness": "wild"})


def test_evidence_captured_for_banned_exact(ca):
    p = _profile(banned_exact=["slot"])
    vs = campaign.evaluate(p, start=0, end=30, text="main slot gacor malam ini")
    v = next(v for v in vs if v.code == "banned_exact")
    assert "slot" in v.evidence


# ---- Compiler (fake LLM) --------------------------------------------------

class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, *, json_mode=True):
        return json.dumps(self.payload)


def test_compile_produces_valid_profile(ca):
    draft = {
        "id": "Brand X!", "name": "Brand X", "strictness": "strict",
        "duration_s": {"min": 15, "max": 60},
        "banned_exact": ["kompetitor"],
        "banned_topics": [{"topic": "judi", "regex": ["jud[iy]"]}],
        "caption_required": {"all_of": ["#brandx"]},
        "unresolved": ["klausul aneh"],
    }
    profile = campaign_compiler.compile_profile("S&K panjang sekali "*3,
                                                llm=_FakeLLM(draft), name_hint="x")
    assert profile["id"] == "brand-x"
    assert profile["strictness"] == "strict"
    assert profile["banned_topics"][0]["topic"] == "judi"
    # and it round-trips through save/load
    cid = campaign.save_profile(profile)
    loaded = campaign.load_profile(cid)
    assert loaded is not None and loaded.banned_exact == ["kompetitor"]


def test_compile_normalizes_bad_strictness(ca):
    # A hallucinated strictness is coerced to the safe default, not accepted.
    profile = campaign_compiler.compile_profile(
        "teks cukup panjang disini "*2,
        llm=_FakeLLM({"id": "x", "strictness": "wild"}))
    assert profile["strictness"] == "standard"


def test_compile_rejects_bad_regex_from_llm(ca):
    # A profile with a broken regex must be rejected by validate_profile.
    with pytest.raises(campaign.CampaignError):
        campaign_compiler.compile_profile(
            "teks cukup panjang disini "*2,
            llm=_FakeLLM({"id": "x", "banned_topics": [{"topic": "t", "regex": ["("]}]}))


def test_compile_short_text_rejected(ca):
    with pytest.raises(ValidationError):
        campaign_compiler.compile_profile("pendek", llm=_FakeLLM({}))


# ---- Audit ----------------------------------------------------------------

def test_audit_append_and_tail(ca):
    audit.record("campaign.saved", subject="brand-x")
    audit.record("job.completed", subject="job-1", detail={"clips": 3})
    entries = audit.tail(limit=10)
    assert len(entries) == 2
    assert entries[0]["action"] == "campaign.saved"
    assert entries[1]["detail"]["clips"] == 3
