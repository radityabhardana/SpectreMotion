"""YouTube ingestion — transcript-first, download only when needed.

For the MVP pipeline we download the whole (bounded-height) video, but this
module also exposes caption fetching so a future optimization can analyze
captions without downloading. Cookie handling is shared and per-run isolated.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from app import config
from app.errors import IngestError

log = logging.getLogger("spectre.youtube")


def _cookie_path() -> Optional[Path]:
    p = config.DATA_DIR / "cookies_shared.txt"
    return p if p.is_file() and p.stat().st_size > 50 else None


def download_youtube(url: str, out_dir: Path, *, max_height: int = 1080,
                     token=None) -> Path:
    if token is not None:
        token.check()
    try:
        import yt_dlp
    except ImportError as exc:
        raise IngestError("yt-dlp tidak terinstall. Jalankan: pip install yt-dlp") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    # Per-run cookie copy so concurrent downloads don't clobber a shared file.
    cookie_tmp: Optional[Path] = None
    cookie = _cookie_path()
    if cookie is not None:
        cookie_tmp = out_dir / "cookies_ytdlp.txt"
        shutil.copy2(cookie, cookie_tmp)

    opts = {
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        # Fail fast instead of hanging forever on a stalled connection.
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    if cookie_tmp is not None:
        opts["cookiefile"] = str(cookie_tmp)

    log.info("Downloading YouTube %s", url[:60])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "sign in" in msg or "bot" in msg or "cookies" in msg:
            raise IngestError(
                "YouTube minta verifikasi. Upload cookies (menu /cookies) lalu coba lagi."
            ) from exc
        raise IngestError(f"Download YouTube gagal: {exc}") from exc
    finally:
        if cookie_tmp is not None:
            cookie_tmp.unlink(missing_ok=True)

    candidates = [p for p in out_dir.glob("source.*")
                  if p.suffix.lower() in config.SUPPORTED_EXTENSIONS
                  or p.suffix.lower() == ".mp4"]
    if not candidates:
        raise IngestError("Download YouTube selesai tapi file tidak ditemukan")
    if token is not None:
        token.check()
    return candidates[0]


def fetch_youtube_captions(url: str, out_dir: Path) -> Optional[str]:
    """Best-effort caption fetch (VTT text). Returns None when unavailable."""
    try:
        import yt_dlp
    except ImportError:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["id", "en"],
        "subtitlesformat": "vtt",
        "outtmpl": str(out_dir / "cap.%(ext)s"),
        "quiet": True,
        "noprogress": True,
    }
    cookie = _cookie_path()
    if cookie is not None:
        opts["cookiefile"] = str(cookie)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        log.warning("Caption fetch gagal: %s", exc)
        return None
    vtts = list(out_dir.glob("cap*.vtt"))
    if not vtts:
        return None
    return vtts[0].read_text(encoding="utf-8", errors="ignore")
