"""產生訪客圖文選單圖片 assets/richmenu_guest.png（2500×1686，小於 1 MB）。

用法：python scripts/make_rich_menu_image.py
需要：pip install pillow，以及系統裡的 Noto Sans CJK 字型（或用 --font 指定任一中文字型檔）。
想換顏色或文字，改 scripts/rich_menu_layout.py 就好。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rich_menu_layout import HEIGHT, WIDTH, cells  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
S = 2  # 先畫兩倍大再縮小，邊緣才平滑
def font_candidates(weight: str) -> list[str]:
    return [
        f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc",     # Linux
        f"/usr/share/fonts/noto-cjk/NotoSansCJK-{weight}.ttc",
        "/System/Library/Fonts/PingFang.ttc",                          # macOS
        "C:/Windows/Fonts/msjhbd.ttc" if weight == "Bold" else "C:/Windows/Fonts/msjh.ttc",  # Windows 微軟正黑體
    ]


def load_font(weight: str, size: int, override: str | None) -> ImageFont.FreeTypeFont:
    for path in [override] if override else font_candidates(weight):
        if not path or not Path(path).exists():
            continue
        for index in range(12):  # .ttc 裡有多套字型，優先挑繁體中文（TC）那一套
            try:
                font = ImageFont.truetype(path, size, index=index)
            except OSError:
                break
            if "TC" in font.getname()[0]:
                return font
        return ImageFont.truetype(path, size)
    raise SystemExit("找不到中文字型，請用 --font 指定字型檔路徑")


def icon(d: ImageDraw.ImageDraw, kind: str, cx: int, cy: int, accent: str, q_font) -> None:
    w = "white"
    u = S  # 尺寸都乘上 S
    if kind == "課程介紹":   # 打開的書
        d.polygon([(cx - 100 * u, cy - 60 * u), (cx - 10 * u, cy - 38 * u), (cx - 10 * u, cy + 78 * u), (cx - 100 * u, cy + 56 * u)], fill=w)
        d.polygon([(cx + 100 * u, cy - 60 * u), (cx + 10 * u, cy - 38 * u), (cx + 10 * u, cy + 78 * u), (cx + 100 * u, cy + 56 * u)], fill=w)
        for i in range(3):
            y = cy - 22 * u + i * 30 * u
            d.line([(cx - 80 * u, y - 4 * u), (cx - 30 * u, y + 8 * u)], fill=accent, width=7 * u)
            d.line([(cx + 80 * u, y - 4 * u), (cx + 30 * u, y + 8 * u)], fill=accent, width=7 * u)
    elif kind == "榜單":     # 獎盃
        d.pieslice([cx - 72 * u, cy - 150 * u, cx + 72 * u, cy + 20 * u], 0, 180, fill=w)
        d.rectangle([cx - 72 * u, cy - 80 * u, cx + 72 * u, cy - 64 * u], fill=w)
        d.arc([cx - 120 * u, cy - 70 * u, cx - 40 * u, cy + 4 * u], 90, 270, fill=w, width=14 * u)
        d.arc([cx + 40 * u, cy - 70 * u, cx + 120 * u, cy + 4 * u], 270, 90, fill=w, width=14 * u)
        d.rectangle([cx - 14 * u, cy + 18 * u, cx + 14 * u, cy + 58 * u], fill=w)
        d.rounded_rectangle([cx - 62 * u, cy + 56 * u, cx + 62 * u, cy + 84 * u], radius=10 * u, fill=w)
    elif kind == "交通資訊":  # 地圖圖釘
        d.ellipse([cx - 66 * u, cy - 104 * u, cx + 66 * u, cy + 28 * u], fill=w)
        d.polygon([(cx - 57 * u, cy - 4 * u), (cx + 57 * u, cy - 4 * u), (cx, cy + 96 * u)], fill=w)
        d.ellipse([cx - 26 * u, cy - 64 * u, cx + 26 * u, cy - 12 * u], fill=accent)
    elif kind == "聯絡我們":  # 對話框
        d.rounded_rectangle([cx - 100 * u, cy - 78 * u, cx + 100 * u, cy + 48 * u], radius=42 * u, fill=w)
        d.polygon([(cx - 60 * u, cy + 40 * u), (cx - 18 * u, cy + 40 * u), (cx - 74 * u, cy + 92 * u)], fill=w)
        for dx in (-48, 0, 48):
            d.ellipse([cx + (dx - 13) * u, cy - 28 * u, cx + (dx + 13) * u, cy - 2 * u], fill=accent)
    elif kind == "常見問題":  # 問號
        d.text((cx, cy - 6 * u), "?", font=q_font, fill=w, anchor="mm")
    elif kind == "家長綁定":  # 鎖頭
        d.arc([cx - 56 * u, cy - 112 * u, cx + 56 * u, cy + 4 * u], 180, 360, fill=w, width=22 * u)
        d.rectangle([cx - 56 * u - 11 * u, cy - 54 * u, cx - 56 * u + 11 * u, cy - 20 * u], fill=w)
        d.rectangle([cx + 56 * u - 11 * u, cy - 54 * u, cx + 56 * u + 11 * u, cy - 20 * u], fill=w)
        d.rounded_rectangle([cx - 84 * u, cy - 30 * u, cx + 84 * u, cy + 92 * u], radius=24 * u, fill=w)
        d.ellipse([cx - 16 * u, cy + 8 * u, cx + 16 * u, cy + 40 * u], fill=accent)
        d.rectangle([cx - 6 * u, cy + 30 * u, cx + 6 * u, cy + 66 * u], fill=accent)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "assets" / "richmenu_guest.png"))
    parser.add_argument("--font", help="中文字型檔路徑（.ttc / .ttf / .otf）")
    args = parser.parse_args()

    label_font = load_font("Bold", 112 * S, args.font)
    sub_font = load_font("Regular", 58 * S, args.font)
    q_font = load_font("Bold", 230 * S, args.font)

    img = Image.new("RGB", (WIDTH * S, HEIGHT * S), "#EEF2F7")
    d = ImageDraw.Draw(img)
    gap = 22 * S
    for _r, _c, x, y, w, h, label, sub, color in cells():
        x, y, w, h = x * S, y * S, w * S, h * S
        d.rounded_rectangle([x + gap, y + gap, x + w - gap, y + h - gap], radius=48 * S,
                            fill="white", outline="#D8E0EA", width=3 * S)
        cx, cy = x + w // 2, y + 300 * S
        d.ellipse([cx - 150 * S, cy - 150 * S, cx + 150 * S, cy + 150 * S], fill=color)
        icon(d, label, cx, cy, color, q_font)
        d.text((cx, y + 590 * S), label, font=label_font, fill="#1F2733", anchor="mm")
        d.text((cx, y + 700 * S), sub, font=sub_font, fill="#6B7685", anchor="mm")

    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(out, optimize=True)
    size_kb = out.stat().st_size / 1024
    print(f"已產生 {out}（{WIDTH}×{HEIGHT}，{size_kb:.0f} KB）")
    if size_kb > 1024:
        print("警告：超過 1 MB，LINE 會拒收，請簡化圖片或改存 JPEG")


if __name__ == "__main__":
    main()
