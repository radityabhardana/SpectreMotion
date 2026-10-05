"""Tests for the migration runner (Phase 0): idempotency, checksum drift,
boot gate, dry-run. Uses a temp DB per test — no network, no media.
"""

import sqlite3

import pytest

from app.db import migrate
from app.errors import BackendError


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(migrate.config, "DB_PATH", db)
    return db


def test_status_reports_head(tmp_db):
    s = migrate.status()
    assert s["current"] == 0
    assert s["head"] >= 1


def test_apply_is_idempotent(tmp_db):
    first = migrate.apply()
    assert 1 in first
    current = migrate.current_version()
    second = migrate.apply()
    assert second == []  # nothing pending
    assert migrate.current_version() == current


def test_schema_has_expected_tables(tmp_db):
    migrate.apply()
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"jobs", "clips", "schema_version"} <= names
    conn.close()


def test_boot_gate_passes_after_apply(tmp_db):
    version = migrate.ensure_schema()
    assert version >= migrate.MIN_SCHEMA_VERSION


def test_checksum_drift_is_detected(tmp_db, monkeypatch):
    migrate.apply()
    # Tamper with the recorded checksum to simulate an edited migration file.
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE schema_version SET checksum='deadbeef' WHERE version=1")
    conn.commit()
    conn.close()
    with pytest.raises(BackendError):
        migrate.apply()


def test_dry_run_does_not_persist(tmp_db):
    pending = migrate.apply(dry_run=True)
    assert 1 in pending
    # Still unapplied afterwards.
    assert migrate.current_version() == 0


def test_unknown_applied_version_errors(tmp_db):
    migrate.apply()
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO schema_version(version, name, applied_at, checksum) "
        "VALUES (9999, 'future', 0, 'x')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(BackendError):
        migrate.apply()
