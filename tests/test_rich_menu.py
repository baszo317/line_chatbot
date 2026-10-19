"""圖文選單：點擊區域、圖片規格，以及每個按鈕 bot 都接得住。"""
from pathlib import Path

from PIL import Image

from app.bot import Bot
from conftest import make_event
from create_rich_menu import build_body
from rich_menu_layout import HEIGHT, WIDTH

ROOT = Path(__file__).resolve().parents[1]


def test_areas_cover_the_whole_image_without_overlap():
    body = build_body()
    areas = [a["bounds"] for a in body["areas"]]
    assert len(areas) == 6
    assert sum(a["width"] * a["height"] for a in areas) == WIDTH * HEIGHT
    for i, a in enumerate(areas):
        for b in areas[i + 1:]:
            overlap_x = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
            overlap_y = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
            assert overlap_x <= 0 or overlap_y <= 0
    assert len(body["chatBarText"]) <= 14
    assert all(len(a["action"]["label"]) <= 20 for a in body["areas"])


def test_image_meets_line_requirements():
    path = ROOT / "assets" / "richmenu_guest.png"
    assert Image.open(path).size == (WIDTH, HEIGHT)
    assert path.stat().st_size < 1024 * 1024


def test_every_button_gets_a_real_answer(line):
    bot = Bot(line)
    for area in build_body()["areas"]:
        bot.handle_event(make_event(text=area["action"]["text"]))
    assert len(line.replies) == 6
    assert not any("目前沒有資料可以回答" in t for t in line.texts())
