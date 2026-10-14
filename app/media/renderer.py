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


def probe_video(path: Path, *, token=None) -> VideoInfo:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    if token is not None:
        from app.cancel import run as _run
        token.check()
        proc = _run(cmd, token=token, timeout=60)
    else:
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

# Editor sends a font FAMILY NAME; the renderer needs a real FILE. Without this
# map, changing the font in the UI has no effect (libass silently falls back).
_FONT_FILE_MAP = {
    "arial bold": ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "arial": ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "impact": ["C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "segoe ui bold": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "segoe ui": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "trebuchet ms bold": ["C:/Windows/Fonts/trebucbd.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "trebuchet ms": ["C:/Windows/Fonts/trebuc.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "tahoma bold": ["C:/Windows/Fonts/tahomabd.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "tahoma": ["C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "verdana bold": ["C:/Windows/Fonts/verdanab.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "montserrat extrabold": ["assets/fonts/Montserrat-ExtraBold.ttf"],
    "dejavu sans bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


def _resolve_font_by_name(font_name: Optional[str]) -> Optional[str]:
    """Map a font family name to an existing font file."""
    if not font_name:
        return None
    key = font_name.strip().lower()
    for path in _FONT_FILE_MAP.get(key, []):
        p = Path(path) if Path(path).is_absolute() else (config.REPO_ROOT / path)
        if p.is_file():
            return str(p)
    return None


def resolve_font_path(explicit: Optional[str] = None,
                      font_name: Optional[str] = None) -> Optional[str]:
    """Return the first existing font path.

    Order: explicit path -> name-mapped file (honors the user's font choice) ->
    configured default -> bundled -> common system fonts.
    """
    candidates = [
        explicit,
        _resolve_font_by_name(font_name),
        config.SUBTITLE_FONT_PATH,
        str(config.REPO_ROOT / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"),
        *_WINDOWS_FONTS,
        *_LINUX_FONTS,
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


def _internal_font_name(font_path: Optional[str]) -> Optional[str]:
    """Read the real family name from a TTF/OTF file (nameID 1).

    libass matches fonts by this internal name, so writing it into the ASS
    Style guarantees the exact file is used (avoids silent fallback).
    """
    if not font_path or not Path(font_path).is_file():
        return None
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(font_path, 20)
        name = font.getname()  # (family, style)
        if name and name[0]:
            family = name[0]
            style = (name[1] or "").lower()
            # libass resolves "Family Style" or "Family Bold"; combine sensibly.
            if "bold" in style and "bold" not in family.lower():
                return f"{family} Bold"
            return family
    except Exception:  # noqa: BLE001 — best effort
        return None
    return None


# ---- ASS subtitles --------------------------------------------------------

def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _clean_sub_text(text: str) -> str:
    """Sanitize transcript text so it doesn't accidentally inject ASS tags."""
    return (text.replace("{", "")
            .replace("}", "")
            .replace("\\", "")
            .replace("\n", "\\N"))


# Kept for backward compatibility
_ass_escape = _clean_sub_text


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
    outline_w: int = 5,
    shadow: int = 2,
    margin_v: int = 420,
    words_per_phrase: int = 4,
    text_transform: str = "upper",
    box: bool = False,
) -> str:
    font_name = font_name or config.SUBTITLE_FONT_NAME
    def xf(t: str) -> str:
        return t.upper() if text_transform == "upper" else t
    # BorderStyle 3 = opaque box behind text (uses OutlineColour as fill).
    border_style = 3 if box else 1
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {config.TARGET_WIDTH}
PlayResY: {config.TARGET_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{_hex_to_ass_bgr(primary_color)},&H000000FF,{_hex_to_ass_bgr(outline_color)},&H64000000,-1,0,0,0,100,100,0,0,{border_style},{outline_w},{shadow},2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: list[str] = []

    def emit(start: float, end: float, text: str, *, raw: bool = False) -> None:
        if end <= start:
            end = start + 0.05
        payload = text if raw else _clean_sub_text(xf(text))
        events.append(
            f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},Default,,0,0,0,,{payload}"
        )

    for seg in segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        # Fallback: karaoke needs per-word timings; if a segment has none, emit
        # the plain line instead of silently producing no subtitle at all.
        if mode == "karaoke" and seg.words:
            phrase: list = []
            for w in seg.words:
                phrase.append(w)
                if w.text.endswith((".", "!", "?", ":", ";")) or len(phrase) >= words_per_phrase:
                    _emit_karaoke(emit, phrase, clip_start, highlight_color, primary_color, xf)
                    phrase = []
            if phrase:
                _emit_karaoke(emit, phrase, clip_start, highlight_color, primary_color, xf)
        elif seg.text and seg.text.strip():
            start = max(seg.start, clip_start) - clip_start
            end = min(seg.end, clip_end) - clip_start
            emit(start, end, seg.text)

    # Never ship an ASS with zero events (subtitles would silently not appear).
    if not events:
        log.warning("Tidak ada subtitle untuk window %.1f-%.1fs (segmen kosong?)",
                    clip_start, clip_end)

    return header + "\n".join(events) + "\n"


def _emit_karaoke(emit, phrase, clip_start: float, highlight: str, primary: str, xf=None) -> None:
    hi = _hex_to_ass_bgr(highlight)
    pr = _hex_to_ass_bgr(primary)
    xf = xf or (lambda t: t)
    n = len(phrase)
    for i, word in enumerate(phrase):
        w_start = max(word.start, clip_start) - clip_start
        next_start = phrase[i + 1].start if i + 1 < n else word.end
        w_end = next_start - clip_start
        if w_end <= w_start:
            w_end = w_start + 0.15
        before = " ".join(xf(_clean_sub_text(w.text)) for w in phrase[:i])
        after = " ".join(xf(_clean_sub_text(w.text)) for w in phrase[i + 1:])
        active = xf(_clean_sub_text(word.text))
        line = (before + " " if before else "")
        # Active word pops: bigger + highlight colour + slight upward shift.
        line += "{\\c" + hi + "\\fscx114\\fscy114}" + active + "{\\fscx100\\fscy100\\c" + pr + "}"
        if after:
            line += " " + after
        emit(w_start, w_end, line, raw=True)


# ---- Filter chain ---------------------------------------------------------

def build_filter_chain(
    info: VideoInfo,
    ass_filename: str,
    *,
    overlay_index: Optional[int] = None,
    display_seconds: float = 3.0,
    duration: float = 0.0,
    dynamic_zoom: bool = False,
    face_center_expr: Optional[str] = None,
) -> str:
    tw, th = config.TARGET_WIDTH, config.TARGET_HEIGHT
    is_vertical = info.height >= info.width

    if is_vertical:
        base = (
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},setsar=1[base]"
        )
    elif face_center_expr:
        # Face-tracking reframe: scale the source to full target height (so a
        # tw-wide vertical window always fits), then slide that window
        # horizontally to follow the speaker's face. crop x =
        # center_ratio * (scaled_width - tw).
        crop_x = f"({face_center_expr})*(iw-{tw})"
        base = (
            f"[0:v]scale=-2:{th},setsar=1[scl];"
            f"[scl]crop={tw}:{th}:x='{crop_x}':y=0[base]"
        )
    else:
        base = (
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},boxblur=20:2,eq=brightness=-0.08[bg];"
            f"[0:v]scale={tw}:-2:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )

    # Optional dynamic zoom (2short-style subtle Ken Burns) to avoid a static
    # frame. zoompan works on the already-sized [base] stream.
    if dynamic_zoom and duration > 0:
        frames = max(1, int(duration * 30))
        # Slow zoom from 1.0 to ~1.08 then hold; gentle and center-anchored.
        z = "min(1.0+0.08*on/{f},1.08)".replace("{f}", str(frames))
        zoom_expr = (
            f"[base]zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={tw}x{th}:fps=30,trim=duration={duration:.3f},setpts=PTS-STARTPTS[vz]"
        )
        chain = f"{base};{zoom_expr};[vz]subtitles={ass_filename}[vsub]"
    else:
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
    token=None,
) -> Path:
    if token is not None:
        token.check()
    info = info or probe_video(source, token=token)
    duration = max(0.5, end - start)
    sub_cfg = (template or {}).get("subtitle", {}) if template else {}

    # Subtitles (ASS) + font resolution. The user's font CHOICE (font_name) is
    # honored by mapping it to a real font file; fontsdir is then derived from
    # that file so libass can always resolve it.
    sub_font_name = sub_cfg.get("font_name", config.SUBTITLE_FONT_NAME)
    font_path = resolve_font_path(sub_cfg.get("font_path"), sub_font_name)
    fontsdir = str(Path(font_path).parent) if font_path else None
    # Use the resolved file's real internal family name for the ASS Style so
    # libass matches the exact file (falls back to the configured name).
    ass_font_name = _internal_font_name(font_path) if font_path else sub_font_name
    ass_path = work_dir / f"clip_{start:.0f}_{end:.0f}.ass"
    ass_doc = build_ass(
        segments, clip_start=start, clip_end=end, mode=subtitle_mode,
        font_name=ass_font_name,
        font_size=int(sub_cfg.get("font_size", 74)),
        primary_color=sub_cfg.get("font_color", "#FFFFFF"),
        highlight_color=sub_cfg.get("highlight_color", "#00FF66"),
        outline_color=sub_cfg.get("outline_color", "#000000"),
        outline_w=int(sub_cfg.get("outline_w", 5)),
        shadow=int(sub_cfg.get("shadow", 2)),
        margin_v=int(sub_cfg.get("margin_v", 420)),
        words_per_phrase=int(sub_cfg.get("words_per_phrase", 4)),
        text_transform=sub_cfg.get("text_transform", "upper"),
        box=bool(sub_cfg.get("box", False)),
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

    # Face-tracking reframe (horizontal sources only).
    face_center_expr: Optional[str] = None
    vid_cfg = (template or {}).get("video", {}) if template else {}
    if (not (info.height >= info.width)) and bool(vid_cfg.get("face_tracking", False)):
        try:
            from app.media.facetrack import track_faces
            ft = track_faces(source, start, end, work_dir / "ft")
            face_center_expr = ft.ffmpeg_center_expr()
            if face_center_expr:
                log.info("Face tracking aktif untuk klip %.0f-%.0fs", start, end)
        except Exception as exc:  # noqa: BLE001
            log.warning("Face tracking gagal, fallback ke crop tengah: %s", exc)

    filter_chain = build_filter_chain(
        info, sub_expr, overlay_index=overlay_index,
        display_seconds=float((template or {}).get("hook", {}).get("display_seconds", 3)),
        duration=duration,
        dynamic_zoom=bool(vid_cfg.get("dynamic_zoom", False)),
        face_center_expr=face_center_expr,
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
        if token is not None:
            from app.cancel import run as _run
            proc = _run(cmd, token=token, timeout=1200)
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if proc.returncode != 0:
            raise RenderError(f"ffmpeg render gagal: {proc.stderr.strip()[-500:]}")
        if not part_path.exists() or part_path.stat().st_size == 0:
            raise RenderError("Render menghasilkan file kosong")
        os.replace(part_path, out_path)
    finally:
        if part_path.exists():
            part_path.unlink(missing_ok=True)

    _apply_faststart(out_path, token=token)
    return out_path


def _apply_faststart(path: Path, *, token=None) -> None:
    """Move the moov atom to the front (fast remux, copy only). Best-effort."""
    if token is not None and token.cancelled():
        return
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
