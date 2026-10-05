"""SpectreMotion — config loader.

All runtime settings come from environment variables (loaded from `.env`).
Relative paths are resolved against the repo root so the app works no matter
the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _path(name: str, default: str) -> Path:
    value = os.getenv(name, default) or default
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---- Server ----
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", 8080)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# ---- Data & storage ----
DATA_DIR = _path("DATA_DIR", "data")
DB_PATH = _path("DB_PATH", "data/app.db")
UPLOAD_DIR = _path("UPLOAD_DIR", "data/uploads")
WORK_DIR = _path("WORK_DIR", "data/work")
OUTPUT_DIR = _path("OUTPUT_DIR", "data/output")
TRANSCRIPT_CACHE_DIR = _path("TRANSCRIPT_CACHE_DIR", "data/cache/transcripts")
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 2048)

# ---- Logging ----
LOG_DIR = _path("LOG_DIR", "data/logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_BACKUP_COUNT = _int("LOG_BACKUP_COUNT", 3)

# ---- Auth ----
APP_TOKEN = os.getenv("APP_TOKEN", "")

# ---- Telegram bot ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Comma-separated numeric Telegram user ids allowed to use the bot.
# Empty = allow everyone (NOT recommended for a public bot).
TELEGRAM_ALLOWED_USERS = [
    int(x.strip()) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]
# Polling window (seconds) for one Actions run; keeps jobs bounded.
BOT_POLL_SECONDS = _int("BOT_POLL_SECONDS", 4 * 60 + 30)
# Long-poll timeout per getUpdates call.
BOT_LONGPOLL_TIMEOUT = _int("BOT_LONGPOLL_TIMEOUT", 30)
# Max video duration (seconds) accepted from users — protects the small runner.
BOT_MAX_INPUT_SECONDS = _int("BOT_MAX_INPUT_SECONDS", 20 * 60)

# ---- Git persistence (for ephemeral GitHub Actions runs) ----
GIT_PERSIST = _bool("GIT_PERSIST", False)
GIT_PUSH = _bool("GIT_PUSH", True)

# ---- Drivers (Phase 1 seams; defaults keep current behaviour) ----
QUEUE_DRIVER = os.getenv("QUEUE_DRIVER", "sqlite")
STORAGE_DRIVER = os.getenv("STORAGE_DRIVER", "local")
LLM_DRIVER = os.getenv("LLM_DRIVER", "openai")
TRANSCRIBER_DRIVER = os.getenv("TRANSCRIBER_DRIVER", "faster_whisper")
JOB_LEASE_SECONDS = _int("JOB_LEASE_SECONDS", 300)

# ---- Transcription ----
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "") or None
WHISPER_BEAM_SIZE = _int("WHISPER_BEAM_SIZE", 3)
WHISPER_VAD_FILTER = _bool("WHISPER_VAD_FILTER", True)

# ---- LLM ----
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "auto")
LLM_TIMEOUT_S = _int("LLM_TIMEOUT_S", 120)
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 3)
LLM_RETRY_BACKOFF_S = _float("LLM_RETRY_BACKOFF_S", 5)
AI_TRANSCRIPT_CHUNK_CHARS = _int("AI_TRANSCRIPT_CHUNK_CHARS", 12000)
AI_MAX_TRANSCRIPT_CHUNKS = _int("AI_MAX_TRANSCRIPT_CHUNKS", 12)

# ---- Rendering ----
TARGET_WIDTH = _int("TARGET_WIDTH", 1080)
TARGET_HEIGHT = _int("TARGET_HEIGHT", 1920)
MAX_CLIPS = _int("MAX_CLIPS", 3)
MAX_PROPOSED_CLIPS = _int("MAX_PROPOSED_CLIPS", 25)
MIN_CLIP_DURATION_S = _float("MIN_CLIP_DURATION_S", 1)
MAX_CLIP_DURATION_S = _float("MAX_CLIP_DURATION_S", 120)
FFMPEG_THREADS = _int("FFMPEG_THREADS", 2)
X264_PRESET = os.getenv("X264_PRESET", "medium")
X264_CRF = os.getenv("X264_CRF", "21")
X264_MAXRATE = os.getenv("X264_MAXRATE", "8M")
X264_BUFSIZE = os.getenv("X264_BUFSIZE", "16M")
AUDIO_LOUDNORM_FILTER = os.getenv(
    "AUDIO_LOUDNORM_FILTER", "loudnorm=I=-16:TP=-1.5:LRA=11"
)

# ---- Subtitles / hook ----
SUBTITLE_FONT_NAME = os.getenv("SUBTITLE_FONT_NAME", "DejaVu Sans Bold")
SUBTITLE_FONT_PATH = os.getenv("SUBTITLE_FONT_PATH", "")
TEMPLATE_PATH = _path("TEMPLATE_PATH", "templates/default_hook.json")

# ---- Worker ----
WORKER_POLL_INTERVAL_S = _float("WORKER_POLL_INTERVAL_S", 3)
WORKER_ONCE = _bool("WORKER_ONCE", False)

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, WORK_DIR, OUTPUT_DIR, DB_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
