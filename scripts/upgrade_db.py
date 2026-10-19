"""手動升級資料庫（平常不用跑：bot 啟動時會自動升級）。

用法（在專案根目錄）：
    python scripts/upgrade_db.py            # 執行 sql/ 裡還沒跑過的升級檔（002、003…）
    python scripts/upgrade_db.py --demo     # 另外重新匯入示範課程與榜單（sql/phase1_seed.sql）

會用 .env 的 DATABASE_URL。升級檔可以重複執行，跑過的會自動略過。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, migrate  # noqa: E402


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="重新匯入示範課程與榜單")
    args = parser.parse_args()

    before = sorted(migrate.applied_versions())
    print(f"目前資料庫版本：{before[-1] if before else '（舊版，沒有升級紀錄）'}")
    try:
        result = migrate.upgrade()
    except RuntimeError as e:
        raise SystemExit(f"升級失敗：{e}")
    if result.created_schema:
        print("資料庫原本是空的，已建立全部資料表（sql/schema.sql）")
    elif result.applied:
        print("已執行升級檔：" + "、".join(result.applied))
    else:
        print("已經是最新版，不用升級")
    for w in result.warnings:
        print("⚠ " + w)

    if args.demo:
        db.execute((ROOT / "sql" / "phase1_seed.sql").read_text("utf-8"))
        print("已重新匯入示範課程與榜單")

    grades = db.fetch_all("""SELECT grade_label(grade) AS label, count(*) AS n FROM courses
                              WHERE status = 'open' GROUP BY grade ORDER BY grade NULLS LAST""")
    print("開放中的班級：" + ("、".join(f"{g['label'] or '不分年級'} {g['n']} 班" for g in grades) or "（沒有）"))


if __name__ == "__main__":
    main()
