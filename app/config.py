"""集中讀取設定。值來自環境變數，或專案根目錄的 .env 檔。

bot 自己的設定都用 BOT_ 開頭，避免和電腦上其他工具（例如 Claude Code 會設定 CLAUDE_EFFORT）撞名。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # 讀取 .env；已經設定好的環境變數優先

ROOT = Path(__file__).resolve().parents[1]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "是")


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str
    line_channel_access_token: str
    database_url: str
    anthropic_api_key: str
    anthropic_base_url: str | None   # 平常留空；只有展示、測試時指向「模擬的 Claude」
    ai_mode: str                     # "claude" = 用 Claude 回答；"offline" = 只用關鍵字比對
    claude_model: str
    claude_effort: str
    claude_timeout: float
    school_name: str
    fee_unit: str                    # 學費的單位，顯示成「4,800 元／期」
    human_pause_minutes: int
    line_api_base: str
    line_data_api_base: str
    # ---- 後台（/admin）與圖片
    admin_password: str              # 後台密碼；沒設定 = 後台關閉
    admin_allow_remote: bool         # false = 後台只能在跑 bot 的這台電腦開（從 tunnel 網址連進來會被擋）
    public_base_url: str | None      # bot 對外的 https 網址（傳圖片給 LINE 用）；留空 = 從 LINE 的請求自動判斷
    vision_model: str                # 「AI 讀圖」用的模型
    auto_migrate: bool               # 啟動時自動升級資料庫
    # ---- 「補習班資料」資料夾（丟檔案就更新）
    content_dir: Path | None         # None = 不使用資料夾同步
    content_poll_seconds: int        # 每幾秒檢查一次資料夾；0 = 只在啟動時和手動同步時檢查
    ai_drafts: bool                  # 只丟圖片時，用 AI 產生文字草稿（要有 ANTHROPIC_API_KEY）


def load_settings() -> Settings:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    mode = os.getenv("BOT_AI_MODE", "").strip().lower()
    if mode not in ("claude", "offline"):
        mode = "claude" if api_key else "offline"   # 沒填 API key 就自動用關鍵字模式
    return Settings(
        line_channel_secret=os.getenv("LINE_CHANNEL_SECRET", "").strip(),
        line_channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip(),
        database_url=os.getenv("DATABASE_URL", "postgresql://postgres@localhost:5432/gaoming"),
        anthropic_api_key=api_key,
        anthropic_base_url=os.getenv("BOT_ANTHROPIC_BASE_URL") or None,
        ai_mode=mode,
        claude_model=os.getenv("BOT_AI_MODEL", "claude-haiku-5-5"),
        claude_effort=os.getenv("BOT_AI_EFFORT", "low"),
        claude_timeout=float(os.getenv("BOT_AI_TIMEOUT", "20")),
        school_name=os.getenv("SCHOOL_NAME", "高明補習班"),
        fee_unit=os.getenv("FEE_UNIT", "期"),
        human_pause_minutes=_int("HUMAN_PAUSE_MINUTES", 30),
        line_api_base=os.getenv("LINE_API_BASE", "https://api.line.me").rstrip("/"),
        line_data_api_base=os.getenv("LINE_DATA_API_BASE", "https://api-data.line.me").rstrip("/"),
        admin_password=os.getenv("BOT_ADMIN_PASSWORD", "").strip(),
        admin_allow_remote=_bool("BOT_ADMIN_ALLOW_REMOTE", False),
        public_base_url=(os.getenv("BOT_PUBLIC_BASE_URL", "").strip().rstrip("/") or None),
        vision_model=os.getenv("BOT_VISION_MODEL", "claude-sonnet-5-5"),
        auto_migrate=_bool("BOT_AUTO_MIGRATE", True),
        content_dir=_content_dir(),
        content_poll_seconds=max(0, _int("BOT_CONTENT_POLL_SECONDS", 30)),
        ai_drafts=_bool("BOT_AI_DRAFTS", True),
    )


def _content_dir() -> Path | None:
    value = os.getenv("BOT_CONTENT_DIR", "").strip()
    if value.lower() in ("off", "none", "false", "0"):
        return None
    path = Path(value).expanduser() if value else ROOT / "補習班資料"
    return path if path.is_absolute() else ROOT / path


settings = load_settings()
