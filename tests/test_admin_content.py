"""後台的「公告與行事曆」「常見問題」「榜單」表單：存檔會寫進「補習班資料」資料夾，並立刻同步給機器人。"""
from __future__ import annotations

import base64
import io
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import admin_content, content, db, knowledge, main
from app.bot import Bot
from test_content import reset_public_data


@pytest.fixture
def folder(tmp_path, monkeypatch):
    root = tmp_path / "補習班資料"
    patched = replace(content.settings, content_dir=root, ai_drafts=False)
    monkeypatch.setattr(content, "settings", patched)
    monkeypatch.setattr(admin_content, "settings", patched)
    monkeypatch.setattr(content, "_state", {"signature": None, "report": None})
    content.sync(force=True)
    yield root
    db.execute("DELETE FROM admin_audit")
    reset_public_data()


@pytest.fixture
def staff(folder):
    c = TestClient(main.app)
    assert c.post("/admin/api/login", json={"name": "櫃台小美", "password": "test-admin-password"}).status_code == 200
    return c


def png_b64(color="#3a7", size=(400, 300)) -> dict:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return {"data": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "name": "x.png"}


def notice(staff, **fields) -> dict:
    r = staff.post("/admin/api/content/notices/save", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def test_exam_notice_form_writes_file_and_bot_uses_it(staff, folder, line):
    r = notice(staff, category="exam", title="段考範圍", body="數學：第 1～3 章", grades=[8], ends_on="2099-11-03")
    assert r == {"key": "公告/考試/段考範圍", "problems": [], "error": None}
    text = (folder / "公告" / "考試" / "段考範圍.txt").read_text("utf-8-sig")
    assert text.startswith("顯示到：2099/11/03\n年級：國二\n\n數學：第 1～3 章")
    item = next(n for n in staff.get("/admin/api/content").json()["notices"] if n["key"] == "公告/考試/段考範圍")
    assert item["grades"] == [8] and item["status"] == "live" and item["ends_on"] == "2099-11-03"
    bot = Bot(line)
    assert "第 1～3 章" in bot._offline_answer("國二段考範圍")[1]["text"]
    assert bot._offline_answer("國一段考範圍")[1]["text"].startswith("目前沒有國一的考試公告")


def test_editing_moves_files_and_keeps_the_image(staff, folder):
    notice(staff, category="event", title="家長日", body="11/20 晚上 7 點", image=png_b64())
    assert (folder / "公告" / "活動" / "家長日.jpg").exists()
    r = notice(staff, key="公告/活動/家長日", category="general", title="期末家長日", body="改到 11/27", grades=[])
    assert r["key"] == "公告/期末家長日"
    assert not list((folder / "公告" / "活動").glob("家長日*"))
    assert (folder / "公告" / "期末家長日.jpg").exists()
    assert (folder / "公告" / "期末家長日.txt").read_text("utf-8-sig").startswith("年級：全部")
    (n,) = [n for n in knowledge.notices() if n["title"] == "期末家長日"]
    assert n["image_token"] and n["body"] == "改到 11/27" and n["grades"] is None
    notice(staff, key="公告/期末家長日", category="general", title="期末家長日", body="改到 11/27", remove_image=True)
    assert not (folder / "公告" / "期末家長日.jpg").exists()
    assert staff.post("/admin/api/content/notices/delete", json={"key": "公告/期末家長日"}).status_code == 200
    assert not list((folder / "公告").glob("期末家長日*"))
    assert "期末家長日" not in {n["title"] for n in knowledge.notices()}


def test_grade_choice_beats_grade_word_in_title(staff):
    notice(staff, category="event", title="國一新生說明會", body="歡迎各年級家長參加", grades=[])
    (n,) = [n for n in knowledge.notices() if n["title"] == "國一新生說明會"]
    assert n["grades"] is None          # 表單選「全部年級」，不會因為標題有「國一」就只給國一


def test_calendar_upload_becomes_current_for_its_grade(staff, folder):
    notice(staff, category="calendar", title="11月行事曆（國小）", grades=[6], image=png_b64("#a33"))
    notice(staff, category="calendar", title="11月行事曆（國中）", grades=[7, 8, 9], image=png_b64("#33a"),
           body="11/3（一）段考週停課")
    cals = {n["title"]: n["grades"] for n in knowledge.calendar_notices()}
    assert cals == {"11月行事曆（國小）": [6], "11月行事曆（國中）": [7, 8, 9]}
    data = staff.get("/admin/api/content").json()
    item = next(n for n in data["notices"] if n["title"] == "11月行事曆（國中）")
    assert item["category"] == "calendar" and item["image"] == "行事曆/11月行事曆（國中）.jpg"
    img = staff.get("/admin/api/content/image", params={"path": item["image"]})
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert staff.get("/admin/api/content/image", params={"path": "../../etc/passwd"}).status_code == 404


def test_form_errors_are_readable(staff):
    r = staff.post("/admin/api/content/notices/save", json={"category": "exam", "title": "  ", "body": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "請填標題"
    r = staff.post("/admin/api/content/notices/save", json={"category": "exam", "title": "空白"})
    assert r.json()["detail"] == "請填內容，或上傳一張圖片"
    heic = {"data": base64.b64encode(b"\x00\x00\x00\x18ftypheic0000").decode()}
    r = staff.post("/admin/api/content/notices/save", json={"category": "exam", "title": "照片", "image": heic})
    assert "HEIC" in r.json()["detail"]
    notice(staff, category="exam", title="模擬考", body="11/15")
    r = staff.post("/admin/api/content/notices/save", json={"category": "exam", "title": "模擬考", "body": "重複"})
    assert r.status_code == 400 and "同樣標題" in r.json()["detail"]
    r = staff.post("/admin/api/content/notices/delete", json={"key": "../secret"})
    assert r.status_code == 400
    r = staff.post("/admin/api/content/read-image", json={"image": png_b64(), "purpose": "calendar"})
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_faq_form(staff, folder, line):
    r = staff.post("/admin/api/content/faqs/save", json={"category": "fee", "title": "可以刷卡嗎",
                                                         "body": "可以，櫃台接受刷卡。", "keywords": ["刷卡", "信用卡"]})
    assert r.json()["key"] == "常見問題/收費/02_可以刷卡嗎"                # 排在同分類最後面
    assert (folder / "常見問題" / "收費" / "02_可以刷卡嗎.txt").read_text("utf-8-sig").startswith("關鍵字：刷卡、信用卡")
    assert "可以，櫃台接受刷卡" in Bot(line)._offline_answer("信用卡可以用嗎")[1]["text"]
    r = staff.post("/admin/api/content/faqs/save", json={"category": "fee", "title": "學費與繳費方式", "body": "x"})
    assert r.status_code == 400 and "已經有一題" in r.json()["detail"]
    r = staff.post("/admin/api/content/faqs/save", json={"key": "常見問題/收費/02_可以刷卡嗎", "category": "fee",
                                                         "title": "可以刷卡嗎？", "body": "可以，不分期。", "keywords": []})
    assert r.json()["key"] == "常見問題/收費/02_可以刷卡嗎？"
    faqs = staff.get("/admin/api/content").json()["faqs"]
    item = next(a for a in faqs if a["key"] == "常見問題/收費/02_可以刷卡嗎？")
    assert (item["title"], item["sort"], item["keywords"], item["live"]) == ("可以刷卡嗎？", 2, [], True)
    staff.post("/admin/api/content/faqs/delete", json={"key": item["key"]})
    assert "可以刷卡嗎？" not in {a["title"] for a in knowledge.articles()}


def test_honor_roll_form(staff, folder):
    rows = [{"school_year": 115, "category": "會考", "display_name": "林○安", "school_name": "○○國中",
             "achievement": "5A++", "consent": True},
            {"school_year": 2026, "category": "段考", "display_name": "王○明", "achievement": "全校第 1 名",
             "consent": False}]
    r = staff.post("/admin/api/content/honors/save", json={"rows": rows})
    assert r.status_code == 200 and r.json()["problems"] == []
    csv_text = (folder / "榜單" / "榜單.csv").read_text("utf-8-sig")
    assert "115,段考,王○明,,全校第 1 名,否,2" in csv_text                     # 西元年自動換成民國年
    assert [h["display_name"] for h in knowledge.honors()] == ["林○安"]          # 沒有家長同意的不公開
    assert [h["display_name"] for h in staff.get("/admin/api/content").json()["honors"]] == ["林○安", "王○明"]


def test_demo_notice_from_outside_the_folder_can_be_deleted(staff):
    db.execute("""INSERT INTO announcements (title, body, category, ends_at)
                  VALUES ('【示範】颱風停課：10/19 晚上課程停課', '示範', 'closure', now() + interval '1 day')""")
    knowledge.invalidate()
    outside = staff.get("/admin/api/content").json()["outside"]
    (demo,) = [o for o in outside if o["title"].startswith("【示範】")]
    assert demo["demo"] and demo["table"] == "announcements"
    warning = next(w for w in staff.get("/admin/api/status").json()["warnings"] if w.get("action") == "delete_outside")
    assert warning["id"] == demo["id"]
    assert staff.post("/admin/api/content/outside/delete", json={"table": "announcements", "id": demo["id"]}).json()["ok"]
    assert not [n for n in knowledge.notices() if n["title"].startswith("【示範】")]
