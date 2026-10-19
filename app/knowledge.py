"""把資料庫裡的公開資料整理成 AI 讀得懂的文字，以及沒有 AI 時用的關鍵字比對工具。

資料每 60 秒重新讀一次：櫃台改了知識庫，一分鐘內 bot 就會用新內容回答
（「補習班資料」資料夾同步完、後台存檔後會立刻清掉快取，不用等一分鐘）。
有附圖的常見問題或公告，在給 AI 的資料裡標成「【標題】（附圖）」；回覆用到時 bot 會把圖片一起傳出去。

關鍵字比對的「判斷順序」在 app/bot.py 的 Bot._offline_decide()；這裡放的是它用到的小工具：
認年級、認科目、認問題類型、找公告和常見問題。
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict

from . import db
from .config import settings

WEEKDAYS = "一二三四五六日"
CACHE_SECONDS = 60
_cache: dict = {"at": float("-inf")}


# ------------------------------------------------------------------ 整理資料
def _hhmm(t) -> str:
    return t.strftime("%H:%M")


def course_summaries(rows: list[dict]) -> list[dict]:
    """v_public_timetable 是一個時段一列；合併成一個班一筆，時段寫成「週一、週三 18:30–20:30」。"""
    courses: OrderedDict[int, dict] = OrderedDict()
    for r in rows:
        c = courses.setdefault(r["course_id"], {
            "name": r["course_name"],
            "subject": r["subject"],
            "grade": r["grade"],
            "grade_label": r["grade_label"] or "不分年級",
            "fee": r["fee"],
            "room": r["room"],
            "description": r["description"],
            "_slots": OrderedDict(),
        })
        span = f"{_hhmm(r['start_time'])}–{_hhmm(r['end_time'])}"
        c["_slots"].setdefault(span, []).append(WEEKDAYS[r["weekday"] - 1])
    result = []
    for c in courses.values():
        slots = c.pop("_slots")
        c["schedule"] = "；".join("週" + "、週".join(days) + f" {span}" for span, days in slots.items())
        c["times_per_week"] = sum(len(days) for days in slots.values())
        result.append(c)
    return result


def _pic(item: dict) -> str:
    return "（附圖）" if item.get("image_token") else ""


# ------------------------------------------------------------------ 年級
GRADE_LABEL = {6: "國小", 7: "國一", 8: "國二", 9: "國三"}
JUNIOR = (7, 8, 9)


def grades_text(grades) -> str:
    """{8} →「國二」；{7, 8, 9} →「國中」；{6, 9} →「國小、國三」；空的 → 空字串（＝全部年級）。"""
    grades = sorted(set(grades or []))
    if not grades or grades == [6, 7, 8, 9]:
        return ""
    if grades == list(JUNIOR):
        return "國中"
    return "、".join(GRADE_LABEL.get(g, str(g)) for g in grades)


def grade_tag(item: dict) -> str:
    """公告的適用年級，例：「國二」「國小、國三」「國中」；全部年級回傳空字串。"""
    return grades_text(item.get("grades"))


def _for_line(item: dict) -> str:
    tag = grade_tag(item)
    return f"適用年級：{tag}（其他年級不適用）\n" if tag else ""


def fee_text(fee: int | None) -> str:
    return f"{fee:,} 元／{settings.fee_unit}" if fee else "請洽櫃台"


def _render(articles, notices, courses, honors) -> str:
    parts = ["〈補習班資料〉以下是目前所有公開資訊，回答只能根據這些內容。"]

    parts.append("## 常見問題與規定")
    parts += [f"【{a['title']}】{_pic(a)}\n{a['content']}" for a in articles] or ["（尚無資料）"]

    parts.append("## 目前有效的公告")
    parts += [f"【{n['title']}】{_pic(n)}\n{_for_line(n)}{n['body']}" for n in notices] or ["（目前沒有公告）"]

    parts.append("## 公開課表")
    lines = []
    for c in courses:
        desc = f"；{c['description']}" if c["description"] else ""
        lines.append(
            f"・{c['name']}（{c['grade_label']}・{c['subject']}）：{c['schedule']}，"
            f"一週 {c['times_per_week']} 次，教室 {c['room'] or '未定'}，學費 {fee_text(c['fee'])}{desc}"
        )
    parts.append("\n".join(lines) or "（尚無公開課程）")

    parts.append("## 榜單")
    honor_lines = [
        f"・{h['school_year']} 學年度 {h['category']}：{h['display_name']}（{h['school_name'] or '學校未填'}）{h['achievement']}"
        for h in honors
    ]
    parts.append("\n".join(honor_lines) or "（尚無公開榜單）")
    return "\n\n".join(parts)


def _load() -> dict:
    now = time.monotonic()
    if now - _cache["at"] < CACHE_SECONDS:
        return _cache
    articles = db.kb_articles()
    notices = db.active_announcements()
    courses = course_summaries(db.public_timetable())
    honors = db.honor_roll()
    subjects = db.subject_names()
    _cache.update(at=now, articles=articles, notices=notices, courses=courses, honors=honors, subjects=subjects,
                  text=_render(articles, notices, courses, honors))
    return _cache


def invalidate() -> None:
    _cache["at"] = float("-inf")


def knowledge_text() -> str:
    return _load()["text"]


def articles() -> list[dict]:
    return _load()["articles"]


def notices() -> list[dict]:
    return _load()["notices"]


def courses() -> list[dict]:
    return _load()["courses"]


def honors() -> list[dict]:
    return _load()["honors"]


def subjects() -> list[str]:
    """科目名稱（資料庫 subjects 表，加上班級用到的科目）。"""
    data = _load()
    names = list(data["subjects"])
    names += [c["subject"] for c in data["courses"] if c["subject"] not in names]
    return names


def article_by_title(title: str) -> dict | None:
    return next((a for a in articles() if a["title"] == title), None)


def contact_text() -> str:
    a = next((a for a in articles() if a["category"] == "contact"), None)
    return a["content"] if a else ""


# ------------------------------------------------------------------ 附圖（回傳圖片代號，最多 4 張）
MAX_IMAGES = 4   # LINE 一次回覆最多 5 則訊息：1 則文字 + 4 張圖


def _tokens(items) -> list[str]:
    seen: list[str] = []
    for item in items:
        token = item.get("image_token")
        if token and token not in seen:
            seen.append(token)
    return seen[:MAX_IMAGES]


def article_images(article: dict) -> list[str]:
    return _tokens([article])


def category_images(category: str) -> list[str]:
    return _tokens(a for a in articles() if a["category"] == category)


def calendar_notices() -> list[dict]:
    return [n for n in notices() if n["category"] == "calendar"]


def calendar_images() -> list[str]:
    return _tokens(calendar_notices())


def closure_notices() -> list[dict]:
    return [n for n in notices() if n["category"] == "closure"]


def closure_images() -> list[str]:
    return _tokens(closure_notices())


def images_for_titles(titles: list[str]) -> list[str]:
    """AI 回覆時列出用到的資料標題；其中有附圖的，把圖片代號找出來。"""
    wanted = {t.strip().strip("【】") for t in titles}
    return _tokens(item for item in [*notices(), *articles()] if item["title"] in wanted)


# ====================================================================== 沒有 AI 時：關鍵字比對的工具
def normalize_question(text: str) -> str:
    """「甚麼」「什么」都當成「什麼」；全形英數字轉半形（「Ａ班」＝「A班」）。"""
    text = text.translate(_FULL_WIDTH)
    return text.replace("甚麼", "什麼").replace("甚么", "什麼").replace("什么", "什麼")


_FULL_WIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


# ---- 年級：問題裡提到哪幾個年級
GRADE_WORDS = {
    6: re.compile(r"國小|小六|六年級"),
    7: re.compile(r"國一|七年級|國中一年級"),
    8: re.compile(r"國二|八年級|國中二年級"),
    9: re.compile(r"國三|九年級|國中三年級"),
}
JUNIOR_HIGH = re.compile(r"國中")
SENIOR_HIGH = re.compile(r"高[一二三]|高中|十[一二]?年級")
_ANY_GRADE_WORD = re.compile("|".join([*(p.pattern for p in GRADE_WORDS.values()), JUNIOR_HIGH.pattern]))


def grade_set(text: str) -> set[int]:
    """問題裡提到的年級，例：「國二段考」→ {8}；只說「國中」＝ {7, 8, 9}；沒提到 → 空集合。"""
    found = {g for g, pattern in GRADE_WORDS.items() if pattern.search(text)}
    if not found and JUNIOR_HIGH.search(text):
        found = set(JUNIOR)
    return found


def for_grades(items: list[dict], grades: set[int]) -> list[dict]:
    """家長有提到年級時，只留下全部年級適用、或適用那個年級的資料（公告、行事曆）。"""
    if not grades:
        return items
    return [i for i in items if not i.get("grades") or grades & set(i["grades"])]


# ---- 科目：問題裡提到哪幾科（含常見的別名）
SUBJECT_ALIASES = {"英語": "英文", "國語": "國文", "物理": "理化", "化學": "理化", "數理": "數學", "社會科": "社會"}


def subject_set(text: str) -> set[str]:
    names = subjects()
    found = {s for s in names if s and s in text}
    found |= {real for alias, real in SUBJECT_ALIASES.items() if alias in text and real in names}
    return found


# ---- 認問題類型
PERSONAL = re.compile(r"成績|分數|考幾分|排名|錯題|到班了|有沒有到|出缺勤|缺席紀錄|餐費|餘額|幫.{0,8}請假|要請假|"
                      r"請個假|繳了沒|繳費紀錄|聯絡簿")
CHILD = re.compile(r"孩子|小孩|女兒|兒子|小朋友|我家")
CHILD_CLASSES = re.compile(r"課表|上什麼課|上哪些課|什麼課|哪些課|哪幾堂|幾點上課|上課時間|幾點下課|哪幾天")
GREETING = re.compile(r"^(你好|您好|哈囉|哈啰|嗨|安安|早安|午安|晚安|老師好|老師您好|hi|hello|hey|在嗎|有人在嗎|請問)"
                      r"[\s!！~～。.,，?？]*$", re.IGNORECASE)
THANKS = re.compile(r"^(謝謝|感謝|謝啦|多謝|thanks?|thank you|thx|好的|好喔|好哦|好唷|ok|okay|收到|了解|瞭解|知道了)"
                    r"[\s!！~～。.,，]*(老師|你|您)?[\s!！~～。.,，謝]*$", re.IGNORECASE)
CLOSURE_WORDS = re.compile(r"停課|颱風|放假|停班|有沒有上課|有上課嗎|照常上課|要上課嗎|會上課嗎")
CALENDAR_WORDS = re.compile(r"行事曆|行程|月曆")
CALENDAR_TIME = re.compile(r"(這個月|本月|下個月|下月|這週|下週|這禮拜|下禮拜|這星期|下星期|最近|近期|這學期).{0,6}"
                           r"(安排|考試|上課|活動|行程|事)|\d{1,2}\s*[/月]\s*\d{1,2}")
HONOR = re.compile(r"榜單|考上|錄取|上榜|金榜")
# 公告的分類 → 問到這類公告會用的字
NOTICE_KINDS = {
    "exam": re.compile(r"考試|段考|模擬考|小考|月考|期中考|期末考|複習考|考什麼|考哪|範圍"),
    "event": re.compile(r"活動|講座|說明會|家長會|家長日|典禮|比賽|營隊|聚會"),
    "enrollment": re.compile(r"招生|寒假|暑假|寒輔|暑輔|先修|新班"),
}
NOTICE_ANY = re.compile(r"公告|最新消息|消息|通知")
KIND_LABEL = {"exam": "考試公告", "event": "活動公告", "enrollment": "招生公告", "closure": "停課公告",
              "general": "公告", "calendar": "行事曆"}
# 有提到年級或科目時，要像這樣在問課程，才回班級資訊（「國二有獎學金嗎」不算）
COURSE_ASK = re.compile(r"課|班|幾點|時間|時段|星期|禮拜|週|周|幾次|學費|多少錢|費用|價格|價錢|教室|報名|試聽|"
                        r"什麼時候|何時|多久|上幾")
POLICY_WORDS = re.compile(r"補課|請假|缺課|退費|退錢|轉班|休學|停課|颱風|放假|停班")
# 沒提到年級、科目，但在問整份課表
COURSE_LIST = re.compile(r"課表|上課時間|幾點上課|什麼時候上課|開課|開班|有哪些課|有什麼課|哪些班|什麼班|班級|課程")
TOPICS = {
    "traffic": re.compile(r"地址|在哪|位置|交通|停車|公車|捷運|怎麼去|怎麼走|接送"),
    "contact": re.compile(r"電話|聯絡|客服|服務時間|營業時間"),
    "fee": re.compile(r"學費|收費|費用|多少錢|價格|繳費"),
}
# 「只有年級／科目／班名」的問題，拿掉這些字之後應該什麼都不剩（例：「國二」「國二數學呢」）
_FILLER = re.compile(r"請問|想問|問一下|我想|想要|想知道|知道|一下|你們|您們|貴班|補習班|這邊|那邊|有沒有|沒有|有|嗎|呢|吧|啊|喔|哦|耶|"
                     r"的|是|在|了|現在|目前|請|問|想|要|可以|資訊|資料|介紹|相關|看看|看|查詢|查|小孩|孩子|我家|我的|女兒|兒子|"
                     r"那|這|什麼|哪些|哪個|哪|我")


def is_bare(question: str, extra_words=()) -> bool:
    """拿掉年級、科目、班名和「請問、呢、的」這類字之後，幾乎什麼都不剩。"""
    q = _norm(question)
    for word in sorted(extra_words, key=len, reverse=True):
        q = q.replace(_norm(word), "")
    q = _ANY_GRADE_WORD.sub("", q)
    q = _FILLER.sub("", q)
    return len(q) <= 1


# ---- 常見問題
_STOP_BIGRAMS = {"補習", "習班", "請問", "可以", "什麼", "一下", "我想", "想要", "老師", "小孩",
                 "孩子", "時候", "多少", "有沒", "沒有", "你們", "我們", "這個", "那個"}


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} - _STOP_BIGRAMS


def search_articles(question: str) -> list[tuple[int, dict]]:
    """關鍵字命中一個 +3 分，標題和問題有相同的兩字詞 +1 分；2 分以上才算找到。"""
    q, qb = _norm(question), _bigrams(question)
    scored = []
    for a in articles():
        score = sum(3 for kw in (a["keywords"] or []) if kw and _norm(kw) in q)
        score += len(_bigrams(a["title"]) & qb)
        if score >= 2:
            scored.append((score, a))
    scored.sort(key=lambda x: -x[0])
    return scored


# ---- 公告（考試、活動、招生、一般；停課和行事曆另外處理）
def _strip_grades_subjects(text: str) -> str:
    text = _ANY_GRADE_WORD.sub(" ", text)
    for s in subjects():
        text = text.replace(s, " ")
    return text


def notice_kinds(question: str) -> set[str]:
    return {kind for kind, pattern in NOTICE_KINDS.items() if pattern.search(question)}


def search_notices(question: str, kinds: set[str]) -> list[dict]:
    """問到的分類（kinds）的公告，加上標題和問題有 2 個以上相同兩字詞的公告（年級、科目不算，
    免得「國二數學」就把「國二數學段考範圍」找出來）。問「公告」「最新消息」時全部列出。還沒依年級篩選。"""
    everything = bool(NOTICE_ANY.search(question))
    qb = _bigrams(_strip_grades_subjects(question))
    scored = []
    for n in notices():
        if n["category"] in ("closure", "calendar"):
            continue
        score = len(_bigrams(_strip_grades_subjects(n["title"])) & qb)
        if n["category"] in kinds:
            score += 2
        if score >= 2 or everything:
            scored.append((score, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored]


# ---- 班級
def courses_named(question: str) -> list[dict]:
    """問題裡寫了完整班名（「國三理化衝刺班」「國二數學A班」）。"""
    q = _norm(question)
    return [c for c in courses() if _norm(c["name"]) in q]


def courses_for(grades: set[int], subjects_: set[str]) -> list[dict]:
    return [c for c in courses()
            if (not grades or c["grade"] in grades) and (not subjects_ or c["subject"] in subjects_)]
