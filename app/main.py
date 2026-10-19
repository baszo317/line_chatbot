"""程式入口。LINE 會把每一則訊息 POST 到 /callback。

啟動：uvicorn app.main:app --host 0.0.0.0 --port 8000

啟動時會：
  1. 自動升級資料庫（sql/ 裡還沒執行過的 00X_*.sql）
  2. 開始監看「補習班資料」資料夾（丟檔案就更新，見 app/content.py）
後台：http://localhost:8000/admin（要先在 .env 設定 BOT_ADMIN_PASSWORD）
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from . import admin, admin_content, admin_messaging, content, media, migrate
from .bot import Bot, is_https
from .config import settings
from .signature import verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gaoming")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_migrate:
        try:
            result = migrate.upgrade()
            if result.created_schema:
                log.info("資料庫是空的，已建立完整的資料表")
            elif result.applied:
                log.info("資料庫已自動升級：%s", "、".join(result.applied))
        except Exception:
            log.exception("資料庫自動升級失敗；可以手動執行 python scripts/upgrade_db.py 看詳細原因")
    poller = None
    if settings.content_dir is not None:
        poller = content.Poller(settings.content_poll_seconds).start()
        log.info("監看「補習班資料」資料夾：%s", settings.content_dir)
    yield
    if poller:
        poller.stop()


app = FastAPI(title=f"{settings.school_name} LINE 助理（第一期）", lifespan=lifespan)
app.include_router(admin.router)
app.include_router(admin_content.router)
app.include_router(admin_messaging.router)
bot = Bot()

if not settings.line_channel_secret or not settings.line_channel_access_token:
    log.warning("還沒設定 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN，請檢查 .env")
log.info("AI 模式：%s%s", settings.ai_mode,
         f"（{settings.claude_model}）" if settings.ai_mode == "claude" else "（只用關鍵字比對）")
if settings.admin_password:
    log.info("後台：http://localhost:8000/admin")
else:
    log.info("後台沒開：要用的話在 .env 加 BOT_ADMIN_PASSWORD=你的密碼")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ai_mode": settings.ai_mode,
            "model": settings.claude_model if settings.ai_mode == "claude" else None}


def public_base(request: Request) -> str | None:
    """bot 對外的 https 網址（傳圖片給 LINE 用）。.env 有設定就用設定；否則看 LINE 打進來時用的網址。"""
    if settings.public_base_url:
        return settings.public_base_url
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not host or host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "testserver"):
        return None
    base = f"https://{host}"
    if is_https(base):
        media.LAST_PUBLIC_BASE["url"] = base
    return base


@app.post("/callback")
async def callback(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()  # 原始 bytes，驗簽章要用這個
    if not verify_signature(body, request.headers.get("x-line-signature", ""), settings.line_channel_secret):
        log.warning("簽章不符，拒絕這個請求")
        raise HTTPException(status_code=400, detail="invalid signature")

    events = json.loads(body.decode("utf-8")).get("events", [])
    base = public_base(request)
    # 先回 200 給 LINE，再在背景慢慢處理（呼叫 AI 可能要幾秒）
    for event in events:
        background.add_task(_handle_safely, event, base)
    return {"ok": True, "events": len(events)}


def _handle_safely(event: dict, base: str | None = None) -> None:
    try:
        bot.handle_event(event, public_base=base)
    except Exception:
        log.exception("處理事件失敗：%s", event.get("webhookEventId"))
