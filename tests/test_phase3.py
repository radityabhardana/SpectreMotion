"""Phase 3 tests: presets, catalog actions, SSE event replay, hook preview.
Hermetic — temp DB, no network.
"""

import json

import pytest

from app import presets
from app import storage
from app.adapters.events_memory import EventBus
from app.db import migrate
from app.errors import ValidationError


@pytest.fixture()
def db(tmp_path, monkeypatch):
    dbp = tmp_path / "app.db"
    monkeypatch.setattr(migrate.config, "DB_PATH", dbp)
    monkeypatch.setattr(storage.config, "DB_PATH", dbp)
    for modname in ("queue_sqlite", "events_memory"):
        m = __import__(f"app.adapters.{modname}", fromlist=["config"])
        monkeypatch.setattr(m.config, "DB_PATH", dbp)
    migrate.apply()
    storage.init_db()
    return dbp


def test_five_presets_present():
    ids = {p["id"] for p in presets.list_presets()}
    assert {"capcut", "hormozi", "cinematic", "minimal", "neon_badge"} <= ids


def test_get_preset_merges_defaults():
    p = presets.get_preset("hormozi")
    assert p is not None
    assert p["hook"]["font_size"] == 84
    # inherited from defaults
    assert "subtitle" in p and "video" in p


def test_named_template_traversal_rejected():
    with pytest.raises(ValidationError):
        presets.named_template_path("../../etc/passwd")
    with pytest.raises(ValidationError):
        presets.named_template_path("..")


def test_named_template_slugifies():
    p = presets.named_template_path("My Cool Style!")
    assert p.name == "my-cool-style.json"


def test_template_override_roundtrip(db):
    oid = storage.save_template_override("My Style", {"hook": {"font_size": 90}})
    got = storage.get_template_override("My Style")
    assert got is not None and got["props"]["hook"]["font_size"] == 90
    # upsert, not duplicate
    oid2 = storage.save_template_override("My Style", {"hook": {"font_size": 70}})
    assert oid == oid2
    assert storage.get_template_override("My Style")["props"]["hook"]["font_size"] == 70


def test_clip_catalog_fields(db):
    job = storage.create_job(source_type="file", source_name="a.mp4")
    clip = storage.Clip(id="c1", job_id=job.id, idx=0, start_s=0, end_s=10,
                        status="candidate")
    storage.add_clip(job.id, clip)
    storage.update_clip("c1", status="rendered", rendered=True, rank=2)
    got = storage.get_clip("c1")
    assert got.status == "rendered" and got.rendered is True and got.rank == 2


def test_events_replay_supports_last_event_id(db):
    bus = EventBus()
    bus.publish("j1", {"type": "stage", "stage": "probe", "progress": 0.1})
    bus.publish("j1", {"type": "stage", "stage": "render", "progress": 0.7})
    bus.publish("j1", {"type": "job.succeeded"})
    # Client had seen seq=1 (probe); replay should return the rest.
    replayed = list(bus._replay("j1", last_seq=1))
    types = [e["type"] for e in replayed]
    assert types == ["stage", "job.succeeded"]
    assert replayed[0]["stage"] == "render"
