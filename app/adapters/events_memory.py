"""In-process EventBus + durable `job_events` log for SSE replay.

- `publish()` persists the event (monotonic per-job seq) AND fans out to any
  live subscribers.
- `subscribe()` replays persisted events past `last_seq`, then yields live ones.
- This gives reconnecting SSE clients (Last-Event-ID) a gapless stream with a
  guaranteed re-sendable terminal event.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Any, Iterator

from app import config
from app import storage as storage_mod

log = logging.getLogger("spectre.events")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _next_seq(conn: sqlite3.Connection, job_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM job_events WHERE job_id=?", (job_id,)
    ).fetchone()
    return int(row["s"]) + 1


class EventBus:
    """EventPort implementation. Channel ids use the job id (job_id)."""

    def __init__(self) -> None:
        self._subs: dict[str, list[queue.Queue]] = defaultdict(list)
        self._lock = threading.Lock()

    # ---- EventPort ----
    def publish(self, channel: str, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("ts", time.time())
        seq = self._persist(channel, payload)
        payload["seq"] = seq
        with self._lock:
            subs = list(self._subs.get(channel, []))
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                log.warning("Event subscriber queue full for %s", channel)

    def subscribe(self, channel: str, last_seq: int = 0) -> Iterator[dict[str, Any]]:
        # Replay persisted events first.
        for ev in self._replay(channel, last_seq):
            yield ev
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs[channel].append(q)
        try:
            while True:
                try:
                    yield q.get(timeout=30)
                except queue.Empty:
                    # Heartbeat keeps proxies from closing the stream.
                    yield {"type": "heartbeat", "ts": time.time()}
        finally:
            with self._lock:
                if q in self._subs.get(channel, []):
                    self._subs[channel].remove(q)

    # ---- persistence ----
    def _persist(self, job_id: str, event: dict[str, Any]) -> int:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            seq = _next_seq(conn, job_id)
            conn.execute(
                "INSERT INTO job_events(job_id, seq, type, data_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (job_id, seq, event.get("type", "event"),
                 json.dumps(event, ensure_ascii=False), time.time()),
            )
            conn.execute("COMMIT")
            return seq
        except sqlite3.OperationalError as exc:
            # job_events may not exist yet in very early boot; don't crash pub.
            log.debug("Event persist skipped: %s", exc)
            return 0
        finally:
            conn.close()

    def _replay(self, job_id: str, last_seq: int) -> Iterator[dict[str, Any]]:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT data_json FROM job_events WHERE job_id=? AND seq>? ORDER BY seq",
                (job_id, last_seq),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        for r in rows:
            try:
                yield json.loads(r["data_json"])
            except json.JSONDecodeError:
                continue
