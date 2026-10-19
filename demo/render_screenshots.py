"""把 demo/output/transcript.json 的對話畫成截圖（demo/output/screenshots/case1.png …）。

用法：
    pip install playwright && playwright install chromium   # 第一次才需要
    python demo/render_screenshots.py

截圖是「本機模擬器」的畫面，用來做報告或教學；實際外觀以 LINE App 為準。
每則 bot 回覆下面的小標籤，說明這則是固定回覆、AI 回答，還是轉真人。
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "demo" / "output"
MENU_IMAGE = ROOT / "assets" / "richmenu_guest.png"
SHOW_MENU = {"case1", "case2"}

CSS = """
* { box-sizing: border-box; margin: 0; }
body { background: #e9eef4; font-family: "Noto Sans CJK TC", "Noto Color Emoji", sans-serif; padding: 16px; }
.phone { width: 400px; background: #f3f5f8; border: 1px solid #d5dde7; border-radius: 22px; overflow: hidden; }
.sim { background: #1f2733; color: #d6dde6; font-size: 11px; text-align: center; padding: 5px 0; letter-spacing: .5px; }
.head { background: #fff; display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid #e1e6ee; }
.avatar { width: 34px; height: 34px; border-radius: 50%; background: #2f6fdb; color: #fff; font-weight: 700;
          display: flex; align-items: center; justify-content: center; font-size: 15px; flex: none; }
.avatar.sm { width: 28px; height: 28px; font-size: 13px; }
.title { font-size: 15px; font-weight: 700; color: #1f2733; }
.subtitle { font-size: 11px; color: #6b7685; margin-top: 1px; }
.chat { padding: 14px 12px 16px; display: flex; flex-direction: column; gap: 12px; }
.user { align-self: flex-end; max-width: 78%; background: #2f6fdb; color: #fff; padding: 9px 13px;
        border-radius: 18px 18px 6px 18px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; }
.bot { display: flex; gap: 8px; align-items: flex-start; }
.col { display: flex; flex-direction: column; gap: 6px; max-width: 82%; }
.bubble { background: #fff; border: 1px solid #e1e6ee; color: #1f2733; padding: 9px 13px; border-radius: 6px 18px 18px 18px;
          font-size: 14px; line-height: 1.55; white-space: pre-wrap; }
.quick { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { border: 1px solid #2f6fdb; color: #2f6fdb; background: #fff; border-radius: 999px; padding: 4px 11px; font-size: 12.5px; }
.tag { font-size: 11px; color: #5b6676; display: flex; align-items: center; gap: 5px; }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.note { align-self: center; background: #dfe5ed; color: #4b5563; font-size: 12px; border-radius: 999px; padding: 4px 12px; }
.menu-bar { background: #fff; border-top: 1px solid #e1e6ee; text-align: center; font-size: 12px; color: #6b7685; padding: 7px 0; }
.menu img { display: block; width: 100%; }
"""

TAG_COLORS = [("轉給櫃台", "#0e8fb0"), ("回到 AI", "#0e8fb0"), ("AI 暫停", "#98a2b3"),
              ("AI 回答", "#7b5be6"), ("關鍵字", "#e59a12"), ("固定回覆", "#1f9d74")]


def tag_color(label: str) -> str:
    return next((c for key, c in TAG_COLORS if key in label), "#98a2b3")


def render_case(case: dict, menu_data_uri: str | None) -> str:
    esc = html.escape
    parts = ['<div class="phone"><div class="sim">本機模擬器畫面（示意）・實際以 LINE App 顯示為準</div>',
             '<div class="head"><div class="avatar">高</div><div><div class="title">高明補習班</div>'
             '<div class="subtitle">AI 小助理・第一期</div></div></div><div class="chat">']
    for step in case["steps"]:
        if step["event"] == "follow":
            parts.append('<div class="note">📥 家長加入官方帳號好友</div>')
        else:
            parts.append(f'<div class="user">{esc(step["user"])}</div>')
        if not step["bot"]:
            parts.append(f'<div class="note">⏸ {esc(step["path_label"])}</div>')
            continue
        for i, msg in enumerate(step["bot"]):
            chips = "".join(f'<span class="chip">{esc(q)}</span>' for q in msg["quick_replies"])
            tag = ""
            if i == len(step["bot"]) - 1:
                label = step["path_label"]
                tag = (f'<div class="tag"><span class="dot" style="background:{tag_color(label)}"></span>'
                       f'{esc(label)}</div>')
            parts.append(f'<div class="bot"><div class="avatar sm">高</div><div class="col">'
                         f'<div class="bubble">{esc(msg["text"])}</div>'
                         f'{f"<div class=quick>{chips}</div>" if chips else ""}{tag}</div></div>')
    parts.append("</div>")
    if menu_data_uri and case["id"] in SHOW_MENU:
        parts.append(f'<div class="menu-bar">點我開啟選單 ▾</div><div class="menu"><img src="{menu_data_uri}"></div>')
    parts.append("</div>")
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(parts)}</body></html>'


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", default=str(OUT / "transcript.json"))
    parser.add_argument("--suffix", default="", help="檔名後綴，例如 _offline")
    args = parser.parse_args()
    transcript = json.loads(Path(args.transcript).read_text("utf-8"))
    menu_uri = None
    if MENU_IMAGE.exists():
        menu_uri = "data:image/png;base64," + base64.b64encode(MENU_IMAGE.read_bytes()).decode()
    shots = OUT / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=2, viewport={"width": 440, "height": 900})
        for case in transcript["cases"]:
            page.set_content(render_case(case, menu_uri))
            page.wait_for_load_state("networkidle")
            path = shots / f"{case['id']}{args.suffix}.png"
            page.locator(".phone").screenshot(path=str(path))
            print(f"已產生 {path}")
        browser.close()


if __name__ == "__main__":
    main()
