"""Tests verifying hook customization is directly in preview and /hook-studio page is removed."""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app import config, storage
from app.web import app

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    dbp = tmp_path / "app.db"
    tmpl_path = tmp_path / "template.json"
    tmpl_path.write_text(json.dumps({"hook": {"font_size": 64, "highlight_color": "#00FF66"}}), encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", dbp)
    monkeypatch.setattr(config, "TEMPLATE_PATH", tmpl_path)
    monkeypatch.setattr(storage.config, "DB_PATH", dbp)
    storage.init_db()
    yield

client = TestClient(app)

def test_hook_studio_page_redirects_and_nav_cleaned():
    # 1. /hook-studio redirects to /
    res = client.get("/hook-studio", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/"

    # 2. Index and history navbars no longer link to /hook-studio
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert '/hook-studio' not in res_index.text

    res_history = client.get("/history")
    assert res_history.status_code == 200
    assert '/hook-studio' not in res_history.text

def test_job_page_has_inline_hook_controls():
    job = storage.create_job(source_type="url", source_name="Hook Test", source_url="https://youtu.be/test")
    res_job = client.get(f"/jobs/{job.id}")
    assert res_job.status_code == 200
    html = res_job.text

    # Navbar has no hook-studio
    assert '/hook-studio' not in html

    # Section 0 has hook presets and customization controls
    assert 'hk-preset-capcut' in html
    assert 'hk-preset-hormozi' in html
    assert 'hk-preset-neon' in html
    assert 'hk-fontsize-range' in html
    assert 'hk-radius-range' in html
    assert 'hk-highlight-color' in html
    assert 'hk-box-on' in html
    assert 'hk-stroke-on' in html
    assert 'applyHookPreset' in html
    assert 'saveAsDefaultHookTemplate' in html
    assert 'style_overrides' in html

def test_pipeline_hook_style_overrides():
    # Test that style_overrides["hook"] overrides template["hook"]
    from app import presets as presets_mod
    template = presets_mod.active_template()
    original_size = template.get("hook", {}).get("font_size", 64)

    style_overrides = {
        "hook": {
            "font_size": 92,
            "highlight_color": "#FF0077",
            "box": False
        }
    }
    hook_overrides = style_overrides.get("hook") or {}
    if hook_overrides:
        template["hook"] = {**(template.get("hook") or {}), **hook_overrides}

    assert template["hook"]["font_size"] == 92
    assert template["hook"]["highlight_color"] == "#FF0077"
    assert template["hook"]["box"] is False
