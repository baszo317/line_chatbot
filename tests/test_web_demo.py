"""網頁版展示（demo/web_demo_template.html）：產生得出來，而且回覆和 Python 版 bot 一字不差。

網頁版把 bot 的判斷和固定回覆用 JavaScript 重寫了一次。改了 app/replies.py 或 app/knowledge.py
卻忘了改網頁版時，比對測試會失敗。比對測試需要 Chromium（playwright install chromium），沒有就自動略過。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from app import db, knowledge, replies
from app.bot import Bot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))
import build_web_demo  # noqa: E402

QUESTIONS = [
    "我想看小孩的成績", "明天颱風會停課嗎？", "颱風天會停課嗎？", "國二數學一週上幾次？學費多少？",
    "國三理化衝刺班在哪間教室", "高一數學", "有哪些課", "上課時間", "去年有人考上建中嗎", "英文班什麼時候",
    "數學", "補習班地址在哪裡？", "補習班在哪裡？可以停車嗎？", "學費可以刷卡分期嗎？", "可以刷卡嗎",
    "可以帶寵物來上課嗎", "電話幾號", "怎麼停車", "請假要怎麼補課", "我要請假", "退費怎麼算", "你好",
    "試聽", "晚上可以在補習班自習嗎", "下課可以留下來寫作業嗎？", "幫我家小孩請假", "收費方式",
    "營業時間", "捷運怎麼去", "多少錢", "國一", "理化", "我想報名國一數學班",
    # 答不出來就說沒有，不拿別的資料充數
    "國二有考試嗎", "國二考試", "國二段考範圍", "國二這個月有考試嗎", "國二行事曆", "國二有獎學金嗎", "數學老師是誰",
    "國二理化", "國二數學呢", "國中數學", "英語", "物理", "國二明天停課嗎", "寒假有開班嗎", "最近有什麼活動",
    "我家小孩上什麼課", "謝謝", "好的謝謝！", "請問", "ＯＫ", "國二數學Ａ班幾點",
]

JS_ANSWERS = """(questions) => {
  const out = { offline: {}, commands: {}, fixed: {} };
  for (const q of questions) {
    const o = offlineAnswer(q);
    out.offline[q] = { intent: o.intent, needsHuman: o.needsHuman, ...o.message };
  }
  for (const [k, [, build]] of Object.entries(COMMANDS)) out.commands[k] = build();
  out.fixed.welcome = R.welcome();
  out.fixed.handoff = R.handoff(30, contactText());
  out.fixed.resume = R.resume();
  out.fixed.unknown = R.unknown();
  out.fixed.greeting = R.greeting();
  out.fixed.thanks = R.thanks();
  out.fixed.binding = R.bindingHelp();
  out.fixed.bindingNotReady = R.bindingNotReady();
  for (const a of DATA.articles) out.fixed["kb:" + a.title] = R.kbAnswer(a);
  out.knowledge = knowledgeText();
  return out;
}"""


def _msg(m: dict) -> dict:
    return {"text": m["text"], "quick": [i["action"]["text"] for i in (m.get("quickReply") or {}).get("items", [])]}


def _python_answers(line) -> dict:
    bot = Bot(line)
    out: dict = {"offline": {}, "commands": {}, "fixed": {}}
    for q in QUESTIONS:
        intent, message, needs_human = bot._offline_answer(q)
        out["offline"][q] = {"intent": intent, "needsHuman": needs_human, **_msg(message)}
    for key, (_, build) in bot.commands.items():
        out["commands"][key] = _msg(build())
    out["fixed"]["welcome"] = _msg(replies.welcome(None))   # 網頁版沒有 LINE 名稱
    out["fixed"]["handoff"] = _msg(replies.handoff(30, knowledge.contact_text()))
    out["fixed"]["resume"] = _msg(replies.resume())
    out["fixed"]["unknown"] = _msg(replies.unknown())
    out["fixed"]["greeting"] = _msg(replies.greeting())
    out["fixed"]["thanks"] = _msg(replies.thanks())
    out["fixed"]["binding"] = _msg(replies.binding_help(None, []))
    out["fixed"]["bindingNotReady"] = _msg(replies.binding_not_ready())
    for a in knowledge.articles():
        out["fixed"]["kb:" + a["title"]] = _msg(replies.kb_answer(a))
    out["knowledge"] = knowledge.knowledge_text()
    return out


def _embedded_data(html: str) -> dict:
    return json.loads(re.search(r"const DATA = (.*?);\nconst MENU_SRC", html, re.S).group(1))


def test_build_embeds_data_and_menu(tmp_path):
    html = build_web_demo.build(tmp_path / "web_demo.html").read_text("utf-8")
    assert "__DATA__" not in html and "__MENU_B64__" not in html
    assert 'const MENU_SRC = "data:image/png;base64,iVBOR' in html
    data = _embedded_data(html)
    assert [c["name"] for c in data["courses"]] == [c["name"] for c in knowledge.courses()]
    assert len(data["articles"]) == len(knowledge.articles())
    assert data["rules"].startswith("你是「高明補習班」LINE 官方帳號的 AI 小助理")


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        chromium = pw.chromium.launch()
    except Exception as e:  # 沒裝 Chromium
        pw.stop()
        pytest.skip(f"沒有 Chromium，略過網頁版比對：{e}")
    yield chromium
    chromium.close()
    pw.stop()


def test_web_demo_replies_match_python_bot(browser, line, tmp_path):
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(build_web_demo.build(tmp_path / "web_demo.html").as_uri())

    # 1. 沒有公告
    page.evaluate("state.closure = false")
    assert page.evaluate(JS_ANSWERS, QUESTIONS) == _python_answers(line)

    # 2. 有颱風停課公告：把網頁版產生的示範公告寫進資料庫，兩邊用同一則公告比對
    (notice,) = page.evaluate("(state.closure = true, notices())")
    db.execute("""INSERT INTO announcements (title, body, category, starts_at, ends_at)
                  VALUES (%s, %s, 'closure', now() - interval '1 minute', now() + interval '1 day')""",
               (notice["title"], notice["body"]))
    knowledge.invalidate()
    assert page.evaluate(JS_ANSWERS, QUESTIONS) == _python_answers(line)
    assert errors == []
    page.close()
