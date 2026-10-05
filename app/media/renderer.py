"""Vertical clip rendering: ffmpeg crop/scale + burned ASS subtitles + hook overlay.

Phase 2 upgrades:
- Hook/badge is now a Pillow-rendered RGBA PNG overlaid by ffmpeg (multi-layer,
  box/stroke/shadow/emphasis), not drawtext — so the template is actually used.
- Robust font resolution: explicit path → bundled font → common system paths.
- Atomic output: write to `.part` then rename, so a crash never leaves a
  half-written mp4 looking like a real clip.

Subtitle modes: `karaoke` (word highlight) and `line`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app import config
from app.errors import RenderError
from app.media.overlay import render_overlay
from app.media.transcriber import Segment

log = logging.getLogger("spectre.render")

_WINDOWS_FONTS = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]
_LINUX_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
]


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe_video(path: Path) -> VideoInfo:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RenderError(f"ffprobe gagal: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    if vstream is None:
        raise RenderError("Tidak ada video stream di file sumber")
    return VideoInfo(
        duration=duration,
        width=int(vstream.get("width", config.TARGET_WIDTH)),
        height=int(vstream.get("height", config.TARGET_HEIGHT)),
        has_audio=has_audio,
    )


# ---- Font resolution ------------------------------------------------------

def resolve_font_path(explicit: Optional[str] = None) -> Optional[str]:
    """Return the first existing font path, or None."""
    candidates = [
        explicit,
        config.SUBTITLE_FONT_PATH,
        str(config.REPO_ROOT / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"),
        *_WINDOWS_FONTS,
        *_LINUX_FONTS,
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


# ---- ASS subtitles --------------------------------------------------------

def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("{", "\\{")
            .replace("}", "\\}").replace("\n", "\\N"))


def _hex_to_ass_bgr(color: str) -> str:
    color = str(color).lstrip("#")
    if len(color) != 6:
        return "&H00FFFFFF"
    r, g, b = color[0:2], color[2:4], color[4:6]
    return f"&H00{b}{g}{r}".upper()


def build_ass(
    segments: list[Segment],
    *,
    clip_start: float,
    clip_end: float,
    mode: str = "karaoke",
    font_name: Optional[str] = None,
    font_size: int = 74,
    primary_color: str = "#FFFFFF",
    highlight_color: str = "#00FF66",
    outline_color: str = "#000000",
    margin_v: int = 420,
    words_per_phrase: int = 4,
) -> str:
    font_name = font_name or config.SUBTITLE_FONT_NAME
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {config.TARGET_WIDTH}
PlayResY: {config.TARGET_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{_hex_to_ass_bgr(primary_color)},&H000000FF,{_hex_to_ass_bgr(outline_color)},&H64000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: list[str] = []

    def emit(start: float, end: float, text: str) -> None:
        if end <= start:
            end = start + 0.05
        events.append(
            f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},Default,,0,0,0,,{_ass_escape(text)}"
        )

    for seg in segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        if mode == "karaoke" and seg.words:
            phrase: list = []
            for w in seg.words:
                phrase.append(w)
                if w.text.endswith((".", "!", "?", ":", ";")) or len(phrase) >= words_per_phrase:
                    _emit_karaoke(emit, phrase, clip_start, highlight_color, primary_color)
                    phrase = []
            if phrase:
                _emit_karaoke(emit, phrase, clip_start, highlight_color, primary_color)
        else:
            start = max(seg.start, clip_start) - clip_start
            end = min(seg.end, clip_end) - clip_start
            emit(start, end, seg.text)

    return header + "\n".join(events) + "\n"


def _emit_karaoke(emit, phrase, clip_start: float, highlight: str, primary: str) -> None:
    hi = _hex_to_ass_bgr(highlight)
    pr = _hex_to_ass_bgr(primary)
    n = len(phrase)
    for i, word in enumerate(phrase):
        w_start = max(word.start, clip_start) - clip_start
        next_start = phrase[i + 1].start if i + 1 < n else word.end
        w_end = next_start - clip_start
        if w_end <= w_start:
            w_end = w_start + 0.15
        before = " ".join(w.text for w in phrase[:i])
        after = " ".join(w.text for w in phrase[i + 1:])
        line = (before + " " if before else "")
        line += f"{{\\c{hi}\\fscx112\\fscy112}}{word.text}{{\\fscx100\\fscy100\\c{pr}}}"
        if after:
            line += " " + after
        emit(w_start, w_end, line)


# ---- Filter chain ---------------------------------------------------------

def build_filter_chain(
    info: VideoInfo,
    ass_filename: str,
    *,
    overlay_index: Optional[int] = None,
    display_seconds: float = 3.0,
) -> str:
    tw, th = config.TARGET_WIDTH, config.TARGET_HEIGHT
    is_vertical = info.height >= info.width

    if is_vertical:
        # Single-input chain: label it on the way in.
        base = (
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},setsar=1[base]"
        )
    else:
        # Blurred-background letterbox already consumes [0:v] internally and
        # produces a labeled [base] stream.
        base = (
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},boxblur=20:2,eq=brightness=-0.08[bg];"
            f"[0:v]scale={tw}:-2:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )

    chain = f"{base};[base]subtitles={ass_filename}[vsub]"

    if overlay_index is not None:
        chain += (
            f";[{overlay_index}:v]format=rgba,"
            f"fade=t=in:st=0:d=0.3:alpha=1[ov];"
            f"[vsub][ov]overlay=0:0:enable='between(t,0,{display_seconds:.2f})'[v]"
        )
    else:
        chain += ";[vsub]null[v]"
    return chain


def _overlay_config(template: Optional[dict], hook_text: str) -> Optional[dict]:
    """Build the overlay config dict from a template + hook text."""
    if not hook_text and not template:
        return None
    hook = (template or {}).get("hook", {}) if template else {}
    cfg = {
        "text": hook_text or hook.get("sample_text", ""),
        "font_size": int(hook.get("font_size", 64)),
        "font_color": hook.get("font_color", "#FFFFFF"),
        "highlight_color": hook.get("highlight_color", "#00FF66"),
        "box": bool(hook.get("box", True)),
        "box_color": hook.get("box_color", "#000000@0.55"),
        "box_radius": int(hook.get("box_radius", 18)),
        "box_border_w": int(hook.get("box_border_w", 18)),
        "stroke_w": int(hook.get("stroke_w", 0)),
        "stroke_color": hook.get("stroke_color", "#000000"),
        "shadow_x": int(hook.get("shadow_x", 0)),
        "shadow_y": int(hook.get("shadow_y", 0)),
        "shadow_color": hook.get("shadow_color", "#000000@0.6"),
        "x": hook.get("x", "(w-text_w)/2"),
        "y": hook.get("y", "h*0.12"),
        "letter_spacing": int(hook.get("letter_spacing", 0)),
        "rotate": float(hook.get("rotate", 0.0)),
        "max_line_chars": int(hook.get("max_line_chars", 20)),
        "max_lines": int(hook.get("max_lines", 3)),
        "font_path": resolve_font_path(hook.get("font_path")),
    }
    return cfg


def render_clip(
    source: Path,
    segments: list[Segment],
    *,
    start: float,
    end: float,
    out_path: Path,
    work_dir: Path,
    hook_text: str = "",
    subtitle_mode: str = "karaoke",
    template: Optional[dict] = None,
    info: Optional[VideoInfo] = None,
) -> Path:
    info = info or probe_video(source)
    duration = max(0.5, end - start)
    sub_cfg = (template or {}).get("subtitle", {}) if template else {}

    # Subtitles (ASS) + font resolution.
    font_path = resolve_font_path(sub_cfg.get("font_path"))
    fontsdir = str(Path(font_path).parent) if font_path else None
    ass_path = work_dir / f"clip_{start:.0f}_{end:.0f}.ass"
    ass_doc = build_ass(
        segments, clip_start=start, clip_end=end, mode=subtitle_mode,
        font_name=sub_cfg.get("font_name", config.SUBTITLE_FONT_NAME),
        font_size=int(sub_cfg.get("font_size", 74)),
        primary_color=sub_cfg.get("font_color", "#FFFFFF"),
        highlight_color=sub_cfg.get("highlight_color", "#00FF66"),
        outline_color=sub_cfg.get("outline_color", "#000000"),
        margin_v=int(sub_cfg.get("margin_v", 420)),
        words_per_phrase=int(sub_cfg.get("words_per_phrase", 4)),
    )
    ass_path.write_text(ass_doc, encoding="utf-8")

    # Hook overlay PNG (Pillow).
    overlay_path: Optional[Path] = None
    overlay_cfg = _overlay_config(template, hook_text)
    if overlay_cfg and overlay_cfg.get("text"):
        try:
            png = render_overlay(overlay_cfg)
            overlay_path = work_dir / f"hook_{start:.0f}_{end:.0f}.png"
            overlay_path.write_bytes(png)
        except Exception as exc:  # noqa: BLE001 — never fail a render for the hook
            log.warning("Hook overlay gagal dirender, dilewati: %s", exc)
            overlay_path = None

    ass_filter_path = str(ass_path).replace("\\", "/").replace(":", "\\:")
    sub_expr = f"'{ass_filter_path}'"
    if fontsdir:
        fd = fontsdir.replace("\\", "/").replace(":", "\\:")
        sub_expr += f":fontsdir='{fd}'"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
    overlay_index: Optional[int] = None
    if overlay_path is not None:
        # A single PNG looped as a still image. The input MUST be bounded
        # (`-t` on the image input) or ffmpeg keeps looping forever and the
        # graph hangs when combined with +faststart.
        cmd += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(overlay_path)]
        overlay_index = 1

    filter_chain = build_filter_chain(
        info, sub_expr, overlay_index=overlay_index,
        display_seconds=float((template or {}).get("hook", {}).get("display_seconds", 3)),
    )
    cmd += ["-filter_complex", filter_chain, "-map", "[v]"]
    if info.has_audio:
        if config.AUDIO_LOUDNORM_FILTER:
            cmd += ["-filter:a", config.AUDIO_LOUDNORM_FILTER]
        cmd += ["-map", "0:a?"]
    cmd += [
        "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", config.X264_CRF,
        "-maxrate", config.X264_MAXRATE, "-bufsize", config.X264_BUFSIZE,
        "-pix_fmt", "yuv420p",
    ]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-threads", str(config.FFMPEG_THREADS)]

    # Atomic output: render to a .part file, then move it into place. The
    # +faststart movflag requires a seekable final container, so we apply it
    # in a fast remux pass AFTER the atomic rename instead of during the
    # (slow) encode — this also keeps the encode from hanging on the moved
    # moov atom while the .part file is still open.
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    cmd += ["-f", "mp4", str(part_path)]

    log.info("Rendering clip %.1f-%.1fs -> %s", start, end, out_path.name)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if proc.returncode != 0:
            raise RenderError(f"ffmpeg render gagal: {proc.stderr.strip()[-500:]}")
        if not part_path.exists() or part_path.stat().st_size == 0:
            raise RenderError("Render menghasilkan file kosong")
        os.replace(part_path, out_path)
    finally:
        if part_path.exists():
            part_path.unlink(missing_ok=True)

    _apply_faststart(out_path)
    return out_path


def _apply_faststart(path: Path) -> None:
    """Move the moov atom to the front (fast remux, copy only). Best-effort."""
    tmp = path.with_suffix(path.suffix + ".fs")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(path), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, path)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("faststart remux dilewati: %s", exc)
    finally:
        tmp.unlink(missing_ok=True)
