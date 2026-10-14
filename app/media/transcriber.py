"""Transcription via faster-whisper (word-level timestamps).

Adds (Phase 2, parity with the reference project):
- Versioned JSON cache keyed by (stem, size, mtime) + model + language, so a
  re-run never re-transcribes and a stale/mismatched cache is hard-rejected.
- Language-tailored `initial_prompt` to bias brand/technical terms.
- Confidence filtering via avg_logprob / no_speech_prob with a fallback.

The model is loaded once and cached at module level.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from app import config
from app.errors import TranscriptionError

log = logging.getLogger("spectre.transcribe")

CACHE_VERSION = 2

_INITIAL_PROMPTS = {
    "id": (
        "Percakapan bahasa Indonesia santai tentang teknologi, bisnis, dan "
        "media sosial. Sebutan umum: YouTube, TikTok, Instagram, Canva, AI."
    ),
    "en": (
        "Casual English conversation about technology, business, and social "
        "media. Common terms: YouTube, TikTok, Instagram, Canva, AI."
    ),
}
_GENERIC_PROMPT = (
    "Casual conversation about technology, business, and social media. "
    "Common terms: YouTube, TikTok, Instagram."
)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    duration: float
    segments: list[Segment]


_model = None

# Map a whisper size name to its HF repo id, so we can find a locally
# downloaded copy and load it WITHOUT touching the network (works around
# ISPs that blackhole cdn-lfs.huggingface.co).
_WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def _local_model_dir(model_name: str) -> Optional[Path]:
    """Return a local snapshot dir for a whisper size, if present on disk."""
    repo = _WHISPER_REPOS.get(model_name)
    if not repo:
        return None
    import os
    hf_home = os.environ.get("HF_HOME")
    root = (Path(hf_home) / "hub") if hf_home else (Path.home() / ".cache" / "huggingface" / "hub")
    repo_dir = root / ("models--" + repo.replace("/", "--"))
    for snap in ("main",):
        candidate = repo_dir / "snapshots" / snap
        if (candidate / "model.bin").is_file():
            return candidate
    # fall back to any snapshot with a model.bin
    snaps = repo_dir / "snapshots"
    if snaps.is_dir():
        for child in snaps.iterdir():
            if (child / "model.bin").is_file():
                return child
    return None


def _get_model():
    global _model
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover
        raise TranscriptionError(
            "faster-whisper belum terinstall. Jalankan: pip install faster-whisper"
        ) from exc

    # Prefer a local copy to avoid a network round-trip on load.
    local_dir = _local_model_dir(config.WHISPER_MODEL)
    source = str(local_dir) if local_dir else config.WHISPER_MODEL
    log.info("Loading whisper model=%s (source=%s) device=%s compute=%s",
             config.WHISPER_MODEL, "local" if local_dir else "remote",
             config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)

    # If we have a local copy, tell huggingface_hub not to hit the network.
    if local_dir:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    _model = WhisperModel(
        source,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
        cpu_threads=max(1, config.FFMPEG_THREADS),
        num_workers=1,
    )
    return _model


def extract_audio(video_path: Path, out_wav: Path, *, token=None) -> Path:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    if token is not None:
        from app.cancel import run as _run
        proc = _run(cmd, token=token, timeout=600)
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise TranscriptionError(f"ffmpeg audio extract gagal: {proc.stderr.strip()}")
    return out_wav


# ---- Cache ---------------------------------------------------------------

def _cache_key(video_path: Path, model: str, language: Optional[str]) -> str:
    try:
        st = video_path.stat()
        sig = f"{video_path.stem}_{int(st.st_size)}_{int(st.st_mtime)}"
    except OSError:
        sig = video_path.stem
    return f"{sig}_{model}_{language or 'auto'}"


def _cache_path(key: str) -> Path:
    return config.TRANSCRIPT_CACHE_DIR / f"{key}.json"


def _transcript_to_json(t: Transcript) -> dict:
    return {
        "version": CACHE_VERSION,
        "language": t.language,
        "duration": t.duration,
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text,
             "words": [asdict(w) for w in s.words]}
            for s in t.segments
        ],
    }


def _transcript_from_json(data: dict, model: str) -> Optional[Transcript]:
    if data.get("version") != CACHE_VERSION:
        return None
    if data.get("model") not in (None, model):
        return None
    try:
        segments = [
            Segment(
                start=float(s["start"]), end=float(s["end"]), text=str(s["text"]),
                words=[Word(float(w["start"]), float(w["end"]), str(w["text"]))
                       for w in s.get("words", [])],
            )
            for s in data["segments"]
        ]
    except (KeyError, TypeError, ValueError):
        return None
    return Transcript(language=data.get("language", "unknown"),
                      duration=float(data.get("duration", 0.0)), segments=segments)


# ---- Main ----------------------------------------------------------------

def transcribe(video_path: Path, work_dir: Path, *, use_cache: bool = True, token=None) -> Transcript:
    config.TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if token is not None:
        token.check()
    key = _cache_key(video_path, config.WHISPER_MODEL, config.WHISPER_LANGUAGE)
    cache_file = _cache_path(key)

    if use_cache and cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            cached = _transcript_from_json(data, config.WHISPER_MODEL)
            if cached is not None:
                log.info("Transcript cache hit for %s (%d segments)",
                         video_path.name, len(cached.segments))
                return cached
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Cache rusak, re-transcribe: %s", exc)

    wav = work_dir / "audio.wav"
    extract_audio(video_path, wav, token=token)
    model = _get_model()
    started = time.time()

    language = config.WHISPER_LANGUAGE
    prompt = _INITIAL_PROMPTS.get(language or "", _GENERIC_PROMPT)

    segments_iter, info = model.transcribe(
        str(wav),
        language=language,
        beam_size=config.WHISPER_BEAM_SIZE,
        vad_filter=config.WHISPER_VAD_FILTER,
        word_timestamps=True,
        initial_prompt=prompt,
    )

    detected_lang = info.language or language or "unknown"
    kept: list[Segment] = []
    dropped = 0
    for seg in segments_iter:
        if token is not None:
            token.check()  # cooperative cancel while streaming segments
        no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
        avg_logprob = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
        if avg_logprob < -2.0 and no_speech > 0.90:
            dropped += 1
            continue
        words = [Word(float(w.start), float(w.end), (w.word or "").strip())
                 for w in (getattr(seg, "words", None) or [])]
        kept.append(Segment(float(seg.start), float(seg.end),
                            (seg.text or "").strip(), words))

    # If filtering removed everything, fall back to unfiltered output.
    if not kept:
        log.warning("Confidence filter dropped all segments; keeping unfiltered")
        kept = _unfiltered_segments(wav, model, language, prompt)

    result = Transcript(
        language=detected_lang,
        duration=float(getattr(info, "duration", 0.0) or 0.0),
        segments=kept,
    )
    log.info("Transcribed %s: %d segments (%d filtered) in %.1fs (lang=%s)",
             video_path.name, len(kept), dropped, time.time() - started, detected_lang)

    out = _transcript_to_json(result)
    out["model"] = config.WHISPER_MODEL
    try:
        cache_file.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("Gagal menulis cache transkrip: %s", exc)
    return result


def _unfiltered_segments(wav: Path, model, language, prompt) -> list[Segment]:
    segments_iter, _ = model.transcribe(
        str(wav), language=language, beam_size=config.WHISPER_BEAM_SIZE,
        word_timestamps=True, initial_prompt=prompt,
    )
    out: list[Segment] = []
    for seg in segments_iter:
        words = [Word(float(w.start), float(w.end), (w.word or "").strip())
                 for w in (getattr(seg, "words", None) or [])]
        out.append(Segment(float(seg.start), float(seg.end),
                           (seg.text or "").strip(), words))
    return out


def transcript_to_text(segments: list[Segment], max_chars: int = 12000) -> str:
    lines: list[str] = []
    total = 0
    for seg in segments:
        line = f"[{seg.start:.1f}-{seg.end:.1f}] {seg.text}"
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)
