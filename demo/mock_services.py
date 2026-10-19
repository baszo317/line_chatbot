"""模擬「LINE 平台」和「Claude API」，讓你不用任何帳號就能在自己電腦上測試 bot。

- 模擬 LINE（port 9100）：收下 bot 的回覆、載入動畫、圖文選單請求，存在記憶體裡。
- 模擬 Claude（port 9200）：依 demo/replay.json 的預錄答案回應；
  沒有預錄的問題會回錯誤，bot 就會改用關鍵字比對（剛好可以看到備援機制）。
  每次收到的請求會存到 demo/output/last_claude_request.json，可以看 bot 實際送給 Claude 的內容。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DEMO_DIR = Path(__file__).resolve().parent
OUTPUT = DEMO_DIR / "output"

# ------------------------------------------------------------------ 模擬 LINE
line_app = FastAPI(title="模擬 LINE 平台")
REPLIES: dict[str, list[dict]] = {}
CALLS: list[dict] = []


@line_app.post("/v2/bot/message/reply")
async def reply(request: Request) -> dict:
    body = await request.json()
    REPLIES[body["replyToken"]] = body["messages"]
    CALLS.append({"api": "reply", "at": time.time()})
    return {}


@line_app.post("/v2/bot/chat/loading/start")
async def loading(request: Request) -> dict:
    CALLS.append({"api": "loading", "body": await request.json(), "at": time.time()})
    return {}


@line_app.get("/v2/bot/profile/{user_id}")
async def profile(user_id: str) -> dict:
    return {"userId": user_id, "displayName": "王媽媽", "pictureUrl": "", "language": "zh-TW"}


# 推播（廣播、通知員工）：記下來，並算進這個月用掉的額度
PUSHED = {"count": 0}


@line_app.post("/v2/bot/message/push")
async def push(request: Request) -> dict:
    CALLS.append({"api": "push", "body": await request.json()})
    PUSHED["count"] += 1
    return {}


@line_app.post("/v2/bot/message/multicast")
async def multicast(request: Request) -> dict:
    body = await request.json()
    CALLS.append({"api": "multicast", "body": body})
    PUSHED["count"] += len(body.get("to", []))
    return {}


@line_app.post("/v2/bot/message/broadcast")
async def broadcast(request: Request) -> dict:
    CALLS.append({"api": "broadcast", "body": await request.json()})
    PUSHED["count"] += 40
    return {}


@line_app.get("/v2/bot/message/quota")
async def quota() -> dict:
    return {"type": "limited", "value": 200}


@line_app.get("/v2/bot/message/quota/consumption")
async def consumption() -> dict:
    return {"totalUsage": 12 + PUSHED["count"]}


@line_app.get("/v2/bot/insight/followers")
async def followers(date: str = "") -> dict:
    return {"status": "ready", "followers": 42, "targetedReaches": 30, "blocks": 2}


@line_app.post("/v2/bot/richmenu")
async def create_rich_menu(request: Request) -> dict:
    CALLS.append({"api": "richmenu.create", "body": await request.json()})
    return {"richMenuId": "richmenu-demo-0001"}


@line_app.post("/v2/bot/richmenu/{menu_id}/content")
async def upload_rich_menu(menu_id: str, request: Request) -> dict:
    data = await request.body()
    CALLS.append({"api": "richmenu.upload", "menu": menu_id, "bytes": len(data),
                  "content_type": request.headers.get("content-type")})
    return {}


@line_app.post("/v2/bot/user/all/richmenu/{menu_id}")
async def set_default(menu_id: str) -> dict:
    CALLS.append({"api": "richmenu.default", "menu": menu_id})
    return {}


@line_app.get("/v2/bot/richmenu/list")
async def list_menus() -> dict:
    return {"richmenus": []}


# ------------------------------------------------------------------ 模擬 Claude
claude_app = FastAPI(title="模擬 Claude API")


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def load_replay() -> dict:
    data = json.loads((DEMO_DIR / "replay.json").read_text("utf-8"))
    return {_norm(q): a for q, a in data.items() if not q.startswith("_")}


REPLAY = load_replay()


@claude_app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "last_claude_request.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), "utf-8")
    content = body["messages"][-1]["content"]
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    question = content.strip().splitlines()[-1] if content.strip() else ""
    answer = REPLAY.get(_norm(question))
    if answer is None:
        return JSONResponse(status_code=400, content={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "模擬 Claude：這個問題沒有預錄答案"},
        })
    return {
        "id": "msg_demo_replay",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "claude-haiku-5-5"),
        "content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ------------------------------------------------------------------ 背景啟動
def serve(app: FastAPI, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError(f"port {port} 啟動失敗（可能已被其他程式占用）")
