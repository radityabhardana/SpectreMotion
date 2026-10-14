"""Tests for face-tracking reframe (Phase C) + dynamic zoom (Phase A).

Detection on a real face needs a real video, so we unit-test the deterministic
parts: the ffmpeg expression builder, smoothing/thinning, graceful fallback,
and the filter-chain construction with/without a face path.
"""

import pytest

from app.media import facetrack, renderer
from app.media.renderer import VideoInfo


def test_face_track_expr_empty_returns_none():
    assert facetrack.FaceTrack([], False).ffmpeg_center_expr() is None
    assert facetrack.FaceTrack([(0, 0.5)], False).ffmpeg_center_expr() is None


def test_face_track_expr_built():
    t = facetrack.FaceTrack([(0.0, 0.40), (2.0, 0.55), (4.0, 0.50)], True)
    expr = t.ffmpeg_center_expr()
    assert expr is not None
    assert "if(" in expr  # piecewise interpolation
    assert "0.55" in expr or "0.5" in expr


def test_cascade_available():
    cv2, cascade = facetrack._cascade()
    assert cascade is not None, "Haar cascade harus tersedia (opencv 4.x)"


def test_filter_chain_vertical_static():
    info = VideoInfo(duration=5, width=720, height=1280, has_audio=True)
    chain = renderer.build_filter_chain(info, "'x.ass'")
    assert "crop=1080:1920" in chain
    assert "[v]" in chain


def test_filter_chain_horizontal_blur_fallback():
    info = VideoInfo(duration=5, width=1920, height=1080, has_audio=True)
    chain = renderer.build_filter_chain(info, "'x.ass'")
    assert "boxblur" in chain  # blurred-background letterbox


def test_filter_chain_face_expr_uses_crop_x():
    info = VideoInfo(duration=5, width=1920, height=1080, has_audio=True)
    expr = "if(lt(t,1.0),0.5,0.6)"
    chain = renderer.build_filter_chain(info, "'x.ass'", face_center_expr=expr)
    assert "crop=1080:1920" in chain
    assert "x='" in chain  # dynamic crop x
    assert "0.5" in chain  # expr embedded


def test_filter_chain_dynamic_zoom():
    info = VideoInfo(duration=5, width=720, height=1280, has_audio=True)
    chain = renderer.build_filter_chain(info, "'x.ass'", duration=5, dynamic_zoom=True)
    assert "zoompan" in chain


def test_filter_chain_no_zoom_by_default():
    info = VideoInfo(duration=5, width=720, height=1280, has_audio=True)
    chain = renderer.build_filter_chain(info, "'x.ass'", duration=5)
    assert "zoompan" not in chain


def test_track_faces_no_face_returns_empty(tmp_path):
    """A blank/tone video has no faces -> empty track, no crash."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg tidak tersedia")
    src = tmp_path / "blank.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=12:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True, timeout=60,
    )
    ft = facetrack.track_faces(src, 0, 3, tmp_path / "ft")
    # testsrc has no faces; expect no crash and found_any False
    assert ft.found_any is False
    assert ft.ffmpeg_center_expr() is None
