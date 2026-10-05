"""Built-in hook presets + per-name template loading.

Presets are plain dicts merged over the default template. Named lookup is
path-traversal safe: names are slugified and must resolve inside the
hook_templates directory.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Optional

from app import config
from app.errors import ValidationError

log = logging.getLogger("spectre.presets")

TEMPLATES_DIR = config.REPO_ROOT / "templates"
_NAME_RE = re.compile(r"[^a-z0-9_-]")

DEFAULT_HOOK_TEMPLATE = {
    "name": "Default Hook",
    "hook": {
        "font_size": 64, "font_color": "#FFFFFF", "highlight_color": "#00FF66",
        "box": True, "box_color": "#000000@0.55", "box_radius": 18,
        "box_border_w": 18, "stroke_w": 0, "stroke_color": "#000000",
        "shadow_x": 0, "shadow_y": 0, "shadow_color": "#000000@0.6",
        "x": "(w-text_w)/2", "y": "h*0.12", "display_seconds": 3,
        "max_line_chars": 20, "max_lines": 3, "letter_spacing": 0, "rotate": 0,
    },
    "subtitle": {
        "mode": "karaoke", "font_name": "DejaVu Sans Bold", "font_size": 74,
        "font_color": "#FFFFFF", "highlight_color": "#00FF66",
        "outline_color": "#000000", "margin_v": 420, "words_per_phrase": 4,
    },
    "video": {"background_blur": 20, "overlay_enabled": True, "width": 1080},
}

# 5 built-in presets (parity with the reference project).
PRESETS: dict[str, dict] = {
    "capcut": {
        "name": "CapCut Modern",
        "hook": {"font_color": "#FFFFFF", "highlight_color": "#00FF66",
                 "box_color": "#141824@0.80", "box_radius": 24, "box_border_w": 24},
        "subtitle": {"highlight_color": "#00FF66", "margin_v": 420},
    },
    "hormozi": {
        "name": "Alex Hormozi",
        "hook": {"font_size": 84, "highlight_color": "#FFE600",
                 "box_color": "#0A0A0A@0.92", "box_radius": 16, "box_border_w": 26,
                 "stroke_w": 3, "stroke_color": "#000000"},
        "subtitle": {"font_size": 76, "highlight_color": "#FFE600",
                     "words_per_phrase": 3, "margin_v": 420},
    },
    "cinematic": {
        "name": "Cinematic",
        "hook": {"font_size": 76, "highlight_color": "#F5C518",
                 "box_color": "#080C14@0.88", "box_radius": 6, "letter_spacing": 2},
        "subtitle": {"font_size": 72, "highlight_color": "#F5C518", "margin_v": 380},
    },
    "minimal": {
        "name": "Minimal",
        "hook": {"font_size": 84, "highlight_color": "#FFFFFF", "box": False},
        "subtitle": {"highlight_color": "#FFFFFF", "margin_v": 400},
    },
    "neon_badge": {
        "name": "Cyber Neon",
        "hook": {"highlight_color": "#00E5FF", "box_color": "#050814@0.92",
                 "box_radius": 20, "box_border_w": 24},
        "subtitle": {"highlight_color": "#00E5FF", "margin_v": 420},
    },
}


def _slug(name: str) -> str:
    raw = str(name).strip()
    # Reject path-like input outright rather than silently slugifying it: a
    # caller passing '../..' or '/etc/passwd' is a bug or an attack, not a name.
    if not raw or ".." in raw or "/" in raw or "\\" in raw:
        raise ValidationError(f"nama preset tidak valid: {name!r}")
    slug = _NAME_RE.sub("-", raw.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    if not slug or slug in {".", ".."}:
        raise ValidationError(f"nama preset tidak valid: {name!r}")
    return slug


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def list_presets() -> list[dict]:
    return [{"id": pid, "name": preset.get("name", pid)} for pid, preset in PRESETS.items()]


def get_preset(preset_id: str) -> Optional[dict]:
    base = PRESETS.get(preset_id)
    return deep_merge(DEFAULT_HOOK_TEMPLATE, base) if base else None


def named_template_path(name: str) -> Path:
    """Resolve a preset name to a file path inside templates/, safely."""
    slug = _slug(name)
    candidate = (TEMPLATES_DIR / f"{slug}.json").resolve()
    root = TEMPLATES_DIR.resolve()
    import os
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        raise ValidationError("nama template keluar dari direktori")
    return candidate


def load_named_template(name: str) -> Optional[dict]:
    path = named_template_path(name)
    if not path.is_file():
        return get_preset(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return get_preset(name)
    return deep_merge(DEFAULT_HOOK_TEMPLATE, data)


def active_template() -> dict:
    """Load the configured active template merged over defaults."""
    path = config.TEMPLATE_PATH
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return deep_merge(DEFAULT_HOOK_TEMPLATE, data)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Template aktif rusak: %s", exc)
    return copy.deepcopy(DEFAULT_HOOK_TEMPLATE)


def save_active_template(data: dict) -> None:
    path = config.TEMPLATE_PATH
    if not Path(path).is_absolute():
        path = config.REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
