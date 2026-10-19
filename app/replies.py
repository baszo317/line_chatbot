"""固定格式的回覆：圖文選單按鈕、歡迎詞、轉真人、綁定……

這些不經過 AI，所以又快、又免費、內容一定正確（直接從資料庫產生）。
改了這裡的文字，記得一起改 demo/web_demo_template.html（tests/test_web_demo.py 會逐字比對）。
"""
from __future__ import annotations

from itertools import groupby

from .config import settings
from .knowledge import fee_text, grade_tag
from .line_api import text_message

MAIN_MENU = ["課程介紹", "行事曆", "榜單", "交通資訊", "聯絡我們", "常見問題"]
HELP_MENU = ["找真人客服", "聯絡我們", "常見問題"]
RESUME_WORD = "回到AI助理"
CHILD_TIMETABLE = "孩子的課表"


def welcome(name: str | None) -> dict:
    hello = f"{name} 您好！" if name else "您好！"
    return text_message(
        f"{hello}歡迎加入{settings.school_name} 👋\n"
        "我是 AI 小助理，可以回答課程、上課時間、行事曆、收費、交通和榜單等問題。\n\n"
        "・點下方選單可以快速查詢\n"
        "・也可以直接打字問我，例如「國二數學什麼時候上課？」\n\n"
        "需要真人協助時，請點「找真人客服」。",
        MAIN_MENU,
    )


def greeting() -> dict:
    return text_message(
        f"您好！我是{settings.school_name}的 AI 小助理 😊\n"
        "可以點下方選單，或直接打字問我，例如「國二數學什麼時候上課？」",
        MAIN_MENU,
    )


def thanks() -> dict:
    return text_message("不客氣 😊 還有其他問題隨時問我！", MAIN_MENU)


def _tag(n: dict) -> str:
    """只適用某些年級的公告，標題後面註明年級，例：（國二）。標題已經寫了（「國二段考範圍」）就不重複。"""
    tag = grade_tag(n)
    return f"（{tag}）" if tag and tag not in n["title"] else ""


def _head(n: dict) -> str:
    return f"【{n['title']}】{_tag(n)}"


def _closure_lines(notices: list[dict]) -> list[str]:
    return [f"⚠️ 公告：{n['title']}{_tag(n)}" for n in notices if n["category"] == "closure"]


def courses(course_list: list[dict], notices: list[dict]) -> dict:
    if not course_list:
        return text_message("目前還沒有公開的課程資訊，請點「聯絡我們」洽詢櫃台。", HELP_MENU)
    lines = ["📚 課程與上課時間"]
    lines += _closure_lines(notices)
    ordered = sorted(course_list, key=lambda c: (c["grade"] or 99, c["name"]))
    for grade_label, group in groupby(ordered, key=lambda c: c["grade_label"]):
        lines.append(f"\n【{grade_label}】")
        for c in group:
            lines.append(f"・{c['name']}\n  {c['schedule']}\n  學費 {fee_text(c['fee'])}")
    lines.append("\n想知道更多，可以直接打字問我，例如「國二數學一週上幾次？」")
    return text_message("\n".join(lines))


def course_detail(course_list: list[dict]) -> dict:
    blocks = []
    for c in course_list:
        lines = [f"📚 {c['name']}", f"{c['schedule']}（一週 {c['times_per_week']} 次）",
                 f"教室 {c['room'] or '未定'}・學費 {fee_text(c['fee'])}"]
        if c["description"]:
            lines.append(c["description"])
        blocks.append("\n".join(lines))
    return text_message("\n\n".join(blocks) + "\n\n想報名或試聽，可以點「找真人客服」詢問櫃台。")


def no_course(label: str, others: list[dict]) -> dict:
    """問的年級／科目沒有開班（例：「國二理化」）。說清楚沒有，列出同年級現在有的班。"""
    text = f"目前沒有{label}的班 🙏"
    if others:
        text += "\n" + f"{others[0]['grade_label']}目前有：" + "、".join(c["name"] for c in others)
    text += "\n\n想開新班或有其他問題，可以點「找真人客服」詢問櫃台。"
    return text_message(text, ["課程介紹", "找真人客服"])


def no_senior_high() -> dict:
    return text_message("目前沒有高中的班 🙏 補習班現在只有國小（六年級）和國中的課程。\n"
                        "想確認的話，可以點「找真人客服」詢問櫃台。", ["課程介紹", "找真人客服"])


def closure(notices: list[dict]) -> dict:
    items = [n for n in notices if n["category"] == "closure"]
    body = "\n\n".join(f"{_head(n)}\n{n['body']}" for n in items)
    return text_message(f"⚠️ 最新公告\n\n{body}")


def notice_list(items: list[dict]) -> dict:
    """考試、活動、招生等一般公告；有附圖時 bot 會在後面接著傳圖片。"""
    body = "\n\n".join(f"{_head(n)}\n{n['body']}" for n in items)
    return text_message(f"📢 公告\n\n{body}")


def nothing(kind: str, grades: str = "", others: list[dict] | None = None, quick: list[str] | None = None) -> dict:
    """問的東西目前沒有資料（例：問國二考試，只有國一的公告）。說清楚沒有，不拿別的年級、別的主題來回答。
    kind：考試公告／活動公告／招生公告／停課公告／公告／行事曆；grades：問題裡的年級，例「國二」。"""
    lines = [f"目前沒有{grades}的{kind} 🙏" if grades else f"目前沒有{kind} 🙏"]
    if others:
        listed = "、".join(f"{grade_tag(n) or '全部年級'}的「{n['title']}」" for n in others[:5])
        lines.append(f"（現在有的是：{listed}）")
    lines.append("\n想確認的話，可以點「找真人客服」詢問櫃台。")
    return text_message("\n".join(lines), quick or HELP_MENU)


def calendar(notices: list[dict]) -> dict:
    """「補習班資料／行事曆」最新的那一份；有附圖時 bot 會在這則文字後面接著傳圖片。"""
    items = [n for n in notices if n["category"] == "calendar"]
    if not items:
        return text_message("📅 目前還沒有公布行事曆。\n想知道近期的活動或停課安排，可以點「找真人客服」詢問櫃台。", HELP_MENU)
    body = "\n\n".join(f"{_head(n)}\n{n['body']}" for n in items)
    return text_message(f"📅 行事曆\n\n{body}")


def honor(rows: list[dict]) -> dict:
    if not rows:
        return text_message("榜單整理中，請稍後再查詢 🙏")
    lines = []
    for year, group in groupby(rows, key=lambda r: r["school_year"]):
        lines.append(f"🏆 {year} 學年度榜單")
        for r in group:
            school = f"（{r['school_name']}）" if r["school_name"] else ""
            lines.append(f"・{r['display_name']}{school} {r['category']} {r['achievement']}")
    lines.append("\n恭喜同學們！想了解課程，請點「課程介紹」。")
    return text_message("\n".join(lines))


def kb_category(articles: list[dict], category: str, header: str, quick: list[str] | None = None) -> dict:
    items = [a for a in articles if a["category"] == category]
    if not items:
        return text_message(f"{header}\n\n這項資訊整理中，請點「找真人客服」詢問櫃台。", HELP_MENU)
    body = "\n\n".join(f"【{a['title']}】\n{a['content']}" for a in items)
    return text_message(f"{header}\n\n{body}", quick)


def contact(articles: list[dict]) -> dict:
    return kb_category(articles, "contact", "☎️ 聯絡我們", ["找真人客服", "交通資訊"])


def traffic(articles: list[dict]) -> dict:
    return kb_category(articles, "traffic", "🚌 交通資訊")


def faq_menu(articles: list[dict]) -> dict:
    titles = [a["title"] for a in articles]
    if not titles:
        return text_message("常見問題整理中，可以直接打字問我。")
    text = "❓ 常見問題\n點下方按鈕看答案，或直接打字問我："
    if len(titles) > 13:
        text += "\n" + "\n".join(f"・{t}" for t in titles[13:])
    return text_message(text, titles)


def kb_answer(article: dict) -> dict:
    return text_message(f"【{article['title']}】\n{article['content']}")


# ------------------------------------------------------------------ 家長綁定與孩子的資料
HOW_TO_BIND = ("綁定方式（擇一）：\n"
               "1. 點「找真人客服」，留下孩子的姓名，櫃台確認後直接幫您綁定\n"
               "2. 向櫃台索取 8 碼的綁定碼，在這裡輸入「綁定 綁定碼」，例如：綁定 AB12CD34")


def binding_not_ready() -> dict:
    """還沒綁定的家長問到孩子的資料。"""
    return text_message(
        "查詢孩子的課表、成績、到班、請假等「家長專屬功能」，要先綁定帳號 🙏\n\n"
        f"{HOW_TO_BIND}\n\n"
        "綁定後目前可以查孩子的課表，其他功能陸續開放。現在有需要，請點「找真人客服」。",
        ["家長綁定", "找真人客服"],
    )


def personal_not_ready() -> dict:
    """已綁定的家長問成績、到班、請假、餐費：還沒開放。"""
    return text_message(
        "成績、到班、請假、餐費等查詢功能還在準備中 🙏\n"
        "目前可以查「孩子的課表」；其他問題請點「找真人客服」，由櫃台老師協助。",
        [CHILD_TIMETABLE, "找真人客服"],
    )


def _children_line(children: list[dict]) -> str:
    return "、".join(f"{k['name']}（{k['grade_label']}）" for k in children) or "（還沒有在學的孩子資料）"


def binding_help(guardian: dict | None, children: list[dict]) -> dict:
    """按「家長綁定」：已綁定 → 顯示綁了誰；沒綁定 → 教怎麼綁。"""
    if guardian:
        return text_message(
            f"您已經完成綁定 ✅\n家長：{guardian['guardian_name']}\n孩子：{_children_line(children)}\n\n"
            "可以直接問我「孩子的課表」。要換綁其他 LINE，請洽櫃台。",
            [CHILD_TIMETABLE, "課程介紹"],
        )
    return text_message(
        f"👨‍👩‍👧 家長綁定\n綁定後可以在這裡查孩子的課表，其他功能陸續開放。\n\n{HOW_TO_BIND}",
        ["找真人客服", "聯絡我們"],
    )


BINDING_FAILED = {
    "invalid": "找不到這個綁定碼，請確認有沒有打錯（8 碼英文和數字，例如 AB12CD34）。",
    "used": "這個綁定碼已經用過了。要重新綁定，請向櫃台索取新的綁定碼。",
    "expired": "這個綁定碼已經過期了，請向櫃台索取新的綁定碼。",
    "line_already_bound": "這個 LINE 已經綁定了另一位家長的資料。需要更改，請洽櫃台。",
}


def binding_result(outcome: str, guardian: dict | None, children: list[dict]) -> dict:
    if outcome == "guardian" and guardian:
        return text_message(
            f"綁定完成 🎉\n{guardian['guardian_name']} 您好，已連結孩子：{_children_line(children)}\n\n"
            "現在可以直接問我「孩子的課表」，成績、到班等功能開放時會再通知您。",
            [CHILD_TIMETABLE, "課程介紹"],
        )
    if outcome in ("staff", "student"):
        return text_message("綁定完成 🎉 之後補習班的通知會傳到這個 LINE。", MAIN_MENU)
    return text_message(BINDING_FAILED.get(outcome, BINDING_FAILED["invalid"]), ["找真人客服", "聯絡我們"])


def child_timetable(children: list[dict]) -> dict:
    if not children:
        return text_message("目前查不到孩子的在學資料 🙏 請點「找真人客服」，請櫃台老師幫您確認。", HELP_MENU)
    blocks = []
    for k in children:
        lines = [f"👦 {k['name']}（{k['grade_label']}）"]
        for c in k["classes"]:
            room = f"（{c['room']}）" if c["room"] else ""
            trial = "［試聽］" if c["trial"] else ""
            lines.append(f"・{c['name']}{trial}：{c['schedule']}{room}")
        if not k["classes"]:
            lines.append("・目前沒有上課")
        blocks.append("\n".join(lines))
    return text_message("📚 孩子的課表\n\n" + "\n\n".join(blocks) + "\n\n要請假或調課，請點「找真人客服」。",
                        ["找真人客服", "行事曆"])


# ------------------------------------------------------------------ 真人客服
def handoff(minutes: int, contact_info: str) -> dict:
    text = (
        "好的，已轉給櫃台老師 🙋\n"
        f"接下來 {minutes} 分鐘內，您打的訊息會由專人回覆（AI 先暫停）。"
    )
    if contact_info:
        text += f"\n\n{contact_info}"
    text += f"\n\n想回到 AI 助理，請點「{RESUME_WORD}」。"
    return text_message(text, [RESUME_WORD])


def resume() -> dict:
    return text_message("已回到 AI 助理模式，有問題隨時問我！", MAIN_MENU)


def unknown() -> dict:
    return text_message(
        "這個問題我目前沒有資料可以回答 🙏\n可以點「找真人客服」，請櫃台老師協助您。",
        HELP_MENU,
    )


def non_text() -> dict:
    return text_message("目前我只看得懂文字訊息喔，請用打字的方式問我 😊", MAIN_MENU)


def ai_reply(text: str, needs_human: bool) -> dict:
    return text_message(text, HELP_MENU if needs_human else None)
