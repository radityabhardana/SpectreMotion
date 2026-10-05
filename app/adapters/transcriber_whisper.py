"""TranscriberPort adapter wrapping the existing faster-whisper module."""

from __future__ import annotations

from pathlib import Path

from app.media import transcriber as _impl


class WhisperTranscriberAdapter:
    """Delegates to app.media.transcriber (faster-whisper backend)."""

    def transcribe(self, video_path: str, work_dir: str):
        return _impl.transcribe(Path(video_path), Path(work_dir))
