"""後台畫面（app/static/admin.html）在真的瀏覽器裡跑一遍：每一頁打得開、沒有 JavaScript 錯誤，
公告的「快速填寫」產生的標題和內容正確，發綁定碼的流程走得通。需要 Chromium，沒有就自動略過。"""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from app import db, main
from app.bot import Bot
from conftest import FakeLine, make_event
from test_admin_content import folder  # noqa: F401  （暫存的「補習班資料」資料夾）


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        chromium = pw.chromium.launch()
    except Exception as e:  # 沒裝 Chromium
        pw.stop()
        pytest.skip(f"沒有 Chromium，略過後台畫面測試：{e}")
    yield chromium
    chromium.close()
    pw.stop()


@pytest.fixture
def server(folder):  # noqa: F811
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)
    for sql in ("DELETE FROM binding_codes", "DELETE FROM enrollments", "DELETE FROM students", "DELETE FROM guardians",
                "DELETE FROM families"):
        db.execute(sql)


def test_every_page_opens_and_forms_work(browser, server):
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.route("https://fonts.googleapis.com/**", lambda route: route.abort())   # 測試不連外網
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/admin")
    page.fill("input[autocomplete=username]", "櫃台小美")
    page.fill("input[type=password]", "test-admin-password")
    page.click("button[type=submit]")
    page.wait_for_selector("nav")
    for name in ("home", "notices", "faq", "students", "courses", "honors", "broadcast", "messages", "test", "settings"):
        page.goto(f"{server}/admin#{name}")
        page.wait_for_selector("main h1")
        assert "載入失敗" not in page.inner_text("main"), name

    # 考試公告：填快速填寫的欄位 → 標題、內容、LINE 預覽自動產生 → 發布
    page.goto(server + "/admin#notices")
    page.click("button:has-text('新增')")
    page.click(".dialog button:has-text('考試公告')")
    page.click(".sheet [aria-label='適用年級'] button:has-text('國二')")
    page.fill(".helper [aria-label='科目']", "數學")
    page.fill(".helper input[type=date]", "2099-11-03")
    page.fill(".helper textarea", "第 1～3 章")
    assert page.input_value(".sheet input[maxlength='60']") == "國二數學段考範圍"
    preview = page.inner_text(".side-preview .chat")
    assert "【國二數學段考範圍】" in preview and "範圍：第 1～3 章" in preview and "（國二）" not in preview
    page.click(".sheet-foot .btn.primary")
    page.wait_for_selector(".sheet", state="detached")
    row = db.fetch_one("SELECT title, body, grades, category FROM announcements WHERE title = '國二數學段考範圍'")
    assert row["grades"] == [8] and row["category"] == "exam" and "考試日期：11/3（二）" in row["body"]

    # 新增學生 → 發綁定碼 → 對話框顯示的碼就是資料庫裡的碼
    page.goto(server + "/admin#students")
    page.click("button:has-text('新增學生')")
    page.fill(".sheet .card:first-child input >> nth=0", "測試生")
    page.click(".sheet [aria-label='年級'] button:has-text('國二')")
    page.fill(".sheet [aria-label='家長姓名']", "測試媽媽")
    page.click(".sheet-foot .btn.primary")
    page.wait_for_selector(".sheet h2:has-text('測試生')")
    page.click(".sheet button:has-text('發綁定碼')")
    code = page.inner_text(".dialog .bigcode")
    assert db.fetch_one("SELECT code FROM binding_codes WHERE used_at IS NULL")["code"] == code
    page.click(".dialog button:has-text('關閉')")

    # 家長在 LINE 傳了訊息 → 「直接綁定」列出這個 LINE（標出提到孩子名字）→ 綁定 → 碼作廢
    Bot(FakeLine()).handle_event(make_event(text="我是測試生的媽媽"))
    page.click(".sheet button:has-text('直接綁定')")
    page.wait_for_selector(".dialog .pick")
    first = page.inner_text(".dialog .pick >> nth=0")
    assert "測試家長" in first and "提到「測試生」" in first and "我是測試生的媽媽" in first
    page.once("dialog", lambda d: d.accept())                      # 「確定要綁定？」
    page.click(".dialog .pick >> nth=0 >> button:has-text('綁定')")
    page.wait_for_selector(".sheet .line-state .st-green")
    state = page.inner_text(".sheet .line-state")
    assert "已綁定 LINE" in state and "測試家長" in state
    assert not db.fetch_one("SELECT count(*) AS n FROM binding_codes WHERE used_at IS NULL AND expires_at > now()")["n"]
    assert errors == []
    page.close()
