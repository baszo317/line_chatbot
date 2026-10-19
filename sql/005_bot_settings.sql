-- =====================================================================
-- v0.4 → v0.5：後台「設定」頁（2026-10-19）
--   bot_settings：機器人設定，例如「LINE 官方帳號已經自動回應的字」（bot 收到一樣的字就不再回，
--   避免家長收到兩次）、加好友時要不要傳 bot 的歡迎詞。
--   message_logs.reply_to：機器人的回覆是回哪一則訊息。
-- bot 啟動時自動執行；可以重複執行。
-- =====================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS bot_settings (
  key         TEXT PRIMARY KEY,                    -- oa_auto_reply_words／welcome_on_follow
  value       JSONB NOT NULL,
  updated_by  TEXT,                                -- 後台登入時填的名字
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE bot_settings IS '機器人設定（後台「設定」頁）：LINE 官方帳號已自動回應的字、加好友時的歡迎詞';
COMMENT ON COLUMN bot_settings.key IS '設定名稱：oa_auto_reply_words 官方帳號已自動回應的字／welcome_on_follow 加好友時傳歡迎詞';
COMMENT ON COLUMN bot_settings.value IS '設定值（JSON）';
COMMENT ON COLUMN bot_settings.updated_by IS '最後修改者';
COMMENT ON COLUMN bot_settings.updated_at IS '最後修改時間';

-- 機器人的回覆記下是回哪一則（後台「對話紀錄」用；家長連續傳好幾則時才不會對錯）
ALTER TABLE message_logs ADD COLUMN IF NOT EXISTS reply_to BIGINT REFERENCES message_logs(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_msg_reply_to ON message_logs (reply_to) WHERE reply_to IS NOT NULL;
COMMENT ON COLUMN message_logs.reply_to IS '回覆哪一則：機器人的回覆（out）對應的家長訊息（in）';

-- 一個家長發新的綁定碼時，舊的還沒用的碼會作廢；這個索引讓查詢快一點
CREATE INDEX IF NOT EXISTS ix_binding_codes_guardian ON binding_codes (guardian_id) WHERE used_at IS NULL;

INSERT INTO schema_migrations (version) VALUES ('005') ON CONFLICT DO NOTHING;

COMMIT;
