-- =====================================================================
-- 從 v0.2 升級到 v0.3（2026-10-18）
--   1. 年級只收「國小＝6（國小部都是六年級）、國一～國三＝7～9」，原本是 1～12（含高中）
--   2. 公告、常見問題可以附圖（新表 media_files）；公告多一個「行事曆」分類
--   3. 「補習班資料」資料夾同步：content_files，以及各表的 source_path（這筆資料來自哪個檔案）
--   4. 後台異動紀錄 admin_audit、升級紀錄 schema_migrations
--   5. 每個欄位加上中文說明（COMMENT ON COLUMN）
--
-- 不用手動執行：bot 啟動時（app/migrate.py）會自動跑還沒跑過的升級檔。
-- 要手動也可以：python scripts/upgrade_db.py   或   psql "$DATABASE_URL" -f sql/003_v03_grades_images_admin.sql
-- 可以重複執行。不會動到你自己建立的資料；只會刪掉年級不符、也沒被其他資料用到的「（示範）」班級，
-- 以及「學測／分科」的示範榜單。
-- =====================================================================
BEGIN;

-- ---------------------------------------------------------------------
-- 1. 年級
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION grade_label(g INT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN g = 6              THEN '國小'
    WHEN g BETWEEN 7 AND 9  THEN '國' || (ARRAY['一','二','三'])[g - 6]
    WHEN g > 9              THEN '已畢業'
  END
$$;

-- 舊示範資料裡的「小五數學班」「高一數學班」：年級不在範圍內，而且沒被其他資料用到才刪（每週時段會一起刪）
DELETE FROM courses c
 WHERE c.description LIKE '%（示範）'
   AND c.grade IS NOT NULL AND c.grade NOT BETWEEN 6 AND 9
   AND NOT EXISTS (SELECT 1 FROM class_sessions x WHERE x.course_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM enrollments    x WHERE x.course_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM exams          x WHERE x.course_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM tuition_bills  x WHERE x.course_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM announcements  x WHERE x.course_id = c.id);
DELETE FROM honor_roll WHERE achievement LIKE '%（示範）' AND category IN ('學測', '分科');

-- 學生存的是「某一學年」的年級。舊資料若存成去年的小五（今年已是六年級），改用今年當基準，現在年級不變
UPDATE students
   SET grade = grade + school_year_of(tw_today()) - grade_school_year,
       grade_school_year = school_year_of(tw_today())
 WHERE grade NOT BETWEEN 6 AND 9
   AND grade + school_year_of(tw_today()) - grade_school_year BETWEEN 6 AND 9;

ALTER TABLE students      DROP CONSTRAINT IF EXISTS students_grade_check;
ALTER TABLE courses       DROP CONSTRAINT IF EXISTS courses_grade_check;
ALTER TABLE announcements DROP CONSTRAINT IF EXISTS announcements_grade_check;

-- 還有不符合的舊資料時，限制先設成 NOT VALID：舊資料保留、之後新增或修改都會檢查，並在升級時提醒
DO $$
DECLARE n INT;
BEGIN
  SELECT count(*) INTO n FROM students WHERE grade NOT BETWEEN 6 AND 9;
  IF n = 0 THEN
    ALTER TABLE students ADD CONSTRAINT students_grade_check CHECK (grade BETWEEN 6 AND 9);
  ELSE
    ALTER TABLE students ADD CONSTRAINT students_grade_check CHECK (grade BETWEEN 6 AND 9) NOT VALID;
    RAISE WARNING '有 % 位學生的年級不是國小或國一～國三，資料先保留，請到後台「學生與家長」修正', n;
  END IF;

  SELECT count(*) INTO n FROM courses WHERE grade NOT BETWEEN 6 AND 9;
  IF n = 0 THEN
    ALTER TABLE courses ADD CONSTRAINT courses_grade_check CHECK (grade BETWEEN 6 AND 9);
  ELSE
    ALTER TABLE courses ADD CONSTRAINT courses_grade_check CHECK (grade BETWEEN 6 AND 9) NOT VALID;
    RAISE WARNING '有 % 個班級的年級不是國小或國一～國三（例如舊的高中班），資料先保留，請到後台「班級與課表」修改或封存', n;
  END IF;

  SELECT count(*) INTO n FROM announcements WHERE grade NOT BETWEEN 6 AND 9;
  IF n = 0 THEN
    ALTER TABLE announcements ADD CONSTRAINT announcements_grade_check CHECK (grade BETWEEN 6 AND 9);
  ELSE
    ALTER TABLE announcements ADD CONSTRAINT announcements_grade_check CHECK (grade BETWEEN 6 AND 9) NOT VALID;
    RAISE WARNING '有 % 則公告指定的年級不是國小或國一～國三，請到後台「公告／行事曆」修正', n;
  END IF;
END $$;


-- ---------------------------------------------------------------------
-- 2. 圖片、行事曆
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_files (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  token          TEXT NOT NULL UNIQUE DEFAULT replace(gen_random_uuid()::text, '-', ''),
  original_name  TEXT,
  content_type   TEXT NOT NULL DEFAULT 'image/jpeg',
  width          INTEGER,
  height         INTEGER,
  bytes          INTEGER NOT NULL,
  data           BYTEA NOT NULL,
  preview        BYTEA NOT NULL,
  note           TEXT,
  uploaded_by    TEXT,
  source_hash    TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_media_source_hash ON media_files (source_hash);

ALTER TABLE kb_articles   ADD COLUMN IF NOT EXISTS image_id BIGINT REFERENCES media_files(id) ON DELETE SET NULL;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS image_id BIGINT REFERENCES media_files(id) ON DELETE SET NULL;

ALTER TABLE announcements DROP CONSTRAINT IF EXISTS announcements_category_check;
ALTER TABLE announcements ADD CONSTRAINT announcements_category_check
  CHECK (category IN ('general','closure','calendar','exam','event','enrollment'));


-- ---------------------------------------------------------------------
-- 3. 「補習班資料」資料夾同步
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_files (
  path           TEXT PRIMARY KEY,
  sha256         TEXT NOT NULL,
  bytes          INTEGER NOT NULL,
  mtime_ns       BIGINT NOT NULL,
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ai_draft_at    TIMESTAMPTZ
);
ALTER TABLE kb_articles   ADD COLUMN IF NOT EXISTS source_path TEXT UNIQUE;
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS source_path TEXT UNIQUE;
ALTER TABLE honor_roll    ADD COLUMN IF NOT EXISTS source_path TEXT UNIQUE;

-- SELECT * 的檢視表要重建，才會帶到新的 image_id、source_path 欄位
CREATE OR REPLACE VIEW v_active_announcements AS
SELECT * FROM announcements
 WHERE starts_at <= now() AND (ends_at IS NULL OR ends_at > now());


-- ---------------------------------------------------------------------
-- 4. 後台異動紀錄、升級紀錄
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_audit (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  table_name  TEXT NOT NULL,
  row_id      TEXT,
  summary     TEXT,
  before      JSONB,
  after       JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_admin_audit_time ON admin_audit (created_at DESC);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version) VALUES ('002'), ('003') ON CONFLICT DO NOTHING;


-- ---------------------------------------------------------------------
-- 5. 表格與欄位說明
-- ---------------------------------------------------------------------
COMMENT ON TABLE media_files       IS '上傳的圖片（行事曆、交通地圖…）；網址 /files/代號.jpg 給 LINE 讀取';
COMMENT ON TABLE content_files     IS '「補習班資料」資料夾裡每個檔案的紀錄：判斷哪個是最新丟進來的、內容有沒有改';
COMMENT ON TABLE announcements     IS '公告：停課、行事曆、考試、活動、招生';
COMMENT ON TABLE admin_audit       IS '後台異動紀錄：誰在什麼時候改了哪筆資料（改前、改後）';
COMMENT ON TABLE schema_migrations IS '已執行過的資料庫升級檔（sql/00X_*.sql）';

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

COMMIT;
