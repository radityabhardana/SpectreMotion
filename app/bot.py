"""SpectreMotion Telegram bot — long-polling, designed for GitHub Actions.

Design constraints (ephemeral runner):
- No web server, no inbound connections. The bot *polls* Telegram via getUpdates.
- The getUpdates offset is persisted so polling resumes across runs.
- Videos are processed in-run (download -> transcribe -> analyze -> render ->
  send) and NOT committed; only the small SQLite state is persisted.
- A run is bounded by BOT_POLL_SECONDS so a cron window never overruns.

Run locally:      python -m app.bot
Run one shot:     python -m app.bot --once
In Actions:       cron → python -m app.bot  (GIT_PERSIST=1)

This deliberately reuses the SAME engine as the rest of the app
(app.media.*, app.campaign, app.pipeline) — no logic is duplicated here.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app import config, storage
from app.adapters import registry
from app.db import migrate
from app.errors import SpectreError
from app.ingest import gdrive, urls, youtube
from app.logging_setup import new_correlation_id, setup_logging
from app.state_store import persist, restore

log = logging.getLogger("spectre.bot")

_API = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# Telegram transport (raw HTTP — no framework needed for long-polling)
# ---------------------------------------------------------------------------

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
                    log.warning("Telegram %s error: %s", method, data.get("description"))
                    return None
                return data.get("result")
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram %s failed (%d/3): %s", method, attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        return None

    def get_updates(self, offset: Optional[int], timeout: int):
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def send_message(self, chat_id: int, text: str, **extra):
        return self.call("sendMessage", chat_id=chat_id, text=text[:4096],
                         parse_mode="HTML", **extra)

    def send_video(self, chat_id: int, path: str, caption: str = "", **extra):
        import requests
        try:
            with open(path, "rb") as fh:
                resp = self.session.post(
                    self._url("sendVideo"),
                    data={"chat_id": chat_id, "caption": caption[:1024],
                          "supports_streaming": "true", **extra},
                    files={"video": fh}, timeout=300,
                )
            data = resp.json()
            if not data.get("ok"):
                log.warning("sendVideo failed: %s", data.get("description"))
            return data
        except requests.RequestException as exc:
            log.error("sendVideo exception: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Access control + session state
# ---------------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    if not config.TELEGRAM_ALLOWED_USERS:
        return True  # open (local/dev). Set the allowlist for anything public.
    return user_id in config.TELEGRAM_ALLOWED_USERS


def _meta_get(key: str) -> Optional[str]:
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _meta_set(key: str, value: str) -> None:
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO bot_meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _touch_user(user_id: int, username: str) -> None:
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO telegram_users(tg_user_id, username, created_at, last_seen) "
            "VALUES (?,?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET "
            "username=excluded.username, last_seen=excluded.last_seen",
            (user_id, username, time.time(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

@dataclass
class Incoming:
    chat_id: int
    user_id: int
    username: str
    text: str = ""
    file_id: Optional[str] = None
    file_name: str = ""


HELP_TEXT = (
    "<b>SpectreMotion</b> — ubah video panjang jadi klip vertikal.\n\n"
    "Kirim:\n"
    "• <b>link</b> YouTube / Google Drive, atau\n"
    "• <b>file video</b> (mp4/mov/mkv).\n\n"
    "Bot akan transkripsi, pilih momen viral, dan kirim klip 9:16 ke kamu.\n"
    "Perintah: /start /help /status"
)


def handle_update(tg: Telegram, upd: dict) -> None:
    msg = upd.get("message") or upd.get("callback_query", {}).get("message") or {}
    frm = upd.get("message", {}).get("from") or upd.get("callback_query", {}).get("from") or {}
    chat_id = msg.get("chat", {}).get("id")
    user_id = frm.get("id")
    if chat_id is None or user_id is None:
        return

    username = frm.get("username", "") or frm.get("first_name", "")
    _touch_user(user_id, username)

    if not is_allowed(user_id):
        tg.send_message(chat_id, "⛔ Maaf, kamu tidak diizinkan memakai bot ini.")
        return

    text = (msg.get("text") or "").strip()
    document = msg.get("document") or msg.get("video") or {}

    # Commands
    if text.startswith("/start") or text.startswith("/help"):
        tg.send_message(chat_id, HELP_TEXT)
        return
    if text.startswith("/status"):
        tg.send_message(chat_id, f"✅ Aktif. Model: <code>{config.WHISPER_MODEL}</code>")
        return

    # URL ingestion
    if text and (urls.is_youtube_url(text) or urls.is_gdrive_url(text) or text.startswith("http")):
        if urls.is_gdrive_folder_url(text):
            tg.send_message(chat_id, "❌ Link folder Drive tidak didukung. Share file individual.")
            return
        _process_url(tg, chat_id, user_id, text)
        return

    # File upload
    if document.get("file_id"):
        _process_upload(tg, chat_id, user_id, document)
        return

    tg.send_message(chat_id, "Kirim <b>link</b> YouTube/Drive atau <b>file video</b>. Ketik /help.")


# ---------------------------------------------------------------------------
# Processing (reuses the shared engine)
# ---------------------------------------------------------------------------

def _download_telegram_file(tg: Telegram, file_id: str, dest: Path) -> bool:
    info = tg.call("getFile", file_id=file_id)
    if not info or "file_path" not in info:
        return False
    url = f"https://api.telegram.org/file/bot{tg.token}/{info['file_path']}"
    try:
        resp = tg.session.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
        return dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        log.error("Download file gagal: %s", exc)
        return False


def _run_pipeline(tg: Telegram, chat_id: int, source_path: Path) -> None:
    """Process one source file and send clips back. Uses the shared engine."""
    from app.media import analyzer, renderer

    work = config.WORK_DIR / f"bot_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        # Guard: reject absurdly long inputs before we burn minutes transcribing.
        from app.media.renderer import probe_video
        try:
            info = probe_video(source_path)
        except SpectreError as exc:
            tg.send_message(chat_id, f"❌ Video tidak bisa dibaca: {exc}")
            return
        limit = config.BOT_MAX_INPUT_SECONDS
        if info.duration and info.duration > limit:
            tg.send_message(
                chat_id,
                f"⚠️ Video terlalu panjang: <b>{info.duration/60:.0f} menit</b>. "
                f"Batas {limit//60} menit. Kirim video yang lebih pendek ya "
                f"(sistem ini untuk video pendek-menengah, bukan film/3 jam).",
            )
            return

        tg.send_message(chat_id, "🎬 Memproses… (transkripsi bisa beberapa menit)")

        transcriber = registry.get_transcriber()
        llm = registry.get_llm()

        transcript = transcriber.transcribe(str(source_path), str(work))
        moments = analyzer.analyze(transcript, llm=llm)
        if not moments:
            tg.send_message(chat_id, "😕 Tidak menemukan momen klip yang layak.")
            return

        from app import presets as presets_mod
        template = presets_mod.active_template()

        to_render = moments[: config.MAX_CLIPS]
        sent = 0
        for i, m in enumerate(to_render):
            tg.send_message(chat_id, f"✂️ Render klip {i + 1}/{len(to_render)}…")
            out = config.OUTPUT_DIR / f"bot_{chat_id}_{int(time.time())}_{i}.mp4"
            try:
                renderer.render_clip(
                    source_path, transcript.segments,
                    start=m.start, end=m.end, out_path=out, work_dir=work,
                    hook_text=m.hook, info=info, template=template,
                    subtitle_mode=(template or {}).get("subtitle", {}).get("mode", "karaoke"),
                )
            except SpectreError as exc:
                log.warning("Render klip %d gagal: %s", i, exc)
                continue
            caption = f"🔥 {m.viral_score}/100 — {m.hook}\n{m.caption}"
            if tg.send_video(chat_id, str(out), caption):
                sent += 1
            out.unlink(missing_ok=True)  # video does not persist

        if sent:
            tg.send_message(chat_id, f"✅ Selesai — {sent} klip terkirim.")
        else:
            tg.send_message(chat_id, "❌ Gagal merender klip. Coba video lain.")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _youtube_duration(url: str) -> Optional[float]:
    """Return a YouTube video's duration in seconds WITHOUT downloading it."""
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
        return float(info.get("duration") or 0) or None
    except Exception as exc:  # noqa: BLE001
        log.warning("Tidak bisa baca durasi YouTube: %s", exc)
        return None


def _load_template() -> Optional[dict]:
    import json
    p = config.TEMPLATE_PATH
    if not Path(p).is_absolute():
        p = config.REPO_ROOT / p
    if Path(p).is_file():
        try:
            return json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _process_url(tg: Telegram, chat_id: int, user_id: int, url: str) -> None:
    kind = urls.classify(url)

    # Pre-flight: for YouTube, read the duration WITHOUT downloading so we can
    # reject a 3-hour video before wasting time/bandwidth on it.
    if kind == "youtube":
        dur = _youtube_duration(url)
        if dur and dur > config.BOT_MAX_INPUT_SECONDS:
            tg.send_message(
                chat_id,
                f"⚠️ Video ini <b>{dur/60:.0f} menit</b> — terlalu panjang "
                f"(batas {config.BOT_MAX_INPUT_SECONDS//60} menit). "
                f"Kirim klip yang lebih pendek ya.",
            )
            return

    tg.send_message(chat_id, f"⬇️ Mengunduh dari <b>{kind}</b>…")
    tmp = config.WORK_DIR / f"dl_{int(time.time())}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "youtube":
            src = youtube.download_youtube(url, tmp)
        elif kind == "gdrive":
            src = gdrive.download_gdrive(url, tmp)
        else:
            src = urls.download_url(url, tmp)
    except SpectreError as exc:
        tg.send_message(chat_id, f"❌ Gagal mengunduh: {exc.message if hasattr(exc, 'message') else exc}")
        return
    _run_pipeline(tg, chat_id, src)


def _process_upload(tg: Telegram, chat_id: int, user_id: int, doc: dict) -> None:
    name = doc.get("file_name", "upload.mp4")
    ext = Path(name).suffix.lower()
    if ext and ext not in config.SUPPORTED_EXTENSIONS:
        tg.send_message(chat_id, f"❌ Format {ext} tidak didukung.")
        return
    tmp = config.WORK_DIR / f"up_{int(time.time())}"
    dest = tmp / (name or "upload.mp4")
    tg.send_message(chat_id, "⬇️ Mengunduh file…")
    if not _download_telegram_file(tg, doc["file_id"], dest):
        tg.send_message(chat_id, "❌ Gagal mengunduh file.")
        return
    _run_pipeline(tg, chat_id, dest)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

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
    log.info("Bot @%s online (once=%s, budget=%ss)",
             me.get("username"), once, budget_s or config.BOT_POLL_SECONDS)

    offset = int(_meta_get("update_offset") or 0)
    deadline = time.time() + (budget_s or config.BOT_POLL_SECONDS)

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
        if time.time() >= deadline:
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
