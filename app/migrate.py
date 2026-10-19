"""資料庫自動升級。

sql/ 資料夾裡 002_xxx.sql、003_xxx.sql… 這種「三位數字開頭」的檔案是升級檔。
bot 啟動時會檢查 schema_migrations 表，把還沒執行過的升級檔依編號順序各執行一次。
也可以手動執行：python scripts/upgrade_db.py

升級檔都寫成「可以重複執行」，而且每個檔案自己包在 BEGIN … COMMIT 裡：
中途出錯會整個檔案還原，不會改一半。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
MIGRATION_FILE = re.compile(r"^(\d{3})_.+\.sql$")
LATEST = "006"   # 程式需要的資料庫版本（sql/schema.sql 也已包含到這一版）


@dataclass
class UpgradeResult:
    applied: list[str] = field(default_factory=list)      # 這次執行的升級檔
    warnings: list[str] = field(default_factory=list)     # 升級檔用 RAISE WARNING 提醒的事
    created_schema: bool = False                          # 資料庫原本是空的，直接建立整份 schema


def migration_files() -> list[tuple[str, Path]]:
    files = []
    for path in sorted(SQL_DIR.glob("*.sql")):
        m = MIGRATION_FILE.match(path.name)
        if m:
            files.append((m.group(1), path))
    return files


def applied_versions() -> set[str]:
    if not db.fetch_one("SELECT to_regclass('public.schema_migrations') AS t")["t"]:
        return set()
    return {r["version"] for r in db.fetch_all("SELECT version FROM schema_migrations")}


def is_up_to_date() -> bool:
    return LATEST in applied_versions()


def upgrade() -> UpgradeResult:
    """執行還沒執行過的升級檔。資料庫完全是空的時候，直接建立 sql/schema.sql。"""
    result = UpgradeResult()

    def collect(diag) -> None:
        if diag.severity_nonlocalized == "WARNING" and diag.message_primary:
            result.warnings.append(diag.message_primary)

    with db.get_pool().connection() as conn:
        conn.add_notice_handler(collect)
        try:
            _upgrade(conn, result)
        finally:
            conn.remove_notice_handler(collect)
    for w in result.warnings:
        log.warning("資料庫升級提醒：%s", w)
    return result


def _upgrade(conn, result: UpgradeResult) -> None:
    has_tables = conn.execute("SELECT to_regclass('public.kb_articles') AS t").fetchone()["t"]
    if not has_tables:
        _run_script(conn, SQL_DIR / "schema.sql")
        result.created_schema = True
        log.info("資料庫是空的，已建立完整的 schema（%s）", LATEST)
        return

    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                      version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, path in migration_files():
        if version in done:
            continue
        log.info("執行資料庫升級檔 %s", path.name)
        _run_script(conn, path)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING", (version,))
        result.applied.append(version)


def _run_script(conn, path: Path) -> None:
    try:
        conn.execute(path.read_text("utf-8"))
    except Exception as e:
        conn.execute("ROLLBACK")   # 檔案裡的 BEGIN 還開著，先還原
        raise RuntimeError(f"執行 {path.name} 失敗，資料庫沒有被修改：{e}") from e
