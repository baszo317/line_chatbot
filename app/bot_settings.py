"""後台「設定」頁的機器人設定，存在資料庫的 bot_settings 表（key → JSON 值）。

welcome_on_follow：加好友時 bot 要不要傳歡迎詞。官方帳號後台的「加入好友的歡迎訊息」開著的話，關掉這個。
notify_unanswered：機器人答不出來時也通知員工（關掉的話，只有家長按「找真人客服」才通知）。
"""
from __future__ import annotations

import logging
import time

import psycopg
from psycopg.types.json import Jsonb

from . import db

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "welcome_on_follow": True,
    "notify_unanswered": True,
}
CACHE_SECONDS = 30
_cache: dict = {"at": float("-inf"), "values": dict(DEFAULTS)}


def values() -> dict:
    now = time.monotonic()
    if now - _cache["at"] < CACHE_SECONDS:
        return _cache["values"]
    merged = dict(DEFAULTS)
    try:
        for r in db.fetch_all("SELECT key, value FROM bot_settings"):
            if r["key"] in DEFAULTS:
                merged[r["key"]] = r["value"]
    except psycopg.Error as e:            # 資料庫還沒升級（沒有 bot_settings 表）→ 用預設值
        log.warning("讀不到 bot_settings，先用預設值：%s", e)
    _cache.update(at=now, values=merged)
    return merged


def get(key: str):
    return values()[key]


def save(conn, changes: dict, actor: str) -> None:
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        conn.execute(
            """INSERT INTO bot_settings (key, value, updated_by, updated_at) VALUES (%s, %s, %s, now())
               ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (key, Jsonb(value), actor))
    invalidate()


def invalidate() -> None:
    _cache["at"] = float("-inf")
