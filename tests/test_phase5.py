"""Phase 5 tests: URL classification, cookie handling, filename sanitization.
Hermetic — no network, yt-dlp imported only when actually downloading.
"""

import pytest

from app import config
from app.errors import UnsupportedMediaError
from app.ingest import gdrive, urls


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for mod in (urls, gdrive):
        monkeypatch.setattr(mod.config, "DATA_DIR", tmp_path)
    return tmp_path


# ---- Classification -------------------------------------------------------

def test_classify_youtube():
    for u in ("https://youtube.com/watch?v=dQw4w9WgXcQ",
              "https://youtu.be/dQw4w9WgXcQ",
              "https://www.youtube.com/shorts/dQw4w9WgXcQ"):
        assert urls.classify(u) == "youtube", u


def test_classify_gdrive():
    for u in ("https://drive.google.com/file/d/abcdefghijklmnopqrstuvwxyz012345/view",
              "https://drive.google.com/open?id=abcdefghijklmnopqrstuvwxyz012345"):
        assert urls.classify(u) == "gdrive", u


def test_classify_other():
    assert urls.classify("https://example.com/video.mp4") == "other"


def test_gdrive_folder_detection():
    folder = "https://drive.google.com/drive/folders/abcdefghijklmnopqrstuvwxyz012345"
    assert urls.is_gdrive_folder_url(folder) is True
    assert urls.is_gdrive_url(folder) is False


def test_extract_gdrive_id():
    fid = urls.extract_gdrive_id(
        "https://drive.google.com/file/d/abcdefghijklmnopqrstuvwxyz012345/view")
    assert fid == "abcdefghijklmnopqrstuvwxyz012345"


# ---- Cookies --------------------------------------------------------------

def test_save_and_has_cookies(data_dir):
    assert urls.has_cookies() is False
    raw = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tX\tY\n"
    path = urls.save_cookies(raw)
    assert path.is_file()
    assert urls.has_cookies() is True


def test_save_cookies_rejects_non_youtube(data_dir):
    with pytest.raises(UnsupportedMediaError):
        urls.save_cookies(b"this is not a cookie file at all, just random data padding")


# ---- GDrive filename sanitize --------------------------------------------

def test_safe_filename_strips_bad_chars():
    assert gdrive._safe_filename('a/b\\c:d*e?f"g<h>i|.mp4') == "a_b_c_d_e_f_g_h_i_.mp4"


def test_safe_filename_forces_video_ext():
    assert gdrive._safe_filename("weird.exe").endswith(".mp4")
    assert gdrive._safe_filename("clip.mp4") == "clip.mp4"


def test_gdrive_invalid_link_raises(data_dir, tmp_path):
    from app.errors import IngestError
    with pytest.raises(IngestError):
        gdrive.download_gdrive("https://drive.google.com/not-a-file", tmp_path)
