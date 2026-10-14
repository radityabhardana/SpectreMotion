"""Tests for the interactive Telegram bot (Phase 9): access control, meta
offset, session state, update dispatching, URL routing, catalog rendering.
Hermetic — Telegram is fully stubbed, no network.
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
    monkeypatch.setattr(bot.config, "DATA_DIR", tmp_path)
    migrate.apply()
    storage.init_db()
    return tmp_path


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.actions: list[tuple] = []
        self.callbacks: list[tuple] = []
        self._msg_id = 100

    def send_message(self, chat_id, text, reply_markup=None, **extra):
        self.sent.append(("msg", chat_id, text, reply_markup))
        self._msg_id += 1
        return {"message_id": self._msg_id}

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append(("edit", chat_id, message_id, text, reply_markup))
        return {"message_id": message_id}

    def edit_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edits.append(("markup", chat_id, message_id, reply_markup))
        return {"ok": True}

    def answer_callback(self, cb_id, text="", show_alert=False):
        self.callbacks.append((cb_id, text, show_alert))
        return True

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))
        return True

    def delete_message(self, chat_id, message_id):
        return True

    def set_my_commands(self, commands):
        return True

    def send_video(self, chat_id, path, caption="", **extra):
        self.sent.append(("video", chat_id, caption))
        return True

    def call(self, method, **params):
        return {"ok": True, "result": {}}

    def _all_text(self):
        return " ".join(m[2] for m in self.sent)


def _msg(chat_id, user_id, text="", file_id=None, file_name="v.mp4"):
    m = {"chat": {"id": chat_id}, "from": {"id": user_id, "username": "u"}, "text": text}
    if file_id:
        m["document"] = {"file_id": file_id, "file_name": file_name}
    return {"message": m}


# ---- Access control -------------------------------------------------------

def test_is_allowed_open_when_empty(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [])
    assert bot.is_allowed(1) is True


def test_is_allowed_restricts(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [111])
    assert bot.is_allowed(111) is True
    assert bot.is_allowed(999) is False


# ---- Meta + session -------------------------------------------------------

def test_meta_offset_roundtrip(db):
    assert bot._meta_get("update_offset") is None
    bot._meta_set("update_offset", "42")
    assert bot._meta_get("update_offset") == "42"


def test_session_roundtrip(db):
    assert bot.session_get(1)["mode"] == "idle"
    bot.session_set(1, 9, {"mode": "awaiting_choice", "clips": [1, 2]})
    st = bot.session_get(1)
    assert st["mode"] == "awaiting_choice" and st["clips"] == [1, 2]


# ---- Commands -------------------------------------------------------------

def test_start_sends_welcome_and_keyboard(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "/start"))
    assert "SpectreMotion" in tg.sent[0][2]
    assert tg.sent[0][3] is not None  # reply keyboard attached


def test_help(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "/help"))
    assert "Cara Pakai" in tg._all_text()


def test_status_idle(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "/status"))
    assert "Bot aktif" in tg._all_text()


# ---- Guards + routing -----------------------------------------------------

def test_disallowed_user_rejected(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(2, 2, "halo"))
    assert "belum punya akses" in tg._all_text()


def test_gdrive_folder_rejected(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1,
        "https://drive.google.com/drive/folders/abcdefghijklmnopqrstuvwxyz012345"))
    assert "folder" in tg._all_text().lower()


def test_plain_text_gets_hint(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "halo bro"))
    assert "link" in tg._all_text().lower()


def test_busy_guard_when_analyzing(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    bot.session_set(1, 1, {"mode": "analyzing"})
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "video baru"))
    assert "masih ada video" in tg._all_text().lower() or "sabar" in tg._all_text().lower()


def test_unsupported_format_rejected(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, file_id="x", file_name="movie.avi"))
    assert "belum didukung" in tg._all_text()


def test_youtube_too_long_rejected(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    monkeypatch.setattr(bot, "_youtube_duration", lambda url: 9999.0)
    monkeypatch.setattr(config, "BOT_MAX_INPUT_SECONDS", 600)
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "https://youtu.be/abc12345678"))
    assert "kepanjangan" in tg._all_text().lower()


# ---- Catalog + callbacks --------------------------------------------------

def test_catalog_text_and_inline(db, monkeypatch):
    clips = [
        {"idx": 0, "start": 0, "end": 30, "score": 92, "hook": "H1", "topic": "T1",
         "caption": "c", "rendered": False},
        {"idx": 1, "start": 40, "end": 70, "score": 60, "hook": "H2", "topic": "T2",
         "caption": "c", "rendered": False},
    ]
    text = bot._catalog_text(clips)
    assert "92/100" in text and "H1" in text
    kb = bot.catalog_inline("abc12345", clips)
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert any(d.startswith("r:abc12345:0") for d in datas)
    assert any(d.startswith("rall:") for d in datas)


def test_callback_render_no_data(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    cq = {"id": "cb1", "data": "r:abcd1234:0",
          "message": {"chat": {"id": 1}, "message_id": 10}, "from": {"id": 1}}
    bot.handle_callback(tg, cq)
    # No stored clips -> friendly toast.
    assert any("hilang" in c[1] or "ulang" in c[1] for c in tg.callbacks)


def test_callback_noop_answered(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    cq = {"id": "cb2", "data": "noop",
          "message": {"chat": {"id": 1}, "message_id": 10}, "from": {"id": 1}}
    bot.handle_callback(tg, cq)
    assert tg.callbacks and tg.callbacks[0][0] == "cb2"


def test_cancel_flow_confirm(db, monkeypatch):
    """Cancel with no active job reports the idle state (new behaviour)."""
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    cq = {"id": "cb3", "data": "cancel:abcd1234",
          "message": {"chat": {"id": 1}, "message_id": 10}, "from": {"id": 1}}
    bot.handle_callback(tg, cq)
    assert "Tidak ada proses aktif" in tg._all_text()


def test_cancel_with_active_job_really_cancels(db, monkeypatch):
    """When a cancel token is live, the callback actually cancels it."""
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    from app import cancel as cancel_mod
    token = cancel_mod.new_token("1")
    try:
        tg = FakeTelegram()
        cq = {"id": "cb5", "data": "cancel:abcd1234",
              "message": {"chat": {"id": 1}, "message_id": 10}, "from": {"id": 1}}
        bot.handle_callback(tg, cq)
        assert token.cancelled() is True
        assert "Dibatalkan" in tg._all_text()
        # Registry must be cleared so a new job starts clean.
        assert cancel_mod.get_token("1") is None
    finally:
        cancel_mod.clear("1")


def test_done_resets_session(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    bot.session_set(1, 1, {"mode": "awaiting_choice"})
    tg = FakeTelegram()
    cq = {"id": "cb4", "data": "done:abcd1234",
          "message": {"chat": {"id": 1}, "message_id": 10}, "from": {"id": 1}}
    bot.handle_callback(tg, cq)
    assert bot.session_get(1)["mode"] == "idle"


def test_clips_empty_state(db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_USERS", [1])
    tg = FakeTelegram()
    bot.handle_update(tg, _msg(1, 1, "/clips"))
    assert "Belum ada klip" in tg._all_text()
