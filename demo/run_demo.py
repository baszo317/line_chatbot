"""一鍵展示：同時啟動「模擬 LINE」「模擬 Claude」和你的 bot，跑完 6 個家長情境。

用法（在專案根目錄）：
    python demo/run_demo.py                 # 跑 6 個情境；AI 回覆來自 demo/replay.json（不需要 API key）
    python demo/run_demo.py --chat          # 自己打字和 bot 聊天
    python demo/run_demo.py --ai offline    # 不用 AI，只用關鍵字比對
    python demo/run_demo.py --ai real       # 用 .env 裡真正的 ANTHROPIC_API_KEY 呼叫 Claude

會用到 .env 的 DATABASE_URL（建議用測試用的資料庫）。
結果存到 demo/output/transcript.json，再跑 python demo/render_screenshots.py 就能產生截圖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

SECRET = "demo-channel-secret"
BOT_PORT = 8765
BOT_URL = f"http://127.0.0.1:{BOT_PORT}/callback"

SCENARIOS = [
    {"id": "case1", "title": "新家長加入好友", "steps": [{"event": "follow"}]},
    {"id": "case2", "title": "點圖文選單「課程介紹」", "steps": [{"text": "課程介紹"}]},
    {"id": "case3", "title": "自由提問：上課次數與學費", "steps": [{"text": "國二數學一週上幾次？學費多少？"}]},
    {"id": "case4", "title": "颱風停課", "steps": [{"text": "明天颱風會停課嗎？"}]},
    {"id": "case5", "title": "資料裡沒有的問題 → 轉真人",
     "steps": [{"text": "學費可以刷卡分期嗎？"}, {"text": "找真人客服"}, {"text": "刷卡會加手續費嗎？"}]},
    {"id": "case6", "title": "問到孩子的個人資料", "steps": [{"text": "我想看我家小孩最近的數學成績"}]},
]


def configure_env(ai: str) -> None:
    os.environ.update({
        "LINE_CHANNEL_SECRET": SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": "demo-access-token",
        "LINE_API_BASE": "http://127.0.0.1:9100",
        "LINE_DATA_API_BASE": "http://127.0.0.1:9100",
        "BOT_CONTENT_DIR": "off",      # 展示不碰「補習班資料」資料夾（示範的颱風公告才不會被匯出成檔案）
    })
    if ai == "replay":
        os.environ.update({"BOT_AI_MODE": "claude", "ANTHROPIC_API_KEY": "demo-key",
                           "BOT_ANTHROPIC_BASE_URL": "http://127.0.0.1:9200"})
    elif ai == "offline":
        os.environ["BOT_AI_MODE"] = "offline"
    else:
        os.environ["BOT_AI_MODE"] = "claude"


def user_id_for(n: int) -> str:
    return "U" + f"{n:02x}" * 16  # 每個情境用一位不同的家長（U + 32 個十六進位字元）


def new_event(user_id: str, etype: str, text: str | None = None) -> dict:
    event = {
        "type": etype,
        "mode": "active",
        "timestamp": int(time.time() * 1000),
        "source": {"type": "user", "userId": user_id},
        "webhookEventId": "01" + uuid.uuid4().hex[:24].upper(),
        "deliveryContext": {"isRedelivery": False},
        "replyToken": uuid.uuid4().hex,
    }
    if etype == "message":
        event["message"] = {"type": "text", "id": str(uuid.uuid4().int)[:18], "text": text}
    return event


def describe_path(path: dict | None, ai: str) -> str:
    """把資料庫裡的處理紀錄翻成一句話（截圖上的小標籤也用這個）。"""
    if not path:
        return "（沒有紀錄）"
    if path["handled_by"] == "staff":
        return "AI 暫停中：交給櫃台老師在後台回覆"
    if path["intent"] == "handoff":
        return "轉給櫃台老師（AI 暫停）" if path["needs_human"] else "回到 AI 助理"
    if path["handled_by"] == "ai":
        source = "模擬回應" if ai == "replay" else path.get("model") or "Claude"
        label = f"AI 回答（{source}）"
    elif (path.get("tool_calls") or {}).get("fallback"):
        tried_ai = (path.get("tool_calls") or {}).get("ai_mode") == "claude"
        label = "AI 沒有回應 → 改用關鍵字比對（備援）" if tried_ai else "關鍵字比對（AI 未啟用）"
    else:
        label = "固定回覆"
    if path["needs_human"]:
        label += "・建議轉真人"
    return label


class Runner:
    def __init__(self, ai: str):
        configure_env(ai)
        import logging
        logging.getLogger("app.ai").setLevel(logging.ERROR)  # 改用關鍵字的原因會顯示在標籤上
        for name in ("httpx", "httpx2", "anthropic", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.WARNING)
        import httpx
        import mock_services
        from app import db
        from app import main as bot_main
        from app.config import settings
        from app.signature import make_signature

        if ai == "real" and not settings.anthropic_api_key:
            raise SystemExit("--ai real 需要在 .env 設定 ANTHROPIC_API_KEY")
        self.ai, self.db, self.mock, self.sign = ai, db, mock_services, make_signature
        mock_services.serve(mock_services.line_app, 9100)
        if ai == "replay":
            mock_services.serve(mock_services.claude_app, 9200)
        mock_services.serve(bot_main.app, BOT_PORT)
        self.http = httpx.Client(timeout=10)

    def prepare_database(self, user_ids: list[str]) -> None:
        db = self.db
        if not db.fetch_one("SELECT count(*) AS n FROM kb_articles")["n"]:
            print("提醒：知識庫是空的，請先執行 python scripts/import_kb.py data/kb_template.csv")
        db.execute("DELETE FROM message_logs WHERE line_user_id = ANY(%s)", (user_ids,))
        db.execute("DELETE FROM line_users WHERE line_user_id = ANY(%s)", (user_ids,))
        # 示範用的颱風停課公告：「明天」停課，2 天後自動失效
        db.execute("DELETE FROM announcements WHERE title LIKE '【示範】颱風停課%'")
        db.execute(
            """INSERT INTO announcements (title, body, category, audience, starts_at, ends_at, is_pinned)
               VALUES ('【示範】颱風停課：' || to_char(tw_today() + 1, 'FMMM/FMDD') || ' 晚上課程停課',
                       '因颱風影響，明天（' || to_char(tw_today() + 1, 'FMMM/FMDD') || '）晚上所有課程停課，補課時間另行通知。',
                       'closure', 'all', now() - interval '1 minute', now() + interval '2 days', true)"""
        )

    def cleanup(self) -> None:
        """展示用的颱風停課公告只在展示時存在。結束就刪掉，免得留在資料庫裡讓真正的家長看到。"""
        self.db.execute("DELETE FROM announcements WHERE title LIKE '【示範】颱風停課%' AND source_path IS NULL")

    def _path(self, event_id: str) -> dict | None:
        return self.db.fetch_one(
            """SELECT i.intent, i.handled_by, i.needs_human, o.model, o.latency_ms, o.tool_calls
                 FROM message_logs i
                 LEFT JOIN LATERAL (
                      SELECT model, latency_ms, tool_calls FROM message_logs o
                       WHERE o.line_user_id = i.line_user_id AND o.direction = 'out' AND o.id > i.id
                       ORDER BY o.id LIMIT 1) o ON true
                WHERE i.webhook_event_id = %s AND i.intent IS NOT NULL""",
            (event_id,),
        )

    def send(self, user_id: str, step: dict) -> dict:
        etype = "follow" if step.get("event") == "follow" else "message"
        event = new_event(user_id, etype, step.get("text"))
        body = json.dumps({"destination": "U" + "f" * 32, "events": [event]}, ensure_ascii=False).encode("utf-8")
        r = self.http.post(BOT_URL, content=body,
                           headers={"x-line-signature": self.sign(body, SECRET), "content-type": "application/json"})
        r.raise_for_status()
        messages, path = None, None
        deadline = time.time() + 40
        while time.time() < deadline:
            path = self._path(event["webhookEventId"])
            if path and path["handled_by"] == "staff":
                break
            if path and event["replyToken"] in self.mock.REPLIES:
                messages = self.mock.REPLIES[event["replyToken"]]
                break
            time.sleep(0.1)
        bot = [{"text": m.get("text", ""),
                "quick_replies": [i["action"]["label"] for i in (m.get("quickReply") or {}).get("items", [])]}
               for m in messages or []]
        return {"user": step.get("text"), "event": etype, "bot": bot, "path": path,
                "path_label": describe_path(path, self.ai)}


def print_step(step: dict) -> None:
    print(f"👩 家長：{step['user']}" if step["user"] else "📥（家長加入好友）")
    if not step["bot"]:
        print("   （沒有回覆）")
    for m in step["bot"]:
        print("🤖 助理：" + m["text"].replace("\n", "\n        "))
        if m["quick_replies"]:
            print("   [按鈕] " + "｜".join(m["quick_replies"]))
    print(f"   └─ {step['path_label']}\n")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 終端機也能顯示表情符號
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ai", choices=["replay", "offline", "real"], default="replay")
    parser.add_argument("--chat", action="store_true", help="自己打字和 bot 聊天")
    parser.add_argument("--out", default=str(ROOT / "demo" / "output" / "transcript.json"), help="對話紀錄存到哪裡")
    args = parser.parse_args()

    runner = Runner(args.ai)
    chat_user = user_id_for(99)
    runner.prepare_database([user_id_for(i) for i in range(1, len(SCENARIOS) + 1)] + [chat_user])
    print(f"✅ 模擬環境已啟動（AI 模式：{args.ai}）\n")
    try:
        run(runner, args, chat_user)
    finally:
        runner.cleanup()


def run(runner: Runner, args, chat_user: str) -> None:
    """跑 6 個情境（或 --chat 自己打字）。"""
    if args.chat:
        print("輸入訊息和 bot 聊天，例如「課程介紹」「補習班在哪裡？」，輸入 exit 結束。")
        if args.ai == "replay":
            print("（展示模式只有 demo/replay.json 裡的問題會用 AI 回答，其他問題會改用關鍵字比對；"
                  "想讓 AI 回答任何問題，請改用 --ai real）")
        print()
        print_step(runner.send(chat_user, {"event": "follow"}))
        while True:
            try:
                text = input("👩 你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in ("exit", "quit", "q"):
                break
            if text:
                step = runner.send(chat_user, {"text": text})
                step["user"] = None
                for m in step["bot"]:
                    print("🤖 助理：" + m["text"].replace("\n", "\n        "))
                    if m["quick_replies"]:
                        print("   [按鈕] " + "｜".join(m["quick_replies"]))
                print(f"   └─ {step['path_label']}\n")
        return

    cases = []
    for n, scenario in enumerate(SCENARIOS, start=1):
        print(f"════ 案例 {n}｜{scenario['title']} ════")
        steps = []
        for step in scenario["steps"]:
            result = runner.send(user_id_for(n), step)
            print_step(result)
            steps.append(result)
        cases.append({"id": scenario["id"], "n": n, "title": scenario["title"], "steps": steps})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    transcript = {"generated_at": datetime.now().isoformat(timespec="seconds"), "ai": args.ai, "cases": cases}
    out.write_text(json.dumps(transcript, ensure_ascii=False, indent=2, default=str), "utf-8")
    print(f"對話紀錄已存到 {out}")
    print("下一步：python demo/render_screenshots.py 產生截圖")


if __name__ == "__main__":
    main()
