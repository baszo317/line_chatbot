"""事件處理：決定每一則訊息要怎麼回。

判斷順序（越前面越優先）：
  1. 「找真人客服」／「回到AI助理」
  2. 圖文選單按鈕（課程介紹、行事曆、榜單、交通資訊……）、「家長綁定」「孩子的課表」→ 固定回覆
  3. 點了「常見問題」裡的某一題（標題完全相同）→ 直接回答案
  4. 綁定碼（「綁定 AB12CD34」）→ 把這個 LINE 綁定到後台的家長資料
  5. AI 暫停中（櫃台接手）→ 不回，交給真人
  6. 已綁定的家長問孩子的資料 → 從資料庫直接回
  7. 其他自由提問 → Claude；Claude 不能用時 → 關鍵字比對（_offline_decide）
用到的常見問題或公告有附圖時，文字後面會接著傳圖片（最多 4 張；LINE 一次最多 5 則訊息）。
需要真人時（按「找真人客服」、答不出來……）回覆家長之後，用 LINE 通知員工（staff_alerts.py）。

decide() 只決定「要回什麼」，不寫資料庫（綁定碼例外）、不呼叫 LINE，後台的「機器人測試」也是用它。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import ai, bot_settings, db, knowledge, media, replies, staff_alerts
from .config import settings
from .line_api import MAX_MESSAGES, LineApi, text_message

log = logging.getLogger(__name__)

HANDOFF_WORDS = {"找真人客服", "真人客服", "找真人", "轉真人", "我要找真人"}
RESUME_WORDS = {"回到ai助理", "回到ai", "回到ai小助理", "結束真人客服"}
BINDING_WORDS = {"家長綁定", "綁定帳號", "綁定", "我要綁定"}
CHILD_WORDS = {"孩子的課表", "孩子課表", "小孩的課表", "我的孩子"}
# 「綁定 AB12CD34」「綁定碼：ab12cd34」，或只傳 8 碼
BINDING_CODE = re.compile(r"^(?:綁定碼?|binding)?\s*[:：]?\s*([0-9a-f]{8})$", re.IGNORECASE)


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def binding_code(text: str) -> str | None:
    m = BINDING_CODE.match(knowledge.normalize_question(text).strip())
    return m.group(1).upper() if m else None


@dataclass
class Decision:
    messages: list[dict]                 # 要回的文字訊息（空的＝不回）
    intent: str
    handled_by: str                      # rule 固定回覆／ai／staff（不回，交給櫃台）
    needs_human: bool = False
    images: list[str] = field(default_factory=list)   # 要接著傳的圖片（media_files.token）
    action: str | None = None            # pause 暫停 AI／resume 恢復 AI
    model: str | None = None
    latency_ms: int | None = None
    detail: dict | None = None


def with_images(messages: list[dict], images: list[dict]) -> list[dict]:
    """文字後面接圖片。LINE 的快速回覆按鈕只會出現在最後一則，所以把按鈕搬到最後一張圖上。"""
    if not images:
        return messages
    out = [dict(m) for m in messages]
    quick = out[-1].pop("quickReply", None) if out else None
    out += images[: MAX_MESSAGES - len(out)]
    if quick:
        out[-1] = {**out[-1], "quickReply": quick}
    return out


def is_https(base: str | None) -> bool:
    return bool(base) and base.startswith("https://")


class Bot:
    def __init__(self, line: LineApi | None = None):
        self.line = line or LineApi()
        calendar = ("announcement", lambda: replies.calendar(knowledge.notices()))
        self.commands = {
            "課程介紹": ("course", lambda: replies.courses(knowledge.courses(), knowledge.notices())),
            "課表": ("course", lambda: replies.courses(knowledge.courses(), knowledge.notices())),
            "行事曆": calendar,
            "本月行事曆": calendar,
            "榜單": ("honor_roll", lambda: replies.honor(knowledge.honors())),
            "交通資訊": ("traffic", lambda: replies.traffic(knowledge.articles())),
            "交通": ("traffic", lambda: replies.traffic(knowledge.articles())),
            "聯絡我們": ("contact", lambda: replies.contact(knowledge.articles())),
            "常見問題": ("faq", lambda: replies.faq_menu(knowledge.articles())),
        }
        # 按鈕回覆要附的圖片（例如交通地圖、本月行事曆）
        self.command_images = {
            "行事曆": knowledge.calendar_images,
            "本月行事曆": knowledge.calendar_images,
            "交通資訊": lambda: knowledge.category_images("traffic"),
            "交通": lambda: knowledge.category_images("traffic"),
            "聯絡我們": lambda: knowledge.category_images("contact"),
        }

    # ------------------------------------------------------------ 入口
    def handle_event(self, event: dict, public_base: str | None = None) -> None:
        """public_base：bot 對外的 https 網址，傳圖片給 LINE 時用（由 main.py 從 LINE 的請求判斷）。"""
        if event.get("mode", "active") != "active":
            return
        source = event.get("source") or {}
        user_id = source.get("userId")
        if source.get("type") != "user" or not user_id:
            return  # 第一期只處理一對一聊天

        etype = event.get("type")
        message = event.get("message") or {}
        mtype = message.get("type", "unknown") if etype == "message" else etype
        text = message.get("text") if mtype == "text" else None

        profile: dict = {}
        if etype == "follow":
            profile = self.line.get_profile(user_id) or {}
            db.record_follow(user_id, profile.get("displayName"), profile.get("pictureUrl"))
        elif etype != "unfollow" and db.ensure_line_user(user_id):
            # bot 上線前就加好友的家長：補查一次 LINE 名稱（後台綁定家長時看得到是誰）
            p = self.line.get_profile(user_id) or {}
            if p.get("displayName"):
                db.save_profile(user_id, p["displayName"], p.get("pictureUrl"))

        log_id = db.log_incoming(event.get("webhookEventId"), user_id, mtype, text)
        if log_id is None:
            log.info("LINE 重送的事件，已處理過，略過：%s", event.get("webhookEventId"))
            return

        if etype == "unfollow":
            db.record_unfollow(user_id)
            return
        reply_token = event.get("replyToken")
        if not reply_token:
            return

        base = public_base or settings.public_base_url
        if etype == "follow":
            if not bot_settings.get("welcome_on_follow"):   # 官方帳號後台的歡迎訊息已經開著
                db.mark_incoming(log_id, intent="greeting", handled_by="rule")
                return
            self._send(user_id, reply_token, Decision([replies.welcome(profile.get("displayName"))],
                                                      "greeting", "rule"), log_id, base)
        elif mtype == "text":
            self._handle_text(user_id, reply_token, text or "", log_id, base)
        elif etype == "message":
            self._send(user_id, reply_token, Decision([replies.non_text()], "other", "rule"), log_id, base)

    # ------------------------------------------------------------ 文字訊息
    def _handle_text(self, user_id: str, reply_token: str, text: str, log_id: int, base: str | None) -> None:
        d = self.decide(text, user_id=user_id, log_id=log_id)
        if d.action == "resume":
            db.resume_bot(user_id)
        elif d.action == "pause":
            db.pause_bot(user_id, settings.human_pause_minutes)
        if d.messages:
            self._send(user_id, reply_token, d, log_id, base)
        else:                # AI 暫停中：交給櫃台
            db.mark_incoming(log_id, intent=d.intent, handled_by=d.handled_by, needs_human=d.needs_human)
        if d.needs_human:    # 先回覆家長（reply token 很快就過期），再通知員工
            staff_alerts.notify(self.line, user_id, log_id, self._why(d))

    @staticmethod
    def _why(d: Decision) -> str:
        if d.action == "pause":
            return "handoff"
        if d.handled_by == "staff":
            return "waiting"
        return "binding" if d.intent == "binding" else "unanswered"

    def decide(self, text: str, *, user_id: str | None = None, log_id: int | None = None) -> Decision:
        """決定要回什麼。user_id 為 None 時是後台的「機器人測試」：不看暫停狀態、不顯示載入動畫、
        沒有對話紀錄，也不會真的綁定。"""
        key = _key(text)

        if key in RESUME_WORDS:
            return Decision([replies.resume()], "handoff", "rule", action="resume")
        if key in HANDOFF_WORDS:
            msg = replies.handoff(settings.human_pause_minutes, knowledge.contact_text())
            return Decision([msg], "handoff", "rule", needs_human=True, action="pause")

        # 圖文選單按鈕：暫停中也照常回答，因為是家長明確按下的按鈕
        if key in self.commands:
            intent, build = self.commands[key]
            images = self.command_images.get(key, list)()
            return Decision([build()], intent, "rule", images=images)
        guardian = db.guardian_for_line(user_id) if user_id else None
        if key in BINDING_WORDS:
            children = db.family_children(guardian["family_id"]) if guardian else []
            return Decision([replies.binding_help(guardian, children)], "binding", "rule")
        if key in CHILD_WORDS:
            return self._child_timetable(guardian)

        article = knowledge.article_by_title(text.strip())
        if article:
            return Decision([replies.kb_answer(article)], article["category"], "rule",
                            images=knowledge.article_images(article))

        code = binding_code(text)
        if code:
            return self._redeem(code, user_id)

        history: list[dict] = []
        if user_id is not None:
            user = db.get_user(user_id)
            if user and user["paused"]:
                return Decision([], "handoff", "staff", needs_human=True)

        if guardian:   # 已綁定的家長問孩子的資料：資料庫直接回，不經過 AI
            q = knowledge.normalize_question(text)
            if knowledge.CHILD.search(q) and knowledge.CHILD_CLASSES.search(q):
                return self._child_timetable(guardian)
            if knowledge.PERSONAL.search(q):
                return Decision([replies.personal_not_ready()], "personal", "rule")

        # 自由提問：先問 Claude
        if ai.enabled():
            if user_id is not None:
                self.line.show_loading(user_id, 20)
                history = db.recent_dialogue(user_id, before_id=log_id) if log_id else []
            note = (f"這位家長已經綁定帳號（{guardian['guardian_name']}）；孩子的成績、到班、請假、餐費查詢還沒開放，"
                    "問到時請他點「找真人客服」，問孩子的課表請他點「孩子的課表」。") if guardian else None
            result = ai.answer(text, history, note=note)
            if result:
                return Decision(
                    [replies.ai_reply(result.reply, result.needs_human)], result.intent, "ai",
                    needs_human=result.needs_human, images=knowledge.images_for_titles(result.used_titles),
                    model=result.model, latency_ms=result.latency_ms,
                    detail={"used_titles": result.used_titles, "usage": result.usage},
                )

        d = self._offline_decide(text)
        d.detail = {"fallback": "keyword", "ai_mode": settings.ai_mode}
        return d

    # ------------------------------------------------------------ 綁定與孩子的資料
    def _redeem(self, code: str, user_id: str | None) -> Decision:
        if user_id is None:   # 後台的機器人測試：不真的綁定
            return Decision([text_message(f"（測試）家長在 LINE 輸入這個綁定碼，就會完成綁定：{code}")],
                            "binding", "rule")
        outcome = db.redeem_binding_code(code, user_id)
        guardian = db.guardian_for_line(user_id) if outcome == "guardian" else None
        children = db.family_children(guardian["family_id"]) if guardian else []
        log.info("綁定碼 %s：%s", code, outcome)
        return Decision([replies.binding_result(outcome, guardian, children)], "binding", "rule",
                        needs_human=outcome not in ("guardian", "staff", "student"),
                        detail={"binding": outcome})

    def _child_timetable(self, guardian: dict | None) -> Decision:
        if not guardian:
            return Decision([replies.binding_not_ready()], "personal", "rule")
        return Decision([replies.child_timetable(db.family_children(guardian["family_id"]))], "personal", "rule")

    # ------------------------------------------------------------ 沒有 AI 時：關鍵字比對
    def _offline_answer(self, text: str) -> tuple[str, dict, bool]:
        """沒有 AI 時的關鍵字比對（只回傳文字部分；測試和網頁版展示用）。"""
        d = self._offline_decide(text)
        return d.intent, d.messages[0], d.needs_human

    def _offline_decide(self, text: str) -> Decision:
        """沒有 AI 時的關鍵字比對。

        原則：先認出家長在問「哪一類」（停課、行事曆、考試公告、課程……），就只從那一類的資料找答案；
        那一類沒有資料、或只有別的年級的，就直接說「目前沒有」並建議找真人，
        不會因為問題裡剛好有「國二」兩個字，就拿國二的課程來回答考試的問題。
        """
        k = knowledge

        def rule(intent: str, message: dict, images: list[str] | None = None, needs_human: bool = False):
            return Decision([message], intent, "rule", needs_human=needs_human, images=images or [])

        q = k.normalize_question(text).strip()
        grades = k.grade_set(q)
        label = k.grades_text(grades)
        subjects = k.subject_set(q)
        calendars = k.calendar_notices()
        my_calendars = k.for_grades(calendars, grades)

        # 1. 打招呼、道謝
        if k.GREETING.match(q):
            return rule("greeting", replies.greeting())
        if k.THANKS.match(q):
            return rule("greeting", replies.thanks())
        # 2. 孩子的個人資料（成績、請假、到班……；「我家小孩上什麼課」也算）
        if k.PERSONAL.search(q) or (k.CHILD.search(q) and k.CHILD_CLASSES.search(q) and not grades and not subjects):
            return rule("personal", replies.binding_not_ready())
        # 3. 停課
        if k.CLOSURE_WORDS.search(q):
            closures = k.closure_notices()
            mine = k.for_grades(closures, grades)
            if mine:
                return rule("announcement", replies.closure(mine), k._tokens(mine))
            if closures:
                return rule("announcement", replies.nothing("停課公告", label, closures), needs_human=True)
            # 沒有停課公告：往下找（「颱風天會停課嗎」→ 常見問題的停課規定）
        # 4. 行事曆
        if k.CALENDAR_WORDS.search(q):
            if my_calendars:
                return rule("announcement", replies.calendar(my_calendars), k._tokens(my_calendars))
            if calendars:
                return rule("announcement", replies.nothing("行事曆", label, calendars), needs_human=True)
            return rule("announcement", replies.calendar([]), needs_human=True)
        # 5. 榜單
        if k.HONOR.search(q):
            return rule("honor_roll", replies.honor(k.honors()))
        # 6. 寫了完整班名
        named = k.courses_named(q)
        if named:
            return rule("course", replies.course_detail(named))
        # 7. 公告：考試、活動、招生，或標題對得上的
        kinds = k.notice_kinds(q)
        hits = k.search_notices(q, kinds)
        if kinds or hits:
            mine = k.for_grades(hits, grades)
            if mine:
                return rule("announcement", replies.notice_list(mine), k._tokens(mine))
            if k.CALENDAR_TIME.search(q) and my_calendars:      # 「這個月有考試嗎」→ 行事曆
                return rule("announcement", replies.calendar(my_calendars), k._tokens(my_calendars))
            kind = k.KIND_LABEL[next(iter(kinds))] if len(kinds) == 1 else "公告"
            if hits:                                             # 只有別的年級的
                return rule("announcement", replies.nothing(kind, label, hits), needs_human=True)
            strong = [a for score, a in k.search_articles(q) if score >= 3]
            if strong:
                return rule(strong[0]["category"], replies.kb_answer(strong[0]), k.article_images(strong[0]))
            quick = ["找真人客服", "行事曆", "聯絡我們"] if my_calendars else None
            return rule("announcement", replies.nothing(kind, label, quick=quick), needs_human=True)
        # 8. 問某一天、這週、下個月 → 行事曆（有那個年級的行事曆才算）
        if k.CALENDAR_TIME.search(q) and my_calendars:
            return rule("announcement", replies.calendar(my_calendars), k._tokens(my_calendars))
        # 9. 高中
        if k.SENIOR_HIGH.search(q) and not grades:
            return rule("course", replies.no_senior_high(), needs_human=True)
        # 10. 有年級或科目，而且在問課程（「國二數學幾點」「國二」「理化」；「國二有獎學金嗎」不算）
        if (grades or subjects) and not k.POLICY_WORDS.search(q) and (
                k.COURSE_ASK.search(q) or k.is_bare(q, [*k.subjects(), *k.SUBJECT_ALIASES])):
            pool = k.courses_for(grades, subjects)
            if pool:
                return rule("course", replies.course_detail(pool))
            what = label + "、".join(sorted(subjects))
            others = k.courses_for(grades, set()) if subjects and len(grades) == 1 else []
            return rule("course", replies.no_course(what or "這個", others), needs_human=True)
        # 11. 問整份課表
        if k.COURSE_LIST.search(q) and not k.POLICY_WORDS.search(q):
            return rule("course", replies.courses(k.courses(), k.notices()))
        # 12. 常見問題
        found = k.search_articles(q)
        if found:
            article = found[0][1]
            return rule(article["category"], replies.kb_answer(article), k.article_images(article))
        # 13. 交通、聯絡、收費
        if k.TOPICS["traffic"].search(q):
            return rule("traffic", replies.traffic(k.articles()), k.category_images("traffic"))
        if k.TOPICS["contact"].search(q):
            return rule("contact", replies.contact(k.articles()), k.category_images("contact"))
        if k.TOPICS["fee"].search(q):
            return rule("fee", replies.kb_category(k.articles(), "fee", "💰 學費與繳費"), k.category_images("fee"))
        return rule("other", replies.unknown(), needs_human=True)

    # ------------------------------------------------------------ 後台的「機器人測試」
    def preview(self, text: str) -> dict:
        d = self.decide(text)
        messages = with_images(d.messages, [media.line_image(t, "") for t in d.images])
        return {"messages": messages, "intent": d.intent, "handled_by": d.handled_by,
                "needs_human": d.needs_human, "action": d.action, "model": d.model,
                "latency_ms": d.latency_ms, "detail": d.detail}

    # ------------------------------------------------------------ 送出並記錄
    def _send(self, user_id: str, reply_token: str, d: Decision, log_id: int, base: str | None) -> None:
        detail = dict(d.detail or {})
        images: list[dict] = []
        if d.images:
            if is_https(base):
                images = [media.line_image(t, base) for t in d.images]
            else:  # LINE 只接受 https 圖片網址；本機測試時只傳文字
                detail["images_skipped"] = len(d.images)
                log.warning("有 %d 張附圖沒傳：不知道 bot 對外的 https 網址（可在 .env 設定 BOT_PUBLIC_BASE_URL）",
                            len(d.images))
        messages = with_images(d.messages, images)
        try:
            self.line.reply(reply_token, messages)
        except Exception as e:  # reply token 過期、網路問題……
            log.error("回覆失敗：%s", e)
            detail["reply_error"] = str(e)[:200]
        content = "\n\n".join(m["text"] if m.get("type") == "text" else "［圖片］" for m in messages)
        db.log_outgoing(user_id, content, intent=d.intent, handled_by=d.handled_by, needs_human=d.needs_human,
                        model=d.model, latency_ms=d.latency_ms, detail=detail or None, reply_to=log_id)
        db.mark_incoming(log_id, intent=d.intent, handled_by=d.handled_by, needs_human=d.needs_human)
