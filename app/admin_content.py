"""後台的「公告與行事曆」「常見問題」「榜單」：在表單填好按儲存 → 寫進「補習班資料」資料夾 → 立刻同步。

資料夾一直是唯一的資料來源，後台只是幫你寫檔案，所以兩種改法（後台表單、直接丟檔案）可以混著用：
・公告：公告/<分類>/<標題>.txt（第一行「年級：…」、「顯示到：…」）＋ 同檔名 .jpg
・行事曆：行事曆/<標題>.txt ＋ .jpg（每個年級用最新存的那一份）
・常見問題：常見問題/<分類>/<排序>_<標題>.txt（「關鍵字：…」）＋ .jpg
・榜單：榜單/榜單.csv
後台存的檔案一定會寫「年級：」那一行（全部年級寫「年級：全部」），所以檔名或資料夾裡的年級字不會蓋掉你在表單選的。
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from . import content, db, media, vision
from .admin import audit, current_user, run
from .config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api/content")

NOTICE_CATEGORIES = ("general", "closure", "exam", "event", "enrollment", "calendar")
FAQ_CATEGORIES = tuple(content.KB_SUBS.values())
GRADE_WORDS = {6: "國小", 7: "國一", 8: "國二", 9: "國三"}
PREVIEW_SIDE = 640


# ====================================================================== 共用
def _root() -> Path:
    if settings.content_dir is None:
        raise HTTPException(400, "「補習班資料」資料夾同步已關閉（.env 的 BOT_CONTENT_DIR=off），不能在後台修改這些資料")
    settings.content_dir.mkdir(parents=True, exist_ok=True)
    return settings.content_dir


def _check_key(root: Path, key: str, sections: tuple[str, ...]) -> str:
    parts = key.split("/")
    if not key or key.startswith("/") or "\\" in key or ".." in parts or parts[0] not in sections or len(parts) < 2:
        raise HTTPException(400, "資料的位置不對，請重新整理頁面再試一次")
    if not (root / key).resolve().is_relative_to(root.resolve()):
        raise HTTPException(400, "資料的位置不對")
    return key


def _files_of(root: Path, key: str) -> list[Path]:
    """這筆資料的檔案：同一個資料夾裡、檔名（不含副檔名）相同的文字檔和圖片。"""
    base = root / key
    if not base.parent.is_dir():
        return []
    return sorted(f for f in base.parent.iterdir()
                  if f.is_file() and f.stem.strip() == base.name
                  and f.suffix.lower() in content.IMAGE_EXT | content.TEXT_EXT)


class ImageIn(BaseModel):
    data: str = Field(max_length=30_000_000)         # base64（可以是 data: 網址）
    name: str | None = Field(default=None, max_length=200)


def _decode_image(img: ImageIn) -> bytes:
    """收到的圖片 → 轉正、壓縮成 LINE 可以用的 JPEG。不能用的圖片（HEIC、壞檔）直接告訴使用者原因。"""
    try:
        raw = base64.b64decode(img.data.split(",", 1)[-1], validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "圖片讀不出來，請重新選一次檔案") from None
    try:
        return media.process(raw).data
    except media.ImageError as e:
        raise HTTPException(400, str(e)) from None


def _clean_title(title: str, limit: int) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title:
        raise HTTPException(400, "請填標題")
    if len(title) > limit:
        raise HTTPException(400, f"標題太長了（最多 {limit} 個字）")
    return title


def _grades_line(grades: list[int]) -> str:
    g = sorted(set(grades))
    if not g or g == [6, 7, 8, 9]:
        return "年級：全部"
    if g == [7, 8, 9]:
        return "年級：國中"
    return "年級：" + "、".join(GRADE_WORDS[x] for x in g)


def _write(path: Path, data: bytes | str) -> None:
    try:
        if isinstance(data, str):
            content.write_text(path, data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    except PermissionError:
        raise HTTPException(400, f"「{path.name}」寫不進去：可能正被其他程式（Excel、看圖軟體）開著，關掉再試一次") from None
    except OSError as e:
        raise HTTPException(400, f"「{path.name}」寫不進去：{e}") from None


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        raise HTTPException(400, f"「{path.name}」刪不掉：可能正被其他程式開著，關掉再試一次") from None


def _save_files(root: Path, old_key: str | None, new_key: str, text: str | None, image: bytes | None,
                remove_image: bool) -> Path | None:
    """寫入（或搬移）一筆資料的 .txt 和圖片，刪掉不再用的舊檔。回傳圖片的路徑（沒有圖片＝None）。"""
    old_files = _files_of(root, old_key) if old_key else []
    if old_key and not old_files:
        raise HTTPException(404, "找不到這筆資料的檔案，可能已經在資料夾裡被刪掉或改名了。請重新整理頁面")
    if new_key != old_key and _files_of(root, new_key):
        raise HTTPException(400, "這個資料夾裡已經有同樣標題的資料了，請換一個標題")
    base = root / new_key
    old_images = [f for f in old_files if f.suffix.lower() in content.IMAGE_EXT]
    keep: set[Path] = set()
    if text is not None:
        txt = base.with_name(base.name + ".txt")
        _write(txt, text)
        keep.add(txt)
    new_image = None
    if image is not None:
        new_image = base.with_name(base.name + ".jpg")
        _write(new_image, image)
    elif old_images and not remove_image:
        src = old_images[0]
        new_image = base.with_name(base.name + src.suffix.lower())
        if src != new_image:
            new_image.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(new_image))
    if new_image:
        keep.add(new_image)
    for f in old_files:
        if f.exists() and f not in keep:
            _remove(f)
    return new_image


def problems_for(problems: list[str], names: list[str]) -> list[str]:
    """同步結果裡和這些檔案有關的問題。每一行是「檔案路徑：說明」（檔名本身也可能有全形冒號，所以用開頭比對）。"""
    out = []
    for p in problems:
        for name in sorted(names, key=len, reverse=True):
            if p.startswith(name + "："):
                out.append(p[len(name) + 1:])
                break
    return out


def _sync(root: Path, key: str, image: Path | None) -> dict:
    """同步，並找出和這筆資料有關的問題（例如標題重複）。"""
    no_drafts = {image.relative_to(root).as_posix()} if image else set()
    report = content.sync(force=True, no_drafts=no_drafts)
    names = [key] + [f.relative_to(root).as_posix() for f in _files_of(root, key)]
    return {"key": key, "problems": problems_for(report.problems if report else [], names),
            "error": report.error if report else None}


# ====================================================================== 列出全部
def catalog() -> dict:
    """後台列表用：資料夾裡的每一筆公告、行事曆、常見問題（含過期、沒用到、有問題的），以及不是從資料夾來的資料。"""
    out: dict = {"enabled": settings.content_dir is not None, "dir": str(settings.content_dir or ""),
                 "notices": [], "faqs": [], "honors": [], "outside": [], "report": content.last_report()}
    today = datetime.now(content.TW).date()
    for r in db.fetch_all("""SELECT id, title, body, category, grades, ends_at, 'announcements' AS tbl FROM v_active_announcements
                              WHERE source_path IS NULL AND audience IN ('all','guardian')
                             UNION ALL
                             SELECT id, title, content, category, NULL, NULL, 'kb_articles' FROM kb_articles
                              WHERE source_path IS NULL AND is_active AND audience = 'public' ORDER BY 1"""):
        out["outside"].append({"table": r["tbl"], "id": r["id"], "title": r["title"], "body": r["body"],
                               "category": r["category"], "grades": r["grades"] or [],
                               "ends_on": (r["ends_at"].astimezone(content.TW) - timedelta(seconds=1)).date().isoformat()
                               if r["ends_at"] else None,
                               "demo": r["title"].startswith("【示範】") or "示範" in (r["body"] or "")})
    if settings.content_dir is None:
        return out
    root = _root()
    report = content.last_report() or {}
    problems = report.get("problems", [])
    entries = content.scan(root)
    seen = {r["path"]: r["first_seen_at"] for r in db.fetch_all("SELECT path, first_seen_at FROM content_files")}
    for e in entries:
        e.first_seen = seen.get(e.rel)
    groups, honor = content.build_groups(entries, content.Report())
    live_notices = {r["source_path"]: r for r in db.fetch_all(
        "SELECT source_path, ends_at FROM announcements WHERE source_path IS NOT NULL")}
    live_faqs = {r["source_path"] for r in db.fetch_all(
        "SELECT source_path FROM kb_articles WHERE source_path IS NOT NULL AND is_active")}
    for g in groups:
        info = g.text_info(today)
        title, sort = content.split_title(g.stem)
        files = [f.rel for f in g.texts + g.images]
        item = {
            "key": g.key, "title": title, "sort": sort, "body": info.body if info else "",
            "draft": bool(info and info.draft), "files": files,
            "image": g.images[0].rel if g.images else None,
            "problems": problems_for(problems, files + [g.key]),
            "first_seen": g.first_seen.isoformat() if any(e.first_seen for e in g.images + g.texts) else None,
        }
        if item["body"] in content.PLACEHOLDER.values():
            item["body"] = ""
        if g.section == content.KB:
            item.update(category=content.KB_SUBS[g.sub or "其他"], keywords=info.keywords if info else [],
                        live=g.key in live_faqs)
            out["faqs"].append(item)
            continue
        category = "calendar" if g.section == content.CALENDAR else content.NOTICE_SUBS.get(g.sub or "一般", "general")
        row = live_notices.get(g.key)
        ends_on = info.ends_on if info else None
        status = ("expired" if ends_on and ends_on < today else "live") if row else (
            "old" if category == "calendar" and not item["problems"] else "problem")
        item.update(category=category, grades=list(content.effective_grades(g, today, content.Report())),
                    ends_on=ends_on.isoformat() if ends_on else None, status=status)
        if category == "general" and re.search(r"停課|颱風|停班|放假", title):
            item["category"] = "closure"
        out["notices"].append(item)
    out["notices"].sort(key=lambda n: (n["status"] != "live", n["category"] != "closure", n["category"] == "calendar",
                                       -(datetime.fromisoformat(n["first_seen"]).timestamp() if n["first_seen"] else 0)))
    out["faqs"].sort(key=lambda a: (FAQ_CATEGORIES.index(a["category"]), a["sort"] or 999, a["title"]))
    if honor:
        out["honors"] = read_honors(honor.path)
    return out


@router.get("")
def list_content(user: str = Depends(current_user)) -> dict:
    return catalog()


@router.get("/image")
def content_image(path: str, user: str = Depends(current_user)) -> Response:
    """後台預覽資料夾裡的圖片（縮小版）。只能讀「補習班資料」裡的圖片。"""
    root = _root()
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file() or target.suffix.lower() not in content.IMAGE_EXT:
        raise HTTPException(404, "找不到圖片")
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(target))
        img.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=82)
    except OSError:
        raise HTTPException(404, "這張圖片打不開") from None
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ====================================================================== 公告與行事曆
class NoticeIn(BaseModel):
    key: str | None = Field(default=None, max_length=300)    # 修改時：原本的位置（source_path）
    outside_id: int | None = None                           # 修改「不是從資料夾來的」公告時，原本那筆的編號
    category: Literal["general", "closure", "exam", "event", "enrollment", "calendar"]
    title: str = Field(max_length=200)
    body: str = Field(default="", max_length=4000)
    grades: list[Literal[6, 7, 8, 9]] = []
    ends_on: date | None = None
    image: ImageIn | None = None
    remove_image: bool = False


def notice_key(category: str, title: str) -> str:
    if category == "calendar":
        return f"{content.CALENDAR}/{content.safe_name(title)}"
    sub = content.NOTICE_SUB_NAME.get(category, "一般")
    folder = content.NOTICES if sub == "一般" else f"{content.NOTICES}/{sub}"
    return f"{folder}/{content.safe_name(title)}"


@router.post("/notices/save")
def save_notice(body: NoticeIn, user: str = Depends(current_user)) -> dict:
    root = _root()
    old_key = _check_key(root, body.key, (content.NOTICES, content.CALENDAR)) if body.key else None
    title = _clean_title(body.title, 60)
    image = _decode_image(body.image) if body.image else None
    text = body.body.strip()
    head = []
    if body.ends_on:
        head.append(f"顯示到：{body.ends_on:%Y/%m/%d}")
    head.append(_grades_line(body.grades))
    new_key = notice_key(body.category, title)
    with content._lock:
        has_old_image = bool(old_key and any(f.suffix.lower() in content.IMAGE_EXT for f in _files_of(root, old_key)))
        if not text and image is None and not (has_old_image and not body.remove_image):
            raise HTTPException(400, "請填內容，或上傳一張圖片")
        saved_image = _save_files(root, old_key, new_key, "\n".join(head) + "\n\n" + (text + "\n" if text else ""),
                                  image, body.remove_image)
        result = _sync(root, new_key, saved_image)

    def record(conn):
        if body.outside_id:     # 原本不是從資料夾來的那一筆：改由檔案管理後，舊的刪掉
            conn.execute("DELETE FROM announcements WHERE id = %s AND source_path IS NULL", (body.outside_id,))
        kind = "行事曆" if body.category == "calendar" else "公告"
        audit(conn, user, "update" if old_key or body.outside_id else "insert", "announcements", new_key,
              f"{'修改' if old_key or body.outside_id else '新增'}{kind}「{title}」",
              after={"title": title, "category": body.category, "grades": body.grades,
                     "ends_on": body.ends_on.isoformat() if body.ends_on else None, "image": bool(saved_image)})
    run(record)
    return result


class KeyIn(BaseModel):
    key: str = Field(max_length=300)


@router.post("/notices/delete")
def delete_notice(body: KeyIn, user: str = Depends(current_user)) -> dict:
    root = _root()
    key = _check_key(root, body.key, (content.NOTICES, content.CALENDAR))
    with content._lock:
        files = _files_of(root, key)
        if not files:
            raise HTTPException(404, "找不到這筆資料的檔案，可能已經被刪掉了")
        for f in files:
            _remove(f)
        content.sync(force=True)
    run(lambda conn: audit(conn, user, "delete", "announcements", key, f"刪除「{key.rsplit('/', 1)[-1]}」"))
    return {"ok": True}


class OutsideIn(BaseModel):
    table: Literal["announcements", "kb_articles"]
    id: int


@router.post("/outside/delete")
def delete_outside(body: OutsideIn, user: str = Depends(current_user)) -> dict:
    """刪掉不是從資料夾來的公告或常見問題（例如 demo/run_demo.py 放的示範颱風公告）。"""
    def do(conn):
        row = conn.execute(f"DELETE FROM {body.table} WHERE id = %s AND source_path IS NULL RETURNING title",
                           (body.id,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到這筆資料")
        audit(conn, user, "delete", body.table, body.id, f"刪除「{row['title']}」（不是從資料夾來的資料）")
        return {"ok": True}
    return run(do)


# ====================================================================== 常見問題
class FaqIn(BaseModel):
    key: str | None = Field(default=None, max_length=300)
    outside_id: int | None = None
    category: Literal["contact", "traffic", "course", "fee", "policy", "enrollment", "other"]
    title: str = Field(max_length=200)
    body: str = Field(default="", max_length=4000)
    keywords: list[str] = Field(default=[], max_length=30)
    sort: int | None = Field(default=None, ge=0, le=999)
    image: ImageIn | None = None
    remove_image: bool = False


def _next_sort(root: Path, folder: str) -> int:
    nums = [content.split_title(p.stem)[1] for p in (root / folder).glob("*") if p.is_file()]
    return max(nums, default=0) + 1


@router.post("/faqs/save")
def save_faq(body: FaqIn, user: str = Depends(current_user)) -> dict:
    root = _root()
    old_key = _check_key(root, body.key, (content.KB,)) if body.key else None
    title = _clean_title(body.title, 40)
    image = _decode_image(body.image) if body.image else None
    text = body.body.strip()
    keywords = [k.strip() for k in body.keywords if k.strip()]
    folder = f"{content.KB}/{content.KB_SUB_NAME[body.category]}"
    with content._lock:
        others = [g for g in content.build_groups(content.scan(root), content.Report())[0]
                  if g.section == content.KB and g.key != old_key]
        if any(content.split_title(g.stem)[0] == content.safe_name(title) for g in others):
            raise HTTPException(400, f"已經有一題叫「{title}」了，同一個問題只能有一題")
        sort = body.sort
        if sort is None:
            sort = content.split_title(old_key.rsplit("/", 1)[-1])[1] if old_key else _next_sort(root, folder)
        stem = f"{sort:02d}_{content.safe_name(title)}" if sort else content.safe_name(title)
        new_key = f"{folder}/{stem}"
        has_old_image = bool(old_key and any(f.suffix.lower() in content.IMAGE_EXT for f in _files_of(root, old_key)))
        if not text and image is None and not (has_old_image and not body.remove_image):
            raise HTTPException(400, "請填答案，或上傳一張圖片")
        head = f"關鍵字：{'、'.join(keywords)}\n\n" if keywords else ""
        saved_image = _save_files(root, old_key, new_key, head + text + "\n" if (text or head) else None,
                                  image, body.remove_image)
        result = _sync(root, new_key, saved_image)

    def record(conn):
        if body.outside_id:
            conn.execute("DELETE FROM kb_articles WHERE id = %s AND source_path IS NULL", (body.outside_id,))
        audit(conn, user, "update" if old_key or body.outside_id else "insert", "kb_articles", new_key,
              f"{'修改' if old_key or body.outside_id else '新增'}常見問題「{title}」",
              after={"title": title, "category": body.category, "keywords": keywords, "image": bool(saved_image)})
    run(record)
    return result


@router.post("/faqs/delete")
def delete_faq(body: KeyIn, user: str = Depends(current_user)) -> dict:
    root = _root()
    key = _check_key(root, body.key, (content.KB,))
    with content._lock:
        files = _files_of(root, key)
        if not files:
            raise HTTPException(404, "找不到這一題的檔案，可能已經被刪掉了")
        for f in files:
            _remove(f)
        content.sync(force=True)
    run(lambda conn: audit(conn, user, "delete", "kb_articles", key,
                           f"刪除常見問題「{content.split_title(key.rsplit('/', 1)[-1])[0]}」"))
    return {"ok": True}


# ====================================================================== 榜單
def read_honors(path: Path) -> list[dict]:
    rows = list(csv.reader(io.StringIO(content.read_text(path))))
    if not rows:
        return []
    header = [content.HONOR_ALIASES.get(h.strip(), h.strip()) for h in rows[0]]
    out = []
    for raw in rows[1:]:
        if not any(c.strip() for c in raw):
            continue
        r = {h: (raw[i].strip() if i < len(raw) else "") for i, h in enumerate(header)}
        out.append({"school_year": re.sub(r"\D", "", r.get("學年度", "")), "category": r.get("類別", ""),
                    "display_name": r.get("顯示名稱", ""), "school_name": r.get("學校", ""),
                    "achievement": r.get("成績", ""), "consent": r.get("家長同意", "").lower() in content.YES,
                    "sort": re.sub(r"\D", "", r.get("排序", ""))})
    return out


class HonorRow(BaseModel):
    school_year: int = Field(ge=80, le=3000)       # 民國年；寫成西元年也會自動換算
    category: str = Field(min_length=1, max_length=30)
    display_name: str = Field(min_length=1, max_length=30)
    school_name: str | None = Field(default=None, max_length=40)
    achievement: str = Field(min_length=1, max_length=80)
    consent: bool = False
    sort: int | None = Field(default=None, ge=0, le=9999)


class HonorsIn(BaseModel):
    rows: list[HonorRow] = Field(max_length=500)


@router.post("/honors/save")
def save_honors(body: HonorsIn, user: str = Depends(current_user)) -> dict:
    root = _root()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(content.HONOR_HEADERS)
    for i, r in enumerate(body.rows, start=1):
        year = r.school_year - 1911 if r.school_year > 1911 else r.school_year
        w.writerow([year, r.category.strip(), r.display_name.strip(), (r.school_name or "").strip(),
                    r.achievement.strip(), "是" if r.consent else "否", r.sort if r.sort is not None else i])
    with content._lock:
        _write(root / content.HONOR / content.HONOR_FILE, buf.getvalue())
        report = content.sync(force=True)
    run(lambda conn: audit(conn, user, "update", "honor_roll", None,
                           f"更新榜單（{len(body.rows)} 筆，公開 {sum(r.consent for r in body.rows)} 筆）"))
    return {"ok": True, "problems": [p for p in (report.problems if report else []) if p.startswith(content.HONOR)]}


# ====================================================================== AI 讀圖（把圖片轉成文字，填進表單給人檢查）
class ReadImageIn(BaseModel):
    image: ImageIn
    purpose: Literal["calendar", "announcement", "kb"] = "announcement"
    hint: str = Field(default="", max_length=200)


@router.post("/read-image")
def read_image(body: ReadImageIn, user: str = Depends(current_user)) -> dict:
    if not vision.available():
        raise HTTPException(400, "AI 讀圖要先在 .env 設定 ANTHROPIC_API_KEY。也可以自己把圖片上的重點打進「內容」。")
    jpeg = _decode_image(body.image)
    try:
        return vision.read_image_bytes(media.shrink_for_ai(jpeg), body.purpose, body.hint)
    except vision.VisionError as e:
        raise HTTPException(400, str(e)) from None

