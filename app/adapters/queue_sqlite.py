"""SQLite-backed queue: lease/heartbeat/requeue + job state machine.

Reimplements the claim logic that used to live ad-hoc in app/storage.py, but
behind the QueuePort protocol so a Redis/Postgres driver can swap in later.
`claim()` uses BEGIN IMMEDIATE for a cross-process-safe claim.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Optional

from app import config
from app.errors import BackendError
from app.ports import (
    LANE_HEAVY,
    QUEUE_CANCELED,
    QUEUE_FAILED,
    QUEUE_LEASED,
    QUEUE_QUEUED,
    QUEUE_RUNNING,
    QUEUE_SUCCEEDED,
    JobLease,
)

log = logging.getLogger("spectre.queue")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class SqliteQueue:
    """QueuePort implementation on the `jobs` table."""

    def enqueue(
        self, kind: str, payload: dict[str, Any], *,
        dedupe_key: Optional[str] = None, priority: int = 5,
        lane: str = LANE_HEAVY, max_attempts: int = 3,
    ) -> str:
        now = time.time()
        job_id = uuid.uuid4().hex
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if dedupe_key is not None:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE settings_json LIKE ? AND status IN (?,?,?) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (f'%"{dedupe_key}"%', QUEUE_QUEUED, QUEUE_LEASED, QUEUE_RUNNING),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return row["id"]
            conn.execute(
                """INSERT INTO jobs
                   (id, created_at, updated_at, status, stage, progress, source_type,
                    settings_json, campaign_id, template_path, lane, priority,
                    attempt, max_attempts, available_at, error, message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, now, now, QUEUE_QUEUED, "queued", 0.0,
                    payload.get("source_type", "file"),
                    json.dumps({**payload.get("settings", {}), "kind": kind,
                                "dedupe_key": dedupe_key, "payload": payload}),
                    payload.get("campaign_id", "none"),
                    payload.get("template_path", ""),
                    lane, priority, 0, max_attempts, now, "", "",
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return job_id

    def claim(self, lanes: list[str], owner: str, lease_seconds: int) -> Optional[JobLease]:
        now = time.time()
        placeholders = ",".join("?" for _ in lanes) or "?"
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""SELECT * FROM jobs
                    WHERE status IN (?, ?) AND lane IN ({placeholders})
                      AND (available_at IS NULL OR available_at <= ?)
                    ORDER BY priority ASC, created_at ASC LIMIT 1""",
                (QUEUE_QUEUED, QUEUE_LEASED, *lanes, now),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            expires = now + lease_seconds
            conn.execute(
                "UPDATE jobs SET status=?, lane=?, lease_owner=?, lease_expires_at=?, "
                "attempt=attempt+1, updated_at=? WHERE id=?",
                (QUEUE_RUNNING, row["lane"], owner, expires, now, row["id"]),
            )
            conn.execute("COMMIT")
            return JobLease(
                job_id=row["id"], lane=row["lane"], owner=owner,
                attempt=row["attempt"] + 1, lease_expires_at=expires,
                payload=json.loads(row["settings_json"] or "{}"),
            )
        finally:
            conn.close()

    def heartbeat(self, lease: JobLease, lease_seconds: int) -> bool:
        now = time.time()
        expires = now + lease_seconds
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=? "
                "WHERE id=? AND lease_owner=? AND status=?",
                (expires, now, lease.job_id, lease.owner, QUEUE_RUNNING),
            )
            conn.commit()
            ok = cur.rowcount > 0
        finally:
            conn.close()
        lease.lease_expires_at = expires
        return ok

    def complete(self, lease: JobLease, result: Optional[dict[str, Any]] = None) -> None:
        now = time.time()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
                "finished_at=?, updated_at=? WHERE id=?",
                (QUEUE_SUCCEEDED, now, now, lease.job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def fail(self, lease: JobLease, error: str, *, retryable: bool = False) -> None:
        now = time.time()
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempt, max_attempts FROM jobs WHERE id=?", (lease.job_id,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return
            can_retry = retryable and row["attempt"] < row["max_attempts"]
            if can_retry:
                backoff = min(60, 5 * (2 ** (row["attempt"] - 1)))
                conn.execute(
                    "UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
                    "available_at=?, error=?, updated_at=? WHERE id=?",
                    (QUEUE_QUEUED, now + backoff, error, now, lease.job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
                    "error=?, finished_at=?, updated_at=? WHERE id=?",
                    (QUEUE_FAILED, error, now, now, lease.job_id),
                )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def requeue_expired(self, now: float) -> int:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
                "available_at=?, updated_at=? "
                "WHERE status=? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (QUEUE_QUEUED, now, now, QUEUE_RUNNING, now),
            )
            conn.commit()
            count = cur.rowcount
        finally:
            conn.close()
        if count:
            log.info("Requeued %d expired job(s)", count)
        return count

    def cancel(self, job_id: str) -> bool:
        now = time.time()
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, finished_at=? "
                "WHERE id=? AND status IN (?,?,?)",
                (QUEUE_CANCELED, now, now, job_id, QUEUE_QUEUED, QUEUE_LEASED, QUEUE_RUNNING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
