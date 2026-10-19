"""家長需要真人時，用 LINE 通知員工。

什麼時候：家長按「找真人客服」、AI 暫停中家長又傳訊息、綁定碼不對，或機器人答不出來
（「答不出來」可以在後台「設定」關掉，只留「找真人客服」）。
同一位家長 30 分鐘內只通知一次；之前通知的是「答不出來」、後來家長按了「找真人客服」，會再通知一次。

通知是 push 訊息：每通知一位員工，就用掉 1 則官方帳號每月的訊息額度（回覆家長的訊息不算）。
員工要先用自己的 LINE 加官方帳號好友、傳一句話，再到後台「設定」把這個 LINE 加進「收通知的員工」。
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone

from . import bot_settings, db
from .line_api import text_message

log = logging.getLogger(__name__)
TW = timezone(timedelta(hours=8))
WINDOW_MINUTES = 30
WHY = {"handoff": "按了「找真人客服」", "waiting": "在等真人回覆", "binding": "綁定沒有成功",
       "unanswered": "問了機器人答不出來的問題"}


def parent(user_id: str) -> dict:
    """這個 LINE 是誰：綁定的家長和孩子，或 LINE 名稱。"""
    row = db.fetch_one(
        """SELECT lu.display_name, g.name AS guardian, g.family_id
             FROM line_users lu
             LEFT JOIN guardians g ON g.line_user_id = lu.line_user_id AND g.is_active
            WHERE lu.line_user_id = %s""", (user_id,)) or {}
    kids = db.fetch_all(
        """SELECT name, current_grade_label AS grade FROM v_students
            WHERE family_id = %s AND status <> 'left' ORDER BY current_grade DESC, name""",
        (row["family_id"],)) if row.get("family_id") else []
    return {"display_name": row.get("display_name"), "guardian": row.get("guardian"), "kids": kids}


def who_text(who: dict) -> str:
    if who.get("guardian"):
        kids = "、".join(f"{k['name']} {k['grade']}" for k in who["kids"])
        return who["guardian"] + (f"（{kids}）" if kids else "")
    return f"LINE「{who.get('display_name') or '沒有名稱'}」（還沒綁定）"


def recent_messages(user_id: str, limit: int = 4) -> list[dict]:
    rows = db.fetch_all(
        """SELECT content, created_at FROM message_logs
            WHERE line_user_id = %s AND direction = 'in' AND message_type = 'text' AND content IS NOT NULL
              AND created_at > now() - make_interval(mins => %s)
            ORDER BY id DESC LIMIT %s""", (user_id, WINDOW_MINUTES, limit))
    return rows[::-1]


def summary(why: str, who: dict, messages: list[dict]) -> str:
    """第一行：誰、為什麼；後面：最近的訊息（台灣時間）。"""
    lines = [f"{who_text(who)} {WHY[why]}"]
    lines += [f"{m['created_at'].astimezone(TW):%H:%M} {m['content'][:300]}" for m in messages]
    return "\n".join(lines)


def claim(user_id: str, reason: str, message_id: int | None, text: str) -> int | None:
    """記一筆「需要真人」。同一位家長最近已經通知過就回傳 None（不再通知）。"""
    with db.get_pool().connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user_id,))
        recent = {r["reason"] for r in conn.execute(
            """SELECT reason FROM staff_alerts
                WHERE line_user_id = %s AND created_at > now() - make_interval(mins => %s)""",
            (user_id, WINDOW_MINUTES)).fetchall()}
        if "handoff" in recent or (recent and reason == "unanswered"):
            return None
        return conn.execute(
            """INSERT INTO staff_alerts (line_user_id, reason, message_id, summary) VALUES (%s, %s, %s, %s)
               RETURNING id""", (user_id, reason, message_id, text)).fetchone()["id"]


def recipients() -> list[dict]:
    """要通知的員工：在職、勾了「收通知」、綁了 LINE，而且沒有封鎖官方帳號。"""
    return db.fetch_all(
        """SELECT s.id, s.name, s.line_user_id FROM staff s
             JOIN line_users lu ON lu.line_user_id = s.line_user_id AND lu.is_following
            WHERE s.is_active AND s.notify_human ORDER BY s.id""")


def alert_message(text: str) -> dict:
    return text_message(f"【需要真人】{text}\n\n請到 LINE 官方帳號的聊天室回覆家長。")


def notify(line, user_id: str, message_id: int | None, why: str) -> int | None:
    """家長需要真人：用 LINE 通知員工。回傳 staff_alerts.id；這次不通知就回傳 None。不會丟出例外。"""
    reason = "handoff" if why in ("handoff", "waiting") else "unanswered"
    try:
        if reason == "unanswered" and not bot_settings.get("notify_unanswered"):
            return None
        staff = recipients()
        if not staff:
            return None
        text = summary(why, parent(user_id), recent_messages(user_id))
        alert_id = claim(user_id, reason, message_id, text)
        if alert_id is None:
            return None
        sent, errors = 0, []
        for s in staff:
            try:
                line.push(s["line_user_id"], [alert_message(text)])
                sent += 1
            except Exception as e:  # noqa: BLE001 — 一位員工失敗不影響其他人
                errors.append(f"{s['name']}：{e}")
        db.execute("UPDATE staff_alerts SET sent_to = %s, error = %s WHERE id = %s",
                   (sent, "；".join(errors) or None, alert_id))
        return alert_id
    except Exception:  # noqa: BLE001 — 通知失敗不能影響回覆家長
        log.exception("通知員工失敗")
        return None
