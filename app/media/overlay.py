"""Pillow-based overlay rendering: multi-layer hook/badge rasterization.

Unlike the previous drawtext-only hook, this renders a full RGBA 1080x1920
canvas with configurable layers, box/bubble, stroke, shadow, emphasis tokens,
letter-spacing and rotation — then the renderer overlays the PNG in ffmpeg.

Position `x`/`y` come from the template as small expressions evaluated through
a whitelisted AST walker (never `eval`). Supported names: w, h, text_w,
text_h (pillow), and numbers/operators/parens.
"""

from __future__ import annotations

import ast
import logging
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("spectre.overlay")

_MAX_POW_EXPONENT = 10
_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class OverlayError(RuntimeError):
    pass


# ---- Safe expression evaluation ------------------------------------------

def _eval_node(node: ast.AST, names: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, names)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise OverlayError(f"Konstanta tidak didukung: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id in names:
            return float(names[node.id])
        raise OverlayError(f"Nama tidak dikenal: {node.id}")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise OverlayError("Operator tidak diizinkan")
        left = _eval_node(node.left, names)
        right = _eval_node(node.right, names)
        if isinstance(node.op, ast.Pow) and (abs(right) > _MAX_POW_EXPONENT or abs(left) > 1e6):
            raise OverlayError("Pangkat di luar batas aman")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARY.get(type(node.op))
        if op is None:
            raise OverlayError("Unary operator tidak diizinkan")
        return op(_eval_node(node.operand, names))
    raise OverlayError(f"Ekspresi tidak diizinkan: {ast.dump(node)[:60]}")


def safe_eval(expr: str, names: dict[str, float], default: float = 0.0) -> float:
    if expr is None:
        return default
    if isinstance(expr, (int, float)):
        return float(expr)
    try:
        tree = ast.parse(str(expr), mode="eval")
        return _eval_node(tree, names)
    except (OverlayError, SyntaxError, ValueError, ZeroDivisionError) as exc:
        log.warning("Gagal evaluasi posisi %r: %s (pakai default)", expr, exc)
        return default


# ---- Colors --------------------------------------------------------------

_NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0),
          "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
          "cyan": (0, 255, 255), "magenta": (255, 0, 255)}


def parse_color(value: Any, default=(255, 255, 255)) -> tuple[int, int, int, int]:
    """Parse '#rrggbb', '#rrggbb@0.5', 'rgb(...)', or named colors → RGBA."""
    if value is None:
        return (*default, 255)
    s = str(value).strip().lower()
    alpha = 255
    if "@" in s:
        s, _, a = s.partition("@")
        try:
            alpha = max(0, min(255, int(float(a) * 255)))
        except ValueError:
            alpha = 255
    if s in _NAMED:
        return (*_NAMED[s], alpha)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) >= 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
            except ValueError:
                pass
    return (*default, alpha)


# ---- Tokens --------------------------------------------------------------

def parse_tokens(text: str) -> list[tuple[str, bool]]:
    """Split text into (word, emphasized) using *word* / [word] emphasis."""
    import re
    out: list[tuple[str, bool]] = []
    for raw in text.split():
        em = False
        w = raw
        if len(w) >= 2 and ((w[0] == "*" and w[-1] == "*") or (w[0] == "[" and w[-1] == "]")):
            em, w = True, w[1:-1]
        out.append((w, em))
    return out


# ---- Layer rendering -----------------------------------------------------

@dataclass
class OverlayConfig:
    text: str = ""
    font_size: int = 64
    font_color: str = "#FFFFFF"
    highlight_color: str = "#00FF66"
    box: bool = True
    box_color: str = "#000000@0.55"
    box_radius: int = 18
    box_border_w: int = 18
    stroke_w: int = 0
    stroke_color: str = "#000000"
    shadow_x: int = 0
    shadow_y: int = 0
    shadow_color: str = "#000000@0.6"
    x: str = "(w-text_w)/2"
    y: str = "h*0.12"
    letter_spacing: int = 0
    rotate: float = 0.0
    max_line_chars: int = 20
    max_lines: int = 3
    font_path: Optional[str] = None


def _load_font(path: Optional[str], size: int):
    from PIL import ImageFont
    if path and Path(path).is_file():
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _wrap(tokens: list[tuple[str, bool]], max_chars: int, max_lines: int) -> list[list[tuple[str, bool]]]:
    lines: list[list[tuple[str, bool]]] = []
    cur: list[tuple[str, bool]] = []
    length = 0
    for tok in tokens:
        w, em = tok
        add = len(w) + (1 if cur else 0)
        if cur and length + add > max_chars:
            lines.append(cur)
            cur, length = [], 0
            add = len(w)
            if len(lines) >= max_lines:
                break
        cur.append(tok)
        length += add
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def render_overlay(config_dict: dict, *, canvas: tuple[int, int] = (1080, 1920)) -> bytes:
    """Render the hook overlay to PNG bytes on a transparent canvas."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise OverlayError("Pillow belum terinstall") from exc

    cfg = OverlayConfig(**{k: v for k, v in (config_dict or {}).items()
                           if k in OverlayConfig.__dataclass_fields__})
    W, H = canvas
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    tokens = parse_tokens(cfg.text or "")
    if not tokens:
        return _png_bytes(img)

    font = _load_font(cfg.font_path, cfg.font_size)
    lines = _wrap(tokens, cfg.max_line_chars, cfg.max_lines)

    # Measure block size.
    line_metrics = []
    for line in lines:
        text = " ".join(w for w, _ in line)
        bbox = draw.textbbox((0, 0), text, font=font)
        line_metrics.append((line, bbox[2] - bbox[0], bbox[3] - bbox[1]))
    block_w = max((m[1] for m in line_metrics), default=0)
    line_h = cfg.font_size + 12
    block_h = line_h * len(lines)

    names = {"w": W, "h": H, "text_w": block_w, "text_h": block_h}
    x = safe_eval(cfg.x, names, (W - block_w) / 2)
    y = safe_eval(cfg.y, names, H * 0.12)

    # Optional box/bubble behind the text.
    if cfg.box:
        pad = cfg.box_border_w
        box_xy = [x - pad, y - pad, x + block_w + pad, y + block_h + pad]
        draw.rounded_rectangle(box_xy, radius=cfg.box_radius,
                               fill=parse_color(cfg.box_color))

    primary = parse_color(cfg.font_color)
    highlight = parse_color(cfg.highlight_color)

    for i, (line, lw, _lh) in enumerate(line_metrics):
        cx = x + (block_w - lw) / 2  # center each line in the block
        ly = y + i * line_h
        for word, em in line:
            color = highlight if em else primary
            if cfg.shadow_x or cfg.shadow_y:
                draw.text((cx + cfg.shadow_x, ly + cfg.shadow_y), word, font=font,
                          fill=parse_color(cfg.shadow_color))
            if cfg.stroke_w:
                draw.text((cx, ly), word, font=font, fill=color,
                          stroke_width=cfg.stroke_w,
                          stroke_fill=parse_color(cfg.stroke_color))
            else:
                draw.text((cx, ly), word, font=font, fill=color)
            bbox = draw.textbbox((0, 0), word, font=font)
            cx += (bbox[2] - bbox[0]) + 8 + cfg.letter_spacing

    if cfg.rotate:
        resample = _resampling(Image)
        img = img.rotate(cfg.rotate, resample=resample, expand=False, center=(x, y))

    return _png_bytes(img)


def _resampling(image_module):
    """Return a BICUBIC resampling constant across Pillow versions."""
    resampling = getattr(image_module, "Resampling", None)
    if resampling is not None:
        return resampling.BICUBIC
    return image_module.BICUBIC


def _png_bytes(img) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
