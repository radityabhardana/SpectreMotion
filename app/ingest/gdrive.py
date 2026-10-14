"""Google Drive ingestion for public ('anyone with link') files.

Two-tier: try yt-dlp's GoogleDrive extractor, then fall back to a direct-stream
download that parses the virus-scan confirmation page. No OAuth — public links
only, by design (keeps the credential surface small).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app import config
from app.errors import IngestError
from app.ingest.urls import extract_gdrive_id

log = logging.getLogger("spectre.gdrive")


class GDrivePermissionError(IngestError):
    pass


class GDriveQuotaError(IngestError):
    pass


def download_gdrive(url: str, out_dir: Path, *, max_height: int = 1080,
                    token=None) -> Path:
    if token is not None:
        token.check()
    file_id = extract_gdrive_id(url)
    if not file_id:
        raise IngestError("Link Google Drive tidak valid")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = _via_ytdlp(url, out_dir, max_height)
    except IngestError as exc:
        log.warning("yt-dlp GDrive gagal (%s); coba direct-stream", exc)
        result = _via_direct(file_id, out_dir)
    if token is not None:
        token.check()
    return result


def _via_ytdlp(url: str, out_dir: Path, max_height: int) -> Path:
    try:
        import yt_dlp
    except ImportError as exc:
        raise IngestError("yt-dlp tidak terinstall") from exc

    outtmpl = str(out_dir / "source.%(ext)s")
    opts = {"outtmpl": outtmpl, "quiet": True, "noprogress": True,
            "format": f"bv*[height<={max_height}]+ba/b/b"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "permission" in msg or "private" in msg or "login" in msg:
            raise GDrivePermissionError("File Drive bersifat privat. Set 'Anyone with the link'.") from exc
        if "quota" in msg:
            raise GDriveQuotaError("Kuota Drive habis. Coba lagi nanti.") from exc
        raise IngestError(f"yt-dlp GDrive gagal: {exc}") from exc

    candidates = [p for p in out_dir.glob("source.*")
                  if p.suffix.lower() in config.SUPPORTED_EXTENSIONS
                  or p.suffix.lower() == ".mp4"]
    if not candidates:
        raise IngestError("File Drive tidak ditemukan setelah download")
    return candidates[0]


def _via_direct(file_id: str, out_dir: Path) -> Path:
    import requests

    session = requests.Session()
    base = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download", "confirm": "t"}
    try:
        head = session.get(base, params=params, stream=True, timeout=60,
                           allow_redirects=True)
    except requests.RequestException as exc:
        raise IngestError(f"Gagal menghubungi Drive: {exc}") from exc

    if head.status_code in (401, 403):
        raise GDrivePermissionError("Akses Drive ditolak (privat).")
    head.raise_for_status()

    # Handle the HTML confirmation page.
    ctype = head.headers.get("Content-Type", "")
    if "text/html" in ctype:
        html = head.text
        m = re.search(r'confirm=([0-9A-Za-z_-]+)', html)
        form = re.search(r'action="([^"]+)"', html)
        if form and m:
            params["confirm"] = m.group(1)
            head = session.get(form.group(1), params=params, stream=True, timeout=60)
        else:
            raise IngestError("Drive menampilkan halaman konfirmasi yang tidak dikenali")

    filename = _safe_filename(_filename_from(head, file_id))
    dest = out_dir / filename
    log.info("Streaming Drive file -> %s", dest.name)
    try:
        with dest.open("wb") as fh:
            for chunk in head.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    except OSError as exc:
        raise IngestError(f"Gagal menyimpan file Drive: {exc}") from exc

    if dest.stat().st_size == 0:
        raise IngestError("File Drive kosong")
    return dest


def _filename_from(resp, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^"]+)"?', cd)
    return m.group(1) if m else f"{fallback}.mp4"


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "source.mp4"
    ext = Path(name).suffix.lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        name = Path(name).stem + ".mp4"
    return name
