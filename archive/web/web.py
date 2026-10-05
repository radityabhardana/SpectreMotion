"""FastAPI web app: upload, job tracking, clip catalog & download.

Run:
    uvicorn app.web:app --reload --port 8080
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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

app = FastAPI(title="SpectreMotion", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
app.include_router(hookstudio.router)


@app.exception_handler(SpectreError)
async def _spectre_error_handler(request: Request, exc: SpectreError):
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    migrate.ensure_schema()
    storage.init_db()
    from app import auth
    auth.bootstrap_admin_from_token()
    if not config.APP_TOKEN and auth.count_users() == 0:
        log.warning("APP_TOKEN kosong & belum ada user — web berjalan TANPA autentikasi (hanya untuk lokal!)")


# ---- Auth: session-based users, with a legacy APP_TOKEN fallback ----

def _legacy_token_ok(request: Request) -> bool:
    if not config.APP_TOKEN:
        return False
    tok = request.cookies.get("app_token") or request.headers.get("X-App-Token", "")
    return tok == config.APP_TOKEN


def current_user(request: Request):
    """Return the authenticated User, or None when auth is disabled entirely."""
    from app import auth
    session_token = request.cookies.get("session") or request.headers.get("X-Session", "")
    if session_token:
        user = auth.resolve_session(session_token)
        if user is not None:
            return user
    # Legacy: APP_TOKEN accepted during migration; map to admin user if present.
    if _legacy_token_ok(request):
        return auth.get_user(1) or user_placeholder()
    # No auth configured at all -> open (local) mode.
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
    user = current_user(request)
    jobs = storage.list_jobs(limit=50)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "jobs": jobs,
        "campaigns": campaign_mod.list_campaigns(),
        "max_clips": config.MAX_CLIPS,
        "user": user,
    })


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), token: str = Form("")):
    from app import auth
    # Legacy token path.
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


@app.post("/register")
def register(request: Request, username: str = Form(""), password: str = Form("")):
    from app import auth
    from app.errors import ValidationError
    if auth.count_users() > 0:
        raise HTTPException(403, "Registrasi tertutup (sudah ada user)")
    try:
        user = auth.create_user(username, password, is_admin=True)
    except ValidationError as exc:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": exc.message}, status_code=400)
    session = auth.create_session(user.id)
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


# ---- API ----

@app.post("/api/jobs")
async def create_job(
    request: Request,
    source_type: str = Form("file"),
    url: str = Form(""),
    campaign_id: str = Form("none"),
    file: UploadFile | None = None,
    _: None = Depends(require_auth),
):
    from app import auth
    user = current_user(request)
    uid = getattr(user, "id", None)
    # Rate limit job creation per user (no-op for the open/local mode).
    if uid is not None:
        auth.check_rate_limit(uid, "create_job", limit=20, window_s=3600)

    if source_type == "url":
        if not url.strip():
            raise HTTPException(400, "URL kosong")
        from app.ingest import urls as ingest_urls
        if ingest_urls.is_gdrive_folder_url(url):
            raise HTTPException(400, "Link folder Drive tidak didukung — share file individual.")
        job = storage.create_job(
            source_type="url", source_url=url.strip(),
            source_name=url.strip()[:80], campaign_id=campaign_id,
            template_path=str(config.TEMPLATE_PATH), user_id=uid,
        )
        from app import audit
        audit.record("job.queued", subject=job.id,
                     detail={"kind": ingest_urls.classify(url)}, actor="user")
        return JSONResponse({"job_id": job.id})

    if file is None:
        raise HTTPException(400, "File tidak ada")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Format {ext} tidak didukung")

    job = storage.create_job(
        source_type="file", source_name=file.filename or "upload",
        campaign_id=campaign_id, template_path=str(config.TEMPLATE_PATH),
        user_id=uid,
    )
    dest = config.UPLOAD_DIR / f"{job.id}{ext}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    # Guard against oversized uploads after the fact.
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
def api_job(job_id: str, _: None = Depends(require_auth)):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    clips = storage.list_clips(job_id)
    return {
        "id": job.id, "status": job.status, "stage": job.stage,
        "progress": job.progress, "error": job.error, "message": job.message,
        "clips": [
            {
                "id": c.id, "idx": c.idx, "start": c.start_s, "end": c.end_s,
                "viral_score": c.viral_score, "hook": c.hook, "topic": c.topic,
                "caption": c.caption, "verdict": c.verdict, "verdict_msg": c.verdict_msg,
                "rendered": c.rendered,
                "download_url": f"/api/clips/{c.id}/download" if c.rendered else None,
            }
            for c in clips
        ],
    }


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: str, _: None = Depends(require_auth)):
    clip = storage.get_clip(clip_id)
    if not clip or not clip.output_path or not Path(clip.output_path).is_file():
        raise HTTPException(404, "Klip belum siap")
    return FileResponse(clip.output_path, media_type="video/mp4",
                        filename=Path(clip.output_path).name)


@app.post("/api/clips/{clip_id}")
async def update_clip(clip_id: str, request: Request, _: None = Depends(require_auth)):
    body = await request.json()
    if not storage.get_clip(clip_id):
        raise HTTPException(404, "Klip tidak ditemukan")
    storage.update_clip(clip_id, **{k: v for k, v in body.items()
                                    if k in {"hook", "topic", "caption"}})
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "version": app.version}


# ---- Phase 3: presets, catalog actions, SSE progress ----

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


@app.post("/api/campaigns")
async def api_save_campaign(request: Request, _: None = Depends(require_auth)):
    body = await request.json()
    cid = campaign_mod.save_profile(body)
    from app import audit
    audit.record("campaign.saved", subject=cid, actor="user")
    return {"id": cid}


@app.post("/api/campaigns/compile")
async def api_compile_campaign(request: Request, _: None = Depends(require_auth)):
    """Compile free-text terms-of-service into a structured profile via the LLM."""
    from app import audit, campaign_compiler
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if len(text) < 20:
        raise HTTPException(400, "Teks S&K terlalu pendek")
    llm = registry.get_llm()
    profile = campaign_compiler.compile_profile(
        text, llm=llm, name_hint=str(body.get("name") or "")
    )
    audit.record("campaign.compiled", subject=profile.get("id", ""),
                 detail={"unresolved": profile.get("unresolved", [])}, actor="user")
    if body.get("save"):
        cid = campaign_mod.save_profile(profile)
        return {"saved": cid, "profile": profile}
    return {"profile": profile}


@app.get("/api/audit")
def api_audit(limit: int = 100, _: None = Depends(require_auth)):
    from app import audit
    return audit.tail(limit=limit)


@app.post("/api/cookies")
async def api_upload_cookies(file: UploadFile, _: None = Depends(require_auth)):
    from app.ingest import urls as ingest_urls
    raw = await file.read()
    path = ingest_urls.save_cookies(raw)
    from app import audit
    audit.record("cookies.updated", actor="user")
    return {"ok": True, "size": path.stat().st_size}


@app.get("/api/cookies")
def api_cookies_status(_: None = Depends(require_auth)):
    from app.ingest import urls as ingest_urls
    return {"has_cookies": ingest_urls.has_cookies()}


@app.get("/api/campaigns")
def api_list_campaigns(_: None = Depends(require_auth)):
    return campaign_mod.list_campaigns()


@app.post("/api/clips/{clip_id}/render")
async def api_render_clip(clip_id: str, request: Request, _: None = Depends(require_auth)):
    """Render a single clip on demand (enqueue a render job)."""
    clip = storage.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Klip tidak ditemukan")
    if clip.rendered:
        return {"ok": True, "already": True}
    queue = registry.get_queue()
    jid = queue.enqueue("render_clip", {
        "source_type": "clip_render", "clip_id": clip.id, "job_id": clip.job_id,
    }, dedupe_key=f"render:{clip.id}")
    return {"ok": True, "dedupe_id": jid}


@app.post("/api/jobs/{job_id}/render-all")
def api_render_all(job_id: str, _: None = Depends(require_auth)):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    queue = registry.get_queue()
    enqueued = []
    for clip in storage.list_clips(job_id):
        if not clip.rendered and clip.status != "rejected":
            enqueued.append(queue.enqueue("render_clip", {
                "source_type": "clip_render", "clip_id": clip.id, "job_id": job_id,
            }, dedupe_key=f"render:{clip.id}"))
    return {"ok": True, "enqueued": len(enqueued)}


@app.get("/events/{job_id}")
async def events(job_id: str, request: Request, _: None = Depends(require_auth)):
    """Server-Sent Events stream for a job, with Last-Event-ID replay."""
    from fastapi.responses import StreamingResponse

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
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
