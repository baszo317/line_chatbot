"""bot 的判斷邏輯（關鍵字模式，不需要 AI）。"""
from app import db
from app.bot import Bot
from conftest import USER, make_event


def test_follow_sends_welcome_and_saves_profile(line):
    Bot(line).handle_event(make_event("follow"))
    assert "歡迎加入高明補習班" in line.texts()[0]
    assert "課程介紹" in line.quick_replies()
    user = db.fetch_one("SELECT display_name, is_following FROM line_users WHERE line_user_id = %s", (USER,))
    assert user == {"display_name": "測試家長", "is_following": True}


def test_course_button_lists_timetable(line):
    Bot(line).handle_event(make_event(text="課程介紹"))
    text = line.texts()[0]
    assert "國二數學 A 班" in text and "週一、週三 18:30–20:30" in text and "4,800 元／期" in text


def test_course_reply_shows_closure_notice(line):
    db.execute("""INSERT INTO announcements (title, body, category, starts_at, ends_at)
                  VALUES ('明晚颱風停課', '明晚停課', 'closure', now() - interval '1 hour', now() + interval '1 day')""")
    Bot(line).handle_event(make_event(text="課程介紹"))
    assert "⚠️ 公告：明晚颱風停課" in line.texts()[0]


def test_redelivered_event_is_answered_once(line):
    bot = Bot(line)
    event = make_event(text="榜單", event_id="01DUPLICATE")
    bot.handle_event(event)
    bot.handle_event(event)  # LINE 重送同一個事件
    assert len(line.replies) == 1
    assert "114 學年度榜單" in line.texts()[0]


def test_faq_button_answers_that_article(line):
    bot = Bot(line)
    bot.handle_event(make_event(text="常見問題"))
    assert "請假與補課規定" in line.quick_replies()
    bot.handle_event(make_event(text="請假與補課規定"))
    assert line.texts()[1].startswith("【請假與補課規定】")


def test_keyword_answer_without_ai(line):
    Bot(line).handle_event(make_event(text="補習班地址在哪裡？"))
    assert "【地址與交通方式】" in line.texts()[0]
    out = db.fetch_one("SELECT handled_by, intent, tool_calls FROM message_logs WHERE direction = 'out'")
    assert out["handled_by"] == "rule" and out["intent"] == "traffic"
    assert out["tool_calls"]["fallback"] == "keyword"


def test_unknown_question_offers_human(line):
    Bot(line).handle_event(make_event(text="可以帶寵物來上課嗎"))
    assert "目前沒有資料" in line.texts()[0]
    assert "找真人客服" in line.quick_replies()
    row = db.fetch_one("SELECT needs_human FROM message_logs WHERE direction = 'in'")
    assert row["needs_human"] is True


def test_personal_question_explains_binding(line):
    Bot(line).handle_event(make_event(text="我想看小孩的成績"))
    assert "家長專屬功能" in line.texts()[0]


def test_handoff_pauses_ai_until_resume(line):
    bot = Bot(line)
    bot.handle_event(make_event(text="找真人客服", token="t1"))
    assert "已轉給櫃台老師" in line.texts()[0]
    bot.handle_event(make_event(text="刷卡有手續費嗎", token="t2"))      # 暫停中：不回
    row = db.fetch_one("SELECT handled_by, needs_human FROM message_logs WHERE direction = 'in' ORDER BY id DESC LIMIT 1")
    assert row == {"handled_by": "staff", "needs_human": True}
    bot.handle_event(make_event(text="課程介紹", token="t3"))            # 按鈕照常回
    bot.handle_event(make_event(text="回到AI助理", token="t4"))
    bot.handle_event(make_event(text="補習班地址在哪裡？", token="t5"))   # 恢復自動回覆
    assert [token for token, _ in line.replies] == ["t1", "t3", "t4", "t5"]


def test_sticker_gets_text_only_notice(line):
    sticker = {"type": "sticker", "id": "1", "packageId": "1", "stickerId": "1"}
    Bot(line).handle_event(make_event(message=sticker))
    assert "只看得懂文字" in line.texts()[0]


def test_unfollow_marks_user(line):
    bot = Bot(line)
    bot.handle_event(make_event("follow"))
    bot.handle_event(make_event("unfollow"))
    assert db.fetch_one("SELECT is_following FROM line_users")["is_following"] is False
    assert len(line.replies) == 1  # 封鎖事件沒有 reply token，不回覆


def test_group_chat_and_standby_events_are_ignored(line):
    bot = Bot(line)
    bot.handle_event(make_event(text="課程介紹", source_type="group"))
    bot.handle_event(make_event(text="課程介紹", mode="standby"))
    assert line.replies == []


def test_offline_finds_the_class_by_grade_and_subject(line):
    Bot(line).handle_event(make_event(text="國二數學一週上幾次？學費多少？"))
    text = line.texts()[0]
    assert "國二數學 A 班" in text and "一週 2 次" in text and "4,800 元／期" in text
    assert "國二英文" not in text


def test_offline_shows_active_closure_notice(line):
    db.execute("""INSERT INTO announcements (title, body, category, starts_at, ends_at)
                  VALUES ('明晚颱風停課', '明天晚上所有課程停課', 'closure', now() - interval '1 hour', now() + interval '1 day')""")
    Bot(line).handle_event(make_event(text="明天颱風會停課嗎？"))
    assert "明天晚上所有課程停課" in line.texts()[0]


def test_offline_without_notice_uses_policy_article(line):
    Bot(line).handle_event(make_event(text="颱風天會停課嗎？"))
    assert "【颱風與停課規定】" in line.texts()[0]


def test_grade_only_question_lists_that_grade(line):
    bot = Bot(line)
    for q in ("國二現在有甚麼課", "八年級有什麼課", "國二"):
        intent, message, _ = bot._offline_answer(q)
        assert intent == "course", q
        assert "國二數學 A 班" in message["text"] and "國二英文 A 班" in message["text"], q
        assert "國一數學班" not in message["text"], q
    intent, message, _ = bot._offline_answer("八年級數學幾點")
    assert "國二數學 A 班" in message["text"] and "國二英文" not in message["text"]


# ------------------------------------------------------------------ 關鍵字比對：答不出來就說沒有，不拿別的資料充數
def test_unanswerable_questions_do_not_jump_to_other_answers(line):
    bot = Bot(line)
    for q in ("國二有獎學金嗎", "數學老師是誰", "國二有考試嗎"):
        intent, message, needs_human = bot._offline_answer(q)
        assert needs_human and "📚" not in message["text"], q        # 不會變成「國二有什麼課」
    intent, message, needs_human = bot._offline_answer("國二理化")
    assert intent == "course" and needs_human
    assert message["text"].startswith("目前沒有國二理化的班") and "國二數學 A 班、國二英文 A 班" in message["text"]
    intent, message, _ = bot._offline_answer("高一數學")
    assert message["text"].startswith("目前沒有高中的班")
    assert bot._offline_answer("你好")[0] == "greeting" and not bot._offline_answer("你好")[2]
    assert bot._offline_answer("好的謝謝")[1]["text"].startswith("不客氣")
    assert bot._offline_answer("國二數學呢")[1]["text"].startswith("📚 國二數學 A 班")   # 只有年級＋科目也算在問課程
    assert bot._offline_answer("國二明天停課嗎")[1]["text"].startswith("【颱風與停課規定】")


# ------------------------------------------------------------------ 設定、需要真人時用 LINE 通知員工
def set_setting(key: str, value) -> None:
    from psycopg.types.json import Jsonb
    from app import bot_settings
    db.execute("""INSERT INTO bot_settings (key, value) VALUES (%s, %s)
                  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, Jsonb(value)))
    bot_settings.invalidate()


STAFF_LINE = "U" + "5f" * 16


def add_staff(line, *, notify: bool = True) -> None:
    """員工先用自己的 LINE 傳一句話給官方帳號，再到後台設成收通知的員工。"""
    Bot(line).handle_event(make_event(text="你好", user=STAFF_LINE))
    db.execute("INSERT INTO staff (name, role, line_user_id, notify_human) VALUES ('小美', 'counter', %s, %s)",
               (STAFF_LINE, notify))
    line.replies.clear()


def test_staff_get_line_alert_when_parent_needs_human(line):
    add_staff(line)
    bot = Bot(line)
    bot.handle_event(make_event(text="老師什麼時候有空"))                 # 機器人答不出來
    assert line.texts()[-1].startswith("這個問題我目前沒有資料") and len(line.pushes) == 1
    to, msgs = line.pushes[0]
    assert to == STAFF_LINE
    text = msgs[0]["text"]
    assert text.startswith("【需要真人】LINE「測試家長」（還沒綁定） 問了機器人答不出來的問題\n")
    assert "老師什麼時候有空" in text and text.endswith("請到 LINE 官方帳號的聊天室回覆家長。")

    bot.handle_event(make_event(text="找真人客服"))                       # 之前是「答不出來」，按了找真人 → 再通知一次
    assert len(line.pushes) == 2 and "按了「找真人客服」" in line.pushed_texts()[-1]
    assert "老師什麼時候有空" in line.pushed_texts()[-1]                   # 附上前面的訊息
    bot.handle_event(make_event(text="我想請假"))                         # 暫停中又傳：30 分鐘內不再通知
    bot.handle_event(make_event(text="找真人客服"))
    assert len(line.pushes) == 2
    rows = db.fetch_all("SELECT reason, sent_to, error, message_id IS NOT NULL AS linked FROM staff_alerts ORDER BY id")
    assert rows == [{"reason": "unanswered", "sent_to": 1, "error": None, "linked": True},
                    {"reason": "handoff", "sent_to": 1, "error": None, "linked": True}]


def test_alert_settings_and_who_gets_them(line):
    from app.line_api import LineError
    add_staff(line, notify=False)                                        # 沒勾「收通知」
    bot = Bot(line)
    bot.handle_event(make_event(text="老師什麼時候有空"))
    assert line.pushes == [] and db.fetch_one("SELECT count(*) AS n FROM staff_alerts")["n"] == 0
    db.execute("UPDATE staff SET notify_human = true")
    set_setting("notify_unanswered", False)                              # 只在家長按「找真人客服」時通知
    other = "U" + "6e" * 16
    bot.handle_event(make_event(text="老師什麼時候有空", user=other))
    assert line.pushes == []
    line.fail_with = LineError(429, "這個月的訊息額度用完了")               # 推播失敗：家長照樣收到回覆，記下原因
    bot.handle_event(make_event(text="找真人客服", user=other))
    assert line.texts()[-1].startswith("好的，已轉給櫃台老師")
    row = db.fetch_one("SELECT sent_to, error FROM staff_alerts")
    assert row["sent_to"] == 0 and "額度用完" in row["error"]


def test_alert_names_the_bound_parent_and_children(line):
    add_staff(line)
    fam = db.fetch_one("INSERT INTO families (family_name) VALUES ('測試王家') RETURNING id")["id"]
    try:
        db.execute("INSERT INTO line_users (line_user_id) VALUES (%s) ON CONFLICT DO NOTHING", (USER,))
        db.execute("INSERT INTO guardians (family_id, name, line_user_id) VALUES (%s, '王媽媽', %s)", (fam, USER))
        db.execute("""INSERT INTO students (student_no, family_id, name, grade, grade_school_year)
                      VALUES ('T-ALERT-1', %s, '王小明', 8, school_year_of(tw_today()))""", (fam,))
        Bot(line).handle_event(make_event(text="找真人客服"))
        assert line.pushed_texts()[0].startswith("【需要真人】王媽媽（王小明 國二） 按了「找真人客服」")
    finally:
        for sql in ("DELETE FROM students WHERE family_id = %s", "DELETE FROM guardians WHERE family_id = %s",
                    "DELETE FROM families WHERE id = %s"):
            db.execute(sql, (fam,))


def test_welcome_can_be_left_to_line_oa(line):
    set_setting("welcome_on_follow", False)
    Bot(line).handle_event(make_event("follow"))
    assert line.replies == []
    assert db.fetch_one("SELECT is_following FROM line_users")["is_following"] is True


def test_unknown_profile_is_fetched_once(line):
    bot = Bot(line)
    bot.handle_event(make_event(text="榜單"))
    bot.handle_event(make_event(text="榜單"))
    assert db.fetch_one("SELECT display_name FROM line_users")["display_name"] == "測試家長"
