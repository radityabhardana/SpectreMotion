"""Integrated Hook Studio: live-preview + edit of the hook/subtitle template.

Mounted into the main FastAPI app. Editing here writes the same JSON the
renderer reads, so preview == output.

Endpoints:
    GET  /hook-studio            -> editor UI
    GET  /api/template           -> current template JSON
    POST /api/template           -> save template JSON
    POST /api/template/preview   -> render a 9:16 PNG preview (Pillow)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import config

log = logging.getLogger("spectre.hookstudio")

router = APIRouter()


def _template_path() -> Path:
    return config.TEMPLATE_PATH


def read_template() -> dict:
    path = _template_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Template rusak: %s", exc)
        return {}


def write_template(data: dict) -> None:
    path = _template_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic


def _auth(request: Request) -> None:
    if not config.APP_TOKEN:
        return
    token = request.cookies.get("app_token") or request.headers.get("X-App-Token", "")
    if token != config.APP_TOKEN:
        raise HTTPException(401, "Unauthorized")


@router.get("/hook-studio")
def studio_page():
    return RedirectResponse(url="/", status_code=302)


@router.get("/api/template")
def get_template(request: Request, _: None = Depends(_auth)):
    return JSONResponse(read_template())


@router.post("/api/template")
async def save_template(request: Request, _: None = Depends(_auth)):
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "JSON tidak valid") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Template harus berupa object JSON")
    if len(json.dumps(body)) > 200_000:
        raise HTTPException(413, "Template terlalu besar")
    write_template(body)
    return {"ok": True}


@router.post("/api/template/preview")
async def preview(request: Request, _: None = Depends(_auth)):
    """Render a 9:16 PNG preview using the SAME overlay renderer as the pipeline."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(500, "Pillow belum terinstall") from exc

    from app.media.overlay import render_overlay
    from app.media.renderer import resolve_font_path

    body = await request.json()
    text = str(body.get("text") or body.get("hook", "CONTOH HOOK TEKS"))

    tmpl = read_template()
    hook = dict(tmpl.get("hook", {}))
    # Editor-supplied overrides win over the stored template.
    hook.update({k: v for k, v in (body.get("hook") or {}).items() if v is not None})

    cfg = {
        "text": text,
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

    overlay_png = render_overlay(cfg, canvas=(config.TARGET_WIDTH, config.TARGET_HEIGHT))
    base = Image.new("RGB", (config.TARGET_WIDTH, config.TARGET_HEIGHT), (11, 14, 20))
    from io import BytesIO
    ov = Image.open(BytesIO(overlay_png)).convert("RGBA")
    base.paste(ov, (0, 0), ov)

    buf = BytesIO()
    base.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")
