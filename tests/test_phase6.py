"""Phase 6 tests: auth (scrypt, sessions, rate limit), ownership.
Hermetic — temp DB, no network.
"""

import pytest

from app import auth, storage
from app.db import migrate
from app.errors import OwnershipError, RateLimitedError, ValidationError


@pytest.fixture()
def db(tmp_path, monkeypatch):
    dbp = tmp_path / "app.db"
    monkeypatch.setattr(migrate.config, "DB_PATH", dbp)
    for mod in (auth, storage):
        monkeypatch.setattr(mod.config, "DB_PATH", dbp)
    from app.adapters import queue_sqlite
    monkeypatch.setattr(queue_sqlite.config, "DB_PATH", dbp)
    migrate.apply()
    storage.init_db()
    return dbp


def test_password_hash_roundtrip():
    h = auth.hash_password("secret123")
    assert h.startswith("scrypt$")
    assert auth.verify_password("secret123", h) is True
    assert auth.verify_password("wrong", h) is False


def test_password_hash_is_salted():
    h1 = auth.hash_password("samepass")
    h2 = auth.hash_password("samepass")
    assert h1 != h2  # random salt each time


def test_short_password_rejected():
    with pytest.raises(ValidationError):
        auth.hash_password("abc")


def test_create_and_authenticate(db):
    user = auth.create_user("alice", "password1")
    assert user.id >= 1
    assert auth.authenticate("alice", "password1") is not None
    assert auth.authenticate("alice", "wrong") is None
    assert auth.authenticate("nobody", "password1") is None


def test_duplicate_username_rejected(db):
    auth.create_user("bob", "password1")
    with pytest.raises(ValidationError):
        auth.create_user("bob", "password2")


def test_session_lifecycle(db):
    user = auth.create_user("carol", "password1")
    token = auth.create_session(user.id)
    resolved = auth.resolve_session(token)
    assert resolved is not None and resolved.id == user.id
    auth.revoke_session(token)
    assert auth.resolve_session(token) is None


def test_invalid_session_token(db):
    assert auth.resolve_session("garbage") is None
    assert auth.resolve_session("") is None


def test_rate_limit_enforced(db):
    user = auth.create_user("dave", "password1")
    for _ in range(3):
        auth.check_rate_limit(user.id, "create_job", limit=3, window_s=3600)
    with pytest.raises(RateLimitedError):
        auth.check_rate_limit(user.id, "create_job", limit=3, window_s=3600)


def test_ownership_same_user_ok(db):
    auth.require_owner(1, 1)  # no raise


def test_ownership_cross_user_raises(db):
    with pytest.raises(OwnershipError):
        auth.require_owner(1, 2)


def test_ownership_legacy_none_ok(db):
    auth.require_owner(None, 5)   # legacy context
    auth.require_owner(5, None)   # unowned resource


def test_job_carries_user_id(db):
    user = auth.create_user("erin", "password1")
    job = storage.create_job(source_type="file", source_name="a.mp4", user_id=user.id)
    assert storage.get_job(job.id).user_id == user.id
