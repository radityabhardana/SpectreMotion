"""URL classification + raw download helpers.

Kept thin and dependency-tolerant: yt-dlp is imported lazily so tests and the
web process don't pay for it unless ingestion actually runs.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app import config
from app.errors import IngestError, UnsupportedMediaError

log = logging.getLogger("spectre.ingest")

_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_GDRIVE_FILE_RE = re.compile(
    r"(?:drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)|docs\.google\.com/file/d/)"
    r"([A-Za-z0-9_-]{20,})"
)
_GDRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{20,})")


def is_youtube_url(url: str) -> bool:
    return bool(_YOUTUBE_RE.search(url or ""))


def is_gdrive_url(url: str) -> bool:
    return bool(_GDRIVE_FILE_RE.search(url or ""))


def is_gdrive_folder_url(url: str) -> bool:
    return bool(_GDRIVE_FOLDER_RE.search(url or ""))


def extract_gdrive_id(url: str) -> Optional[str]:
    m = _GDRIVE_FILE_RE.search(url or "")
    return m.group(1) if m else None


def classify(url: str) -> str:
    """Return 'youtube' | 'gdrive' | 'other'."""
    if is_youtube_url(url):
        return "youtube"
    if is_gdrive_url(url):
        return "gdrive"
    return "other"


def _cookie_file() -> Optional[Path]:
    path = config.DATA_DIR / "cookies_shared.txt"
    if path.is_file() and path.stat().st_size > 50:
        return path
    return None


def download_url(url: str, out_dir: Path, *, max_height: int = 1080,
                 token=None) -> Path:
    """Download a generic URL via yt-dlp into out_dir; return the media file."""
    if token is not None:
        token.check()
    try:
        import yt_dlp
    except ImportError as exc:
        raise IngestError("yt-dlp tidak terinstall. Jalankan: pip install yt-dlp") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out_dir / "source.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
    }
    cookie = _cookie_file()
    if cookie is not None:
        opts["cookiefile"] = str(cookie)

    log.info("Downloading %s", url[:60])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises broad exceptions
        raise IngestError(f"Download gagal: {exc}") from exc
    if token is not None:
        token.check()

    candidates = [p for p in out_dir.glob("source.*")
                  if p.suffix.lower() in config.SUPPORTED_EXTENSIONS
                  or p.suffix.lower() == ".mp4"]
    if not candidates:
        raise IngestError("Download selesai tapi file media tidak ditemukan")
    return candidates[0]


def save_cookies(raw: bytes) -> Path:
    """Validate + store the shared YouTube cookies file."""
    text = raw.decode("utf-8", errors="ignore")
    if "youtube" not in text and ".youtube.com" not in text:
        raise UnsupportedMediaError("File tidak terlihat seperti cookies YouTube")
    path = config.DATA_DIR / "cookies_shared.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)
    return path


def has_cookies() -> bool:
    return _cookie_file() is not None
