"""Tests for Session / History System and Total Deletion."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, storage
from app.web import app


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect ALL storage paths to a temp dir so tests never touch the real
    data/app.db. Point every module that reads DB_PATH at the temp DB too."""
    dbp = tmp_path / "app.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", dbp)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "TRANSCRIPT_CACHE_DIR", tmp_path / "cache")
    for d in (config.UPLOAD_DIR, config.OUTPUT_DIR, config.WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Modules that connect with their own DB_PATH
    monkeypatch.setattr(storage.config, "DB_PATH", dbp)
    from app.db import migrate
    monkeypatch.setattr(migrate.config, "DB_PATH", dbp)
    for modname in ("queue_sqlite", "events_memory"):
        m = __import__(f"app.adapters.{modname}", fromlist=["config"])
        monkeypatch.setattr(m.config, "DB_PATH", dbp)
    yield


client = TestClient(app)


def test_list_sessions_and_stats():
    storage.init_db()
    job = storage.create_job(
        source_type="url",
        source_name="Testing History Session",
        source_url="https://youtu.be/sample123",
    )
    c1 = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=0, start_s=0.0, end_s=25.0, rendered=True, status="rendered")
    c2 = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=1, start_s=25.0, end_s=50.0, rendered=False, status="idle")
    c3 = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=2, start_s=50.0, end_s=75.0, rendered=True, status="rendered")
    storage.replace_clips(job.id, [c1, c2, c3])

    sessions = storage.list_sessions_with_stats(limit=50)
    found = next((s for s in sessions if s["id"] == job.id), None)
    assert found is not None
    assert found["total_clips"] == 3
    assert found["rendered_clips"] == 2


def test_delete_job_and_assets():
    storage.init_db()
    job = storage.create_job(
        source_type="file",
        source_name="delete_test.mp4",
    )
    # Create a dummy source file
    source_file = config.UPLOAD_DIR / f"{job.id}_source.mp4"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"dummy source video content")
    storage.update_job(job.id, source_path=str(source_file))

    # Create dummy output clips
    out_file1 = config.OUTPUT_DIR / f"{job.id}_clip1.mp4"
    out_file1.parent.mkdir(parents=True, exist_ok=True)
    out_file1.write_bytes(b"dummy clip 1")

    out_file2 = config.OUTPUT_DIR / f"{job.id}_clip2.mp4"
    out_file2.write_bytes(b"dummy clip 2")

    c1 = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=0, start_s=0.0, end_s=20.0, output_path=str(out_file1), rendered=True)
    c2 = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=1, start_s=20.0, end_s=40.0, output_path=str(out_file2), rendered=True)
    storage.replace_clips(job.id, [c1, c2])

    assert source_file.is_file()
    assert out_file1.is_file()
    assert out_file2.is_file()

    # Perform total deletion
    res = storage.delete_job_and_assets(job.id)
    assert res["ok"] is True
    assert res["deleted_files"] >= 3

    # Assert files are gone
    assert not source_file.is_file()
    assert not out_file1.is_file()
    assert not out_file2.is_file()

    # Assert database records are gone
    assert storage.get_job(job.id) is None
    assert len(storage.list_clips(job.id)) == 0


def test_history_and_delete_endpoints():
    storage.init_db()
    job = storage.create_job(
        source_type="url",
        source_name="API Delete Test",
        source_url="https://youtu.be/api_test",
    )
    clip = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=0, start_s=0.0, end_s=30.0, hook="Test hook", rendered=False)
    storage.replace_clips(job.id, [clip])

    # 1. GET /history HTML
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "Riwayat sesi" in resp.text

    # 2. GET /api/history JSON
    resp = client.get("/api/history")
    assert resp.status_code == 200
    data = resp.json()
    assert any(s["id"] == job.id for s in data)

    # 3. POST /api/clips/{clip_id}/render
    resp = client.post(f"/api/clips/{clip.id}/render")
    assert resp.status_code == 200
    c_updated = storage.get_clip(clip.id)
    assert c_updated.status == "rendering"

    # 4. POST /api/jobs/{job.id}/delete
    resp = client.post(f"/api/jobs/{job.id}/delete")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert storage.get_job(job.id) is None


def test_render_clip_with_custom_style_overrides():
    storage.init_db()
    job = storage.create_job(
        source_type="file",
        source_name="style_test.mp4",
    )
    clip = storage.Clip(id=uuid.uuid4().hex, job_id=job.id, idx=0, start_s=0.0, end_s=20.0, hook="Test hook", rendered=False)
    storage.replace_clips(job.id, [clip])

    custom_payload = {
        "force": True,
        "style_overrides": {
            "preset_id": "hormozi",
            "position": "center",
            "subtitle": {
                "font_name": "Impact",
                "font_size": 84,
                "highlight_color": "#00FF88",
                "words_per_phrase": 2,
                "text_transform": "upper",
                "mode": "karaoke",
            },
            "video": {
                "face_tracking": True,
                "dynamic_zoom": True,
            }
        }
    }
    resp = client.post(f"/api/clips/{clip.id}/render", json=custom_payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Verify the child render job got enqueued with the style overrides
    queue_job_id = resp.json()["job_id"]
    child_job = storage.get_job(queue_job_id)
    assert child_job is not None
    payload = (child_job.settings or {}).get("payload", {})
    assert payload.get("style_overrides", {}).get("preset_id") == "hormozi"
    assert payload["style_overrides"]["subtitle"]["font_name"] == "Impact"


def test_create_job_auto_render_flag():
    storage.init_db()
    # 1. Default (auto_render=false for 2short.ai mode)
    resp = client.post("/api/jobs", data={"source_type": "url", "url": "https://youtu.be/test2short", "auto_render": "false"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    job = storage.get_job(job_id)
    assert job.settings.get("auto_render") is False

    # 2. When auto_render=true
    resp2 = client.post("/api/jobs", data={"source_type": "url", "url": "https://youtu.be/test_autorender", "auto_render": "true"})
    assert resp2.status_code == 200
    job_id2 = resp2.json()["job_id"]
    job2 = storage.get_job(job_id2)
    assert job2.settings.get("auto_render") is True


def test_batch_delete_jobs_and_clips():
    storage.init_db()

    # 1. Create two jobs with files and clips
    j1 = storage.create_job(source_type="file", source_name="batch1.mp4")
    j2 = storage.create_job(source_type="file", source_name="batch2.mp4")

    c1 = storage.Clip(id=uuid.uuid4().hex, job_id=j1.id, idx=0, start_s=0.0, end_s=10.0, hook="Clip 1", rendered=False)
    c2 = storage.Clip(id=uuid.uuid4().hex, job_id=j1.id, idx=1, start_s=10.0, end_s=20.0, hook="Clip 2", rendered=False)
    c3 = storage.Clip(id=uuid.uuid4().hex, job_id=j2.id, idx=0, start_s=0.0, end_s=10.0, hook="Clip 3", rendered=False)
    storage.replace_clips(j1.id, [c1, c2])
    storage.replace_clips(j2.id, [c3])

    # Test clip batch delete via API
    resp_clip = client.post("/api/clips/batch-delete", json={"clip_ids": [c1.id, c2.id]})
    assert resp_clip.status_code == 200
    assert resp_clip.json()["ok"] is True
    assert resp_clip.json()["deleted_clips"] == 2
    assert storage.get_clip(c1.id) is None
    assert storage.get_clip(c2.id) is None
    assert storage.get_clip(c3.id) is not None

    # Test job batch delete via API
    resp_job = client.post("/api/jobs/batch-delete", json={"job_ids": [j1.id, j2.id]})
    assert resp_job.status_code == 200
    assert resp_job.json()["ok"] is True
    assert resp_job.json()["deleted_jobs"] == 2
    assert storage.get_job(j1.id) is None
    assert storage.get_job(j2.id) is None
    assert storage.get_clip(c3.id) is None


def test_job_page_renders_sound_button_and_caption_copy():
    storage.init_db()
    job = storage.create_job(
        source_type="url",
        source_name="Caption & Sound Test",
        source_url="https://youtu.be/sample_caption",
    )
    clip = storage.Clip(
        id=uuid.uuid4().hex,
        job_id=job.id,
        idx=0,
        start_s=0.0,
        end_s=15.0,
        hook="Hook viral keren",
        caption="Ini caption viral siap copypaste ke TikTok #fyp",
        rendered=False,
        status="idle",
    )
    storage.replace_clips(job.id, [clip])

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    html = resp.text

    # 1. Preview video: no hardcoded muted attribute, sound button exists
    assert 'id="ed-sound-btn"' in html
    assert 'toggleEditorSound()' in html
    assert '<video id="ed-video" playsinline loop' in html
    assert '<video id="ed-video" playsinline muted loop' not in html

    # 2. Caption copy button
    assert 'class="clip-caption"' in html
    assert 'class="copy-btn"' in html
    assert 'copyText(this,' in html
    assert 'Ini caption viral siap copypaste ke TikTok #fyp' in html

    # 3. Face track toggle button in preview toolbar
    assert 'id="ed-facetrack-btn"' in html
    assert 'toggleFaceTracking()' in html
    assert '👤 Face Track: ON' in html


def test_facetrack_endpoint_and_preset_default():
    from app import presets
    assert presets.DEFAULT_HOOK_TEMPLATE["video"]["face_tracking"] is True

    storage.init_db()
    job = storage.create_job(
        source_type="file",
        source_name="facetrack_test.mp4",
    )
    clip = storage.Clip(
        id=uuid.uuid4().hex,
        job_id=job.id,
        idx=0,
        start_s=0.0,
        end_s=10.0,
        hook="Face track hook",
        rendered=False,
    )
    storage.replace_clips(job.id, [clip])

    resp = client.get(f"/api/clips/{clip.id}/facetrack")
    assert resp.status_code == 200
    data = resp.json()
    assert "found" in data
    assert "keyframes" in data


def test_dead_sessions_purged_and_excluded_from_history():
    storage.init_db()

    # 1. Create a successful session
    job_ok = storage.create_job(source_type="url", source_name="Valid Video", source_url="https://youtu.be/valid")
    storage.update_job(job_ok.id, status=storage.JOB_DONE, stage="done")

    # 2. Create a failed session
    job_failed = storage.create_job(source_type="url", source_name="Failed Video", source_url="https://youtu.be/failed")
    storage.update_job(job_failed.id, status=storage.JOB_FAILED, stage="failed", error="Download error")

    # 3. Create a canceled session
    job_canceled = storage.create_job(source_type="url", source_name="Canceled Video", source_url="https://youtu.be/canceled")
    storage.update_job(job_canceled.id, status=storage.JOB_CANCELED, stage="canceled")

    # Listing sessions should purge failed & canceled and only return the valid one
    sessions = storage.list_sessions_with_stats()
    session_ids = [s["id"] for s in sessions]
    assert job_ok.id in session_ids
    assert job_failed.id not in session_ids
    assert job_canceled.id not in session_ids

    # Dead jobs should be completely deleted from the database
    assert storage.get_job(job_failed.id) is None
    assert storage.get_job(job_canceled.id) is None

    # HTML page and JSON endpoint must not contain failed or canceled sessions
    resp_html = client.get("/history")
    assert resp_html.status_code == 200
    assert "Failed Video" not in resp_html.text
    assert "Canceled Video" not in resp_html.text
    assert "Valid Video" in resp_html.text

    resp_json = client.get("/api/history")
    assert resp_json.status_code == 200
    json_ids = [s["id"] for s in resp_json.json()]
    assert job_ok.id in json_ids
    assert job_failed.id not in json_ids
    assert job_canceled.id not in json_ids



