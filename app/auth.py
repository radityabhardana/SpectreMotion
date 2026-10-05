"""Auth: users, scrypt password hashing, opaque sessions, rate limits.

Uses only stdlib (hashlib.scrypt) — no bcrypt/argon2 dependency. Sessions are
opaque random tokens stored *hashed* at rest, so a DB leak doesn't hand out
live sessions. Ownership checks map to 404 (never leak existence).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from app import config
from app.errors import AuthError, RateLimitedError, ValidationError

log = logging.getLogger("spectre.auth")

SESSION_TTL_S = 30 * 24 * 3600          # 30 days
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_DKLEN = 32


class AuthBackendError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---- Password hashing -----------------------------------------------------

def hash_password(password: str) -> str:
    if not password or len(password) < 6:
        raise ValidationError("Password minimal 6 karakter")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                        r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=_DKLEN)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---- Users ----------------------------------------------------------------

@dataclass
class User:
    id: int
    username: str
    is_admin: bool


def create_user(username: str, password: str, *, is_admin: bool = False) -> User:
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise ValidationError("Username tidak valid")
    now = time.time()
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (username, hash_password(password), 1 if is_admin else 0, "active", now, now),
        )
        conn.commit()
        new_id = cur.lastrowid
        if new_id is None:
            raise AuthBackendError("Gagal membuat user")
        return User(id=int(new_id), username=username, is_admin=is_admin)
    except sqlite3.IntegrityError as exc:
        raise ValidationError("Username sudah dipakai") from exc
    finally:
        conn.close()


def authenticate(username: str, password: str) -> Optional[User]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND status='active'", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return User(id=row["id"], username=row["username"], is_admin=bool(row["is_admin"]))


def get_user(user_id: int) -> Optional[User]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return User(id=row["id"], username=row["username"], is_admin=bool(row["is_admin"]))


def count_users() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
    finally:
        conn.close()


# ---- Sessions -------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, *, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent) "
            "VALUES (?,?,?,?,?)",
            (_token_hash(token), user_id, now, now + SESSION_TTL_S, user_agent[:200]),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def resolve_session(token: str) -> Optional[User]:
    if not token:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at > ?",
            (_token_hash(token), time.time()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return get_user(row["user_id"])


def revoke_session(token: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?",
                     (time.time(), _token_hash(token)))
        conn.commit()
    finally:
        conn.close()


# ---- Rate limiting --------------------------------------------------------

def check_rate_limit(user_id: int, action: str, *, limit: int, window_s: int) -> None:
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT window_end, count FROM rate_limits WHERE user_id=? AND action=?",
            (user_id, action),
        ).fetchone()
        if row is None or row["window_end"] < now:
            conn.execute(
                "INSERT INTO rate_limits(user_id, action, window_end, count) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id, action) DO UPDATE SET window_end=excluded.window_end, count=1",
                (user_id, action, now + window_s, 1),
            )
            conn.execute("COMMIT")
            return
        if row["count"] >= limit:
            retry_after = int(row["window_end"] - now) + 1
            conn.execute("COMMIT")
            raise RateLimitedError(
                f"Terlalu banyak permintaan; coba lagi dalam {retry_after}s",
                detail={"retry_after": retry_after},
            )
        conn.execute(
            "UPDATE rate_limits SET count=count+1 WHERE user_id=? AND action=?",
            (user_id, action),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def require_owner(user_id: Optional[int], resource_user_id: Optional[int]) -> None:
    """Raise (as 404) if a non-admin user tries to touch someone else's row."""
    if user_id is None:
        return  # legacy/system context
    if resource_user_id is None:
        return  # unowned (legacy) resource
    if user_id != resource_user_id:
        from app.errors import OwnershipError
        raise OwnershipError("Resource tidak ditemukan")


def bootstrap_admin_from_token() -> Optional[User]:
    """If APP_TOKEN is set and no users exist, create an 'admin' user so the
    legacy token maps to a real account during migration."""
    if not config.APP_TOKEN or count_users() > 0:
        return None
    try:
        user = create_user("admin", config.APP_TOKEN[:32], is_admin=True)
        log.info("Bootstrapped admin user from APP_TOKEN")
        return user
    except ValidationError:
        return None
