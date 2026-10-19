"""AI 讀圖：把圖片（行事曆、公告、收費表、課表）轉成文字。

用在兩個地方：後台表單的「用 AI 讀圖片的字」按鈕（文字放進表單，給人看過、改過再儲存），
以及「補習班資料」資料夾只丟圖片時自動產生的 AI 草稿。都不會直接給家長。
家長問問題時，AI 是根據這段文字回答；圖片本身會在回覆時一起附上。
需要 .env 的 ANTHROPIC_API_KEY；模型用 BOT_VISION_MODEL（預設 claude-sonnet-5-5，看圖比較準）。
"""
from __future__ import annotations

import base64
import json
import logging

import anthropic

from . import ai, media
from .config import settings

log = logging.getLogger(__name__)

PURPOSES: dict[str, tuple[str, str]] = {
    "calendar": ("行事曆", "這張圖是補習班的行事曆或行程表。把每一個日期和事項都轉成文字，照日期先後排列，"
                         "一行一個日期，格式像「11/3（一）國中部段考週，暫停上課」；同一天有好幾件事用「；」隔開。"
                         "月份或年份寫在圖上的話，放進 month 欄位。"),
    "announcement": ("公告", "這張圖是補習班要給家長看的公告或通知。把內容完整轉成通順的文字，"
                            "日期、時間、地點、金額、要帶的東西等細節都要保留。"),
    "kb": ("常見問題資料", "這張圖是補習班的資料（例如收費表、交通地圖、規定）。把家長會想知道的內容完整轉成文字，"
                         "表格請改成一行一項，例如「國二數學：每期 4,800 元」。"),
    "timetable": ("課表", "這張圖是補習班的課表。把每個班級的上課星期、時間、科目、教室轉成文字，一行一個班，"
                        "例如「國二數學 A 班：週一、週三 18:30–20:30（201 教室）」。"),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "建議的標題，20 字以內，例如「11 月行事曆」"},
        "text": {"type": "string", "description": "圖片內容的文字版：純文字，不要 Markdown"},
        "month": {"type": "string", "description": "行事曆所屬年月，格式 YYYY-MM；看不出來或不是行事曆就給空字串"},
        "unclear": {"type": "array", "items": {"type": "string"},
                    "description": "看不清楚、需要人工確認的地方；沒有就給空陣列"},
    },
    "required": ["title", "text", "month", "unclear"],
    "additionalProperties": False,
}

SYSTEM = """你是「{school}」櫃台的小幫手，負責把圖片轉成文字，讓 LINE 機器人之後可以根據這段文字回答家長。

規則：
1. 只寫圖片裡真的有的內容，不要補充、不要推測、不要改寫成別的意思。
2. 看不清楚的字，在文字裡寫「（？）」，並把那個地方列在 unclear，讓老師確認。
3. 用繁體中文、台灣用語。
4. LINE 不顯示 Markdown：不要用 #、**、表格；條列用「・」，分段用換行。
5. 民國年請保留原樣；只寫月日的日期，依照下面「今天」的日期判斷是哪一年。"""


class VisionError(RuntimeError):
    """AI 讀圖失敗（訊息會顯示在後台）。"""


def available() -> bool:
    return bool(settings.anthropic_api_key)


def build_request(image: bytes, purpose: str, hint: str = "") -> dict:
    label, instructions = PURPOSES.get(purpose, PURPOSES["announcement"])
    text = f"用途：{label}\n{instructions}"
    if hint.strip():
        text += f"\n上傳的老師補充說明：{hint.strip()}"
    return {
        "model": settings.vision_model,
        "max_tokens": 4096,
        "system": SYSTEM.format(school=settings.school_name) + "\n\n" + ai.today_line(),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(image).decode()}},
                {"type": "text", "text": text},
            ],
        }],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


def read_image(media_id: int, purpose: str, hint: str = "") -> dict:
    if not available():
        raise VisionError("還沒設定 ANTHROPIC_API_KEY，不能用 AI 讀圖。可以自己把圖片上的重點打進「內容」。")
    try:
        image = media.for_ai(media_id)
    except media.ImageError as e:
        raise VisionError(str(e)) from None
    return read_image_bytes(image, purpose, hint)


def read_image_bytes(image: bytes, purpose: str, hint: str = "") -> dict:
    """image：JPEG（最長邊 2000 以內，見 media.shrink_for_ai）。"""
    if not available():
        raise VisionError("還沒設定 ANTHROPIC_API_KEY，不能用 AI 讀圖。可以自己把圖片上的重點打進「內容」。")
    try:
        resp = ai.get_client().with_options(timeout=120).messages.create(**build_request(image, purpose, hint))
    except anthropic.APIError as e:
        log.warning("AI 讀圖失敗：%s", e)
        raise VisionError(f"AI 讀圖失敗，請稍後再試（{getattr(e, 'status_code', '') or '連線問題'}）") from None
    except Exception as e:  # 網路斷線等
        log.exception("AI 讀圖失敗")
        raise VisionError(f"AI 讀圖失敗：{e}") from None

    if resp.stop_reason == "refusal":
        raise VisionError("AI 沒有處理這張圖片，請改成自己輸入內容")
    if resp.stop_reason == "max_tokens":
        raise VisionError("圖片上的字太多，一次轉不完。請把圖片裁成上下兩半，分兩次上傳")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise VisionError("AI 回傳的格式不對，請再試一次") from None
    return {
        "title": ai._plain(str(data.get("title", "")))[:60],
        "text": ai._plain(str(data.get("text", ""))),
        "month": str(data.get("month", "")),
        "unclear": [str(u) for u in data.get("unclear", [])],
        "model": resp.model,
    }
