"""SQLite-backed storage for jobs and clips.

Deliberately small and dependency-free (stdlib `sqlite3`). The schema is kept
migration-friendly: bump `SCHEMA_VERSION` and add an ALTER in `_migrate` when
you need Postgres later — the accessors below are the only thing the rest of
the app touches, so swapping the backend later is a contained change.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app import config

SCHEMA_VERSION = 1

# Job lifecycle: queued -> running -> done | failed
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Ensure the schema exists by running the migration runner.

    Single source of truth: migrations own the schema. This used to duplicate
    CREATE TABLEs, which drifted from migrations (missing columns like
    user_id). Delegating avoids that class of bug entirely.
    """
    config.ensure_dirs()
    from app.db import migrate
    migrate.ensure_schema()


@dataclass
class Clip:
    id: str
    job_id: str
    idx: int
    start_s: float
    end_s: float
    viral_score: int = 0
    hook: str = ""
    topic: str = ""
    caption: str = ""
    reason: str = ""
    risk_flags: list[str] = field(default_factory=list)
    verdict: str = "pass"
    verdict_msg: str = ""
    output_path: str = ""
    rendered: bool = False
    status: str = "candidate"
    rank: int = 0
    thumbnail_path: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def duration(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass
class Job:
    id: str
    status: str
    stage: str = ""
    progress: float = 0.0
    source_type: str = "file"
    source_name: str = ""
    source_path: str = ""
    source_url: str = ""
    campaign_id: str = "none"
    template_path: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    message: str = ""
    user_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        status=row["status"],
        stage=row["stage"],
        progress=row["progress"],
        source_type=row["source_type"],
        source_name=row["source_name"],
        source_path=row["source_path"],
        source_url=row["source_url"],
        campaign_id=row["campaign_id"],
        template_path=row["template_path"],
        settings=json.loads(row["settings_json"] or "{}"),
        error=row["error"],
        message=row["message"],
        user_id=row["user_id"] if "user_id" in row.keys() else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _clip_from_row(row: sqlite3.Row) -> Clip:
    return Clip(
        id=row["id"],
        job_id=row["job_id"],
        idx=row["idx"],
        start_s=row["start_s"],
        end_s=row["end_s"],
        viral_score=row["viral_score"],
        hook=row["hook"],
        topic=row["topic"],
        caption=row["caption"],
        reason=row["reason"],
        risk_flags=json.loads(row["risk_flags"] or "[]"),
        verdict=row["verdict"],
        verdict_msg=row["verdict_msg"],
        output_path=row["output_path"],
        rendered=bool(row["rendered"]),
        status=row["status"] if "status" in row.keys() else "candidate",
        rank=row["rank"] if "rank" in row.keys() else 0,
        thumbnail_path=row["thumbnail_path"] if "thumbnail_path" in row.keys() else "",
        created_at=row["created_at"],
    )


def create_job(
    *,
    source_type: str,
    source_name: str = "",
    source_path: str = "",
    source_url: str = "",
    campaign_id: str = "none",
    template_path: str = "",
    settings: Optional[dict[str, Any]] = None,
    user_id: Optional[int] = None,
) -> Job:
    now = time.time()
    job = Job(
        id=uuid.uuid4().hex,
        status=JOB_QUEUED,
        source_type=source_type,
        source_name=source_name,
        source_path=source_path,
        source_url=source_url,
        campaign_id=campaign_id,
        template_path=template_path,
        settings=settings or {},
        created_at=now,
        updated_at=now,
    )
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO jobs
               (id, created_at, updated_at, status, stage, progress, source_type,
                source_name, source_path, source_url, campaign_id, template_path,
                settings_json, error, message, user_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job.id, job.created_at, job.updated_at, job.status, job.stage,
                job.progress, job.source_type, job.source_name, job.source_path,
                job.source_url, job.campaign_id, job.template_path,
                json.dumps(job.settings), job.error, job.message, user_id,
            ),
        )
        conn.commit()
    return job


def get_job(job_id: str) -> Optional[Job]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def list_jobs(limit: int = 100) -> list[Job]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_job_from_row(r) for r in rows]


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status", "stage", "progress", "error", "message",
        "campaign_id", "template_path", "source_path",
    }
    cols, vals = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "settings":
            key, value = "settings_json", json.dumps(value)
        if key == "settings_json":
            pass
        cols.append(f"{key}=?")
        vals.append(value)
    if not cols:
        return
    cols.append("updated_at=?")
    vals.append(time.time())
    vals.append(job_id)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)
        conn.commit()


def claim_next_job() -> Optional[Job]:
    """Atomically claim the oldest queued job (safe across processes)."""
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at LIMIT 1",
            (JOB_QUEUED,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE jobs SET status=?, stage=?, updated_at=? WHERE id=?",
            (JOB_RUNNING, "starting", time.time(), row["id"]),
        )
        conn.commit()
        job = _job_from_row(row)
    job.status = JOB_RUNNING
    return job


# ---- Clips ----

def add_clip(job_id: str, clip: Clip) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO clips
               (id, job_id, idx, created_at, start_s, end_s, viral_score, hook,
                topic, caption, reason, risk_flags, verdict, verdict_msg,
                output_path, rendered)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                clip.id, job_id, clip.idx, clip.created_at, clip.start_s, clip.end_s,
                clip.viral_score, clip.hook, clip.topic, clip.caption, clip.reason,
                json.dumps(clip.risk_flags), clip.verdict, clip.verdict_msg,
                clip.output_path, 1 if clip.rendered else 0,
            ),
        )
        conn.commit()


def replace_clips(job_id: str, clips: Iterable[Clip]) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM clips WHERE job_id=?", (job_id,))
        for clip in clips:
            conn.execute(
                """INSERT INTO clips
                   (id, job_id, idx, created_at, start_s, end_s, viral_score, hook,
                    topic, caption, reason, risk_flags, verdict, verdict_msg,
                    output_path, rendered)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    clip.id, job_id, clip.idx, clip.created_at, clip.start_s,
                    clip.end_s, clip.viral_score, clip.hook, clip.topic,
                    clip.caption, clip.reason, json.dumps(clip.risk_flags),
                    clip.verdict, clip.verdict_msg, clip.output_path,
                    1 if clip.rendered else 0,
                ),
            )
        conn.commit()


def get_clip(clip_id: str) -> Optional[Clip]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    return _clip_from_row(row) if row else None


def list_clips(job_id: str) -> list[Clip]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM clips WHERE job_id=? ORDER BY idx", (job_id,)
        ).fetchall()
    return [_clip_from_row(r) for r in rows]


def update_clip(clip_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "hook", "topic", "caption", "verdict", "verdict_msg",
        "output_path", "rendered", "risk_flags", "viral_score",
        "status", "rank", "thumbnail_path",
    }
    cols, vals = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "risk_flags":
            value = json.dumps(value)
        if key == "rendered":
            value = 1 if value else 0
        cols.append(f"{key}=?")
        vals.append(value)
    if not cols:
        return
    vals.append(clip_id)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE clips SET {', '.join(cols)} WHERE id=?", vals)
        conn.commit()


# ---- Template overrides (custom presets) ----

def list_template_overrides() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM template_overrides ORDER BY updated_at DESC"
        ).fetchall()
    return [
        {
            "id": r["id"], "preset_id": r["preset_id"], "name": r["name"],
            "props": json.loads(r["props_json"] or "{}"),
            "is_default": bool(r["is_default"]),
        }
        for r in rows
    ]


def save_template_override(name: str, props: dict[str, Any],
                           preset_id: str = "custom") -> str:
    now = time.time()
    with _lock, _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM template_overrides WHERE name=?", (name,)
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE template_overrides SET props_json=?, preset_id=?, updated_at=? "
                "WHERE id=?",
                (json.dumps(props), preset_id, now, existing["id"]),
            )
            conn.commit()
            return existing["id"]
        oid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO template_overrides(id, preset_id, name, props_json, "
            "is_default, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (oid, preset_id, name, json.dumps(props), 0, now, now),
        )
        conn.commit()
    return oid


def get_template_override(name: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM template_overrides WHERE name=?", (name,)
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "preset_id": row["preset_id"],
            "props": json.loads(row["props_json"] or "{}")}
