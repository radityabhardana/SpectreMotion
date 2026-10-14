"""Phase 2 tests: analyzer depth, sentence reconstruction, overlay rendering,
ASS generation, and the hermetic pipeline (fake drivers, no network).

A tiny real video is generated with ffmpeg at test time so the pipeline's
render step is exercised for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app import config
from app.db import migrate
from app.media import analyzer, renderer
from app.media.overlay import parse_color, parse_tokens, render_overlay, safe_eval
from app.media.transcriber import Segment, Transcript, Word

ffmpeg = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(ffmpeg is None, reason="ffmpeg tidak tersedia")


# ---- Sentence reconstruction ---------------------------------------------

def _seg(start, end, text):
    return Segment(start=start, end=end, text=text, words=[Word(start, end, text)])


def test_build_sentences_merges_cues():
    segs = [
        _seg(0, 1, "Halo ini"),
        _seg(1, 2, "kalimat pertama."),
        _seg(2.2, 3, "Lalu yang"),
        _seg(3, 4, "kedua ada di sini."),
    ]
    sents = analyzer.build_sentences(segs)
    assert len(sents) == 2
    assert "pertama" in sents[0].text
    assert sents[1].start == pytest.approx(2.2)


def test_chunking_splits_long_transcript():
    sents = [analyzer.Sentence(i, i + 1, "x" * 100) for i in range(50)]
    chunks = analyzer._chunk_sentences(sents, max_chars=500)
    assert len(chunks) > 1
    # No sentence lost.
    assert sum(len(c) for c in chunks) == 50


def test_snap_to_sentences():
    sents = [analyzer.Sentence(0, 5, "a"), analyzer.Sentence(5, 10, "b")]
    m = analyzer.ClipMoment(start=0.4, end=9.6)
    analyzer.snap_to_sentences(m, sents)
    assert m.start == 0 and m.end == 10


# ---- Analyzer (fake LLM) ------------------------------------------------

class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, *, json_mode=True):
        return json.dumps(self.payload)


def _transcript():
    return Transcript(language="id", duration=20.0, segments=[
        _seg(0, 5, "Ini pembuka yang sangat menarik."),
        _seg(5, 12, "Ini poin utamanya dan berdiri sendiri."),
        _seg(12, 20, "Ini kesimpulannya."),
    ])


def test_analyze_parses_and_snaps():
    payload = {"clips": [
        {"start": 0.2, "end": 11.8, "viral_score": 90, "hook": "H",
         "topic": "t", "caption": "c", "reason": "r", "risk_flags": ["x"]},
    ]}
    moments = analyzer.analyze(_transcript(), llm=_FakeLLM(payload))
    assert len(moments) == 1
    assert moments[0].start == 0 and moments[0].end == 12  # snapped
    assert moments[0].risk_flags == ["x"]


def test_analyze_dedupes_overlaps():
    payload = {"clips": [
        {"start": 0, "end": 10, "viral_score": 90, "hook": "a"},
        {"start": 2, "end": 11, "viral_score": 80, "hook": "b"},
    ]}
    moments = analyzer.analyze(_transcript(), llm=_FakeLLM(payload))
    assert len(moments) == 1


def test_analyze_returns_empty_on_no_clips():
    assert analyzer.analyze(_transcript(), llm=_FakeLLM({"clips": []})) == []


# ---- Overlay -------------------------------------------------------------

def test_safe_eval_arithmetic():
    assert safe_eval("(w-text_w)/2", {"w": 100, "text_w": 20}) == 40
    assert safe_eval("h*0.12", {"h": 1000}) == 120


def test_safe_eval_rejects_unsafe():
    # Attribute access and names must be rejected (fall back to default).
    assert safe_eval("__import__('os')", {}, default=-1) == -1
    assert safe_eval("open('x')", {}, default=-1) == -1


def test_parse_color_forms():
    assert parse_color("#ffffff") == (255, 255, 255, 255)
    assert parse_color("#000000@0.5")[3] == 127
    assert parse_color("white") == (255, 255, 255, 255)


def test_parse_tokens_emphasis():
    toks = parse_tokens("ini *penting* sekali")
    assert toks[0] == ("ini", False)
    assert toks[1] == ("penting", True)


def test_render_overlay_produces_png():
    png = render_overlay({"text": "HOOK TEST", "font_size": 48})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ---- ASS -----------------------------------------------------------------

def test_ass_karaoke_contains_words():
    seg = Segment(0, 3, "halo dunia",
                  [Word(0, 1, "halo"), Word(1, 3, "dunia")])
    ass = renderer.build_ass([seg], clip_start=0, clip_end=3, mode="karaoke")
    assert "[Script Info]" in ass and "Dialogue:" in ass
    assert "HALO" in ass and "DUNIA" in ass
    # Ensure ASS override tags are proper control blocks, not escaped literal text
    assert r"\{" not in ass
    assert r"\}" not in ass
    assert r"\\c" not in ass
    assert r"\\fscx" not in ass
    assert r"{\c" in ass
    assert r"\fscx114" in ass


def test_resolve_font_path_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(config, "SUBTITLE_FONT_PATH", "")
    monkeypatch.setattr(renderer, "_WINDOWS_FONTS", [])
    monkeypatch.setattr(renderer, "_LINUX_FONTS", [])
    monkeypatch.setattr(config, "REPO_ROOT", __import__("pathlib").Path("/nonexistent"))
    assert renderer.resolve_font_path() is None


# ---- Hermetic pipeline end-to-end ---------------------------------------

@requires_ffmpeg
def test_pipeline_end_to_end_with_fakes(tmp_path, monkeypatch):
    """Run the whole pipeline with fake transcriber/LLM on a real tiny video."""
    # Redirect all data dirs into tmp.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "TRANSCRIPT_CACHE_DIR", tmp_path / "cache")
    for d in (config.WORK_DIR, config.OUTPUT_DIR, config.UPLOAD_DIR,
              config.TRANSCRIPT_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # Also point storage + queue + events at the tmp DB.
    from app import storage
    from app.adapters import registry, queue_sqlite, events_memory
    monkeypatch.setattr(storage.config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(queue_sqlite.config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(events_memory.config, "DB_PATH", tmp_path / "app.db")

    # Force fake drivers.
    monkeypatch.setattr(config, "LLM_DRIVER", "fake")
    monkeypatch.setattr(config, "TRANSCRIBER_DRIVER", "fake")
    # Keep the render fast for tests (production defaults are heavier).
    monkeypatch.setattr(config, "X264_PRESET", "ultrafast")
    monkeypatch.setattr(config, "X264_CRF", "30")
    monkeypatch.setattr(config, "TARGET_WIDTH", 540)
    monkeypatch.setattr(config, "TARGET_HEIGHT", 960)
    registry.reset()

    migrate.apply()
    storage.init_db()

    # Generate a short test video with a tone.
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12:duration=8",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         "-shortest", str(src)],
        check=True, timeout=120,
    )

    job = storage.create_job(source_type="file", source_name="src.mp4",
                             source_path=str(src), campaign_id="none")
    from app.pipeline import process_job
    process_job(job)

    refreshed = storage.get_job(job.id)
    assert refreshed.status == storage.JOB_DONE, refreshed.error
    clips = storage.list_clips(job.id)
    assert len(clips) >= 1
    rendered = [c for c in clips if c.rendered]
    assert rendered, "tidak ada klip yang dirender"
    for c in rendered:
        from pathlib import Path
        assert Path(c.output_path).is_file()
        assert Path(c.output_path).stat().st_size > 0
