"""LINE Messaging API 的小型用戶端。

直接呼叫 REST API（沒有用官方 SDK），每個請求長什麼樣子一看就懂。
官方文件：https://developers.line.biz/en/reference/messaging-api/
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings

log = logging.getLogger(__name__)

MAX_TEXT = 5000        # 一則文字訊息的字數上限
MAX_MESSAGES = 5       # 一次回覆最多 5 則訊息
MAX_QUICK_REPLY = 13   # 快速回覆按鈕最多 13 個
MAX_LABEL = 20         # 按鈕文字最多 20 字
MULTICAST_MAX = 500    # multicast 一次最多傳給 500 個 LINE

ERROR_TEXT = {
    400: "LINE 說訊息格式不對",
    401: "LINE 的 Channel access token 不對（.env 的 LINE_CHANNEL_ACCESS_TOKEN）",
    403: "這個官方帳號不能用這個功能",
    429: "這個月的訊息額度用完了，或是傳得太快",
}


class LineError(Exception):
    """LINE API 回傳錯誤或連不上。訊息會直接顯示在後台。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def text_message(text: str, quick_replies: list[str] | None = None) -> dict:
    """組一則文字訊息；quick_replies 會變成訊息下方的按鈕，按下去等於替家長打出那段文字。"""
    msg: dict = {"type": "text", "text": text[:MAX_TEXT]}
    if quick_replies:
        msg["quickReply"] = {
            "items": [
                {"type": "action", "action": {"type": "message", "label": q[:MAX_LABEL], "text": q}}
                for q in quick_replies[:MAX_QUICK_REPLY]
            ]
        }
    return msg


class LineApi:
    def __init__(self, token: str | None = None, base: str | None = None,
                 data_base: str | None = None, timeout: float = 10.0):
        self.token = settings.line_channel_access_token if token is None else token
        self.base = base or settings.line_api_base
        self.data_base = data_base or settings.line_data_api_base
        self.client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {self.token}"})

    # ---------------------------------------------------------- 回覆與互動
    def reply(self, reply_token: str, messages: list[dict]) -> None:
        r = self.client.post(
            f"{self.base}/v2/bot/message/reply",
            json={"replyToken": reply_token, "messages": messages[:MAX_MESSAGES]},
        )
        if r.status_code >= 400:
            log.error("LINE 回覆失敗 %s：%s", r.status_code, r.text[:300])
        r.raise_for_status()

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        """在聊天室顯示「輸入中…」動畫（5～60 秒，送出訊息後自動消失）。失敗也不影響回覆。"""
        try:
            self.client.post(
                f"{self.base}/v2/bot/chat/loading/start",
                json={"chatId": user_id, "loadingSeconds": seconds},
            )
        except httpx.HTTPError as e:
            log.warning("載入動畫失敗：%s", e)

    def get_profile(self, user_id: str) -> dict | None:
        try:
            r = self.client.get(f"{self.base}/v2/bot/profile/{user_id}")
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    # ---------------------------------------------------------- 主動傳訊息（會用掉每月的訊息額度）
    def _call(self, method: str, path: str, body: dict | None = None, retry_key: str | None = None) -> dict:
        headers = {"X-Line-Retry-Key": retry_key} if retry_key else None
        try:
            r = self.client.request(method, f"{self.base}{path}", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LineError(0, f"連不到 LINE（{type(e).__name__}）") from None
        if r.status_code == 409 and retry_key:    # 同一個 retry key 已經傳過了（重送）：當作成功
            return {}
        if r.status_code >= 400:
            try:
                detail = r.json().get("message") or ""
            except ValueError:
                detail = r.text[:200]
            log.error("LINE %s %s 失敗 %s：%s", method, path, r.status_code, detail)
            text = ERROR_TEXT.get(r.status_code, "LINE 回傳錯誤")
            raise LineError(r.status_code, f"{text}（{r.status_code}{'：' + detail if detail else ''}）")
        return r.json() if r.content else {}

    def push(self, to: str, messages: list[dict], retry_key: str | None = None) -> None:
        self._call("POST", "/v2/bot/message/push", {"to": to, "messages": messages[:MAX_MESSAGES]}, retry_key)

    def multicast(self, to: list[str], messages: list[dict], retry_key: str | None = None) -> None:
        self._call("POST", "/v2/bot/message/multicast",
                   {"to": to[:MULTICAST_MAX], "messages": messages[:MAX_MESSAGES]}, retry_key)

    def broadcast(self, messages: list[dict], retry_key: str | None = None) -> None:
        """傳給所有加官方帳號好友的人。"""
        self._call("POST", "/v2/bot/message/broadcast", {"messages": messages[:MAX_MESSAGES]}, retry_key)

    def quota(self) -> dict:
        """這個月的訊息額度：limit 上限（None＝沒有上限）、used 已用。"""
        q = self._call("GET", "/v2/bot/message/quota")
        used = self._call("GET", "/v2/bot/message/quota/consumption").get("totalUsage", 0)
        return {"limit": q.get("value") if q.get("type") == "limited" else None, "used": used}

    def friend_count(self) -> int | None:
        """好友人數（扣掉封鎖的），LINE 前一天的統計；拿不到時回傳 None。"""
        day = (datetime.now(timezone(timedelta(hours=9))) - timedelta(days=1)).strftime("%Y%m%d")
        try:
            data = self._call("GET", f"/v2/bot/insight/followers?date={day}")
        except LineError:
            return None
        if data.get("status") != "ready" or data.get("followers") is None:
            return None
        return max(data["followers"] - (data.get("blocks") or 0), 0)

    # ---------------------------------------------------------- 圖文選單
    def create_rich_menu(self, body: dict) -> str:
        r = self.client.post(f"{self.base}/v2/bot/richmenu", json=body)
        r.raise_for_status()
        return r.json()["richMenuId"]

    def upload_rich_menu_image(self, rich_menu_id: str, image: bytes, content_type: str = "image/png") -> None:
        r = self.client.post(
            f"{self.data_base}/v2/bot/richmenu/{rich_menu_id}/content",
            content=image,
            headers={"Content-Type": content_type},
        )
        r.raise_for_status()

    def set_default_rich_menu(self, rich_menu_id: str) -> None:
        r = self.client.post(f"{self.base}/v2/bot/user/all/richmenu/{rich_menu_id}")
        r.raise_for_status()

    def list_rich_menus(self) -> list[dict]:
        r = self.client.get(f"{self.base}/v2/bot/richmenu/list")
        r.raise_for_status()
        return r.json().get("richmenus", [])

    def delete_rich_menu(self, rich_menu_id: str) -> None:
        r = self.client.delete(f"{self.base}/v2/bot/richmenu/{rich_menu_id}")
        r.raise_for_status()
