"""廣播：後台用 LINE 推播訊息給家長（全體好友、某個年級、某一班）。

・全體好友：LINE 的 broadcast，傳給所有加官方帳號好友的人（包含還沒綁定的家長）。
・某個年級／某一班：LINE 的 multicast（每次最多 500 人），只傳給「孩子在那個年級／那一班（在學、在班或試聽），
  而且已經綁定 LINE」的家長；還沒綁定的家長收不到，後台會列出是哪些學生。
推播會用掉官方帳號每月的訊息額度：傳給幾個人就用掉幾則（回覆家長的訊息不算）。
同一個 request_id 只會傳一次（後台重送、按兩下都不會重複傳）。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from . import db, media
from .line_api import MULTICAST_MAX, LineError, text_message

log = logging.getLogger(__name__)

GRADE_LABEL = {6: "國小", 7: "國一", 8: "國二", 9: "國三"}


@dataclass
class Audience:
    kind: str                                  # all／grade／course
    label: str                                 # 全體好友／國二家長／國二數學 A 班家長
    grade: int | None = None
    course_id: int | None = None
    line_ids: list[str] = field(default_factory=list)   # 年級、班級：要傳的家長 LINE
    students: int = 0                          # 年級、班級：對象學生人數
    missing: list[str] = field(default_factory=list)    # 家長還沒綁定 LINE 的學生（收不到）


class BroadcastError(ValueError):
    """不能傳（訊息會直接顯示在後台）。"""


def audience(kind: str, grade: int | None = None, course_id: int | None = None) -> Audience:
    if kind == "all":
        return Audience("all", "全體好友")
    if kind == "grade":
        if grade not in GRADE_LABEL:
            raise BroadcastError("請選年級")
        students = db.fetch_all("""SELECT id, name, family_id FROM v_students
                                    WHERE status = 'active' AND current_grade = %s ORDER BY name""", (grade,))
        aud = Audience("grade", f"{GRADE_LABEL[grade]}家長", grade=grade)
    elif kind == "course":
        course = db.fetch_one("SELECT id, name FROM courses WHERE id = %s", (course_id,)) if course_id else None
        if not course:
            raise BroadcastError("請選班級")
        students = db.fetch_all(
            """SELECT DISTINCT s.id, s.name, s.family_id
                 FROM enrollments e JOIN students s ON s.id = e.student_id AND s.status = 'active'
                WHERE e.course_id = %s AND e.status IN ('trial','active')
                  AND (e.end_date IS NULL OR e.end_date >= tw_today())
                ORDER BY s.name""", (course["id"],))
        aud = Audience("course", f"{course['name']}家長", course_id=course["id"])
    else:
        raise BroadcastError("請選對象")
    families = sorted({s["family_id"] for s in students})
    bound = db.fetch_all(
        """SELECT DISTINCT g.family_id, g.line_user_id
             FROM guardians g JOIN line_users lu ON lu.line_user_id = g.line_user_id AND lu.is_following
            WHERE g.is_active AND g.family_id = ANY(%s::bigint[])""", (families,))
    reached = {r["family_id"] for r in bound}
    aud.line_ids = sorted({r["line_user_id"] for r in bound})
    aud.students = len(students)
    aud.missing = [s["name"] for s in students if s["family_id"] not in reached]
    return aud


def line_messages(body: str, image_token: str | None, base: str | None) -> list[dict]:
    messages = [text_message(body)]
    if image_token:
        messages.append(media.line_image(image_token, base or ""))
    return messages


def send(line, *, request_id: str, aud: Audience, body: str, image_id: int | None,
         announcement_id: int | None, base: str | None, actor: str) -> dict:
    """建立並推播一筆廣播。同一個 request_id 只會傳一次。"""
    old = db.fetch_one("SELECT id FROM broadcasts WHERE request_id = %s", (request_id,))
    if old:
        return get(old["id"])
    if aud.kind != "all" and not aud.line_ids:
        raise BroadcastError(f"{aud.label.removesuffix('家長')}還沒有家長綁定 LINE，沒有人收得到")
    image = db.fetch_one("SELECT token FROM media_files WHERE id = %s", (image_id,)) if image_id else None
    if image and not (base and base.startswith("https://")):
        raise BroadcastError("還不知道 bot 對外的 https 網址，圖片傳不出去。可以先拿掉圖片，"
                             "或等 LINE 傳一則訊息進來（或在 .env 設定 BOT_PUBLIC_BASE_URL）之後再傳")
    row = db.fetch_one(
        """INSERT INTO broadcasts (request_id, audience, grade, course_id, audience_label, body, image_id,
                                   announcement_id, recipients, sent_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (request_id) DO NOTHING
           RETURNING id""",
        (request_id, aud.kind, aud.grade, aud.course_id, aud.label, body, image_id, announcement_id,
         None if aud.kind == "all" else len(aud.line_ids), actor))
    if row is None:                       # 另一個請求剛好同時建立了同一筆
        return get(db.fetch_one("SELECT id FROM broadcasts WHERE request_id = %s", (request_id,))["id"])
    messages = line_messages(body, image["token"] if image else None, base)
    sent, error = 0, None
    try:
        if aud.kind == "all":
            line.broadcast(messages, retry_key=request_id)
        else:
            for i in range(0, len(aud.line_ids), MULTICAST_MAX):
                chunk = aud.line_ids[i:i + MULTICAST_MAX]
                key = request_id if i == 0 else str(uuid.uuid5(uuid.UUID(request_id), str(i)))
                line.multicast(chunk, messages, retry_key=key)
                sent += len(chunk)
    except LineError as e:
        error = str(e)
    status = "sent" if error is None else ("partial" if sent else "failed")
    db.execute("""UPDATE broadcasts SET status = %s, error = %s,
                                        sent_at = CASE WHEN %s <> 'failed' THEN now() END WHERE id = %s""",
               (status, error, status, row["id"]))
    if status != "failed" and announcement_id:
        db.execute("UPDATE announcements SET pushed_at = now() WHERE id = %s", (announcement_id,))
    return get(row["id"])


SELECT = """
SELECT b.id, b.audience, b.grade, b.course_id, b.audience_label, b.body, b.recipients, b.status, b.error,
       b.sent_by, b.created_at, b.sent_at, b.announcement_id, m.token AS image
  FROM broadcasts b LEFT JOIN media_files m ON m.id = b.image_id
"""


def get(bid: int) -> dict:
    return db.fetch_one(SELECT + " WHERE b.id = %s", (bid,))


def history(limit: int = 50) -> list[dict]:
    return db.fetch_all(SELECT + " ORDER BY b.id DESC LIMIT %s", (limit,))
