"""Phase 1 tests: queue state machine, leases, requeue, storage port, events.
Hermetic — temp DB, no network, no media.
"""

import pytest

from app.adapters.events_memory import EventBus
from app.adapters.queue_sqlite import SqliteQueue
from app.adapters.storage_local import LocalStorage
from app.db import migrate
from app.ports import (
    LANE_HEAVY,
    QUEUE_CANCELED,
    QUEUE_FAILED,
    QUEUE_QUEUED,
    QUEUE_RUNNING,
    QUEUE_SUCCEEDED,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate.config, "DB_PATH", tmp_path / "q.db")
    migrate.apply()
    # queue/events read DB_PATH from config too
    from app.adapters import queue_sqlite, events_memory
    monkeypatch.setattr(queue_sqlite.config, "DB_PATH", tmp_path / "q.db")
    monkeypatch.setattr(events_memory.config, "DB_PATH", tmp_path / "q.db")
    return tmp_path / "q.db"


def _status(db, job_id):
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return row[0]


def test_enqueue_claim_complete(db):
    q = SqliteQueue()
    jid = q.enqueue("process_video", {"source_type": "file"})
    assert _status(db, jid) == QUEUE_QUEUED

    lease = q.claim([LANE_HEAVY], "worker-1", lease_seconds=60)
    assert lease is not None and lease.job_id == jid
    assert lease.attempt == 1
    assert _status(db, jid) == QUEUE_RUNNING

    q.complete(lease)
    assert _status(db, jid) == QUEUE_SUCCEEDED


def test_claim_empty_returns_none(db):
    q = SqliteQueue()
    assert q.claim([LANE_HEAVY], "w", 60) is None


def test_fail_non_retryable(db):
    q = SqliteQueue()
    jid = q.enqueue("process_video", {})
    lease = q.claim([LANE_HEAVY], "w", 60)
    q.fail(lease, "boom", retryable=False)
    assert _status(db, jid) == QUEUE_FAILED


def test_fail_retryable_requeues_with_backoff(db):
    q = SqliteQueue()
    jid = q.enqueue("process_video", {}, max_attempts=3)
    lease = q.claim([LANE_HEAVY], "w", 60)
    q.fail(lease, "transient", retryable=True)
    assert _status(db, jid) == QUEUE_QUEUED


def test_retryable_exhausts_attempts(db):
    q = SqliteQueue()
    jid = q.enqueue("process_video", {}, max_attempts=1)
    lease = q.claim([LANE_HEAVY], "w", 60)
    # attempt is now 1 == max_attempts, so a retryable failure must NOT requeue
    q.fail(lease, "transient", retryable=True)
    assert _status(db, jid) == QUEUE_FAILED


def test_requeue_expired_recovers_crashed_job(db):
    q = SqliteQueue()
    jid = q.enqueue("process_video", {})
    lease = q.claim([LANE_HEAVY], "worker-1", lease_seconds=1)
    # Simulate lease expiry in the past.
    count = q.requeue_expired(now=lease.lease_expires_at + 10)
    assert count == 1
    assert _status(db, jid) == QUEUE_QUEUED


def test_heartbeat_extends_lease(db):
    q = SqliteQueue()
    q.enqueue("process_video", {})
    lease = q.claim([LANE_HEAVY], "w", lease_seconds=1)
    old = lease.lease_expires_at
    assert q.heartbeat(lease, lease_seconds=120) is True
    assert lease.lease_expires_at > old


def test_cancel(db):
    q = SqliteQueue()
    jid = q.enqueue("process_video", {})
    assert q.cancel(jid) is True
    assert _status(db, jid) == QUEUE_CANCELED


def test_dedupe_key_reuses_live_job(db):
    q = SqliteQueue()
    j1 = q.enqueue("process_video", {}, dedupe_key="abc")
    j2 = q.enqueue("process_video", {}, dedupe_key="abc")
    assert j1 == j2


def test_storage_roundtrip_and_traversal(tmp_path):
    st = LocalStorage(root=tmp_path / "blobs")
    blob = st.put_bytes("users/1/jobs/2/input.txt", b"hello")
    assert blob.size == 5
    assert st.get_bytes("users/1/jobs/2/input.txt") == b"hello"
    assert st.stat("users/1/jobs/2/input.txt").exists is True

    from app.errors import ValidationError
    with pytest.raises(ValidationError):
        st.local_path("../../etc/passwd")
    with pytest.raises(ValidationError):
        st.put_bytes("a/../../../b.txt", b"x")


def test_storage_delete(tmp_path):
    st = LocalStorage(root=tmp_path / "blobs")
    st.put_bytes("a/b.txt", b"x")
    st.delete("a/b.txt")
    assert st.stat("a/b.txt").exists is False


def test_events_append_and_replay(db):
    bus = EventBus()
    bus.publish("job1", {"type": "stage", "stage": "probe"})
    bus.publish("job1", {"type": "stage", "stage": "render"})
    replayed = [e for e in bus._replay("job1", 0)]
    assert [e["stage"] for e in replayed] == ["probe", "render"]
    # last_seq skips already-seen events
    replayed2 = list(bus._replay("job1", 1))
    assert len(replayed2) == 1 and replayed2[0]["stage"] == "render"
