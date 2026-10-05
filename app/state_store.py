"""Git-backed persistence helpers for ephemeral (GitHub Actions) runs.

On Actions the filesystem is thrown away after every job, so anything that must
survive (the SQLite DB, transcript cache) has to be committed back to the repo
and restored on the next run. This module centralizes that:

    restore()  -> pull the persisted tree out of git (best-effort)
    persist()  -> commit + push the persisted paths (best-effort, never raises)

It is deliberately a no-op when GIT_PERSIST=0 (local runs), so the same code
works locally and in CI.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from app import config

log = logging.getLogger("spectre.state")


def _git(args: list[str], cwd: Path, *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=120, check=check)


def _repo_root() -> Path:
    return config.REPO_ROOT


def _configure_identity() -> None:
    """Ensure git has a commit identity inside CI (it usually does not)."""
    name = _git(["config", "user.name"], _repo_root())
    if name.returncode != 0 or not name.stdout.strip():
        _git(["config", "user.name", "spectremotion-bot"], _repo_root())
        _git(["config", "user.email", "bot@spectremotion.local"], _repo_root())


def restore() -> None:
    """Best-effort: ensure the working tree has the latest persisted state.

    In CI the checkout already contains whatever was committed last run, so
    there is usually nothing to do. Kept as a seam for future remotes.
    """
    if not config.GIT_PERSIST:
        return
    root = _repo_root()
    if not (root / ".git").exists():
        log.warning("GIT_PERSIST aktif tapi bukan git repo; skip restore")
        return
    # Nothing to fetch by default (single-branch checkout carries last state).
    log.info("State restore: menggunakan snapshot dari checkout")


def persist(message: Optional[str] = None, *, push: Optional[bool] = None) -> bool:
    """Commit the persisted paths and (optionally) push. Never raises."""
    if not config.GIT_PERSIST:
        return False
    root = _repo_root()
    if not (root / ".git").exists():
        log.warning("GIT_PERSIST aktif tapi bukan git repo; skip persist")
        return False

    _configure_identity()
    paths = [str(config.DB_PATH.relative_to(root)) if config.DB_PATH.is_absolute()
             and root in config.DB_PATH.parents else str(config.DB_PATH)]
    rel: list[str] = []
    for p in (config.DB_PATH, config.TRANSCRIPT_CACHE_DIR):
        try:
            rel.append(str(p.relative_to(root)))
        except ValueError:
            rel.append(str(p))
    try:
        _git(["add", "--force", *rel], root)
        diff = _git(["diff", "--cached", "--quiet"], root)
        if diff.returncode == 0:
            log.info("State persist: tidak ada perubahan")
            return False
        msg = message or "chore(state): update persisted state [skip ci]"
        _git(["commit", "-m", msg], root)
        do_push = config.GIT_PUSH if push is None else push
        if do_push:
            res = _git(["push"], root)
            if res.returncode != 0:
                log.warning("git push gagal: %s", res.stderr.strip()[:200])
                return False
        log.info("State persist: committed %s", rel)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("State persist gagal: %s", exc)
        return False
