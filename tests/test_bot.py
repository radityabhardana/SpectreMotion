"""Tests for the Telegram bot: access control, meta offset persistence,
update dispatching, and URL routing. Hermetic — no network (Telegram stubbed).
"""

import pytest

from app import bot, config, storage
from app.db import migrate


@pytest.fixture()
def db(tmp_path, monkeypatch):
    dbp = tmp_path / "app.db"
    monkeypatch.setattr(migrate.config, "DB_PATH", dbp)
    monkeypatch.setattr(storage.config, "DB_PATH", dbp)
    monkeypatch.setattr(bot.config, "DB_PATH", dbp)
    monkeypatch.setattr(bot.config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(bot.config, "OUTPUT_DIR", tmp_path / "out")
    migrate.apply()
    storage.init_db()
    return dbp


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple] = []

    def send_message(self, chat_id, text, **extra):
        self.sent.append(("msg", chat_id, text))
        return {"ok": True}

    def send_video(self, chat_id, path, caption="", **extra):
        self.sent.append(("video", chat_id, caption))
        return {"ok": True}


# ---- Access control -------------------------------------------------------

def test_is_allowed_open_when_list_empty(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [])
    assert bot.is_allowed(12345) is True


def test_is_allowed_restricts(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [111, 222])
    assert bot.is_allowed(111) is True
    assert bot.is_allowed(999) is False


# ---- Meta offset ----------------------------------------------------------

def test_meta_offset_roundtrip(db):
    assert bot._meta_get("update_offset") is None
    bot._meta_set("update_offset", "42")
    assert bot._meta_get("update_offset") == "42"
    bot._meta_set("update_offset", "43")  # upsert
    assert bot._meta_get("update_offset") == "43"


def test_touch_user(db):
    bot._touch_user(555, "rex")
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT username FROM telegram_users WHERE tg_user_id=555").fetchone()
    conn.close()
    assert row[0] == "rex"


# ---- Update handling ------------------------------------------------------

def test_start_command_sends_help(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    upd = {"message": {"chat": {"id": 1}, "from": {"id": 1, "username": "a"},
                       "text": "/start"}}
    bot.handle_update(tg, upd)
    assert tg.sent and "SpectreMotion" in tg.sent[0][2]


def test_disallowed_user_gets_rejected(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    upd = {"message": {"chat": {"id": 9}, "from": {"id": 9, "username": "stranger"},
                       "text": "hello"}}
    bot.handle_update(tg, upd)
    assert "tidak diizinkan" in tg.sent[0][2]


def test_gdrive_folder_url_rejected(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    upd = {"message": {"chat": {"id": 1}, "from": {"id": 1},
                       "text": "https://drive.google.com/drive/folders/abcdefghijklmnopqrstuvwxyz012345"}}
    bot.handle_update(tg, upd)
    assert any("folder Drive" in m[2] for m in tg.sent)


def test_plain_text_gets_hint(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    upd = {"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "halo bro"}}
    bot.handle_update(tg, upd)
    assert "link" in tg.sent[0][2].lower()


def test_youtube_url_routes_to_download(db, monkeypatch):
    """A YouTube link should attempt the download path (stubbed to fail fast)."""
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    from app.ingest import youtube

    def boom(url, out_dir, **kw):
        from app.errors import IngestError
        raise IngestError("stub")

    monkeypatch.setattr(youtube, "download_youtube", boom)
    tg = FakeTelegram()
    upd = {"message": {"chat": {"id": 1}, "from": {"id": 1},
                       "text": "https://youtu.be/dQw4w9WgXcQ"}}
    bot.handle_update(tg, upd)
    # It should have announced downloading and then reported the failure.
    assert any("Mengunduh" in m[2] for m in tg.sent)
    assert any("Gagal" in m[2] for m in tg.sent)
