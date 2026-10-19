"""測試共用設定。

測試會清空 line_users 與 message_logs，所以一定要用「名稱裡有 test」的資料庫：
    TEST_DATABASE_URL=postgresql://postgres:密碼@localhost:5432/gaoming_test pytest
資料庫是空的時候，會自動建立 schema、匯入示範資料。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/gaoming_test")
os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "test-token"
os.environ["BOT_AI_MODE"] = "offline"
os.environ.pop("BOT_ANTHROPIC_BASE_URL", None)
os.environ["BOT_CONTENT_DIR"] = "off"            # 資料夾同步的測試用暫存資料夾（tests/test_content.py）
os.environ["BOT_ADMIN_PASSWORD"] = "test-admin-password"
os.environ.pop("BOT_PUBLIC_BASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

if "test" not in os.environ["DATABASE_URL"].rsplit("/", 1)[-1]:
    raise SystemExit("為了保護正式資料，TEST_DATABASE_URL 的資料庫名稱必須包含 test")

from app import bot_settings, db, knowledge, migrate  # noqa: E402

USER = "U" + "ab" * 16


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    if not db.fetch_one("SELECT to_regclass('public.kb_articles') AS t")["t"]:
        for sql_file in ("sql/schema.sql", "sql/phase1_seed.sql"):
            db.execute((ROOT / sql_file).read_text("utf-8"))
        from import_kb import import_file
        import_file(str(ROOT / "data" / "kb_template.csv"))
    elif not migrate.is_up_to_date():      # 以前建立的測試資料庫：升級到最新版，再換成新的示範資料
        migrate.upgrade()
        db.execute((ROOT / "sql" / "phase1_seed.sql").read_text("utf-8"))
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    db.execute("DELETE FROM broadcasts")
    db.execute("DELETE FROM staff")
    db.execute("DELETE FROM message_logs")
    db.execute("DELETE FROM binding_codes")
    db.execute("UPDATE guardians SET line_user_id = NULL")
    db.execute("DELETE FROM line_users")
    db.execute("DELETE FROM announcements")
    db.execute("DELETE FROM bot_settings")
    knowledge.invalidate()
    bot_settings.invalidate()
    yield


class FakeLine:
    """代替真正的 LINE API：把要送出的回覆、推播記下來，讓測試檢查。"""

    def __init__(self):
        self.replies: list[tuple[str, list[dict]]] = []
        self.pushes: list[tuple[str, list[dict]]] = []       # (傳給誰：LINE ID／multicast:…／broadcast, 訊息)
        self.retry_keys: list[str | None] = []
        self.loading = 0
        self.fail_with = None                                # 設成 LineError 時，推播會失敗

    def reply(self, reply_token: str, messages: list[dict]) -> None:
        self.replies.append((reply_token, messages))

    def _push(self, to: str, messages: list[dict], retry_key: str | None) -> None:
        if self.fail_with:
            raise self.fail_with
        self.pushes.append((to, messages))
        self.retry_keys.append(retry_key)

    def push(self, to: str, messages: list[dict], retry_key: str | None = None) -> None:
        self._push(to, messages, retry_key)

    def multicast(self, to: list[str], messages: list[dict], retry_key: str | None = None) -> None:
        self._push("multicast:" + ",".join(to), messages, retry_key)

    def broadcast(self, messages: list[dict], retry_key: str | None = None) -> None:
        self._push("broadcast", messages, retry_key)

    def quota(self) -> dict:
        return {"limit": 200, "used": 12}

    def friend_count(self) -> int | None:
        return 87

    def pushed_texts(self) -> list[str]:
        return [m["text"] for _, msgs in self.pushes for m in msgs if m.get("type") == "text"]

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        self.loading += 1

    def get_profile(self, user_id: str) -> dict:
        return {"displayName": "測試家長"}

    def texts(self) -> list[str]:
        return [m["text"] for _, msgs in self.replies for m in msgs]

    def quick_replies(self, i: int = -1) -> list[str]:
        msg = self.replies[i][1][0]
        return [item["action"]["label"] for item in (msg.get("quickReply") or {}).get("items", [])]


@pytest.fixture
def line() -> FakeLine:
    return FakeLine()


_counter = {"n": 0}


def make_event(etype: str = "message", text: str | None = None, *, token: str | None = None,
               event_id: str | None = None, user: str = USER, source_type: str = "user",
               mode: str = "active", message: dict | None = None) -> dict:
    _counter["n"] += 1
    n = _counter["n"]
    event = {
        "type": etype, "mode": mode, "timestamp": 1760000000000 + n,
        "source": {"type": source_type, "userId": user},
        "webhookEventId": event_id or f"01TEST{n:020d}",
        "deliveryContext": {"isRedelivery": False},
    }
    if etype != "unfollow":
        event["replyToken"] = token or f"token-{n}"
    if etype == "message":
        event["message"] = message or {"type": "text", "id": str(n), "text": text}
    return event
