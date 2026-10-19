"""後台（http://localhost:8000/admin）：學生／家長（含 LINE 綁定）、班級名單、上課時段、機器人設定、
對話紀錄與機器人測試。公告、行事曆、常見問題、榜單的表單在 app/admin_content.py（寫進「補習班資料」資料夾）。

安全：
・.env 要設定 BOT_ADMIN_PASSWORD 才會開啟。
・預設只能在跑 bot 的這台電腦上開；從 cloudflared／ngrok 的網址連進來會被擋（BOT_ADMIN_ALLOW_REMOTE=true 才開放）。
・登入後的 cookie 是 HttpOnly + SameSite=Strict，別的網站沒辦法冒用。
・每次新增、修改、刪除都記在 admin_audit（誰、什麼時候、改前改後）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import date
from datetime import time as dtime
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from . import ai, bot_settings, content, db, knowledge, media, migrate, vision
from .config import settings

log = logging.getLogger(__name__)
router = APIRouter()
UI_FILE = Path(__file__).resolve().parent / "static" / "admin.html"

COOKIE = "gaoming_admin"
SESSION_SECONDS = 12 * 3600
_SECRET = secrets.token_bytes(32)          # 每次啟動 bot 都換一把，重開後要重新登入
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
TUNNEL_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "forwarded",
                  "x-real-ip", "ngrok-trace-id")

GRADES = {6: "國小", 7: "國一", 8: "國二", 9: "國三"}
STUDENT_STATUS = {"active": "在學", "paused": "暫停", "left": "離班"}
ENROLL_STATUS = {"trial": "試聽", "active": "在班", "dropped": "退班", "finished": "結業"}
COURSE_STATUS = {"open": "開放中", "closed": "已結束", "archived": "封存"}


# ====================================================================== 登入
def _sign(name: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps([name, int(time.time()) + SESSION_SECONDS]).encode()).decode()
    sig = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _read(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    good = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        name, exp = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    return name if exp > time.time() else None


def _via_tunnel(request: Request) -> bool:
    if any(h in request.headers for h in TUNNEL_HEADERS):
        return True
    return (request.client.host if request.client else "") not in LOCAL_CLIENTS


def guard(request: Request) -> None:
    if not settings.admin_password:
        raise HTTPException(503, "後台還沒開啟：請在 .env 加一行 BOT_ADMIN_PASSWORD=你要的密碼，然後重新啟動 bot。")
    if not settings.admin_allow_remote and _via_tunnel(request):
        raise HTTPException(403, "後台只能在跑 bot 的這台電腦上開（http://localhost:8000/admin）。"
                                 "要從別台電腦開，請在 .env 設定 BOT_ADMIN_ALLOW_REMOTE=true，並確定密碼夠長。")


def current_user(request: Request) -> str:
    guard(request)
    name = _read(request.cookies.get(COOKIE))
    if not name:
        raise HTTPException(401, "請先登入")
    return name


class Login(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    password: str


@router.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    try:
        guard(request)
    except HTTPException as e:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>後台</title>"
                            f"<body style='font-family:sans-serif;padding:2rem;line-height:1.7'>{e.detail}</body>",
                            status_code=e.status_code)
    return FileResponse(UI_FILE, media_type="text/html", headers={"Cache-Control": "no-store"})


@router.post("/admin/api/login")
async def login(body: Login, request: Request, response: Response) -> dict:
    guard(request)
    if not hmac.compare_digest(body.password.encode(), settings.admin_password.encode()):
        await asyncio.sleep(1)                     # 放慢猜密碼的速度
        raise HTTPException(401, "密碼不對")
    secure = request.url.scheme == "https"
    response.set_cookie(COOKIE, _sign(body.name.strip()), max_age=SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=secure, path="/admin")
    return {"name": body.name.strip()}


@router.post("/admin/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/admin")
    return {"ok": True}


@router.get("/admin/api/me")
def me(user: str = Depends(current_user)) -> dict:
    return {"name": user}


# ====================================================================== 共用
def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def audit(conn, actor: str, action: str, table: str, row_id, summary: str,
          before: dict | None = None, after: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO admin_audit (actor, action, table_name, row_id, summary, before, after)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (actor, action, table, str(row_id) if row_id is not None else None, summary,
         Jsonb(before, dumps=_dumps) if before is not None else None,
         Jsonb(after, dumps=_dumps) if after is not None else None))


FRIENDLY = {
    "uq_enrollment_current": "這位學生已經在這個班上了",
    "students_student_no_key": "學號重複了，請換一個（留空會自動產生）",
    "students_grade_check": "年級只能是國小或國一～國三",
    "courses_grade_check": "班級年級只能是國小、國一～國三，或不分年級",
    "courses_check": "結束日要在開課日之後",
    "courses_fee_check": "學費不能是負的",
    "course_schedules_check": "下課時間要晚於上課時間",
    "subjects_name_key": "已經有這個科目了",
    "guardians_line_user_id_key": "這個 LINE 已經綁定給別的家長了，要改綁請先到那位家長的資料按「解除綁定」",
    "staff_line_user_id_key": "這個 LINE 已經是別的員工了",
}
LABELS = {"name": "姓名", "family_id": "家庭", "grade": "年級", "subject_id": "科目", "start_date": "開課日",
          "weekday": "星期", "start_time": "上課時間", "end_time": "下課時間", "family_name": "家庭名稱"}


def db_error(e: psycopg.Error) -> HTTPException:
    diag = e.diag
    if diag.constraint_name in FRIENDLY:
        return HTTPException(400, FRIENDLY[diag.constraint_name])
    if isinstance(e, psycopg.errors.ForeignKeyViolation):
        return HTTPException(400, "這筆資料還有其他資料在用（例如上課紀錄、成績），不能刪除；可以改成停用、封存或離班")
    if isinstance(e, psycopg.errors.NotNullViolation):
        return HTTPException(400, f"「{LABELS.get(diag.column_name, diag.column_name)}」不能空白")
    log.warning("資料庫錯誤：%s", e)
    return HTTPException(400, diag.message_primary or str(e))


def run(fn):
    """在同一個交易裡執行 fn(conn)；資料庫錯誤轉成看得懂的訊息。"""
    try:
        with db.get_pool().connection() as conn, conn.transaction():
            result = fn(conn)
    except psycopg.Error as e:
        raise db_error(e) from None
    knowledge.invalidate()            # 班級、時段改了，機器人馬上用新資料
    return result


def _one(conn, sql: str, params) -> dict:
    row = conn.execute(sql, params).fetchone()
    if not row:
        raise HTTPException(404, "找不到這筆資料")
    return row


def _clean(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


# ====================================================================== 總覽
@router.get("/admin/api/status")
def status(user: str = Depends(current_user)) -> dict:
    out: dict = {
        "ai": {"mode": settings.ai_mode, "model": settings.claude_model, "key_set": bool(settings.anthropic_api_key),
               "vision": vision.available(), "vision_model": settings.vision_model,
               "rules": ai.RULES.format(school=settings.school_name)},
        "public_base": settings.public_base_url or media.LAST_PUBLIC_BASE["url"],
        "public_base_from_env": bool(settings.public_base_url),
        "content": {"dir": str(settings.content_dir) if settings.content_dir else None,
                    "poll_seconds": settings.content_poll_seconds, "report": content.last_report()},
        "school": settings.school_name,
    }
    try:
        out["db"] = {"ok": True, "schema_ok": migrate.is_up_to_date()}
        out["counts"] = db.fetch_one("""
            SELECT (SELECT count(*) FROM students WHERE status = 'active')                      AS students,
                   (SELECT count(*) FROM courses WHERE status = 'open')                         AS courses,
                   (SELECT count(*) FROM enrollments WHERE status IN ('trial','active'))        AS enrollments,
                   (SELECT count(*) FROM kb_articles WHERE is_active AND audience = 'public')   AS kb,
                   (SELECT count(*) FROM v_active_announcements WHERE category <> 'calendar')   AS notices,
                   (SELECT count(*) FROM v_active_announcements WHERE category = 'calendar')    AS calendars,
                   (SELECT count(*) FROM honor_roll WHERE is_published)                         AS honors,
                   (SELECT count(*) FROM guardians WHERE is_active)                             AS guardians,
                   (SELECT count(*) FROM guardians WHERE is_active AND line_user_id IS NOT NULL) AS bound,
                   (SELECT count(*) FROM message_logs WHERE direction = 'in' AND needs_human
                                                        AND created_at > now() - interval '3 days') AS needs_human,
                   (SELECT count(*) FROM message_logs WHERE direction = 'in'
                                                        AND created_at > now() - interval '7 days') AS messages_7d""")
        out["warnings"] = warnings()
    except psycopg.Error as e:
        out["db"] = {"ok": False, "error": str(e)[:300]}
        out["warnings"] = []
    return out


def warnings() -> list[dict]:
    """會讓家長收到錯誤或不完整資訊的地方。level：error 一定要處理／warn 建議處理／info 參考。
    page：後台哪一頁可以處理；action：首頁可以直接按的按鈕。"""
    w: list[dict] = []
    for r in db.fetch_all("""SELECT id, title FROM v_active_announcements
                              WHERE source_path IS NULL AND title LIKE '【示範】%%' ORDER BY id"""):
        w.append({"level": "error", "text": f"家長看得到示範公告「{r['title']}」",
                  "action": "delete_outside", "table": "announcements", "id": r["id"]})
    if not settings.anthropic_api_key:
        w.append({"level": "info", "text": "沒有設定 ANTHROPIC_API_KEY，家長打字問的問題只用關鍵字比對回答"})
    if not (settings.public_base_url or media.LAST_PUBLIC_BASE["url"]):
        w.append({"level": "info", "text": "還不知道 bot 的 https 網址，附圖暫時傳不出去"})
    for r in db.fetch_all("""SELECT c.id, c.name FROM courses c
                              WHERE c.status = 'open' AND c.is_public
                                AND NOT EXISTS (SELECT 1 FROM course_schedules s WHERE s.course_id = c.id)
                              ORDER BY c.name"""):
        w.append({"level": "warn", "text": f"「{r['name']}」沒有上課時段", "course_id": r["id"]})
    for r in db.fetch_all("SELECT id, name, grade FROM courses WHERE grade IS NOT NULL AND grade NOT BETWEEN 6 AND 9"):
        w.append({"level": "error", "text": f"「{r['name']}」的年級不對", "course_id": r["id"]})
    demo = db.fetch_one("""SELECT count(*) FILTER (WHERE status = 'open') AS n FROM courses
                            WHERE description LIKE '%%（示範）'""")["n"]
    if demo:
        w.append({"level": "error", "text": f"還有 {demo} 個示範班級，家長會看到", "action": "remove_demo"})
    kb_demo = db.fetch_one("""SELECT count(*) AS n FROM kb_articles
                               WHERE is_active AND (content LIKE '%%示範%%' OR content LIKE '%%0900-000-000%%')""")["n"]
    if kb_demo:
        w.append({"level": "error", "text": f"常見問題還有 {kb_demo} 題是示範內容", "page": "faq"})
    honor_demo = db.fetch_one("SELECT count(*) AS n FROM honor_roll WHERE is_published AND achievement LIKE '%%示範%%'")["n"]
    if honor_demo:
        w.append({"level": "warn", "text": f"榜單還有 {honor_demo} 筆示範資料", "page": "honors"})
    n = db.fetch_one("""SELECT count(*) AS n FROM students s WHERE s.status = 'active' AND NOT EXISTS
                          (SELECT 1 FROM guardians g WHERE g.family_id = s.family_id AND g.is_active AND g.phone IS NOT NULL)""")["n"]
    if n:
        w.append({"level": "warn", "text": f"{n} 位在學學生沒有家長電話", "page": "students"})
    report = content.last_report()
    if report and report.get("problems"):
        w.append({"level": "warn", "text": f"「補習班資料」有 {len(report['problems'])} 個檔案有問題", "page": "settings"})
    if report and report.get("drafts"):
        w.append({"level": "warn", "text": f"{len(report['drafts'])} 份 AI 草稿還沒核對", "page": "notices"})
    return w


@router.post("/admin/api/sync")
def sync_now(user: str = Depends(current_user)) -> dict:
    if settings.content_dir is None:
        raise HTTPException(400, "資料夾同步已關閉（.env 的 BOT_CONTENT_DIR=off）")
    content.sync(force=True)
    return {"report": content.last_report()}


class PreviewIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


_preview_bot = None


@router.post("/admin/api/preview")
def preview(body: PreviewIn, user: str = Depends(current_user)) -> dict:
    global _preview_bot
    if _preview_bot is None:
        from .bot import Bot
        _preview_bot = Bot()
    return _preview_bot.preview(body.text)


@router.get("/admin/api/knowledge")
def knowledge_text(user: str = Depends(current_user)) -> dict:
    return {"text": knowledge.knowledge_text()}


@router.get("/admin/api/audit")
def recent_changes(limit: int = 15, user: str = Depends(current_user)) -> list[dict]:
    return db.fetch_all("""SELECT id, actor, action, table_name, summary, created_at FROM admin_audit
                            ORDER BY id DESC LIMIT %s""", (min(max(limit, 1), 100),))


# ====================================================================== 學生
STUDENT_SELECT = """
SELECT s.id, s.student_no, s.name, s.school_name, s.status, s.phone, s.joined_on, s.left_on, s.note,
       s.family_id, f.family_name,
       (s.grade + school_year_of(tw_today()) - s.grade_school_year)::int AS grade,
       grade_label(s.grade + school_year_of(tw_today()) - s.grade_school_year) AS grade_label,
       COALESCE((SELECT json_agg(json_build_object('id', g.id, 'name', g.name, 'relationship', g.relationship,
                                                   'phone', g.phone, 'line', g.line_user_id IS NOT NULL)
                                 ORDER BY g.is_primary DESC, g.id)
                   FROM guardians g WHERE g.family_id = s.family_id AND g.is_active), '[]') AS guardians,
       EXISTS (SELECT 1 FROM guardians g WHERE g.family_id = s.family_id AND g.is_active
                                          AND g.line_user_id IS NOT NULL) AS line_bound,
       COALESCE((SELECT json_agg(json_build_object('enrollment_id', e.id, 'course_id', c.id, 'name', c.name,
                                                   'status', e.status) ORDER BY c.name)
                   FROM enrollments e JOIN courses c ON c.id = e.course_id
                  WHERE e.student_id = s.id AND e.status IN ('trial','active')), '[]') AS courses
  FROM students s JOIN families f ON f.id = s.family_id
"""


@router.get("/admin/api/students")
def list_students(q: str = "", grade: str = "", status: str = "active",
                  user: str = Depends(current_user)) -> list[dict]:
    where, params = [], {}
    if q.strip():
        where.append("""(s.name ILIKE %(like)s OR s.student_no ILIKE %(like)s OR s.school_name ILIKE %(like)s
                         OR f.family_name ILIKE %(like)s OR s.phone ILIKE %(like)s
                         OR EXISTS (SELECT 1 FROM guardians g WHERE g.family_id = s.family_id
                                     AND (g.name ILIKE %(like)s OR g.phone ILIKE %(like)s)))""")
        params["like"] = f"%{q.strip()}%"
    current = "(s.grade + school_year_of(tw_today()) - s.grade_school_year)"
    if grade == "graduated":
        where.append(f"{current} > 9")
    elif grade.isdigit():
        where.append(f"{current} = %(grade)s")
        params["grade"] = int(grade)
    if status in STUDENT_STATUS:
        where.append("s.status = %(status)s")
        params["status"] = status
    sql = STUDENT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {current}, s.name"
    return db.fetch_all(sql, params)


@router.get("/admin/api/students/{student_id}")
def get_student(student_id: int, user: str = Depends(current_user)) -> dict:
    with db.get_pool().connection() as conn:
        s = _one(conn, STUDENT_SELECT + " WHERE s.id = %s", (student_id,))
        s["family"] = conn.execute("SELECT id, family_name, phone, address, note FROM families WHERE id = %s",
                                   (s["family_id"],)).fetchone()
        s["all_guardians"] = conn.execute(
            """SELECT g.id, g.name, g.relationship, g.phone, g.email, g.is_primary, g.is_active,
                      g.line_user_id IS NOT NULL AS line, lu.display_name AS line_name,
                      bc.code AS pending_code, bc.expires_at AS code_expires_at
                 FROM guardians g
                 LEFT JOIN line_users lu ON lu.line_user_id = g.line_user_id
                 LEFT JOIN LATERAL (SELECT code, expires_at FROM binding_codes b
                                     WHERE b.guardian_id = g.id AND b.used_at IS NULL AND b.expires_at > now()
                                     ORDER BY b.created_at DESC LIMIT 1) bc ON true
                WHERE g.family_id = %s ORDER BY g.is_active DESC, g.is_primary DESC, g.id""",
            (s["family_id"],)).fetchall()
        s["enrollments"] = conn.execute(
            """SELECT e.id, e.course_id, c.name AS course_name, c.grade, grade_label(c.grade) AS grade_label,
                      e.status, e.start_date, e.end_date
                 FROM enrollments e JOIN courses c ON c.id = e.course_id
                WHERE e.student_id = %s
                ORDER BY (e.status IN ('trial','active')) DESC, e.start_date DESC""", (student_id,)).fetchall()
        s["siblings"] = conn.execute(
            """SELECT id, name, grade_label(grade + school_year_of(tw_today()) - grade_school_year) AS grade_label, status
                 FROM students WHERE family_id = %s AND id <> %s ORDER BY name""",
            (s["family_id"], student_id)).fetchall()
    return s


GradeValue = Literal[6, 7, 8, 9]


class StudentFields(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    grade: GradeValue | None = None
    school_name: str | None = None
    student_no: str | None = None
    phone: str | None = None
    status: Literal["active", "paused", "left"] | None = None
    joined_on: date | None = None
    left_on: date | None = None
    note: str | None = None


class FamilyIn(BaseModel):
    family_name: str | None = None
    phone: str | None = None
    address: str | None = None
    note: str | None = None


class GuardianIn(BaseModel):
    name: str | None = Field(default=None, max_length=50)
    relationship: str | None = None
    phone: str | None = None
    email: str | None = None
    is_primary: bool | None = None
    is_active: bool | None = None


class CourseChoice(BaseModel):
    course_id: int
    status: Literal["active", "trial"] = "active"


class NewStudent(BaseModel):
    student: StudentFields
    family_id: int | None = None
    family: FamilyIn | None = None
    guardian: GuardianIn | None = None
    courses: list[CourseChoice] = []


def _next_student_no(conn) -> str:
    year = conn.execute("SELECT school_year_of(tw_today()) AS y").fetchone()["y"]
    n = conn.execute(r"""SELECT COALESCE(max(substring(student_no FROM '^G\d{3}(\d+)$')::int), 0) + 1 AS n
                           FROM students WHERE student_no ~ ('^G' || %s || '\d+$')""", (str(year),)).fetchone()["n"]
    return f"G{year}{n:03d}"


def _enroll(conn, actor: str, student_id: int, course_id: int, status: str) -> int:
    course = _one(conn, "SELECT id, name, status FROM courses WHERE id = %s", (course_id,))
    if course["status"] != "open":
        raise HTTPException(400, f"「{course['name']}」不是開放中的班級")
    student = _one(conn, "SELECT name FROM students WHERE id = %s", (student_id,))
    row = conn.execute("""INSERT INTO enrollments (student_id, course_id, status) VALUES (%s, %s, %s)
                          RETURNING id""", (student_id, course_id, status)).fetchone()
    word = "試聽" if status == "trial" else "加入"
    audit(conn, actor, "enroll", "enrollments", row["id"], f"{student['name']} {word} {course['name']}")
    return row["id"]


@router.post("/admin/api/students")
def create_student(body: NewStudent, user: str = Depends(current_user)) -> dict:
    st = body.student
    if not _clean(st.name) or st.grade is None:
        raise HTTPException(400, "請填學生姓名和年級")

    def do(conn):
        family_id = body.family_id
        if family_id:
            _one(conn, "SELECT id FROM families WHERE id = %s", (family_id,))
        else:
            fam = body.family or FamilyIn()
            g = body.guardian or GuardianIn()
            family_id = conn.execute(
                "INSERT INTO families (family_name, phone, address) VALUES (%s, %s, %s) RETURNING id",
                (_clean(fam.family_name) or f"{st.name.strip()}家", _clean(fam.phone) or _clean(g.phone),
                 _clean(fam.address))).fetchone()["id"]
            if _clean(g.name):
                conn.execute("""INSERT INTO guardians (family_id, name, relationship, phone, email, is_primary)
                                VALUES (%s, %s, %s, %s, %s, true)""",
                             (family_id, g.name.strip(), _clean(g.relationship) or "家長", _clean(g.phone),
                              _clean(g.email)))
        row = conn.execute(
            """INSERT INTO students (student_no, family_id, name, school_name, grade, grade_school_year, phone,
                                     joined_on, note)
               VALUES (%s, %s, %s, %s, %s, school_year_of(tw_today()), %s, COALESCE(%s, tw_today()), %s)
               RETURNING id""",
            (_clean(st.student_no) or _next_student_no(conn), family_id, st.name.strip(), _clean(st.school_name),
             st.grade, _clean(st.phone), st.joined_on, _clean(st.note))).fetchone()
        audit(conn, user, "insert", "students", row["id"], f"新增學生 {st.name.strip()}（{GRADES[st.grade]}）",
              after=body.model_dump(mode="json"))
        for c in body.courses:
            _enroll(conn, user, row["id"], c.course_id, c.status)
        return {"id": row["id"]}
    return run(do)


@router.patch("/admin/api/students/{student_id}")
def update_student(student_id: int, body: StudentFields, user: str = Depends(current_user)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and not _clean(fields["name"]):
        raise HTTPException(400, "姓名不能空白")

    def do(conn):
        before = _one(conn, "SELECT * FROM students WHERE id = %s", (student_id,))
        sets, params = [], []
        for k, v in fields.items():
            if k == "grade":
                if v is None:
                    raise HTTPException(400, "年級不能空白")
                sets += ["grade = %s", "grade_school_year = school_year_of(tw_today())"]
                params.append(v)
            elif k == "student_no":
                sets.append("student_no = %s")
                params.append(_clean(v) or before["student_no"])
            else:
                sets.append(f"{k} = %s")
                params.append(_clean(v))
        dropped = 0
        if fields.get("status") == "left":
            if "left_on" not in fields:
                sets.append("left_on = COALESCE(left_on, tw_today())")
            dropped = conn.execute("""UPDATE enrollments SET status = 'dropped', end_date = tw_today()
                                       WHERE student_id = %s AND status IN ('trial','active')""",
                                   (student_id,)).rowcount
        if sets:
            conn.execute(f"UPDATE students SET {', '.join(sets)} WHERE id = %s", (*params, student_id))
        after = conn.execute("SELECT * FROM students WHERE id = %s", (student_id,)).fetchone()
        changed = {k: before[k] for k in before if before[k] != after[k] and k != "updated_at"}
        if changed:
            audit(conn, user, "update", "students", student_id, f"修改學生 {after['name']}",
                  before=changed, after={k: after[k] for k in changed})
        return {"ok": True, "dropped": dropped}
    return run(do)


@router.get("/admin/api/families")
def search_families(q: str = "", user: str = Depends(current_user)) -> list[dict]:
    if not q.strip():
        return []
    like = f"%{q.strip()}%"
    return db.fetch_all(
        """SELECT f.id, f.family_name, f.phone,
                  (SELECT string_agg(g.name || COALESCE(' ' || g.phone, ''), '、' ORDER BY g.is_primary DESC, g.id)
                     FROM guardians g WHERE g.family_id = f.id AND g.is_active) AS guardians,
                  (SELECT string_agg(s.name, '、' ORDER BY s.name) FROM students s WHERE s.family_id = f.id) AS students
             FROM families f
            WHERE f.family_name ILIKE %(like)s OR f.phone ILIKE %(like)s
               OR EXISTS (SELECT 1 FROM guardians g WHERE g.family_id = f.id
                           AND (g.name ILIKE %(like)s OR g.phone ILIKE %(like)s))
               OR EXISTS (SELECT 1 FROM students s WHERE s.family_id = f.id AND s.name ILIKE %(like)s)
            ORDER BY f.family_name LIMIT 20""", {"like": like})


@router.patch("/admin/api/families/{family_id}")
def update_family(family_id: int, body: FamilyIn, user: str = Depends(current_user)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "family_name" in fields and not _clean(fields["family_name"]):
        raise HTTPException(400, "家庭名稱不能空白")

    def do(conn):
        before = _one(conn, "SELECT family_name, phone, address, note FROM families WHERE id = %s", (family_id,))
        if fields:
            conn.execute(f"UPDATE families SET {', '.join(f'{k} = %s' for k in fields)} WHERE id = %s",
                         (*[_clean(v) for v in fields.values()], family_id))
            audit(conn, user, "update", "families", family_id, f"修改家庭 {before['family_name']}",
                  before=dict(before), after=fields)
        return {"ok": True}
    return run(do)


@router.post("/admin/api/families/{family_id}/guardians")
def add_guardian(family_id: int, body: GuardianIn, user: str = Depends(current_user)) -> dict:
    if not _clean(body.name):
        raise HTTPException(400, "請填家長姓名")

    def do(conn):
        _one(conn, "SELECT id FROM families WHERE id = %s", (family_id,))
        has_primary = conn.execute("SELECT 1 FROM guardians WHERE family_id = %s AND is_primary AND is_active",
                                   (family_id,)).fetchone()
        row = conn.execute(
            """INSERT INTO guardians (family_id, name, relationship, phone, email, is_primary)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (family_id, body.name.strip(), _clean(body.relationship) or "家長", _clean(body.phone), _clean(body.email),
             bool(body.is_primary) or not has_primary)).fetchone()
        audit(conn, user, "insert", "guardians", row["id"], f"新增家長 {body.name.strip()}",
              after=body.model_dump(mode="json"))
        return {"id": row["id"]}
    return run(do)


@router.patch("/admin/api/guardians/{guardian_id}")
def update_guardian(guardian_id: int, body: GuardianIn, user: str = Depends(current_user)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and not _clean(fields["name"]):
        raise HTTPException(400, "家長姓名不能空白")

    def do(conn):
        before = _one(conn, "SELECT family_id, name, relationship, phone, email, is_primary, is_active "
                            "FROM guardians WHERE id = %s", (guardian_id,))
        if fields.get("is_primary"):
            conn.execute("UPDATE guardians SET is_primary = false WHERE family_id = %s", (before["family_id"],))
        if fields:
            conn.execute(f"UPDATE guardians SET {', '.join(f'{k} = %s' for k in fields)} WHERE id = %s",
                         (*[_clean(v) for v in fields.values()], guardian_id))
            audit(conn, user, "update", "guardians", guardian_id, f"修改家長 {before['name']}",
                  before=dict(before), after=fields)
        return {"ok": True}
    return run(do)


@router.delete("/admin/api/guardians/{guardian_id}")
def remove_guardian(guardian_id: int, user: str = Depends(current_user)) -> dict:
    """沒被其他資料用到就刪除；綁過 LINE、請過假的家長改成停用，紀錄保留。"""
    def deactivate(conn):
        conn.execute("UPDATE guardians SET is_active = false, is_primary = false WHERE id = %s", (guardian_id,))
        return "停用"

    def do(conn):
        g = _one(conn, "SELECT name, line_user_id FROM guardians WHERE id = %s", (guardian_id,))
        if g["line_user_id"]:
            how = deactivate(conn)
        else:
            try:
                with conn.transaction():
                    conn.execute("DELETE FROM guardians WHERE id = %s", (guardian_id,))
                how = "刪除"
            except psycopg.errors.ForeignKeyViolation:
                how = deactivate(conn)
        audit(conn, user, "delete", "guardians", guardian_id, f"{how}家長 {g['name']}")
        return {"ok": True, "how": how}
    return run(do)


# ====================================================================== 家長 LINE 綁定
BIND_DAYS = 14


@router.post("/admin/api/guardians/{guardian_id}/binding-code")
def issue_binding_code(guardian_id: int, user: str = Depends(current_user)) -> dict:
    """發一組 8 碼綁定碼給家長（14 天內有效）。同一位家長之前還沒用的碼會作廢。
    家長在補習班的 LINE 官方帳號輸入「綁定 綁定碼」，這個 LINE 就會連到這位家長。"""
    def do(conn):
        g = _one(conn, "SELECT name, is_active FROM guardians WHERE id = %s", (guardian_id,))
        if not g["is_active"]:
            raise HTTPException(400, "這位家長已經停用，不能發綁定碼")
        conn.execute("""UPDATE binding_codes SET expires_at = now()
                         WHERE guardian_id = %s AND used_at IS NULL AND expires_at > now()""", (guardian_id,))
        code = conn.execute("SELECT issue_binding_code('guardian', %s, NULL, %s) AS c",
                            (guardian_id, BIND_DAYS)).fetchone()["c"]
        expires = conn.execute("SELECT expires_at FROM binding_codes WHERE code = %s", (code,)).fetchone()["expires_at"]
        audit(conn, user, "insert", "binding_codes", code, f"發綁定碼給家長 {g['name']}")
        return {"code": code, "expires_at": expires, "guardian": g["name"],
                "message": f"請在「{settings.school_name}」的 LINE 官方帳號輸入：綁定 {code}"
                           f"（{expires.astimezone(content.TW):%m/%d} 前有效）"}
    return run(do)


@router.delete("/admin/api/guardians/{guardian_id}/line")
def unbind_guardian(guardian_id: int, user: str = Depends(current_user)) -> dict:
    """解除這位家長和 LINE 的綁定（例如換手機、綁錯人）。"""
    def do(conn):
        g = _one(conn, "SELECT name, line_user_id FROM guardians WHERE id = %s", (guardian_id,))
        if not g["line_user_id"]:
            raise HTTPException(400, "這位家長還沒有綁定 LINE")
        conn.execute("UPDATE guardians SET line_user_id = NULL WHERE id = %s", (guardian_id,))
        conn.execute("UPDATE line_users SET menu_role = 'guest' WHERE line_user_id = %s", (g["line_user_id"],))
        audit(conn, user, "update", "guardians", guardian_id, f"解除家長 {g['name']} 的 LINE 綁定")
        return {"ok": True}
    return run(do)


# ---------------------------------------------------------------------- 櫃台直接綁定（家長不用輸入綁定碼）
# LINE 不提供「用電話或 LINE ID 找人」：家長要先加官方帳號好友、傳過訊息，bot 才知道他的 LINE。
# 櫃台從這些 LINE 裡挑一個綁定；訊息裡提到孩子名字、家長電話的會標出來，但還是要櫃台確認是本人。
GENERIC_NAMES = {"媽媽", "爸爸", "媽咪", "爹地", "阿嬤", "阿公", "奶奶", "爺爺", "外婆", "外公", "家長"}
CANDIDATE_LIMIT = 30


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def _line_hints(c: dict, kids: list[str], parents: list[str], phones: set[str]) -> list[str]:
    """這個 LINE 像不像這位家長（只是提示）：訊息或 LINE 名稱提到孩子的名字、訊息裡有家長電話、LINE 名稱像家長姓名。"""
    texts = [m["text"] for m in c["recent"]]
    name = c["display_name"] or ""
    hints = []
    for kid in kids:
        for word in (kid, kid[1:] if len(kid) >= 3 else None):          # 王小明 → 也找「小明」
            if word and (word in name or any(word in t for t in texts)):
                hints.append(f"提到「{word}」")
                break
    if any(p in _digits(t) for t in texts for p in phones):
        hints.append("訊息有家長電話")
    for p in parents:
        if len(name) >= 2 and (p in name or (name in p and name not in GENERIC_NAMES)):
            hints.append(f"LINE 名稱像「{p}」")
            break
    return hints


def line_users(q: str = "", limit: int = 100) -> list[dict]:
    """加過好友或傳過訊息、而且沒有封鎖官方帳號的 LINE（最近傳訊息的在前），附上最近 5 則文字訊息和綁定給誰。
    q：搜尋 LINE 名稱或訊息內容。"""
    where, params = ["lu.is_following"], {"limit": limit}
    if q.strip():
        where.append("""(lu.display_name ILIKE %(like)s
                         OR EXISTS (SELECT 1 FROM message_logs ml WHERE ml.line_user_id = lu.line_user_id
                                     AND ml.direction = 'in' AND ml.content ILIKE %(like)s))""")
        params["like"] = f"%{q.strip()}%"
    return db.fetch_all(f"""
        SELECT lu.line_user_id, lu.display_name, lu.picture_url, lu.last_active_at,
               g.name AS guardian_name, g.family_name AS guardian_family, g.is_active AS guardian_active,
               (SELECT st.name FROM staff st WHERE st.line_user_id = lu.line_user_id) AS staff_name,
               (SELECT s.name FROM students s WHERE s.line_user_id = lu.line_user_id) AS student_name,
               COALESCE(m.recent, '[]') AS recent
          FROM line_users lu
          LEFT JOIN LATERAL (SELECT x.name, f.family_name, x.is_active
                               FROM guardians x JOIN families f ON f.id = x.family_id
                              WHERE x.line_user_id = lu.line_user_id) g ON true
          LEFT JOIN LATERAL (
                SELECT json_agg(json_build_object('text', t.content, 'at', t.created_at) ORDER BY t.id DESC) AS recent
                  FROM (SELECT id, content, created_at FROM message_logs ml
                         WHERE ml.line_user_id = lu.line_user_id AND ml.direction = 'in'
                           AND ml.message_type = 'text' AND ml.content IS NOT NULL
                         ORDER BY ml.id DESC LIMIT 5) t) m ON true
         WHERE {" AND ".join(where)}
         ORDER BY lu.last_active_at DESC NULLS LAST, lu.created_at DESC
         LIMIT %(limit)s""", params)


@router.get("/admin/api/guardians/{guardian_id}/line-candidates")
def line_candidates(guardian_id: int, q: str = "", user: str = Depends(current_user)) -> list[dict]:
    """「直接綁定」可以選的 LINE。像這位家長的排最前面，其次是最近傳訊息的；已經綁給別的家長的排最後（不能選）。"""
    g = db.fetch_one("SELECT family_id FROM guardians WHERE id = %s", (guardian_id,))
    if not g:
        raise HTTPException(404, "找不到這筆資料")
    fam = db.fetch_one(
        """SELECT ARRAY(SELECT name FROM students WHERE family_id = %(f)s AND status <> 'left' ORDER BY name) AS kids,
                  ARRAY(SELECT name FROM guardians WHERE family_id = %(f)s AND is_active ORDER BY id) AS parents,
                  ARRAY(SELECT phone FROM guardians WHERE family_id = %(f)s AND is_active AND phone IS NOT NULL
                        UNION SELECT phone FROM families WHERE id = %(f)s AND phone IS NOT NULL) AS phones""",
        {"f": g["family_id"]})
    kids = list(fam["kids"])
    parents = [p for p in fam["parents"] if len(p) >= 2]
    phones = {d for d in map(_digits, fam["phones"]) if len(d) >= 8}
    rows = line_users(q)
    for c in rows:
        c["hints"] = _line_hints(c, kids, parents, phones)
        name, family, active = c.pop("guardian_name"), c.pop("guardian_family"), c.pop("guardian_active")
        student = c.pop("student_name")
        c["selectable"] = not student and (name is None or not active)
        if student:
            c["bound_text"] = f"已綁定：學生 {student}"
        elif name and active:
            c["bound_text"] = f"已綁定：家長 {name}（{family}）"
        elif name:
            c["bound_text"] = f"原本綁的家長 {name} 已停用，可以改綁到這裡"
        else:
            c["bound_text"] = f"員工：{c['staff_name']}" if c["staff_name"] else None
    rows.sort(key=lambda c: (not c["selectable"], -len(c["hints"])))     # 排序穩定：同一組裡最近傳訊息的在前
    return rows[:CANDIDATE_LIMIT]


class LineBindIn(BaseModel):
    line_user_id: str = Field(pattern=r"^U[0-9a-f]{32}$")


@router.post("/admin/api/guardians/{guardian_id}/line")
def bind_guardian_line(guardian_id: int, body: LineBindIn, user: str = Depends(current_user)) -> dict:
    """櫃台直接幫家長綁定 LINE（不用綁定碼）。這位家長還沒用的綁定碼會作廢；綁錯了按「解除綁定」。"""
    uid = body.line_user_id

    def do(conn):
        g = _one(conn, "SELECT name, is_active, line_user_id FROM guardians WHERE id = %s FOR UPDATE", (guardian_id,))
        if not g["is_active"]:
            raise HTTPException(400, "這位家長已經停用，不能綁定")
        if g["line_user_id"] == uid:
            return {"ok": True, "line_name": None, "moved_from": None}
        if g["line_user_id"]:
            raise HTTPException(400, f"{g['name']} 已經綁定了另一個 LINE；要換，請先按「解除綁定」")
        lu = conn.execute("SELECT display_name, is_following FROM line_users WHERE line_user_id = %s", (uid,)).fetchone()
        if not lu:
            raise HTTPException(404, "找不到這個 LINE：家長要先加官方帳號好友，並傳一則訊息")
        if not lu["is_following"]:
            raise HTTPException(400, "這個 LINE 已經封鎖或刪除官方帳號，不能綁定")
        moved = None
        for o in conn.execute(
                """SELECT 'guardian' AS kind, x.id, x.name, x.is_active, f.family_name AS family
                     FROM guardians x JOIN families f ON f.id = x.family_id WHERE x.line_user_id = %(u)s
                   UNION ALL SELECT 'student', id, name, true, NULL FROM students WHERE line_user_id = %(u)s""",
                {"u": uid}).fetchall():
            if o["kind"] == "guardian" and not o["is_active"]:        # 綁在已停用（移除）的家長：改綁到這裡
                conn.execute("UPDATE guardians SET line_user_id = NULL WHERE id = %s", (o["id"],))
                moved = o["name"]
            elif o["kind"] == "guardian":
                raise HTTPException(400, f"這個 LINE 已經綁定給家長 {o['name']}（{o['family']}）了；"
                                         "要改綁，請先到那位家長的資料按「解除綁定」")
            else:
                raise HTTPException(400, f"這個 LINE 已經綁定給學生 {o['name']} 了")
        conn.execute("UPDATE guardians SET line_user_id = %s WHERE id = %s", (uid, guardian_id))
        conn.execute("UPDATE line_users SET menu_role = 'guardian' WHERE line_user_id = %s", (uid,))
        conn.execute("""UPDATE binding_codes SET expires_at = now()
                         WHERE guardian_id = %s AND used_at IS NULL AND expires_at > now()""", (guardian_id,))
        summary = f"直接綁定家長 {g['name']} 的 LINE（LINE 名稱：{lu['display_name'] or '沒有'}）"
        if moved:
            summary += f"，原本綁在已停用的家長 {moved}"
        audit(conn, user, "update", "guardians", guardian_id, summary, after={"line_user_id": uid})
        return {"ok": True, "line_name": lu["display_name"], "moved_from": moved}
    return run(do)


# ====================================================================== 機器人設定
class SettingsIn(BaseModel):
    welcome_on_follow: bool | None = None
    notify_unanswered: bool | None = None


@router.get("/admin/api/settings")
def get_settings(user: str = Depends(current_user)) -> dict:
    bot_settings.invalidate()
    return bot_settings.values()


@router.put("/admin/api/settings")
def put_settings(body: SettingsIn, user: str = Depends(current_user)) -> dict:
    changes = body.model_dump(exclude_none=True)

    def do(conn):
        before = bot_settings.values()
        bot_settings.save(conn, changes, user)
        audit(conn, user, "update", "bot_settings", None, "修改機器人設定",
              before={k: before[k] for k in changes}, after=changes)
        return {**before, **changes}
    return run(do)


# ====================================================================== 對話紀錄
@router.get("/admin/api/messages")
def recent_messages(only_human: bool = False, limit: int = 60, user: str = Depends(current_user)) -> list[dict]:
    """家長最近傳的訊息，和 bot 的回覆（下一則 out）。only_human：只看需要真人回覆的。"""
    return db.fetch_all(
        """SELECT i.id, i.created_at, i.message_type, i.content, i.intent, i.handled_by, i.needs_human,
                  lu.display_name, i.line_user_id,
                  (SELECT g.name FROM guardians g WHERE g.line_user_id = i.line_user_id) AS guardian,
                  COALESCE(r.content, old.content) AS reply, COALESCE(r.tool_calls, old.tool_calls) AS detail,
                  COALESCE(r.model, old.model) AS model,
                  (SELECT json_build_object('sent_to', a.sent_to, 'error', a.error)
                     FROM staff_alerts a WHERE a.message_id = i.id) AS alert
             FROM message_logs i
             JOIN line_users lu ON lu.line_user_id = i.line_user_id
             LEFT JOIN LATERAL (SELECT content, tool_calls, model FROM message_logs o
                                 WHERE o.reply_to = i.id ORDER BY o.id LIMIT 1) r ON true
             -- 升級前的紀錄沒有 reply_to：用「下一則回覆」對應
             LEFT JOIN LATERAL (SELECT content, tool_calls, model FROM message_logs o
                                 WHERE r.content IS NULL AND o.reply_to IS NULL
                                   AND o.line_user_id = i.line_user_id AND o.direction = 'out' AND o.id > i.id
                                   AND o.id < COALESCE((SELECT min(n.id) FROM message_logs n
                                                         WHERE n.line_user_id = i.line_user_id
                                                           AND n.direction = 'in' AND n.id > i.id), 9223372036854775807)
                                 ORDER BY o.id LIMIT 1) old ON true
            WHERE i.direction = 'in' AND (NOT %s OR i.needs_human)
            ORDER BY i.id DESC LIMIT %s""",
        (only_human, min(max(limit, 1), 300)))


# ====================================================================== 班級
COURSE_SELECT = """
SELECT c.id, c.name, c.subject_id, sub.name AS subject, c.grade, grade_label(c.grade) AS grade_label,
       c.start_date, c.end_date, c.fee, c.is_public, c.status, c.description,
       COALESCE((SELECT json_agg(json_build_object('id', cs.id, 'weekday', cs.weekday,
                                                   'start_time', to_char(cs.start_time, 'HH24:MI'),
                                                   'end_time', to_char(cs.end_time, 'HH24:MI'), 'room', cs.room)
                                 ORDER BY cs.weekday, cs.start_time)
                   FROM course_schedules cs WHERE cs.course_id = c.id), '[]') AS schedules,
       (SELECT count(*) FROM enrollments e WHERE e.course_id = c.id AND e.status = 'active')::int AS active_count,
       (SELECT count(*) FROM enrollments e WHERE e.course_id = c.id AND e.status = 'trial')::int AS trial_count,
       c.description LIKE '%%（示範）' AS is_demo
  FROM courses c JOIN subjects sub ON sub.id = c.subject_id
"""


@router.get("/admin/api/courses")
def list_courses(user: str = Depends(current_user)) -> list[dict]:
    return db.fetch_all(COURSE_SELECT + """
        ORDER BY CASE c.status WHEN 'open' THEN 0 WHEN 'closed' THEN 1 ELSE 2 END,
                 c.grade NULLS LAST, c.name""")


@router.get("/admin/api/courses/{course_id}")
def get_course(course_id: int, user: str = Depends(current_user)) -> dict:
    with db.get_pool().connection() as conn:
        c = _one(conn, COURSE_SELECT + " WHERE c.id = %s", (course_id,))
        c["roster"] = conn.execute(
            """SELECT e.id AS enrollment_id, e.status, e.start_date, s.id AS student_id, s.name, s.school_name,
                      s.status AS student_status,
                      grade_label(s.grade + school_year_of(tw_today()) - s.grade_school_year) AS grade_label,
                      (s.grade + school_year_of(tw_today()) - s.grade_school_year)::int AS grade,
                      (SELECT g.name || COALESCE(' ' || g.phone, '') FROM guardians g
                        WHERE g.family_id = s.family_id AND g.is_active
                        ORDER BY g.is_primary DESC, g.id LIMIT 1) AS guardian
                 FROM enrollments e JOIN students s ON s.id = e.student_id
                WHERE e.course_id = %s AND e.status IN ('trial','active')
                ORDER BY e.status DESC, s.name""", (course_id,)).fetchall()
        c["used"] = conn.execute(
            """SELECT EXISTS (SELECT 1 FROM enrollments WHERE course_id = %(id)s)
                   OR EXISTS (SELECT 1 FROM class_sessions WHERE course_id = %(id)s)
                   OR EXISTS (SELECT 1 FROM exams WHERE course_id = %(id)s)
                   OR EXISTS (SELECT 1 FROM tuition_bills WHERE course_id = %(id)s) AS used""",
            {"id": course_id}).fetchone()["used"]
    return c


class ScheduleIn(BaseModel):
    weekday: int = Field(ge=1, le=7)
    start_time: dtime
    end_time: dtime
    room: str | None = None

    @field_validator("end_time")
    @classmethod
    def after_start(cls, v, info):
        start = info.data.get("start_time")
        if start and v <= start:
            raise ValueError("下課時間要晚於上課時間")
        return v


class CourseIn(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    subject_id: int | None = None
    grade: GradeValue | None = None
    start_date: date | None = None
    end_date: date | None = None
    fee: int | None = Field(default=None, ge=0)
    is_public: bool | None = None
    status: Literal["open", "closed", "archived"] | None = None
    description: str | None = None
    schedules: list[ScheduleIn] | None = None


def _save_schedules(conn, course_id: int, schedules: list[ScheduleIn]) -> None:
    conn.execute("DELETE FROM course_schedules WHERE course_id = %s", (course_id,))
    for s in schedules:
        conn.execute("""INSERT INTO course_schedules (course_id, weekday, start_time, end_time, room)
                        VALUES (%s, %s, %s, %s, %s)""",
                     (course_id, s.weekday, s.start_time, s.end_time, _clean(s.room)))


@router.post("/admin/api/courses")
def create_course(body: CourseIn, user: str = Depends(current_user)) -> dict:
    if not _clean(body.name) or not body.subject_id:
        raise HTTPException(400, "請填班級名稱和科目")

    def do(conn):
        row = conn.execute(
            """INSERT INTO courses (name, subject_id, grade, start_date, end_date, fee, is_public, status, description)
               VALUES (%s, %s, %s, COALESCE(%s, tw_today()), %s, %s, %s, %s, %s) RETURNING id""",
            (body.name.strip(), body.subject_id, body.grade, body.start_date, body.end_date, body.fee,
             True if body.is_public is None else body.is_public, body.status or "open",
             _clean(body.description))).fetchone()
        _save_schedules(conn, row["id"], body.schedules or [])
        audit(conn, user, "insert", "courses", row["id"], f"新增班級 {body.name.strip()}",
              after=body.model_dump(mode="json"))
        return {"id": row["id"]}
    return run(do)


@router.patch("/admin/api/courses/{course_id}")
def update_course(course_id: int, body: CourseIn, user: str = Depends(current_user)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    schedules = fields.pop("schedules", None)
    if "name" in fields and not _clean(fields["name"]):
        raise HTTPException(400, "班級名稱不能空白")
    for required in ("subject_id", "start_date", "is_public", "status"):
        if required in fields and fields[required] is None:
            fields.pop(required)

    def do(conn):
        before = _one(conn, COURSE_SELECT + " WHERE c.id = %s", (course_id,))
        if fields:
            conn.execute(f"UPDATE courses SET {', '.join(f'{k} = %s' for k in fields)} WHERE id = %s",
                         (*[_clean(v) for v in fields.values()], course_id))
        if schedules is not None:
            _save_schedules(conn, course_id, body.schedules or [])
        after = conn.execute(COURSE_SELECT + " WHERE c.id = %s", (course_id,)).fetchone()
        keys = [k for k in ("name", "subject", "grade", "start_date", "end_date", "fee", "is_public", "status",
                            "description", "schedules") if before[k] != after[k]]
        if keys:
            audit(conn, user, "update", "courses", course_id, f"修改班級 {after['name']}",
                  before={k: before[k] for k in keys}, after={k: after[k] for k in keys})
        return {"ok": True}
    return run(do)


@router.delete("/admin/api/courses/{course_id}")
def delete_course(course_id: int, user: str = Depends(current_user)) -> dict:
    def do(conn):
        c = _one(conn, "SELECT name FROM courses WHERE id = %s", (course_id,))
        try:
            with conn.transaction():
                conn.execute("DELETE FROM courses WHERE id = %s", (course_id,))
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, "這個班有學生或上課紀錄，不能刪除；請把狀態改成「已結束」或「封存」") from None
        audit(conn, user, "delete", "courses", course_id, f"刪除班級 {c['name']}")
        return {"ok": True}
    return run(do)


@router.post("/admin/api/courses/remove-demo")
def remove_demo_courses(user: str = Depends(current_user)) -> dict:
    """刪掉還沒有學生、上課紀錄的示範班級；有用到的改成封存。"""
    def do(conn):
        removed, archived = [], []
        for c in conn.execute("SELECT id, name FROM courses WHERE description LIKE '%%（示範）' ORDER BY id").fetchall():
            try:
                with conn.transaction():
                    conn.execute("DELETE FROM courses WHERE id = %s", (c["id"],))
                removed.append(c["name"])
            except psycopg.errors.ForeignKeyViolation:
                conn.execute("UPDATE courses SET status = 'archived', is_public = false WHERE id = %s", (c["id"],))
                archived.append(c["name"])
        if removed or archived:
            audit(conn, user, "delete", "courses", None,
                  f"移除示範班級：刪除 {len(removed)} 個、封存 {len(archived)} 個",
                  after={"removed": removed, "archived": archived})
        return {"removed": removed, "archived": archived}
    return run(do)


class EnrollIn(BaseModel):
    student_id: int
    status: Literal["active", "trial"] = "active"


@router.post("/admin/api/courses/{course_id}/enroll")
def enroll(course_id: int, body: EnrollIn, user: str = Depends(current_user)) -> dict:
    return run(lambda conn: {"id": _enroll(conn, user, body.student_id, course_id, body.status)})


class EnrollmentPatch(BaseModel):
    status: Literal["active", "trial", "dropped", "finished"]


@router.patch("/admin/api/enrollments/{enrollment_id}")
def update_enrollment(enrollment_id: int, body: EnrollmentPatch, user: str = Depends(current_user)) -> dict:
    def do(conn):
        e = _one(conn, """SELECT e.status, s.name AS student, c.name AS course FROM enrollments e
                            JOIN students s ON s.id = e.student_id JOIN courses c ON c.id = e.course_id
                           WHERE e.id = %s""", (enrollment_id,))
        ending = body.status in ("dropped", "finished")
        conn.execute("""UPDATE enrollments SET status = %s,
                               end_date = CASE WHEN %s THEN COALESCE(end_date, tw_today()) ELSE NULL END
                         WHERE id = %s""", (body.status, ending, enrollment_id))
        audit(conn, user, "drop" if ending else "update", "enrollments", enrollment_id,
              f"{e['student']} 在 {e['course']}：{ENROLL_STATUS[e['status']]} → {ENROLL_STATUS[body.status]}")
        return {"ok": True}
    return run(do)


@router.get("/admin/api/subjects")
def list_subjects(user: str = Depends(current_user)) -> list[dict]:
    return db.fetch_all("SELECT id, name FROM subjects ORDER BY id")


class SubjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=20)


@router.post("/admin/api/subjects")
def add_subject(body: SubjectIn, user: str = Depends(current_user)) -> dict:
    def do(conn):
        row = conn.execute("INSERT INTO subjects (name) VALUES (%s) RETURNING id, name", (body.name.strip(),)).fetchone()
        audit(conn, user, "insert", "subjects", row["id"], f"新增科目 {row['name']}")
        return row
    return run(do)


# ====================================================================== 圖片（公開，LINE 會來讀）
@router.get("/files/{name}", include_in_schema=False)
def file(name: str) -> Response:
    stem = name.removesuffix(".jpg")
    preview_img = stem.endswith("-preview")
    token = stem.removesuffix("-preview")
    data = media.load(token, preview=preview_img) if name.endswith(".jpg") else None
    if data is None:
        raise HTTPException(404, "not found")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

