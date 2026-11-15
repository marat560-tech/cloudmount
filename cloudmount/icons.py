"""Значки: облако с диском на цветном фоне (для дисков, плиток и трея)."""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _shade(rgb, k):
    return tuple(max(0, min(255, int(v * k))) for v in rgb)


def draw(tint: str = "#2176FF", size: int = 256, glyph: str = "cloud", rounded: bool = True) -> Image.Image:
    """Рисует значок в 4× разрешении и уменьшает — для гладких краёв."""
    S = 1024
    base = _hex(tint)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bottom = _shade(base, 1.12), _shade(base, 0.82)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    if rounded:
        ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=255)
    else:
        ImageDraw.Draw(mask).rectangle((0, 0, S, S), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    W = (255, 255, 255, 255)
    if glyph == "server":
        for y0 in (250, 520):
            d.rounded_rectangle((230, y0, 794, y0 + 220), radius=50, fill=W)
            d.ellipse((640, y0 + 85, 690, y0 + 135), fill=_shade(base, 0.8) + (255,))
    elif glyph == "bucket":
        d.polygon([(250, 330), (774, 330), (700, 800), (324, 800)], fill=W)
        d.ellipse((250, 260, 774, 400), fill=W)
        d.ellipse((300, 285, 724, 375), fill=_shade(base, 0.85) + (255,))
    elif glyph == "globe":
        d.ellipse((232, 232, 792, 792), outline=W, width=56)
        d.ellipse((392, 232, 632, 792), outline=W, width=48)
        d.line([(250, 512), (774, 512)], fill=W, width=48)
    else:  # облако с диском
        d.ellipse((200, 380, 480, 660), fill=W)
        d.ellipse((340, 250, 700, 610), fill=W)
        d.ellipse((560, 380, 830, 650), fill=W)
        d.rounded_rectangle((260, 500, 780, 660), radius=80, fill=W)
        d.rounded_rectangle((250, 720, 774, 830), radius=40, fill=(255, 255, 255, 235))
        d.ellipse((660, 752, 706, 798), fill=(40, 200, 120, 255))
        d.rounded_rectangle((300, 760, 560, 790), radius=15, fill=_shade(base, 0.85) + (255,))
    return img.resize((size, size), Image.LANCZOS)


def ensure_ico(path: str, tint: str, glyph: str = "cloud") -> str:
    """Создаёт .ico (если ещё нет) и возвращает путь."""
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        draw(tint, 256, glyph).save(path, sizes=ICO_SIZES)
    return path
