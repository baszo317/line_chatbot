"""「補習班資料」資料夾 → 資料庫：丟檔案就更新，不用打指令。

資料夾結構（第一次啟動自動建立，並把資料庫裡現有的常見問題、公告、榜單匯出成檔案）：

    補習班資料/
      使用說明.txt
      行事曆/          最新丟進來的那一份 = 家長看到的行事曆（圖片；可加同檔名 .txt 當文字版）
      公告/            一個檔案（.txt 或圖片）＝一則公告；檔案在＝有效，刪掉＝下架
        停課/ 考試/ 活動/ 招生/      放在子資料夾就是那一類；直接放在「公告」裡＝一般公告
      常見問題/
        聯絡/ 交通/ 課程/ 收費/ 規定/ 招生/ 其他/
                       一個 .txt＝一題，檔名就是問題（也是 LINE 上的按鈕，20 字以內）
      榜單/榜單.csv    用 Excel 編輯；「家長同意」填「是」才會公開
      _同步結果.txt     每次同步後自動更新：家長現在看得到什麼、哪些檔案有問題

・同一則的檔案用檔名配對：「颱風停課.txt」＋「颱風停課.jpg」＝一則有附圖的公告。
・.txt 最前面可以加：「顯示到：2026/10/21」（過了就自動下架）、「關鍵字：電話、聯絡」（沒有 AI 時比對用）、
  「年級：國一、國二」（公告、行事曆只給這些年級；沒寫就看年級資料夾和檔名，例如「國一行事曆」＝國一）。
・後台（/admin）的「公告與行事曆」「常見問題」「榜單」頁面也是寫進這個資料夾，再立刻同步；兩種方法可以混著用。
・檔名前加數字可以排順序：「01_聯絡電話.txt」→ 標題「聯絡電話」，排第一。
・只放圖片時：有 ANTHROPIC_API_KEY 的話，會在旁邊產生同名 .txt 的「AI 草稿」。
  草稿裡【AI 草稿】開頭的行刪掉、存檔後，機器人才會用這段文字回答；在那之前只會傳圖片。

bot 執行時每 30 秒檢查一次資料夾（BOT_CONTENT_POLL_SECONDS），有變動才同步。
資料夾位置：.env 的 BOT_CONTENT_DIR（預設是專案裡的「補習班資料」；放在 Google 雲端硬碟的同步資料夾裡，
就能從別台電腦或手機丟檔案）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, knowledge, media, vision
from .config import settings

log = logging.getLogger(__name__)
TW = ZoneInfo("Asia/Taipei")

CALENDAR, NOTICES, KB, HONOR = "行事曆", "公告", "常見問題", "榜單"
HONOR_FILE = "榜單.csv"
GUIDE_FILE = "使用說明.txt"
REPORT_FILE = "_同步結果.txt"

NOTICE_SUBS = {"一般": "general", "停課": "closure", "考試": "exam", "活動": "event", "招生": "enrollment"}
NOTICE_SUB_NAME = {v: k for k, v in NOTICE_SUBS.items()}
KB_SUBS = {"聯絡": "contact", "交通": "traffic", "課程": "course", "收費": "fee", "規定": "policy",
           "招生": "enrollment", "其他": "other"}
KB_SUB_NAME = {v: k for k, v in KB_SUBS.items()}
# 行事曆、公告可以再放一層年級資料夾；沒有年級資料夾＝全部年級
GRADE_FOLDERS = {"國小": (6,), "國一": (7,), "國二": (8,), "國三": (9,), "國中": (7, 8, 9)}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_EXT = {".txt", ".md"}
HINTS = {
    ".heic": "iPhone 的 HEIC 格式 LINE 不支援：請改存成 JPG（iPhone「設定 → 相機 → 格式 → 最相容」），或截圖後再放進來",
    ".heif": "iPhone 的 HEIF 格式 LINE 不支援：請改存成 JPG，或截圖後再放進來",
    ".pdf": "PDF 不能直接用：請截圖成 JPG／PNG，或把文字貼到記事本存成 .txt",
    ".doc": "Word 檔不能直接用：請把文字貼到記事本，存成 .txt",
    ".docx": "Word 檔不能直接用：請把文字貼到記事本，存成 .txt",
    ".xls": "Excel 檔不能直接用：榜單請在 Excel「另存新檔 → CSV UTF-8（逗號分隔）」",
    ".xlsx": "Excel 檔不能直接用：榜單請在 Excel「另存新檔 → CSV UTF-8（逗號分隔）」",
}
IGNORED = {GUIDE_FILE.casefold(), REPORT_FILE.casefold(), "desktop.ini", "thumbs.db", ".ds_store"}
DRAFT_MARK = "【AI 草稿】"
CALENDAR_BATCH = timedelta(minutes=10)     # 10 分鐘內一起丟進來的行事曆算同一批（例如正反兩面）
STILL_COPYING = 3                           # 秒；剛改過的檔案可能還在複製，下一輪再同步
MAX_DRAFTS_PER_SYNC = 3
PLACEHOLDER = {
    KB: "（答案在圖片裡，請看下方圖片）",
    NOTICES: "（內容在圖片裡，請看下方圖片）",
    CALENDAR: "（行事曆在圖片裡，請看下方圖片）",
}
SORT_PREFIX = re.compile(r"^(\d{1,3})[\s_\-.、．]+(?=\S)")
GENERIC_NAME = re.compile(
    r"^(img|dsc|dscn|dcim|pxl|photo|image|screenshot|screen shot|line_album|line_|s__|螢幕擷取畫面|截圖|未命名|新增)",
    re.IGNORECASE)
ENDS_LINE = re.compile(r"^(?:顯示到|到期日?|下架日?|截止日?)\s*[:：]\s*(.+)$")
KEYWORDS_LINE = re.compile(r"^關鍵字\s*[:：]\s*(.*)$")
GRADES_LINE = re.compile(r"^(?:適用)?年級\s*[:：]\s*(.*)$")
ALL_GRADES_WORDS = {"全部", "全部年級", "全年級", "所有年級", "不限", "不分年級", "都可以"}
# 檔名、標題裡的年級字：「國一行事曆」就是國一的
TITLE_GRADES = {6: re.compile(r"國小|小六|六年級"), 7: re.compile(r"國一|七年級"), 8: re.compile(r"國二|八年級"),
                9: re.compile(r"國三|九年級")}


# ====================================================================== 讀檔
@dataclass
class Entry:
    rel: str                  # 相對路徑，用 / 分隔，例：行事曆/11月行事曆.jpg
    path: Path
    size: int
    mtime_ns: int
    sha: str = ""
    first_seen: datetime | None = None
    ai_draft_at: datetime | None = None

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()


@dataclass
class TextInfo:
    body: str
    ends_on: date | None = None
    keywords: list[str] = field(default_factory=list)
    grades: tuple[int, ...] | None = None      # 「年級：」那一行；None＝沒寫，() ＝全部年級
    draft: bool = False
    problems: list[str] = field(default_factory=list)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):          # 記事本存成「Unicode」
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp950"):              # 一般是 UTF-8；舊版記事本可能是 Big5
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    """寫成 Windows 記事本、Excel 都讀得懂的格式（UTF-8 有 BOM、CRLF 換行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)


def parse_date(text: str, today: date) -> date | None:
    """2026/10/21、2026-10-21、115/10/21（民國）、2026年10月21日、10/21（沒寫年 → 今年或明年）"""
    m = re.search(r"(\d{2,4})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})", text)
    try:
        if m:
            y, mo, d = (int(x) for x in m.groups())
            return date(y + 1911 if y < 1911 else y, mo, d)
        m = re.search(r"(\d{1,2})\s*[/\-.月]\s*(\d{1,2})", text)
        if m:
            mo, d = (int(x) for x in m.groups())
            result = date(today.year, mo, d)
            if result < today - timedelta(days=60):
                result = date(today.year + 1, mo, d)
            return result
    except ValueError:
        return None
    return None


def parse_text(raw: str, today: date) -> TextInfo:
    lines = raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    draft = any(line.strip().startswith(DRAFT_MARK) for line in lines)
    lines = [line for line in lines if not line.strip().startswith(DRAFT_MARK)]
    info = TextInfo(body="", draft=draft)
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        m = ENDS_LINE.match(s)
        if m:
            info.ends_on = parse_date(m.group(1), today)
            if not info.ends_on:
                info.problems.append(f"看不懂「{s}」的日期，請寫成 2026/10/21")
            i += 1
            continue
        m = KEYWORDS_LINE.match(s)
        if m:
            info.keywords = [k for k in re.split(r"[、,，;；\s]+", m.group(1)) if k]
            i += 1
            continue
        m = GRADES_LINE.match(s)
        if m:
            info.grades = parse_grades(m.group(1), info.problems)
            i += 1
            continue
        break
    info.body = "\n".join(line.rstrip() for line in lines[i:]).strip()
    return info


GRADE_TOKENS = {"國小": (6,), "小六": (6,), "六年級": (6,), "國中一年級": (7,), "國一": (7,), "七年級": (7,),
                "國中二年級": (8,), "國二": (8,), "八年級": (8,), "國中三年級": (9,), "國三": (9,), "九年級": (9,),
                "國中": (7, 8, 9)}
_GRADE_TOKEN = re.compile("|".join(sorted([*GRADE_TOKENS, *ALL_GRADES_WORDS], key=len, reverse=True)))


def parse_grades(text: str, problems: list[str]) -> tuple[int, ...] | None:
    """「國一、國二」「國一國二」→ (7, 8)；「國中」→ (7, 8, 9)；「全部」或空白 → ()。看不懂時記在 problems、回傳 None。"""
    if not text.strip():
        return ()
    tokens = _GRADE_TOKEN.findall(text)
    if not tokens:
        problems.append(f"看不懂「年級：{text.strip()}」，請寫國小、國一、國二、國三、國中或全部，例如「年級：國一、國二」")
        return None
    if any(t in ALL_GRADES_WORDS for t in tokens):
        return ()
    found = {g for t in tokens for g in GRADE_TOKENS[t]}
    return () if found == {6, 7, 8, 9} else tuple(sorted(found))


def title_grades(title: str) -> set[int]:
    return {g for g, pattern in TITLE_GRADES.items() if pattern.search(title)}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: Path) -> list[Entry]:
    entries = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if (name.casefold() in IGNORED or name.startswith(("~$", ".", "~"))
                or name.lower().endswith((".tmp", ".crdownload", ".part", ".partial"))):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append(Entry(p.relative_to(root).as_posix(), p, st.st_size, st.st_mtime_ns))
    return sorted(entries, key=lambda e: e.rel)


def signature(entries: list[Entry]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(f"{e.rel}\0{e.size}\0{e.mtime_ns}\n".encode())
    return h.hexdigest()


# ====================================================================== 同步結果
@dataclass
class Report:
    folder: str = ""
    synced_at: datetime | None = None
    calendar: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    kb: list[str] = field(default_factory=list)
    honor_public: int = 0
    honor_hidden: int = 0
    drafts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unused_calendars: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)       # 不是從資料夾來、但家長看得到的資料
    exported: list[str] = field(default_factory=list)
    error: str | None = None

    def problem(self, rel: str, message: str) -> None:
        self.problems.append(f"{rel}：{message}")

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["synced_at"] = self.synced_at.isoformat() if self.synced_at else None
        return d

    def text(self) -> str:
        t = self.synced_at or datetime.now(TW)
        lines = [f"補習班資料 同步結果（{t:%Y/%m/%d %H:%M}）",
                 "這個檔案是系統自動產生的，改了也沒用；有問題的檔案修好後，約 30 秒會重新同步。", ""]
        if self.error:
            lines += ["⚠ 同步失敗：" + self.error, ""]
        lines.append("■ 家長現在看得到")
        lines.append("行事曆：" + ("、".join(self.calendar) if self.calendar else "（沒有）"))
        lines.append(f"公告（{len(self.notices)}）：" + ("、".join(self.notices) if self.notices else "（沒有）"))
        lines.append(f"常見問題（{len(self.kb)}）：")
        lines += [f"  ・{x}" for x in self.kb] or ["  （沒有）"]
        hidden = f"；另有 {self.honor_hidden} 筆沒有家長同意，不會公開" if self.honor_hidden else ""
        lines.append(f"榜單：公開 {self.honor_public} 筆{hidden}")
        if self.outside:
            lines += ["", "■ 不是從這個資料夾來的資料（例如用 SQL 新增），家長也看得到："]
            lines += [f"  ・{x}" for x in self.outside]
        if self.drafts:
            lines += ["", "■ 待確認的 AI 草稿（機器人還不會用這些文字，只會傳圖片）",
                      "  打開檔案對照圖片，把【AI 草稿】開頭的行刪掉再存檔："]
            lines += [f"  ・{x}" for x in self.drafts]
        if self.problems:
            lines += ["", "■ 有問題的檔案（這些沒有更新到機器人）"]
            lines += [f"  ・{x}" for x in self.problems]
        if self.expired:
            lines += ["", "■ 已經過了「顯示到」的日期（家長看不到了，可以刪掉檔案）"]
            lines += [f"  ・{x}" for x in self.expired]
        if self.unused_calendars:
            lines += ["", "■ 舊的行事曆（只用最新丟進來的那一批，這些沒有用到，可以刪掉）"]
            lines += [f"  ・{x}" for x in self.unused_calendars]
        if self.notes or self.exported:
            lines += ["", "■ 其他"]
            lines += [f"  ・{x}" for x in self.notes]
            if self.exported:
                lines.append(f"  ・資料夾裡少了某一區（第一次使用，或被刪掉／搬走），已把資料庫裡的 {len(self.exported)} 筆資料匯出成檔案")
        return "\n".join(lines) + "\n"


# ====================================================================== 分組
@dataclass
class Group:
    section: str
    sub: str | None           # 公告／常見問題的子資料夾（分類）
    folder: str               # 相對資料夾，例：常見問題/交通
    stem: str                 # 檔名（不含副檔名）
    images: list[Entry] = field(default_factory=list)
    texts: list[Entry] = field(default_factory=list)
    grades: tuple[int, ...] = ()   # 年級資料夾的年級；空的＝沒有年級資料夾
    _info: TextInfo | None = None
    _parsed: bool = False

    @property
    def key(self) -> str:      # 存在資料表的 source_path
        return f"{self.folder}/{self.stem}"

    def text_info(self, today: date) -> TextInfo | None:
        """同檔名 .txt 的內容（只讀一次）。"""
        if not self._parsed:
            self._parsed = True
            if self.texts:
                self._info = parse_text(read_text(self.texts[0].path), today)
        return self._info

    @property
    def first_seen(self) -> datetime:
        times = [e.first_seen for e in (self.images or self.texts) if e.first_seen]
        return max(times) if times else datetime.now(TW)


def build_groups(entries: list[Entry], report: Report) -> tuple[list[Group], Entry | None]:
    groups: dict[str, Group] = {}
    honor: Entry | None = None
    for e in entries:
        parts = e.rel.split("/")
        top = parts[0]
        if len(parts) == 1:
            report.problem(e.rel, "放在最外層不會被使用，請放進「行事曆」「公告」「常見問題」或「榜單」資料夾")
            continue
        if top == HONOR:
            if e.path.name == HONOR_FILE:
                honor = e
            else:
                report.problem(e.rel, f"榜單資料夾只看「{HONOR_FILE}」這個檔案"
                               + (f"（{HINTS[e.ext]}）" if e.ext in HINTS else ""))
            continue
        if top not in (CALENDAR, NOTICES, KB):
            report.problem(e.rel, f"不認得「{top}」這個資料夾，請放進「行事曆」「公告」「常見問題」或「榜單」")
            continue
        if e.ext in HINTS:
            report.problem(e.rel, HINTS[e.ext])
            continue
        if e.ext not in IMAGE_EXT and e.ext not in TEXT_EXT:
            report.problem(e.rel, "只能放圖片（JPG、PNG）或文字檔（.txt）")
            continue

        sub = None
        grades: set[int] = set()
        if top in (NOTICES, CALENDAR):                 # 中間的資料夾：分類（公告才有）或年級
            for folder_name in parts[1:-1]:
                if folder_name in GRADE_FOLDERS:
                    grades.update(GRADE_FOLDERS[folder_name])
                elif top == NOTICES and folder_name in NOTICE_SUBS and sub is None:
                    sub = folder_name
                else:
                    kinds = "年級（國小、國一、國二、國三、國中）" if top == CALENDAR else \
                        f"分類（{'、'.join(NOTICE_SUBS)}）或年級（國小、國一、國二、國三、國中）"
                    report.notes.append(f"{e.rel}：不認得「{folder_name}」資料夾，略過這一層（可用的：{kinds}）")
        if top == KB:
            sub = parts[1] if len(parts) >= 3 and parts[1] in KB_SUBS else "其他"
            if len(parts) < 3 or parts[1] not in KB_SUBS:
                report.notes.append(f"{e.rel}：沒有放在分類資料夾裡，當成「其他」"
                                    f"（可用的分類：{'、'.join(KB_SUBS)}）")
        folder = "/".join(parts[:-1])
        stem = e.path.stem.strip()
        g = groups.setdefault(f"{folder}/{stem}", Group(top, sub, folder, stem, grades=tuple(sorted(grades))))
        (g.images if e.ext in IMAGE_EXT else g.texts).append(e)
    for g in groups.values():
        if len(g.images) > 1:
            report.notes.append(f"{g.key}：有 {len(g.images)} 張同檔名的圖片，只用「{g.images[0].path.name}」")
        if len(g.texts) > 1:
            report.notes.append(f"{g.key}：有 {len(g.texts)} 個同檔名的文字檔，只用「{g.texts[0].path.name}」")
    return list(groups.values()), honor


def effective_grades(g: Group, today: date, report: Report) -> tuple[int, ...]:
    """公告、行事曆的適用年級（空的＝全部年級）：
    1. .txt 裡有「年級：」那一行 → 照寫的
    2. 檔名寫了年級（「國一行事曆」）→ 檔名的年級；同時有年級資料夾時取兩者都有的（放在「國中」的「國一行事曆」＝國一）
    3. 年級資料夾
    """
    info = g.text_info(today)
    if info and info.grades is not None:
        return info.grades
    named = title_grades(split_title(g.stem)[0])
    folder = set(g.grades)
    if named and folder:
        result = named & folder
        if not result:
            result = named
            report.notes.append(f"{g.key}：檔名寫「{knowledge.grades_text(named)}」，但放在「{knowledge.grades_text(folder)}」"
                                f"資料夾，以檔名為準")
    else:
        result = named or folder
    return () if result == {6, 7, 8, 9} else tuple(sorted(result))


def split_title(stem: str) -> tuple[str, int]:
    """「01_聯絡電話」→（聯絡電話, 1）"""
    m = SORT_PREFIX.match(stem)
    if m:
        return stem[m.end():].strip(), int(m.group(1))
    return stem, 0


# ====================================================================== 同步
_lock = threading.RLock()     # 後台存檔時會先拿鎖再呼叫 sync()，所以要可以重複進入
_state: dict = {"signature": None, "report": None}


def last_report() -> dict | None:
    r = _state.get("report")
    return r.as_dict() if r else None


def sync(force: bool = False, no_drafts: set[str] = frozenset()) -> Report | None:
    """檢查資料夾，有變動就更新資料庫。回傳同步結果；資料夾沒變動時回傳 None。
    no_drafts：不要幫這些圖片產生 AI 草稿（後台存的）。"""
    root = settings.content_dir
    if root is None:
        return None
    with _lock:
        report = Report(folder=str(root))
        try:
            root.mkdir(parents=True, exist_ok=True)
            entries = scan(root)
            now_ts = time.time()
            if not force and any(now_ts - e.mtime_ns / 1e9 < STILL_COPYING for e in entries):
                return None                                     # 還在複製，下一輪再說
            if not force and signature(entries) == _state["signature"] and _state["report"]:
                return None
            ensure_structure(root, report)
            entries = scan(root)
            refresh_records(entries)
            groups, honor = build_groups(entries, Report())     # 先分組，看要不要產生 AI 草稿
            if make_drafts(root, groups, report, set(no_drafts)):
                entries = scan(root)
                refresh_records(entries)
            groups, honor = build_groups(entries, report)
            apply(groups, honor, report)
            _state["signature"] = signature(entries)
        except Exception as e:  # 寫進同步結果，下一輪再試
            log.exception("資料夾同步失敗")
            report.error = str(e)[:300]
            _state["signature"] = None
        report.synced_at = datetime.now(TW)
        _state["report"] = report
        try:
            write_text(root / REPORT_FILE, report.text())
        except OSError as e:
            log.warning("寫不進 %s：%s", REPORT_FILE, e)
        knowledge.invalidate()
        return report


def refresh_records(entries: list[Entry]) -> None:
    """content_files：記住每個檔案的指紋和「第一次看到」的時間。"""
    with db.get_pool().connection() as conn, conn.transaction():
        rows = {r["path"]: r for r in conn.execute(
            "SELECT path, sha256, bytes, mtime_ns, first_seen_at, ai_draft_at FROM content_files").fetchall()}
        now = datetime.now(TW)
        present = {e.rel for e in entries}
        moved_from: dict[str, dict] = {}                 # 這次不見的檔案，用內容指紋找
        for path, r in rows.items():
            if path not in present:
                moved_from.setdefault(r["sha256"], r)
        for e in entries:
            r = rows.get(e.rel)
            if r and r["bytes"] == e.size and r["mtime_ns"] == e.mtime_ns:
                e.sha, e.first_seen, e.ai_draft_at = r["sha256"], r["first_seen_at"], r["ai_draft_at"]
                continue
            e.sha = sha256_of(e.path)
            if r and r["sha256"] == e.sha:            # 只是存檔時間變了，內容一樣
                e.first_seen, e.ai_draft_at = r["first_seen_at"], r["ai_draft_at"]
            elif r is None and e.sha in moved_from:  # 搬了資料夾或改了檔名（例如在後台改標題）：當成同一個檔案
                old = moved_from.pop(e.sha)
                e.first_seen, e.ai_draft_at = old["first_seen_at"], old["ai_draft_at"]
            else:
                e.first_seen, e.ai_draft_at = now, None
            conn.execute(
                """INSERT INTO content_files (path, sha256, bytes, mtime_ns, first_seen_at, last_seen_at, ai_draft_at)
                   VALUES (%s, %s, %s, %s, %s, now(), %s)
                   ON CONFLICT (path) DO UPDATE
                      SET sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes, mtime_ns = EXCLUDED.mtime_ns,
                          first_seen_at = EXCLUDED.first_seen_at, last_seen_at = now(),
                          ai_draft_at = EXCLUDED.ai_draft_at""",
                (e.rel, e.sha, e.size, e.mtime_ns, e.first_seen, e.ai_draft_at))
        gone = [p for p in rows if p not in present]
        if gone:
            conn.execute("DELETE FROM content_files WHERE path = ANY(%s)", (gone,))


def ensure_media(conn, e: Entry) -> int:
    """資料夾裡的圖片 → media_files（同一張圖只存一次）。圖片不能用時丟出 media.ImageError。"""
    row = conn.execute("SELECT id FROM media_files WHERE source_hash = %s ORDER BY id LIMIT 1", (e.sha,)).fetchone()
    if row:
        return row["id"]
    saved = media.save(e.path.read_bytes(), original_name=e.path.name, uploaded_by="folder", note=e.rel,
                       source_hash=e.sha, conn=conn)
    return saved["id"]


def make_drafts(root: Path, groups: list[Group], report: Report, skip: set[str] = frozenset()) -> bool:
    """只有圖片、沒有文字檔的：請 AI 讀圖，寫成同名 .txt 的草稿。回傳有沒有寫出新檔案。
    skip：後台剛存的圖片（後台有自己的「AI 讀圖」按鈕，不另外產生草稿檔）。"""
    if skip:
        with db.get_pool().connection() as conn:
            conn.execute("UPDATE content_files SET ai_draft_at = now() WHERE path = ANY(%s) AND ai_draft_at IS NULL",
                         (list(skip),))
    if not (settings.ai_drafts and vision.available()):
        return False
    todo = [g for g in groups
            if g.images and not g.texts and g.images[0].ai_draft_at is None and g.images[0].rel not in skip]
    wrote = False
    for g in todo[:MAX_DRAFTS_PER_SYNC]:
        image = g.images[0]
        purpose = {CALENDAR: "calendar", NOTICES: "announcement", KB: "kb"}[g.section]
        with db.get_pool().connection() as conn:
            conn.execute("UPDATE content_files SET ai_draft_at = now() WHERE path = %s", (image.rel,))
            try:
                with conn.transaction():
                    media_id = ensure_media(conn, image)
            except media.ImageError as e:
                report.problem(image.rel, str(e))
                continue
        try:
            result = vision.read_image(media_id, purpose, hint=f"檔名：{g.stem}")
        except vision.VisionError as e:
            report.problem(image.rel, f"{e}（不會自動重試；可以自己建立同名 .txt 打上文字）")
            continue
        unclear = "、".join(result["unclear"]) or "沒有"
        draft = (f"{DRAFT_MARK}請對照圖片檢查下面的文字，沒問題就把這兩行刪掉再存檔，機器人才會開始使用。\n"
                 f"{DRAFT_MARK}AI 看不清楚的地方：{unclear}\n\n{result['text']}\n")
        target = image.path.with_suffix(".txt")
        if not target.exists():
            write_text(target, draft)
            wrote = True
            log.info("已產生 AI 草稿：%s", target.relative_to(root).as_posix())
    if len(todo) > MAX_DRAFTS_PER_SYNC:
        report.notes.append(f"還有 {len(todo) - MAX_DRAFTS_PER_SYNC} 張圖片等著產生 AI 草稿，下一輪會繼續")
    return wrote


@dataclass
class Item:
    title: str
    body: str
    sort: int
    image_id: int | None
    ends_on: date | None
    keywords: list[str]


def build_item(conn, g: Group, report: Report, today: date) -> Item | None:
    info = g.text_info(today)
    text_entry = g.texts[0] if g.texts else None
    if text_entry:
        for p in info.problems:
            report.problem(text_entry.rel, p)
        if info.draft:
            report.drafts.append(text_entry.rel)
    image_id = None
    image_failed = False
    if g.images:
        try:
            with conn.transaction():
                image_id = ensure_media(conn, g.images[0])
        except media.ImageError as e:
            report.problem(g.images[0].rel, str(e))
            image_failed = True
    body = info.body if info and not info.draft else ""
    if not body:
        if image_id is None:
            if text_entry and not info.draft:
                report.problem(text_entry.rel, "內容是空的" + ("，圖片也不能用" if image_failed else ""))
            return None
        body = PLACEHOLDER[g.section]

    title, sort = split_title(g.stem)
    if GENERIC_NAME.match(title):
        seen = g.first_seen.astimezone(TW)
        if g.section == CALENDAR:
            title = f"{seen.month} 月行事曆"
        else:
            report.notes.append(f"{g.key}：檔名看起來是相機或截圖的預設名稱，家長會看到這個標題，建議改成有意義的名字")
    if g.section == KB and len(title) > 20:
        report.notes.append(f"{g.key}：標題超過 20 字，LINE 按鈕上會被截斷")
    return Item(title=title, body=body, sort=sort, image_id=image_id,
                ends_on=info.ends_on if info else None, keywords=info.keywords if info else [])


def _ends_at(d: date | None) -> datetime | None:
    return datetime.combine(d + timedelta(days=1), dtime(0), TW) if d else None


def apply(groups: list[Group], honor: Entry | None, report: Report) -> None:
    today = datetime.now(TW).date()
    with db.get_pool().connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7240318)")   # 同時只有一個同步在寫
        _apply_kb(conn, [g for g in groups if g.section == KB], report, today)
        _apply_notices(conn, [g for g in groups if g.section in (NOTICES, CALENDAR)], report, today)
        _apply_honor(conn, honor, report)
        conn.execute("""DELETE FROM media_files m
                         WHERE m.uploaded_by = 'folder'
                           AND NOT EXISTS (SELECT 1 FROM kb_articles a WHERE a.image_id = m.id)
                           AND NOT EXISTS (SELECT 1 FROM announcements n WHERE n.image_id = m.id)""")
        _outside(conn, report)


def _apply_kb(conn, groups: list[Group], report: Report, today: date) -> None:
    owned = {r["source_path"]: r["id"] for r in conn.execute(
        "SELECT id, source_path FROM kb_articles WHERE source_path LIKE %s", (KB + "/%",)).fetchall()}
    items = {}
    for g in sorted(groups, key=lambda g: g.key):
        item = build_item(conn, g, report, today)
        if item:
            items[g.key] = (g, item)
    for key, row_id in owned.items():                          # 檔案被刪掉 → 這題下架
        if key not in items:
            conn.execute("DELETE FROM kb_articles WHERE id = %s", (row_id,))
    used: dict[str, str] = {}
    for key, (g, it) in items.items():
        if it.title in used:
            report.problem(key, f"標題和「{used[it.title]}」重複，同一個問題只能有一個檔案")
            continue
        holder = conn.execute("SELECT id, source_path FROM kb_articles WHERE title = %s", (it.title,)).fetchone()
        row_id = owned.get(key)
        if holder and holder["id"] != row_id:
            if holder["source_path"] is None and row_id is None:
                row_id = holder["id"]                          # 以前用其他方式建立的同一題 → 改由這個檔案管理
            else:
                report.problem(key, "資料庫裡已經有另一題用了同樣的標題")
                continue
        values = (KB_SUBS[g.sub or "其他"], it.title, it.body, it.keywords, it.sort, it.image_id, key)
        if row_id:
            conn.execute("""UPDATE kb_articles SET category = %s, title = %s, content = %s, keywords = %s,
                                   sort_order = %s, image_id = %s, source_path = %s, is_active = true,
                                   audience = 'public'
                             WHERE id = %s""", (*values, row_id))
        else:
            conn.execute("""INSERT INTO kb_articles (category, title, content, keywords, sort_order, image_id,
                                                     source_path)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""", values)
        used[it.title] = key
        report.kb.append(f"{g.sub or '其他'}／{it.title}" + ("（附圖）" if it.image_id else ""))


def _apply_notices(conn, groups: list[Group], report: Report, today: date) -> None:
    owned = {r["source_path"]: r["id"] for r in conn.execute(
        "SELECT id, source_path FROM announcements WHERE source_path LIKE %s OR source_path LIKE %s",
        (NOTICES + "/%", CALENDAR + "/%")).fetchall()}

    grades_of = {g.key: effective_grades(g, today, report) for g in groups}

    # 行事曆：只用最新丟進來的那一批
    calendars = [g for g in groups if g.section == CALENDAR]
    live_calendars: list[Group] = []
    built: dict[str, tuple[Group, Item]] = {}
    buckets: dict[tuple[int, ...], list[Group]] = {}
    for g in calendars:                                        # 每個年級各自用最新的一批
        buckets.setdefault(grades_of[g.key], []).append(g)
    for bucket in buckets.values():
        ordered = sorted(bucket, key=lambda g: (g.first_seen, max(e.mtime_ns for e in g.images + g.texts), g.stem),
                         reverse=True)
        latest = ordered[0].first_seen
        for g in ordered:
            if latest - g.first_seen <= CALENDAR_BATCH:
                live_calendars.append(g)
            else:
                report.unused_calendars.append(g.key)
    for g in sorted(live_calendars, key=lambda g: (grades_of[g.key], g.stem)) + sorted(
            (g for g in groups if g.section == NOTICES), key=lambda g: g.key):
        item = build_item(conn, g, report, today)
        if item:
            built[g.key] = (g, item)

    for key, row_id in owned.items():
        if key not in built:
            conn.execute("DELETE FROM announcements WHERE id = %s", (row_id,))
    for key, (g, it) in built.items():
        category = "calendar" if g.section == CALENDAR else NOTICE_SUBS.get(g.sub or "一般", "general")
        if category == "general" and re.search(r"停課|颱風|停班|放假", it.title):
            category = "closure"                               # 停課公告：課程介紹會置頂提醒
        row_id = owned.get(key)
        if row_id is None:                                     # 以前用其他方式建立、標題相同的有效公告 → 改由檔案管理
            holder = conn.execute(
                """SELECT id FROM announcements WHERE source_path IS NULL AND title = %s
                     AND (ends_at IS NULL OR ends_at > now()) ORDER BY id LIMIT 1""", (it.title,)).fetchone()
            row_id = holder["id"] if holder else None
        grades = list(grades_of[g.key]) or None
        values = (it.title, it.body, category, it.image_id, _ends_at(it.ends_on), key, grades)
        if row_id:
            conn.execute("""UPDATE announcements SET title = %s, body = %s, category = %s, image_id = %s,
                                   ends_at = %s, source_path = %s, grades = %s::smallint[], audience = 'all'
                             WHERE id = %s""", (*values, row_id))
        else:
            conn.execute("""INSERT INTO announcements (title, body, category, image_id, ends_at, source_path, grades)
                            VALUES (%s, %s, %s, %s, %s, %s, %s::smallint[])""", values)
        tag = knowledge.grade_tag({"grades": grades})
        label = it.title + (f"（{tag}）" if tag else "") + ("（附圖）" if it.image_id else "")
        if it.ends_on and it.ends_on < today:
            report.expired.append(f"{key}（顯示到 {it.ends_on:%Y/%m/%d}）")
        elif g.section == CALENDAR:
            report.calendar.append(label)
        else:
            until = f"，顯示到 {it.ends_on:%m/%d}" if it.ends_on else ""
            report.notices.append(f"{label}［{NOTICE_SUB_NAME.get(category, '一般')}{until}］")


HONOR_HEADERS = ["學年度", "類別", "顯示名稱", "學校", "成績", "家長同意", "排序"]
HONOR_ALIASES = {"姓名": "顯示名稱", "名字": "顯示名稱", "學生": "顯示名稱", "錄取": "成績", "成績／錄取": "成績",
                 "同意": "家長同意", "家長同意公開": "家長同意", "順序": "排序", "就讀學校": "學校"}
YES = {"是", "y", "yes", "v", "o", "1", "true", "同意", "✓", "✔", "○"}


def _apply_honor(conn, honor: Entry | None, report: Report) -> None:
    conn.execute("DELETE FROM honor_roll WHERE source_path LIKE %s", (HONOR + "/%",))
    if honor is None:
        return
    rows = list(csv.reader(io.StringIO(read_text(honor.path))))
    if not rows:
        return
    header = [HONOR_ALIASES.get(h.strip(), h.strip()) for h in rows[0]]
    missing = [h for h in ("學年度", "類別", "顯示名稱", "成績") if h not in header]
    if missing:
        report.problem(honor.rel, f"第一列要有欄位名稱，缺少：{'、'.join(missing)}（範例：{','.join(HONOR_HEADERS)}）")
        return
    for line_no, raw in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue
        r = {h: (raw[i].strip() if i < len(raw) else "") for i, h in enumerate(header)}
        year = re.sub(r"\D", "", r["學年度"])
        if not (year and r["類別"] and r["顯示名稱"] and r["成績"]):
            report.problem(f"{honor.rel} 第 {line_no} 列", "學年度、類別、顯示名稱、成績都要填")
            continue
        consent = r.get("家長同意", "").lower() in YES
        sort = int(re.sub(r"\D", "", r.get("排序", "")) or 0)
        conn.execute("""INSERT INTO honor_roll (school_year, category, display_name, school_name, achievement,
                                                consent, is_published, sort_order, source_path)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (int(year), r["類別"], r["顯示名稱"], r.get("學校") or None, r["成績"], consent, consent,
                      sort, f"{HONOR}/{HONOR_FILE}#{line_no}"))
        if consent:
            report.honor_public += 1
            name = r["顯示名稱"]
            if len(name) >= 2 and not re.search(r"[○〇Ｏ*＊xX]", name):
                report.notes.append(f"榜單第 {line_no} 列「{name}」：建議把名字中間改成○（例：王○明）")
        else:
            report.honor_hidden += 1


def _outside(conn, report: Report) -> None:
    for r in conn.execute("""SELECT title FROM kb_articles
                              WHERE source_path IS NULL AND is_active AND audience = 'public' ORDER BY id""").fetchall():
        report.outside.append(f"常見問題：{r['title']}")
    for r in conn.execute("""SELECT title FROM v_active_announcements
                              WHERE source_path IS NULL AND audience IN ('all','guardian') ORDER BY id""").fetchall():
        report.outside.append(f"公告：{r['title']}")
    n = conn.execute("SELECT count(*) AS n FROM honor_roll WHERE source_path IS NULL AND is_published").fetchone()["n"]
    if n:
        report.outside.append(f"榜單：{n} 筆")
        report.honor_public += n


# ====================================================================== 第一次使用：建立資料夾、匯出現有資料
GUIDE = """補習班資料：丟檔案就能更新 LINE 機器人
=====================================================
這個資料夾裡有什麼，家長在 LINE 上就看到什麼。有兩種改法，可以混著用：
  1. 後台（瀏覽器打開 http://localhost:8000/admin）的「公告與行事曆」「常見問題」「榜單」：填表單、
     上傳圖片，按儲存就好。後台其實也是把檔案寫進這個資料夾。
  2. 直接在這個資料夾丟檔案、改檔案、刪檔案，大約 30 秒後機器人就會更新（bot 要開著）。
每次更新的結果寫在「_同步結果.txt」，有問題的檔案也會列在那裡。

■ 行事曆
  把行事曆（照片或圖片）丟進「行事曆」資料夾。每個年級各自用最新丟進來的那一份；
  舊的不用刪（也可以刪）。正反兩面可以一起丟，10 分鐘內丟進來的算同一份。
  想讓 AI 回答「11/15 有上課嗎？」這種問題，要有文字版：放一個同檔名的 .txt，例如
  「11月行事曆.jpg」配「11月行事曆.txt」，在 .txt 裡寫上每天的事項。

■ 只給某個年級看（行事曆、公告）
  三種寫法，擇一即可：
  ・放進年級資料夾：國小、國一、國二、國三、國中（＝國一～國三），例如「公告\\考試\\國二\\段考範圍.png」
  ・檔名寫年級：「國一行事曆.png」就只給國一（放在「國中」資料夾也一樣只給國一）
  ・.txt 第一行寫「年級：國一、國二」
  都沒有＝全部年級。家長問「國二段考範圍」時，只會收到國二和全部年級的；沒有的話機器人會直接說沒有，
  不會拿別的年級的資料回答。沒說年級時，會全部列出並註明是哪個年級的。

■ 公告
  一個檔案就是一則公告：寫成 .txt（檔名＝標題，內容＝公告內文），或直接放圖片，或兩個一起
  （同檔名）。放在「公告」裡＝一般公告；放在「停課」「考試」「活動」「招生」子資料夾就是那一類。
  颱風停課請放「停課」，家長點「課程介紹」時會在最上面看到。
  公告不用了就把檔案刪掉。也可以在 .txt 第一行寫「顯示到：2026/10/21」，過了這天自動下架。

■ 常見問題
  在「常見問題」的分類資料夾裡（聯絡、交通、課程、收費、規定、招生、其他），一個 .txt 就是一題：
  檔名＝問題（也是 LINE 上的按鈕文字，20 字以內），內容＝標準答案。同檔名的圖片（例如交通地圖）
  回答時會一起傳給家長。
  檔名前面加數字可以排順序：「01_聯絡電話與服務時間.txt」會排在第一個，標題不會出現數字。
  .txt 第一行可以寫「關鍵字：電話、聯絡、客服」，沒有 AI 的時候用來比對家長的問題。

■ 榜單
  用 Excel 打開「榜單」資料夾裡的「榜單.csv」編輯（或用後台的「榜單」頁）。「家長同意」要填「是」才會公開。
  名字請寫成「王○明」這種形式。存檔時選「CSV UTF-8（逗號分隔）」。

■ 只丟圖片的時候
  機器人會直接把圖片傳給家長。如果有設定 AI（.env 的 ANTHROPIC_API_KEY），系統會自動在旁邊產生
  一個同名的 .txt「AI 草稿」，把圖片轉成文字。請對照圖片檢查，把【AI 草稿】開頭的那兩行刪掉再存檔，
  機器人才會開始用這段文字回答（例如回答某一天有沒有上課）。沒刪之前不會用，避免 AI 看錯字。

■ 注意
  ・要下架一則資料，刪掉那個檔案就好。不要刪整個「常見問題」「公告」這些資料夾：資料夾不見時，
    系統會把機器人現有的資料重新寫回來（避免雲端硬碟還沒同步好時，機器人的資料被清空）。
  ・文字檔請用記事本存成 .txt；Word、PDF、Excel 檔不能直接用（「_同步結果.txt」會提醒你）。
  ・iPhone 的照片如果是 HEIC 格式，請改成 JPG，或截圖後再放進來。
  ・學生資料、班級名單、家長的 LINE 綁定不在這裡改，請用後台。
  ・這份說明是系統產生的，改版時會自動更新。
"""


def ensure_structure(root: Path, report: Report) -> None:
    """建立資料夾。某一區的資料夾不見了（第一次使用、或整個被刪掉／搬走）時，把資料庫裡的內容匯出成檔案，
    而不是把機器人的資料清空：要下架請刪掉裡面的檔案，不要刪整個資料夾。"""
    root.mkdir(parents=True, exist_ok=True)
    guide = root / GUIDE_FILE
    if not guide.exists() or read_text(guide).replace("\r\n", "\n").strip() != GUIDE.strip():
        write_text(guide, GUIDE)                               # 改版時說明也跟著更新
    if not (root / CALENDAR).exists():
        for sub in ("國小", "國中"):
            (root / CALENDAR / sub).mkdir(parents=True, exist_ok=True)
        _export_notices(root, report, calendar=True)
    if not (root / NOTICES).exists():
        for sub in ("停課", "考試", "活動", "招生"):
            (root / NOTICES / sub).mkdir(parents=True, exist_ok=True)
        _export_notices(root, report)
    if not (root / KB).exists():
        for sub in KB_SUBS:
            (root / KB / sub).mkdir(parents=True, exist_ok=True)
        _export_kb(root, report)
    if not (root / HONOR).exists():
        (root / HONOR).mkdir(parents=True, exist_ok=True)
        _export_honor(root, report)


_BAD_CHARS = str.maketrans({"\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞",
                            "|": "｜", "\n": " ", "\r": " ", "\t": " "})


def safe_name(title: str) -> str:
    """標題 → Windows 可以用的檔名。"""
    return title.translate(_BAD_CHARS).strip().rstrip(".") or "未命名"


def _write_item(conn, root: Path, source_path: str, text: str | None, image_id: int | None) -> Path:
    """把一筆資料寫回檔案：文字 → 同名 .txt；附圖 → 同名 .jpg。只有圖片（文字是預設的「請看圖片」）就不寫 .txt。"""
    base = root / source_path
    base.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        write_text(base.with_name(base.name + ".txt"), text)
    if image_id:
        row = conn.execute("SELECT data FROM media_files WHERE id = %s", (image_id,)).fetchone()
        if row:
            base.with_name(base.name + ".jpg").write_bytes(bytes(row["data"]))
    return base


def _export_kb(root: Path, report: Report) -> None:
    with db.get_pool().connection() as conn, conn.transaction():
        rows = conn.execute("""SELECT id, category, title, content, keywords, image_id, source_path FROM kb_articles
                                WHERE is_active AND (source_path IS NULL OR source_path LIKE %s)
                                ORDER BY category, sort_order, id""", (KB + "/%",)).fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            sub = KB_SUB_NAME.get(r["category"], "其他")
            counter[sub] = counter.get(sub, 0) + 1
            source = r["source_path"] or f"{KB}/{sub}/{counter[sub]:02d}_{safe_name(r['title'])}"
            head = f"關鍵字：{'、'.join(r['keywords'])}\n\n" if r["keywords"] else ""
            body = None if r["content"] == PLACEHOLDER[KB] and r["image_id"] else head + r["content"].strip() + "\n"
            _write_item(conn, root, source, body, r["image_id"])
            conn.execute("UPDATE kb_articles SET source_path = %s WHERE id = %s", (source, r["id"]))
            report.exported.append(source)


def _export_notices(root: Path, report: Report, calendar: bool = False) -> None:
    with db.get_pool().connection() as conn, conn.transaction():
        if calendar:
            rows = conn.execute("""SELECT id, title, body, category, ends_at, image_id, source_path FROM announcements
                                    WHERE source_path LIKE %s ORDER BY id""", (CALENDAR + "/%",)).fetchall()
        else:
            rows = conn.execute("""SELECT id, title, body, category, ends_at, image_id, source_path FROM announcements
                                    WHERE source_path LIKE %s
                                       OR (source_path IS NULL AND (ends_at IS NULL OR ends_at > now())
                                           AND category <> 'calendar'
                                           AND title NOT LIKE '【示範】%%')   -- demo/run_demo.py 的示範公告
                                    ORDER BY id""", (NOTICES + "/%",)).fetchall()
        for r in rows:
            source = r["source_path"]
            if not source:
                sub = NOTICE_SUB_NAME.get(r["category"], "一般")
                source = f"{NOTICES}/{safe_name(r['title'])}" if sub == "一般" else f"{NOTICES}/{sub}/{safe_name(r['title'])}"
            head = ""
            if r["ends_at"]:
                last_day = (r["ends_at"].astimezone(TW) - timedelta(seconds=1)).date()
                head = f"顯示到：{last_day:%Y/%m/%d}\n\n"
            placeholder = r["body"] in (PLACEHOLDER[NOTICES], PLACEHOLDER[CALENDAR]) and r["image_id"]
            body = head + r["body"].strip() + "\n" if not placeholder else (head or None)
            _write_item(conn, root, source, body, r["image_id"])
            conn.execute("UPDATE announcements SET source_path = %s WHERE id = %s", (source, r["id"]))
            report.exported.append(source)


def _export_honor(root: Path, report: Report) -> None:
    with db.get_pool().connection() as conn, conn.transaction():
        rows = conn.execute("""SELECT id, school_year, category, display_name, school_name, achievement, consent,
                                      sort_order
                                 FROM honor_roll WHERE source_path IS NULL OR source_path LIKE %s
                                ORDER BY school_year DESC, sort_order, id""", (HONOR + "/%",)).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(HONOR_HEADERS)
        for line_no, r in enumerate(rows, start=2):
            w.writerow([r["school_year"], r["category"], r["display_name"], r["school_name"] or "", r["achievement"],
                        "是" if r["consent"] else "否", r["sort_order"]])
            conn.execute("UPDATE honor_roll SET source_path = %s WHERE id = %s",
                         (f"{HONOR}/{HONOR_FILE}#{line_no}", r["id"]))
        write_text(root / HONOR / HONOR_FILE, buf.getvalue())
        if rows:
            report.exported.append(f"{HONOR}/{HONOR_FILE}（{len(rows)} 筆）")


# ====================================================================== 背景自動同步
class Poller:
    """bot 執行時，每隔幾秒檢查資料夾；有變動才同步。"""

    def __init__(self, interval: int):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Poller":
        self._thread = threading.Thread(target=self._run, name="content-sync", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        first = True
        while not self._stop.wait(0 if first else self.interval):
            try:
                report = sync(force=first)
                if report:
                    log.info("「補習班資料」同步完成：行事曆 %d、公告 %d、常見問題 %d、問題檔案 %d",
                             len(report.calendar), len(report.notices), len(report.kb), len(report.problems))
            except Exception:
                log.exception("資料夾同步失敗")
            first = False
            if not self.interval:
                break

    def stop(self) -> None:
        self._stop.set()
