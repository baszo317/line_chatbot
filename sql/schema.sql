-- =====================================================================
-- 高明補習班 LINE AI 助理 — 資料庫結構 v0.6   (2026-10-19)
-- v0.2：line_users 加 bot_paused_until（轉真人時暫停 AI）、kb_articles 標題不可重複
-- v0.3：年級只收「國小（六年級）＝6、國一～國三＝7～9」；公告與常見問題可以附圖（media_files）；
--       公告多一個「行事曆」分類；「補習班資料」資料夾同步（content_files、各表的 source_path）；新增後台異動紀錄 admin_audit、升級紀錄 schema_migrations；
--       欄位說明改用 COMMENT ON COLUMN（docs/資料庫結構.md、DBeaver、Supabase 都看得到）
--       舊資料庫不用重建：bot 啟動時會自動執行 sql/003_v03_grades_images_admin.sql
-- v0.4：公告、行事曆可以指定適用年級 announcements.grades（sql/004_notice_grades.sql）
-- v0.5：後台「設定」頁 bot_settings（歡迎詞開關）、message_logs.reply_to（sql/005_bot_settings.sql）
-- v0.6：廣播 broadcasts、需要真人時通知員工 staff.notify_human／staff_alerts（sql/006_broadcast_staff_alerts.sql）
-- 資料庫：PostgreSQL 14 以上（自架或 Supabase 等託管服務都可直接執行）
--
-- 模組
--   B. LINE 帳號（先建，人員表會參照）
--   A. 家庭／家長／學生／教職員、綁定碼
--   C. 科目、課程（班級）、每週課表、每一堂課、選課
--   D. 請假、出缺勤
--   E. 考試成績、逐題錯題、導師紀錄
--   F. 訂餐、餐費錢包（帳本）
--   G. 學費
--   H. 圖片、AI 知識庫、公告（含行事曆）、榜單
--   I. 對話紀錄、待確認動作、UPAD12 同步紀錄、後台異動紀錄、升級紀錄、機器人設定
--   最後：函式、觸發器、檢視表（AI 查詢用）、索引、預設科目、欄位說明
--
-- 慣例
--   * 金額一律 INTEGER（新台幣元），不用浮點數。
--   * 狀態欄用 TEXT + CHECK；要新增狀態時改 CHECK 即可。
--   * 「今天」一律用 tw_today()（台灣時間）。雲端資料庫預設時區常是 UTC，
--     直接用 CURRENT_DATE 在台灣凌晨 0~8 點會差一天。
--   * 給 AI 讀的資料一律走 v_ 開頭的檢視表，內部備註等欄位不會出現在裡面。
--   * 年級：6 = 國小（國小部都是六年級，不再細分）、7 = 國一、8 = 國二、9 = 國三。
--     學生存的是「入學那一學年」的年級，每年 8/1 由 v_students 自動升一級（國小 → 國一 …），
--     國三之後顯示「已畢業」。
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 共用函式
-- ---------------------------------------------------------------------

-- 台灣時間的今天
CREATE FUNCTION tw_today() RETURNS DATE
LANGUAGE sql STABLE AS $$
  SELECT (now() AT TIME ZONE 'Asia/Taipei')::date
$$;

-- 某天屬於哪個學年度（民國年，8/1 起算新學年）。例：2026-10-17 → 115
CREATE FUNCTION school_year_of(d DATE) RETURNS SMALLINT
LANGUAGE sql IMMUTABLE AS $$
  SELECT (EXTRACT(YEAR FROM d)::int - 1911
          - CASE WHEN EXTRACT(MONTH FROM d) >= 8 THEN 0 ELSE 1 END)::smallint
$$;

-- 年級數字 → 中文。本班只有國小部（都是六年級）和國中部：6 → 國小、8 → 國二、10 以上 → 已畢業
CREATE FUNCTION grade_label(g INT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN g = 6              THEN '國小'
    WHEN g BETWEEN 7 AND 9  THEN '國' || (ARRAY['一','二','三'])[g - 6]
    WHEN g > 9              THEN '已畢業'
  END
$$;

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;


-- =====================================================================
-- B. LINE 帳號
-- =====================================================================
CREATE TABLE line_users (
  line_user_id    TEXT PRIMARY KEY CHECK (line_user_id ~ '^U[0-9a-f]{32}$'),
  display_name    TEXT,
  picture_url     TEXT,
  is_following    BOOLEAN NOT NULL DEFAULT true,     -- 收到 unfollow（封鎖）事件時改 false
  menu_role       TEXT NOT NULL DEFAULT 'guest'
                  CHECK (menu_role IN ('guest','guardian','student','staff')),
                  -- 最後一次幫他連結的圖文選單；真正身分以 v_line_identity 為準
  followed_at     TIMESTAMPTZ,
  unfollowed_at   TIMESTAMPTZ,
  last_active_at  TIMESTAMPTZ,
  bot_paused_until TIMESTAMPTZ,                      -- 家長按「找真人客服」後，AI 暫停自動回覆到這個時間
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =====================================================================
-- A. 家庭／家長／學生／教職員
-- =====================================================================
CREATE TABLE families (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_name  TEXT NOT NULL,                  -- 例：王小明家
  phone        TEXT,                           -- 家中主要電話
  address      TEXT,
  note         TEXT,                           -- 內部備註
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE staff (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('admin','tutor','teacher','counter')),
                -- admin 管理者／tutor 導師／teacher 授課老師／counter 櫃台
  phone         TEXT,
  line_user_id  TEXT UNIQUE REFERENCES line_users(line_user_id),
  is_active     BOOLEAN NOT NULL DEFAULT true,
  external_ref  TEXT UNIQUE,                   -- UPAD12 的對應代號（有的話）
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  notify_human  BOOLEAN NOT NULL DEFAULT false  -- 家長需要真人時，用 LINE 通知這位員工（會用掉訊息額度）
);

CREATE TABLE guardians (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_id     BIGINT NOT NULL REFERENCES families(id),
  name          TEXT NOT NULL,
  relationship  TEXT NOT NULL DEFAULT '家長',   -- 父／母／祖父母／其他
  phone         TEXT,
  email         TEXT,
  line_user_id  TEXT UNIQUE REFERENCES line_users(line_user_id),
  is_primary    BOOLEAN NOT NULL DEFAULT false, -- 主要聯絡人（繳費、緊急聯絡）
  is_active     BOOLEAN NOT NULL DEFAULT true,
  external_ref  TEXT UNIQUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE students (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_no         TEXT NOT NULL UNIQUE,      -- 學號（建議直接沿用 UPAD12 學號）
  family_id          BIGINT NOT NULL REFERENCES families(id),
  name               TEXT NOT NULL,
  school_name        TEXT,                       -- 就讀學校
  grade              SMALLINT NOT NULL CONSTRAINT students_grade_check CHECK (grade BETWEEN 6 AND 9),
                     -- 6 國小、7~9 國一~國三；是「grade_school_year 那一學年」的年級
  grade_school_year  SMALLINT NOT NULL,          -- 上面年級所屬學年度，例：115。現在年級由 v_students 自動推算
  phone              TEXT,                       -- 學生本人手機（選填）
  line_user_id       TEXT UNIQUE REFERENCES line_users(line_user_id),  -- 預留：學生本人也綁 LINE
  status             TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','paused','left')),     -- 在學／暫停／離班
  joined_on          DATE,
  left_on            DATE,
  note               TEXT,                       -- 內部備註（不會出現在 AI 用的檢視表）
  external_ref       TEXT UNIQUE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE binding_codes (
  code          TEXT PRIMARY KEY,                -- 8 碼大寫英數，用 issue_binding_code() 產生
  guardian_id   BIGINT REFERENCES guardians(id),
  student_id    BIGINT REFERENCES students(id),
  staff_id      BIGINT REFERENCES staff(id),
  expires_at    TIMESTAMPTZ NOT NULL,
  used_at       TIMESTAMPTZ,
  used_by       TEXT REFERENCES line_users(line_user_id),
  created_by    BIGINT REFERENCES staff(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(guardian_id, student_id, staff_id) = 1)   -- 一張碼只對應一個人
);


-- =====================================================================
-- C. 科目、課程、課表
-- =====================================================================
CREATE TABLE subjects (
  id    SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE
);

CREATE TABLE courses (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name          TEXT NOT NULL,                   -- 例：國二數學 A 班
  subject_id    SMALLINT NOT NULL REFERENCES subjects(id),
  grade         SMALLINT CONSTRAINT courses_grade_check CHECK (grade BETWEEN 6 AND 9),
                -- 6 國小、7~9 國一~國三；跨年級可留空
  start_date    DATE NOT NULL,
  end_date      DATE,
  fee           INTEGER CHECK (fee >= 0),        -- 對外公開的學費參考（實際帳單在 tuition_bills）
  is_public     BOOLEAN NOT NULL DEFAULT true,   -- 未綁定的訪客也查得到（招生用）
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','archived')),
  description   TEXT,
  external_ref  TEXT UNIQUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (end_date IS NULL OR end_date >= start_date)
);

CREATE TABLE course_staff (
  course_id  BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  staff_id   BIGINT NOT NULL REFERENCES staff(id),
  role       TEXT NOT NULL CHECK (role IN ('teacher','tutor')),   -- 授課老師／導師
  PRIMARY KEY (course_id, staff_id, role)
);

CREATE TABLE course_schedules (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id   BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  weekday     SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 7),  -- 1=週一 … 7=週日
  start_time  TIME NOT NULL,
  end_time    TIME NOT NULL,
  room        TEXT,
  CHECK (end_time > start_time)
);

CREATE TABLE class_sessions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id     BIGINT NOT NULL REFERENCES courses(id),
  session_date  DATE NOT NULL,
  start_time    TIME NOT NULL,
  end_time      TIME NOT NULL,
  room          TEXT,
  status        TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled','cancelled','makeup')),  -- 正常／停課／補課
  note          TEXT,                            -- 例：颱風停課、調到週六
  UNIQUE (course_id, session_date, start_time),
  CHECK (end_time > start_time)
);

CREATE TABLE enrollments (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id  BIGINT NOT NULL REFERENCES students(id),
  course_id   BIGINT NOT NULL REFERENCES courses(id),
  start_date  DATE NOT NULL DEFAULT tw_today(),
  end_date    DATE,                              -- 退班／結業日；NULL = 仍在班上
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('trial','active','dropped','finished')),  -- 試聽／在班／退班／結業
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =====================================================================
-- D. 請假、出缺勤
-- =====================================================================
CREATE TABLE leave_requests (
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id             BIGINT NOT NULL REFERENCES students(id),
  session_id             BIGINT NOT NULL REFERENCES class_sessions(id),  -- 一堂課一筆；請整天就多筆
  leave_type             TEXT NOT NULL DEFAULT 'personal'
                         CHECK (leave_type IN ('sick','personal','school','other')),  -- 病假／事假／學校活動／其他
  reason                 TEXT,
  status                 TEXT NOT NULL DEFAULT 'submitted'
                         CHECK (status IN ('submitted','approved','rejected','cancelled')),
  need_makeup            BOOLEAN NOT NULL DEFAULT false,   -- 需要補課／補看影片
  source                 TEXT NOT NULL DEFAULT 'line'
                         CHECK (source IN ('line','phone','counter','upad12')),
  requested_by_guardian  BIGINT REFERENCES guardians(id),
  requested_by_staff     BIGINT REFERENCES staff(id),
  reviewed_by            BIGINT REFERENCES staff(id),
  reviewed_at            TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE attendance (
  session_id        BIGINT NOT NULL REFERENCES class_sessions(id),
  student_id        BIGINT NOT NULL REFERENCES students(id),
  status            TEXT NOT NULL CHECK (status IN ('present','late','absent','leave')),
  check_in_at       TIMESTAMPTZ,
  check_out_at      TIMESTAMPTZ,
  leave_request_id  BIGINT REFERENCES leave_requests(id),
  source            TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','upad12')),
  recorded_by       BIGINT REFERENCES staff(id),
  PRIMARY KEY (session_id, student_id)
);


-- =====================================================================
-- E. 考試成績、錯題、導師紀錄
-- =====================================================================
CREATE TABLE exams (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  course_id     BIGINT REFERENCES courses(id),         -- 哪一班考的；全年級模擬考可留空
  session_id    BIGINT REFERENCES class_sessions(id),  -- 哪一堂課考的（選填）
  subject_id    SMALLINT NOT NULL REFERENCES subjects(id),
  title         TEXT NOT NULL,                         -- 例：第 5 週小考：二次函數
  exam_type     TEXT NOT NULL DEFAULT 'quiz'
                CHECK (exam_type IN ('quiz','weekly','mock','school','other')),  -- 小考／週考／模擬考／學校段考／其他
  exam_date     DATE NOT NULL,
  full_score    NUMERIC(6,2) NOT NULL DEFAULT 100 CHECK (full_score > 0),
  scope         TEXT,                                  -- 考試範圍（家長問「下次考什麼」用）
  paper_url     TEXT,                                  -- 考卷檔案
  scores_published     BOOLEAN NOT NULL DEFAULT false,  -- 老師按「發布成績」後家長才查得到分數（範圍隨時可查）
  scores_published_at  TIMESTAMPTZ,
  created_by    BIGINT REFERENCES staff(id),
  external_ref  TEXT UNIQUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE exam_scores (
  exam_id          BIGINT NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
  student_id       BIGINT NOT NULL REFERENCES students(id),
  score            NUMERIC(6,2),                       -- 缺考時為 NULL
  status           TEXT NOT NULL DEFAULT 'normal'
                   CHECK (status IN ('normal','absent','makeup')),   -- 正常／缺考／補考
  items_recorded   BOOLEAN NOT NULL DEFAULT false,     -- 已登錄逐題對錯（分辨「全對」與「沒登錄」）
  teacher_comment  TEXT,                               -- 給家長看的評語
  entered_by       BIGINT REFERENCES staff(id),
  entered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (exam_id, student_id),
  CHECK ((status = 'absent') = (score IS NULL))
);

CREATE TABLE exam_items (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  exam_id             BIGINT NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
  item_no             SMALLINT NOT NULL,              -- 題號
  points              NUMERIC(5,2),
  unit_tag            TEXT,                            -- 單元／觀念，例：二次函數-配方法（弱點分析用）
  question_image_url  TEXT,                            -- 題目截圖
  answer              TEXT,
  explanation         TEXT,                            -- 詳解（文字或圖片網址）
  question_bank_ref   TEXT,                            -- 若從 UPAD12 題庫出題，記題目代號
  UNIQUE (exam_id, item_no)
);

CREATE TABLE student_wrong_items (
  exam_item_id    BIGINT NOT NULL REFERENCES exam_items(id) ON DELETE CASCADE,
  student_id      BIGINT NOT NULL REFERENCES students(id),
  student_answer  TEXT,
  is_corrected    BOOLEAN NOT NULL DEFAULT false,      -- 學生已訂正
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (exam_item_id, student_id)
);

CREATE TABLE student_notes (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id  BIGINT NOT NULL REFERENCES students(id),
  staff_id    BIGINT NOT NULL REFERENCES staff(id),
  note_date   DATE NOT NULL DEFAULT tw_today(),
  content     TEXT NOT NULL,
  visibility  TEXT NOT NULL DEFAULT 'internal'
              CHECK (visibility IN ('internal','guardian')),    -- 內部／家長可見
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =====================================================================
-- F. 訂餐、餐費錢包
-- =====================================================================
CREATE TABLE vendors (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name       TEXT NOT NULL,
  phone      TEXT,
  note       TEXT,
  is_active  BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE meal_menus (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  meal_date       DATE NOT NULL,
  meal_slot       TEXT NOT NULL DEFAULT 'dinner' CHECK (meal_slot IN ('lunch','dinner')),
  order_deadline  TIMESTAMPTZ NOT NULL,             -- 訂餐／改餐截止時間
  status          TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','open','closed','delivered')),  -- 草稿／開放訂餐／截止／已送達
  note            TEXT,
  UNIQUE (meal_date, meal_slot)
);

CREATE TABLE meal_options (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  meal_menu_id  BIGINT NOT NULL REFERENCES meal_menus(id) ON DELETE CASCADE,
  option_no     SMALLINT NOT NULL CHECK (option_no BETWEEN 1 AND 9),  -- 目前是 1、2、3 號餐
  vendor_id     BIGINT NOT NULL REFERENCES vendors(id),                -- 每個選項可來自不同店家
  item_name     TEXT NOT NULL,
  price         INTEGER NOT NULL CHECK (price >= 0),
  UNIQUE (meal_menu_id, option_no),
  UNIQUE (id, meal_menu_id)        -- 讓 meal_orders 能檢查「選的餐屬於同一餐單」
);

CREATE TABLE meal_orders (
  id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id           BIGINT NOT NULL REFERENCES students(id),
  meal_menu_id         BIGINT NOT NULL REFERENCES meal_menus(id),
  meal_option_id       BIGINT,                      -- NULL = 明確勾選「不訂」
  unit_price           INTEGER NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
                       -- 下單當下的價格（觸發器自動從 meal_options 帶入，之後改菜單價格不影響已確認的訂單）
  status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
  source               TEXT NOT NULL DEFAULT 'paper' CHECK (source IN ('paper','line','staff')),
  ordered_by_guardian  BIGINT REFERENCES guardians(id),
  entered_by_staff     BIGINT REFERENCES staff(id),
  confirmed_at         TIMESTAMPTZ,                 -- 家長在 LINE 按下「確認金額」的時間
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (meal_option_id, meal_menu_id) REFERENCES meal_options (id, meal_menu_id)
);

CREATE TABLE wallets (
  student_id         BIGINT PRIMARY KEY REFERENCES students(id),
  balance            INTEGER NOT NULL DEFAULT 0,    -- 由 wallet_transactions 自動維護，請勿手動修改
  low_balance_alert  INTEGER NOT NULL DEFAULT 200,  -- 低於此金額提醒儲值
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
  -- 若補習班規定「餐費不能欠」，加上 CHECK (balance >= 0)，扣款函式會自動把不足的人列出來
);

CREATE TABLE wallet_transactions (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id     BIGINT NOT NULL REFERENCES students(id),
  txn_type       TEXT NOT NULL
                 CHECK (txn_type IN ('topup','meal_charge','meal_refund','adjust')),  -- 儲值／訂餐扣款／取消退回／人工調整
  amount         INTEGER NOT NULL CHECK (amount <> 0),   -- 正數 = 加錢，負數 = 扣錢
  meal_order_id  BIGINT REFERENCES meal_orders(id),
  note           TEXT,
  created_by     BIGINT REFERENCES staff(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ( (txn_type IN ('topup','meal_refund') AND amount > 0)
       OR (txn_type = 'meal_charge' AND amount < 0)
       OR  txn_type = 'adjust' ),
  CHECK (txn_type NOT IN ('meal_charge','meal_refund') OR meal_order_id IS NOT NULL)
);


-- =====================================================================
-- G. 學費（若帳單以 UPAD12 為準，這張表改成同步副本）
-- =====================================================================
CREATE TABLE tuition_bills (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_id    BIGINT NOT NULL REFERENCES students(id),
  course_id     BIGINT REFERENCES courses(id),
  title         TEXT NOT NULL,                      -- 例：115 上 國二數學 A 班 第 1 期
  amount        INTEGER NOT NULL CHECK (amount >= 0),
  paid_amount   INTEGER NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
  pay_status    TEXT GENERATED ALWAYS AS (
                  CASE WHEN paid_amount >= amount THEN 'paid'
                       WHEN paid_amount > 0       THEN 'partial'
                       ELSE 'unpaid' END) STORED,   -- 自動計算，不會和金額不一致
  due_date      DATE,
  paid_at       TIMESTAMPTZ,
  pay_method    TEXT,                               -- 現金／轉帳／…
  is_void       BOOLEAN NOT NULL DEFAULT false,     -- 作廢
  note          TEXT,
  external_ref  TEXT UNIQUE,                        -- UPAD12 帳單編號
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =====================================================================
-- H. 圖片、AI 知識庫、公告、榜單（第一期「家長自動回覆」主要靠這區）
-- =====================================================================
CREATE TABLE media_files (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  token          TEXT NOT NULL UNIQUE DEFAULT replace(gen_random_uuid()::text, '-', ''),
                 -- 圖片網址用的隨機代號（/files/代號.jpg），別人猜不到其他圖片的網址
  original_name  TEXT,                              -- 上傳時的檔名
  content_type   TEXT NOT NULL DEFAULT 'image/jpeg',
  width          INTEGER,
  height         INTEGER,
  bytes          INTEGER NOT NULL,                  -- 原圖大小（位元組）
  data           BYTEA NOT NULL,                    -- 原圖：已轉正、壓成 JPEG、最長邊 4096（LINE 上限 10MB）
  preview        BYTEA NOT NULL,                    -- 預覽圖：最長邊 1024（LINE 規定 1MB 以內）
  note           TEXT,                              -- 用途說明，例：115 年 11 月行事曆
  uploaded_by    TEXT,                              -- 上傳的人（後台登入名稱）；從資料夾來的是 folder
  source_hash    TEXT,                              -- 原始檔案的 SHA-256（同一張圖不重複存）
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE content_files (
  path           TEXT PRIMARY KEY,                  -- 「補習班資料」資料夾裡的相對路徑，例：行事曆/11月行事曆.jpg
  sha256         TEXT NOT NULL,                     -- 檔案內容的指紋，內容一改就不同
  bytes          INTEGER NOT NULL,
  mtime_ns       BIGINT NOT NULL,                   -- 檔案修改時間（只用來判斷要不要重算指紋）
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                 -- 第一次看到這個檔案（或內容改變）的時間；「最新丟進來的行事曆」以這個為準
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ai_draft_at    TIMESTAMPTZ                        -- 已經幫這張圖產生過 AI 草稿（不重複呼叫 AI）
);

CREATE TABLE kb_articles (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  category    TEXT NOT NULL
              CHECK (category IN ('contact','traffic','course','fee','policy','enrollment','other')),
              -- 聯絡方式／交通停車／課程介紹／收費／規定（請假、退費）／招生試聽／其他
  title       TEXT NOT NULL,                        -- 例：怎麼到補習班？
  content     TEXT NOT NULL,                        -- 標準答案，AI 依此回覆（可放地圖、網址）
  image_id    BIGINT REFERENCES media_files(id) ON DELETE SET NULL,  -- 附圖（例：交通地圖、收費表），回覆時一起傳給家長
  keywords    TEXT[] NOT NULL DEFAULT '{}',
  audience    TEXT NOT NULL DEFAULT 'public' CHECK (audience IN ('public','guardian','staff')),
  sort_order  INTEGER NOT NULL DEFAULT 0,
  is_active   BOOLEAN NOT NULL DEFAULT true,
  source_path TEXT UNIQUE,                          -- 來自「補習班資料」的哪個檔案（不含副檔名）；空白 = 用其他方式建立
  updated_by  BIGINT REFERENCES staff(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE announcements (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,                        -- 內容；有附圖時，這裡放圖片的文字版（AI 靠這段回答）
  image_id    BIGINT REFERENCES media_files(id) ON DELETE SET NULL,  -- 附圖（例：本月行事曆）
  category    TEXT NOT NULL DEFAULT 'general'
              CONSTRAINT announcements_category_check
              CHECK (category IN ('general','closure','calendar','exam','event','enrollment')),
              -- 一般／停課(颱風)／行事曆／考試／活動／招生
  audience    TEXT NOT NULL DEFAULT 'all'
              CHECK (audience IN ('all','guardian','staff','course','grade')),
  course_id   BIGINT REFERENCES courses(id),        -- audience = course 時填
  grade       SMALLINT CONSTRAINT announcements_grade_check CHECK (grade BETWEEN 6 AND 9),
              -- audience = grade 時填：6 國小、7~9 國一~國三
  grades      SMALLINT[] CONSTRAINT announcements_grades_check CHECK (grades <@ ARRAY[6,7,8,9]::smallint[]),
              -- 適用年級，可以多個；空白 = 全部年級（「補習班資料」用年級資料夾設定）
  starts_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ends_at     TIMESTAMPTZ,
  is_pinned   BOOLEAN NOT NULL DEFAULT false,
  source_path TEXT UNIQUE,                          -- 來自「補習班資料」的哪個檔案（不含副檔名）；空白 = 用其他方式建立
  pushed_at   TIMESTAMPTZ,                          -- 已主動推播的時間（推播會用掉訊息額度）
  created_by  BIGINT REFERENCES staff(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (audience <> 'course' OR course_id IS NOT NULL),
  CHECK (audience <> 'grade'  OR grade IS NOT NULL)
);

CREATE TABLE honor_roll (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  school_year   SMALLINT NOT NULL,                  -- 學年度
  category      TEXT NOT NULL,                      -- 會考／私中入學／段考／競賽
  student_id    BIGINT REFERENCES students(id),
  display_name  TEXT NOT NULL,                      -- 對外顯示，例：王○明
  school_name   TEXT,                               -- 原就讀學校
  achievement   TEXT NOT NULL,                      -- 例：會考 5A++、錄取第一志願
  consent       BOOLEAN NOT NULL DEFAULT false,     -- 家長同意公開
  is_published  BOOLEAN NOT NULL DEFAULT false,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  source_path   TEXT UNIQUE,                        -- 來自「補習班資料／榜單／榜單.csv」的第幾列；空白 = 用其他方式建立
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (NOT is_published OR consent)               -- 沒有同意就不能公開
);


-- =====================================================================
-- I. 對話紀錄、待確認動作、同步紀錄、後台異動紀錄、升級紀錄
-- =====================================================================
CREATE TABLE message_logs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  line_user_id      TEXT NOT NULL REFERENCES line_users(line_user_id),
  webhook_event_id  TEXT UNIQUE,                    -- LINE 的 webhookEventId；重送時用來避免重複處理
  direction         TEXT NOT NULL CHECK (direction IN ('in','out')),
  message_type      TEXT NOT NULL DEFAULT 'text',   -- text／postback／image／sticker／flex…
  content           TEXT,
  intent            TEXT,                           -- AI 判斷的意圖：faq／schedule／score／leave／meal…
  tool_calls        JSONB,                          -- AI 呼叫了哪些查詢、帶什麼參數
  handled_by        TEXT NOT NULL DEFAULT 'ai' CHECK (handled_by IN ('ai','rule','staff')),
  needs_human       BOOLEAN NOT NULL DEFAULT false, -- AI 答不出來或家長要求真人 → 通知櫃台
  model             TEXT,
  latency_ms        INTEGER,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  reply_to          BIGINT REFERENCES message_logs(id) ON DELETE SET NULL   -- 機器人的回覆對應哪一則家長訊息
);

CREATE TABLE pending_actions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),   -- 放進確認按鈕的 postback data
  line_user_id  TEXT NOT NULL REFERENCES line_users(line_user_id),
  action_type   TEXT NOT NULL CHECK (action_type IN ('leave','meal_order','meal_cancel')),
  payload       JSONB NOT NULL,          -- 要執行的內容：學生、日期、餐點、金額…
  expires_at    TIMESTAMPTZ NOT NULL DEFAULT now() + interval '10 minutes',
  confirmed_at  TIMESTAMPTZ,
  cancelled_at  TIMESTAMPTZ,
  result        JSONB,                   -- 執行結果，例：{"leave_request_ids":[12,13]}
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sync_runs (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source         TEXT NOT NULL DEFAULT 'upad12',
  entity         TEXT NOT NULL,          -- students／courses／attendance／exam_scores…
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  rows_read      INTEGER,
  rows_upserted  INTEGER,
  status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','success','failed')),
  error          TEXT
);

CREATE TABLE admin_audit (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor       TEXT NOT NULL,                       -- 誰改的（後台登入時填的名字）
  action      TEXT NOT NULL,                       -- insert 新增／update 修改／delete 刪除／upload 上傳／enroll 加入班級／drop 退班
  table_name  TEXT NOT NULL,
  row_id      TEXT,
  summary     TEXT,                                -- 一句話說明，例：把 王小明 加入 國二數學 A 班
  before      JSONB,                               -- 修改前
  after       JSONB,                               -- 修改後
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE schema_migrations (
  version     TEXT PRIMARY KEY,                    -- 升級檔編號，例：003
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bot_settings (
  key         TEXT PRIMARY KEY,                    -- welcome_on_follow／notify_unanswered
  value       JSONB NOT NULL,
  updated_by  TEXT,                                -- 後台登入時填的名字
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE staff_alerts (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  line_user_id  TEXT NOT NULL REFERENCES line_users(line_user_id) ON DELETE CASCADE,  -- 哪一位家長的 LINE
  reason        TEXT NOT NULL CHECK (reason IN ('handoff','unanswered')),
  message_id    BIGINT REFERENCES message_logs(id) ON DELETE SET NULL,
  summary       TEXT NOT NULL,                       -- 通知內容：哪位家長、最近的訊息
  sent_to       INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE broadcasts (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_id       UUID NOT NULL UNIQUE,
  audience         TEXT NOT NULL CHECK (audience IN ('all','grade','course')),
  grade            SMALLINT CHECK (grade BETWEEN 6 AND 9),
  course_id        BIGINT REFERENCES courses(id) ON DELETE SET NULL,
  audience_label   TEXT NOT NULL,                    -- 傳送當時的對象名稱，例：國二家長
  body             TEXT NOT NULL,
  image_id         BIGINT REFERENCES media_files(id) ON DELETE SET NULL,
  announcement_id  BIGINT REFERENCES announcements(id) ON DELETE SET NULL,
  recipients       INTEGER,
  status           TEXT NOT NULL DEFAULT 'sending' CHECK (status IN ('sending','sent','partial','failed')),
  error            TEXT,
  sent_by          TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at          TIMESTAMPTZ
);


-- =====================================================================
-- 表格說明（在 Supabase / DBeaver 等工具裡看得到）
-- =====================================================================
COMMENT ON TABLE line_users          IS '加入官方帳號的每個 LINE 使用者，含尚未綁定的訪客';
COMMENT ON TABLE families            IS '家庭：把兄弟姊妹和家長綁在一起；家長可查同一家庭所有學生';
COMMENT ON TABLE staff               IS '教職員：管理者、導師、授課老師、櫃台；綁定 LINE 後可以收「需要真人」的通知';
COMMENT ON TABLE guardians           IS '家長／監護人，屬於某個家庭';
COMMENT ON TABLE students            IS '學生基本資料，屬於某個家庭';
COMMENT ON TABLE binding_codes       IS '一次性綁定碼：補習班發給家長或導師，在 LINE 輸入後完成綁定';
COMMENT ON TABLE subjects            IS '科目';
COMMENT ON TABLE courses             IS '課程／班級，例：國二數學 A 班';
COMMENT ON TABLE course_staff        IS '班級的授課老師與導師';
COMMENT ON TABLE course_schedules    IS '班級每週固定上課時段';
COMMENT ON TABLE class_sessions      IS '每一堂實際的課（由 generate_sessions 產生；停課、補課在此標記）';
COMMENT ON TABLE enrollments         IS '學生上哪些班（= 補了哪些科目）';
COMMENT ON TABLE leave_requests      IS '請假單，一堂課一筆';
COMMENT ON TABLE attendance          IS '到班紀錄（可由 UPAD12 出勤同步）';
COMMENT ON TABLE exams               IS '考試（補習班小考、週考、模擬考，或學校段考）';
COMMENT ON TABLE exam_scores         IS '每位學生每次考試的分數';
COMMENT ON TABLE exam_items          IS '考卷上的每一題（題目圖、答案、詳解、單元）';
COMMENT ON TABLE student_wrong_items IS '學生答錯的題目，錯題本的來源（只記錯的，減少輸入）';
COMMENT ON TABLE student_notes       IS '導師紀錄／聯絡簿；只有 visibility = guardian 的才會給家長';
COMMENT ON TABLE vendors             IS '餐點店家';
COMMENT ON TABLE meal_menus          IS '餐單上的一格：某一天的某一餐';
COMMENT ON TABLE meal_options        IS '該餐的 1、2、3 號餐（店家、品名、價格）';
COMMENT ON TABLE meal_orders         IS '學生的訂餐（含「不訂」）';
COMMENT ON TABLE wallets             IS '每位學生的餐費餘額（快取，真正依據是帳本）';
COMMENT ON TABLE wallet_transactions IS '餐費帳本：只能新增不能修改，更正請新增 adjust';
COMMENT ON TABLE tuition_bills       IS '學費帳單';
COMMENT ON TABLE media_files         IS '上傳的圖片（行事曆、交通地圖…）；網址 /files/代號.jpg 給 LINE 讀取';
COMMENT ON TABLE content_files       IS '「補習班資料」資料夾裡每個檔案的紀錄：判斷哪個是最新丟進來的、內容有沒有改';
COMMENT ON TABLE kb_articles         IS 'AI 知識庫：交通、電話、收費、請假規定等標準答案';
COMMENT ON TABLE announcements       IS '公告：停課、行事曆、考試、活動、招生';
COMMENT ON TABLE honor_roll          IS '榜單（需家長同意才公開）';
COMMENT ON TABLE message_logs        IS 'LINE 對話紀錄（除錯、找出 AI 答不好的問題）';
COMMENT ON TABLE pending_actions     IS 'AI 提出、等家長按「確認」才執行的動作（請假、訂餐）';
COMMENT ON TABLE sync_runs           IS 'UPAD12 資料匯入紀錄';
COMMENT ON TABLE admin_audit         IS '後台異動紀錄：誰在什麼時候改了哪筆資料（改前、改後）';
COMMENT ON TABLE schema_migrations   IS '已執行過的資料庫升級檔（sql/00X_*.sql）';
COMMENT ON TABLE bot_settings        IS '機器人設定（後台「設定」頁）：加好友時的歡迎詞、機器人答不出來時要不要通知員工';
COMMENT ON TABLE staff_alerts        IS '需要真人的紀錄：家長按「找真人客服」或機器人答不出來時，用 LINE 通知員工（同一位家長 30 分鐘內只通知一次）';
COMMENT ON TABLE broadcasts          IS '廣播：後台用 LINE 推播給家長的訊息（全體好友、某個年級或某一班的家長）';


-- =====================================================================
-- 觸發器
-- =====================================================================
CREATE TRIGGER trg_families_updated  BEFORE UPDATE ON families      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_staff_updated     BEFORE UPDATE ON staff         FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_guardians_updated BEFORE UPDATE ON guardians     FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_students_updated  BEFORE UPDATE ON students      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_courses_updated   BEFORE UPDATE ON courses       FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_meal_orders_upd   BEFORE UPDATE ON meal_orders   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_bills_updated     BEFORE UPDATE ON tuition_bills FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_kb_updated        BEFORE UPDATE ON kb_articles   FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 分數不可超過滿分（防止 85 打成 850）
CREATE FUNCTION check_score_range() RETURNS trigger
LANGUAGE plpgsql AS $$
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
CREATE TRIGGER trg_score_range BEFORE INSERT OR UPDATE ON exam_scores
  FOR EACH ROW EXECUTE FUNCTION check_score_range();

-- 訂餐：自動帶入價格快照；已扣款的訂單不能直接改餐
CREATE FUNCTION meal_order_price() RETURNS trigger
LANGUAGE plpgsql AS $$
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
CREATE TRIGGER trg_meal_order_price BEFORE INSERT OR UPDATE OF meal_option_id ON meal_orders
  FOR EACH ROW EXECUTE FUNCTION meal_order_price();

-- 帳本新增一筆 → 更新餘額（AFTER：只有真的寫入的交易才會動到餘額）
-- 先用餘額 0 建立錢包再 UPDATE：若加了 CHECK (balance >= 0)，餘額不足時這裡會擋下並整筆交易失敗
CREATE FUNCTION apply_wallet_txn() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO wallets (student_id) VALUES (NEW.student_id) ON CONFLICT (student_id) DO NOTHING;
  UPDATE wallets SET balance = balance + NEW.amount, updated_at = now()
   WHERE student_id = NEW.student_id;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_wallet_apply AFTER INSERT ON wallet_transactions
  FOR EACH ROW EXECUTE FUNCTION apply_wallet_txn();

-- 帳本只能新增，不能修改或刪除
CREATE FUNCTION forbid_ledger_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% 是只能新增的帳本；要更正請新增一筆 adjust 交易', TG_TABLE_NAME;
END $$;
CREATE TRIGGER trg_wallet_immutable BEFORE UPDATE OR DELETE ON wallet_transactions
  FOR EACH ROW EXECUTE FUNCTION forbid_ledger_change();


-- =====================================================================
-- 業務函式
-- =====================================================================

-- 依每週課表產生某段期間的每一堂課（重複執行不會重複產生）
CREATE FUNCTION generate_sessions(p_from DATE, p_to DATE) RETURNS INTEGER
LANGUAGE plpgsql AS $$
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

-- 產生一次性綁定碼。p_target：'guardian' | 'student' | 'staff'
CREATE FUNCTION issue_binding_code(
  p_target      TEXT,
  p_target_id   BIGINT,
  p_created_by  BIGINT  DEFAULT NULL,
  p_valid_days  INTEGER DEFAULT 14
) RETURNS TEXT
LANGUAGE plpgsql AS $$
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

-- 在 LINE 輸入綁定碼。
-- 成功回傳身分：'guardian' | 'student' | 'staff'（之後由程式切換對應的圖文選單）
-- 失敗回傳：'invalid'（查無此碼）| 'used'（已用過）| 'expired'（過期）| 'line_already_bound'（這個 LINE 已綁定其他人）
CREATE FUNCTION redeem_binding_code(p_code TEXT, p_line_user_id TEXT) RETURNS TEXT
LANGUAGE plpgsql AS $$
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

-- 一鍵扣款：某一餐所有尚未扣款的訂單。重複執行不會重複扣（帳本有唯一索引保護）。
-- 回傳每筆的結果：charged／already_charged／insufficient_balance（只有加了 balance >= 0 規則才會出現）
CREATE FUNCTION charge_meals(p_meal_menu_id BIGINT, p_staff_id BIGINT DEFAULT NULL)
RETURNS TABLE (order_id BIGINT, stu_id BIGINT, charge INTEGER, outcome TEXT)
LANGUAGE plpgsql AS $$
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

-- 取消訂餐；若已扣款會自動退回餘額
-- 回傳：cancelled／cancelled_and_refunded／already_cancelled／not_found
CREATE FUNCTION cancel_meal_order(p_order_id BIGINT, p_staff_id BIGINT DEFAULT NULL) RETURNS TEXT
LANGUAGE plpgsql AS $$
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


-- =====================================================================
-- 檢視表：AI 與後台都從這裡讀
-- =====================================================================

-- 學生（自動推算現在年級；不含內部備註）
CREATE VIEW v_students AS
SELECT s.id, s.student_no, s.family_id, s.name, s.school_name, s.status, s.line_user_id,
       (s.grade + school_year_of(tw_today()) - s.grade_school_year)::smallint            AS current_grade,
       grade_label(s.grade + school_year_of(tw_today()) - s.grade_school_year)            AS current_grade_label
  FROM students s;

-- LINE 使用者是誰（webhook 收到訊息第一步就查這個，決定能用哪些功能）
CREATE VIEW v_line_identity AS
SELECT lu.line_user_id, lu.display_name, lu.is_following,
       st.id AS staff_id, st.role AS staff_role,
       g.id  AS guardian_id, g.family_id,
       s.id  AS student_id,
       CASE WHEN st.id IS NOT NULL THEN 'staff'
            WHEN g.id  IS NOT NULL THEN 'guardian'
            WHEN s.id  IS NOT NULL THEN 'student'
            ELSE 'guest' END AS role
  FROM line_users lu
  LEFT JOIN staff     st ON st.line_user_id = lu.line_user_id AND st.is_active
  LEFT JOIN guardians g  ON g.line_user_id  = lu.line_user_id AND g.is_active
  LEFT JOIN students  s  ON s.line_user_id  = lu.line_user_id AND s.status <> 'left';

-- 權限核心：這個 LINE 家長可以查哪些學生（AI 的每個查詢都要先過這一關）
CREATE VIEW v_guardian_students AS
SELECT g.line_user_id, g.id AS guardian_id, g.name AS guardian_name, g.family_id,
       s.id AS student_id, s.name AS student_name, s.current_grade, s.current_grade_label
  FROM guardians g
  JOIN v_students s ON s.family_id = g.family_id AND s.status <> 'left'
 WHERE g.is_active AND g.line_user_id IS NOT NULL;

-- 對外公開的課表（訪客可查）
CREATE VIEW v_public_timetable AS
SELECT c.id AS course_id, c.name AS course_name, sub.name AS subject,
       c.grade, grade_label(c.grade) AS grade_label,
       cs.weekday, cs.start_time, cs.end_time, cs.room,
       c.fee, c.start_date, c.end_date, c.description
  FROM courses c
  JOIN subjects sub        ON sub.id = c.subject_id
  JOIN course_schedules cs ON cs.course_id = c.id
 WHERE c.is_public AND c.status = 'open';

-- 某位學生的每週課表（= 補了哪些科目、幾點上課、誰教）
CREATE VIEW v_student_timetable AS
SELECT e.student_id, c.id AS course_id, c.name AS course_name, sub.name AS subject,
       cs.weekday, cs.start_time, cs.end_time, cs.room,
       (SELECT string_agg(st.name, '、' ORDER BY st.name)
          FROM course_staff x JOIN staff st ON st.id = x.staff_id
         WHERE x.course_id = c.id AND x.role = 'teacher') AS teachers,
       e.status AS enrollment_status
  FROM enrollments e
  JOIN courses c           ON c.id = e.course_id AND c.status = 'open'
  JOIN subjects sub        ON sub.id = c.subject_id
  JOIN course_schedules cs ON cs.course_id = c.id
 WHERE e.status IN ('trial','active')
   AND (e.end_date IS NULL OR e.end_date >= tw_today());

-- 某位學生每一堂課（含停課、請假狀態）：「明天有課嗎」「要請哪幾堂」
CREATE VIEW v_student_sessions AS
SELECT e.student_id, ses.id AS session_id, ses.session_date, ses.start_time, ses.end_time,
       ses.room, ses.status AS session_status, ses.note AS session_note,
       c.id AS course_id, c.name AS course_name, sub.name AS subject,
       lr.id AS leave_id, lr.status AS leave_status
  FROM enrollments e
  JOIN class_sessions ses ON ses.course_id = e.course_id
                         AND ses.session_date >= e.start_date
                         AND (e.end_date IS NULL OR ses.session_date <= e.end_date)
  JOIN courses c          ON c.id = ses.course_id
  JOIN subjects sub       ON sub.id = c.subject_id
  LEFT JOIN leave_requests lr ON lr.session_id = ses.id AND lr.student_id = e.student_id
                             AND lr.status IN ('submitted','approved')
 WHERE e.status IN ('trial','active');

-- 成績（只含已發布的考試），附全班平均、最高分、排名
CREATE VIEW v_student_scores AS
SELECT sc.student_id, ex.id AS exam_id, ex.exam_date, ex.title, ex.exam_type,
       sub.name AS subject, c.name AS course_name,
       sc.score, ex.full_score, sc.status,
       round(sc.score * 100.0 / ex.full_score, 1)              AS score_pct,
       round(avg(sc.score) OVER w, 1)                           AS exam_avg,
       max(sc.score) OVER w                                     AS exam_max,
       count(sc.score) OVER w                                   AS takers,
       CASE WHEN sc.score IS NOT NULL
            THEN rank() OVER (PARTITION BY ex.id ORDER BY sc.score DESC NULLS LAST) END AS exam_rank,
       sc.teacher_comment
  FROM exam_scores sc
  JOIN exams ex      ON ex.id = sc.exam_id AND ex.scores_published
  JOIN subjects sub  ON sub.id = ex.subject_id
  LEFT JOIN courses c ON c.id = ex.course_id
WINDOW w AS (PARTITION BY ex.id);

-- 錯題本
CREATE VIEW v_wrong_book AS
SELECT w.student_id, ex.exam_date, sub.name AS subject, ex.title AS exam_title,
       it.item_no, it.unit_tag, it.question_image_url, it.answer, it.explanation,
       w.student_answer, w.is_corrected
  FROM student_wrong_items w
  JOIN exam_items it ON it.id = w.exam_item_id
  JOIN exams ex      ON ex.id = it.exam_id AND ex.scores_published
  JOIN subjects sub  ON sub.id = ex.subject_id;

-- 弱點單元：各單元錯誤率（只算有登錄逐題對錯的考試）
CREATE VIEW v_unit_weakness AS
SELECT sc.student_id, sub.name AS subject, it.unit_tag,
       count(*)                                               AS attempted,
       count(w.exam_item_id)                                  AS wrong,
       round(count(w.exam_item_id) * 100.0 / count(*), 1)     AS wrong_rate
  FROM exam_scores sc
  JOIN exams ex      ON ex.id = sc.exam_id AND ex.scores_published
  JOIN subjects sub  ON sub.id = ex.subject_id
  JOIN exam_items it ON it.exam_id = ex.id AND it.unit_tag IS NOT NULL
  LEFT JOIN student_wrong_items w ON w.exam_item_id = it.id AND w.student_id = sc.student_id
 WHERE sc.items_recorded
 GROUP BY sc.student_id, sub.name, it.unit_tag;

-- 導師紀錄中「家長可見」的部分
CREATE VIEW v_student_notes_for_guardian AS
SELECT n.student_id, n.note_date, st.name AS staff_name, n.content
  FROM student_notes n JOIN staff st ON st.id = n.staff_id
 WHERE n.visibility = 'guardian';

-- 導師用：請假名單（附「當天有訂餐」提醒，方便取消餐點）
CREATE VIEW v_session_leaves AS
SELECT ses.session_date, ses.start_time, c.id AS course_id, c.name AS course_name,
       s.id AS student_id, s.name AS student_name,
       lr.id AS leave_id, lr.leave_type, lr.reason, lr.status, lr.need_makeup, lr.source, lr.created_at,
       EXISTS (SELECT 1 FROM meal_orders mo JOIN meal_menus mm ON mm.id = mo.meal_menu_id
                WHERE mo.student_id = s.id AND mm.meal_date = ses.session_date
                  AND mo.status = 'active' AND mo.meal_option_id IS NOT NULL) AS has_meal_order
  FROM leave_requests lr
  JOIN class_sessions ses ON ses.id = lr.session_id
  JOIN courses c          ON c.id = ses.course_id
  JOIN students s         ON s.id = lr.student_id
 WHERE lr.status IN ('submitted','approved');

-- 店家訂餐彙總（每餐、每個選項幾份、多少錢）
CREATE VIEW v_meal_order_summary AS
SELECT mm.id AS meal_menu_id, mm.meal_date, mm.meal_slot,
       op.option_no, op.item_name, v.name AS vendor, v.phone AS vendor_phone, op.price,
       count(o.id)                    AS qty,
       coalesce(sum(o.unit_price), 0) AS total
  FROM meal_menus mm
  JOIN meal_options op ON op.meal_menu_id = mm.id
  JOIN vendors v       ON v.id = op.vendor_id
  LEFT JOIN meal_orders o ON o.meal_option_id = op.id AND o.status = 'active'
 GROUP BY mm.id, mm.meal_date, mm.meal_slot, op.option_no, op.item_name, v.name, v.phone, op.price;

-- 學生訂了什麼餐、扣款了沒
CREATE VIEW v_student_meals AS
SELECT o.id AS meal_order_id, o.student_id, mm.meal_date, mm.meal_slot, mm.order_deadline,
       op.option_no, op.item_name, v.name AS vendor, o.unit_price, o.status,
       EXISTS (SELECT 1 FROM wallet_transactions t
                WHERE t.meal_order_id = o.id AND t.txn_type = 'meal_charge') AS is_charged
  FROM meal_orders o
  JOIN meal_menus mm        ON mm.id = o.meal_menu_id
  LEFT JOIN meal_options op ON op.id = o.meal_option_id
  LEFT JOIN vendors v       ON v.id = op.vendor_id;

-- 餘額與「快不夠了」提醒（只列有使用餐費服務的學生；查無資料 = 尚未儲值過）
CREATE VIEW v_wallet_status AS
SELECT s.id AS student_id, s.name, s.family_id,
       w.balance, w.low_balance_alert,
       w.balance < w.low_balance_alert AS is_low
  FROM students s
  JOIN wallets w ON w.student_id = s.id
 WHERE s.status <> 'left';

-- 帳本明細（含每筆之後的餘額）
CREATE VIEW v_wallet_ledger AS
SELECT t.id, t.student_id, t.created_at, t.txn_type, t.amount, t.meal_order_id, t.note,
       sum(t.amount) OVER (PARTITION BY t.student_id ORDER BY t.id) AS balance_after
  FROM wallet_transactions t;

-- 學費未繳
CREATE VIEW v_tuition_status AS
SELECT b.id, b.student_id, b.title, b.amount, b.paid_amount,
       b.amount - b.paid_amount AS outstanding, b.pay_status, b.due_date
  FROM tuition_bills b
 WHERE NOT b.is_void;

-- 目前有效的公告
CREATE VIEW v_active_announcements AS
SELECT * FROM announcements
 WHERE starts_at <= now() AND (ends_at IS NULL OR ends_at > now());


-- =====================================================================
-- 索引
-- =====================================================================
CREATE INDEX ix_guardians_family      ON guardians (family_id);
CREATE INDEX ix_students_family       ON students (family_id);
CREATE INDEX ix_enrollments_student   ON enrollments (student_id);
CREATE INDEX ix_enrollments_course    ON enrollments (course_id);
CREATE UNIQUE INDEX uq_enrollment_current ON enrollments (student_id, course_id)
  WHERE status IN ('trial','active');                         -- 同一班不會重複在班
CREATE INDEX ix_sessions_date         ON class_sessions (session_date);
CREATE INDEX ix_leave_session         ON leave_requests (session_id);
CREATE INDEX ix_leave_student         ON leave_requests (student_id);
CREATE UNIQUE INDEX uq_leave_current  ON leave_requests (student_id, session_id)
  WHERE status IN ('submitted','approved');                   -- 同一堂課不會重複請假
CREATE INDEX ix_attendance_student    ON attendance (student_id);
CREATE INDEX ix_exams_course_date     ON exams (course_id, exam_date);
CREATE INDEX ix_scores_student        ON exam_scores (student_id);
CREATE INDEX ix_wrong_student         ON student_wrong_items (student_id);
CREATE INDEX ix_notes_student         ON student_notes (student_id, note_date DESC);
CREATE INDEX ix_meal_orders_menu      ON meal_orders (meal_menu_id);
CREATE INDEX ix_meal_orders_option    ON meal_orders (meal_option_id);
CREATE UNIQUE INDEX uq_meal_order_active ON meal_orders (student_id, meal_menu_id)
  WHERE status = 'active';                                    -- 一餐只能有一筆有效訂單
CREATE INDEX ix_wallet_txn_student    ON wallet_transactions (student_id, id);
CREATE UNIQUE INDEX uq_wallet_txn_once_per_order ON wallet_transactions (meal_order_id, txn_type)
  WHERE meal_order_id IS NOT NULL;                            -- 同一張訂單只扣一次、只退一次
CREATE INDEX ix_bills_student         ON tuition_bills (student_id);
CREATE INDEX ix_msg_user_time         ON message_logs (line_user_id, created_at DESC);
CREATE INDEX ix_msg_needs_human       ON message_logs (created_at) WHERE needs_human;
CREATE INDEX ix_msg_reply_to          ON message_logs (reply_to) WHERE reply_to IS NOT NULL;
CREATE INDEX ix_pending_open          ON pending_actions (line_user_id)
  WHERE confirmed_at IS NULL AND cancelled_at IS NULL;
CREATE UNIQUE INDEX uq_kb_title        ON kb_articles (title);  -- 常見問題按鈕用標題比對，不可重複
CREATE INDEX ix_admin_audit_time     ON admin_audit (created_at DESC);
CREATE INDEX ix_media_source_hash    ON media_files (source_hash);
CREATE INDEX ix_binding_codes_guardian ON binding_codes (guardian_id) WHERE used_at IS NULL;
CREATE INDEX ix_staff_alerts_user    ON staff_alerts (line_user_id, created_at DESC);
CREATE INDEX ix_staff_alerts_message ON staff_alerts (message_id) WHERE message_id IS NOT NULL;
CREATE INDEX ix_broadcasts_time      ON broadcasts (created_at DESC);


-- =====================================================================
-- 預設資料
-- =====================================================================
INSERT INTO subjects (name) VALUES
  ('國文'), ('英文'), ('數學'), ('自然'), ('理化'), ('生物'), ('地科'),
  ('社會'), ('歷史'), ('地理'), ('公民'), ('作文');

-- 這份 schema 已經包含 002～006 升級檔的內容，bot 啟動時不會再執行它們
INSERT INTO schema_migrations (version) VALUES ('002'), ('003'), ('004'), ('005'), ('006');


-- =====================================================================
-- 欄位說明（docs/資料庫結構.md、DBeaver、Supabase 都看得到）
-- =====================================================================
COMMENT ON COLUMN line_users.line_user_id IS 'LINE 使用者 ID';
COMMENT ON COLUMN line_users.display_name IS 'LINE 暱稱';
COMMENT ON COLUMN line_users.picture_url IS '大頭貼網址';
COMMENT ON COLUMN line_users.is_following IS '仍是好友：收到 unfollow（封鎖）事件時改 false';
COMMENT ON COLUMN line_users.menu_role IS '圖文選單身分：最後一次幫他連結的圖文選單；真正身分以 v_line_identity 為準';
COMMENT ON COLUMN line_users.followed_at IS '加好友時間';
COMMENT ON COLUMN line_users.unfollowed_at IS '封鎖時間';
COMMENT ON COLUMN line_users.last_active_at IS '最後互動時間';
COMMENT ON COLUMN line_users.bot_paused_until IS 'AI 暫停到：家長按「找真人客服」後，AI 暫停自動回覆到這個時間';
COMMENT ON COLUMN line_users.created_at IS '建立時間';
COMMENT ON COLUMN families.id IS '編號（自動產生）';
COMMENT ON COLUMN families.family_name IS '家庭名稱：例：王小明家';
COMMENT ON COLUMN families.phone IS '家中電話：家中主要電話';
COMMENT ON COLUMN families.address IS '地址';
COMMENT ON COLUMN families.note IS '內部備註';
COMMENT ON COLUMN families.created_at IS '建立時間';
COMMENT ON COLUMN families.updated_at IS '最後修改時間';
COMMENT ON COLUMN staff.id IS '編號（自動產生）';
COMMENT ON COLUMN staff.name IS '姓名';
COMMENT ON COLUMN staff.role IS '職務：admin 管理者／tutor 導師／teacher 授課老師／counter 櫃台';
COMMENT ON COLUMN staff.phone IS '電話';
COMMENT ON COLUMN staff.line_user_id IS '綁定的 LINE';
COMMENT ON COLUMN staff.is_active IS '在職';
COMMENT ON COLUMN staff.external_ref IS 'UPAD12 對應代號：UPAD12 的對應代號（有的話）';
COMMENT ON COLUMN staff.created_at IS '建立時間';
COMMENT ON COLUMN staff.updated_at IS '最後修改時間';
COMMENT ON COLUMN staff.notify_human IS '收通知：家長需要真人時，用 LINE 通知這位員工（會用掉訊息額度）';
COMMENT ON COLUMN guardians.id IS '編號（自動產生）';
COMMENT ON COLUMN guardians.family_id IS '家庭';
COMMENT ON COLUMN guardians.name IS '家長姓名';
COMMENT ON COLUMN guardians.relationship IS '關係：父／母／祖父母／其他';
COMMENT ON COLUMN guardians.phone IS '電話';
COMMENT ON COLUMN guardians.email IS 'Email';
COMMENT ON COLUMN guardians.line_user_id IS '綁定的 LINE';
COMMENT ON COLUMN guardians.is_primary IS '主要聯絡人：繳費、緊急聯絡';
COMMENT ON COLUMN guardians.is_active IS '有效';
COMMENT ON COLUMN guardians.external_ref IS 'UPAD12 對應代號';
COMMENT ON COLUMN guardians.created_at IS '建立時間';
COMMENT ON COLUMN guardians.updated_at IS '最後修改時間';
COMMENT ON COLUMN students.id IS '編號（自動產生）';
COMMENT ON COLUMN students.student_no IS '學號：建議直接沿用 UPAD12 學號';
COMMENT ON COLUMN students.family_id IS '家庭';
COMMENT ON COLUMN students.name IS '學生姓名';
COMMENT ON COLUMN students.school_name IS '就讀學校';
COMMENT ON COLUMN students.grade IS '年級：6 國小、7~9 國一~國三；是「grade_school_year 那一學年」的年級';
COMMENT ON COLUMN students.grade_school_year IS '年級所屬學年度：上面年級所屬學年度，例：115。現在年級由 v_students 自動推算';
COMMENT ON COLUMN students.phone IS '學生手機：學生本人手機（選填）';
COMMENT ON COLUMN students.line_user_id IS '學生本人的 LINE：預留：學生本人也綁 LINE';
COMMENT ON COLUMN students.status IS '在學狀態：active 在學／paused 暫停／left 離班';
COMMENT ON COLUMN students.joined_on IS '入班日';
COMMENT ON COLUMN students.left_on IS '離班日';
COMMENT ON COLUMN students.note IS '內部備註：不會出現在 AI 用的檢視表';
COMMENT ON COLUMN students.external_ref IS 'UPAD12 對應代號';
COMMENT ON COLUMN students.created_at IS '建立時間';
COMMENT ON COLUMN students.updated_at IS '最後修改時間';
COMMENT ON COLUMN binding_codes.code IS '綁定碼：8 碼大寫英數，用 issue_binding_code() 產生';
COMMENT ON COLUMN binding_codes.guardian_id IS '給哪位家長';
COMMENT ON COLUMN binding_codes.student_id IS '給哪位學生';
COMMENT ON COLUMN binding_codes.staff_id IS '給哪位教職員';
COMMENT ON COLUMN binding_codes.expires_at IS '到期時間';
COMMENT ON COLUMN binding_codes.used_at IS '使用時間';
COMMENT ON COLUMN binding_codes.used_by IS '用掉的 LINE 帳號';
COMMENT ON COLUMN binding_codes.created_by IS '發碼的人';
COMMENT ON COLUMN binding_codes.created_at IS '建立時間';
COMMENT ON COLUMN subjects.id IS '編號（自動產生）';
COMMENT ON COLUMN subjects.name IS '科目名稱';
COMMENT ON COLUMN courses.id IS '編號（自動產生）';
COMMENT ON COLUMN courses.name IS '班級名稱：例：國二數學 A 班';
COMMENT ON COLUMN courses.subject_id IS '科目';
COMMENT ON COLUMN courses.grade IS '年級：6 國小、7~9 國一~國三；跨年級可留空';
COMMENT ON COLUMN courses.start_date IS '開課日';
COMMENT ON COLUMN courses.end_date IS '結束日';
COMMENT ON COLUMN courses.fee IS '學費：對外公開的學費參考（實際帳單在 tuition_bills）';
COMMENT ON COLUMN courses.is_public IS '對外公開：未綁定的訪客也查得到（招生用）';
COMMENT ON COLUMN courses.status IS '狀態：open 開放中／closed 已結束／archived 封存';
COMMENT ON COLUMN courses.description IS '班級說明';
COMMENT ON COLUMN courses.external_ref IS 'UPAD12 對應代號';
COMMENT ON COLUMN courses.created_at IS '建立時間';
COMMENT ON COLUMN courses.updated_at IS '最後修改時間';
COMMENT ON COLUMN course_staff.course_id IS '班級';
COMMENT ON COLUMN course_staff.staff_id IS '教職員';
COMMENT ON COLUMN course_staff.role IS '角色：teacher 授課老師／tutor 導師';
COMMENT ON COLUMN course_schedules.id IS '編號（自動產生）';
COMMENT ON COLUMN course_schedules.course_id IS '班級';
COMMENT ON COLUMN course_schedules.weekday IS '星期：1=週一 … 7=週日';
COMMENT ON COLUMN course_schedules.start_time IS '上課時間';
COMMENT ON COLUMN course_schedules.end_time IS '下課時間';
COMMENT ON COLUMN course_schedules.room IS '教室';
COMMENT ON COLUMN class_sessions.id IS '編號（自動產生）';
COMMENT ON COLUMN class_sessions.course_id IS '班級';
COMMENT ON COLUMN class_sessions.session_date IS '上課日期';
COMMENT ON COLUMN class_sessions.start_time IS '上課時間';
COMMENT ON COLUMN class_sessions.end_time IS '下課時間';
COMMENT ON COLUMN class_sessions.room IS '教室';
COMMENT ON COLUMN class_sessions.status IS '狀態：scheduled 正常／cancelled 停課／makeup 補課';
COMMENT ON COLUMN class_sessions.note IS '備註：例：颱風停課、調到週六';
COMMENT ON COLUMN enrollments.id IS '編號（自動產生）';
COMMENT ON COLUMN enrollments.student_id IS '學生';
COMMENT ON COLUMN enrollments.course_id IS '班級';
COMMENT ON COLUMN enrollments.start_date IS '入班日';
COMMENT ON COLUMN enrollments.end_date IS '退班／結業日：NULL = 仍在班上';
COMMENT ON COLUMN enrollments.status IS '狀態：trial 試聽／active 在班／dropped 退班／finished 結業';
COMMENT ON COLUMN enrollments.created_at IS '建立時間';
COMMENT ON COLUMN leave_requests.id IS '編號（自動產生）';
COMMENT ON COLUMN leave_requests.student_id IS '學生';
COMMENT ON COLUMN leave_requests.session_id IS '那一堂課：一堂課一筆；請整天就多筆';
COMMENT ON COLUMN leave_requests.leave_type IS '假別：sick 病假／personal 事假／school 學校活動／other 其他';
COMMENT ON COLUMN leave_requests.reason IS '原因';
COMMENT ON COLUMN leave_requests.status IS '審核狀態：submitted 已送出／approved 已核准／rejected 不核准／cancelled 已取消';
COMMENT ON COLUMN leave_requests.need_makeup IS '要補課：需要補課／補看影片';
COMMENT ON COLUMN leave_requests.source IS '請假管道：line／phone 電話／counter 櫃台／upad12';
COMMENT ON COLUMN leave_requests.requested_by_guardian IS '請假的家長';
COMMENT ON COLUMN leave_requests.requested_by_staff IS '代請的老師';
COMMENT ON COLUMN leave_requests.reviewed_by IS '審核老師';
COMMENT ON COLUMN leave_requests.reviewed_at IS '審核時間';
COMMENT ON COLUMN leave_requests.created_at IS '建立時間';
COMMENT ON COLUMN attendance.session_id IS '那一堂課';
COMMENT ON COLUMN attendance.student_id IS '學生';
COMMENT ON COLUMN attendance.status IS '出缺勤：present 到班／late 遲到／absent 缺席／leave 請假';
COMMENT ON COLUMN attendance.check_in_at IS '到班時間';
COMMENT ON COLUMN attendance.check_out_at IS '離班時間';
COMMENT ON COLUMN attendance.leave_request_id IS '對應的請假單';
COMMENT ON COLUMN attendance.source IS '資料來源：manual 手動登錄／upad12';
COMMENT ON COLUMN attendance.recorded_by IS '登錄老師';
COMMENT ON COLUMN exams.id IS '編號（自動產生）';
COMMENT ON COLUMN exams.course_id IS '班級：哪一班考的；全年級模擬考可留空';
COMMENT ON COLUMN exams.session_id IS '那一堂課：哪一堂課考的（選填）';
COMMENT ON COLUMN exams.subject_id IS '科目';
COMMENT ON COLUMN exams.title IS '考試名稱：例：第 5 週小考：二次函數';
COMMENT ON COLUMN exams.exam_type IS '考試類型：quiz 小考／weekly 週考／mock 模擬考／school 學校段考／other 其他';
COMMENT ON COLUMN exams.exam_date IS '考試日期';
COMMENT ON COLUMN exams.full_score IS '滿分';
COMMENT ON COLUMN exams.scope IS '考試範圍：家長問「下次考什麼」用';
COMMENT ON COLUMN exams.paper_url IS '考卷檔案';
COMMENT ON COLUMN exams.scores_published IS '已發布成績：老師按「發布成績」後家長才查得到分數（範圍隨時可查）';
COMMENT ON COLUMN exams.scores_published_at IS '發布時間';
COMMENT ON COLUMN exams.created_by IS '出題老師';
COMMENT ON COLUMN exams.external_ref IS 'UPAD12 對應代號';
COMMENT ON COLUMN exams.created_at IS '建立時間';
COMMENT ON COLUMN exam_scores.exam_id IS '考試';
COMMENT ON COLUMN exam_scores.student_id IS '學生';
COMMENT ON COLUMN exam_scores.score IS '分數：缺考時為 NULL';
COMMENT ON COLUMN exam_scores.status IS '狀態：normal 正常／absent 缺考／makeup 補考';
COMMENT ON COLUMN exam_scores.items_recorded IS '已登錄逐題：對錯（分辨「全對」與「沒登錄」）';
COMMENT ON COLUMN exam_scores.teacher_comment IS '老師評語：給家長看的評語';
COMMENT ON COLUMN exam_scores.entered_by IS '登錄老師';
COMMENT ON COLUMN exam_scores.entered_at IS '登錄時間';
COMMENT ON COLUMN exam_items.id IS '編號（自動產生）';
COMMENT ON COLUMN exam_items.exam_id IS '考試';
COMMENT ON COLUMN exam_items.item_no IS '題號';
COMMENT ON COLUMN exam_items.points IS '配分';
COMMENT ON COLUMN exam_items.unit_tag IS '單元：／觀念，例：二次函數-配方法（弱點分析用）';
COMMENT ON COLUMN exam_items.question_image_url IS '題目圖：題目截圖';
COMMENT ON COLUMN exam_items.answer IS '答案';
COMMENT ON COLUMN exam_items.explanation IS '詳解：文字或圖片網址';
COMMENT ON COLUMN exam_items.question_bank_ref IS '題庫代號：若從 UPAD12 題庫出題，記題目代號';
COMMENT ON COLUMN student_wrong_items.exam_item_id IS '題目';
COMMENT ON COLUMN student_wrong_items.student_id IS '學生';
COMMENT ON COLUMN student_wrong_items.student_answer IS '學生答案';
COMMENT ON COLUMN student_wrong_items.is_corrected IS '已訂正：學生已訂正';
COMMENT ON COLUMN student_wrong_items.created_at IS '建立時間';
COMMENT ON COLUMN student_notes.id IS '編號（自動產生）';
COMMENT ON COLUMN student_notes.student_id IS '學生';
COMMENT ON COLUMN student_notes.staff_id IS '教職員';
COMMENT ON COLUMN student_notes.note_date IS '日期';
COMMENT ON COLUMN student_notes.content IS '內容';
COMMENT ON COLUMN student_notes.visibility IS '誰看得到：internal 內部／guardian 家長可見';
COMMENT ON COLUMN student_notes.created_at IS '建立時間';
COMMENT ON COLUMN vendors.id IS '編號（自動產生）';
COMMENT ON COLUMN vendors.name IS '店家名稱';
COMMENT ON COLUMN vendors.phone IS '電話';
COMMENT ON COLUMN vendors.note IS '備註';
COMMENT ON COLUMN vendors.is_active IS '合作中';
COMMENT ON COLUMN meal_menus.id IS '編號（自動產生）';
COMMENT ON COLUMN meal_menus.meal_date IS '日期';
COMMENT ON COLUMN meal_menus.meal_slot IS '哪一餐：lunch 午餐／dinner 晚餐';
COMMENT ON COLUMN meal_menus.order_deadline IS '訂餐截止：訂餐／改餐截止時間';
COMMENT ON COLUMN meal_menus.status IS '狀態：draft 草稿／open 開放訂餐／closed 截止／delivered 已送達';
COMMENT ON COLUMN meal_menus.note IS '備註';
COMMENT ON COLUMN meal_options.id IS '編號（自動產生）';
COMMENT ON COLUMN meal_options.meal_menu_id IS '餐單';
COMMENT ON COLUMN meal_options.option_no IS '幾號餐：目前是 1、2、3 號餐';
COMMENT ON COLUMN meal_options.vendor_id IS '店家：每個選項可來自不同店家';
COMMENT ON COLUMN meal_options.item_name IS '品名';
COMMENT ON COLUMN meal_options.price IS '價格';
COMMENT ON COLUMN meal_orders.id IS '編號（自動產生）';
COMMENT ON COLUMN meal_orders.student_id IS '學生';
COMMENT ON COLUMN meal_orders.meal_menu_id IS '餐單';
COMMENT ON COLUMN meal_orders.meal_option_id IS '選的餐：NULL = 明確勾選「不訂」';
COMMENT ON COLUMN meal_orders.unit_price IS '價格：下單當下的價格（觸發器自動從 meal_options 帶入，之後改菜單價格不影響已確認的訂單）';
COMMENT ON COLUMN meal_orders.status IS '狀態：active 有效／cancelled 已取消';
COMMENT ON COLUMN meal_orders.source IS '訂餐管道：paper 紙本／line／staff 老師登錄';
COMMENT ON COLUMN meal_orders.ordered_by_guardian IS '訂餐家長';
COMMENT ON COLUMN meal_orders.entered_by_staff IS '登錄老師';
COMMENT ON COLUMN meal_orders.confirmed_at IS '家長確認時間：家長在 LINE 按下「確認金額」的時間';
COMMENT ON COLUMN meal_orders.created_at IS '建立時間';
COMMENT ON COLUMN meal_orders.updated_at IS '最後修改時間';
COMMENT ON COLUMN wallets.student_id IS '學生';
COMMENT ON COLUMN wallets.balance IS '餘額：由 wallet_transactions 自動維護，請勿手動修改';
COMMENT ON COLUMN wallets.low_balance_alert IS '提醒門檻：低於此金額提醒儲值';
COMMENT ON COLUMN wallets.updated_at IS '最後異動';
COMMENT ON COLUMN wallet_transactions.id IS '編號（自動產生）';
COMMENT ON COLUMN wallet_transactions.student_id IS '學生';
COMMENT ON COLUMN wallet_transactions.txn_type IS '類型：topup 儲值／meal_charge 訂餐扣款／meal_refund 取消退回／adjust 人工調整';
COMMENT ON COLUMN wallet_transactions.amount IS '金額：正數 = 加錢，負數 = 扣錢';
COMMENT ON COLUMN wallet_transactions.meal_order_id IS '對應訂單';
COMMENT ON COLUMN wallet_transactions.note IS '備註';
COMMENT ON COLUMN wallet_transactions.created_by IS '經手人';
COMMENT ON COLUMN wallet_transactions.created_at IS '建立時間';
COMMENT ON COLUMN tuition_bills.id IS '編號（自動產生）';
COMMENT ON COLUMN tuition_bills.student_id IS '學生';
COMMENT ON COLUMN tuition_bills.course_id IS '班級';
COMMENT ON COLUMN tuition_bills.title IS '帳單名稱：例：115 上 國二數學 A 班 第 1 期';
COMMENT ON COLUMN tuition_bills.amount IS '應繳';
COMMENT ON COLUMN tuition_bills.paid_amount IS '已繳';
COMMENT ON COLUMN tuition_bills.pay_status IS '繳費狀態：自動計算，不會和金額不一致';
COMMENT ON COLUMN tuition_bills.due_date IS '繳費期限';
COMMENT ON COLUMN tuition_bills.paid_at IS '繳費時間';
COMMENT ON COLUMN tuition_bills.pay_method IS '付款方式：現金／轉帳／…';
COMMENT ON COLUMN tuition_bills.is_void IS '作廢';
COMMENT ON COLUMN tuition_bills.note IS '備註';
COMMENT ON COLUMN tuition_bills.external_ref IS 'UPAD12 對應代號：UPAD12 帳單編號';
COMMENT ON COLUMN tuition_bills.created_at IS '建立時間';
COMMENT ON COLUMN tuition_bills.updated_at IS '最後修改時間';
COMMENT ON COLUMN media_files.id IS '編號（自動產生）';
COMMENT ON COLUMN media_files.token IS '網址代號：圖片網址用的隨機代號（/files/代號.jpg），別人猜不到其他圖片的網址';
COMMENT ON COLUMN media_files.original_name IS '原檔名：上傳時的檔名';
COMMENT ON COLUMN media_files.content_type IS '檔案類型';
COMMENT ON COLUMN media_files.width IS '寬（像素）';
COMMENT ON COLUMN media_files.height IS '高（像素）';
COMMENT ON COLUMN media_files.bytes IS '檔案大小：原圖大小（位元組）';
COMMENT ON COLUMN media_files.data IS '原圖：已轉正、壓成 JPEG、最長邊 4096（LINE 上限 10MB）';
COMMENT ON COLUMN media_files.preview IS '預覽圖：最長邊 1024（LINE 規定 1MB 以內）';
COMMENT ON COLUMN media_files.note IS '用途：說明，例：115 年 11 月行事曆';
COMMENT ON COLUMN media_files.uploaded_by IS '上傳者：上傳的人（後台登入名稱）；從資料夾來的是 folder';
COMMENT ON COLUMN media_files.source_hash IS '檔案指紋：原始檔案的 SHA-256（同一張圖不重複存）';
COMMENT ON COLUMN media_files.created_at IS '建立時間';
COMMENT ON COLUMN content_files.path IS '檔案路徑：「補習班資料」資料夾裡的相對路徑，例：行事曆/11月行事曆.jpg';
COMMENT ON COLUMN content_files.sha256 IS '檔案指紋：檔案內容的指紋，內容一改就不同';
COMMENT ON COLUMN content_files.bytes IS '檔案大小';
COMMENT ON COLUMN content_files.mtime_ns IS '修改時間：檔案修改時間（只用來判斷要不要重算指紋）';
COMMENT ON COLUMN content_files.first_seen_at IS '第一次看到：這個檔案（或內容改變）的時間；「最新丟進來的行事曆」以這個為準';
COMMENT ON COLUMN content_files.last_seen_at IS '最後看到';
COMMENT ON COLUMN content_files.ai_draft_at IS 'AI 草稿時間：已經幫這張圖產生過 AI 草稿（不重複呼叫 AI）';
COMMENT ON COLUMN kb_articles.id IS '編號（自動產生）';
COMMENT ON COLUMN kb_articles.category IS '分類：contact 聯絡方式／traffic 交通停車／course 課程介紹／fee 收費／policy 規定（請假、退費）／enrollment 招生試聽／other 其他';
COMMENT ON COLUMN kb_articles.title IS '問題標題：例：怎麼到補習班？';
COMMENT ON COLUMN kb_articles.content IS '標準答案：AI 依此回覆（可放地圖、網址）';
COMMENT ON COLUMN kb_articles.image_id IS '附圖：例：交通地圖、收費表；回覆時一起傳給家長';
COMMENT ON COLUMN kb_articles.keywords IS '關鍵字';
COMMENT ON COLUMN kb_articles.audience IS '對象：public 所有人／guardian 已綁定家長／staff 教職員';
COMMENT ON COLUMN kb_articles.sort_order IS '排序（數字小的在前）';
COMMENT ON COLUMN kb_articles.is_active IS '啟用中';
COMMENT ON COLUMN kb_articles.source_path IS '來源檔案：來自「補習班資料」的哪個檔案（不含副檔名）；空白 = 用其他方式建立';
COMMENT ON COLUMN kb_articles.updated_by IS '最後修改者';
COMMENT ON COLUMN kb_articles.created_at IS '建立時間';
COMMENT ON COLUMN kb_articles.updated_at IS '最後修改時間';
COMMENT ON COLUMN announcements.id IS '編號（自動產生）';
COMMENT ON COLUMN announcements.title IS '標題';
COMMENT ON COLUMN announcements.body IS '內容：有附圖時，這裡放圖片的文字版（AI 靠這段回答）';
COMMENT ON COLUMN announcements.image_id IS '附圖：例：本月行事曆';
COMMENT ON COLUMN announcements.category IS '分類：general 一般／closure 停課(颱風)／calendar 行事曆／exam 考試／event 活動／enrollment 招生';
COMMENT ON COLUMN announcements.audience IS '對象：all 所有人／guardian 家長／staff 教職員／course 某一班／grade 某年級';
COMMENT ON COLUMN announcements.course_id IS '指定班級：audience = course 時填';
COMMENT ON COLUMN announcements.grade IS '指定年級：audience = grade 時填：6 國小、7~9 國一~國三';
COMMENT ON COLUMN announcements.grades IS '適用年級：6 國小、7~9 國一~國三，可以多個；空白 = 全部年級（「補習班資料」用年級資料夾設定）';
COMMENT ON COLUMN announcements.starts_at IS '開始顯示';
COMMENT ON COLUMN announcements.ends_at IS '結束顯示';
COMMENT ON COLUMN announcements.is_pinned IS '置頂';
COMMENT ON COLUMN announcements.source_path IS '來源檔案：來自「補習班資料」的哪個檔案（不含副檔名）；空白 = 用其他方式建立';
COMMENT ON COLUMN announcements.pushed_at IS '推播時間：已主動推播的時間（推播會用掉訊息額度）';
COMMENT ON COLUMN announcements.created_by IS '建立者';
COMMENT ON COLUMN announcements.created_at IS '建立時間';
COMMENT ON COLUMN honor_roll.id IS '編號（自動產生）';
COMMENT ON COLUMN honor_roll.school_year IS '學年度';
COMMENT ON COLUMN honor_roll.category IS '類別：會考／私中入學／段考／競賽';
COMMENT ON COLUMN honor_roll.student_id IS '學生';
COMMENT ON COLUMN honor_roll.display_name IS '顯示名稱：對外顯示，例：王○明';
COMMENT ON COLUMN honor_roll.school_name IS '學校：原就讀學校';
COMMENT ON COLUMN honor_roll.achievement IS '成績／錄取：例：會考 5A++、錄取第一志願';
COMMENT ON COLUMN honor_roll.consent IS '家長同意：公開';
COMMENT ON COLUMN honor_roll.is_published IS '公開';
COMMENT ON COLUMN honor_roll.sort_order IS '排序（數字小的在前）';
COMMENT ON COLUMN honor_roll.source_path IS '來源檔案：來自「補習班資料／榜單／榜單.csv」的第幾列；空白 = 用其他方式建立';
COMMENT ON COLUMN honor_roll.created_at IS '建立時間';
COMMENT ON COLUMN message_logs.id IS '編號（自動產生）';
COMMENT ON COLUMN message_logs.line_user_id IS 'LINE 使用者 ID';
COMMENT ON COLUMN message_logs.webhook_event_id IS 'LINE 事件 ID：LINE 的 webhookEventId；重送時用來避免重複處理';
COMMENT ON COLUMN message_logs.direction IS '方向：in 收到／out 回覆';
COMMENT ON COLUMN message_logs.message_type IS '訊息類型：text／postback／image／sticker／flex…';
COMMENT ON COLUMN message_logs.content IS '內容';
COMMENT ON COLUMN message_logs.intent IS '意圖：AI 判斷的意圖：faq／schedule／score／leave／meal…';
COMMENT ON COLUMN message_logs.tool_calls IS '處理細節：AI 呼叫了哪些查詢、帶什麼參數';
COMMENT ON COLUMN message_logs.handled_by IS '誰回的：ai 由 AI 回覆／rule 固定回覆或關鍵字比對／staff 真人';
COMMENT ON COLUMN message_logs.needs_human IS '需要真人：AI 答不出來或家長要求真人 → 通知櫃台';
COMMENT ON COLUMN message_logs.model IS 'AI 模型';
COMMENT ON COLUMN message_logs.latency_ms IS '回覆耗時（毫秒）';
COMMENT ON COLUMN message_logs.reply_to IS '回覆哪一則：機器人的回覆（out）對應的家長訊息（in）';
COMMENT ON COLUMN message_logs.created_at IS '建立時間';
COMMENT ON COLUMN pending_actions.id IS '確認碼：放進確認按鈕的 postback data';
COMMENT ON COLUMN pending_actions.line_user_id IS 'LINE 使用者 ID';
COMMENT ON COLUMN pending_actions.action_type IS '動作：leave 請假／meal_order 訂餐／meal_cancel 取消訂餐';
COMMENT ON COLUMN pending_actions.payload IS '內容：要執行的內容：學生、日期、餐點、金額…';
COMMENT ON COLUMN pending_actions.expires_at IS '過期時間';
COMMENT ON COLUMN pending_actions.confirmed_at IS '確認時間';
COMMENT ON COLUMN pending_actions.cancelled_at IS '取消時間';
COMMENT ON COLUMN pending_actions.result IS '結果：執行結果，例：{"leave_request_ids":[12,13]}';
COMMENT ON COLUMN pending_actions.created_at IS '建立時間';
COMMENT ON COLUMN sync_runs.id IS '編號（自動產生）';
COMMENT ON COLUMN sync_runs.source IS '來源';
COMMENT ON COLUMN sync_runs.entity IS '資料種類：students／courses／attendance／exam_scores…';
COMMENT ON COLUMN sync_runs.started_at IS '開始';
COMMENT ON COLUMN sync_runs.finished_at IS '結束';
COMMENT ON COLUMN sync_runs.rows_read IS '讀取筆數';
COMMENT ON COLUMN sync_runs.rows_upserted IS '寫入筆數';
COMMENT ON COLUMN sync_runs.status IS '狀態：running 執行中／success 成功／failed 失敗';
COMMENT ON COLUMN sync_runs.error IS '錯誤訊息';
COMMENT ON COLUMN admin_audit.id IS '編號（自動產生）';
COMMENT ON COLUMN admin_audit.actor IS '操作人：誰改的（後台登入時填的名字）';
COMMENT ON COLUMN admin_audit.action IS '動作：insert 新增／update 修改／delete 刪除／upload 上傳／enroll 加入班級／drop 退班';
COMMENT ON COLUMN admin_audit.table_name IS '資料表';
COMMENT ON COLUMN admin_audit.row_id IS '資料編號';
COMMENT ON COLUMN admin_audit.summary IS '說明：一句話說明，例：把 王小明 加入 國二數學 A 班';
COMMENT ON COLUMN admin_audit.before IS '修改前';
COMMENT ON COLUMN admin_audit.after IS '修改後';
COMMENT ON COLUMN admin_audit.created_at IS '時間';
COMMENT ON COLUMN schema_migrations.version IS '升級檔：編號，例：003';
COMMENT ON COLUMN schema_migrations.applied_at IS '執行時間';
COMMENT ON COLUMN bot_settings.key IS '設定名稱：welcome_on_follow 加好友時傳歡迎詞／notify_unanswered 機器人答不出來時也通知員工';
COMMENT ON COLUMN bot_settings.value IS '設定值（JSON）';
COMMENT ON COLUMN bot_settings.updated_by IS '最後修改者';
COMMENT ON COLUMN bot_settings.updated_at IS '最後修改時間';
COMMENT ON COLUMN staff_alerts.id IS '編號（自動產生）';
COMMENT ON COLUMN staff_alerts.line_user_id IS '哪一位家長的 LINE';
COMMENT ON COLUMN staff_alerts.reason IS '原因：handoff 家長按「找真人客服」或正在等真人／unanswered 機器人答不出來';
COMMENT ON COLUMN staff_alerts.message_id IS '是哪一則訊息';
COMMENT ON COLUMN staff_alerts.summary IS '通知內容：哪位家長、最近的訊息';
COMMENT ON COLUMN staff_alerts.sent_to IS '通知到幾位員工';
COMMENT ON COLUMN staff_alerts.error IS '通知失敗的原因';
COMMENT ON COLUMN staff_alerts.created_at IS '時間';
COMMENT ON COLUMN broadcasts.id IS '編號（自動產生）';
COMMENT ON COLUMN broadcasts.request_id IS '傳送代號：後台按「傳送」時產生；同一個請求重送不會傳兩次（也是 LINE 的 X-Line-Retry-Key）';
COMMENT ON COLUMN broadcasts.audience IS '對象：all 全體好友／grade 某個年級的家長／course 某一班的家長';
COMMENT ON COLUMN broadcasts.grade IS '年級：audience = grade 時填，6 國小、7~9 國一~國三';
COMMENT ON COLUMN broadcasts.course_id IS '班級：audience = course 時填';
COMMENT ON COLUMN broadcasts.audience_label IS '對象名稱：傳送當時的名稱，例：國二家長、國二數學 A 班家長';
COMMENT ON COLUMN broadcasts.body IS '內容';
COMMENT ON COLUMN broadcasts.image_id IS '附圖';
COMMENT ON COLUMN broadcasts.announcement_id IS '從哪一則公告帶入';
COMMENT ON COLUMN broadcasts.recipients IS '傳給幾個 LINE：全體好友時是空的（由 LINE 傳給所有好友）';
COMMENT ON COLUMN broadcasts.status IS '狀態：sending 傳送中／sent 已傳送／partial 只傳了一部分／failed 失敗';
COMMENT ON COLUMN broadcasts.error IS '失敗的原因';
COMMENT ON COLUMN broadcasts.sent_by IS '誰傳的（後台登入名稱）';
COMMENT ON COLUMN broadcasts.created_at IS '建立時間';
COMMENT ON COLUMN broadcasts.sent_at IS '傳送時間';

COMMIT;
