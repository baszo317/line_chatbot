"""把知識庫 CSV（可以用 Excel 編輯）匯入資料庫的 kb_articles。

用法（在專案根目錄）：
    python scripts/import_kb.py data/kb_template.csv

CSV 欄位：category, title, content, keywords, audience, sort_order, is_active
  category   填英文或中文都可以：contact 聯絡、traffic 交通、course 課程、fee 收費、
             policy 規定、enrollment 招生、other 其他
  title      問題標題（建議 20 字以內，會變成「常見問題」的按鈕文字），不可重複
  content    標準答案；可以換行
  keywords   用頓號、逗號或空白分隔，例如「電話、聯絡、客服」
  audience   public（所有人）；第一期都填 public
  is_active  填 0 代表下架：資料保留，但 AI 不會再用
相同標題會更新內容，新標題會新增。改完約一分鐘內 bot 就會用新內容回答。
"""
from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402

CATEGORY = {"聯絡": "contact", "交通": "traffic", "課程": "course", "收費": "fee",
            "規定": "policy", "招生": "enrollment", "其他": "other"}
VALID = set(CATEGORY.values())

UPSERT = """
INSERT INTO kb_articles (category, title, content, keywords, audience, sort_order, is_active)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (title) DO UPDATE
   SET category = EXCLUDED.category, content = EXCLUDED.content, keywords = EXCLUDED.keywords,
       audience = EXCLUDED.audience, sort_order = EXCLUDED.sort_order, is_active = EXCLUDED.is_active
RETURNING (xmax = 0) AS inserted
"""


def read_rows(path: str) -> list[dict]:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp950"):   # Excel 存檔可能是 UTF-8 或 Big5
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(encoding))))
        except UnicodeDecodeError:
            continue
    raise SystemExit("看不懂檔案編碼。請在 Excel 選「另存新檔 → CSV UTF-8（逗號分隔）」。")


def import_file(path: str) -> tuple[int, int, int]:
    """匯入一個 CSV，回傳（新增, 更新, 略過）筆數。"""
    inserted = updated = skipped = 0
    for line_no, row in enumerate(read_rows(path), start=2):
        title = (row.get("title") or "").strip()
        content = (row.get("content") or "").strip()
        category = CATEGORY.get((row.get("category") or "").strip(), (row.get("category") or "").strip())
        if not title or not content or category not in VALID:
            print(f"第 {line_no} 列略過：標題、內容不可空白，分類要是 {sorted(VALID)} 或中文名稱")
            skipped += 1
            continue
        if len(title) > 20:
            print(f"提醒：第 {line_no} 列標題超過 20 字，按鈕上會被截斷：{title}")
        keywords = [k for k in re.split(r"[、,，\s]+", row.get("keywords") or "") if k]
        audience = (row.get("audience") or "public").strip() or "public"
        sort_order = int((row.get("sort_order") or "0").strip() or 0)
        is_active = (row.get("is_active") or "1").strip() not in ("0", "false", "FALSE", "否")
        result = db.fetch_one(UPSERT, (category, title, content, keywords, audience, sort_order, is_active))
        if result and result["inserted"]:
            inserted += 1
        else:
            updated += 1
    return inserted, updated, skipped


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    inserted, updated, skipped = import_file(sys.argv[1])
    print(f"完成：新增 {inserted} 筆、更新 {updated} 筆、略過 {skipped} 筆")


if __name__ == "__main__":
    main()
