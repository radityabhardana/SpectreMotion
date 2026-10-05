"""Local filesystem StoragePort implementation.

Keys are server-constructed, opaque, and mapped to a path under a single root
with a containment check to make traversal impossible even if a key is somehow
user-influenced.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import BinaryIO, Optional

from app import config
from app.errors import BackendError, NotFoundError, ValidationError
from app.ports import BlobMeta, StoredBlob

log = logging.getLogger("spectre.storage")

_ROOT = config.DATA_DIR / "blobs"


def _safe_path(key: str) -> Path:
    if not key or key.startswith("/") or "\\" in key:
        raise ValidationError(f"storage key tidak valid: {key!r}")
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValidationError("storage key mengandung '..'")
    candidate = (_ROOT / Path(*parts)).resolve()
    root = _ROOT.resolve()
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        raise ValidationError("storage key keluar dari root")
    return candidate


class LocalStorage:
    """StoragePort backed by the local filesystem under DATA_DIR/blobs."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or _ROOT
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or key.startswith(("/", "\\")) or "\\" in key:
            raise ValidationError(f"storage key tidak valid: {key!r}")
        parts = [p for p in key.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValidationError("storage key mengandung '..'")
        candidate = (self._root / Path(*parts)).resolve()
        root = self._root.resolve()
        if os.path.commonpath([str(root), str(candidate)]) != str(root):
            raise ValidationError("storage key keluar dari root")
        return candidate

    def put(self, key: str, data: BinaryIO, *, content_type: str = "application/octet-stream") -> StoredBlob:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        with tmp.open("wb") as fh:
            shutil.copyfileobj(data, fh)
        tmp.replace(path)  # atomic
        return StoredBlob(key=key, size=path.stat().st_size, content_type=content_type)

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredBlob:
        import io
        return self.put(key, io.BytesIO(data), content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise NotFoundError(f"blob tidak ada: {key}")
        return path.read_bytes()

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.is_file():
            raise NotFoundError(f"blob tidak ada: {key}")
        return path.open("rb")

    def local_path(self, key: str, *, scratch: Optional[object] = None) -> str:
        # Local backend: the key IS a real path.
        path = self._path(key)
        if not path.is_file():
            raise NotFoundError(f"blob tidak ada: {key}")
        return str(path)

    def url(self, key: str, *, ttl: int = 3600, download: bool = False) -> str:
        # The web layer serves blobs; return a stable app-relative URL.
        return f"/api/blobs/{key}"

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
            parent = path.parent
            if parent != self._root and not any(parent.iterdir()):
                parent.rmdir()
        except FileNotFoundError:
            pass

    def stat(self, key: str) -> BlobMeta:
        try:
            path = self._path(key)
        except ValidationError:
            return BlobMeta(key=key, size=0, exists=False)
        if path.is_file():
            return BlobMeta(key=key, size=path.stat().st_size, exists=True)
        return BlobMeta(key=key, size=0, exists=False)
