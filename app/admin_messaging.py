"""後台：廣播、收通知的員工、LINE 訊息額度。

廣播和通知員工都是 LINE 的推播，會用掉官方帳號每月的訊息額度；回覆家長的訊息不算。
"""
from __future__ import annotations

import base64
import binascii
import logging
import uuid
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import broadcast, db, media
from .admin import _one, audit, current_user, line_users, run
from .config import settings
from .line_api import LineApi, LineError, text_message

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api")


def line_client() -> LineApi:      # 測試時換成假的
    return LineApi()


def public_base() -> str | None:
    base = settings.public_base_url or media.LAST_PUBLIC_BASE["url"]
    return base if base and base.startswith("https://") else None


# ====================================================================== 訊息額度
@router.get("/line/quota")
def quota(user: str = Depends(current_user)) -> dict:
    """這個月用了幾則、上限幾則、大約有幾位好友。"""
    if not settings.line_channel_access_token:
        return {"ok": False, "error": "沒有設定 LINE_CHANNEL_ACCESS_TOKEN"}
    client = line_client()
    try:
        q = client.quota()
    except LineError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **q, "friends": client.friend_count()}


# ====================================================================== 廣播
@router.get("/broadcasts")
def list_broadcasts(user: str = Depends(current_user)) -> dict:
    return {"items": broadcast.history(), "image_ok": public_base() is not None}


@router.get("/broadcasts/audience")
def preview_audience(kind: str, grade: int | None = None, course_id: int | None = None,
                     user: str = Depends(current_user)) -> dict:
    try:
        aud = broadcast.audience(kind, grade, course_id)
    except broadcast.BroadcastError as e:
        raise HTTPException(400, str(e)) from None
    return {"label": aud.label, "recipients": None if aud.kind == "all" else len(aud.line_ids),
            "students": aud.students, "missing": aud.missing}


class ImageIn(BaseModel):
    data: str = Field(max_length=30_000_000)         # base64（可以是 data: 網址）
    name: str | None = Field(default=None, max_length=200)


class BroadcastIn(BaseModel):
    request_id: uuid.UUID                            # 後台按「傳送」時產生；重送同一個不會傳兩次
    kind: Literal["all", "grade", "course"]
    grade: int | None = None
    course_id: int | None = None
    body: str = Field(max_length=4800)
    image: ImageIn | None = None                     # 新選的圖片
    notice_key: str | None = Field(default=None, max_length=500)   # 從公告帶入（用公告的圖片）


@router.post("/broadcasts")
def send_broadcast(body: BroadcastIn, user: str = Depends(current_user)) -> dict:
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "請寫要傳的內容")
    try:
        aud = broadcast.audience(body.kind, body.grade, body.course_id)
    except broadcast.BroadcastError as e:
        raise HTTPException(400, str(e)) from None
    announcement_id = image_id = None
    if body.notice_key:
        row = db.fetch_one("SELECT id, image_id FROM announcements WHERE source_path = %s", (body.notice_key,))
        if row:      # 公告的圖片：知道 bot 的 https 網址才一起傳（LINE 只收 https 圖片）
            announcement_id, image_id = row["id"], row["image_id"] if public_base() else None
    if body.image:
        try:
            raw = base64.b64decode(body.image.data.split(",", 1)[-1], validate=False)
            image_id = media.save(raw, original_name=body.image.name, uploaded_by=user, note="廣播")["id"]
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, str(e) if isinstance(e, media.ImageError) else "圖片讀不出來，請重新選一次") from None
    try:
        row = broadcast.send(line_client(), request_id=str(body.request_id), aud=aud, body=text, image_id=image_id,
                             announcement_id=announcement_id, base=public_base(), actor=user)
    except broadcast.BroadcastError as e:
        raise HTTPException(400, str(e)) from None
    count = "全體好友" if row["recipients"] is None else f"{row['recipients']} 位家長"
    state = {"sent": "已傳送", "partial": "只傳了一部分", "failed": "沒有傳出去"}.get(row["status"], row["status"])
    run(lambda conn: audit(conn, user, "insert", "broadcasts", row["id"],
                           f"廣播給{row['audience_label']}（{count}）：{state}", after={"body": text}))
    return row


# ====================================================================== 收通知的員工
STAFF_SELECT = """
SELECT s.id, s.name, s.notify_human, s.line_user_id IS NOT NULL AS line,
       lu.display_name AS line_name, lu.picture_url, COALESCE(lu.is_following, false) AS following
  FROM staff s LEFT JOIN line_users lu ON lu.line_user_id = s.line_user_id
"""


@router.get("/staff")
def list_staff(user: str = Depends(current_user)) -> list[dict]:
    return db.fetch_all(STAFF_SELECT + " WHERE s.is_active ORDER BY s.id")


@router.get("/staff/line-candidates")
def staff_line_candidates(q: str = "", user: str = Depends(current_user)) -> list[dict]:
    """可以設成員工的 LINE（最近傳訊息的在前）。已經是員工的不能選。"""
    rows = line_users(q, limit=30)
    for c in rows:
        guardian = c.pop("guardian_name")
        family, active = c.pop("guardian_family"), c.pop("guardian_active")
        c.pop("student_name")
        c["hints"] = []
        c["selectable"] = c["staff_name"] is None
        c["bound_text"] = (f"已經是員工：{c['staff_name']}" if c["staff_name"] else
                           f"家長：{guardian}（{family}）" if guardian and active else None)
    rows.sort(key=lambda c: not c["selectable"])
    return rows


class StaffIn(BaseModel):
    name: str | None = Field(default=None, max_length=30)
    line_user_id: str | None = Field(default=None, pattern=r"^U[0-9a-f]{32}$")
    notify_human: bool | None = None


def _check_line(conn, uid: str) -> dict:
    lu = conn.execute("SELECT display_name, is_following FROM line_users WHERE line_user_id = %s", (uid,)).fetchone()
    if not lu:
        raise HTTPException(404, "找不到這個 LINE：要先用這個 LINE 加官方帳號好友，並傳一則訊息")
    if not lu["is_following"]:
        raise HTTPException(400, "這個 LINE 已經封鎖或刪除官方帳號")
    other = conn.execute("SELECT name FROM staff WHERE line_user_id = %s", (uid,)).fetchone()
    if other:
        raise HTTPException(400, f"這個 LINE 已經是員工 {other['name']} 了")
    return lu


@router.post("/staff")
def add_staff(body: StaffIn, user: str = Depends(current_user)) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "請填員工姓名")
    if not body.line_user_id:
        raise HTTPException(400, "請選這位員工的 LINE")

    def do(conn):
        lu = _check_line(conn, body.line_user_id)
        row = conn.execute("""INSERT INTO staff (name, role, line_user_id, notify_human)
                              VALUES (%s, 'counter', %s, %s) RETURNING id""",
                           (name, body.line_user_id, body.notify_human is not False)).fetchone()
        audit(conn, user, "insert", "staff", row["id"], f"新增收通知的員工 {name}（LINE 名稱：{lu['display_name'] or '沒有'}）")
        return {"id": row["id"]}
    return run(do)


@router.patch("/staff/{staff_id}")
def update_staff(staff_id: int, body: StaffIn, user: str = Depends(current_user)) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and not (fields["name"] or "").strip():
        raise HTTPException(400, "員工姓名不能空白")

    def do(conn):
        s = _one(conn, "SELECT name, line_user_id FROM staff WHERE id = %s AND is_active", (staff_id,))
        if fields.get("line_user_id") and fields["line_user_id"] != s["line_user_id"]:
            _check_line(conn, fields["line_user_id"])
        sets = {k: (v.strip() if isinstance(v, str) and k == "name" else v) for k, v in fields.items()
                if k in ("name", "line_user_id", "notify_human") and v is not None}
        if sets:
            conn.execute(f"UPDATE staff SET {', '.join(f'{k} = %s' for k in sets)} WHERE id = %s",
                         (*sets.values(), staff_id))
            audit(conn, user, "update", "staff", staff_id, f"修改收通知的員工 {s['name']}", after=sets)
        return {"ok": True}
    return run(do)


@router.delete("/staff/{staff_id}")
def remove_staff(staff_id: int, user: str = Depends(current_user)) -> dict:
    def do(conn):
        s = _one(conn, "SELECT name FROM staff WHERE id = %s", (staff_id,))
        try:
            with conn.transaction():
                conn.execute("DELETE FROM staff WHERE id = %s", (staff_id,))
        except psycopg.errors.ForeignKeyViolation:      # 有其他資料用到（第二期的請假、成績……）：改成離職
            conn.execute("""UPDATE staff SET is_active = false, notify_human = false, line_user_id = NULL
                             WHERE id = %s""", (staff_id,))
        audit(conn, user, "delete", "staff", staff_id, f"移除收通知的員工 {s['name']}")
        return {"ok": True}
    return run(do)


@router.post("/staff/{staff_id}/test")
def test_staff(staff_id: int, user: str = Depends(current_user)) -> dict:
    """傳一則測試通知給這位員工（會用掉 1 則訊息額度）。"""
    s = db.fetch_one("SELECT name, line_user_id FROM staff WHERE id = %s AND is_active", (staff_id,))
    if not s or not s["line_user_id"]:
        raise HTTPException(400, "這位員工沒有綁 LINE")
    try:
        line_client().push(s["line_user_id"], [text_message(
            f"【測試】{settings.school_name}的 LINE 機器人通知。家長需要真人時，會像這樣傳訊息給你。")])
    except LineError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.get("/alerts")
def recent_alerts(limit: int = 30, user: str = Depends(current_user)) -> list[dict]:
    """最近的「需要真人」通知（設定頁看得到通知有沒有傳出去）。"""
    return db.fetch_all("""SELECT id, reason, summary, sent_to, error, created_at FROM staff_alerts
                            ORDER BY id DESC LIMIT %s""", (min(max(limit, 1), 100),))

