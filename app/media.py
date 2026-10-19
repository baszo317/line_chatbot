"""圖片：轉正、壓縮、做預覽圖，存進資料庫的 media_files 表（「補習班資料」資料夾裡的圖片會經過這裡）。

・原圖：最長邊 4096、JPEG，LINE 規定 10MB 以內
・預覽圖：最長邊 1024，LINE 規定 1MB 以內（聊天室裡先看到的小圖）
・網址：/files/<代號>.jpg 與 /files/<代號>-preview.jpg。代號是隨機的 32 碼，別人猜不到。
  LINE 只接受 https 網址，所以傳圖給家長時要用 tunnel（cloudflared / ngrok）或正式網域的網址。
圖片存在資料庫裡（不是電腦的資料夾），備份資料庫就會一起備份到，換電腦或搬到雲端也不會掉。
"""
from __future__ import annotations

import io
import secrets
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from . import db

MAX_UPLOAD = 20 * 1024 * 1024
MAX_SIDE = 4096
PREVIEW_SIDE = 1024
AI_SIDE = 2000                       # 給 AI 讀圖用的尺寸（API 單張上限 5MB）
LINE_MAX_ORIGINAL = 10 * 1024 * 1024
LINE_MAX_PREVIEW = 1024 * 1024
HEIC_BRANDS = (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypheim", b"ftypheis", b"ftypmif1", b"ftypmsf1")


class ImageError(ValueError):
    """上傳的檔案不能用（訊息會直接顯示給後台使用者看）。"""


@dataclass
class Processed:
    data: bytes
    preview: bytes
    width: int
    height: int


def _jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _shrink_to(img: Image.Image, side: int) -> Image.Image:
    copy = img.copy()
    copy.thumbnail((side, side), Image.LANCZOS)
    return copy


def process(raw: bytes) -> Processed:
    if not raw:
        raise ImageError("沒有收到檔案")
    if len(raw) > MAX_UPLOAD:
        raise ImageError("檔案太大了（上限 20MB），請先縮小或截圖後再上傳")
    if raw[4:12] in HEIC_BRANDS:
        raise ImageError("這是 iPhone 的 HEIC 格式，LINE 不支援。請改成 JPG：iPhone「設定 → 相機 → 格式 → 最相容」，"
                         "或把照片截圖後再上傳")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ImageError("看不懂這個檔案，請上傳 JPG 或 PNG 圖片") from None

    img = ImageOps.exif_transpose(img)            # 手機照片轉正
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")  # 透明背景補白色
        img.paste(rgba, mask=rgba.split()[-1])
    elif img.mode != "RGB":
        img = img.convert("RGB")

    img = _shrink_to(img, MAX_SIDE)
    data = _jpeg(img, 90)
    if len(data) > LINE_MAX_ORIGINAL:
        data = _jpeg(img, 75)
    side = PREVIEW_SIDE
    preview = _jpeg(_shrink_to(img, side), 80)
    while len(preview) > LINE_MAX_PREVIEW and side > 256:
        side = int(side * 0.8)
        preview = _jpeg(_shrink_to(img, side), 75)
    return Processed(data=data, preview=preview, width=img.width, height=img.height)


# ------------------------------------------------------------------ 資料庫
def save(raw: bytes, *, original_name: str | None, uploaded_by: str | None, note: str | None = None,
         source_hash: str | None = None, conn=None) -> dict:
    p = process(raw)
    sql = """INSERT INTO media_files (token, original_name, content_type, width, height, bytes, data, preview,
                                      note, uploaded_by, source_hash)
             VALUES (%s, %s, 'image/jpeg', %s, %s, %s, %s, %s, %s, %s, %s)
             RETURNING id, token, original_name, width, height, bytes, created_at"""
    params = (secrets.token_hex(16), (original_name or "")[:200] or None, p.width, p.height, len(p.data),
              p.data, p.preview, note, uploaded_by, source_hash)
    if conn is not None:
        return conn.execute(sql, params).fetchone()
    return db.fetch_one(sql, params)


def load(token: str, *, preview: bool = False) -> bytes | None:
    if not token.isalnum() or len(token) > 64:
        return None
    column = "preview" if preview else "data"
    row = db.fetch_one(f"SELECT {column} AS b FROM media_files WHERE token = %s", (token,))
    return bytes(row["b"]) if row else None


def for_ai(media_id: int) -> bytes:
    """給 AI 讀圖用：最長邊 2000 的 JPEG。"""
    row = db.fetch_one("SELECT data FROM media_files WHERE id = %s", (media_id,))
    if not row:
        raise ImageError("找不到這張圖片")
    return shrink_for_ai(bytes(row["data"]))


def shrink_for_ai(jpeg: bytes) -> bytes:
    """已經處理過的 JPEG → 最長邊 2000（AI 讀圖的 API 單張上限 5MB）。"""
    img = Image.open(io.BytesIO(jpeg))
    img.load()
    if max(img.size) <= AI_SIDE:
        return jpeg
    return _jpeg(_shrink_to(img.convert("RGB"), AI_SIDE), 88)


# ------------------------------------------------------------------ 網址與 LINE 訊息
LAST_PUBLIC_BASE: dict[str, str | None] = {"url": None}   # 最近一次 LINE 打進來時看到的 https 網址（後台顯示用）


def url(token: str, base: str = "") -> str:
    return f"{base}/files/{token}.jpg"


def preview_url(token: str, base: str = "") -> str:
    return f"{base}/files/{token}-preview.jpg"


def line_image(token: str, base: str) -> dict:
    """LINE 的圖片訊息。base 必須是 https 網址，例如 https://xxxx.trycloudflare.com"""
    return {"type": "image", "originalContentUrl": url(token, base), "previewImageUrl": preview_url(token, base)}
