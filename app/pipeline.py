"""Job pipeline: turns a queued source video into clips.

Orchestrator the worker calls. Synchronous, single job at a time. Dependencies
(transcriber, LLM, storage, events) come from the adapter registry so drivers
can be swapped via config without touching this file.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Callable, Optional

from app import campaign as campaign_mod
from app import config, storage
from app.adapters import registry
from app.errors import PipelineError
from app.media import analyzer, renderer

log = logging.getLogger("spectre.pipeline")

ProgressFn = Callable[[str, float, str], None]


def _segments_for_window(segments, start: float, end: float) -> str:
    parts = [s.text for s in segments if s.end > start and s.start < end]
    return " ".join(parts)


def render_clip_job(job: "storage.Job") -> None:
    """Render a single, user-selected clip (kind == 'render_clip').

    Renders just the requested clip window. The transcript comes from the
    cache populated during the source job's first run, so timing is consistent
    without re-transcribing the whole video from scratch.
    """
    events = registry.get_events()
    settings = job.settings or {}
    clip_id = settings.get("clip_id") or ""
    parent_job_id = settings.get("job_id") or ""

    def emit(stage: str, pct: float, msg: str = "") -> None:
        storage.update_job(job.id, stage=stage, progress=pct, message=msg)
        if parent_job_id:
            events.publish(parent_job_id, {"type": "render.stage", "stage": stage,
                                           "progress": pct, "message": msg,
                                           "clip_id": clip_id})
        log.info("[render %s] %s (%.0f%%) %s", job.id[:8], stage, pct * 100, msg)

    try:
        clip = storage.get_clip(clip_id)
        parent = storage.get_job(parent_job_id) if parent_job_id else None
        if clip is None or parent is None:
            raise PipelineError("Klip atau job induk tidak ditemukan")

        source = Path(parent.source_path)
        if not source.is_file():
            raise PipelineError(f"File sumber tidak ditemukan: {source}")

        emit("transcribe", 0.2, "Menyiapkan transkrip...")
        transcriber = registry.get_transcriber()
        work_dir = config.WORK_DIR / job.id
        work_dir.mkdir(parents=True, exist_ok=True)
        out_path: Optional[Path] = None
        try:
            transcript = transcriber.transcribe(str(source), str(work_dir))
            info = renderer.probe_video(source)
            template = _load_template(parent.template_path)

            emit("render", 0.6, "Render klip...")
            out_path = config.OUTPUT_DIR / f"{parent.id}_clip{clip.idx + 1}.mp4"
            renderer.render_clip(
                source, transcript.segments,
                start=clip.start_s, end=clip.end_s,
                out_path=out_path, work_dir=work_dir,
                hook_text=clip.hook, info=info, template=template,
                subtitle_mode=(template or {}).get("subtitle", {}).get("mode", "karaoke"),
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        storage.update_clip(clip.id, output_path=str(out_path), rendered=True,
                            status="rendered")
        emit("done", 1.0, "Klip selesai")
        storage.update_job(job.id, status=storage.JOB_DONE, stage="done", progress=1.0)
        if parent_job_id:
            events.publish(parent_job_id, {"type": "clip.rendered",
                                           "clip_id": clip.id,
                                           "download_url": f"/api/clips/{clip.id}/download"})
    except Exception as exc:  # noqa: BLE001
        log.exception("[render %s] FAILED: %s", job.id[:8], exc)
        storage.update_job(job.id, status=storage.JOB_FAILED, stage="failed",
                           error=str(exc))
        if parent_job_id:
            events.publish(parent_job_id, {"type": "clip.failed", "clip_id": clip_id,
                                           "message": str(exc)})


def _load_template(template_path: str) -> Optional[dict]:
    """Load the hook template for a job, falling back to the configured one."""
    import json
    for candidate in (template_path, str(config.TEMPLATE_PATH)):
        if not candidate:
            continue
        p = Path(candidate)
        if not p.is_absolute():
            p = config.REPO_ROOT / p
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("Template rusak, dilewati: %s", p)
    return None


def process_job(job: storage.Job, progress: Optional[ProgressFn] = None) -> None:
    """Run the full pipeline for a claimed job. Updates storage + events."""

    events = registry.get_events()
    channel = job.id

    def emit(stage: str, pct: float, message: str = "") -> None:
        storage.update_job(job.id, stage=stage, progress=pct, message=message)
        events.publish(channel, {"type": "stage", "stage": stage,
                                 "progress": pct, "message": message})
        if progress:
            progress(stage, pct, message)
        log.info("[job %s] %s (%.0f%%) %s", job.id[:8], stage, pct * 100, message)

    work_dir = config.WORK_DIR / job.id
    work_dir.mkdir(parents=True, exist_ok=True)

    source = Path(job.source_path)
    try:
        events.publish(channel, {"type": "job.started", "job_id": job.id})

        if job.source_type == "url":
            emit("download", 0.05, "Mengunduh sumber...")
            source = _download_source(job.source_url, work_dir)

        if not source.is_file():
            raise PipelineError(f"File sumber tidak ditemukan: {source}")

        emit("probe", 0.12, "Membaca metadata video...")
        info = renderer.probe_video(source)

        emit("transcribe", 0.20, "Transkripsi audio (bisa lama)...")
        transcriber = registry.get_transcriber()
        transcript = transcriber.transcribe(str(source), str(work_dir))

        profile = campaign_mod.load_profile(job.campaign_id)
        rules_text = analyzer.build_campaign_rules_text(profile.raw if profile else None)

        emit("analyze", 0.55, "Menganalisis momen viral...")
        llm = registry.get_llm()
        moments = analyzer.analyze(transcript, rules_text, llm=llm)
        if not moments:
            raise PipelineError("AI tidak menemukan momen klip yang layak")

        # Persist candidate clips (with verdicts) so the UI can show the catalog.
        clips: list[storage.Clip] = []
        for idx, m in enumerate(moments[: config.MAX_PROPOSED_CLIPS]):
            text = _segments_for_window(transcript.segments, m.start, m.end)
            verdicts = campaign_mod.evaluate(
                profile, start=m.start, end=m.end, text=text,
                caption=m.caption, risk_flags=m.risk_flags,
            )
            clips.append(storage.Clip(
                id=uuid.uuid4().hex, job_id=job.id, idx=idx,
                start_s=m.start, end_s=m.end, viral_score=m.viral_score,
                hook=m.hook, topic=m.topic, caption=m.caption, reason=m.reason,
                risk_flags=m.risk_flags,
                verdict=campaign_mod.worst_level(verdicts),
                verdict_msg=campaign_mod.summarize(verdicts),
            ))
        storage.replace_clips(job.id, clips)
        events.publish(channel, {"type": "catalog.ready", "job_id": job.id,
                                 "count": len(clips)})

        # Render the top N clips.
        template = _load_template(job.template_path)
        to_render = clips[: config.MAX_CLIPS]
        total = max(1, len(to_render))
        for i, clip in enumerate(to_render):
            emit("render", 0.6 + 0.4 * (i / total),
                 f"Render klip {i + 1}/{total}...")
            out_name = f"{job.id}_clip{clip.idx + 1}.mp4"
            out_path = config.OUTPUT_DIR / out_name
            renderer.render_clip(
                source, transcript.segments,
                start=clip.start_s, end=clip.end_s,
                out_path=out_path, work_dir=work_dir,
                hook_text=clip.hook, info=info, template=template,
                subtitle_mode=(template or {}).get("subtitle", {}).get("mode", "karaoke"),
            )
            storage.update_clip(clip.id, output_path=str(out_path), rendered=True)

        emit("done", 1.0, f"{len(to_render)} klip selesai")
        storage.update_job(job.id, status=storage.JOB_DONE, stage="done",
                           progress=1.0, message=f"{len(to_render)} klip selesai")
        events.publish(channel, {"type": "job.succeeded", "job_id": job.id})
        from app import audit
        audit.record("job.completed", subject=job.id,
                     detail={"clips": len(clips), "rendered": len(to_render),
                             "campaign_id": job.campaign_id}, actor="worker")

    except Exception as exc:  # noqa: BLE001 — record any failure
        log.exception("[job %s] FAILED: %s", job.id[:8], exc)
        storage.update_job(job.id, status=storage.JOB_FAILED, stage="failed",
                           error=str(exc))
        events.publish(channel, {"type": "job.failed", "job_id": job.id,
                                 "message": str(exc)})
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _download_source(url: str, work_dir: Path) -> Path:
    """Download a URL, dispatching to the right ingester by URL type."""
    from app.ingest import gdrive, urls, youtube

    kind = urls.classify(url)
    log.info("Ingesting URL as %s", kind)
    if kind == "youtube":
        return youtube.download_youtube(url, work_dir)
    if kind == "gdrive":
        return gdrive.download_gdrive(url, work_dir)
    return urls.download_url(url, work_dir)
