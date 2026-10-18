"""Tests for cooperative cancellation (Phase: cancel) — hermetic, no long work."""

import subprocess
import sys
import time

import pytest

from app.cancel import CancelToken, JobCancelled, cancel_key, clear, get_token, new_token


def test_token_not_cancelled_initially():
    t = new_token("chat1")
    try:
        assert t.cancelled() is False
        t.check()  # no raise
    finally:
        clear("chat1")


def test_token_cancel_flag():
    t = new_token("chat2")
    try:
        t.cancel()
        assert t.cancelled() is True
        with pytest.raises(JobCancelled):
            t.check()
    finally:
        clear("chat2")


def test_registry_get_and_cancel():
    new_token("chat3")
    try:
        assert get_token("chat3") is not None
        assert cancel_key("chat3") is True
        assert get_token("chat3").cancelled() is True
    finally:
        clear("chat3")


def test_cancel_unknown_key_returns_false():
    assert cancel_key("nope-nonexistent") is False


def test_clear_removes_token():
    new_token("chat4")
    clear("chat4")
    assert get_token("chat4") is None


def test_run_kills_process_on_cancel():
    """Start a long subprocess, cancel it, assert it is terminated fast."""
    from app.cancel import run
    t = new_token("chat5")
    try:
        # A process that would run ~10s; we cancel after ~0.3s.
        import threading
        def canceller():
            time.sleep(0.4)
            t.cancel()
        threading.Thread(target=canceller, daemon=True).start()

        start = time.time()
        with pytest.raises((JobCancelled, subprocess.SubprocessError)):
            run([sys.executable, "-c", "import time; time.sleep(10)"],
                token=t, timeout=30)
        elapsed = time.time() - start
        assert elapsed < 5, f"proses tidak dihentikan cepat ({elapsed:.1f}s)"
    finally:
        clear("chat5")


def test_run_without_token_normal():
    from app.cancel import run
    start = time.time()
    proc = run([sys.executable, "-c", "print('hi')"], timeout=10)
    assert proc.returncode == 0
    assert "hi" in proc.stdout
    assert time.time() - start < 5


def test_all_transcriber_adapters_accept_token():
    """Regression: every transcriber driver must accept a `token` kwarg.

    The bot calls transcribe(..., token=...) unconditionally; an adapter that
    forgets the kwarg crashes at runtime (this happened with the whisper
    adapter). Guard every implementation here.
    """
    import inspect
    from app.adapters.fakes import FakeTranscriber
    from app.adapters.transcriber_whisper import WhisperTranscriberAdapter
    from app.media import transcriber as impl

    for fn in (FakeTranscriber.transcribe, WhisperTranscriberAdapter.transcribe,
               impl.transcribe):
        params = inspect.signature(fn).parameters
        assert "token" in params, f"{fn.__qualname__} harus terima 'token'"


def test_fake_transcriber_error_on_cancelled_token():
    from app.adapters.fakes import FakeTranscriber
    from app.cancel import JobCancelled
    t = new_token("x")
    t.cancel()
    try:
        with pytest.raises(JobCancelled):
            FakeTranscriber().transcribe("v.mp4", "/tmp", token=t)
    finally:
        clear("x")


def test_cancel_job_api_and_button_visibility(tmp_path, monkeypatch):
    """Test cancelling queued job via API and verifying UI button display logic."""
    from fastapi.testclient import TestClient
    from app import config, storage
    from app.web import app

    dbp = tmp_path / "app_cancel_test.db"
    monkeypatch.setattr(config, "DB_PATH", dbp)
    monkeypatch.setattr(storage.config, "DB_PATH", dbp)
    from app.db import migrate
    monkeypatch.setattr(migrate.config, "DB_PATH", dbp)
    for modname in ("queue_sqlite", "events_memory"):
        m = __import__(f"app.adapters.{modname}", fromlist=["config"])
        monkeypatch.setattr(m.config, "DB_PATH", dbp)

    storage.init_db()
    client = TestClient(app)

    # 1. Create a queued job
    job = storage.create_job(source_type="url", source_url="https://youtu.be/cancel_test")
    assert job.status == storage.JOB_QUEUED

    # 2. In queued state, 'Batalkan' must be visible and 'Hapus sesi' must be hidden
    resp_page = client.get(f"/jobs/{job.id}")
    assert resp_page.status_code == 200
    assert 'id="btn-cancel"' in resp_page.text
    assert 'style="display: inline-block;"' in resp_page.text
    assert 'id="btn-delete-job" class="btn btn-sm btn-danger" onclick="deleteSession()" style="display: none;"' in resp_page.text

    # 3. Cancel the job via POST /api/jobs/{job_id}/cancel
    resp_cancel = client.post(f"/api/jobs/{job.id}/cancel")
    assert resp_cancel.status_code == 200
    assert resp_cancel.json()["ok"] is True

    # 4. Storage must reflect status 'canceled'
    refreshed = storage.get_job(job.id)
    assert refreshed.status == storage.JOB_CANCELED
    assert refreshed.stage == "canceled"

    # 5. In canceled state, 'Batalkan' must be hidden and 'Hapus sesi' must be visible
    resp_page2 = client.get(f"/jobs/{job.id}")
    assert resp_page2.status_code == 200
    assert 'id="btn-cancel" class="btn btn-sm btn-secondary" onclick="cancelJob()" style="display: none;"' in resp_page2.text
    assert 'id="btn-delete-job" class="btn btn-sm btn-danger" onclick="deleteSession()" style="display: inline-block;"' in resp_page2.text

    # 6. Delete session via POST /api/jobs/{job_id}/delete
    resp_del = client.post(f"/api/jobs/{job.id}/delete")
    assert resp_del.status_code == 200
    assert resp_del.json()["ok"] is True
    assert storage.get_job(job.id) is None

