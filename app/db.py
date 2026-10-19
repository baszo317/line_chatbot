"""資料庫存取。第一期用到的 SQL 全部集中在這裡，方便對照 sql/schema.sql。"""
from __future__ import annotations

import atexit
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


@atexit.register
def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params: Any = None) -> dict | None:
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: Any = None) -> None:
    with get_pool().connection() as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------- LINE 使用者
def ensure_line_user(user_id: str) -> bool:
    """記下這位使用者（最後互動時間）。回傳 True 表示還不知道他的 LINE 名稱（例如 bot 上線前就加好友的人）。"""
    row = fetch_one(
        """INSERT INTO line_users (line_user_id, last_active_at) VALUES (%s, now())
           ON CONFLICT (line_user_id) DO UPDATE SET last_active_at = now()
           RETURNING display_name IS NULL AS unknown""",
        (user_id,),
    )
    return bool(row and row["unknown"])


def save_profile(user_id: str, display_name: str | None, picture_url: str | None) -> None:
    execute(
        """UPDATE line_users SET display_name = COALESCE(%s, display_name), picture_url = COALESCE(%s, picture_url)
            WHERE line_user_id = %s""",
        (display_name, picture_url, user_id),
    )


def record_follow(user_id: str, display_name: str | None, picture_url: str | None) -> None:
    execute(
        """INSERT INTO line_users (line_user_id, display_name, picture_url, is_following,
                                   followed_at, last_active_at)
           VALUES (%s, %s, %s, true, now(), now())
           ON CONFLICT (line_user_id) DO UPDATE
              SET display_name  = COALESCE(EXCLUDED.display_name, line_users.display_name),
                  picture_url   = COALESCE(EXCLUDED.picture_url, line_users.picture_url),
                  is_following  = true,
                  followed_at   = now(),
                  unfollowed_at = NULL,
                  last_active_at = now()""",
        (user_id, display_name, picture_url),
    )


def record_unfollow(user_id: str) -> None:
    execute(
        "UPDATE line_users SET is_following = false, unfollowed_at = now() WHERE line_user_id = %s",
        (user_id,),
    )


def get_user(user_id: str) -> dict | None:
    return fetch_one(
        """SELECT line_user_id, display_name, bot_paused_until,
                  COALESCE(bot_paused_until > now(), false) AS paused
             FROM line_users WHERE line_user_id = %s""",
        (user_id,),
    )


def pause_bot(user_id: str, minutes: int) -> None:
    execute(
        "UPDATE line_users SET bot_paused_until = now() + make_interval(mins => %s) WHERE line_user_id = %s",
        (minutes, user_id),
    )


def resume_bot(user_id: str) -> None:
    execute("UPDATE line_users SET bot_paused_until = NULL WHERE line_user_id = %s", (user_id,))


# ---------------------------------------------------------------- 對話紀錄
def log_incoming(event_id: str | None, user_id: str, message_type: str, content: str | None) -> int | None:
    """寫入收到的訊息。回傳紀錄 id；若是 LINE 重送的同一事件（webhookEventId 重複）則回傳 None。"""
    row = fetch_one(
        """INSERT INTO message_logs (line_user_id, webhook_event_id, direction, message_type, content)
           VALUES (%s, %s, 'in', %s, %s)
           ON CONFLICT (webhook_event_id) DO NOTHING
           RETURNING id""",
        (user_id, event_id, message_type, content),
    )
    return row["id"] if row else None


def mark_incoming(log_id: int, *, intent: str | None = None, handled_by: str = "ai",
                  needs_human: bool = False) -> None:
    execute(
        "UPDATE message_logs SET intent = %s, handled_by = %s, needs_human = %s WHERE id = %s",
        (intent, handled_by, needs_human, log_id),
    )


def log_outgoing(user_id: str, content: str, *, intent: str | None, handled_by: str,
                 needs_human: bool = False, model: str | None = None,
                 latency_ms: int | None = None, detail: dict | None = None, reply_to: int | None = None) -> None:
    """reply_to：回覆的是哪一則家長訊息（message_logs.id）。"""
    execute(
        """INSERT INTO message_logs (line_user_id, direction, message_type, content, intent,
                                     handled_by, needs_human, model, latency_ms, tool_calls, reply_to)
           VALUES (%s, 'out', 'text', %s, %s, %s, %s, %s, %s, %s, %s)""",
        (user_id, content, intent, handled_by, needs_human, model, latency_ms,
         Jsonb(detail) if detail else None, reply_to),
    )


def recent_dialogue(user_id: str, before_id: int, minutes: int = 30, limit: int = 8) -> list[dict]:
    """最近 30 分鐘內的對話，讓 AI 看得懂「那英文呢？」這種追問。"""
    rows = fetch_all(
        """SELECT direction, content FROM message_logs
            WHERE line_user_id = %s AND id < %s
              AND message_type = 'text' AND content IS NOT NULL
              AND created_at > now() - make_interval(mins => %s)
            ORDER BY id DESC LIMIT %s""",
        (user_id, before_id, minutes, limit),
    )
    return list(reversed(rows))


# ---------------------------------------------------------------- 公開資料（第一期）
def kb_articles() -> list[dict]:
    """image_token：有附圖時是圖片代號（media_files.token），沒有就是 None。"""
    return fetch_all(
        """SELECT a.id, a.category, a.title, a.content, a.keywords, m.token AS image_token
             FROM kb_articles a
             LEFT JOIN media_files m ON m.id = a.image_id
            WHERE a.is_active AND a.audience = 'public'
            ORDER BY a.sort_order, a.id"""
    )


def active_announcements() -> list[dict]:
    """目前有效的公告（含行事曆）。最多 40 則：太多的話 AI 讀不完，家長也看不完。"""
    return fetch_all(
        """SELECT n.id, n.title, n.body, n.category, n.grades, n.starts_at, n.ends_at, n.is_pinned,
                  m.token AS image_token
             FROM v_active_announcements n
             LEFT JOIN media_files m ON m.id = n.image_id
            WHERE n.audience IN ('all', 'guardian')
            ORDER BY n.is_pinned DESC, n.starts_at DESC, n.id DESC
            LIMIT 40"""
    )


def subject_names() -> list[str]:
    return [r["name"] for r in fetch_all("SELECT name FROM subjects ORDER BY id")]


def public_timetable() -> list[dict]:
    return fetch_all(
        """SELECT course_id, course_name, subject, grade, grade_label, weekday,
                  start_time, end_time, room, fee, start_date, end_date, description
             FROM v_public_timetable
            ORDER BY grade NULLS LAST, course_name, weekday, start_time"""
    )


def honor_roll(limit: int = 20) -> list[dict]:
    return fetch_all(
        """SELECT school_year, category, display_name, school_name, achievement
             FROM honor_roll
            WHERE is_published
            ORDER BY school_year DESC, sort_order, id
            LIMIT %s""",
        (limit,),
    )


# ---------------------------------------------------------------- 家長綁定（binding_codes、guardians.line_user_id）
def guardian_for_line(user_id: str) -> dict | None:
    """這個 LINE 綁定的家長（沒綁定回傳 None）。"""
    return fetch_one(
        """SELECT g.id AS guardian_id, g.name AS guardian_name, g.family_id
             FROM guardians g
            WHERE g.line_user_id = %s AND g.is_active""",
        (user_id,),
    )


def redeem_binding_code(code: str, user_id: str) -> str:
    """回傳 guardian／student／staff（成功），或 invalid／used／expired／line_already_bound（失敗）。"""
    return fetch_one("SELECT redeem_binding_code(%s, %s) AS r", (code, user_id))["r"]


def family_children(family_id: int) -> list[dict]:
    """家庭裡在學的孩子，和每個孩子現在上的班（時段合併成「週一、週三 18:30–20:30」）。"""
    kids = fetch_all(
        """SELECT id, name, current_grade_label AS grade_label FROM v_students
            WHERE family_id = %s AND status <> 'left' ORDER BY current_grade DESC, name""",
        (family_id,),
    )
    if not kids:
        return []
    rows = fetch_all(
        """SELECT student_id, course_id, course_name, subject, weekday, start_time, end_time, room, enrollment_status
             FROM v_student_timetable
            WHERE student_id = ANY(%s)
            ORDER BY course_name, weekday, start_time""",
        ([k["id"] for k in kids],),
    )
    for k in kids:
        classes: dict[int, dict] = {}
        for r in rows:
            if r["student_id"] != k["id"]:
                continue
            c = classes.setdefault(r["course_id"], {"name": r["course_name"], "room": r["room"],
                                                    "trial": r["enrollment_status"] == "trial", "slots": {}})
            span = f"{r['start_time']:%H:%M}–{r['end_time']:%H:%M}"
            c["slots"].setdefault(span, []).append("一二三四五六日"[r["weekday"] - 1])
        k["classes"] = [{"name": c["name"], "room": c["room"], "trial": c["trial"],
                         "schedule": "；".join("週" + "、週".join(d) + f" {s}" for s, d in c["slots"].items())}
                        for c in classes.values()]
    return kids
