"""SpectreMotion Telegram bot — interactive long-polling, for GitHub Actions or VPS.

UX (Phase 9): guided menus, inline buttons, live single-message status edits,
per-clip rendering, and friendly Bahasa Indonesia error copy. Reuses the same
engine as the rest of the app (app.media.*, app.campaign, app.presets) — no
logic duplicated here.

Design constraints:
- Ephemeral runner (Actions): getUpdates offset + session state are persisted to
  SQLite so polling resumes across runs. Videos are NOT persisted.
- One job = one status message (progress is an EDIT, never a flood).
- callback_data <= 64 bytes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app import config, storage
from app.adapters import registry
from app.db import migrate
from app.errors import SpectreError
from app.ingest import gdrive, urls, youtube
from app.logging_setup import new_correlation_id, setup_logging
from app.state_store import persist, restore

log = logging.getLogger("spectre.bot")

_API = "https://api.telegram.org/bot{token}/{method}"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# stage -> (label, percent)
STAGES = {
    "receive":   ("📥 Menerima file…", 5),
    "download":  ("⬇️ Mengunduh…", 10),
    "probe":     ("⏳ Membaca durasi…", 15),
    "transcribe": ("🎧 Transkripsi audio…  (paling lama, sabar ya 🙏)", 25),
    "analyze":   ("🧠 Mencari momen viral…", 65),
    "catalog":   ("📦 Menyusun pilihan klip…", 90),
    "render":    ("✂️ Merender klip…", 50),
    "done":      ("✅ Selesai", 100),
}


# ===========================================================================
# Telegram transport
# ===========================================================================

class Telegram:
    def __init__(self, token: str) -> None:
        import requests
        self._requests = requests
        self.token = token
        self.session = requests.Session()

    def _url(self, method: str) -> str:
        return _API.format(token=self.token, method=method)

    def call(self, method: str, **params):
        for attempt in range(3):
            try:
                resp = self.session.post(self._url(method), json=params, timeout=60)
                data = resp.json()
                if not data.get("ok"):
                    desc = data.get("description", "")
                    # These are normal, harmless API responses — don't warn.
                    if "not modified" in desc or "too old" in desc or "query ID is invalid" in desc:
                        return data.get("result")
                    log.warning("Telegram %s: %s", method, desc)
                    return data.get("result")
                return data.get("result")
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram %s failed (%d/3): %s", method, attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        return None

    def get_updates(self, offset: Optional[int], timeout: int):
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    # ---- sending ----
    def send_message(self, chat_id: int, text: str, reply_markup=None, **extra):
        params = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        params.update(extra)
        return self.call("sendMessage", **params)

    def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup=None):
        params = {"chat_id": chat_id, "message_id": message_id,
                  "text": text[:4096], "parse_mode": "HTML"}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("editMessageText", **params)

    def edit_reply_markup(self, chat_id: int, message_id: int, reply_markup=None):
        return self.call("editMessageReplyMarkup", chat_id=chat_id,
                         message_id=message_id, reply_markup=reply_markup)

    def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False):
        # "query is too old" is harmless (user tapped an old button) — the call
        # already logs at WARNING; downgrade by calling directly.
        return self.call("answerCallbackQuery", callback_query_id=callback_id,
                         text=text[:200], show_alert=show_alert)

    def send_chat_action(self, chat_id: int, action: str = "typing"):
        return self.call("sendChatAction", chat_id=chat_id, action=action)

    def delete_message(self, chat_id: int, message_id: int):
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def set_my_commands(self, commands: list[dict]):
        return self.call("setMyCommands", commands=commands)

    # ---- video upload (retry + document fallback) ----
    def send_video(self, chat_id: int, path: str, caption: str = "", **extra) -> bool:
        for attempt in range(1, 4):
            if self._upload(chat_id, "sendVideo", "video", path, caption, extra):
                return True
            log.warning("sendVideo attempt %d/3 gagal, retry...", attempt)
            time.sleep(3 * attempt)
        log.info("Fallback ke sendDocument untuk %s", path)
        return self._upload(chat_id, "sendDocument", "document", path, caption, extra)

    def _upload(self, chat_id: int, method: str, field: str, path: str,
                caption: str, extra: dict) -> bool:
        import requests
        try:
            with open(path, "rb") as fh:
                data = {"chat_id": chat_id, "caption": caption[:1024], **extra}
                if method == "sendVideo":
                    data["supports_streaming"] = "true"
                resp = self.session.post(self._url(method), data=data,
                                         files={field: fh}, timeout=(30, 600))
            try:
                payload = resp.json()
            except ValueError:
                log.warning("%s: respons bukan JSON (%s)", method, resp.status_code)
                return False
            if not payload.get("ok"):
                log.warning("%s failed: %s", method, payload.get("description"))
                return False
            return True
        except requests.RequestException as exc:
            log.error("%s exception: %s", method, str(exc)[:160])
            return False


# ===========================================================================
# Access control + DB helpers
# ===========================================================================

def is_allowed(user_id: int) -> bool:
    if not config.TELEGRAM_ALLOWED_USERS:
        return True
    return user_id in config.TELEGRAM_ALLOWED_USERS


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _meta_get(key: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _meta_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO bot_meta(key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _touch_user(user_id: int, username: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO telegram_users(tg_user_id, username, created_at, last_seen) "
                  "VALUES (?,?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET "
                  "username=excluded.username, last_seen=excluded.last_seen",
                  (user_id, username, time.time(), time.time()))


def session_get(chat_id: int) -> dict:
    with _conn() as c:
        row = c.execute("SELECT state_json FROM bot_sessions WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        return {"mode": "idle"}
    try:
        state = json.loads(row["state_json"] or "{}")
        state.setdefault("mode", "idle")
        return state
    except json.JSONDecodeError:
        return {"mode": "idle"}


def session_set(chat_id: int, user_id: int, state: dict) -> None:
    with _conn() as c:
        c.execute("INSERT INTO bot_sessions(chat_id, user_id, state_json, updated_at) "
                  "VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                  "user_id=excluded.user_id, state_json=excluded.state_json, "
                  "updated_at=excluded.updated_at",
                  (chat_id, user_id, json.dumps(state, ensure_ascii=False), time.time()))


# ===========================================================================
# Keyboards
# ===========================================================================

def main_reply_keyboard():
    return {"keyboard": [
        [{"text": "🎬 Kirim Video"}, {"text": "🔗 Kirim Link"}],
        [{"text": "📋 Riwayat"}, {"text": "⚙️ Pengaturan"}],
        [{"text": "❓ Bantuan"}],
    ], "resize_keyboard": True, "is_persistent": True}


def _btn(label: str, data: str) -> dict:
    return {"text": label, "callback_data": data[:64]}


def catalog_inline(job8: str, clips: list, page: int = 0, per_page: int = 4):
    rows = []
    start = page * per_page
    chunk = clips[start:start + per_page]
    for i, c in enumerate(chunk):
        idx = c["idx"]
        if c.get("rendered"):
            rows.append([_btn(f"✅ Klip {idx+1} · sudah dikirim", "noop")])
        else:
            emoji = "🔥" if c["score"] >= 85 else "⚡" if c["score"] >= 70 else "👍" if c["score"] >= 50 else "🙂"
            rows.append([
                _btn(f"🎬 Klip {idx+1} ({emoji}{c['score']}) ▶️ Render", f"r:{job8}:{idx}"),
                _btn(f"♻️ Hook {idx+1}", f"h:{job8}:{idx}"),
            ])
    if len(clips) > start + per_page:
        rows.append([_btn(f"➕ {len(clips)-start-per_page} momen lagi…", f"more:{job8}:{page+1}")])
    rows.append([_btn("⚡ Render Semua", f"rall:{job8}")])
    rows.append([_btn("⚙️ Ubah Gaya", f"style:{job8}"), _btn("❌ Batal", f"cancel:{job8}")])
    return {"inline_keyboard": rows}


def styles_inline(job8: str):
    from app import presets
    rows = []
    for p in presets.list_presets():
        rows.append([_btn(f"🎨 {p['name']}", f"set:style:{p['id']}|{job8}")])
    rows.append([_btn("🔙 Kembali", f"cat:{job8}")])
    return {"inline_keyboard": rows}


def confirm_cancel_inline(job8: str):
    return {"inline_keyboard": [[
        _btn("✅ Ya, batalkan", f"cancel:yes:{job8}"),
        _btn("↩️ Lanjutkan", f"cancel:no:{job8}"),
    ]]}


def followup_inline(job8: str):
    return {"inline_keyboard": [[
        _btn("🎬 Pilih Klip Lain", f"cat:{job8}"),
        _btn("🏁 Selesai", f"done:{job8}"),
    ]]}


# ===========================================================================
# Status message manager (one message, edited live)
# ===========================================================================

class StatusMessage:
    def __init__(self, tg: Telegram, chat_id: int, message_id: Optional[int] = None,
                 job8: Optional[str] = None, interval: float = 5.0):
        self.tg = tg
        self.chat_id = chat_id
        self.message_id = message_id
        self.started = time.time()
        self.job8 = job8
        self.interval = interval
        self._last_text = ""
        self._stage = "download"
        self._note = ""
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_sent = 0.0

    def _cancel_markup(self):
        if not self.job8:
            return None
        return {"inline_keyboard": [[
            {"text": "❌ Batal", "callback_data": f"cancel:{self.job8}"},
        ]]}

    def _text(self, stage: str, note: str = "") -> str:
        label, pct = STAGES.get(stage, (stage, 50))
        filled = int(pct / 10)
        bar = "▰" * filled + "▱" * (10 - filled)
        spin = _SPINNER[int(time.time() * 4) % len(_SPINNER)]
        elapsed = int(time.time() - self.started)
        etime = f"{elapsed}s" if elapsed < 60 else f"{elapsed//60}m {elapsed%60:02d}s"
        body = f"{label}\n{bar} {pct}%\n⏱ {etime} • {spin}"
        if note:
            body += f"\n\n{note}"
        if self.job8:
            body += "\n\n<i>Tekan ❌ Batal untuk menghentikan.</i>"
        return body

    def _render_current(self) -> str:
        return self._text(self._stage, self._note)

    def update(self, stage: str, note: str = "") -> None:
        with self._lock:
            self._stage = stage
            self._note = note
        self._send_edit(self._render_current())

    def _send_edit(self, text: str) -> None:
        # Skip identical edits (Telegram rejects them and it saves API calls).
        if text == self._last_text:
            return
        self._last_sent = time.time()
        if self.message_id is None:
            res = self.tg.send_message(self.chat_id, text, reply_markup=self._cancel_markup())
            if res:
                self.message_id = res.get("message_id")
                self._last_text = text
        else:
            self.tg.edit_message(self.chat_id, self.message_id, text,
                                 reply_markup=self._cancel_markup())
            self._last_text = text

    def start_heartbeat(self) -> None:
        """Tick the status message every `interval` seconds in a background
        thread, so the timer/spinner keep moving while the main work blocks."""
        if self._hb_thread is not None:
            return
        self._hb_stop.clear()

        def loop():
            while not self._hb_stop.wait(self.interval):
                try:
                    # Re-render with the CURRENT stage so the timer advances.
                    self._send_edit(self._render_current())
                except Exception as exc:  # noqa: BLE001 — never kill the job
                    log.debug("Heartbeat edit gagal: %s", exc)

        self._hb_thread = threading.Thread(target=loop, name="status-heartbeat", daemon=True)
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2)
            self._hb_thread = None

    def replace(self, text: str, reply_markup=None) -> None:
        # Manual replacement (e.g. catalog) -> stop the timer first.
        self.stop_heartbeat()
        with self._lock:
            self._stage = "__raw__"
        if self.message_id is None:
            res = self.tg.send_message(self.chat_id, text, reply_markup=reply_markup)
            if res:
                self.message_id = res.get("message_id")
        else:
            self.tg.edit_message(self.chat_id, self.message_id, text, reply_markup=reply_markup)
        self._last_text = text

    def show_cancelled(self) -> None:
        self.stop_heartbeat()
        text = "🛑 <b>Dibatalkan</b>\n\nProses dihentikan atas permintaanmu.\nKirim video baru kapan saja ya 👋"
        if self.message_id is None:
            self.tg.send_message(self.chat_id, text)
        else:
            self.tg.edit_message(self.chat_id, self.message_id, text, reply_markup=None)

    def delete(self) -> None:
        self.stop_heartbeat()
        if self.message_id:
            self.tg.delete_message(self.chat_id, self.message_id)


# ===========================================================================
# Copy
# ===========================================================================

WELCOME = (
    "🎬 <b>SpectreMotion</b>\n\n"
    "Ubah video panjang jadi <b>klip vertikal 9:16</b> — subtitle dibakar + hook otomatis. "
    "Siap posting ke TikTok / Reels / Shorts.\n\n"
    "<b>Cara pakai (gampang):</b>\n"
    "1️⃣ Kirim <b>link YouTube</b> atau <b>upload video</b>\n"
    "2️⃣ Tunggu bot analisis (transkripsi + pilih momen)\n"
    "3️⃣ Pilih klip yang mau kamu render\n"
    "4️⃣ Terima videonya ✨\n\n"
    "Mau mulai? Kirim link atau video sekarang 👇"
)

HELP = (
    "📖 <b>Cara Pakai SpectreMotion</b>\n\n"
    "<b>1. Kirim sumber video</b>\n"
    "• Link YouTube, atau\n"
    "• Upload file (mp4, mov, mkv, webm, m4v)\n"
    "• Maksimal <b>{limit} menit</b> per video.\n\n"
    "<b>2. Bot proses otomatis</b>\n"
    "🎧 Transkripsi audio → 🧠 cari momen viral → ✂️ siapkan klip.\n\n"
    "<b>3. Pilih klip</b>\n"
    "Tap tombol <b>Render</b> di klip yang kamu mau, atau <b>Render Semua</b>.\n\n"
    "<b>Perintah</b>\n"
    "/start — menu utama\n/help — panduan ini\n/cancel — batalkan proses\n"
    "/status — cek status\n/settings — pengaturan\n/clips — klip terakhir\n"
)


# ===========================================================================
# Update handling
# ===========================================================================

@dataclass
class Incoming:
    chat_id: int
    user_id: int
    username: str
    text: str = ""
    file_id: Optional[str] = None
    file_name: str = ""


def _extract(upd: dict) -> Optional[Incoming]:
    if "callback_query" in upd:
        cq = upd["callback_query"]
        msg = cq.get("message", {})
        frm = cq.get("from", {})
        return Incoming(chat_id=msg.get("chat", {}).get("id"),
                        user_id=frm.get("id"), username=frm.get("username", ""),
                        text="")  # callback handled separately
    msg = upd.get("message") or {}
    frm = msg.get("from") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id is None or frm.get("id") is None:
        return None
    doc = msg.get("document") or msg.get("video") or {}
    return Incoming(
        chat_id=chat_id, user_id=frm["id"],
        username=frm.get("username", "") or frm.get("first_name", ""),
        text=(msg.get("text") or "").strip(),
        file_id=doc.get("file_id"), file_name=doc.get("file_name", "") or "video.mp4",
    )


def handle_update(tg: Telegram, upd: dict) -> None:
    if "callback_query" in upd:
        handle_callback(tg, upd["callback_query"])
        return
    inc = _extract(upd)
    if inc is None:
        return
    _touch_user(inc.user_id, inc.username)
    if not is_allowed(inc.user_id):
        tg.send_message(inc.chat_id, "⛔ Maaf, kamu belum punya akses ke bot ini.")
        return
    handle_message(tg, inc)


def handle_message(tg: Telegram, inc: Incoming) -> None:
    text = inc.text
    chat_id = inc.chat_id

    # Commands
    if text.startswith("/start"):
        session_set(chat_id, inc.user_id, {"mode": "idle"})
        tg.send_message(chat_id, WELCOME, reply_markup=main_reply_keyboard())
        return
    if text.startswith("/help") or text == "❓ Bantuan":
        tg.send_message(chat_id, HELP.format(limit=config.BOT_MAX_INPUT_SECONDS // 60))
        return
    if text.startswith("/status"):
        state = session_get(chat_id)
        if state.get("mode") in ("analyzing", "rendering"):
            tg.send_message(chat_id, "⏳ Ada proses yang sedang berjalan. Sabar ya 🙏")
        else:
            tg.send_message(chat_id, f"✅ <b>Bot aktif</b>\n\nTidak ada proses berjalan.\n"
                                     f"🎙️ Model: <code>{config.WHISPER_MODEL}</code>")
        return
    if text.startswith("/cancel"):
        _handle_cancel(tg, chat_id, inc.user_id)
        return
    if text.startswith("/settings") or text == "⚙️ Pengaturan":
        _show_settings(tg, chat_id)
        return
    if text.startswith("/clips") or text == "📋 Riwayat":
        _show_last_catalog(tg, chat_id)
        return
    if text in ("🎬 Kirim Video", "🔗 Kirim Link"):
        tg.send_message(chat_id, "Kirim video atau link-nya sekarang ya 👇")
        return

    state = session_get(chat_id)
    mode = state.get("mode", "idle")

    # Busy guard
    if mode in ("analyzing", "rendering"):
        tg.send_message(chat_id, "⏳ Sabar ya, masih ada video yang diproses. "
                                 "Tunggu selesai atau ketik /cancel untuk membatalkan.")
        return

    # File upload
    if inc.file_id:
        ext = Path(inc.file_name).suffix.lower()
        if ext and ext not in config.SUPPORTED_EXTENSIONS:
            tg.send_message(chat_id, f"❌ <b>Format {ext} belum didukung</b>\n\n"
                                     f"Format yang bisa: mp4, mov, mkv, webm, m4v.")
            return
        _start_upload(tg, inc)
        return

    # URL
    if text and (urls.is_youtube_url(text) or urls.is_gdrive_url(text) or text.startswith("http")):
        if urls.is_gdrive_folder_url(text):
            tg.send_message(chat_id, "📁 <b>Link folder belum didukung</b>\n\n"
                                     "Share <b>file</b> Drive-nya (bukan folder).")
            return
        _start_url(tg, inc)
        return

    # Fallback
    tg.send_message(chat_id, "Kirim <b>link YouTube</b> atau <b>video</b> ya 👇\n"
                             "Ketik /help untuk panduan.", reply_markup=main_reply_keyboard())


# ===========================================================================
# Flows
# ===========================================================================

def _start_url(tg: Telegram, inc: Incoming) -> None:
    kind = urls.classify(inc.text)
    if kind == "youtube":
        dur = _youtube_duration(inc.text)
        if dur and dur > config.BOT_MAX_INPUT_SECONDS:
            tg.send_message(inc.chat_id,
                f"⚠️ <b>Videonya kepanjangan</b>\n\n"
                f"Durasi video ini <b>{dur/60:.0f} menit</b> — batas bot "
                f"<b>{config.BOT_MAX_INPUT_SECONDS//60} menit</b> 🥲\n\n"
                f"Bot ini untuk video pendek–menengah. Kirim klip yang lebih pendek ya.")
            return

    st = StatusMessage(tg, inc.chat_id, job8=_job8(inc.chat_id))
    st.update("download")
    st.start_heartbeat()   # tick during the (blocking) download
    session_set(inc.chat_id, inc.user_id,
                {"mode": "analyzing", "status_message_id": None, "source_kind": kind})

    tmp = config.WORK_DIR / f"dl_{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)
    tg.send_chat_action(inc.chat_id, "upload_video")
    from app import cancel as cancel_mod
    token = cancel_mod.new_token(str(inc.chat_id))
    try:
        if kind == "youtube":
            src = youtube.download_youtube(inc.text, tmp, token=token)
        elif kind == "gdrive":
            src = gdrive.download_gdrive(inc.text, tmp, token=token)
        else:
            src = urls.download_url(inc.text, tmp, token=token)
    except SpectreError as exc:
        cancel_mod.clear(str(inc.chat_id))
        st.delete()
        session_set(inc.chat_id, inc.user_id, {"mode": "idle"})
        msg = getattr(exc, "message", str(exc))
        if "verifikasi" in msg.lower() or "cookies" in msg.lower() or "bot" in msg.lower():
            tg.send_message(inc.chat_id, "🔐 <b>YouTube minta verifikasi</b>\n\n"
                "Coba lagi nanti, atau upload file videonya langsung ke sini.")
        else:
            tg.send_message(inc.chat_id, f"❌ <b>Gagal mengunduh videonya</b>\n\n{msg}\n\n"
                "Coba lagi, atau upload file videonya langsung.")
        return

    _run_pipeline(tg, inc.chat_id, inc.user_id, src, st)


def _start_upload(tg: Telegram, inc: Incoming) -> None:
    st = StatusMessage(tg, inc.chat_id, job8=_job8(inc.chat_id))
    st.update("receive")
    st.start_heartbeat()   # tick during the (blocking) file download
    tmp = config.WORK_DIR / f"up_{int(time.time())}"
    dest = tmp / (inc.file_name or "video.mp4")
    tg.send_chat_action(inc.chat_id, "upload_document")
    if not _download_telegram_file(tg, inc.file_id or "", dest):
        st.delete()
        tg.send_message(inc.chat_id, "❌ <b>Gagal mengunduh file</b>\n\nCoba upload ulang ya.")
        return
    session_set(inc.chat_id, inc.user_id, {"mode": "analyzing", "source_kind": "file"})
    _run_pipeline(tg, inc.chat_id, inc.user_id, dest, st)


def _run_pipeline(tg: Telegram, chat_id: int, user_id: int,
                  source_path: Path, st: StatusMessage) -> None:
    from app.media import analyzer, renderer
    from app import presets as presets_mod
    from app import cancel as cancel_mod
    from app.cancel import JobCancelled

    work = config.WORK_DIR / f"bot_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    token = cancel_mod.new_token(str(chat_id))
    st.job8 = _job8(chat_id)
    st.start_heartbeat()   # keep the timer/spinner moving while we block
    try:
        # Duration guard
        try:
            info = renderer.probe_video(source_path, token=token)
        except SpectreError as exc:
            st.delete()
            session_set(chat_id, user_id, {"mode": "idle"})
            tg.send_message(chat_id, f"❌ <b>Videonya tidak bisa dibaca</b>\n\n{exc}")
            return
        if info.duration and info.duration > config.BOT_MAX_INPUT_SECONDS:
            st.delete()
            session_set(chat_id, user_id, {"mode": "idle"})
            tg.send_message(chat_id,
                f"⚠️ <b>Videonya kepanjangan</b>\n\nVideo ini {info.duration/60:.0f} menit, "
                f"batas {config.BOT_MAX_INPUT_SECONDS//60} menit.")
            return

        st.update("transcribe")
        tg.send_chat_action(chat_id, "typing")
        transcriber = registry.get_transcriber()
        transcript = transcriber.transcribe(str(source_path), str(work), token=token)
        token.check()

        st.update("analyze")
        llm = registry.get_llm()
        moments = analyzer.analyze(transcript, llm=llm)
        token.check()
        if not moments:
            st.delete()
            session_set(chat_id, user_id, {"mode": "idle"})
            tg.send_message(chat_id, "😕 <b>Belum ada momen yang cocok</b>\n\n"
                "Video selesai ditranskripsi, tapi tidak ada potongan yang cukup "
                "'berdiri sendiri'. Coba video dengan percakapan lebih padat ya.")
            return

        st.update("catalog")
        clips_meta = []
        template = presets_mod.active_template()
        for i, m in enumerate(moments[: config.MAX_PROPOSED_CLIPS]):
            clips_meta.append({
                "idx": i, "start": m.start, "end": m.end, "score": m.viral_score,
                "hook": m.hook, "topic": m.topic, "caption": m.caption,
                "reason": m.reason, "rendered": False,
            })
        _save_clips(chat_id, static_clips=clips_meta)

        text = _catalog_text(clips_meta)
        markup = catalog_inline(_job8(chat_id), clips_meta, page=0)
        st.replace(text, reply_markup=markup)

        state = session_get(chat_id)
        state.update({"mode": "awaiting_choice", "clips": clips_meta,
                      "source_path": str(source_path), "template": template,
                      "info_duration": info.duration})
        session_set(chat_id, user_id, state)
    except JobCancelled:
        log.info("Pipeline dibatalkan untuk chat %s", chat_id)
        st.show_cancelled()
        session_set(chat_id, user_id, {"mode": "idle"})
    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline gagal untuk chat %s", chat_id)
        st.delete()
        session_set(chat_id, user_id, {"mode": "idle"})
        tg.send_message(chat_id, f"❌ <b>Terjadi kesalahan</b>\n\n{str(exc)[:200]}")
    finally:
        cancel_mod.clear(str(chat_id))
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _job8(chat_id: int) -> str:
    """Short, stable-ish id derived from chat id (fits callback_data)."""
    import hashlib
    return hashlib.sha1(str(chat_id).encode()).hexdigest()[:8]


# In-memory clip store per chat is insufficient across runs; persist to a file
# in data/ so /clips and rendering survive even a restart within a session.
def _clips_path(chat_id: int) -> Path:
    return config.DATA_DIR / f"bot_clips_{chat_id}.json"


def _save_clips(chat_id: int, static_clips=None, **kw) -> None:
    if static_clips is not None:
        _clips_path(chat_id).write_text(json.dumps(static_clips, ensure_ascii=False),
                                        encoding="utf-8")


def _load_clips(chat_id: int) -> list:
    p = _clips_path(chat_id)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    return []


def _catalog_text(clips: list) -> str:
    lines = [f"🎬 <b>Ketemu {len(clips)} momen bagus!</b>",
             "Pilih klip yang mau dirender. Skor makin tinggi = makin potensial viral.",
             "────────────────"]
    for c in clips[:4]:
        emoji = "🔥" if c["score"] >= 85 else "⚡" if c["score"] >= 70 else "👍" if c["score"] >= 50 else "🙂"
        dur = c["end"] - c["start"]
        lines.append(f"<b>{c['idx']+1}️⃣ {emoji} Skor {c['score']}/100</b>")
        if c.get("topic"):
            lines.append(f"🎯 <i>{c['topic']}</i>")
        if c.get("hook"):
            lines.append(f"💡 \"{c['hook']}\"")
        lines.append(f"⏱️ {int(dur)}s")
        lines.append("")
    return "\n".join(lines)


def _download_telegram_file(tg: Telegram, file_id: str, dest: Path) -> bool:
    info = tg.call("getFile", file_id=file_id)
    if not info or "file_path" not in info:
        return False
    url = f"https://api.telegram.org/file/bot{tg.token}/{info['file_path']}"
    try:
        resp = tg.session.get(url, stream=True, timeout=(30, 300))
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(1024 * 1024):
                if chunk:
                    fh.write(chunk)
        return dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        log.error("Download file gagal: %s", exc)
        return False


def _youtube_duration(url: str) -> Optional[float]:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
        return float(info.get("duration") or 0) or None
    except Exception as exc:  # noqa: BLE001
        log.warning("Tidak bisa baca durasi YouTube: %s", exc)
        return None


# ===========================================================================
# Callback handling (inline buttons)
# ===========================================================================

def handle_callback(tg: Telegram, cq: dict) -> None:
    data = cq.get("data", "") or ""
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
    message_id = (cq.get("message") or {}).get("message_id")
    user_id = (cq.get("from") or {}).get("id")
    cb_id = cq.get("id", "")
    if chat_id is None or user_id is None:
        return
    if not is_allowed(user_id):
        tg.answer_callback(cb_id, "⛔ Tidak diizinkan")
        return

    if data == "noop":
        tg.answer_callback(cb_id)
        return

    op = data.split(":", 1)[0]
    try:
        if op == "r":
            _, job8, idx = data.split(":")
            _render_one(tg, chat_id, user_id, int(idx), cb_id)
        elif op == "rall":
            _render_all(tg, chat_id, user_id, cb_id)
        elif op == "cat":
            _reshow_catalog(tg, chat_id, cb_id)
        elif op == "more":
            _, job8, page = data.split(":")
            _reshow_catalog(tg, chat_id, cb_id, page=int(page))
        elif op == "style":
            tg.answer_callback(cb_id, "Pilih gaya:")
            if message_id is not None:
                tg.edit_reply_markup(chat_id, message_id, styles_inline(job8_of(chat_id)))
        elif op == "set" and data.startswith("set:style:"):
            preset_id = data.split(":", 2)[2].split("|")[0]
            _apply_style(tg, chat_id, preset_id, cb_id)
        elif op == "h":
            tg.answer_callback(cb_id, "Gaya hook bisa diubah via ⚙️ Ubah Gaya.", show_alert=True)
        elif op == "cancel":
            if data.startswith("cancel:yes:"):
                tg.answer_callback(cb_id, "Membatalkan…")
                _really_cancel(tg, chat_id, user_id)
            elif data.startswith("cancel:no:"):
                tg.answer_callback(cb_id, "Lanjut")
            else:
                tg.answer_callback(cb_id, "⏳ Membatalkan…")
                _really_cancel(tg, chat_id, user_id)
        elif op == "done":
            session_set(chat_id, user_id, {"mode": "idle"})
            tg.answer_callback(cb_id, "Selesai")
            tg.send_message(chat_id, "✅ Selesai! Kirim video baru kapan saja 🎬")
        else:
            tg.answer_callback(cb_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Callback gagal: %s", data)
        tg.answer_callback(cb_id, f"Error: {str(exc)[:80]}", show_alert=True)


def job8_of(chat_id: int) -> str:
    return _job8(chat_id)


def _reshow_catalog(tg: Telegram, chat_id: int, cb_id: str, page: int = 0) -> None:
    clips = _load_clips(chat_id)
    tg.answer_callback(cb_id)
    tg.send_message(chat_id, _catalog_text(clips),
                    reply_markup=catalog_inline(_job8(chat_id), clips, page=page))


def _apply_style(tg: Telegram, chat_id: int, preset_id: str, cb_id: str) -> None:
    from app import presets
    tpl = presets.get_preset(preset_id)
    if not tpl:
        tg.answer_callback(cb_id, "Preset tidak ditemukan")
        return
    state = session_get(chat_id)
    state["template"] = tpl
    session_set(chat_id, state.get("user_id", 0), state)
    tg.answer_callback(cb_id, f"Gaya '{preset_id}' diterapkan. Render ulang klipmu.")


def _render_one(tg: Telegram, chat_id: int, user_id: int, idx: int, cb_id: str) -> None:
    clips = _load_clips(chat_id)
    clip = next((c for c in clips if c["idx"] == idx), None)
    state = session_get(chat_id)
    if clip is None or not state.get("source_path"):
        tg.answer_callback(cb_id, "Data klip hilang, kirim video ulang ya")
        return
    tg.answer_callback(cb_id, f"⏳ Menyiapkan Klip {idx+1}…")
    ok = _render_and_send(tg, chat_id, user_id, clip, state)
    if ok:
        clip["rendered"] = True
        _clips_path(chat_id).write_text(json.dumps(clips, ensure_ascii=False), encoding="utf-8")


def _render_all(tg: Telegram, chat_id: int, user_id: int, cb_id: str) -> None:
    clips = _load_clips(chat_id)
    state = session_get(chat_id)
    if not state.get("source_path"):
        tg.answer_callback(cb_id, "Kirim video ulang ya")
        return
    tg.answer_callback(cb_id, "⏳ Merender semua…")
    todo = [c for c in clips if not c.get("rendered")][: config.MAX_CLIPS]
    sent = 0
    for c in todo:
        if not _render_and_send(tg, chat_id, user_id, c, state):
            # cancelled mid-way: stop the loop, keep what was sent
            _clips_path(chat_id).write_text(json.dumps(clips, ensure_ascii=False), encoding="utf-8")
            return
        c["rendered"] = True
        sent += 1
    _clips_path(chat_id).write_text(json.dumps(clips, ensure_ascii=False), encoding="utf-8")
    if sent:
        tg.send_message(chat_id, f"✅ <b>{sent} klip terkirim!</b> 🎉",
                        reply_markup=followup_inline(_job8(chat_id)))


def _render_and_send(tg: Telegram, chat_id: int, user_id: int, clip: dict, state: dict) -> bool:
    """Render one clip and send it. Returns False if cancelled."""
    from app.media import renderer
    from app import cancel as cancel_mod
    from app.cancel import JobCancelled
    st = StatusMessage(tg, chat_id, job8=_job8(chat_id))
    st.update("render", f"🎬 Klip {clip['idx']+1}: \"{clip.get('hook','')}\"")
    st.start_heartbeat()
    tg.send_chat_action(chat_id, "upload_video")
    work = config.WORK_DIR / f"rnd_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    out = config.OUTPUT_DIR / f"bot_{chat_id}_{int(time.time())}_{clip['idx']}.mp4"
    token = cancel_mod.new_token(str(chat_id))
    try:
        import shutil
        source = Path(state["source_path"])
        transcript = _cached_or_transcribe(tg, source, work, token=token)
        if transcript is None:
            st.delete()
            tg.send_message(chat_id, "❌ Gagal menyiapkan subtitle. Coba kirim video ulang.")
            return True
        renderer.render_clip(
            source, transcript.segments,
            start=clip["start"], end=clip["end"], out_path=out, work_dir=work,
            hook_text=clip.get("hook", ""), info=renderer.probe_video(source, token=token),
            template=state.get("template"),
            subtitle_mode=(state.get("template") or {}).get("subtitle", {}).get("mode", "karaoke"),
            token=token,
        )
        token.check()
        st.delete()
        caption = f"🔥 Skor {clip['score']}/100 — {clip.get('topic','')}\n\n💡 Hook: \"{clip.get('hook','')}\"\n{clip.get('caption','')}"
        if tg.send_video(chat_id, str(out), caption):
            tg.send_message(chat_id, "✅ Selesai! Mau lanjut?", reply_markup=followup_inline(_job8(chat_id)))
        else:
            tg.send_message(chat_id, "❌ Gagal mengirim klip. Coba render lagi.")
        return True
    except JobCancelled:
        log.info("Render dibatalkan untuk chat %s", chat_id)
        st.show_cancelled()
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("Render klip gagal")
        st.delete()
        tg.send_message(chat_id, f"❌ <b>Klip gagal dirender</b>\n\n{str(exc)[:150]}\n\n"
                                 "Coba lagi ya.")
        return True
    finally:
        cancel_mod.clear(str(chat_id))
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        out.unlink(missing_ok=True)


def _cached_or_transcribe(tg: Telegram, source: Path, work: Path,
                          state: Optional[dict] = None, *, token=None):
    """Re-transcribe the source to render a single clip (uses the transcript
    cache when present, so this is fast on a repeat render)."""
    from app.media import transcriber
    try:
        return transcriber.transcribe(source, work, token=token)
    except SpectreError as exc:
        log.warning("Transkripsi ulang gagal: %s", exc)
        return None


def _show_settings(tg: Telegram, chat_id: int) -> None:
    from app import presets
    state = session_get(chat_id)
    style = (state.get("template") or {}).get("name", "Default")
    tg.send_message(chat_id,
        f"⚙️ <b>Pengaturan</b>\n\n🎨 Gaya hook: <b>{style}</b>\n"
        f"🎙️ Model transkripsi: <code>{config.WHISPER_MODEL}</code>\n"
        f"📦 Maks klip: <b>{config.MAX_CLIPS}</b>",
        reply_markup=styles_inline(_job8(chat_id)))


def _show_last_catalog(tg: Telegram, chat_id: int) -> None:
    clips = _load_clips(chat_id)
    if not clips:
        tg.send_message(chat_id, "📭 <b>Belum ada klip</b>\n\nKirim link atau video dulu ya 👇")
        return
    tg.send_message(chat_id, _catalog_text(clips),
                    reply_markup=catalog_inline(_job8(chat_id), clips))


def _really_cancel(tg: Telegram, chat_id: int, user_id: int) -> None:
    """Actually stop any running job for this chat."""
    from app import cancel as cancel_mod
    found = cancel_mod.cancel_key(str(chat_id))
    # Remove from registry so a new job starts clean. The running worker holds
    # its own reference to the token, so its `cancelled` flag stays True even
    # after the registry entry is gone.
    cancel_mod.clear(str(chat_id))
    session_set(chat_id, user_id, {"mode": "idle"})
    if found:
        tg.send_message(chat_id, "🛑 <b>Dibatalkan</b>\n\nProses dihentikan. "
                                 "Kirim video baru kapan saja ya 👋")
    else:
        tg.send_message(chat_id, "Tidak ada proses aktif. Kirim video baru kapan saja 🎬")


def _handle_cancel(tg: Telegram, chat_id: int, user_id: int) -> None:
    from app import cancel as cancel_mod
    state = session_get(chat_id)
    has_job = cancel_mod.get_token(str(chat_id)) is not None
    if state.get("mode") in ("analyzing", "rendering") or has_job:
        tg.send_message(chat_id, "🛑 <b>Batalkan proses?</b>\n\n"
                                 "Kalau ya, proses yang sedang jalan akan dihentikan.",
                        reply_markup=confirm_cancel_inline(_job8(chat_id)))
    else:
        tg.send_message(chat_id, "Tidak ada proses yang berjalan. Kirim video baru kapan saja 🎬")


# ===========================================================================
# Main loop
# ===========================================================================

COMMANDS = [
    {"command": "start", "description": "🎬 Menu utama"},
    {"command": "help", "description": "📖 Panduan"},
    {"command": "clips", "description": "✂️ Klip terakhir"},
    {"command": "status", "description": "⏳ Status proses"},
    {"command": "settings", "description": "⚙️ Pengaturan"},
    {"command": "cancel", "description": "❌ Batalkan"},
]


def run(once: bool = False, budget_s: Optional[int] = None) -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diset (isi di .env atau secrets Actions).")

    config.ensure_dirs()
    migrate.ensure_schema()
    storage.init_db()
    restore()

    tg = Telegram(config.TELEGRAM_BOT_TOKEN)
    me = tg.call("getMe")
    if not me:
        raise SystemExit("Token bot tidak valid (getMe gagal).")
    tg.set_my_commands(COMMANDS)

    # Budget: 0 or None means "run forever" (local/VPS). A positive value caps
    # the run (used for GitHub Actions so cron jobs don't overlap).
    budget = budget_s if budget_s is not None else config.BOT_POLL_SECONDS
    has_deadline = bool(budget and budget > 0)
    log.info("Bot @%s online (once=%s, budget=%s)",
             me.get("username"), once, f"{budget}s" if has_deadline else "unlimited")

    offset = int(_meta_get("update_offset") or 0)
    deadline = (time.time() + budget) if has_deadline else None

    while True:
        updates = tg.get_updates(offset, config.BOT_LONGPOLL_TIMEOUT)
        if updates:
            for upd in updates:
                new_correlation_id("tg")
                try:
                    handle_update(tg, upd)
                except Exception:  # noqa: BLE001
                    log.exception("Error handling update %s", upd.get("update_id"))
                offset = max(offset, int(upd["update_id"]) + 1)
            _meta_set("update_offset", str(offset))

        if once:
            break
        if deadline is not None and time.time() >= deadline:
            log.info("Polling budget habis; persist + keluar")
            break

    persist()


def main() -> None:
    setup_logging(log_dir=config.LOG_DIR, level=getattr(logging, config.LOG_LEVEL, logging.INFO))
    parser = argparse.ArgumentParser(description="SpectreMotion Telegram bot")
    parser.add_argument("--once", action="store_true", help="Process pending updates then exit")
    parser.add_argument("--seconds", type=int, default=None, help="Polling budget in seconds")
    args = parser.parse_args()
    run(once=args.once, budget_s=args.seconds)


if __name__ == "__main__":
    main()
