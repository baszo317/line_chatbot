"""立刻同步一次「補習班資料」資料夾（平常不用跑：bot 開著時每 30 秒會自動檢查）。

用法（在專案根目錄）：
    python scripts/sync_content.py

適合 bot 沒開、但想先看看檔案有沒有問題的時候。結果會印出來，也會寫進資料夾裡的「_同步結果.txt」。
資料夾位置看 .env 的 BOT_CONTENT_DIR（預設是專案裡的「補習班資料」）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import content, migrate  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if settings.content_dir is None:
        raise SystemExit("資料夾同步已關閉（.env 的 BOT_CONTENT_DIR=off）")
    if not migrate.is_up_to_date():
        migrate.upgrade()
    report = content.sync(force=True)
    print(f"資料夾：{settings.content_dir}\n")
    print(report.text())


if __name__ == "__main__":
    main()
