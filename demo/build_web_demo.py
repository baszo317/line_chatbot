"""產生網頁版展示：把資料庫裡的公開資料和圖文選單圖片放進 web_demo_template.html。

    python demo/build_web_demo.py        → demo/output/web_demo.html

產生的是單一 HTML 檔：
・直接用瀏覽器打開：圖文選單、快速回覆、轉真人都照常運作；打字的問題用關鍵字比對（等於 bot 沒設定 API key）
・發佈成 claude.ai 的 artifact（宣告 sample 能力）：打字的問題改由看網頁的人自己的 Claude 回答
櫃台改了知識庫、課表或榜單之後，重跑一次就會更新。
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ai, knowledge  # noqa: E402
from app.config import settings  # noqa: E402

TEMPLATE = ROOT / "demo" / "web_demo_template.html"
MENU_IMAGE = ROOT / "assets" / "richmenu_guest.png"
OUT = ROOT / "demo" / "output" / "web_demo.html"


def export_data() -> dict:
    """網頁版需要的資料。公告不匯出：網頁上有「示範公告：明天颱風停課」的開關。"""
    return {
        "school": settings.school_name,
        "feeUnit": settings.fee_unit,
        "courses": knowledge.courses(),
        "articles": [{**{k: a[k] for k in ("category", "title", "content", "keywords")},
                      "has_image": bool(a.get("image_token"))} for a in knowledge.articles()],
        "honors": knowledge.honors(),
        "subjects": knowledge.subjects(),
        "rules": ai.RULES.format(school=settings.school_name),
        "intents": ai.INTENTS,
    }


def build(out: Path | None = None) -> Path:
    out = out or OUT
    data = json.dumps(export_data(), ensure_ascii=False).replace("</", "<\\/")  # 避免提早結束 <script>
    menu = base64.b64encode(MENU_IMAGE.read_bytes()).decode()
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__DATA__", data, 1).replace("__MENU_B64__", menu, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    path = build()
    print(f"已產生 {path.relative_to(ROOT)}（{path.stat().st_size // 1024} KB），用瀏覽器打開就能看")
