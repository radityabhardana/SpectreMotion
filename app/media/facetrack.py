"""Face-tracking reframe for horizontal → vertical conversion.

The problem: a horizontal (16:9) video cropped to vertical (9:16) by a static
center crop often pushes the speaker out of frame. 2short.ai's "killer feature"
is tracking the speaker's face and sliding the vertical window to follow it.

This module samples the clip, detects the largest face per sampled frame with
OpenCV's Haar cascade, smooths the center path (to avoid jitter), and returns a
list of (time, center_x_ratio) keyframes. The renderer turns those into an
ffmpeg expression that pans the crop window.

Degrades gracefully: if OpenCV or the cascade is unavailable, or no face is
found, callers fall back to the static center crop.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("spectre.facetrack")

# Sample at most this many frames (keeps CPU bounded on long clips).
_MAX_SAMPLES = 60
_SAMPLE_FPS = 2  # frames per second to inspect


@dataclass
class FaceTrack:
    keyframes: list[tuple[float, float]]  # (t_seconds, center_x_ratio 0..1)
    found_any: bool

    def ffmpeg_center_expr(self) -> Optional[str]:
        """Build an ffmpeg expression for the crop x offset over time.

        Returns an expression in terms of `t` that, when used inside
        `crop=w=OW:h=OH:x='EXPR'`, follows the smoothed face path via linear
        interpolation between keyframes.
        """
        if not self.found_any or not self.keyframes:
            return None
        pts = self.keyframes
        # Build nested if() linear interpolation. For many points this is long
        # but ffmpeg handles it; we cap keyframes to keep it sane.
        expr = f"{pts[-1][1]:.4f}"  # default = last position
        for i in range(len(pts) - 1, -1, -1):
            t0, x0 = pts[i]
            if i == 0:
                # before first keyframe: hold first position
                expr = f"if(lt(t\\,{t0:.3f})\\,{x0:.4f}\\,{expr})"
                break
            t1, x1 = pts[i - 1] if i - 1 >= 0 else (t0, x0)
            # interpolate x1->x0 between t1 and t0
            # guard against divide-by-zero
            dt = max(0.001, t0 - t1)
            seg = (f"if(between(t\\,{t1:.3f}\\,{t0:.3f})\\,"
                   f"({x0:.4f}+({x1 - x0:.4f})*(t-{t0:.3f})/{dt:.3f})\\,{expr})")
            expr = seg
        return expr


def _cascade():
    try:
        import cv2
    except ImportError:
        return None, None
    try:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            return None, None
        return cv2, cascade
    except Exception as exc:  # noqa: BLE001
        log.warning("Gagal memuat Haar cascade: %s", exc)
        return None, None


def _sample_frames(source: Path, start: float, end: float, tmp_dir: Path) -> list[Path]:
    """Extract small frames for analysis (scaled down for speed)."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pattern = tmp_dir / "ft_%03d.jpg"
    duration = max(0.1, end - start)
    fps = min(_SAMPLE_FPS, max(1, _MAX_SAMPLES / duration))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        "-vf", f"fps={fps:.3f},scale=480:-2",
        "-frames:v", str(_MAX_SAMPLES), str(pattern),
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return sorted(tmp_dir.glob("ft_*.jpg"))


def track_faces(source: Path, start: float, end: float, tmp_dir: Path) -> FaceTrack:
    """Return a smoothed face-center path for [start, end], or empty track."""
    cv2, cascade = _cascade()
    if cv2 is None:
        log.info("OpenCV/haarcascade tidak tersedia; skip face tracking")
        return FaceTrack([], False)

    frames = _sample_frames(source, start, end, tmp_dir)
    if not frames:
        return FaceTrack([], False)

    duration = max(0.1, end - start)
    n = len(frames)
    raw: list[tuple[float, float]] = []
    for i, fp in enumerate(frames):
        img = cv2.imread(str(fp))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(40, 40))
        t = (i / max(1, n - 1)) * duration
        if len(faces) > 0:
            # pick the largest face
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx = (x + w / 2) / img.shape[1]
            raw.append((t, cx))

    if not raw:
        log.info("Tidak ada wajah terdeteksi; pakai crop tengah statis")
        return FaceTrack([], False)

    # Smooth the path (exponential moving average) to avoid jitter.
    smoothed: list[tuple[float, float]] = []
    prev = raw[0][1]
    alpha = 0.35
    for t, cx in raw:
        prev = alpha * cx + (1 - alpha) * prev
        smoothed.append((round(t, 3), round(min(1.0, max(0.0, prev)), 4)))

    # Thin keyframes: keep every ~1s plus endpoints to bound expression size.
    thinned: list[tuple[float, float]] = []
    last_t = -999.0
    for t, cx in smoothed:
        if t - last_t >= 1.0:
            thinned.append((t, cx))
            last_t = t
    if thinned and thinned[-1][0] < smoothed[-1][0]:
        thinned.append(smoothed[-1])

    log.info("Face tracking: %d wajah dari %d sample -> %d keyframe",
             len(raw), n, len(thinned))
    return FaceTrack(thinned, True)
