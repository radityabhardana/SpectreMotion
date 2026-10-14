"""Manual Whisper model downloader for networks where huggingface_hub hangs.

Some ISPs blackhole `cdn-lfs.huggingface.co`, which makes `huggingface_hub`
stall. A plain `requests.get()` follows the redirect to `us.aws.cdn.hf.co`
(which works), so this script downloads the model files directly and drops
them into the HF cache layout faster-whisper expects.

Usage:
    python scripts/fetch_whisper.py tiny
    python scripts/fetch_whisper.py base
    python scripts/fetch_whisper.py small
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

REPO_MAP = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
}
FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]


def _cache_root() -> Path:
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _download_file(url: str, dest: Path, retries: int = 8) -> None:
    """Download with HTTP Range resume + retries (handles flaky connections)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        already = tmp.stat().st_size if tmp.is_file() else 0
        headers = {"Range": f"bytes={already}-"} if already else {}
        try:
            with requests.get(url, stream=True, timeout=(15, 60), headers=headers) as r:
                if already and r.status_code == 200:
                    already = 0  # server ignored Range; start over
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0)) + already
                mode = "ab" if already else "wb"
                got = already
                t0 = time.time()
                with tmp.open(mode) as fh:
                    for chunk in r.iter_content(1024 * 512):
                        if chunk:
                            fh.write(chunk)
                            got += len(chunk)
                            if got % (5 * 1024 * 1024) < 512 * 1024:
                                speed = (got - already) / max(0.1, time.time() - t0) / 1e6
                                pct = (got / total * 100) if total else 0
                                print(f"      {got/1e6:.1f}/{total/1e6:.1f} MB ({pct:.0f}%) {speed:.1f} MB/s",
                                      flush=True)
                if total and got < total:
                    raise IOError(f"terputus di {got/1e6:.1f}/{total/1e6:.1f} MB")
                tmp.replace(dest)
                print(f"    done {got/1e6:.1f} MB")
                return
        except (requests.RequestException, IOError, OSError) as exc:
            print(f"    attempt {attempt}/{retries} gagal: {str(exc)[:80]} — resume...", flush=True)
            if attempt == retries:
                raise SystemExit(1)
            time.sleep(3)


def download(size: str) -> None:
    if size not in REPO_MAP:
        raise SystemExit(f"Ukuran tidak dikenal: {size}. Pilih: {', '.join(REPO_MAP)}")
    repo = REPO_MAP[size]
    repo_dir = _cache_root() / ("models--" + repo.replace("/", "--"))
    blobs = repo_dir / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)

    base = f"https://huggingface.co/{repo}/resolve/main"
    print(f"Download model '{size}' ({repo}) -> {blobs}")
    for name in FILES:
        url = f"{base}/{name}"
        dest = blobs / name
        if dest.is_file() and dest.stat().st_size > 0:
            print(f"  [ok] {name} sudah ada ({dest.stat().st_size/1e6:.1f} MB)")
            continue
        print(f"  [dl] {name} ...", flush=True)
        _download_file(url, dest)

    # Create the snapshot symlink dir so faster-whisper finds the model.
    snaps = repo_dir / "snapshots" / "main"
    snaps.mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text("main")
    for name in FILES:
        blob = blobs / name
        link = snaps / name
        if blob.is_file():
            if link.exists():
                link.unlink()
            try:
                link.symlink_to(blob)
            except OSError:
                import shutil
                shutil.copy2(blob, link)
    print(f"\nSELESAI. Model '{size}' siap dipakai.")
    print("Sekarang jalankan: python -m app.bot")


if __name__ == "__main__":
    size = sys.argv[1] if len(sys.argv) > 1 else "tiny"
    download(size)
