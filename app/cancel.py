"""Cooperative cancellation for long-running jobs.

A job runs in a worker thread. When the user taps "Batal", we set a flag AND
terminate any child subprocess (ffmpeg/ffprobe/yt-dlp) so the work actually
stops instead of finishing and sending the clip anyway.

Usage in a pipeline:
    token = new_token(job_id)
    ...
    check(token)            # raises JobCancelled if the user cancelled
    run_subprocess(cmd, token=token)   # killed mid-flight on cancel
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("spectre.cancel")


class JobCancelled(Exception):
    """Raised when a job is cancelled by the user."""


@dataclass
class CancelToken:
    job_id: str
    _event: threading.Event = field(default_factory=threading.Event)
    _procs: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        # Kill any child process currently running for this job.
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    log.info("Menghentikan subprocess (job %s, pid %s)", self.job_id[:8], proc.pid)
            except Exception as exc:  # noqa: BLE001
                log.warning("Gagal terminate proc: %s", exc)

    def track(self, proc) -> None:
        with self._lock:
            self._procs.append(proc)

    def untrack(self, proc) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def check(self) -> None:
        if self._event.is_set():
            raise JobCancelled(f"Job {self.job_id[:8]} dibatalkan")


# Registry of active tokens by job key (usually chat_id), so the bot can cancel.
_registry: dict[str, CancelToken] = {}
_reg_lock = threading.Lock()


def new_token(key: str) -> CancelToken:
    token = CancelToken(job_id=key)
    with _reg_lock:
        _registry[key] = token
    return token


def get_token(key: str) -> Optional[CancelToken]:
    with _reg_lock:
        return _registry.get(key)


def cancel_key(key: str) -> bool:
    """Cancel an active job by key (e.g. chat id). Returns True if found."""
    with _reg_lock:
        token = _registry.get(key)
    if token is None:
        return False
    token.cancel()
    return True


def clear(key: str) -> None:
    with _reg_lock:
        _registry.pop(key, None)


def run(cmd: list[str], *, token: Optional[CancelToken] = None,
        timeout: float = 1200, text: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, tracked for cancellation.

    If the token is cancelled while the process runs, it is terminated and
    JobCancelled is raised.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=text)
    if token is not None:
        token.track(proc)
        if token.cancelled():
            proc.terminate()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        raise
    finally:
        if token is not None:
            token.untrack(proc)
    if token is not None and token.cancelled():
        raise JobCancelled(f"proses dibatalkan (rc={proc.returncode})")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
