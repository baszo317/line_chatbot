"""「補習班資料」資料夾同步：丟檔案就更新資料庫，機器人回覆會帶圖片。"""
from __future__ import annotations

import io
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest
from PIL import Image

from app import content, db, knowledge, vision
from app.bot import Bot
from conftest import ROOT, make_event


def jpg(path: Path, color: str = "#3a7", size=(800, 600)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def txt(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def reset_public_data() -> None:
    """還原成 conftest 建立時的示範資料，避免影響其他測試。"""
    from import_kb import import_file
    for table in ("announcements", "kb_articles", "honor_roll", "content_files", "media_files"):
        db.execute(f"DELETE FROM {table}")
    db.execute((ROOT / "sql" / "phase1_seed.sql").read_text("utf-8"))
    import_file(str(ROOT / "data" / "kb_template.csv"))
    knowledge.invalidate()


@pytest.fixture
def folder(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "補習班資料"
    monkeypatch.setattr(content, "settings", replace(content.settings, content_dir=root, ai_drafts=False))
    monkeypatch.setattr(content, "_state", {"signature": None, "report": None})
    yield root
    reset_public_data()


def sync() -> content.Report:
    report = content.sync(force=True)
    assert report.error is None, report.error
    return report


def test_first_sync_exports_current_data(folder):
    db.execute("""INSERT INTO announcements (title, body, category, ends_at) VALUES
                  ('寒假班開始報名', '12/20 前報名有優惠', 'enrollment', now() + interval '30 days'),
                  ('【示範】颱風停課：10/19 晚上課程停課', '示範', 'closure', now() + interval '1 day')""")
    report = sync()
    notice = folder / "公告" / "招生" / "寒假班開始報名.txt"
    assert notice.read_text("utf-8-sig").startswith("顯示到：") and "12/20 前報名有優惠" in notice.read_text("utf-8-sig")
    assert not list((folder / "公告").rglob("【示範】*"))          # run_demo.py 的示範公告不匯出
    assert "公告：【示範】颱風停課：10/19 晚上課程停課" in report.outside
    assert (folder / "使用說明.txt").exists() and (folder / "_同步結果.txt").exists()
    article = folder / "常見問題" / "規定" / "01_請假與補課規定.txt"
    assert "請在上課前透過 LINE 或電話請假" in article.read_text("utf-8-sig")
    assert "關鍵字：請假、補課" in article.read_text("utf-8-sig")
    assert "會考" in (folder / "榜單" / "榜單.csv").read_text("utf-8-sig")
    assert len(report.kb) == 8 and report.honor_public == 3 and report.exported
    # 匯出後由檔案管理，同一題不會變成兩題
    assert db.fetch_one("SELECT count(*) AS n FROM kb_articles")["n"] == 8
    assert db.fetch_one("SELECT count(*) AS n FROM kb_articles WHERE source_path IS NULL")["n"] == 0
    # 沒有變動時不重複同步
    assert content.sync() is None


def test_latest_calendar_wins_and_bot_sends_the_image(folder, line):
    sync()
    jpg(folder / "行事曆" / "10月行事曆.jpg", "#a33")
    sync()
    db.execute("UPDATE content_files SET first_seen_at = now() - interval '20 days'")   # 好像是上個月丟的
    jpg(folder / "行事曆" / "11月行事曆.jpg", "#33a")
    txt(folder / "行事曆" / "11月行事曆.txt", "11/3（一）段考週，國中部停課\n11/15（六）國三模擬考")
    report = sync()
    assert report.calendar == ["11月行事曆（附圖）"]
    assert report.unused_calendars == ["行事曆/10月行事曆"]

    Bot(line).handle_event(make_event(text="行事曆"), public_base="https://bot.example.com")
    (msgs,) = [m for _, m in line.replies]
    assert msgs[0]["type"] == "text" and "11/15（六）國三模擬考" in msgs[0]["text"]
    assert msgs[1]["type"] == "image"
    assert msgs[1]["originalContentUrl"].startswith("https://bot.example.com/files/")
    assert msgs[1]["previewImageUrl"].endswith("-preview.jpg")


def test_calendar_pages_dropped_together_are_one_batch(folder):
    jpg(folder / "行事曆" / "11月-正面.jpg")
    jpg(folder / "行事曆" / "11月-背面.jpg", "#fff")
    report = sync()
    assert sorted(report.calendar) == ["11月-正面（附圖）", "11月-背面（附圖）"]
    assert len(knowledge.calendar_images()) == 2


def test_camera_file_name_gets_a_readable_title(folder):
    jpg(folder / "行事曆" / "IMG_20261101_0930.jpg")
    report = sync()
    assert report.calendar == [f"{datetime.now(content.TW).month} 月行事曆（附圖）"]


def test_notice_files_and_expiry(folder, line):
    txt(folder / "公告" / "停課" / "颱風停課.txt", "顯示到：2099/12/31\n\n明天晚上所有課程停課。")
    jpg(folder / "公告" / "停課" / "颱風停課.jpg")
    txt(folder / "公告" / "舊活動.txt", "顯示到：2020/1/1\n\n去年的活動")
    txt(folder / "公告" / "11月模擬考.txt", "國三模擬考請帶 2B 鉛筆", encoding="cp950")   # 舊版記事本的 Big5
    report = sync()
    assert "颱風停課（附圖）［停課，顯示到 12/31］" in report.notices
    assert "11月模擬考［一般］" in report.notices
    assert report.expired == ["公告/舊活動（顯示到 2020/01/01）"]
    active = {n["title"] for n in knowledge.notices()}
    assert active == {"颱風停課", "11月模擬考"}

    Bot(line).handle_event(make_event(text="明天颱風會停課嗎？"), public_base="https://bot.example.com")
    msgs = line.replies[0][1]
    assert "明天晚上所有課程停課" in msgs[0]["text"] and msgs[1]["type"] == "image"

    (folder / "公告" / "停課" / "颱風停課.txt").unlink()        # 刪掉文字檔：只剩圖片，還是同一則
    (folder / "公告" / "停課" / "颱風停課.jpg").unlink()        # 兩個都刪 → 下架
    sync()
    assert {n["title"] for n in knowledge.notices()} == {"11月模擬考"}


def test_kb_files_add_edit_delete(folder, line):
    sync()
    jpg(folder / "常見問題" / "交通" / "01_地址與交通方式.jpg")
    txt(folder / "常見問題" / "收費" / "02_可以刷卡嗎.txt", "關鍵字：刷卡、信用卡\n\n可以，櫃台接受刷卡（不分期）。")
    (folder / "常見問題" / "其他" / "01_自習教室.txt").unlink()
    report = sync()
    assert "交通／地址與交通方式（附圖）" in report.kb and "收費／可以刷卡嗎" in report.kb
    titles = {a["title"] for a in knowledge.articles()}
    assert "可以刷卡嗎" in titles and "自習教室" not in titles

    bot = Bot(line)
    bot.handle_event(make_event(text="請問可以刷卡嗎？"))
    assert "可以，櫃台接受刷卡" in line.texts()[0]
    bot.handle_event(make_event(text="交通資訊"), public_base="https://bot.example.com")
    msgs = line.replies[1][1]
    assert msgs[0]["text"].startswith("🚌 交通資訊") and msgs[1]["type"] == "image"


def test_quick_reply_moves_to_the_last_message(folder):
    sync()
    jpg(folder / "常見問題" / "聯絡" / "01_聯絡電話與服務時間.jpg")
    sync()
    out = Bot().preview("聯絡我們")["messages"]
    assert [m["type"] for m in out] == ["text", "image"]
    assert "quickReply" not in out[0] and out[1]["quickReply"]["items"][0]["action"]["text"] == "找真人客服"


def test_images_are_skipped_without_https_address(folder, line):
    jpg(folder / "行事曆" / "11月行事曆.jpg")
    sync()
    Bot(line).handle_event(make_event(text="行事曆"))      # 本機測試，不知道 https 網址
    assert [m["type"] for m in line.replies[0][1]] == ["text"]
    out = db.fetch_one("SELECT tool_calls FROM message_logs WHERE direction = 'out'")
    assert out["tool_calls"] == {"images_skipped": 1}


def test_problem_files_are_reported(folder):
    (folder / "公告").mkdir(parents=True)
    (folder / "公告" / "照片.HEIC").write_bytes(b"\x00\x00\x00\x18ftypheic0000")
    (folder / "公告" / "通知.docx").write_bytes(b"PK")
    txt(folder / "公告" / "空白.txt", "   \n")
    (folder / "公告" / "壞掉.jpg").write_bytes(b"not an image")
    txt(folder / "雜項" / "a.txt", "?")
    report = sync()
    text = "\n".join(report.problems)
    assert "HEIC" in text and "Word" in text and "空白.txt：內容是空的" in text
    assert "壞掉.jpg：看不懂這個檔案" in text and "不認得「雜項」" in text
    assert "公告/壞掉：" not in text                                     # 同一個問題不重複列
    assert "■ 有問題的檔案" in (folder / "_同步結果.txt").read_text("utf-8-sig")


def test_honor_csv(folder):
    sync()
    txt(folder / "榜單" / "榜單.csv",
        "學年度,類別,姓名,學校,成績,家長同意,排序\n"
        "114,會考,張○安,○○國中,5A++,是,1\n"
        "114,會考,王小明,○○國中,4A,否,2\n"
        "115學年度,段考,陳小華,○○國中,全校第 3 名,是,3\n"
        ",會考,,,,,\n",
        encoding="utf-8-sig")
    report = sync()
    assert report.honor_public == 2 and report.honor_hidden == 1
    assert [h["display_name"] for h in knowledge.honors()] == ["陳小華", "張○安"]     # 新的學年度在前
    assert any("陳小華" in n and "○" in n for n in report.notes)                      # 提醒名字要遮
    assert any("第 5 列" in p for p in report.problems)


def test_ai_draft_is_not_used_until_checked(folder, monkeypatch, line):
    monkeypatch.setattr(content, "settings", replace(content.settings, ai_drafts=True))
    monkeypatch.setattr(vision, "available", lambda: True)
    calls = []

    def fake_read(media_id, purpose, hint=""):
        calls.append((purpose, hint))
        return {"title": "11月行事曆", "text": "11/3（一）段考週停課", "month": "2026-11", "unclear": ["11/20 的字"],
                "model": "fake"}
    monkeypatch.setattr(vision, "read_image", fake_read)

    jpg(folder / "行事曆" / "11月行事曆.jpg")
    report = sync()
    draft = folder / "行事曆" / "11月行事曆.txt"
    assert calls == [("calendar", "檔名：11月行事曆")]
    assert draft.read_text("utf-8-sig").startswith("【AI 草稿】")
    assert report.drafts == ["行事曆/11月行事曆.txt"]
    assert "段考週" not in knowledge.knowledge_text()              # 草稿還沒核對：只傳圖片

    sync()
    assert len(calls) == 1                                        # 不會重複請 AI 讀同一張圖
    checked = "\n".join(l for l in draft.read_text("utf-8-sig").splitlines() if not l.startswith("【AI 草稿】"))
    draft.write_text(checked, encoding="utf-8")
    report = sync()
    assert report.drafts == [] and "11/3（一）段考週停課" in knowledge.knowledge_text()


def test_parse_dates():
    today = date(2026, 10, 18)
    assert content.parse_date("2026/10/21", today) == date(2026, 10, 21)
    assert content.parse_date("115/10/21", today) == date(2026, 10, 21)           # 民國年
    assert content.parse_date("2026年10月21日", today) == date(2026, 10, 21)
    assert content.parse_date("10/21", today) == date(2026, 10, 21)
    assert content.parse_date("1/5", today) == date(2027, 1, 5)                   # 已經過了很久 → 明年
    assert content.parse_date("13/40", today) is None
    info = content.parse_text("﻿顯示到：10/25\n關鍵字：a、b, c\n\n內文\n第二行\n", today)
    assert (info.ends_on, info.keywords, info.body) == (date(2026, 10, 25), ["a", "b", "c"], "內文\n第二行")
    grades = lambda line: content.parse_text(f"年級：{line}\n\n內文", today)   # noqa: E731
    assert grades("國一、國二").grades == (7, 8) and grades("國一國二").grades == (7, 8)
    assert grades("國中").grades == (7, 8, 9) and grades("全部").grades == () and grades("國小 國一 國二 國三").grades == ()
    assert grades("高一").grades is None and "看不懂" in grades("高一").problems[0]


def test_files_endpoint_serves_images(folder):
    from fastapi.testclient import TestClient
    from app import main
    jpg(folder / "行事曆" / "11月行事曆.jpg", size=(5000, 3000))
    sync()
    token = knowledge.calendar_images()[0]
    client = TestClient(main.app)
    big = client.get(f"/files/{token}.jpg")
    small = client.get(f"/files/{token}-preview.jpg")
    assert big.status_code == 200 and big.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(big.content)).size) == 4096           # 原圖縮到 LINE 的上限內
    assert max(Image.open(io.BytesIO(small.content)).size) == 1024 and len(small.content) < 1024 * 1024
    assert client.get("/files/not-a-token.jpg").status_code == 404


def test_date_questions_use_the_calendar_only_when_there_is_one(folder, line):
    bot = Bot(line)
    assert bot._offline_answer("國二數學 11/3 有上課嗎")[0] == "course"          # 還沒有行事曆 → 照舊回班級資訊
    jpg(folder / "行事曆" / "11月行事曆.jpg")
    txt(folder / "行事曆" / "11月行事曆.txt", "11/3（一）段考週，國中部暫停上課")
    sync()
    intent, message, _ = bot._offline_answer("國二數學 11/3 有上課嗎")
    assert intent == "announcement" and "段考週" in message["text"]
    assert bot._offline_answer("下禮拜有什麼活動")[0] == "announcement"


def test_deleting_a_whole_folder_restores_it_instead_of_wiping_the_bot(folder):
    """整個資料夾被刪掉或搬走（例如雲端硬碟還沒同步好）時，不會把機器人的資料清空，而是從資料庫把檔案寫回來。"""
    import shutil
    sync()
    jpg(folder / "行事曆" / "11月行事曆.jpg")
    jpg(folder / "常見問題" / "交通" / "01_地址與交通方式.jpg")
    txt(folder / "公告" / "寒假班.txt", "顯示到：2099/1/31\n\n開始報名")
    sync()
    before = {a["title"] for a in knowledge.articles()}
    for name in ("常見問題", "行事曆", "公告", "榜單"):
        shutil.rmtree(folder / name)
    report = sync()
    assert {a["title"] for a in knowledge.articles()} == before and len(before) == 8
    assert report.calendar == ["11月行事曆（附圖）"] and report.notices == ["寒假班［一般，顯示到 01/31］"]
    assert (folder / "行事曆" / "11月行事曆.jpg").exists() and not (folder / "行事曆" / "11月行事曆.txt").exists()
    assert (folder / "常見問題" / "交通" / "01_地址與交通方式.jpg").exists()
    assert (folder / "公告" / "寒假班.txt").read_text("utf-8-sig").startswith("顯示到：2099/01/31")
    assert report.honor_public == 3
    (folder / "常見問題" / "其他" / "01_自習教室.txt").unlink()          # 刪單一檔案 → 這題下架
    sync()
    assert "自習教室" not in {a["title"] for a in knowledge.articles()}


def test_exam_notice_image_can_be_found_by_keywords(folder, line):
    jpg(folder / "公告" / "考試" / "11月段考範圍.png")
    sync()
    bot = Bot(line)
    for q in ("段考考什麼", "11月段考範圍", "最近有什麼公告"):
        intent, message, _ = bot._offline_answer(q)
        assert intent == "announcement" and "【11月段考範圍】" in message["text"], q
    bot.handle_event(make_event(text="國二段考範圍是什麼"), public_base="https://bot.example.com")
    assert [m["type"] for m in line.replies[0][1]] == ["text", "image"]
    assert bot._offline_answer("補習班在哪裡？")[0] == "traffic"          # 其他問題不受影響


def test_notices_and_calendars_by_grade(folder, line):
    """年級資料夾：家長說了年級就只給那個年級（和全部年級）的；沒說就全部列出並註明年級。"""
    jpg(folder / "公告" / "考試" / "國二" / "段考範圍.png")
    txt(folder / "公告" / "考試" / "國三" / "段考範圍.txt", "國三理化考到第 4 章")
    txt(folder / "公告" / "考試" / "模擬考報名.txt", "全部年級都可以報名模擬考")
    jpg(folder / "行事曆" / "國小" / "11月行事曆.jpg")
    jpg(folder / "行事曆" / "國中" / "11月行事曆.jpg", "#a33")
    report = sync()
    assert sorted(report.calendar) == ["11月行事曆（國中）（附圖）", "11月行事曆（國小）（附圖）"]
    assert "段考範圍（國三）［考試］" in report.notices
    assert db.fetch_one("SELECT grades FROM announcements WHERE source_path = '公告/考試/國二/段考範圍'")["grades"] == [8]

    bot = Bot(line)
    _, message, _ = bot._offline_answer("國二段考範圍")
    assert "【段考範圍】（國二）" in message["text"] and "國三" not in message["text"]
    assert "【模擬考報名】" in message["text"]                       # 全部年級的也會給
    _, message, _ = bot._offline_answer("段考考什麼")                 # 沒說年級 → 全部列出並註明
    assert "【段考範圍】（國二）" in message["text"] and "【段考範圍】（國三）" in message["text"]
    _, message, _ = bot._offline_answer("國小行事曆")
    assert "（國小）" in message["text"] and "（國中）" not in message["text"]
    _, message, _ = bot._offline_answer("八年級這個月行事曆")         # 國中＝國一～國三
    assert "（國中）" in message["text"] and "（國小）" not in message["text"]
    bot.handle_event(make_event(text="行事曆"), public_base="https://bot.example.com")
    assert [m["type"] for m in line.replies[0][1]] == ["text", "image", "image"]
    assert "適用年級：國三（其他年級不適用）" in knowledge.knowledge_text()     # AI 也知道是哪個年級的


def test_other_grades_notice_is_not_sent(folder, line):
    """問國二考試、只有國一的公告：不傳國一的，直接說沒有國二的，並建議找真人。"""
    txt(folder / "公告" / "考試" / "國一" / "段考範圍.txt", "國一數學考到第 3 章")
    jpg(folder / "行事曆" / "國小" / "11月行事曆.jpg")
    sync()
    bot = Bot(line)
    for q in ("國二考試", "國二有考試嗎", "國二段考範圍"):
        intent, message, needs_human = bot._offline_answer(q)
        assert intent == "announcement" and needs_human, q
        assert message["text"].startswith("目前沒有國二的考試公告") and "國一的「段考範圍」" in message["text"], q
        assert "第 3 章" not in message["text"] and "國二數學 A 班" not in message["text"], q   # 不傳國一的、也不改回課程
        assert "找真人客服" in [i["action"]["text"] for i in message["quickReply"]["items"]]
    intent, message, _ = bot._offline_answer("國二行事曆")
    assert message["text"].startswith("目前沒有國二的行事曆")
    assert bot._offline_answer("國一段考範圍")[1]["text"].startswith("📢 公告")   # 問對年級照常回答


def test_no_exam_notice_at_all_does_not_fall_back_to_courses(folder, line):
    """完全沒有考試公告時，問「國二考試」要說沒有，不能回「國二有什麼課」。"""
    jpg(folder / "行事曆" / "國中" / "11月行事曆.jpg")
    sync()
    intent, message, needs_human = Bot(line)._offline_answer("國二有考試嗎")
    assert intent == "announcement" and needs_human
    assert message["text"].startswith("目前沒有國二的考試公告") and "📚" not in message["text"]
    assert [i["action"]["text"] for i in message["quickReply"]["items"]] == ["找真人客服", "行事曆", "聯絡我們"]


def test_grade_in_file_name_wins_over_junior_high_folder(folder, line):
    """「國一行事曆.png」放在「國中」資料夾：只給國一，不會拿去回答國二。"""
    jpg(folder / "行事曆" / "國中" / "國一行事曆.png")
    jpg(folder / "行事曆" / "國中" / "國二行事曆.png", "#a33")
    txt(folder / "公告" / "考試" / "國二段考範圍.txt", "年級：國二、國三\n\n範圍：第 1～3 章")
    report = sync()
    assert sorted(report.calendar) == ["國一行事曆（國一）（附圖）", "國二行事曆（國二）（附圖）"]   # 兩個年級各自最新
    grades = {r["title"]: r["grades"] for r in db.fetch_all("SELECT title, grades FROM announcements")}
    assert grades == {"國一行事曆": [7], "國二行事曆": [8], "國二段考範圍": [8, 9]}      # 「年級：」那一行優先
    bot = Bot(line)
    _, message, _ = bot._offline_answer("國二行事曆")
    assert "國二行事曆" in message["text"] and "國一行事曆" not in message["text"]
    _, message, _ = bot._offline_answer("國三段考範圍")
    assert "第 1～3 章" in message["text"]


def test_renaming_a_calendar_keeps_it_current(folder):
    """在後台改行事曆標題（檔案改名）不會讓舊的行事曆變成「最新的」。"""
    jpg(folder / "行事曆" / "10月行事曆.jpg", "#a33")
    sync()
    db.execute("UPDATE content_files SET first_seen_at = now() - interval '20 days'")
    jpg(folder / "行事曆" / "11月行事曆.jpg", "#33a")
    sync()
    (folder / "行事曆" / "10月行事曆.jpg").rename(folder / "行事曆" / "十月行事曆.jpg")
    report = sync()
    assert report.calendar == ["11月行事曆（附圖）"] and report.unused_calendars == ["行事曆/十月行事曆"]


def test_guide_is_refreshed_when_the_program_updates(folder):
    (folder).mkdir(parents=True)
    (folder / "使用說明.txt").write_text("舊版的說明", encoding="utf-8")
    sync()
    assert "只給某個年級看" in (folder / "使用說明.txt").read_text("utf-8-sig")
