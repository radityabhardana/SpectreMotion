"""Tests for the deterministic campaign engine (no network, no media)."""

import json

from app import campaign


def test_duration_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "CAMPAIGNS_DIR", tmp_path)
    profile = campaign.CampaignProfile(id="t", duration_s={"min": 15, "max": 60})
    verdicts = campaign.evaluate(profile, start=0, end=5, text="halo dunia")
    levels = {v.code: v.level for v in verdicts}
    assert levels["duration_min"] == "fail"


def test_banned_exact(tmp_path):
    profile = campaign.CampaignProfile(id="t", banned_exact=["judi"])
    verdicts = campaign.evaluate(profile, start=0, end=30, text="ini tentang judi online")
    assert any(v.code == "banned_exact" and v.level == "fail" for v in verdicts)


def test_banned_exact_word_boundary():
    profile = campaign.CampaignProfile(id="t", banned_exact=["slot"])
    ok = campaign.evaluate(profile, start=0, end=30, text="slotted screwdriver")
    assert not any(v.code == "banned_exact" for v in ok)


def test_caption_required_is_warn():
    profile = campaign.CampaignProfile(id="t", caption_required={"all_of": ["#fyp"]})
    verdicts = campaign.evaluate(profile, start=0, end=30, text="halo", caption="tanpa tag")
    assert any(v.code == "caption_required" and v.level == "warn" for v in verdicts)


def test_no_profile_passes():
    assert campaign.evaluate(None, start=0, end=30, text="apa saja") == []


def test_worst_level():
    vs = [
        campaign.Verdict("pass", "a", ""),
        campaign.Verdict("warn", "b", ""),
        campaign.Verdict("fail", "c", ""),
    ]
    assert campaign.worst_level(vs) == "fail"


def test_save_and_load_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "CAMPAIGNS_DIR", tmp_path)
    cid = campaign.save_profile({"id": "Brand X!", "name": "Brand X", "banned_exact": ["x"]})
    assert cid == "brand-x"
    loaded = campaign.load_profile("brand-x")
    assert loaded is not None
    assert loaded.banned_exact == ["x"]
