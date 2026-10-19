"""驗證 LINE 送來的 webhook 真的來自 LINE（沒有被偽造或竄改）。

LINE 的做法：簽章 = Base64( HMAC-SHA256( key = Channel secret, 訊息 = 原始 request body ) )
放在 request header 的 x-line-signature。

注意：一定要用「收到的原始 bytes」計算。先把 JSON 解析再轉回字串，空白或跳脫字元
一變，簽章就對不上。
"""
from __future__ import annotations

import base64
import hashlib
import hmac


def make_signature(body: bytes, channel_secret: str) -> str:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    if not signature or not channel_secret:
        return False
    return hmac.compare_digest(make_signature(body, channel_secret), signature)
