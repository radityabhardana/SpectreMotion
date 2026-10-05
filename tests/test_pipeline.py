"""Tests for storage, subtitle/ASS generation, and API shape (no media)."""

from app import storage
from app.media import renderer
from app.media.transcriber import Segment, Word


def _use_tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(storage.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage.config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(storage.config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(storage.config, "WORK_DIR", tmp_path / "work")


def test_job_lifecycle(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    storage.init_db()
    job = storage.create_job(source_type="file", source_name="a.mp4")
    assert storage.get_job(job.id).status == storage.JOB_QUEUED

    claimed = storage.claim_next_job()
    assert claimed is not None and claimed.id == job.id

    storage.update_job(job.id, status=storage.JOB_DONE)
    assert storage.get_job(job.id).status == storage.JOB_DONE
    assert storage.claim_next_job() is None


def test_clips_roundtrip(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    storage.init_db()
    job = storage.create_job(source_type="file")
    clip = storage.Clip(id="c1", job_id=job.id, idx=0, start_s=1.0, end_s=11.0,
                        viral_score=88, hook="HOOK", verdict="pass", risk_flags=["x"])
    storage.add_clip(job.id, clip)
    got = storage.list_clips(job.id)
    assert len(got) == 1 and got[0].hook == "HOOK" and got[0].risk_flags == ["x"]


def test_ass_generation_karaoke():
    seg = Segment(
        start=0.0, end=3.0, text="halo dunia",
        words=[Word(0.0, 1.0, "halo"), Word(1.0, 3.0, "dunia")],
    )
    ass = renderer.build_ass([seg], clip_start=0.0, clip_end=3.0, mode="karaoke")
    assert "[Script Info]" in ass
    assert "Dialogue:" in ass
    assert "halo" in ass and "dunia" in ass


def test_ass_generation_line():
    seg = Segment(start=0.0, end=2.0, text="kalimat sederhana", words=[])
    ass = renderer.build_ass([seg], clip_start=0.0, clip_end=2.0, mode="line")
    assert "kalimat sederhana" in ass


def test_ass_outside_window_excluded():
    seg = Segment(start=100.0, end=110.0, text="jauh di luar", words=[])
    ass = renderer.build_ass([seg], clip_start=0.0, clip_end=5.0, mode="line")
    assert "jauh di luar" not in ass
