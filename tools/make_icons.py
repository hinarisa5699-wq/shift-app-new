"""アプリアイコン（Alohaシフト）を生成する。

生成物は static/icons/ に置き、そのままリポジトリへコミットする
（本番の実行時に Pillow は不要）。デザインを変えたいときだけ再実行する:

    python tools/make_icons.py
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "static", "icons")

# 海 → 夕日のグラデーション（アロハらしい配色）
TOP = (14, 165, 183)      # ターコイズ
MID = (56, 189, 174)      # エメラルド
BOTTOM = (251, 146, 60)   # サンセットオレンジ
PETAL = (255, 255, 255)
PETAL_EDGE = (255, 228, 196)
CENTER = (250, 204, 21)   # 花芯（黄）
STAMEN = (249, 115, 22)


def _lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), TOP)
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        color = _lerp(TOP, MID, t / 0.55) if t < 0.55 else _lerp(MID, BOTTOM, (t - 0.55) / 0.45)
        d.line([(0, y), (size, y)], fill=color)
    return img


def _rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return mask


def _petal(draw: ImageDraw.ImageDraw, cx, cy, angle_deg, length, width, fill, outline):
    """花びら1枚（中心から外へ伸びる楕円）を描く。"""
    ang = math.radians(angle_deg)
    px = cx + math.cos(ang) * length * 0.52
    py = cy + math.sin(ang) * length * 0.52
    petal = Image.new("RGBA", (int(length * 1.4), int(width * 1.4)), (0, 0, 0, 0))
    pd = ImageDraw.Draw(petal)
    pd.ellipse([0, 0, petal.width - 1, petal.height - 1], fill=fill, outline=outline,
               width=max(1, int(width * 0.06)))
    petal = petal.rotate(-angle_deg, expand=True, resample=Image.BICUBIC)
    draw._image.paste(petal, (int(px - petal.width / 2), int(py - petal.height / 2)), petal)


def make_icon(size: int) -> Image.Image:
    img = _gradient(size).convert("RGBA")
    d = ImageDraw.Draw(img)
    d._image = img

    # 下部に波（白の弧を2本）
    wave_y = int(size * 0.74)
    for i, alpha in enumerate((80, 130, 200)):
        off = int(size * 0.055) * i
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse([-size * 0.35, wave_y + off, size * 1.35, wave_y + off + size * 0.6],
                   fill=(255, 255, 255, alpha))
        img.alpha_composite(layer)

    # ハイビスカス（5枚の花びら）
    cx, cy = size * 0.5, size * 0.44
    length, width = size * 0.30, size * 0.21
    for k in range(5):
        _petal(d, cx, cy, -90 + k * 72, length, width, PETAL, PETAL_EDGE)

    # 花芯
    r = size * 0.055
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CENTER)
    # おしべ
    d.line([cx + size * 0.015, cy - size * 0.015,
            cx + size * 0.155, cy - size * 0.155], fill=STAMEN,
           width=max(2, int(size * 0.022)))
    r2 = size * 0.028
    d.ellipse([cx + size * 0.155 - r2, cy - size * 0.155 - r2,
               cx + size * 0.155 + r2, cy - size * 0.155 + r2], fill=STAMEN)

    img.putalpha(_rounded_mask(size))
    return img


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for size, name in ((512, "icon-512.png"), (192, "icon-192.png"),
                       (180, "apple-touch-icon.png"), (32, "favicon-32.png")):
        make_icon(size).save(os.path.join(OUT_DIR, name))
    # favicon.ico（16/32/48をまとめる）
    make_icon(256).save(os.path.join(OUT_DIR, "favicon.ico"),
                        sizes=[(16, 16), (32, 32), (48, 48)])
    print("icons written to", OUT_DIR)


if __name__ == "__main__":
    main()
