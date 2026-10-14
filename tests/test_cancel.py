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
