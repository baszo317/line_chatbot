--
-- PostgreSQL database dump
--

\restrict uDy4cDHU7QvwTyCXo8WvEgN6IhqPawl2Qq3hxwsUdX3FlQW7Q6b4xoHjBTkHngq

-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: apply_wallet_txn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.apply_wallet_txn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  INSERT INTO wallets (student_id) VALUES (NEW.student_id) ON CONFLICT (student_id) DO NOTHING;
  UPDATE wallets SET balance = balance + NEW.amount, updated_at = now()
   WHERE student_id = NEW.student_id;
  RETURN NULL;
END $$;


--
-- Name: cancel_meal_order(bigint, bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.cancel_meal_order(p_order_id bigint, p_staff_id bigint DEFAULT NULL::bigint) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE o meal_orders%ROWTYPE;
BEGIN
  SELECT * INTO o FROM meal_orders WHERE id = p_order_id FOR UPDATE;
  IF NOT FOUND THEN RETURN 'not_found'; END IF;
  IF o.status = 'cancelled' THEN RETURN 'already_cancelled'; END IF;

  UPDATE meal_orders SET status = 'cancelled' WHERE id = p_order_id;

  IF EXISTS (SELECT 1 FROM wallet_transactions
              WHERE meal_order_id = p_order_id AND txn_type = 'meal_charge') THEN
    INSERT INTO wallet_transactions (student_id, txn_type, amount, meal_order_id, created_by, note)
    VALUES (o.student_id, 'meal_refund', o.unit_price, p_order_id, p_staff_id, '取消訂餐退回')
    ON CONFLICT DO NOTHING;
    RETURN 'cancelled_and_refunded';
  END IF;
  RETURN 'cancelled';
END $$;


--
-- Name: charge_meals(bigint, bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.charge_meals(p_meal_menu_id bigint, p_staff_id bigint DEFAULT NULL::bigint) RETURNS TABLE(order_id bigint, stu_id bigint, charge integer, outcome text)
    LANGUAGE plpgsql
    AS $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT o.id, o.student_id, o.unit_price
      FROM meal_orders o
     WHERE o.meal_menu_id = p_meal_menu_id
       AND o.status = 'active'
       AND o.meal_option_id IS NOT NULL
       AND o.unit_price > 0
       AND NOT EXISTS (SELECT 1 FROM wallet_transactions t
                        WHERE t.meal_order_id = o.id AND t.txn_type = 'meal_charge')
     ORDER BY o.id
  LOOP
    order_id := r.id;  stu_id := r.student_id;  charge := r.unit_price;
    BEGIN
      INSERT INTO wallet_transactions (student_id, txn_type, amount, meal_order_id, created_by, note)
      VALUES (r.student_id, 'meal_charge', -r.unit_price, r.id, p_staff_id, '訂餐扣款');
      outcome := 'charged';
    EXCEPTION
      WHEN unique_violation THEN outcome := 'already_charged';       -- 別人同時按了扣款
      WHEN check_violation  THEN outcome := 'insufficient_balance';
    END;
    RETURN NEXT;
  END LOOP;
END $$;


--
-- Name: check_score_range(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.check_score_range() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE v_full NUMERIC;
BEGIN
  IF NEW.score IS NOT NULL THEN
    SELECT full_score INTO v_full FROM exams WHERE id = NEW.exam_id;
    IF NEW.score < 0 OR NEW.score > v_full THEN
      RAISE EXCEPTION '分數 % 超出範圍（0 ~ %）', NEW.score, v_full;
    END IF;
  END IF;
  RETURN NEW;
END $$;


--
-- Name: forbid_ledger_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.forbid_ledger_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  RAISE EXCEPTION '% 是只能新增的帳本；要更正請新增一筆 adjust 交易', TG_TABLE_NAME;
END $$;


--
-- Name: generate_sessions(date, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.generate_sessions(p_from date, p_to date) RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE n INTEGER;
BEGIN
  INSERT INTO class_sessions (course_id, session_date, start_time, end_time, room)
  SELECT cs.course_id, d::date, cs.start_time, cs.end_time, cs.room
    FROM course_schedules cs
    JOIN courses c ON c.id = cs.course_id AND c.status = 'open'
   CROSS JOIN generate_series(p_from, p_to, interval '1 day') AS d
   WHERE EXTRACT(ISODOW FROM d) = cs.weekday
     AND d::date >= c.start_date
     AND (c.end_date IS NULL OR d::date <= c.end_date)
  ON CONFLICT (course_id, session_date, start_time) DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;


--
-- Name: grade_label(integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.grade_label(g integer) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
  SELECT CASE
    WHEN g = 6              THEN '國小'
    WHEN g BETWEEN 7 AND 9  THEN '國' || (ARRAY['一','二','三'])[g - 6]
    WHEN g > 9              THEN '已畢業'
  END
$$;


--
-- Name: issue_binding_code(text, bigint, bigint, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.issue_binding_code(p_target text, p_target_id bigint, p_created_by bigint DEFAULT NULL::bigint, p_valid_days integer DEFAULT 14) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE v_code TEXT;
BEGIN
  IF p_target NOT IN ('guardian','student','staff') THEN
    RAISE EXCEPTION 'p_target 必須是 guardian / student / staff';
  END IF;
  LOOP
    v_code := upper(left(replace(gen_random_uuid()::text, '-', ''), 8));
    BEGIN
      INSERT INTO binding_codes (code, guardian_id, student_id, staff_id, expires_at, created_by)
      VALUES (v_code,
              CASE WHEN p_target = 'guardian' THEN p_target_id END,
              CASE WHEN p_target = 'student'  THEN p_target_id END,
              CASE WHEN p_target = 'staff'    THEN p_target_id END,
              now() + make_interval(days => p_valid_days),
              p_created_by);
      RETURN v_code;
    EXCEPTION WHEN unique_violation THEN
      NULL;  -- 極少數撞號，重抽一次
    END;
  END LOOP;
END $$;


--
-- Name: meal_order_price(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.meal_order_price() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND EXISTS (
       SELECT 1 FROM wallet_transactions t
        WHERE t.meal_order_id = OLD.id AND t.txn_type = 'meal_charge') THEN
    RAISE EXCEPTION '訂單 % 已扣款，要改餐請先取消（會自動退款）再重新訂', OLD.id;
  END IF;

  IF NEW.meal_option_id IS NULL THEN
    NEW.unit_price := 0;
  ELSE
    SELECT price INTO NEW.unit_price FROM meal_options WHERE id = NEW.meal_option_id;
  END IF;
  RETURN NEW;
END $$;


--
-- Name: redeem_binding_code(text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.redeem_binding_code(p_code text, p_line_user_id text) RETURNS text
    LANGUAGE plpgsql
    AS $$
DECLARE
  bc     binding_codes%ROWTYPE;
  v_role TEXT;
BEGIN
  SELECT * INTO bc FROM binding_codes WHERE code = upper(trim(p_code)) FOR UPDATE;
  IF NOT FOUND              THEN RETURN 'invalid'; END IF;
  IF bc.used_at IS NOT NULL THEN RETURN 'used';    END IF;
  IF bc.expires_at < now()  THEN RETURN 'expired'; END IF;

  INSERT INTO line_users (line_user_id) VALUES (p_line_user_id) ON CONFLICT DO NOTHING;

  BEGIN
    IF bc.guardian_id IS NOT NULL THEN
      UPDATE guardians SET line_user_id = p_line_user_id WHERE id = bc.guardian_id;
      v_role := 'guardian';
    ELSIF bc.staff_id IS NOT NULL THEN
      UPDATE staff SET line_user_id = p_line_user_id WHERE id = bc.staff_id;
      v_role := 'staff';
    ELSE
      UPDATE students SET line_user_id = p_line_user_id WHERE id = bc.student_id;
      v_role := 'student';
    END IF;
  EXCEPTION WHEN unique_violation THEN
    RETURN 'line_already_bound';
  END;

  UPDATE binding_codes SET used_at = now(), used_by = p_line_user_id WHERE code = bc.code;
  UPDATE line_users SET menu_role = v_role WHERE line_user_id = p_line_user_id;
  RETURN v_role;
END $$;


--
-- Name: school_year_of(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.school_year_of(d date) RETURNS smallint
    LANGUAGE sql IMMUTABLE
    AS $$
  SELECT (EXTRACT(YEAR FROM d)::int - 1911
          - CASE WHEN EXTRACT(MONTH FROM d) >= 8 THEN 0 ELSE 1 END)::smallint
$$;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;


--
-- Name: tw_today(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.tw_today() RETURNS date
    LANGUAGE sql STABLE
    AS $$
  SELECT (now() AT TIME ZONE 'Asia/Taipei')::date
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: admin_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_audit (
    id bigint NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    table_name text NOT NULL,
    row_id text,
    summary text,
    before jsonb,
    after jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE admin_audit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.admin_audit IS '後台異動紀錄：誰在什麼時候改了哪筆資料（改前、改後）';


--
-- Name: COLUMN admin_audit.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.id IS '編號（自動產生）';


--
-- Name: COLUMN admin_audit.actor; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.actor IS '操作人：誰改的（後台登入時填的名字）';


--
-- Name: COLUMN admin_audit.action; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.action IS '動作：insert 新增／update 修改／delete 刪除／upload 上傳／enroll 加入班級／drop 退班';


--
-- Name: COLUMN admin_audit.table_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.table_name IS '資料表';


--
-- Name: COLUMN admin_audit.row_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.row_id IS '資料編號';


--
-- Name: COLUMN admin_audit.summary; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.summary IS '說明：一句話說明，例：把 王小明 加入 國二數學 A 班';


--
-- Name: COLUMN admin_audit.before; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.before IS '修改前';


--
-- Name: COLUMN admin_audit.after; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.after IS '修改後';


--
-- Name: COLUMN admin_audit.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_audit.created_at IS '時間';


--
-- Name: admin_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.admin_audit ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.admin_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: announcements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.announcements (
    id bigint NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    category text DEFAULT 'general'::text NOT NULL,
    audience text DEFAULT 'all'::text NOT NULL,
    course_id bigint,
    grade smallint,
    starts_at timestamp with time zone DEFAULT now() NOT NULL,
    ends_at timestamp with time zone,
    is_pinned boolean DEFAULT false NOT NULL,
    pushed_at timestamp with time zone,
    created_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    image_id bigint,
    source_path text,
    grades smallint[],
    CONSTRAINT announcements_audience_check CHECK ((audience = ANY (ARRAY['all'::text, 'guardian'::text, 'staff'::text, 'course'::text, 'grade'::text]))),
    CONSTRAINT announcements_category_check CHECK ((category = ANY (ARRAY['general'::text, 'closure'::text, 'calendar'::text, 'exam'::text, 'event'::text, 'enrollment'::text]))),
    CONSTRAINT announcements_check CHECK (((audience <> 'course'::text) OR (course_id IS NOT NULL))),
    CONSTRAINT announcements_check1 CHECK (((audience <> 'grade'::text) OR (grade IS NOT NULL))),
    CONSTRAINT announcements_grade_check CHECK (((grade >= 6) AND (grade <= 9))),
    CONSTRAINT announcements_grades_check CHECK ((grades <@ ARRAY[(6)::smallint, (7)::smallint, (8)::smallint, (9)::smallint]))
);


--
-- Name: TABLE announcements; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.announcements IS '公告：停課、行事曆、考試、活動、招生';


--
-- Name: COLUMN announcements.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.id IS '編號（自動產生）';


--
-- Name: COLUMN announcements.title; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.title IS '標題';


--
-- Name: COLUMN announcements.body; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.body IS '內容：有附圖時，這裡放圖片的文字版（AI 靠這段回答）';


--
-- Name: COLUMN announcements.category; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.category IS '分類：general 一般／closure 停課(颱風)／calendar 行事曆／exam 考試／event 活動／enrollment 招生';


--
-- Name: COLUMN announcements.audience; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.audience IS '對象：all 所有人／guardian 家長／staff 教職員／course 某一班／grade 某年級';


--
-- Name: COLUMN announcements.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.course_id IS '指定班級：audience = course 時填';


--
-- Name: COLUMN announcements.grade; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.grade IS '指定年級：audience = grade 時填：6 國小、7~9 國一~國三';


--
-- Name: COLUMN announcements.starts_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.starts_at IS '開始顯示';


--
-- Name: COLUMN announcements.ends_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.ends_at IS '結束顯示';


--
-- Name: COLUMN announcements.is_pinned; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.is_pinned IS '置頂';


--
-- Name: COLUMN announcements.pushed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.pushed_at IS '推播時間：已主動推播的時間（推播會用掉訊息額度）';


--
-- Name: COLUMN announcements.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.created_by IS '建立者';


--
-- Name: COLUMN announcements.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.created_at IS '建立時間';


--
-- Name: COLUMN announcements.image_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.image_id IS '附圖：例：本月行事曆';


--
-- Name: COLUMN announcements.source_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.source_path IS '來源檔案：來自「補習班資料」的哪個檔案（不含副檔名）；空白 = 用其他方式建立';


--
-- Name: COLUMN announcements.grades; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.announcements.grades IS '適用年級：6 國小、7~9 國一~國三，可以多個；空白 = 全部年級（「補習班資料」用年級資料夾設定）';


--
-- Name: announcements_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.announcements ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.announcements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: attendance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attendance (
    session_id bigint NOT NULL,
    student_id bigint NOT NULL,
    status text NOT NULL,
    check_in_at timestamp with time zone,
    check_out_at timestamp with time zone,
    leave_request_id bigint,
    source text DEFAULT 'manual'::text NOT NULL,
    recorded_by bigint,
    CONSTRAINT attendance_source_check CHECK ((source = ANY (ARRAY['manual'::text, 'upad12'::text]))),
    CONSTRAINT attendance_status_check CHECK ((status = ANY (ARRAY['present'::text, 'late'::text, 'absent'::text, 'leave'::text])))
);


--
-- Name: TABLE attendance; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.attendance IS '到班紀錄（可由 UPAD12 出勤同步）';


--
-- Name: COLUMN attendance.session_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.session_id IS '那一堂課';


--
-- Name: COLUMN attendance.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.student_id IS '學生';


--
-- Name: COLUMN attendance.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.status IS '出缺勤：present 到班／late 遲到／absent 缺席／leave 請假';


--
-- Name: COLUMN attendance.check_in_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.check_in_at IS '到班時間';


--
-- Name: COLUMN attendance.check_out_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.check_out_at IS '離班時間';


--
-- Name: COLUMN attendance.leave_request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.leave_request_id IS '對應的請假單';


--
-- Name: COLUMN attendance.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.source IS '資料來源：manual 手動登錄／upad12';


--
-- Name: COLUMN attendance.recorded_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.attendance.recorded_by IS '登錄老師';


--
-- Name: binding_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.binding_codes (
    code text NOT NULL,
    guardian_id bigint,
    student_id bigint,
    staff_id bigint,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    used_by text,
    created_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT binding_codes_check CHECK ((num_nonnulls(guardian_id, student_id, staff_id) = 1))
);


--
-- Name: TABLE binding_codes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.binding_codes IS '一次性綁定碼：補習班發給家長或導師，在 LINE 輸入後完成綁定';


--
-- Name: COLUMN binding_codes.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.code IS '綁定碼：8 碼大寫英數，用 issue_binding_code() 產生';


--
-- Name: COLUMN binding_codes.guardian_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.guardian_id IS '給哪位家長';


--
-- Name: COLUMN binding_codes.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.student_id IS '給哪位學生';


--
-- Name: COLUMN binding_codes.staff_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.staff_id IS '給哪位教職員';


--
-- Name: COLUMN binding_codes.expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.expires_at IS '到期時間';


--
-- Name: COLUMN binding_codes.used_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.used_at IS '使用時間';


--
-- Name: COLUMN binding_codes.used_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.used_by IS '用掉的 LINE 帳號';


--
-- Name: COLUMN binding_codes.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.created_by IS '發碼的人';


--
-- Name: COLUMN binding_codes.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.binding_codes.created_at IS '建立時間';


--
-- Name: bot_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_settings (
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE bot_settings; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.bot_settings IS '機器人設定（後台「設定」頁）：加好友時的歡迎詞、機器人答不出來時要不要通知員工';


--
-- Name: COLUMN bot_settings.key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bot_settings.key IS '設定名稱：welcome_on_follow 加好友時傳歡迎詞／notify_unanswered 機器人答不出來時也通知員工';


--
-- Name: COLUMN bot_settings.value; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bot_settings.value IS '設定值（JSON）';


--
-- Name: COLUMN bot_settings.updated_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bot_settings.updated_by IS '最後修改者';


--
-- Name: COLUMN bot_settings.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bot_settings.updated_at IS '最後修改時間';


--
-- Name: broadcasts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcasts (
    id bigint NOT NULL,
    request_id uuid NOT NULL,
    audience text NOT NULL,
    grade smallint,
    course_id bigint,
    audience_label text NOT NULL,
    body text NOT NULL,
    image_id bigint,
    announcement_id bigint,
    recipients integer,
    status text DEFAULT 'sending'::text NOT NULL,
    error text,
    sent_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sent_at timestamp with time zone,
    CONSTRAINT broadcasts_audience_check CHECK ((audience = ANY (ARRAY['all'::text, 'grade'::text, 'course'::text]))),
    CONSTRAINT broadcasts_grade_check CHECK (((grade >= 6) AND (grade <= 9))),
    CONSTRAINT broadcasts_status_check CHECK ((status = ANY (ARRAY['sending'::text, 'sent'::text, 'partial'::text, 'failed'::text])))
);


--
-- Name: TABLE broadcasts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.broadcasts IS '廣播：後台用 LINE 推播給家長的訊息（全體好友、某個年級或某一班的家長）';


--
-- Name: COLUMN broadcasts.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.id IS '編號（自動產生）';


--
-- Name: COLUMN broadcasts.request_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.request_id IS '傳送代號：後台按「傳送」時產生；同一個請求重送不會傳兩次（也是 LINE 的 X-Line-Retry-Key）';


--
-- Name: COLUMN broadcasts.audience; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.audience IS '對象：all 全體好友／grade 某個年級的家長／course 某一班的家長';


--
-- Name: COLUMN broadcasts.grade; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.grade IS '年級：audience = grade 時填，6 國小、7~9 國一~國三';


--
-- Name: COLUMN broadcasts.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.course_id IS '班級：audience = course 時填';


--
-- Name: COLUMN broadcasts.audience_label; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.audience_label IS '對象名稱：傳送當時的名稱，例：國二家長、國二數學 A 班家長';


--
-- Name: COLUMN broadcasts.body; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.body IS '內容';


--
-- Name: COLUMN broadcasts.image_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.image_id IS '附圖';


--
-- Name: COLUMN broadcasts.announcement_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.announcement_id IS '從哪一則公告帶入';


--
-- Name: COLUMN broadcasts.recipients; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.recipients IS '傳給幾個 LINE：全體好友時是空的（由 LINE 傳給所有好友）';


--
-- Name: COLUMN broadcasts.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.status IS '狀態：sending 傳送中／sent 已傳送／partial 只傳了一部分／failed 失敗';


--
-- Name: COLUMN broadcasts.error; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.error IS '失敗的原因';


--
-- Name: COLUMN broadcasts.sent_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.sent_by IS '誰傳的（後台登入名稱）';


--
-- Name: COLUMN broadcasts.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.created_at IS '建立時間';


--
-- Name: COLUMN broadcasts.sent_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.broadcasts.sent_at IS '傳送時間';


--
-- Name: broadcasts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.broadcasts ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.broadcasts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: class_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.class_sessions (
    id bigint NOT NULL,
    course_id bigint NOT NULL,
    session_date date NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone NOT NULL,
    room text,
    status text DEFAULT 'scheduled'::text NOT NULL,
    note text,
    CONSTRAINT class_sessions_check CHECK ((end_time > start_time)),
    CONSTRAINT class_sessions_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'cancelled'::text, 'makeup'::text])))
);


--
-- Name: TABLE class_sessions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.class_sessions IS '每一堂實際的課（由 generate_sessions 產生；停課、補課在此標記）';


--
-- Name: COLUMN class_sessions.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.id IS '編號（自動產生）';


--
-- Name: COLUMN class_sessions.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.course_id IS '班級';


--
-- Name: COLUMN class_sessions.session_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.session_date IS '上課日期';


--
-- Name: COLUMN class_sessions.start_time; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.start_time IS '上課時間';


--
-- Name: COLUMN class_sessions.end_time; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.end_time IS '下課時間';


--
-- Name: COLUMN class_sessions.room; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.room IS '教室';


--
-- Name: COLUMN class_sessions.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.status IS '狀態：scheduled 正常／cancelled 停課／makeup 補課';


--
-- Name: COLUMN class_sessions.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.class_sessions.note IS '備註：例：颱風停課、調到週六';


--
-- Name: class_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.class_sessions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.class_sessions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: content_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.content_files (
    path text NOT NULL,
    sha256 text NOT NULL,
    bytes integer NOT NULL,
    mtime_ns bigint NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    ai_draft_at timestamp with time zone
);


--
-- Name: TABLE content_files; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.content_files IS '「補習班資料」資料夾裡每個檔案的紀錄：判斷哪個是最新丟進來的、內容有沒有改';


--
-- Name: COLUMN content_files.path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.path IS '檔案路徑：「補習班資料」資料夾裡的相對路徑，例：行事曆/11月行事曆.jpg';


--
-- Name: COLUMN content_files.sha256; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.sha256 IS '檔案指紋：檔案內容的指紋，內容一改就不同';


--
-- Name: COLUMN content_files.bytes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.bytes IS '檔案大小';


--
-- Name: COLUMN content_files.mtime_ns; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.mtime_ns IS '修改時間：檔案修改時間（只用來判斷要不要重算指紋）';


--
-- Name: COLUMN content_files.first_seen_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.first_seen_at IS '第一次看到：這個檔案（或內容改變）的時間；「最新丟進來的行事曆」以這個為準';


--
-- Name: COLUMN content_files.last_seen_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.last_seen_at IS '最後看到';


--
-- Name: COLUMN content_files.ai_draft_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.content_files.ai_draft_at IS 'AI 草稿時間：已經幫這張圖產生過 AI 草稿（不重複呼叫 AI）';


--
-- Name: course_schedules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.course_schedules (
    id bigint NOT NULL,
    course_id bigint NOT NULL,
    weekday smallint NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone NOT NULL,
    room text,
    CONSTRAINT course_schedules_check CHECK ((end_time > start_time)),
    CONSTRAINT course_schedules_weekday_check CHECK (((weekday >= 1) AND (weekday <= 7)))
);


--
-- Name: TABLE course_schedules; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.course_schedules IS '班級每週固定上課時段';


--
-- Name: COLUMN course_schedules.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_schedules.id IS '編號（自動產生）';


--
-- Name: COLUMN course_schedules.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_schedules.course_id IS '班級';


--
-- Name: COLUMN course_schedules.weekday; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_schedules.weekday IS '星期：1=週一 … 7=週日';


--
-- Name: COLUMN course_schedules.start_time; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_schedules.start_time IS '上課時間';


--
-- Name: COLUMN course_schedules.end_time; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_schedules.end_time IS '下課時間';


--
-- Name: COLUMN course_schedules.room; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_schedules.room IS '教室';


--
-- Name: course_schedules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.course_schedules ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.course_schedules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: course_staff; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.course_staff (
    course_id bigint NOT NULL,
    staff_id bigint NOT NULL,
    role text NOT NULL,
    CONSTRAINT course_staff_role_check CHECK ((role = ANY (ARRAY['teacher'::text, 'tutor'::text])))
);


--
-- Name: TABLE course_staff; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.course_staff IS '班級的授課老師與導師';


--
-- Name: COLUMN course_staff.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_staff.course_id IS '班級';


--
-- Name: COLUMN course_staff.staff_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_staff.staff_id IS '教職員';


--
-- Name: COLUMN course_staff.role; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.course_staff.role IS '角色：teacher 授課老師／tutor 導師';


--
-- Name: courses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.courses (
    id bigint NOT NULL,
    name text NOT NULL,
    subject_id smallint NOT NULL,
    grade smallint,
    start_date date NOT NULL,
    end_date date,
    fee integer,
    is_public boolean DEFAULT true NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    description text,
    external_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT courses_check CHECK (((end_date IS NULL) OR (end_date >= start_date))),
    CONSTRAINT courses_fee_check CHECK ((fee >= 0)),
    CONSTRAINT courses_grade_check CHECK (((grade >= 6) AND (grade <= 9))),
    CONSTRAINT courses_status_check CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text, 'archived'::text])))
);


--
-- Name: TABLE courses; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.courses IS '課程／班級，例：國二數學 A 班';


--
-- Name: COLUMN courses.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.id IS '編號（自動產生）';


--
-- Name: COLUMN courses.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.name IS '班級名稱：例：國二數學 A 班';


--
-- Name: COLUMN courses.subject_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.subject_id IS '科目';


--
-- Name: COLUMN courses.grade; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.grade IS '年級：6 國小、7~9 國一~國三；跨年級可留空';


--
-- Name: COLUMN courses.start_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.start_date IS '開課日';


--
-- Name: COLUMN courses.end_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.end_date IS '結束日';


--
-- Name: COLUMN courses.fee; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.fee IS '學費：對外公開的學費參考（實際帳單在 tuition_bills）';


--
-- Name: COLUMN courses.is_public; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.is_public IS '對外公開：未綁定的訪客也查得到（招生用）';


--
-- Name: COLUMN courses.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.status IS '狀態：open 開放中／closed 已結束／archived 封存';


--
-- Name: COLUMN courses.description; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.description IS '班級說明';


--
-- Name: COLUMN courses.external_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.external_ref IS 'UPAD12 對應代號';


--
-- Name: COLUMN courses.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.created_at IS '建立時間';


--
-- Name: COLUMN courses.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.courses.updated_at IS '最後修改時間';


--
-- Name: courses_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.courses ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.courses_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: enrollments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.enrollments (
    id bigint NOT NULL,
    student_id bigint NOT NULL,
    course_id bigint NOT NULL,
    start_date date DEFAULT public.tw_today() NOT NULL,
    end_date date,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT enrollments_status_check CHECK ((status = ANY (ARRAY['trial'::text, 'active'::text, 'dropped'::text, 'finished'::text])))
);


--
-- Name: TABLE enrollments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.enrollments IS '學生上哪些班（= 補了哪些科目）';


--
-- Name: COLUMN enrollments.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.id IS '編號（自動產生）';


--
-- Name: COLUMN enrollments.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.student_id IS '學生';


--
-- Name: COLUMN enrollments.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.course_id IS '班級';


--
-- Name: COLUMN enrollments.start_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.start_date IS '入班日';


--
-- Name: COLUMN enrollments.end_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.end_date IS '退班／結業日：NULL = 仍在班上';


--
-- Name: COLUMN enrollments.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.status IS '狀態：trial 試聽／active 在班／dropped 退班／finished 結業';


--
-- Name: COLUMN enrollments.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.enrollments.created_at IS '建立時間';


--
-- Name: enrollments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.enrollments ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.enrollments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: exam_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exam_items (
    id bigint NOT NULL,
    exam_id bigint NOT NULL,
    item_no smallint NOT NULL,
    points numeric(5,2),
    unit_tag text,
    question_image_url text,
    answer text,
    explanation text,
    question_bank_ref text
);


--
-- Name: TABLE exam_items; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.exam_items IS '考卷上的每一題（題目圖、答案、詳解、單元）';


--
-- Name: COLUMN exam_items.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.id IS '編號（自動產生）';


--
-- Name: COLUMN exam_items.exam_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.exam_id IS '考試';


--
-- Name: COLUMN exam_items.item_no; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.item_no IS '題號';


--
-- Name: COLUMN exam_items.points; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.points IS '配分';


--
-- Name: COLUMN exam_items.unit_tag; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.unit_tag IS '單元：／觀念，例：二次函數-配方法（弱點分析用）';


--
-- Name: COLUMN exam_items.question_image_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.question_image_url IS '題目圖：題目截圖';


--
-- Name: COLUMN exam_items.answer; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.answer IS '答案';


--
-- Name: COLUMN exam_items.explanation; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.explanation IS '詳解：文字或圖片網址';


--
-- Name: COLUMN exam_items.question_bank_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_items.question_bank_ref IS '題庫代號：若從 UPAD12 題庫出題，記題目代號';


--
-- Name: exam_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.exam_items ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.exam_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: exam_scores; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exam_scores (
    exam_id bigint NOT NULL,
    student_id bigint NOT NULL,
    score numeric(6,2),
    status text DEFAULT 'normal'::text NOT NULL,
    items_recorded boolean DEFAULT false NOT NULL,
    teacher_comment text,
    entered_by bigint,
    entered_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT exam_scores_check CHECK (((status = 'absent'::text) = (score IS NULL))),
    CONSTRAINT exam_scores_status_check CHECK ((status = ANY (ARRAY['normal'::text, 'absent'::text, 'makeup'::text])))
);


--
-- Name: TABLE exam_scores; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.exam_scores IS '每位學生每次考試的分數';


--
-- Name: COLUMN exam_scores.exam_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.exam_id IS '考試';


--
-- Name: COLUMN exam_scores.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.student_id IS '學生';


--
-- Name: COLUMN exam_scores.score; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.score IS '分數：缺考時為 NULL';


--
-- Name: COLUMN exam_scores.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.status IS '狀態：normal 正常／absent 缺考／makeup 補考';


--
-- Name: COLUMN exam_scores.items_recorded; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.items_recorded IS '已登錄逐題：對錯（分辨「全對」與「沒登錄」）';


--
-- Name: COLUMN exam_scores.teacher_comment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.teacher_comment IS '老師評語：給家長看的評語';


--
-- Name: COLUMN exam_scores.entered_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.entered_by IS '登錄老師';


--
-- Name: COLUMN exam_scores.entered_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exam_scores.entered_at IS '登錄時間';


--
-- Name: exams; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exams (
    id bigint NOT NULL,
    course_id bigint,
    session_id bigint,
    subject_id smallint NOT NULL,
    title text NOT NULL,
    exam_type text DEFAULT 'quiz'::text NOT NULL,
    exam_date date NOT NULL,
    full_score numeric(6,2) DEFAULT 100 NOT NULL,
    scope text,
    paper_url text,
    scores_published boolean DEFAULT false NOT NULL,
    scores_published_at timestamp with time zone,
    created_by bigint,
    external_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT exams_exam_type_check CHECK ((exam_type = ANY (ARRAY['quiz'::text, 'weekly'::text, 'mock'::text, 'school'::text, 'other'::text]))),
    CONSTRAINT exams_full_score_check CHECK ((full_score > (0)::numeric))
);


--
-- Name: TABLE exams; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.exams IS '考試（補習班小考、週考、模擬考，或學校段考）';


--
-- Name: COLUMN exams.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.id IS '編號（自動產生）';


--
-- Name: COLUMN exams.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.course_id IS '班級：哪一班考的；全年級模擬考可留空';


--
-- Name: COLUMN exams.session_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.session_id IS '那一堂課：哪一堂課考的（選填）';


--
-- Name: COLUMN exams.subject_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.subject_id IS '科目';


--
-- Name: COLUMN exams.title; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.title IS '考試名稱：例：第 5 週小考：二次函數';


--
-- Name: COLUMN exams.exam_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.exam_type IS '考試類型：quiz 小考／weekly 週考／mock 模擬考／school 學校段考／other 其他';


--
-- Name: COLUMN exams.exam_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.exam_date IS '考試日期';


--
-- Name: COLUMN exams.full_score; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.full_score IS '滿分';


--
-- Name: COLUMN exams.scope; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.scope IS '考試範圍：家長問「下次考什麼」用';


--
-- Name: COLUMN exams.paper_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.paper_url IS '考卷檔案';


--
-- Name: COLUMN exams.scores_published; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.scores_published IS '已發布成績：老師按「發布成績」後家長才查得到分數（範圍隨時可查）';


--
-- Name: COLUMN exams.scores_published_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.scores_published_at IS '發布時間';


--
-- Name: COLUMN exams.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.created_by IS '出題老師';


--
-- Name: COLUMN exams.external_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.external_ref IS 'UPAD12 對應代號';


--
-- Name: COLUMN exams.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.exams.created_at IS '建立時間';


--
-- Name: exams_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.exams ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.exams_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: families; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.families (
    id bigint NOT NULL,
    family_name text NOT NULL,
    phone text,
    address text,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE families; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.families IS '家庭：把兄弟姊妹和家長綁在一起；家長可查同一家庭所有學生';


--
-- Name: COLUMN families.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.id IS '編號（自動產生）';


--
-- Name: COLUMN families.family_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.family_name IS '家庭名稱：例：王小明家';


--
-- Name: COLUMN families.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.phone IS '家中電話：家中主要電話';


--
-- Name: COLUMN families.address; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.address IS '地址';


--
-- Name: COLUMN families.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.note IS '內部備註';


--
-- Name: COLUMN families.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.created_at IS '建立時間';


--
-- Name: COLUMN families.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.families.updated_at IS '最後修改時間';


--
-- Name: families_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.families ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.families_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: guardians; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guardians (
    id bigint NOT NULL,
    family_id bigint NOT NULL,
    name text NOT NULL,
    relationship text DEFAULT '家長'::text NOT NULL,
    phone text,
    email text,
    line_user_id text,
    is_primary boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    external_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE guardians; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.guardians IS '家長／監護人，屬於某個家庭';


--
-- Name: COLUMN guardians.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.id IS '編號（自動產生）';


--
-- Name: COLUMN guardians.family_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.family_id IS '家庭';


--
-- Name: COLUMN guardians.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.name IS '家長姓名';


--
-- Name: COLUMN guardians.relationship; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.relationship IS '關係：父／母／祖父母／其他';


--
-- Name: COLUMN guardians.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.phone IS '電話';


--
-- Name: COLUMN guardians.email; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.email IS 'Email';


--
-- Name: COLUMN guardians.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.line_user_id IS '綁定的 LINE';


--
-- Name: COLUMN guardians.is_primary; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.is_primary IS '主要聯絡人：繳費、緊急聯絡';


--
-- Name: COLUMN guardians.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.is_active IS '有效';


--
-- Name: COLUMN guardians.external_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.external_ref IS 'UPAD12 對應代號';


--
-- Name: COLUMN guardians.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.created_at IS '建立時間';


--
-- Name: COLUMN guardians.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.guardians.updated_at IS '最後修改時間';


--
-- Name: guardians_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.guardians ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.guardians_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: honor_roll; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.honor_roll (
    id bigint NOT NULL,
    school_year smallint NOT NULL,
    category text NOT NULL,
    student_id bigint,
    display_name text NOT NULL,
    school_name text,
    achievement text NOT NULL,
    consent boolean DEFAULT false NOT NULL,
    is_published boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    source_path text,
    CONSTRAINT honor_roll_check CHECK (((NOT is_published) OR consent))
);


--
-- Name: TABLE honor_roll; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.honor_roll IS '榜單（需家長同意才公開）';


--
-- Name: COLUMN honor_roll.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.id IS '編號（自動產生）';


--
-- Name: COLUMN honor_roll.school_year; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.school_year IS '學年度';


--
-- Name: COLUMN honor_roll.category; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.category IS '類別：會考／私中入學／段考／競賽';


--
-- Name: COLUMN honor_roll.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.student_id IS '學生';


--
-- Name: COLUMN honor_roll.display_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.display_name IS '顯示名稱：對外顯示，例：王○明';


--
-- Name: COLUMN honor_roll.school_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.school_name IS '學校：原就讀學校';


--
-- Name: COLUMN honor_roll.achievement; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.achievement IS '成績／錄取：例：會考 5A++、錄取第一志願';


--
-- Name: COLUMN honor_roll.consent; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.consent IS '家長同意：公開';


--
-- Name: COLUMN honor_roll.is_published; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.is_published IS '公開';


--
-- Name: COLUMN honor_roll.sort_order; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.sort_order IS '排序（數字小的在前）';


--
-- Name: COLUMN honor_roll.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.created_at IS '建立時間';


--
-- Name: COLUMN honor_roll.source_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.honor_roll.source_path IS '來源檔案：來自「補習班資料／榜單／榜單.csv」的第幾列；空白 = 用其他方式建立';


--
-- Name: honor_roll_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.honor_roll ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.honor_roll_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: kb_articles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kb_articles (
    id bigint NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    keywords text[] DEFAULT '{}'::text[] NOT NULL,
    audience text DEFAULT 'public'::text NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    updated_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    image_id bigint,
    source_path text,
    CONSTRAINT kb_articles_audience_check CHECK ((audience = ANY (ARRAY['public'::text, 'guardian'::text, 'staff'::text]))),
    CONSTRAINT kb_articles_category_check CHECK ((category = ANY (ARRAY['contact'::text, 'traffic'::text, 'course'::text, 'fee'::text, 'policy'::text, 'enrollment'::text, 'other'::text])))
);


--
-- Name: TABLE kb_articles; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.kb_articles IS 'AI 知識庫：交通、電話、收費、請假規定等標準答案';


--
-- Name: COLUMN kb_articles.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.id IS '編號（自動產生）';


--
-- Name: COLUMN kb_articles.category; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.category IS '分類：contact 聯絡方式／traffic 交通停車／course 課程介紹／fee 收費／policy 規定（請假、退費）／enrollment 招生試聽／other 其他';


--
-- Name: COLUMN kb_articles.title; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.title IS '問題標題：例：怎麼到補習班？';


--
-- Name: COLUMN kb_articles.content; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.content IS '標準答案：AI 依此回覆（可放地圖、網址）';


--
-- Name: COLUMN kb_articles.keywords; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.keywords IS '關鍵字';


--
-- Name: COLUMN kb_articles.audience; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.audience IS '對象：public 所有人／guardian 已綁定家長／staff 教職員';


--
-- Name: COLUMN kb_articles.sort_order; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.sort_order IS '排序（數字小的在前）';


--
-- Name: COLUMN kb_articles.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.is_active IS '啟用中';


--
-- Name: COLUMN kb_articles.updated_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.updated_by IS '最後修改者';


--
-- Name: COLUMN kb_articles.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.created_at IS '建立時間';


--
-- Name: COLUMN kb_articles.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.updated_at IS '最後修改時間';


--
-- Name: COLUMN kb_articles.image_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.image_id IS '附圖：例：交通地圖、收費表；回覆時一起傳給家長';


--
-- Name: COLUMN kb_articles.source_path; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kb_articles.source_path IS '來源檔案：來自「補習班資料」的哪個檔案（不含副檔名）；空白 = 用其他方式建立';


--
-- Name: kb_articles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.kb_articles ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.kb_articles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: leave_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leave_requests (
    id bigint NOT NULL,
    student_id bigint NOT NULL,
    session_id bigint NOT NULL,
    leave_type text DEFAULT 'personal'::text NOT NULL,
    reason text,
    status text DEFAULT 'submitted'::text NOT NULL,
    need_makeup boolean DEFAULT false NOT NULL,
    source text DEFAULT 'line'::text NOT NULL,
    requested_by_guardian bigint,
    requested_by_staff bigint,
    reviewed_by bigint,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT leave_requests_leave_type_check CHECK ((leave_type = ANY (ARRAY['sick'::text, 'personal'::text, 'school'::text, 'other'::text]))),
    CONSTRAINT leave_requests_source_check CHECK ((source = ANY (ARRAY['line'::text, 'phone'::text, 'counter'::text, 'upad12'::text]))),
    CONSTRAINT leave_requests_status_check CHECK ((status = ANY (ARRAY['submitted'::text, 'approved'::text, 'rejected'::text, 'cancelled'::text])))
);


--
-- Name: TABLE leave_requests; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.leave_requests IS '請假單，一堂課一筆';


--
-- Name: COLUMN leave_requests.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.id IS '編號（自動產生）';


--
-- Name: COLUMN leave_requests.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.student_id IS '學生';


--
-- Name: COLUMN leave_requests.session_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.session_id IS '那一堂課：一堂課一筆；請整天就多筆';


--
-- Name: COLUMN leave_requests.leave_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.leave_type IS '假別：sick 病假／personal 事假／school 學校活動／other 其他';


--
-- Name: COLUMN leave_requests.reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.reason IS '原因';


--
-- Name: COLUMN leave_requests.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.status IS '審核狀態：submitted 已送出／approved 已核准／rejected 不核准／cancelled 已取消';


--
-- Name: COLUMN leave_requests.need_makeup; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.need_makeup IS '要補課：需要補課／補看影片';


--
-- Name: COLUMN leave_requests.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.source IS '請假管道：line／phone 電話／counter 櫃台／upad12';


--
-- Name: COLUMN leave_requests.requested_by_guardian; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.requested_by_guardian IS '請假的家長';


--
-- Name: COLUMN leave_requests.requested_by_staff; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.requested_by_staff IS '代請的老師';


--
-- Name: COLUMN leave_requests.reviewed_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.reviewed_by IS '審核老師';


--
-- Name: COLUMN leave_requests.reviewed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.reviewed_at IS '審核時間';


--
-- Name: COLUMN leave_requests.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.leave_requests.created_at IS '建立時間';


--
-- Name: leave_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.leave_requests ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.leave_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: line_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.line_users (
    line_user_id text NOT NULL,
    display_name text,
    picture_url text,
    is_following boolean DEFAULT true NOT NULL,
    menu_role text DEFAULT 'guest'::text NOT NULL,
    followed_at timestamp with time zone,
    unfollowed_at timestamp with time zone,
    last_active_at timestamp with time zone,
    bot_paused_until timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT line_users_line_user_id_check CHECK ((line_user_id ~ '^U[0-9a-f]{32}$'::text)),
    CONSTRAINT line_users_menu_role_check CHECK ((menu_role = ANY (ARRAY['guest'::text, 'guardian'::text, 'student'::text, 'staff'::text])))
);


--
-- Name: TABLE line_users; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.line_users IS '加入官方帳號的每個 LINE 使用者，含尚未綁定的訪客';


--
-- Name: COLUMN line_users.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.line_user_id IS 'LINE 使用者 ID';


--
-- Name: COLUMN line_users.display_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.display_name IS 'LINE 暱稱';


--
-- Name: COLUMN line_users.picture_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.picture_url IS '大頭貼網址';


--
-- Name: COLUMN line_users.is_following; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.is_following IS '仍是好友：收到 unfollow（封鎖）事件時改 false';


--
-- Name: COLUMN line_users.menu_role; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.menu_role IS '圖文選單身分：最後一次幫他連結的圖文選單；真正身分以 v_line_identity 為準';


--
-- Name: COLUMN line_users.followed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.followed_at IS '加好友時間';


--
-- Name: COLUMN line_users.unfollowed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.unfollowed_at IS '封鎖時間';


--
-- Name: COLUMN line_users.last_active_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.last_active_at IS '最後互動時間';


--
-- Name: COLUMN line_users.bot_paused_until; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.bot_paused_until IS 'AI 暫停到：家長按「找真人客服」後，AI 暫停自動回覆到這個時間';


--
-- Name: COLUMN line_users.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.line_users.created_at IS '建立時間';


--
-- Name: meal_menus; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meal_menus (
    id bigint NOT NULL,
    meal_date date NOT NULL,
    meal_slot text DEFAULT 'dinner'::text NOT NULL,
    order_deadline timestamp with time zone NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    note text,
    CONSTRAINT meal_menus_meal_slot_check CHECK ((meal_slot = ANY (ARRAY['lunch'::text, 'dinner'::text]))),
    CONSTRAINT meal_menus_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'open'::text, 'closed'::text, 'delivered'::text])))
);


--
-- Name: TABLE meal_menus; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.meal_menus IS '餐單上的一格：某一天的某一餐';


--
-- Name: COLUMN meal_menus.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_menus.id IS '編號（自動產生）';


--
-- Name: COLUMN meal_menus.meal_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_menus.meal_date IS '日期';


--
-- Name: COLUMN meal_menus.meal_slot; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_menus.meal_slot IS '哪一餐：lunch 午餐／dinner 晚餐';


--
-- Name: COLUMN meal_menus.order_deadline; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_menus.order_deadline IS '訂餐截止：訂餐／改餐截止時間';


--
-- Name: COLUMN meal_menus.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_menus.status IS '狀態：draft 草稿／open 開放訂餐／closed 截止／delivered 已送達';


--
-- Name: COLUMN meal_menus.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_menus.note IS '備註';


--
-- Name: meal_menus_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.meal_menus ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.meal_menus_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: meal_options; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meal_options (
    id bigint NOT NULL,
    meal_menu_id bigint NOT NULL,
    option_no smallint NOT NULL,
    vendor_id bigint NOT NULL,
    item_name text NOT NULL,
    price integer NOT NULL,
    CONSTRAINT meal_options_option_no_check CHECK (((option_no >= 1) AND (option_no <= 9))),
    CONSTRAINT meal_options_price_check CHECK ((price >= 0))
);


--
-- Name: TABLE meal_options; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.meal_options IS '該餐的 1、2、3 號餐（店家、品名、價格）';


--
-- Name: COLUMN meal_options.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_options.id IS '編號（自動產生）';


--
-- Name: COLUMN meal_options.meal_menu_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_options.meal_menu_id IS '餐單';


--
-- Name: COLUMN meal_options.option_no; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_options.option_no IS '幾號餐：目前是 1、2、3 號餐';


--
-- Name: COLUMN meal_options.vendor_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_options.vendor_id IS '店家：每個選項可來自不同店家';


--
-- Name: COLUMN meal_options.item_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_options.item_name IS '品名';


--
-- Name: COLUMN meal_options.price; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_options.price IS '價格';


--
-- Name: meal_options_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.meal_options ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.meal_options_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: meal_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meal_orders (
    id bigint NOT NULL,
    student_id bigint NOT NULL,
    meal_menu_id bigint NOT NULL,
    meal_option_id bigint,
    unit_price integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    source text DEFAULT 'paper'::text NOT NULL,
    ordered_by_guardian bigint,
    entered_by_staff bigint,
    confirmed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT meal_orders_source_check CHECK ((source = ANY (ARRAY['paper'::text, 'line'::text, 'staff'::text]))),
    CONSTRAINT meal_orders_status_check CHECK ((status = ANY (ARRAY['active'::text, 'cancelled'::text]))),
    CONSTRAINT meal_orders_unit_price_check CHECK ((unit_price >= 0))
);


--
-- Name: TABLE meal_orders; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.meal_orders IS '學生的訂餐（含「不訂」）';


--
-- Name: COLUMN meal_orders.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.id IS '編號（自動產生）';


--
-- Name: COLUMN meal_orders.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.student_id IS '學生';


--
-- Name: COLUMN meal_orders.meal_menu_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.meal_menu_id IS '餐單';


--
-- Name: COLUMN meal_orders.meal_option_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.meal_option_id IS '選的餐：NULL = 明確勾選「不訂」';


--
-- Name: COLUMN meal_orders.unit_price; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.unit_price IS '價格：下單當下的價格（觸發器自動從 meal_options 帶入，之後改菜單價格不影響已確認的訂單）';


--
-- Name: COLUMN meal_orders.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.status IS '狀態：active 有效／cancelled 已取消';


--
-- Name: COLUMN meal_orders.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.source IS '訂餐管道：paper 紙本／line／staff 老師登錄';


--
-- Name: COLUMN meal_orders.ordered_by_guardian; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.ordered_by_guardian IS '訂餐家長';


--
-- Name: COLUMN meal_orders.entered_by_staff; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.entered_by_staff IS '登錄老師';


--
-- Name: COLUMN meal_orders.confirmed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.confirmed_at IS '家長確認時間：家長在 LINE 按下「確認金額」的時間';


--
-- Name: COLUMN meal_orders.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.created_at IS '建立時間';


--
-- Name: COLUMN meal_orders.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.meal_orders.updated_at IS '最後修改時間';


--
-- Name: meal_orders_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.meal_orders ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.meal_orders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: media_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.media_files (
    id bigint NOT NULL,
    token text DEFAULT replace((gen_random_uuid())::text, '-'::text, ''::text) NOT NULL,
    original_name text,
    content_type text DEFAULT 'image/jpeg'::text NOT NULL,
    width integer,
    height integer,
    bytes integer NOT NULL,
    data bytea NOT NULL,
    preview bytea NOT NULL,
    note text,
    uploaded_by text,
    source_hash text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE media_files; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.media_files IS '上傳的圖片（行事曆、交通地圖…）；網址 /files/代號.jpg 給 LINE 讀取';


--
-- Name: COLUMN media_files.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.id IS '編號（自動產生）';


--
-- Name: COLUMN media_files.token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.token IS '網址代號：圖片網址用的隨機代號（/files/代號.jpg），別人猜不到其他圖片的網址';


--
-- Name: COLUMN media_files.original_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.original_name IS '原檔名：上傳時的檔名';


--
-- Name: COLUMN media_files.content_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.content_type IS '檔案類型';


--
-- Name: COLUMN media_files.width; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.width IS '寬（像素）';


--
-- Name: COLUMN media_files.height; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.height IS '高（像素）';


--
-- Name: COLUMN media_files.bytes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.bytes IS '檔案大小：原圖大小（位元組）';


--
-- Name: COLUMN media_files.data; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.data IS '原圖：已轉正、壓成 JPEG、最長邊 4096（LINE 上限 10MB）';


--
-- Name: COLUMN media_files.preview; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.preview IS '預覽圖：最長邊 1024（LINE 規定 1MB 以內）';


--
-- Name: COLUMN media_files.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.note IS '用途：說明，例：115 年 11 月行事曆';


--
-- Name: COLUMN media_files.uploaded_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.uploaded_by IS '上傳者：上傳的人（後台登入名稱）；從資料夾來的是 folder';


--
-- Name: COLUMN media_files.source_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.source_hash IS '檔案指紋：原始檔案的 SHA-256（同一張圖不重複存）';


--
-- Name: COLUMN media_files.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.media_files.created_at IS '建立時間';


--
-- Name: media_files_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.media_files ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.media_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: message_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_logs (
    id bigint NOT NULL,
    line_user_id text NOT NULL,
    webhook_event_id text,
    direction text NOT NULL,
    message_type text DEFAULT 'text'::text NOT NULL,
    content text,
    intent text,
    tool_calls jsonb,
    handled_by text DEFAULT 'ai'::text NOT NULL,
    needs_human boolean DEFAULT false NOT NULL,
    model text,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reply_to bigint,
    CONSTRAINT message_logs_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text]))),
    CONSTRAINT message_logs_handled_by_check CHECK ((handled_by = ANY (ARRAY['ai'::text, 'rule'::text, 'staff'::text])))
);


--
-- Name: TABLE message_logs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.message_logs IS 'LINE 對話紀錄（除錯、找出 AI 答不好的問題）';


--
-- Name: COLUMN message_logs.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.id IS '編號（自動產生）';


--
-- Name: COLUMN message_logs.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.line_user_id IS 'LINE 使用者 ID';


--
-- Name: COLUMN message_logs.webhook_event_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.webhook_event_id IS 'LINE 事件 ID：LINE 的 webhookEventId；重送時用來避免重複處理';


--
-- Name: COLUMN message_logs.direction; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.direction IS '方向：in 收到／out 回覆';


--
-- Name: COLUMN message_logs.message_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.message_type IS '訊息類型：text／postback／image／sticker／flex…';


--
-- Name: COLUMN message_logs.content; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.content IS '內容';


--
-- Name: COLUMN message_logs.intent; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.intent IS '意圖：AI 判斷的意圖：faq／schedule／score／leave／meal…';


--
-- Name: COLUMN message_logs.tool_calls; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.tool_calls IS '處理細節：AI 呼叫了哪些查詢、帶什麼參數';


--
-- Name: COLUMN message_logs.handled_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.handled_by IS '誰回的：ai 由 AI 回覆／rule 固定回覆或關鍵字比對／staff 真人';


--
-- Name: COLUMN message_logs.needs_human; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.needs_human IS '需要真人：AI 答不出來或家長要求真人 → 通知櫃台';


--
-- Name: COLUMN message_logs.model; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.model IS 'AI 模型';


--
-- Name: COLUMN message_logs.latency_ms; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.latency_ms IS '回覆耗時（毫秒）';


--
-- Name: COLUMN message_logs.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.created_at IS '建立時間';


--
-- Name: COLUMN message_logs.reply_to; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.message_logs.reply_to IS '回覆哪一則：機器人的回覆（out）對應的家長訊息（in）';


--
-- Name: message_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.message_logs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.message_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: pending_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_actions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    line_user_id text NOT NULL,
    action_type text NOT NULL,
    payload jsonb NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '00:10:00'::interval) NOT NULL,
    confirmed_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    result jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pending_actions_action_type_check CHECK ((action_type = ANY (ARRAY['leave'::text, 'meal_order'::text, 'meal_cancel'::text])))
);


--
-- Name: TABLE pending_actions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.pending_actions IS 'AI 提出、等家長按「確認」才執行的動作（請假、訂餐）';


--
-- Name: COLUMN pending_actions.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.id IS '確認碼：放進確認按鈕的 postback data';


--
-- Name: COLUMN pending_actions.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.line_user_id IS 'LINE 使用者 ID';


--
-- Name: COLUMN pending_actions.action_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.action_type IS '動作：leave 請假／meal_order 訂餐／meal_cancel 取消訂餐';


--
-- Name: COLUMN pending_actions.payload; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.payload IS '內容：要執行的內容：學生、日期、餐點、金額…';


--
-- Name: COLUMN pending_actions.expires_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.expires_at IS '過期時間';


--
-- Name: COLUMN pending_actions.confirmed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.confirmed_at IS '確認時間';


--
-- Name: COLUMN pending_actions.cancelled_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.cancelled_at IS '取消時間';


--
-- Name: COLUMN pending_actions.result; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.result IS '結果：執行結果，例：{"leave_request_ids":[12,13]}';


--
-- Name: COLUMN pending_actions.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.pending_actions.created_at IS '建立時間';


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE schema_migrations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.schema_migrations IS '已執行過的資料庫升級檔（sql/00X_*.sql）';


--
-- Name: COLUMN schema_migrations.version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.schema_migrations.version IS '升級檔：編號，例：003';


--
-- Name: COLUMN schema_migrations.applied_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.schema_migrations.applied_at IS '執行時間';


--
-- Name: staff; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.staff (
    id bigint NOT NULL,
    name text NOT NULL,
    role text NOT NULL,
    phone text,
    line_user_id text,
    is_active boolean DEFAULT true NOT NULL,
    external_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    notify_human boolean DEFAULT false NOT NULL,
    CONSTRAINT staff_role_check CHECK ((role = ANY (ARRAY['admin'::text, 'tutor'::text, 'teacher'::text, 'counter'::text])))
);


--
-- Name: TABLE staff; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.staff IS '教職員：管理者、導師、授課老師、櫃台；綁定 LINE 後可以收「需要真人」的通知';


--
-- Name: COLUMN staff.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.id IS '編號（自動產生）';


--
-- Name: COLUMN staff.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.name IS '姓名';


--
-- Name: COLUMN staff.role; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.role IS '職務：admin 管理者／tutor 導師／teacher 授課老師／counter 櫃台';


--
-- Name: COLUMN staff.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.phone IS '電話';


--
-- Name: COLUMN staff.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.line_user_id IS '綁定的 LINE';


--
-- Name: COLUMN staff.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.is_active IS '在職';


--
-- Name: COLUMN staff.external_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.external_ref IS 'UPAD12 對應代號：UPAD12 的對應代號（有的話）';


--
-- Name: COLUMN staff.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.created_at IS '建立時間';


--
-- Name: COLUMN staff.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.updated_at IS '最後修改時間';


--
-- Name: COLUMN staff.notify_human; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff.notify_human IS '收通知：家長需要真人時，用 LINE 通知這位員工（會用掉訊息額度）';


--
-- Name: staff_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.staff_alerts (
    id bigint NOT NULL,
    line_user_id text NOT NULL,
    reason text NOT NULL,
    message_id bigint,
    summary text NOT NULL,
    sent_to integer DEFAULT 0 NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT staff_alerts_reason_check CHECK ((reason = ANY (ARRAY['handoff'::text, 'unanswered'::text])))
);


--
-- Name: TABLE staff_alerts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.staff_alerts IS '需要真人的紀錄：家長按「找真人客服」或機器人答不出來時，用 LINE 通知員工（同一位家長 30 分鐘內只通知一次）';


--
-- Name: COLUMN staff_alerts.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.id IS '編號（自動產生）';


--
-- Name: COLUMN staff_alerts.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.line_user_id IS '哪一位家長的 LINE';


--
-- Name: COLUMN staff_alerts.reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.reason IS '原因：handoff 家長按「找真人客服」或正在等真人／unanswered 機器人答不出來';


--
-- Name: COLUMN staff_alerts.message_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.message_id IS '是哪一則訊息';


--
-- Name: COLUMN staff_alerts.summary; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.summary IS '通知內容：哪位家長、最近的訊息';


--
-- Name: COLUMN staff_alerts.sent_to; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.sent_to IS '通知到幾位員工';


--
-- Name: COLUMN staff_alerts.error; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.error IS '通知失敗的原因';


--
-- Name: COLUMN staff_alerts.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.staff_alerts.created_at IS '時間';


--
-- Name: staff_alerts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.staff_alerts ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.staff_alerts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: staff_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.staff ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.staff_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: student_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.student_notes (
    id bigint NOT NULL,
    student_id bigint NOT NULL,
    staff_id bigint NOT NULL,
    note_date date DEFAULT public.tw_today() NOT NULL,
    content text NOT NULL,
    visibility text DEFAULT 'internal'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT student_notes_visibility_check CHECK ((visibility = ANY (ARRAY['internal'::text, 'guardian'::text])))
);


--
-- Name: TABLE student_notes; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.student_notes IS '導師紀錄／聯絡簿；只有 visibility = guardian 的才會給家長';


--
-- Name: COLUMN student_notes.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.id IS '編號（自動產生）';


--
-- Name: COLUMN student_notes.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.student_id IS '學生';


--
-- Name: COLUMN student_notes.staff_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.staff_id IS '教職員';


--
-- Name: COLUMN student_notes.note_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.note_date IS '日期';


--
-- Name: COLUMN student_notes.content; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.content IS '內容';


--
-- Name: COLUMN student_notes.visibility; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.visibility IS '誰看得到：internal 內部／guardian 家長可見';


--
-- Name: COLUMN student_notes.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_notes.created_at IS '建立時間';


--
-- Name: student_notes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.student_notes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.student_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: student_wrong_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.student_wrong_items (
    exam_item_id bigint NOT NULL,
    student_id bigint NOT NULL,
    student_answer text,
    is_corrected boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE student_wrong_items; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.student_wrong_items IS '學生答錯的題目，錯題本的來源（只記錯的，減少輸入）';


--
-- Name: COLUMN student_wrong_items.exam_item_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_wrong_items.exam_item_id IS '題目';


--
-- Name: COLUMN student_wrong_items.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_wrong_items.student_id IS '學生';


--
-- Name: COLUMN student_wrong_items.student_answer; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_wrong_items.student_answer IS '學生答案';


--
-- Name: COLUMN student_wrong_items.is_corrected; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_wrong_items.is_corrected IS '已訂正：學生已訂正';


--
-- Name: COLUMN student_wrong_items.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.student_wrong_items.created_at IS '建立時間';


--
-- Name: students; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.students (
    id bigint NOT NULL,
    student_no text NOT NULL,
    family_id bigint NOT NULL,
    name text NOT NULL,
    school_name text,
    grade smallint NOT NULL,
    grade_school_year smallint NOT NULL,
    phone text,
    line_user_id text,
    status text DEFAULT 'active'::text NOT NULL,
    joined_on date,
    left_on date,
    note text,
    external_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT students_grade_check CHECK (((grade >= 6) AND (grade <= 9))),
    CONSTRAINT students_status_check CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'left'::text])))
);


--
-- Name: TABLE students; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.students IS '學生基本資料，屬於某個家庭';


--
-- Name: COLUMN students.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.id IS '編號（自動產生）';


--
-- Name: COLUMN students.student_no; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.student_no IS '學號：建議直接沿用 UPAD12 學號';


--
-- Name: COLUMN students.family_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.family_id IS '家庭';


--
-- Name: COLUMN students.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.name IS '學生姓名';


--
-- Name: COLUMN students.school_name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.school_name IS '就讀學校';


--
-- Name: COLUMN students.grade; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.grade IS '年級：6 國小、7~9 國一~國三；是「grade_school_year 那一學年」的年級';


--
-- Name: COLUMN students.grade_school_year; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.grade_school_year IS '年級所屬學年度：上面年級所屬學年度，例：115。現在年級由 v_students 自動推算';


--
-- Name: COLUMN students.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.phone IS '學生手機：學生本人手機（選填）';


--
-- Name: COLUMN students.line_user_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.line_user_id IS '學生本人的 LINE：預留：學生本人也綁 LINE';


--
-- Name: COLUMN students.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.status IS '在學狀態：active 在學／paused 暫停／left 離班';


--
-- Name: COLUMN students.joined_on; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.joined_on IS '入班日';


--
-- Name: COLUMN students.left_on; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.left_on IS '離班日';


--
-- Name: COLUMN students.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.note IS '內部備註：不會出現在 AI 用的檢視表';


--
-- Name: COLUMN students.external_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.external_ref IS 'UPAD12 對應代號';


--
-- Name: COLUMN students.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.created_at IS '建立時間';


--
-- Name: COLUMN students.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.students.updated_at IS '最後修改時間';


--
-- Name: students_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.students ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.students_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: subjects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subjects (
    id smallint NOT NULL,
    name text NOT NULL
);


--
-- Name: TABLE subjects; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.subjects IS '科目';


--
-- Name: COLUMN subjects.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.subjects.id IS '編號（自動產生）';


--
-- Name: COLUMN subjects.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.subjects.name IS '科目名稱';


--
-- Name: subjects_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.subjects ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.subjects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: sync_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sync_runs (
    id bigint NOT NULL,
    source text DEFAULT 'upad12'::text NOT NULL,
    entity text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    rows_read integer,
    rows_upserted integer,
    status text DEFAULT 'running'::text NOT NULL,
    error text,
    CONSTRAINT sync_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'success'::text, 'failed'::text])))
);


--
-- Name: TABLE sync_runs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.sync_runs IS 'UPAD12 資料匯入紀錄';


--
-- Name: COLUMN sync_runs.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.id IS '編號（自動產生）';


--
-- Name: COLUMN sync_runs.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.source IS '來源';


--
-- Name: COLUMN sync_runs.entity; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.entity IS '資料種類：students／courses／attendance／exam_scores…';


--
-- Name: COLUMN sync_runs.started_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.started_at IS '開始';


--
-- Name: COLUMN sync_runs.finished_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.finished_at IS '結束';


--
-- Name: COLUMN sync_runs.rows_read; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.rows_read IS '讀取筆數';


--
-- Name: COLUMN sync_runs.rows_upserted; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.rows_upserted IS '寫入筆數';


--
-- Name: COLUMN sync_runs.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.status IS '狀態：running 執行中／success 成功／failed 失敗';


--
-- Name: COLUMN sync_runs.error; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sync_runs.error IS '錯誤訊息';


--
-- Name: sync_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.sync_runs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.sync_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: tuition_bills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuition_bills (
    id bigint NOT NULL,
    student_id bigint NOT NULL,
    course_id bigint,
    title text NOT NULL,
    amount integer NOT NULL,
    paid_amount integer DEFAULT 0 NOT NULL,
    pay_status text GENERATED ALWAYS AS (
CASE
    WHEN (paid_amount >= amount) THEN 'paid'::text
    WHEN (paid_amount > 0) THEN 'partial'::text
    ELSE 'unpaid'::text
END) STORED,
    due_date date,
    paid_at timestamp with time zone,
    pay_method text,
    is_void boolean DEFAULT false NOT NULL,
    note text,
    external_ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tuition_bills_amount_check CHECK ((amount >= 0)),
    CONSTRAINT tuition_bills_paid_amount_check CHECK ((paid_amount >= 0))
);


--
-- Name: TABLE tuition_bills; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.tuition_bills IS '學費帳單';


--
-- Name: COLUMN tuition_bills.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.id IS '編號（自動產生）';


--
-- Name: COLUMN tuition_bills.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.student_id IS '學生';


--
-- Name: COLUMN tuition_bills.course_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.course_id IS '班級';


--
-- Name: COLUMN tuition_bills.title; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.title IS '帳單名稱：例：115 上 國二數學 A 班 第 1 期';


--
-- Name: COLUMN tuition_bills.amount; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.amount IS '應繳';


--
-- Name: COLUMN tuition_bills.paid_amount; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.paid_amount IS '已繳';


--
-- Name: COLUMN tuition_bills.pay_status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.pay_status IS '繳費狀態：自動計算，不會和金額不一致';


--
-- Name: COLUMN tuition_bills.due_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.due_date IS '繳費期限';


--
-- Name: COLUMN tuition_bills.paid_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.paid_at IS '繳費時間';


--
-- Name: COLUMN tuition_bills.pay_method; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.pay_method IS '付款方式：現金／轉帳／…';


--
-- Name: COLUMN tuition_bills.is_void; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.is_void IS '作廢';


--
-- Name: COLUMN tuition_bills.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.note IS '備註';


--
-- Name: COLUMN tuition_bills.external_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.external_ref IS 'UPAD12 對應代號：UPAD12 帳單編號';


--
-- Name: COLUMN tuition_bills.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.created_at IS '建立時間';


--
-- Name: COLUMN tuition_bills.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tuition_bills.updated_at IS '最後修改時間';


--
-- Name: tuition_bills_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.tuition_bills ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.tuition_bills_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: v_active_announcements; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_active_announcements AS
 SELECT id,
    title,
    body,
    category,
    audience,
    course_id,
    grade,
    starts_at,
    ends_at,
    is_pinned,
    pushed_at,
    created_by,
    created_at,
    image_id,
    source_path,
    grades
   FROM public.announcements
  WHERE ((starts_at <= now()) AND ((ends_at IS NULL) OR (ends_at > now())));


--
-- Name: v_students; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_students AS
 SELECT id,
    student_no,
    family_id,
    name,
    school_name,
    status,
    line_user_id,
    ((grade + public.school_year_of(public.tw_today())) - grade_school_year) AS current_grade,
    public.grade_label((((grade + public.school_year_of(public.tw_today())) - grade_school_year))::integer) AS current_grade_label
   FROM public.students s;


--
-- Name: v_guardian_students; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_guardian_students AS
 SELECT g.line_user_id,
    g.id AS guardian_id,
    g.name AS guardian_name,
    g.family_id,
    s.id AS student_id,
    s.name AS student_name,
    s.current_grade,
    s.current_grade_label
   FROM (public.guardians g
     JOIN public.v_students s ON (((s.family_id = g.family_id) AND (s.status <> 'left'::text))))
  WHERE (g.is_active AND (g.line_user_id IS NOT NULL));


--
-- Name: v_line_identity; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_line_identity AS
 SELECT lu.line_user_id,
    lu.display_name,
    lu.is_following,
    st.id AS staff_id,
    st.role AS staff_role,
    g.id AS guardian_id,
    g.family_id,
    s.id AS student_id,
        CASE
            WHEN (st.id IS NOT NULL) THEN 'staff'::text
            WHEN (g.id IS NOT NULL) THEN 'guardian'::text
            WHEN (s.id IS NOT NULL) THEN 'student'::text
            ELSE 'guest'::text
        END AS role
   FROM (((public.line_users lu
     LEFT JOIN public.staff st ON (((st.line_user_id = lu.line_user_id) AND st.is_active)))
     LEFT JOIN public.guardians g ON (((g.line_user_id = lu.line_user_id) AND g.is_active)))
     LEFT JOIN public.students s ON (((s.line_user_id = lu.line_user_id) AND (s.status <> 'left'::text))));


--
-- Name: vendors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendors (
    id bigint NOT NULL,
    name text NOT NULL,
    phone text,
    note text,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: TABLE vendors; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.vendors IS '餐點店家';


--
-- Name: COLUMN vendors.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.vendors.id IS '編號（自動產生）';


--
-- Name: COLUMN vendors.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.vendors.name IS '店家名稱';


--
-- Name: COLUMN vendors.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.vendors.phone IS '電話';


--
-- Name: COLUMN vendors.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.vendors.note IS '備註';


--
-- Name: COLUMN vendors.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.vendors.is_active IS '合作中';


--
-- Name: v_meal_order_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_meal_order_summary AS
 SELECT mm.id AS meal_menu_id,
    mm.meal_date,
    mm.meal_slot,
    op.option_no,
    op.item_name,
    v.name AS vendor,
    v.phone AS vendor_phone,
    op.price,
    count(o.id) AS qty,
    COALESCE(sum(o.unit_price), (0)::bigint) AS total
   FROM (((public.meal_menus mm
     JOIN public.meal_options op ON ((op.meal_menu_id = mm.id)))
     JOIN public.vendors v ON ((v.id = op.vendor_id)))
     LEFT JOIN public.meal_orders o ON (((o.meal_option_id = op.id) AND (o.status = 'active'::text))))
  GROUP BY mm.id, mm.meal_date, mm.meal_slot, op.option_no, op.item_name, v.name, v.phone, op.price;


--
-- Name: v_public_timetable; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_public_timetable AS
 SELECT c.id AS course_id,
    c.name AS course_name,
    sub.name AS subject,
    c.grade,
    public.grade_label((c.grade)::integer) AS grade_label,
    cs.weekday,
    cs.start_time,
    cs.end_time,
    cs.room,
    c.fee,
    c.start_date,
    c.end_date,
    c.description
   FROM ((public.courses c
     JOIN public.subjects sub ON ((sub.id = c.subject_id)))
     JOIN public.course_schedules cs ON ((cs.course_id = c.id)))
  WHERE (c.is_public AND (c.status = 'open'::text));


--
-- Name: v_session_leaves; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_session_leaves AS
 SELECT ses.session_date,
    ses.start_time,
    c.id AS course_id,
    c.name AS course_name,
    s.id AS student_id,
    s.name AS student_name,
    lr.id AS leave_id,
    lr.leave_type,
    lr.reason,
    lr.status,
    lr.need_makeup,
    lr.source,
    lr.created_at,
    (EXISTS ( SELECT 1
           FROM (public.meal_orders mo
             JOIN public.meal_menus mm ON ((mm.id = mo.meal_menu_id)))
          WHERE ((mo.student_id = s.id) AND (mm.meal_date = ses.session_date) AND (mo.status = 'active'::text) AND (mo.meal_option_id IS NOT NULL)))) AS has_meal_order
   FROM (((public.leave_requests lr
     JOIN public.class_sessions ses ON ((ses.id = lr.session_id)))
     JOIN public.courses c ON ((c.id = ses.course_id)))
     JOIN public.students s ON ((s.id = lr.student_id)))
  WHERE (lr.status = ANY (ARRAY['submitted'::text, 'approved'::text]));


--
-- Name: wallet_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_transactions (
    id bigint NOT NULL,
    student_id bigint NOT NULL,
    txn_type text NOT NULL,
    amount integer NOT NULL,
    meal_order_id bigint,
    note text,
    created_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wallet_transactions_amount_check CHECK ((amount <> 0)),
    CONSTRAINT wallet_transactions_check CHECK ((((txn_type = ANY (ARRAY['topup'::text, 'meal_refund'::text])) AND (amount > 0)) OR ((txn_type = 'meal_charge'::text) AND (amount < 0)) OR (txn_type = 'adjust'::text))),
    CONSTRAINT wallet_transactions_check1 CHECK (((txn_type <> ALL (ARRAY['meal_charge'::text, 'meal_refund'::text])) OR (meal_order_id IS NOT NULL))),
    CONSTRAINT wallet_transactions_txn_type_check CHECK ((txn_type = ANY (ARRAY['topup'::text, 'meal_charge'::text, 'meal_refund'::text, 'adjust'::text])))
);


--
-- Name: TABLE wallet_transactions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.wallet_transactions IS '餐費帳本：只能新增不能修改，更正請新增 adjust';


--
-- Name: COLUMN wallet_transactions.id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.id IS '編號（自動產生）';


--
-- Name: COLUMN wallet_transactions.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.student_id IS '學生';


--
-- Name: COLUMN wallet_transactions.txn_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.txn_type IS '類型：topup 儲值／meal_charge 訂餐扣款／meal_refund 取消退回／adjust 人工調整';


--
-- Name: COLUMN wallet_transactions.amount; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.amount IS '金額：正數 = 加錢，負數 = 扣錢';


--
-- Name: COLUMN wallet_transactions.meal_order_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.meal_order_id IS '對應訂單';


--
-- Name: COLUMN wallet_transactions.note; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.note IS '備註';


--
-- Name: COLUMN wallet_transactions.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.created_by IS '經手人';


--
-- Name: COLUMN wallet_transactions.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallet_transactions.created_at IS '建立時間';


--
-- Name: v_student_meals; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_student_meals AS
 SELECT o.id AS meal_order_id,
    o.student_id,
    mm.meal_date,
    mm.meal_slot,
    mm.order_deadline,
    op.option_no,
    op.item_name,
    v.name AS vendor,
    o.unit_price,
    o.status,
    (EXISTS ( SELECT 1
           FROM public.wallet_transactions t
          WHERE ((t.meal_order_id = o.id) AND (t.txn_type = 'meal_charge'::text)))) AS is_charged
   FROM (((public.meal_orders o
     JOIN public.meal_menus mm ON ((mm.id = o.meal_menu_id)))
     LEFT JOIN public.meal_options op ON ((op.id = o.meal_option_id)))
     LEFT JOIN public.vendors v ON ((v.id = op.vendor_id)));


--
-- Name: v_student_notes_for_guardian; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_student_notes_for_guardian AS
 SELECT n.student_id,
    n.note_date,
    st.name AS staff_name,
    n.content
   FROM (public.student_notes n
     JOIN public.staff st ON ((st.id = n.staff_id)))
  WHERE (n.visibility = 'guardian'::text);


--
-- Name: v_student_scores; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_student_scores AS
 SELECT sc.student_id,
    ex.id AS exam_id,
    ex.exam_date,
    ex.title,
    ex.exam_type,
    sub.name AS subject,
    c.name AS course_name,
    sc.score,
    ex.full_score,
    sc.status,
    round(((sc.score * 100.0) / ex.full_score), 1) AS score_pct,
    round(avg(sc.score) OVER w, 1) AS exam_avg,
    max(sc.score) OVER w AS exam_max,
    count(sc.score) OVER w AS takers,
        CASE
            WHEN (sc.score IS NOT NULL) THEN rank() OVER (PARTITION BY ex.id ORDER BY sc.score DESC NULLS LAST)
            ELSE NULL::bigint
        END AS exam_rank,
    sc.teacher_comment
   FROM (((public.exam_scores sc
     JOIN public.exams ex ON (((ex.id = sc.exam_id) AND ex.scores_published)))
     JOIN public.subjects sub ON ((sub.id = ex.subject_id)))
     LEFT JOIN public.courses c ON ((c.id = ex.course_id)))
  WINDOW w AS (PARTITION BY ex.id);


--
-- Name: v_student_sessions; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_student_sessions AS
 SELECT e.student_id,
    ses.id AS session_id,
    ses.session_date,
    ses.start_time,
    ses.end_time,
    ses.room,
    ses.status AS session_status,
    ses.note AS session_note,
    c.id AS course_id,
    c.name AS course_name,
    sub.name AS subject,
    lr.id AS leave_id,
    lr.status AS leave_status
   FROM ((((public.enrollments e
     JOIN public.class_sessions ses ON (((ses.course_id = e.course_id) AND (ses.session_date >= e.start_date) AND ((e.end_date IS NULL) OR (ses.session_date <= e.end_date)))))
     JOIN public.courses c ON ((c.id = ses.course_id)))
     JOIN public.subjects sub ON ((sub.id = c.subject_id)))
     LEFT JOIN public.leave_requests lr ON (((lr.session_id = ses.id) AND (lr.student_id = e.student_id) AND (lr.status = ANY (ARRAY['submitted'::text, 'approved'::text])))))
  WHERE (e.status = ANY (ARRAY['trial'::text, 'active'::text]));


--
-- Name: v_student_timetable; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_student_timetable AS
 SELECT e.student_id,
    c.id AS course_id,
    c.name AS course_name,
    sub.name AS subject,
    cs.weekday,
    cs.start_time,
    cs.end_time,
    cs.room,
    ( SELECT string_agg(st.name, '、'::text ORDER BY st.name) AS string_agg
           FROM (public.course_staff x
             JOIN public.staff st ON ((st.id = x.staff_id)))
          WHERE ((x.course_id = c.id) AND (x.role = 'teacher'::text))) AS teachers,
    e.status AS enrollment_status
   FROM (((public.enrollments e
     JOIN public.courses c ON (((c.id = e.course_id) AND (c.status = 'open'::text))))
     JOIN public.subjects sub ON ((sub.id = c.subject_id)))
     JOIN public.course_schedules cs ON ((cs.course_id = c.id)))
  WHERE ((e.status = ANY (ARRAY['trial'::text, 'active'::text])) AND ((e.end_date IS NULL) OR (e.end_date >= public.tw_today())));


--
-- Name: v_tuition_status; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_tuition_status AS
 SELECT id,
    student_id,
    title,
    amount,
    paid_amount,
    (amount - paid_amount) AS outstanding,
    pay_status,
    due_date
   FROM public.tuition_bills b
  WHERE (NOT is_void);


--
-- Name: v_unit_weakness; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_unit_weakness AS
 SELECT sc.student_id,
    sub.name AS subject,
    it.unit_tag,
    count(*) AS attempted,
    count(w.exam_item_id) AS wrong,
    round((((count(w.exam_item_id))::numeric * 100.0) / (count(*))::numeric), 1) AS wrong_rate
   FROM ((((public.exam_scores sc
     JOIN public.exams ex ON (((ex.id = sc.exam_id) AND ex.scores_published)))
     JOIN public.subjects sub ON ((sub.id = ex.subject_id)))
     JOIN public.exam_items it ON (((it.exam_id = ex.id) AND (it.unit_tag IS NOT NULL))))
     LEFT JOIN public.student_wrong_items w ON (((w.exam_item_id = it.id) AND (w.student_id = sc.student_id))))
  WHERE sc.items_recorded
  GROUP BY sc.student_id, sub.name, it.unit_tag;


--
-- Name: v_wallet_ledger; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_wallet_ledger AS
 SELECT id,
    student_id,
    created_at,
    txn_type,
    amount,
    meal_order_id,
    note,
    sum(amount) OVER (PARTITION BY student_id ORDER BY id) AS balance_after
   FROM public.wallet_transactions t;


--
-- Name: wallets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallets (
    student_id bigint NOT NULL,
    balance integer DEFAULT 0 NOT NULL,
    low_balance_alert integer DEFAULT 200 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE wallets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.wallets IS '每位學生的餐費餘額（快取，真正依據是帳本）';


--
-- Name: COLUMN wallets.student_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallets.student_id IS '學生';


--
-- Name: COLUMN wallets.balance; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallets.balance IS '餘額：由 wallet_transactions 自動維護，請勿手動修改';


--
-- Name: COLUMN wallets.low_balance_alert; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallets.low_balance_alert IS '提醒門檻：低於此金額提醒儲值';


--
-- Name: COLUMN wallets.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.wallets.updated_at IS '最後異動';


--
-- Name: v_wallet_status; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_wallet_status AS
 SELECT s.id AS student_id,
    s.name,
    s.family_id,
    w.balance,
    w.low_balance_alert,
    (w.balance < w.low_balance_alert) AS is_low
   FROM (public.students s
     JOIN public.wallets w ON ((w.student_id = s.id)))
  WHERE (s.status <> 'left'::text);


--
-- Name: v_wrong_book; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_wrong_book AS
 SELECT w.student_id,
    ex.exam_date,
    sub.name AS subject,
    ex.title AS exam_title,
    it.item_no,
    it.unit_tag,
    it.question_image_url,
    it.answer,
    it.explanation,
    w.student_answer,
    w.is_corrected
   FROM (((public.student_wrong_items w
     JOIN public.exam_items it ON ((it.id = w.exam_item_id)))
     JOIN public.exams ex ON (((ex.id = it.exam_id) AND ex.scores_published)))
     JOIN public.subjects sub ON ((sub.id = ex.subject_id)));


--
-- Name: vendors_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.vendors ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vendors_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: wallet_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.wallet_transactions ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.wallet_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: admin_audit admin_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit
    ADD CONSTRAINT admin_audit_pkey PRIMARY KEY (id);


--
-- Name: announcements announcements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcements
    ADD CONSTRAINT announcements_pkey PRIMARY KEY (id);


--
-- Name: announcements announcements_source_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcements
    ADD CONSTRAINT announcements_source_path_key UNIQUE (source_path);


--
-- Name: attendance attendance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance
    ADD CONSTRAINT attendance_pkey PRIMARY KEY (session_id, student_id);


--
-- Name: binding_codes binding_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binding_codes
    ADD CONSTRAINT binding_codes_pkey PRIMARY KEY (code);


--
-- Name: bot_settings bot_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_settings
    ADD CONSTRAINT bot_settings_pkey PRIMARY KEY (key);


--
-- Name: broadcasts broadcasts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_pkey PRIMARY KEY (id);


--
-- Name: broadcasts broadcasts_request_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_request_id_key UNIQUE (request_id);


--
-- Name: class_sessions class_sessions_course_id_session_date_start_time_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.class_sessions
    ADD CONSTRAINT class_sessions_course_id_session_date_start_time_key UNIQUE (course_id, session_date, start_time);


--
-- Name: class_sessions class_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.class_sessions
    ADD CONSTRAINT class_sessions_pkey PRIMARY KEY (id);


--
-- Name: content_files content_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_files
    ADD CONSTRAINT content_files_pkey PRIMARY KEY (path);


--
-- Name: course_schedules course_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.course_schedules
    ADD CONSTRAINT course_schedules_pkey PRIMARY KEY (id);


--
-- Name: course_staff course_staff_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.course_staff
    ADD CONSTRAINT course_staff_pkey PRIMARY KEY (course_id, staff_id, role);


--
-- Name: courses courses_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.courses
    ADD CONSTRAINT courses_external_ref_key UNIQUE (external_ref);


--
-- Name: courses courses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.courses
    ADD CONSTRAINT courses_pkey PRIMARY KEY (id);


--
-- Name: enrollments enrollments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_pkey PRIMARY KEY (id);


--
-- Name: exam_items exam_items_exam_id_item_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_items
    ADD CONSTRAINT exam_items_exam_id_item_no_key UNIQUE (exam_id, item_no);


--
-- Name: exam_items exam_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_items
    ADD CONSTRAINT exam_items_pkey PRIMARY KEY (id);


--
-- Name: exam_scores exam_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_scores
    ADD CONSTRAINT exam_scores_pkey PRIMARY KEY (exam_id, student_id);


--
-- Name: exams exams_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exams
    ADD CONSTRAINT exams_external_ref_key UNIQUE (external_ref);


--
-- Name: exams exams_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exams
    ADD CONSTRAINT exams_pkey PRIMARY KEY (id);


--
-- Name: families families_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.families
    ADD CONSTRAINT families_pkey PRIMARY KEY (id);


--
-- Name: guardians guardians_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardians
    ADD CONSTRAINT guardians_external_ref_key UNIQUE (external_ref);


--
-- Name: guardians guardians_line_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardians
    ADD CONSTRAINT guardians_line_user_id_key UNIQUE (line_user_id);


--
-- Name: guardians guardians_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardians
    ADD CONSTRAINT guardians_pkey PRIMARY KEY (id);


--
-- Name: honor_roll honor_roll_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.honor_roll
    ADD CONSTRAINT honor_roll_pkey PRIMARY KEY (id);


--
-- Name: honor_roll honor_roll_source_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.honor_roll
    ADD CONSTRAINT honor_roll_source_path_key UNIQUE (source_path);


--
-- Name: kb_articles kb_articles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_articles
    ADD CONSTRAINT kb_articles_pkey PRIMARY KEY (id);


--
-- Name: kb_articles kb_articles_source_path_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_articles
    ADD CONSTRAINT kb_articles_source_path_key UNIQUE (source_path);


--
-- Name: leave_requests leave_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_requests
    ADD CONSTRAINT leave_requests_pkey PRIMARY KEY (id);


--
-- Name: line_users line_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.line_users
    ADD CONSTRAINT line_users_pkey PRIMARY KEY (line_user_id);


--
-- Name: meal_menus meal_menus_meal_date_meal_slot_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_menus
    ADD CONSTRAINT meal_menus_meal_date_meal_slot_key UNIQUE (meal_date, meal_slot);


--
-- Name: meal_menus meal_menus_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_menus
    ADD CONSTRAINT meal_menus_pkey PRIMARY KEY (id);


--
-- Name: meal_options meal_options_id_meal_menu_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_options
    ADD CONSTRAINT meal_options_id_meal_menu_id_key UNIQUE (id, meal_menu_id);


--
-- Name: meal_options meal_options_meal_menu_id_option_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_options
    ADD CONSTRAINT meal_options_meal_menu_id_option_no_key UNIQUE (meal_menu_id, option_no);


--
-- Name: meal_options meal_options_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_options
    ADD CONSTRAINT meal_options_pkey PRIMARY KEY (id);


--
-- Name: meal_orders meal_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_orders
    ADD CONSTRAINT meal_orders_pkey PRIMARY KEY (id);


--
-- Name: media_files media_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_pkey PRIMARY KEY (id);


--
-- Name: media_files media_files_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_token_key UNIQUE (token);


--
-- Name: message_logs message_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_logs
    ADD CONSTRAINT message_logs_pkey PRIMARY KEY (id);


--
-- Name: message_logs message_logs_webhook_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_logs
    ADD CONSTRAINT message_logs_webhook_event_id_key UNIQUE (webhook_event_id);


--
-- Name: pending_actions pending_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_actions
    ADD CONSTRAINT pending_actions_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: staff_alerts staff_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff_alerts
    ADD CONSTRAINT staff_alerts_pkey PRIMARY KEY (id);


--
-- Name: staff staff_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff
    ADD CONSTRAINT staff_external_ref_key UNIQUE (external_ref);


--
-- Name: staff staff_line_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff
    ADD CONSTRAINT staff_line_user_id_key UNIQUE (line_user_id);


--
-- Name: staff staff_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff
    ADD CONSTRAINT staff_pkey PRIMARY KEY (id);


--
-- Name: student_notes student_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.student_notes
    ADD CONSTRAINT student_notes_pkey PRIMARY KEY (id);


--
-- Name: student_wrong_items student_wrong_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.student_wrong_items
    ADD CONSTRAINT student_wrong_items_pkey PRIMARY KEY (exam_item_id, student_id);


--
-- Name: students students_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.students
    ADD CONSTRAINT students_external_ref_key UNIQUE (external_ref);


--
-- Name: students students_line_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.students
    ADD CONSTRAINT students_line_user_id_key UNIQUE (line_user_id);


--
-- Name: students students_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.students
    ADD CONSTRAINT students_pkey PRIMARY KEY (id);


--
-- Name: students students_student_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.students
    ADD CONSTRAINT students_student_no_key UNIQUE (student_no);


--
-- Name: subjects subjects_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subjects
    ADD CONSTRAINT subjects_name_key UNIQUE (name);


--
-- Name: subjects subjects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subjects
    ADD CONSTRAINT subjects_pkey PRIMARY KEY (id);


--
-- Name: sync_runs sync_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sync_runs
    ADD CONSTRAINT sync_runs_pkey PRIMARY KEY (id);


--
-- Name: tuition_bills tuition_bills_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuition_bills
    ADD CONSTRAINT tuition_bills_external_ref_key UNIQUE (external_ref);


--
-- Name: tuition_bills tuition_bills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuition_bills
    ADD CONSTRAINT tuition_bills_pkey PRIMARY KEY (id);


--
-- Name: vendors vendors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendors
    ADD CONSTRAINT vendors_pkey PRIMARY KEY (id);


--
-- Name: wallet_transactions wallet_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_pkey PRIMARY KEY (id);


--
-- Name: wallets wallets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallets
    ADD CONSTRAINT wallets_pkey PRIMARY KEY (student_id);


--
-- Name: ix_admin_audit_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_admin_audit_time ON public.admin_audit USING btree (created_at DESC);


--
-- Name: ix_attendance_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_attendance_student ON public.attendance USING btree (student_id);


--
-- Name: ix_bills_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_bills_student ON public.tuition_bills USING btree (student_id);


--
-- Name: ix_binding_codes_guardian; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_binding_codes_guardian ON public.binding_codes USING btree (guardian_id) WHERE (used_at IS NULL);


--
-- Name: ix_broadcasts_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_broadcasts_time ON public.broadcasts USING btree (created_at DESC);


--
-- Name: ix_enrollments_course; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_enrollments_course ON public.enrollments USING btree (course_id);


--
-- Name: ix_enrollments_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_enrollments_student ON public.enrollments USING btree (student_id);


--
-- Name: ix_exams_course_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_exams_course_date ON public.exams USING btree (course_id, exam_date);


--
-- Name: ix_guardians_family; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_guardians_family ON public.guardians USING btree (family_id);


--
-- Name: ix_leave_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leave_session ON public.leave_requests USING btree (session_id);


--
-- Name: ix_leave_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_leave_student ON public.leave_requests USING btree (student_id);


--
-- Name: ix_meal_orders_menu; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_meal_orders_menu ON public.meal_orders USING btree (meal_menu_id);


--
-- Name: ix_meal_orders_option; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_meal_orders_option ON public.meal_orders USING btree (meal_option_id);


--
-- Name: ix_media_source_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_source_hash ON public.media_files USING btree (source_hash);


--
-- Name: ix_msg_needs_human; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_msg_needs_human ON public.message_logs USING btree (created_at) WHERE needs_human;


--
-- Name: ix_msg_reply_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_msg_reply_to ON public.message_logs USING btree (reply_to) WHERE (reply_to IS NOT NULL);


--
-- Name: ix_msg_user_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_msg_user_time ON public.message_logs USING btree (line_user_id, created_at DESC);


--
-- Name: ix_notes_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notes_student ON public.student_notes USING btree (student_id, note_date DESC);


--
-- Name: ix_pending_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pending_open ON public.pending_actions USING btree (line_user_id) WHERE ((confirmed_at IS NULL) AND (cancelled_at IS NULL));


--
-- Name: ix_scores_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_scores_student ON public.exam_scores USING btree (student_id);


--
-- Name: ix_sessions_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sessions_date ON public.class_sessions USING btree (session_date);


--
-- Name: ix_staff_alerts_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_staff_alerts_message ON public.staff_alerts USING btree (message_id) WHERE (message_id IS NOT NULL);


--
-- Name: ix_staff_alerts_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_staff_alerts_user ON public.staff_alerts USING btree (line_user_id, created_at DESC);


--
-- Name: ix_students_family; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_students_family ON public.students USING btree (family_id);


--
-- Name: ix_wallet_txn_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wallet_txn_student ON public.wallet_transactions USING btree (student_id, id);


--
-- Name: ix_wrong_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wrong_student ON public.student_wrong_items USING btree (student_id);


--
-- Name: uq_enrollment_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_enrollment_current ON public.enrollments USING btree (student_id, course_id) WHERE (status = ANY (ARRAY['trial'::text, 'active'::text]));


--
-- Name: uq_kb_title; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_kb_title ON public.kb_articles USING btree (title);


--
-- Name: uq_leave_current; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_leave_current ON public.leave_requests USING btree (student_id, session_id) WHERE (status = ANY (ARRAY['submitted'::text, 'approved'::text]));


--
-- Name: uq_meal_order_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_meal_order_active ON public.meal_orders USING btree (student_id, meal_menu_id) WHERE (status = 'active'::text);


--
-- Name: uq_wallet_txn_once_per_order; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_wallet_txn_once_per_order ON public.wallet_transactions USING btree (meal_order_id, txn_type) WHERE (meal_order_id IS NOT NULL);


--
-- Name: tuition_bills trg_bills_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_bills_updated BEFORE UPDATE ON public.tuition_bills FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: courses trg_courses_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_courses_updated BEFORE UPDATE ON public.courses FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: families trg_families_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_families_updated BEFORE UPDATE ON public.families FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: guardians trg_guardians_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_guardians_updated BEFORE UPDATE ON public.guardians FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: kb_articles trg_kb_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_kb_updated BEFORE UPDATE ON public.kb_articles FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: meal_orders trg_meal_order_price; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_meal_order_price BEFORE INSERT OR UPDATE OF meal_option_id ON public.meal_orders FOR EACH ROW EXECUTE FUNCTION public.meal_order_price();


--
-- Name: meal_orders trg_meal_orders_upd; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_meal_orders_upd BEFORE UPDATE ON public.meal_orders FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: exam_scores trg_score_range; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_score_range BEFORE INSERT OR UPDATE ON public.exam_scores FOR EACH ROW EXECUTE FUNCTION public.check_score_range();


--
-- Name: staff trg_staff_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_staff_updated BEFORE UPDATE ON public.staff FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: students trg_students_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_students_updated BEFORE UPDATE ON public.students FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: wallet_transactions trg_wallet_apply; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_wallet_apply AFTER INSERT ON public.wallet_transactions FOR EACH ROW EXECUTE FUNCTION public.apply_wallet_txn();


--
-- Name: wallet_transactions trg_wallet_immutable; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_wallet_immutable BEFORE DELETE OR UPDATE ON public.wallet_transactions FOR EACH ROW EXECUTE FUNCTION public.forbid_ledger_change();


--
-- Name: announcements announcements_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcements
    ADD CONSTRAINT announcements_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);


--
-- Name: announcements announcements_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcements
    ADD CONSTRAINT announcements_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.staff(id);


--
-- Name: announcements announcements_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.announcements
    ADD CONSTRAINT announcements_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.media_files(id) ON DELETE SET NULL;


--
-- Name: attendance attendance_leave_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance
    ADD CONSTRAINT attendance_leave_request_id_fkey FOREIGN KEY (leave_request_id) REFERENCES public.leave_requests(id);


--
-- Name: attendance attendance_recorded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance
    ADD CONSTRAINT attendance_recorded_by_fkey FOREIGN KEY (recorded_by) REFERENCES public.staff(id);


--
-- Name: attendance attendance_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance
    ADD CONSTRAINT attendance_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.class_sessions(id);


--
-- Name: attendance attendance_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance
    ADD CONSTRAINT attendance_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: binding_codes binding_codes_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binding_codes
    ADD CONSTRAINT binding_codes_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.staff(id);


--
-- Name: binding_codes binding_codes_guardian_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binding_codes
    ADD CONSTRAINT binding_codes_guardian_id_fkey FOREIGN KEY (guardian_id) REFERENCES public.guardians(id);


--
-- Name: binding_codes binding_codes_staff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binding_codes
    ADD CONSTRAINT binding_codes_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES public.staff(id);


--
-- Name: binding_codes binding_codes_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binding_codes
    ADD CONSTRAINT binding_codes_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: binding_codes binding_codes_used_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.binding_codes
    ADD CONSTRAINT binding_codes_used_by_fkey FOREIGN KEY (used_by) REFERENCES public.line_users(line_user_id);


--
-- Name: broadcasts broadcasts_announcement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_announcement_id_fkey FOREIGN KEY (announcement_id) REFERENCES public.announcements(id) ON DELETE SET NULL;


--
-- Name: broadcasts broadcasts_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id) ON DELETE SET NULL;


--
-- Name: broadcasts broadcasts_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.media_files(id) ON DELETE SET NULL;


--
-- Name: class_sessions class_sessions_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.class_sessions
    ADD CONSTRAINT class_sessions_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);


--
-- Name: course_schedules course_schedules_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.course_schedules
    ADD CONSTRAINT course_schedules_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id) ON DELETE CASCADE;


--
-- Name: course_staff course_staff_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.course_staff
    ADD CONSTRAINT course_staff_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id) ON DELETE CASCADE;


--
-- Name: course_staff course_staff_staff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.course_staff
    ADD CONSTRAINT course_staff_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES public.staff(id);


--
-- Name: courses courses_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.courses
    ADD CONSTRAINT courses_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id);


--
-- Name: enrollments enrollments_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);


--
-- Name: enrollments enrollments_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: exam_items exam_items_exam_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_items
    ADD CONSTRAINT exam_items_exam_id_fkey FOREIGN KEY (exam_id) REFERENCES public.exams(id) ON DELETE CASCADE;


--
-- Name: exam_scores exam_scores_entered_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_scores
    ADD CONSTRAINT exam_scores_entered_by_fkey FOREIGN KEY (entered_by) REFERENCES public.staff(id);


--
-- Name: exam_scores exam_scores_exam_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_scores
    ADD CONSTRAINT exam_scores_exam_id_fkey FOREIGN KEY (exam_id) REFERENCES public.exams(id) ON DELETE CASCADE;


--
-- Name: exam_scores exam_scores_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exam_scores
    ADD CONSTRAINT exam_scores_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: exams exams_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exams
    ADD CONSTRAINT exams_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);


--
-- Name: exams exams_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exams
    ADD CONSTRAINT exams_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.staff(id);


--
-- Name: exams exams_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exams
    ADD CONSTRAINT exams_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.class_sessions(id);


--
-- Name: exams exams_subject_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exams
    ADD CONSTRAINT exams_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES public.subjects(id);


--
-- Name: guardians guardians_family_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardians
    ADD CONSTRAINT guardians_family_id_fkey FOREIGN KEY (family_id) REFERENCES public.families(id);


--
-- Name: guardians guardians_line_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guardians
    ADD CONSTRAINT guardians_line_user_id_fkey FOREIGN KEY (line_user_id) REFERENCES public.line_users(line_user_id);


--
-- Name: honor_roll honor_roll_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.honor_roll
    ADD CONSTRAINT honor_roll_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: kb_articles kb_articles_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_articles
    ADD CONSTRAINT kb_articles_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.media_files(id) ON DELETE SET NULL;


--
-- Name: kb_articles kb_articles_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kb_articles
    ADD CONSTRAINT kb_articles_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.staff(id);


--
-- Name: leave_requests leave_requests_requested_by_guardian_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_requests
    ADD CONSTRAINT leave_requests_requested_by_guardian_fkey FOREIGN KEY (requested_by_guardian) REFERENCES public.guardians(id);


--
-- Name: leave_requests leave_requests_requested_by_staff_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_requests
    ADD CONSTRAINT leave_requests_requested_by_staff_fkey FOREIGN KEY (requested_by_staff) REFERENCES public.staff(id);


--
-- Name: leave_requests leave_requests_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_requests
    ADD CONSTRAINT leave_requests_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES public.staff(id);


--
-- Name: leave_requests leave_requests_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_requests
    ADD CONSTRAINT leave_requests_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.class_sessions(id);


--
-- Name: leave_requests leave_requests_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leave_requests
    ADD CONSTRAINT leave_requests_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: meal_options meal_options_meal_menu_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_options
    ADD CONSTRAINT meal_options_meal_menu_id_fkey FOREIGN KEY (meal_menu_id) REFERENCES public.meal_menus(id) ON DELETE CASCADE;


--
-- Name: meal_options meal_options_vendor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_options
    ADD CONSTRAINT meal_options_vendor_id_fkey FOREIGN KEY (vendor_id) REFERENCES public.vendors(id);


--
-- Name: meal_orders meal_orders_entered_by_staff_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_orders
    ADD CONSTRAINT meal_orders_entered_by_staff_fkey FOREIGN KEY (entered_by_staff) REFERENCES public.staff(id);


--
-- Name: meal_orders meal_orders_meal_menu_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_orders
    ADD CONSTRAINT meal_orders_meal_menu_id_fkey FOREIGN KEY (meal_menu_id) REFERENCES public.meal_menus(id);


--
-- Name: meal_orders meal_orders_meal_option_id_meal_menu_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_orders
    ADD CONSTRAINT meal_orders_meal_option_id_meal_menu_id_fkey FOREIGN KEY (meal_option_id, meal_menu_id) REFERENCES public.meal_options(id, meal_menu_id);


--
-- Name: meal_orders meal_orders_ordered_by_guardian_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_orders
    ADD CONSTRAINT meal_orders_ordered_by_guardian_fkey FOREIGN KEY (ordered_by_guardian) REFERENCES public.guardians(id);


--
-- Name: meal_orders meal_orders_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meal_orders
    ADD CONSTRAINT meal_orders_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: message_logs message_logs_line_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_logs
    ADD CONSTRAINT message_logs_line_user_id_fkey FOREIGN KEY (line_user_id) REFERENCES public.line_users(line_user_id);


--
-- Name: message_logs message_logs_reply_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_logs
    ADD CONSTRAINT message_logs_reply_to_fkey FOREIGN KEY (reply_to) REFERENCES public.message_logs(id) ON DELETE SET NULL;


--
-- Name: pending_actions pending_actions_line_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_actions
    ADD CONSTRAINT pending_actions_line_user_id_fkey FOREIGN KEY (line_user_id) REFERENCES public.line_users(line_user_id);


--
-- Name: staff_alerts staff_alerts_line_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff_alerts
    ADD CONSTRAINT staff_alerts_line_user_id_fkey FOREIGN KEY (line_user_id) REFERENCES public.line_users(line_user_id) ON DELETE CASCADE;


--
-- Name: staff_alerts staff_alerts_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff_alerts
    ADD CONSTRAINT staff_alerts_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.message_logs(id) ON DELETE SET NULL;


--
-- Name: staff staff_line_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.staff
    ADD CONSTRAINT staff_line_user_id_fkey FOREIGN KEY (line_user_id) REFERENCES public.line_users(line_user_id);


--
-- Name: student_notes student_notes_staff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.student_notes
    ADD CONSTRAINT student_notes_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES public.staff(id);


--
-- Name: student_notes student_notes_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.student_notes
    ADD CONSTRAINT student_notes_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: student_wrong_items student_wrong_items_exam_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.student_wrong_items
    ADD CONSTRAINT student_wrong_items_exam_item_id_fkey FOREIGN KEY (exam_item_id) REFERENCES public.exam_items(id) ON DELETE CASCADE;


--
-- Name: student_wrong_items student_wrong_items_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.student_wrong_items
    ADD CONSTRAINT student_wrong_items_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: students students_family_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.students
    ADD CONSTRAINT students_family_id_fkey FOREIGN KEY (family_id) REFERENCES public.families(id);


--
-- Name: students students_line_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.students
    ADD CONSTRAINT students_line_user_id_fkey FOREIGN KEY (line_user_id) REFERENCES public.line_users(line_user_id);


--
-- Name: tuition_bills tuition_bills_course_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuition_bills
    ADD CONSTRAINT tuition_bills_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id);


--
-- Name: tuition_bills tuition_bills_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuition_bills
    ADD CONSTRAINT tuition_bills_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: wallet_transactions wallet_transactions_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.staff(id);


--
-- Name: wallet_transactions wallet_transactions_meal_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_meal_order_id_fkey FOREIGN KEY (meal_order_id) REFERENCES public.meal_orders(id);


--
-- Name: wallet_transactions wallet_transactions_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- Name: wallets wallets_student_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallets
    ADD CONSTRAINT wallets_student_id_fkey FOREIGN KEY (student_id) REFERENCES public.students(id);


--
-- PostgreSQL database dump complete
--

\unrestrict uDy4cDHU7QvwTyCXo8WvEgN6IhqPawl2Qq3hxwsUdX3FlQW7Q6b4xoHjBTkHngq

