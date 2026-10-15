"""Face-tracking reframe for horizontal → vertical conversion.

The problem: a horizontal (16:9) video cropped to vertical (9:16) by a static
center crop often pushes the speaker out of frame. 2short.ai's "killer feature"
is tracking the speaker's face and sliding the vertical window to follow it.

This module samples the clip, detects the largest face per sampled frame with
OpenCV's Haar cascade, filters out outliers, applies deadzone stabilization and
cinematic camera damping, and returns a list of (time, center_x_ratio) keyframes.
The renderer turns those into an ffmpeg expression that pans the crop window.

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

# Sample rate: 4 fps provides fine temporal resolution without overburdening CPU.
_SAMPLE_FPS = 4
_MAX_SAMPLES = 120

# Stabilization parameters
_DEADZONE = 0.035  # ±3.5% horizontal deadzone: small head bobs/nodding won't shake camera
_SMOOTH_ALPHA = 0.22  # Lower alpha = smoother cinematic camera glide (inertia)
_MAX_SHIFT = 0.08  # Max camera movement per frame: prevents sudden jerk if a false face is detected
_KEYFRAME_INTERVAL = 0.40  # Thinning interval in seconds: keeps ffmpeg expression compact and fluid


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
        # Build nested if() linear interpolation from start to end.
        expr = f"{pts[-1][1]:.4f}"  # default = last position
        for i in range(len(pts) - 1, -1, -1):
            t0, x0 = pts[i]
            if i == 0:
                # Before first keyframe: hold first position
                expr = f"if(lt(t\\,{t0:.3f})\\,{x0:.4f}\\,{expr})"
                break
            t1, x1 = pts[i - 1]
            dt = max(0.001, t0 - t1)
            # Correct interpolation from (t1, x1) to (t0, x0) as t progresses:
            # x(t) = x1 + (x0 - x1) * (t - t1) / dt
            seg = (f"if(between(t\\,{t1:.3f}\\,{t0:.3f})\\,"
                   f"({x1:.4f}+({x0 - x1:.4f})*(t-{t1:.3f})/{dt:.3f})\\,{expr})")
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
    fps = min(_SAMPLE_FPS, max(1.0, _MAX_SAMPLES / duration))
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
    raw_detections: list[tuple[float, Optional[float]]] = []
    
    for i, fp in enumerate(frames):
        img = cv2.imread(str(fp))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(36, 36)
        )
        t = (i / max(1, n - 1)) * duration
        if len(faces) > 0:
            # Pick largest face
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            cx = (x + w / 2.0) / img.shape[1]
            raw_detections.append((t, cx))
        else:
            raw_detections.append((t, None))

    # Check if any face was detected at all
    valid_faces = [cx for _, cx in raw_detections if cx is not None]
    if not valid_faces:
        log.info("Tidak ada wajah terdeteksi; pakai crop tengah statis")
        return FaceTrack([], False)

    # 1. Fill missing frames with nearest/held position to prevent sudden gaps
    filled: list[tuple[float, float]] = []
    current_val = valid_faces[0]
    for t, cx in raw_detections:
        if cx is not None:
            current_val = cx
        filled.append((t, current_val))

    # 2. Cinematic stabilization (Deadzone + Damped Spring Smoothing + Outlier Clamp)
    smoothed: list[tuple[float, float]] = []
    cam_pos = filled[0][1]
    
    for t, target_x in filled:
        # Outlier clamp: limit max movement per frame to prevent jumping
        delta = target_x - cam_pos
        if abs(delta) > _MAX_SHIFT:
            target_x = cam_pos + (_MAX_SHIFT if delta > 0 else -_MAX_SHIFT)
        
        # Deadzone filter: if speaker moved within deadzone, hold camera steady
        diff = target_x - cam_pos
        if abs(diff) < _DEADZONE:
            effective_target = cam_pos
        else:
            effective_target = target_x - (_DEADZONE if diff > 0 else -_DEADZONE)

        # Exponential damping (smooth camera glide)
        cam_pos = _SMOOTH_ALPHA * effective_target + (1.0 - _SMOOTH_ALPHA) * cam_pos
        smoothed.append((round(t, 3), round(min(0.95, max(0.05, cam_pos)), 4)))

    # 3. Thinning: keep keyframes every ~0.4s (plus endpoints) for fine-grained, smooth tracking
    thinned: list[tuple[float, float]] = []
    last_t = -999.0
    for t, cx in smoothed:
        if t - last_t >= _KEYFRAME_INTERVAL:
            thinned.append((t, cx))
            last_t = t
    if thinned and thinned[-1][0] < smoothed[-1][0]:
        thinned.append(smoothed[-1])

    log.info("Face tracking: %d detections dari %d samples -> %d smoothed keyframes",
             len(valid_faces), n, len(thinned))
    return FaceTrack(thinned, True)
