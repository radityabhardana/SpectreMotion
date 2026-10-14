"""SpectreMotion Web Application (FastAPI).

Provides:
- Web UI: Dashboard, Video Upload / YouTube Link, Hook Preset selector.
- Live Job Tracking: Server-Sent Events (SSE) with real-time progress & stage heartbeat.
- Viral Clip Catalog: Card grid, viral score, standalone reasoning, and in-browser 9:16 video player preview.
- Hook Studio: Integrated visual template editor.
- Embedded Worker: Runs in a background daemon thread so 1-click startup is all that is needed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import campaign as campaign_mod
from app import config, hookstudio, storage
from app.adapters import registry
from app.db import migrate
from app.errors import SpectreError
from app.logging_setup import setup_logging

setup_logging(log_dir=config.LOG_DIR, level=getattr(logging, config.LOG_LEVEL, logging.INFO))
log = logging.getLogger("spectre.web")

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="SpectreMotion", version="0.2.0")

# Ensure template and static directories exist before mounting
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.include_router(hookstudio.router)

_worker_thread: Optional[threading.Thread] = None


@app.exception_handler(SpectreError)
async def _spectre_error_handler(request: Request, exc: SpectreError):
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


def _start_embedded_worker() -> None:
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    from app import worker
    log.info("Starting embedded worker in background daemon thread...")
    _worker_thread = threading.Thread(target=worker.run, args=(False,), name="spectre-worker", daemon=True)
    _worker_thread.start()


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    migrate.ensure_schema()
    storage.init_db()
    from app import auth
    auth.bootstrap_admin_from_token()
    if not config.APP_TOKEN and auth.count_users() == 0:
        log.info("SpectreMotion running in OPEN mode (local use).")

    # Start the worker thread automatically for local runs (unless explicitly disabled)
    run_worker = os.getenv("EMBEDDED_WORKER", "1").strip().lower() in {"1", "true", "yes"}
    if run_worker:
        _start_embedded_worker()


# ---- Auth helpers ----

def _legacy_token_ok(request: Request) -> bool:
    if not config.APP_TOKEN:
        return False
    tok = request.cookies.get("app_token") or request.headers.get("X-App-Token", "")
    return tok == config.APP_TOKEN


def current_user(request: Request):
    from app import auth
    session_token = request.cookies.get("session") or request.headers.get("X-Session", "")
    if session_token:
        user = auth.resolve_session(session_token)
        if user is not None:
            return user
    if _legacy_token_ok(request):
        return auth.get_user(1) or user_placeholder()
    if not config.APP_TOKEN and auth.count_users() == 0:
        return None
    return _UNAUTHENTICATED


class _Unauthenticated:
    id = None


_UNAUTHENTICATED = _Unauthenticated()


def user_placeholder():
    from app import auth
    return auth.User(id=1, username="admin", is_admin=True)


def require_auth(request: Request) -> None:
    user = current_user(request)
    if user is _UNAUTHENTICATED:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _ok(request: Request) -> bool:
    return current_user(request) is not _UNAUTHENTICATED


# ---- Pages ----

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _ok(request):
        return templates.TemplateResponse("login.html", {"request": request})
    from app import presets as presets_mod
    user = current_user(request)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "presets": presets_mod.list_presets(),
        "max_clips": config.MAX_CLIPS,
        "user": user,
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    if not _ok(request):
        return templates.TemplateResponse("login.html", {"request": request})
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    from app import auth
    user = current_user(request)
    auth.require_owner(getattr(user, "id", None), job.user_id)
    clips = storage.list_clips(job_id)
    return templates.TemplateResponse("job.html", {
        "request": request, "job": job, "clips": clips, "user": user,
    })


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    if not _ok(request):
        return templates.TemplateResponse("login.html", {"request": request})
    sessions = storage.list_sessions_with_stats(limit=100)
    return templates.TemplateResponse("history.html", {
        "request": request, "sessions": sessions, "user": current_user(request),
    })


# ---- Auth routes ----

@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), token: str = Form("")):
    from app import auth
    if token:
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("app_token", token, httponly=True, samesite="lax")
        return resp
    user = auth.authenticate(username, password)
    if user is None:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Username/password salah"},
            status_code=401)
    session = auth.create_session(user.id, user_agent=request.headers.get("user-agent", ""))
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("session", session, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout(request: Request):
    from app import auth
    token = request.cookies.get("session")
    if token:
        auth.revoke_session(token)
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("session")
    resp.delete_cookie("app_token")
    return resp


# ---- Job & Clip APIs ----

@app.post("/api/jobs")
async def create_job(
    request: Request,
    source_type: str = Form("file"),
    url: str = Form(""),
    campaign_id: str = Form("none"),
    preset_id: str = Form(""),
    auto_render: str = Form("false"),
    file: UploadFile | None = None,
    _: None = Depends(require_auth),
):
    from app import auth, presets as presets_mod
    user = current_user(request)
    uid = getattr(user, "id", None)
    if uid is not None:
        auth.check_rate_limit(uid, "create_job", limit=30, window_s=3600)

    # Determine template to use
    template_path = str(config.TEMPLATE_PATH)
    job_settings = {
        "auto_render": auto_render.strip().lower() in {"1", "true", "yes", "on"},
    }
    if preset_id and preset_id != "default":
        preset_data = presets_mod.get_preset(preset_id)
        if preset_data:
            job_settings["preset_id"] = preset_id

    if source_type == "url":
        if not url.strip():
            raise HTTPException(400, "URL kosong")
        from app.ingest import urls as ingest_urls
        if ingest_urls.is_gdrive_folder_url(url):
            raise HTTPException(400, "Link folder Drive tidak didukung — share file individual.")
        job = storage.create_job(
            source_type="url", source_url=url.strip(),
            source_name=url.strip()[:80], campaign_id=campaign_id,
            template_path=template_path, user_id=uid, settings=job_settings,
        )
        return JSONResponse({"job_id": job.id})

    if file is None:
        raise HTTPException(400, "File tidak ada")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Format {ext} tidak didukung (gunakan mp4, mov, mkv, webm)")

    job = storage.create_job(
        source_type="file", source_name=file.filename or "upload",
        campaign_id=campaign_id, template_path=template_path,
        user_id=uid, settings=job_settings,
    )
    dest = config.UPLOAD_DIR / f"{job.id}{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    if config.MAX_UPLOAD_MB and dest.stat().st_size > config.MAX_UPLOAD_MB * 1024 * 1024:
        dest.unlink(missing_ok=True)
        storage.update_job(job.id, status=storage.JOB_FAILED, error="File terlalu besar")
        raise HTTPException(413, f"File melebihi {config.MAX_UPLOAD_MB} MB")

    storage.update_job(job.id, source_path=str(dest))
    return JSONResponse({"job_id": job.id})


@app.get("/api/jobs")
def api_list_jobs(_: None = Depends(require_auth)):
    return [
        {
            "id": j.id, "status": j.status, "stage": j.stage,
            "progress": j.progress, "source_name": j.source_name or j.source_url,
            "campaign_id": j.campaign_id, "created_at": j.created_at,
        }
        for j in storage.list_jobs(limit=100)
    ]


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str, _: None = Depends(require_auth)):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    clips = storage.list_clips(job_id)
    return {
        "id": job.id, "status": job.status, "stage": job.stage,
        "progress": job.progress, "message": job.message, "error": job.error,
        "source_name": job.source_name or job.source_url,
        "clips": [
            {
                "id": c.id, "idx": c.idx, "start": c.start_s, "end": c.end_s,
                "score": c.viral_score, "hook": c.hook, "topic": c.topic,
                "caption": c.caption, "reason": c.reason, "verdict": c.verdict,
                "verdict_msg": c.verdict_msg,
                # Truth comes from the FILE on disk, not the DB flag: a clip
                # whose file disappeared must not be advertised as "ready".
                "rendered": bool(c.output_path) and Path(c.output_path).is_file(),
                "status": _clip_true_status(c),
                "output_path": c.output_path,
                "has_source": bool(job.source_path) and Path(job.source_path).is_file(),
            }
            for c in clips
        ],
    }


def _clip_true_status(c) -> str:
    """Return the clip status that reflects reality (checks the file on disk)."""
    if c.output_path and Path(c.output_path).is_file():
        return "rendered"
    if c.status == "rendering":
        return "rendering"
    if c.status == "failed":
        return "failed"
    return "idle"


@app.get("/api/history")
def api_history(_: None = Depends(require_auth)):
    """Return all historical video sessions with statistics."""
    return storage.list_sessions_with_stats(limit=100)


@app.post("/api/jobs/{job_id}/delete")
@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str, _: None = Depends(require_auth)):
    """Permanently delete a session, all its rendered clips, source video, and DB rows."""
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Sesi tidak ditemukan")
    res = storage.delete_job_and_assets(job_id)
    return res


@app.post("/api/jobs/batch-delete")
async def api_batch_delete_jobs(request: Request, _: None = Depends(require_auth)):
    """Permanently delete multiple video sessions and all their files to free disk space."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    job_ids = payload.get("job_ids", [])
    if not job_ids:
        raise HTTPException(400, "Daftar job_ids tidak boleh kosong")
    return storage.delete_jobs_batch(job_ids)


@app.post("/api/clips/batch-delete")
async def api_batch_delete_clips(request: Request, _: None = Depends(require_auth)):
    """Permanently delete multiple clips from a session."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    clip_ids = payload.get("clip_ids", [])
    if not clip_ids:
        raise HTTPException(400, "Daftar clip_ids tidak boleh kosong")
    return storage.delete_clips_batch(clip_ids)


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str, _: None = Depends(require_auth)):
    from app import cancel as cancel_mod
    queue = registry.get_queue()
    queue.cancel(job_id)
    cancel_mod.cancel_key(job_id)
    storage.update_job(job_id, status=storage.JOB_CANCELED, stage="canceled", message="Dibatalkan pengguna")
    return {"ok": True}


# ---- Clip Streaming & Download ----

@app.get("/api/clips/{clip_id}/stream")
def stream_clip(clip_id: str, _: None = Depends(require_auth)):
    """Stream clip inline for in-browser video player preview."""
    clip = storage.get_clip(clip_id)
    if not clip or not clip.output_path or not Path(clip.output_path).is_file():
        raise HTTPException(404, "Klip belum siap atau belum dirender")
    return FileResponse(clip.output_path, media_type="video/mp4",
                        headers={"Content-Disposition": f"inline; filename={Path(clip.output_path).name}"})


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: str, _: None = Depends(require_auth)):
    """Download clip as an attachment."""
    clip = storage.get_clip(clip_id)
    if not clip or not clip.output_path or not Path(clip.output_path).is_file():
        raise HTTPException(404, "Klip belum siap atau belum dirender")
    return FileResponse(clip.output_path, media_type="video/mp4",
                        filename=Path(clip.output_path).name)


@app.get("/api/clips/{clip_id}/preview")
def preview_clip(clip_id: str, _: None = Depends(require_auth)):
    """Preview the RAW source segment for a clip so the style editor can show
    video while the user adjusts settings — before the clip is rendered.

    Cuts [start,end] from the job's source video (no subtitles/hook) with ffmpeg,
    cached on disk so repeat views are instant. Falls back to the rendered clip
    when the source is unavailable.
    """
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Klip tidak ditemukan")

    # If already rendered, just stream the final clip.
    if clip.output_path and Path(clip.output_path).is_file():
        return FileResponse(clip.output_path, media_type="video/mp4",
                            headers={"Content-Disposition": "inline"})

    job = storage.get_job(clip.job_id)
    if not job or not job.source_path or not Path(job.source_path).is_file():
        raise HTTPException(404, "Klip belum dirender dan video sumber tidak ditemukan")

    cache_dir = config.DATA_DIR / "previews"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{clip.id}.mp4"

    if not out.is_file():
        import subprocess
        start = max(0.0, float(clip.start_s))
        dur = max(0.3, float(clip.end_s) - float(clip.start_s))
        # Fast, low-res, no re-encode of audio: scale to 480p, ultrafast.
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(job.source_path),
            "-vf", "scale=-2:480",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            "-f", "mp4", str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            raise HTTPException(500, f"Gagal membuat preview: {proc.stderr.strip()[-200:]}")

    return FileResponse(str(out), media_type="video/mp4",
                        headers={"Content-Disposition": "inline"})


@app.get("/api/clips/{clip_id}/transcript")
def clip_transcript(clip_id: str, _: None = Depends(require_auth)):
    """Return word-level subtitle data for a clip, rebased to clip time.

    Powers the REAL-TIME browser preview in the style editor (like 2short.ai):
    the frontend overlays these words on the video and restyles them instantly
    with CSS — no render needed just to see a font/color change.
    """
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Klip tidak ditemukan")
    job = storage.get_job(clip.job_id)
    if not job or not job.source_path or not Path(job.source_path).is_file():
        return {"words": [], "reason": "source_missing"}

    try:
        from app.media import transcriber
        work = config.WORK_DIR / f"tran_{clip.id}"
        work.mkdir(parents=True, exist_ok=True)
        import shutil
        try:
            tr = transcriber.transcribe(Path(job.source_path), work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("Transcript untuk preview gagal: %s", exc)
        return {"words": [], "reason": "transcribe_failed"}

    start = float(clip.start_s)
    end = float(clip.end_s)
    words_out = []
    for seg in tr.segments:
        if seg.end <= start or seg.start >= end:
            continue
        # Prefer real word timings; fall back to the segment text as one block.
        if seg.words:
            for w in seg.words:
                if w.end <= start or w.start >= end:
                    continue
                words_out.append({
                    "start": round(max(0.0, w.start - start), 3),
                    "end": round(max(0.0, min(w.end, end) - start), 3),
                    "text": w.text,
                })
        elif seg.text and seg.text.strip():
            words_out.append({
                "start": round(max(0.0, seg.start - start), 3),
                "end": round(max(0.0, min(seg.end, end) - start), 3),
                "text": seg.text.strip(),
            })
    return {"words": words_out, "language": tr.language,
            "duration": round(end - start, 3), "reason": "ok"}


@app.post("/api/clips/{clip_id}/render")
async def api_render_clip(clip_id: str, request: Request, _: None = Depends(require_auth)):
    """Enqueue a render job for an individual clip with optional custom styling."""
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Klip tidak ditemukan")

    # Read optional JSON body for custom style overrides from the 2short.ai editor
    style_overrides = {}
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            style_overrides = body.get("style_overrides", {})
            force = bool(body.get("force", False))
    except Exception:
        pass

    if not force and not style_overrides and clip.rendered and clip.output_path and Path(clip.output_path).is_file():
        return {"ok": True, "already": True}

    storage.update_clip(clip.id, status="rendering")
    queue = registry.get_queue()
    import time
    dedupe = f"render:{clip.id}:{time.time()}" if (force or style_overrides) else f"render:{clip.id}"
    jid = queue.enqueue("render_clip", {
        "source_type": "clip_render",
        "clip_id": clip.id,
        "job_id": clip.job_id,
        "style_overrides": style_overrides,
    }, dedupe_key=dedupe)
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/{job_id}/render-all")
def api_render_all(job_id: str, _: None = Depends(require_auth)):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    queue = registry.get_queue()
    enqueued = []
    for clip in storage.list_clips(job_id):
        if not clip.rendered and clip.status != "rejected":
            storage.update_clip(clip.id, status="rendering")
            enqueued.append(queue.enqueue("render_clip", {
                "source_type": "clip_render", "clip_id": clip.id, "job_id": job_id,
            }, dedupe_key=f"render:{clip.id}"))
    return {"ok": True, "enqueued": len(enqueued)}


# ---- Real-time SSE Progress ----

@app.get("/events/{job_id}")
async def events(job_id: str, request: Request, _: None = Depends(require_auth)):
    """Server-Sent Events stream for a job with Last-Event-ID replay."""
    events_bus = registry.get_events()
    last_seq = 0
    header_last = request.headers.get("Last-Event-ID")
    if header_last and header_last.isdigit():
        last_seq = int(header_last)

    def stream():
        for event in events_bus.subscribe(job_id, last_seq=last_seq):
            seq = event.get("seq", 0)
            payload = json.dumps(event, ensure_ascii=False)
            yield f"id: {seq}\ndata: {payload}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- Preset & Campaign APIs ----

@app.get("/api/presets")
def api_presets(_: None = Depends(require_auth)):
    from app import presets as presets_mod
    return presets_mod.list_presets()


@app.get("/api/preset/{preset_id}")
def api_preset(preset_id: str, _: None = Depends(require_auth)):
    from app import presets as presets_mod
    preset = presets_mod.get_preset(preset_id)
    if preset is None:
        raise HTTPException(404, "Preset tidak ditemukan")
    return JSONResponse(preset)


@app.get("/api/campaigns")
def api_list_campaigns(_: None = Depends(require_auth)):
    return campaign_mod.list_campaigns()


@app.post("/api/campaigns/compile")
async def api_compile_campaign(request: Request, _: None = Depends(require_auth)):
    from app import audit, campaign_compiler
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if len(text) < 20:
        raise HTTPException(400, "Teks S&K terlalu pendek")
    llm = registry.get_llm()
    profile = campaign_compiler.compile_profile(text, llm=llm, name_hint=str(body.get("name") or ""))
    if body.get("save"):
        cid = campaign_mod.save_profile(profile)
        return {"saved": cid, "profile": profile}
    return {"profile": profile}


@app.get("/health")
def health():
    return {"ok": True, "version": app.version}
