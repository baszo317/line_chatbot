"""後台 API：登入保護、學生／家長、班級名單、上課時段。"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import admin, bot_settings, db, knowledge, main
from app.bot import Bot
from conftest import ROOT, USER, FakeLine, make_event

DEFAULT_SUBJECTS = ("國文", "英文", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民", "作文")


@pytest.fixture
def client():
    c = TestClient(main.app)
    yield c
    for sql in ("DELETE FROM binding_codes", "DELETE FROM enrollments", "DELETE FROM students", "DELETE FROM guardians",
                "DELETE FROM families", "DELETE FROM admin_audit", "DELETE FROM courses", "DELETE FROM bot_settings"):
        db.execute(sql)
    db.execute("DELETE FROM subjects WHERE NOT (name = ANY(%s))", (list(DEFAULT_SUBJECTS),))
    db.execute((ROOT / "sql" / "phase1_seed.sql").read_text("utf-8"))
    knowledge.invalidate()


@pytest.fixture
def staff(client):
    r = client.post("/admin/api/login", json={"name": "櫃台小美", "password": "test-admin-password"})
    assert r.status_code == 200
    return client


def course_id(name: str) -> int:
    return db.fetch_one("SELECT id FROM courses WHERE name = %s", (name,))["id"]


def new_student(staff, name="王小明", grade=8, courses=(), **extra) -> int:
    body = {"student": {"name": name, "grade": grade, "school_name": "○○國中"},
            "guardian": {"name": "王媽媽", "relationship": "母", "phone": "0912-345-678"},
            "courses": [{"course_id": c} for c in courses], **extra}
    r = staff.post("/admin/api/students", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ------------------------------------------------------------------ 進入後台的限制
def test_page_needs_password_setting(client, monkeypatch):
    monkeypatch.setattr(admin, "settings", replace(admin.settings, admin_password=""))
    r = client.get("/admin")
    assert r.status_code == 503 and "BOT_ADMIN_PASSWORD" in r.text


def test_page_is_blocked_through_tunnel(client):
    assert client.get("/admin").status_code == 200
    r = client.get("/admin", headers={"x-forwarded-for": "1.2.3.4", "cf-connecting-ip": "1.2.3.4"})
    assert r.status_code == 403 and "localhost:8000/admin" in r.text
    r = client.post("/admin/api/login", json={"name": "x", "password": "test-admin-password"},
                    headers={"x-forwarded-for": "1.2.3.4"})
    assert r.status_code == 403


def test_login_required_and_wrong_password(client):
    assert client.get("/admin/api/students").status_code == 401
    assert client.post("/admin/api/login", json={"name": "x", "password": "nope"}).status_code == 401
    assert client.post("/admin/api/login", json={"name": "小美", "password": "test-admin-password"}).status_code == 200
    assert client.get("/admin/api/me").json() == {"name": "小美"}
    client.post("/admin/api/logout")
    assert client.get("/admin/api/me").status_code == 401


def test_forged_cookie_is_rejected(client):
    client.cookies.set("gaoming_admin", "eyJ4Ijox.fake", path="/admin")
    assert client.get("/admin/api/me").status_code == 401


# ------------------------------------------------------------------ 學生
def test_new_student_joins_grade8_class(staff):
    """「多一個人要進國二班」：新增學生＋家長，同時加入國二數學 A 班。"""
    sid = new_student(staff, courses=[course_id("國二數學 A 班")])
    s = staff.get(f"/admin/api/students/{sid}").json()
    assert s["grade_label"] == "國二" and s["student_no"].startswith("G115")
    assert s["family"]["family_name"] == "王小明家" and s["family"]["phone"] == "0912-345-678"
    assert [g["name"] for g in s["guardians"]] == ["王媽媽"]
    assert [e["course_name"] for e in s["enrollments"]] == ["國二數學 A 班"]

    roster = staff.get(f"/admin/api/courses/{course_id('國二數學 A 班')}").json()["roster"]
    assert [(r["name"], r["status"], r["guardian"]) for r in roster] == [("王小明", "active", "王媽媽 0912-345-678")]
    listed = staff.get("/admin/api/students", params={"grade": 8}).json()
    assert [x["name"] for x in listed] == ["王小明"] and listed[0]["courses"][0]["name"] == "國二數學 A 班"
    assert staff.get("/admin/api/students", params={"q": "0912"}).json()[0]["id"] == sid   # 用家長電話也找得到

    audit = staff.get("/admin/api/audit").json()
    assert {a["summary"] for a in audit} == {"新增學生 王小明（國二）", "王小明 加入 國二數學 A 班"}
    assert {a["actor"] for a in audit} == {"櫃台小美"}


def test_sibling_joins_existing_family(staff):
    sid = new_student(staff)
    family_id = staff.get(f"/admin/api/students/{sid}").json()["family_id"]
    assert staff.get("/admin/api/families", params={"q": "王媽媽"}).json()[0]["id"] == family_id
    r = staff.post("/admin/api/students", json={"student": {"name": "王小美", "grade": 6}, "family_id": family_id})
    sister = staff.get(f"/admin/api/students/{r.json()['id']}").json()
    assert sister["grade_label"] == "國小" and [s["name"] for s in sister["siblings"]] == ["王小明"]
    assert db.fetch_one("SELECT count(*) AS n FROM families")["n"] == 1


def test_grade_must_be_elementary6_or_junior_high(staff):
    r = staff.post("/admin/api/students", json={"student": {"name": "高一生", "grade": 10}})
    assert r.status_code == 422
    r = staff.post("/admin/api/students", json={"student": {"name": "", "grade": 7}})
    assert r.status_code == 400 and "姓名" in r.json()["detail"]


def test_duplicate_enrollment_and_student_no(staff):
    cid = course_id("國二英文 A 班")
    sid = new_student(staff, courses=[cid])
    r = staff.post(f"/admin/api/courses/{cid}/enroll", json={"student_id": sid})
    assert r.status_code == 400 and r.json()["detail"] == "這位學生已經在這個班上了"
    no = staff.get(f"/admin/api/students/{sid}").json()["student_no"]
    r = staff.post("/admin/api/students", json={"student": {"name": "撞號", "grade": 7, "student_no": no}})
    assert r.status_code == 400 and "學號重複" in r.json()["detail"]


def test_edit_student_and_leave(staff):
    cid = course_id("國一數學班")
    sid = new_student(staff, grade=7, courses=[cid])
    assert staff.patch(f"/admin/api/students/{sid}", json={"grade": 8, "school_name": "△△國中"}).status_code == 200
    s = staff.get(f"/admin/api/students/{sid}").json()
    assert (s["grade_label"], s["school_name"]) == ("國二", "△△國中")
    assert db.fetch_one("SELECT grade_school_year = school_year_of(tw_today()) AS ok FROM students")["ok"]

    r = staff.patch(f"/admin/api/students/{sid}", json={"status": "left"})
    assert r.json()["dropped"] == 1                          # 離班時自動退掉所有班
    s = staff.get(f"/admin/api/students/{sid}").json()
    assert s["left_on"] and s["courses"] == [] and s["enrollments"][0]["status"] == "dropped"
    assert staff.get("/admin/api/students").json() == []                         # 預設只列在學
    assert len(staff.get("/admin/api/students", params={"status": "all"}).json()) == 1


def test_guardians(staff):
    sid = new_student(staff)
    fam = staff.get(f"/admin/api/students/{sid}").json()["family_id"]
    gid = staff.post(f"/admin/api/families/{fam}/guardians",
                     json={"name": "王爸爸", "relationship": "父", "phone": "0922"}).json()["id"]
    staff.patch(f"/admin/api/guardians/{gid}", json={"is_primary": True})
    s = staff.get(f"/admin/api/students/{sid}").json()
    assert [g["name"] for g in s["guardians"]] == ["王爸爸", "王媽媽"]          # 主要聯絡人在前
    assert staff.delete(f"/admin/api/guardians/{gid}").json()["how"] == "刪除"
    staff.patch(f"/admin/api/families/{fam}", json={"address": "○○路 1 號"})
    assert staff.get(f"/admin/api/students/{sid}").json()["family"]["address"] == "○○路 1 號"


# ------------------------------------------------------------------ 班級
def test_course_with_timeslots_shows_up_for_parents(staff):
    math = db.fetch_one("SELECT id FROM subjects WHERE name = '數學'")["id"]
    r = staff.post("/admin/api/courses", json={
        "name": "國二數學 B 班", "subject_id": math, "grade": 8, "fee": 4800,
        "schedules": [{"weekday": 2, "start_time": "19:00", "end_time": "21:00", "room": "203"}]})
    cid = r.json()["id"]
    reply = staff.post("/admin/api/preview", json={"text": "課程介紹"}).json()
    assert "國二數學 B 班\n  週二 19:00–21:00" in reply["messages"][0]["text"]        # 不用等，機器人馬上知道

    staff.patch(f"/admin/api/courses/{cid}", json={"schedules": [
        {"weekday": 2, "start_time": "19:00", "end_time": "21:00", "room": "203"},
        {"weekday": 4, "start_time": "19:00", "end_time": "21:00", "room": "203"}]})
    c = staff.get(f"/admin/api/courses/{cid}").json()
    assert [(s["weekday"], s["start_time"]) for s in c["schedules"]] == [(2, "19:00"), (4, "19:00")]

    bad = staff.patch(f"/admin/api/courses/{cid}", json={"schedules": [
        {"weekday": 1, "start_time": "21:00", "end_time": "19:00"}]})
    assert bad.status_code == 422
    assert staff.patch(f"/admin/api/courses/{cid}", json={"grade": 10}).status_code == 422


def test_trial_then_regular_then_drop(staff):
    cid = course_id("國三理化衝刺班")
    sid = new_student(staff, name="陳同學", grade=9)
    staff.post(f"/admin/api/courses/{cid}/enroll", json={"student_id": sid, "status": "trial"})
    (row,) = staff.get(f"/admin/api/courses/{cid}").json()["roster"]
    assert row["status"] == "trial"
    staff.patch(f"/admin/api/enrollments/{row['enrollment_id']}", json={"status": "active"})
    staff.patch(f"/admin/api/enrollments/{row['enrollment_id']}", json={"status": "dropped"})
    c = staff.get(f"/admin/api/courses/{cid}").json()
    assert c["roster"] == [] and c["used"] is True
    assert staff.delete(f"/admin/api/courses/{cid}").status_code == 400            # 有過學生的班不能刪


def test_remove_demo_courses(staff):
    sid = new_student(staff, courses=[course_id("國二數學 A 班")])
    status = staff.get("/admin/api/status").json()
    assert any(w.get("action") == "remove_demo" for w in status["warnings"])
    r = staff.post("/admin/api/courses/remove-demo").json()
    assert r["archived"] == ["國二數學 A 班"] and len(r["removed"]) == 5
    assert not any(w.get("action") == "remove_demo" for w in staff.get("/admin/api/status").json()["warnings"])
    assert "## 公開課表\n\n（尚無公開課程）" in knowledge.knowledge_text()       # 家長看不到假的班級了
    assert staff.get(f"/admin/api/students/{sid}").json()["courses"][0]["name"] == "國二數學 A 班"


def test_status_and_preview(staff):
    s = staff.get("/admin/api/status").json()
    assert s["db"] == {"ok": True, "schema_ok": True}
    assert s["ai"]["mode"] == "offline" and "繁體中文" in s["ai"]["rules"]
    assert s["counts"]["courses"] == 6
    p = staff.post("/admin/api/preview", json={"text": "我想看小孩的成績"}).json()
    assert p["handled_by"] == "rule" and p["intent"] == "personal" and "家長專屬功能" in p["messages"][0]["text"]
    assert "國二數學 A 班" in staff.get("/admin/api/knowledge").json()["text"]


def test_new_subject(staff):
    r = staff.post("/admin/api/subjects", json={"name": "國語"})
    assert r.json()["name"] == "國語"
    assert staff.post("/admin/api/subjects", json={"name": "國語"}).json()["detail"] == "已經有這個科目了"


# ------------------------------------------------------------------ 家長 LINE 綁定
def test_parent_binds_line_with_code_and_sees_child_timetable(staff, line):
    """後台發綁定碼 → 家長在 LINE 輸入「綁定 XXXXXXXX」→ 可以查孩子的課表；後台可以解除綁定。"""
    sid = new_student(staff, courses=[course_id("國二數學 A 班")])
    guardian = staff.get(f"/admin/api/students/{sid}").json()["all_guardians"][0]
    r = staff.post(f"/admin/api/guardians/{guardian['id']}/binding-code").json()
    code = r["code"]
    assert len(code) == 8 and f"綁定 {code}" in r["message"]
    again = staff.post(f"/admin/api/guardians/{guardian['id']}/binding-code").json()["code"]   # 重發：舊的作廢
    s = staff.get(f"/admin/api/students/{sid}").json()
    assert s["all_guardians"][0]["pending_code"] == again and not s["all_guardians"][0]["line"]

    bot = Bot(line)
    bot.handle_event(make_event(text="孩子的課表"))
    assert "要先綁定帳號" in line.texts()[-1]
    bot.handle_event(make_event(text=f"綁定 {code}"))                     # 作廢的碼
    assert "過期" in line.texts()[-1]
    bot.handle_event(make_event(text=f"綁定碼：{again.lower()}"))
    assert line.texts()[-1].startswith("綁定完成") and "王小明（國二）" in line.texts()[-1]
    bot.handle_event(make_event(text="我家小孩上什麼課"))
    assert "國二數學 A 班：週一、週三 18:30–20:30（201）" in line.texts()[-1]
    bot.handle_event(make_event(text="我想看小孩的成績"))
    assert "還在準備中" in line.texts()[-1]
    bot.handle_event(make_event(text="家長綁定"))
    assert line.texts()[-1].startswith("您已經完成綁定")

    s = staff.get(f"/admin/api/students/{sid}").json()
    g = s["all_guardians"][0]
    assert g["line"] and g["line_name"] == "測試家長" and g["pending_code"] is None
    assert staff.get("/admin/api/students").json()[0]["line_bound"] is True
    assert staff.delete(f"/admin/api/guardians/{guardian['id']}/line").status_code == 200
    bot.handle_event(make_event(text="孩子的課表"))
    assert "要先綁定帳號" in line.texts()[-1]
    bot.handle_event(make_event(text=f"綁定 {again}"))                    # 用過的碼
    assert "用過了" in line.texts()[-1]


def test_staff_binds_parent_line_directly(staff, line):
    """櫃台直接綁定：家長傳過訊息 → 後台列出這個 LINE（訊息提到孩子名字、家長電話的排最前面）→ 綁定 → 家長可以查課表。"""
    sid = new_student(staff, courses=[course_id("國二數學 A 班")])
    guardian = staff.get(f"/admin/api/students/{sid}").json()["all_guardians"][0]
    code = staff.post(f"/admin/api/guardians/{guardian['id']}/binding-code").json()["code"]
    other = "U" + "cd" * 16
    bot = Bot(line)
    bot.handle_event(make_event(text="找真人客服"))
    bot.handle_event(make_event(text="我是小明的媽媽，電話 0912-345-678"))
    bot.handle_event(make_event(text="你好", user=other))                 # 比較晚傳，但沒提到孩子
    url = f"/admin/api/guardians/{guardian['id']}/line-candidates"
    rows = staff.get(url).json()
    assert [r["line_user_id"] for r in rows] == [USER, other]
    assert rows[0]["hints"] == ["提到「小明」", "訊息有家長電話"] and rows[0]["selectable"]
    assert rows[0]["display_name"] == "測試家長" and rows[0]["recent"][0]["text"].startswith("我是小明的媽媽")
    assert rows[1]["hints"] == [] and rows[1]["bound_text"] is None
    assert [r["line_user_id"] for r in staff.get(url, params={"q": "你好"}).json()] == [other]

    r = staff.post(f"/admin/api/guardians/{guardian['id']}/line", json={"line_user_id": USER})
    assert r.status_code == 200 and r.json()["line_name"] == "測試家長"
    g = staff.get(f"/admin/api/students/{sid}").json()["all_guardians"][0]
    assert g["line"] and g["line_name"] == "測試家長" and g["pending_code"] is None     # 之前發的碼作廢
    assert staff.get("/admin/api/audit").json()[0]["summary"] == "直接綁定家長 王媽媽 的 LINE（LINE 名稱：測試家長）"
    bot.handle_event(make_event(text="回到AI助理"))
    bot.handle_event(make_event(text="孩子的課表"))
    assert "國二數學 A 班：週一、週三 18:30–20:30（201）" in line.texts()[-1]
    bot.handle_event(make_event(text=f"綁定 {code}", user=other))
    assert "過期" in line.texts()[-1]

    # 已經綁給別的家長的 LINE 不能選；已綁定的家長要先解除才能換
    sid2 = new_student(staff, name="李小華", grade=7,
                       guardian={"name": "李爸爸", "relationship": "父", "phone": "0922-000-111"})
    g2 = staff.get(f"/admin/api/students/{sid2}").json()["all_guardians"][0]
    rows = staff.get(f"/admin/api/guardians/{g2['id']}/line-candidates").json()
    assert [r["line_user_id"] for r in rows] == [other, USER]            # 不能選的排最後
    assert not rows[1]["selectable"] and rows[1]["bound_text"] == "已綁定：家長 王媽媽（王小明家）"
    r = staff.post(f"/admin/api/guardians/{g2['id']}/line", json={"line_user_id": USER})
    assert r.status_code == 400 and "已經綁定給家長 王媽媽（王小明家）" in r.json()["detail"]
    r = staff.post(f"/admin/api/guardians/{guardian['id']}/line", json={"line_user_id": other})
    assert r.status_code == 400 and "解除綁定" in r.json()["detail"]
    assert staff.post(f"/admin/api/guardians/{g2['id']}/line", json={"line_user_id": "U" + "ef" * 16}).status_code == 404
    assert staff.post(f"/admin/api/guardians/{g2['id']}/line", json={"line_user_id": "王媽媽"}).status_code == 422

    # 封鎖官方帳號的 LINE 不會列出、不能綁
    bot.handle_event(make_event("unfollow", user=other))
    assert other not in [r["line_user_id"] for r in staff.get(f"/admin/api/guardians/{g2['id']}/line-candidates").json()]
    assert staff.post(f"/admin/api/guardians/{g2['id']}/line", json={"line_user_id": other}).status_code == 400

    # 綁過 LINE 的家長被移除（改成停用）後，這個 LINE 可以改綁給別的家長
    assert staff.delete(f"/admin/api/guardians/{guardian['id']}").json()["how"] == "停用"
    rows = staff.get(f"/admin/api/guardians/{g2['id']}/line-candidates").json()
    assert rows[0]["selectable"] and "已停用" in rows[0]["bound_text"]
    r = staff.post(f"/admin/api/guardians/{g2['id']}/line", json={"line_user_id": USER})
    assert r.status_code == 200 and r.json()["moved_from"] == "王媽媽"
    assert db.fetch_one("SELECT line_user_id FROM guardians WHERE id = %s", (guardian["id"],))["line_user_id"] is None
    bot.handle_event(make_event(text="家長綁定"))
    assert "李爸爸" in line.texts()[-1] and "李小華（國一）" in line.texts()[-1]


# ------------------------------------------------------------------ 設定、對話紀錄
def test_settings_page(staff):
    assert staff.get("/admin/api/settings").json() == {"welcome_on_follow": True, "notify_unanswered": True}
    r = staff.put("/admin/api/settings", json={"welcome_on_follow": False, "notify_unanswered": False})
    assert r.json() == {"welcome_on_follow": False, "notify_unanswered": False}
    assert bot_settings.get("welcome_on_follow") is False
    assert staff.get("/admin/api/audit").json()[0]["summary"] == "修改機器人設定"
    assert staff.put("/admin/api/settings", json={"oa_auto_reply_words": ["最新消息"]}).json()["welcome_on_follow"] is False


# ------------------------------------------------------------------ 廣播、收通知的員工
@pytest.fixture
def pushes(monkeypatch, line):
    """後台的推播改傳到假的 LINE。"""
    from app import admin_messaging
    monkeypatch.setattr(admin_messaging, "line_client", lambda: line)
    monkeypatch.setattr(admin_messaging, "public_base", lambda: "https://bot.example.com")
    return line


def bind(staff, sid: int, user: str) -> None:
    """模擬家長傳過訊息，再由櫃台直接綁定。"""
    Bot(FakeLine()).handle_event(make_event(text="你好", user=user))
    gid = staff.get(f"/admin/api/students/{sid}").json()["all_guardians"][0]["id"]
    assert staff.post(f"/admin/api/guardians/{gid}/line", json={"line_user_id": user}).status_code == 200


def test_broadcast_to_grade_class_and_everyone(staff, pushes):
    import uuid
    a = new_student(staff, courses=[course_id("國二數學 A 班")])
    b = new_student(staff, name="陳怡君", grade=8, guardian={"name": "陳爸爸", "relationship": "父"})
    new_student(staff, name="林子晴", grade=7, guardian={"name": "林媽媽", "relationship": "母"})
    ua, ub = "U" + "a1" * 16, "U" + "b2" * 16
    bind(staff, a, ua)
    bind(staff, b, ub)

    aud = staff.get("/admin/api/broadcasts/audience", params={"kind": "grade", "grade": 8}).json()
    assert aud == {"label": "國二家長", "recipients": 2, "students": 2, "missing": []}
    aud = staff.get("/admin/api/broadcasts/audience", params={"kind": "grade", "grade": 7}).json()
    assert aud["recipients"] == 0 and aud["missing"] == ["林子晴"]           # 還沒綁定，收不到
    aud = staff.get("/admin/api/broadcasts/audience", params={"kind": "course", "course_id": course_id("國二數學 A 班")}).json()
    assert aud == {"label": "國二數學 A 班家長", "recipients": 1, "students": 1, "missing": []}

    rid = str(uuid.uuid4())
    body = {"request_id": rid, "kind": "grade", "grade": 8, "body": "明天國二段考，晚上照常上課。"}
    r = staff.post("/admin/api/broadcasts", json=body).json()
    assert r["status"] == "sent" and r["recipients"] == 2 and r["audience_label"] == "國二家長"
    assert pushes.pushes == [(f"multicast:{ua},{ub}", [{"type": "text", "text": "明天國二段考，晚上照常上課。"}])]
    assert pushes.retry_keys == [rid]
    assert staff.post("/admin/api/broadcasts", json=body).json()["id"] == r["id"]   # 按兩下：不會傳兩次
    assert len(pushes.pushes) == 1

    r = staff.post("/admin/api/broadcasts", json={"request_id": str(uuid.uuid4()), "kind": "grade", "grade": 7,
                                                  "body": "國一家長您好"})
    assert r.status_code == 400 and "國一還沒有家長綁定 LINE" in r.json()["detail"]
    r = staff.post("/admin/api/broadcasts", json={"request_id": str(uuid.uuid4()), "kind": "all", "body": "  "})
    assert r.status_code == 400

    r = staff.post("/admin/api/broadcasts", json={"request_id": str(uuid.uuid4()), "kind": "all", "body": "颱風停課"}).json()
    assert r["status"] == "sent" and r["recipients"] is None and pushes.pushes[-1][0] == "broadcast"
    from app.line_api import LineError
    pushes.fail_with = LineError(429, "這個月的訊息額度用完了")
    r = staff.post("/admin/api/broadcasts", json={"request_id": str(uuid.uuid4()), "kind": "all", "body": "再一次"}).json()
    assert r["status"] == "failed" and "額度" in r["error"]
    items = staff.get("/admin/api/broadcasts").json()["items"]
    assert [i["status"] for i in items] == ["failed", "sent", "sent"]
    assert staff.get("/admin/api/audit").json()[0]["summary"] == "廣播給全體好友（全體好友）：沒有傳出去"
    q = staff.get("/admin/api/line/quota").json()
    assert q == {"ok": True, "limit": 200, "used": 12, "friends": 87}


def test_staff_who_receive_alerts(staff, pushes):
    me = "U" + "5f" * 16
    Bot(FakeLine()).handle_event(make_event(text="我是小美", user=me))
    cands = staff.get("/admin/api/staff/line-candidates").json()
    assert cands[0]["line_user_id"] == me and cands[0]["selectable"]
    r = staff.post("/admin/api/staff", json={"name": "小美", "line_user_id": me})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert staff.get("/admin/api/staff").json() == [{"id": sid, "name": "小美", "notify_human": True, "line": True,
                                                     "line_name": "測試家長", "picture_url": None, "following": True}]
    assert not staff.get("/admin/api/staff/line-candidates").json()[0]["selectable"]
    assert "已經是員工" in staff.post("/admin/api/staff", json={"name": "小美2", "line_user_id": me}).json()["detail"]
    assert staff.post("/admin/api/staff", json={"name": "小美3", "line_user_id": "U" + "ef" * 16}).status_code == 404
    assert staff.post(f"/admin/api/staff/{sid}/test").status_code == 200
    assert pushes.pushes[-1][0] == me and pushes.pushed_texts()[-1].startswith("【測試】")
    assert staff.patch(f"/admin/api/staff/{sid}", json={"notify_human": False}).status_code == 200
    assert staff.get("/admin/api/staff").json()[0]["notify_human"] is False
    assert staff.delete(f"/admin/api/staff/{sid}").status_code == 200
    assert staff.get("/admin/api/staff").json() == []


def test_message_log_pairs_questions_with_replies(staff, line):
    bot = Bot(line)
    bot.handle_event(make_event(text="國二有獎學金嗎"))
    bot.handle_event(make_event(text="榜單"))
    rows = staff.get("/admin/api/messages").json()
    assert [r["content"] for r in rows] == ["榜單", "國二有獎學金嗎"]
    assert rows[1]["needs_human"] and rows[1]["reply"].startswith("這個問題我目前沒有資料")
    assert rows[0]["display_name"] == "測試家長" and "114 學年度榜單" in rows[0]["reply"]
    assert [r["content"] for r in staff.get("/admin/api/messages", params={"only_human": True}).json()] == ["國二有獎學金嗎"]
