"""建立「訪客」圖文選單，並設為所有好友的預設選單。

用法（在專案根目錄，.env 要先填好 LINE_CHANNEL_ACCESS_TOKEN）：
    python scripts/create_rich_menu.py --dry-run    # 只印出設定，不呼叫 LINE
    python scripts/create_rich_menu.py              # 建立 → 上傳圖片 → 設為預設
    python scripts/create_rich_menu.py --replace    # 先刪掉舊的同名選單再建立

每個按鈕的動作是「替家長送出那段文字」（message action），bot 收到後回固定內容。
官方文件：https://developers.line.biz/en/docs/messaging-api/using-rich-menus/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from rich_menu_layout import HEIGHT, WIDTH, cells  # noqa: E402

MENU_NAME = "guest-menu"


def build_body() -> dict:
    areas = [
        {"bounds": {"x": x, "y": y, "width": w, "height": h},
         "action": {"type": "message", "label": label, "text": label}}
        for _r, _c, x, y, w, h, label, _sub, _color in cells()
    ]
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,              # 進聊天室時選單預設展開
        "name": MENU_NAME,
        "chatBarText": "點我開啟選單",   # 聊天室下方那條按鈕的文字（14 字內）
        "areas": areas,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=str(ROOT / "assets" / "richmenu_guest.png"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    body = build_body()
    if args.dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return

    image = Path(args.image)
    if not image.exists():
        raise SystemExit(f"找不到圖片 {image}，請先執行 python scripts/make_rich_menu_image.py")
    if image.stat().st_size > 1024 * 1024:
        raise SystemExit("圖片超過 1 MB，LINE 會拒收")

    from app.line_api import LineApi
    line = LineApi()
    if args.replace:
        for menu in line.list_rich_menus():
            if menu.get("name") == MENU_NAME:
                line.delete_rich_menu(menu["richMenuId"])
                print(f"已刪除舊選單 {menu['richMenuId']}")

    menu_id = line.create_rich_menu(body)
    print(f"1/3 建立選單：{menu_id}")
    content_type = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    line.upload_rich_menu_image(menu_id, image.read_bytes(), content_type)
    print("2/3 上傳圖片：完成")
    line.set_default_rich_menu(menu_id)
    print("3/3 設為預設選單：完成。重新打開和官方帳號的聊天室就看得到。")


if __name__ == "__main__":
    main()
