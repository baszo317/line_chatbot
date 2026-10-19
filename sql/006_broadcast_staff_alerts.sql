-- =====================================================================
-- v0.5 → v0.6：廣播、需要真人時通知員工（2026-10-19）
--   broadcasts：後台「廣播」用 LINE 推播出去的訊息（全體好友、某個年級或某一班的家長）。
--   staff.notify_human：家長需要真人時，用 LINE 通知這位員工。
--   staff_alerts：家長需要真人的紀錄（同一位家長 30 分鐘內只通知一次）。
--   推播（廣播、通知員工）會用掉官方帳號每月的訊息額度；回覆家長的訊息不算。
--   bot_settings 拿掉「官方帳號已自動回應的字」。
-- bot 啟動時自動執行；可以重複執行。
-- =====================================================================
BEGIN;

ALTER TABLE staff ADD COLUMN IF NOT EXISTS notify_human BOOLEAN NOT NULL DEFAULT false;
COMMENT ON TABLE staff IS '教職員：管理者、導師、授課老師、櫃台；綁定 LINE 後可以收「需要真人」的通知';
COMMENT ON COLUMN staff.notify_human IS '收通知：家長需要真人時，用 LINE 通知這位員工（會用掉訊息額度）';

CREATE TABLE IF NOT EXISTS staff_alerts (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  line_user_id  TEXT NOT NULL REFERENCES line_users(line_user_id) ON DELETE CASCADE,
  reason        TEXT NOT NULL CHECK (reason IN ('handoff','unanswered')),
  message_id    BIGINT REFERENCES message_logs(id) ON DELETE SET NULL,
  summary       TEXT NOT NULL,
  sent_to       INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_staff_alerts_user ON staff_alerts (line_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_staff_alerts_message ON staff_alerts (message_id) WHERE message_id IS NOT NULL;
COMMENT ON TABLE staff_alerts IS '需要真人的紀錄：家長按「找真人客服」或機器人答不出來時，用 LINE 通知員工（同一位家長 30 分鐘內只通知一次）';
COMMENT ON COLUMN staff_alerts.id IS '編號（自動產生）';
COMMENT ON COLUMN staff_alerts.line_user_id IS '哪一位家長的 LINE';
COMMENT ON COLUMN staff_alerts.reason IS '原因：handoff 家長按「找真人客服」或正在等真人／unanswered 機器人答不出來';
COMMENT ON COLUMN staff_alerts.message_id IS '是哪一則訊息';
COMMENT ON COLUMN staff_alerts.summary IS '通知內容：哪位家長、最近的訊息';
COMMENT ON COLUMN staff_alerts.sent_to IS '通知到幾位員工';
COMMENT ON COLUMN staff_alerts.error IS '通知失敗的原因';
COMMENT ON COLUMN staff_alerts.created_at IS '時間';

CREATE TABLE IF NOT EXISTS broadcasts (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_id       UUID NOT NULL UNIQUE,
  audience         TEXT NOT NULL CHECK (audience IN ('all','grade','course')),
  grade            SMALLINT CHECK (grade BETWEEN 6 AND 9),
  course_id        BIGINT REFERENCES courses(id) ON DELETE SET NULL,
  audience_label   TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS ix_broadcasts_time ON broadcasts (created_at DESC);
COMMENT ON TABLE broadcasts IS '廣播：後台用 LINE 推播給家長的訊息（全體好友、某個年級或某一班的家長）';
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

-- 「官方帳號已自動回應的字」拿掉了
DELETE FROM bot_settings WHERE key = 'oa_auto_reply_words';
COMMENT ON TABLE bot_settings IS '機器人設定（後台「設定」頁）：加好友時的歡迎詞、機器人答不出來時要不要通知員工';
COMMENT ON COLUMN bot_settings.key IS '設定名稱：welcome_on_follow 加好友時傳歡迎詞／notify_unanswered 機器人答不出來時也通知員工';

INSERT INTO schema_migrations (version) VALUES ('006') ON CONFLICT DO NOTHING;

COMMIT;
