"""Database migration runner.

Design:
- Migrations live in `app/db/migrations/NNNN_description.sql`, applied in
  lexical order.
- A `schema_version` table records each applied version + SHA256 checksum.
- Each migration runs in its own transaction; a failure rolls back atomically
  and leaves `schema_version` unchanged.
- Checksum drift (file edited after being applied) is a hard error.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app import config
from app.errors import BackendError

log = logging.getLogger("spectre.db.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIN_SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^(\d{4})_(.+)\.sql$")


@dataclass
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def _discover() -> list[Migration]:
    out: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if not m:
            log.warning("Skipping unrecognized migration file: %s", path.name)
            continue
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        out.append(Migration(int(m.group(1)), m.group(2), path, sql, checksum))
    out.sort(key=lambda x: x.version)
    return out


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    `executescript` implicitly commits and would break our explicit
    transaction (so ROLLBACK would be a no-op). We split on `;` at
    end-of-line and drop comment-only / empty fragments. Sufficient for
    our DDL-only migrations; SQL containing semicolons inside string
    literals or triggers is out of scope and would need a real parser.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.split("--", 1)[0].rstrip()
        if not line:
            continue
        buffer.append(line)
        if line.endswith(";"):
            stmt = "\n".join(buffer).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            buffer = []
    trailing = "\n".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at REAL NOT NULL,
            checksum   TEXT NOT NULL
        )
        """
    )


def _applied(conn: sqlite3.Connection) -> dict[int, str]:
    _ensure_version_table(conn)
    rows = conn.execute("SELECT version, checksum FROM schema_version").fetchall()
    return {r["version"]: r["checksum"] for r in rows}


def current_version(conn: Optional[sqlite3.Connection] = None) -> int:
    own = conn is None
    conn = conn or _connect()
    try:
        _ensure_version_table(conn)
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0
    finally:
        if own:
            conn.close()


def head_version() -> int:
    migrations = _discover()
    return migrations[-1].version if migrations else 0


def _verify_checksums(conn: sqlite3.Connection, migrations: list[Migration]) -> None:
    applied = _applied(conn)
    by_version = {m.version: m for m in migrations}
    for version, checksum in applied.items():
        mig = by_version.get(version)
        if mig is None:
            raise BackendError(
                f"DB punya migrasi v{version} yang tidak ada di disk — versi DB lebih baru dari kode?"
            )
        if mig.checksum != checksum:
            raise BackendError(
                f"Checksum migrasi v{version} ({mig.name}) berubah setelah diterapkan. "
                "File migrasi tidak boleh diedit — buat migrasi baru."
            )


def apply(dry_run: bool = False) -> list[int]:
    """Apply all pending migrations. Returns list of applied versions."""
    migrations = _discover()
    conn = _connect()
    applied_versions: list[int] = []
    try:
        # Advisory lock: serialize concurrent web+worker startup migrations.
        conn.execute("BEGIN IMMEDIATE")
        try:
            _verify_checksums(conn, migrations)
            done = set(_applied(conn))
        finally:
            conn.execute("COMMIT")

        for mig in migrations:
            if mig.version in done:
                continue
            if dry_run:
                log.info("[dry-run] would apply v%d %s", mig.version, mig.name)
                applied_versions.append(mig.version)
                continue
            log.info("Applying migration v%d %s", mig.version, mig.name)
            conn.execute("BEGIN")
            try:
                for statement in _split_statements(mig.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version(version, name, applied_at, checksum) "
                    "VALUES (?,?,?,?)",
                    (mig.version, mig.name, time.time(), mig.checksum),
                )
                conn.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass  # transaction already unwound
                raise BackendError(f"Migrasi v{mig.version} gagal (dibatalkan): {exc}") from exc
            applied_versions.append(mig.version)
        return applied_versions
    finally:
        conn.close()


def ensure_schema() -> int:
    """Called on startup by web + worker. Applies pending, enforces boot gate."""
    applied = apply()
    version = current_version()
    if applied:
        log.info("Applied %d migration(s); schema now v%d", len(applied), version)
    else:
        log.info("Schema up to date at v%d", version)
    if version < MIN_SCHEMA_VERSION:
        raise BackendError(
            f"Schema v{version} < minimum v{MIN_SCHEMA_VERSION} yang dibutuhkan kode ini"
        )
    return version


def status() -> dict[str, int]:
    return {"current": current_version(), "head": head_version()}


def _main() -> None:
    import argparse
    import sys

    from app.logging_setup import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="SpectreMotion migrations")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "apply", "dry-run"])
    args = parser.parse_args()

    if args.command == "status":
        s = status()
        print(f"current: v{s['current']}  head: v{s['head']}")
    elif args.command == "apply":
        applied = apply()
        print(f"applied: {applied or 'nothing'}  ->  v{current_version()}")
    elif args.command == "dry-run":
        pending = apply(dry_run=True)
        print(f"pending: {pending or 'nothing'}")
        sys.exit(0)


if __name__ == "__main__":
    _main()
