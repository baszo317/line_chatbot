-- =====================================================================
-- v0.3 → v0.4：公告、行事曆可以指定適用年級（2026-10-18）
--   「補習班資料」裡放在年級資料夾的檔案（例：公告/考試/國二/段考範圍.png、行事曆/國小/11月行事曆.jpg）
--   會記在 announcements.grades。空白＝全部年級。
-- bot 啟動時自動執行；可以重複執行。
-- =====================================================================
BEGIN;

ALTER TABLE announcements ADD COLUMN IF NOT EXISTS grades SMALLINT[];
ALTER TABLE announcements DROP CONSTRAINT IF EXISTS announcements_grades_check;
ALTER TABLE announcements ADD CONSTRAINT announcements_grades_check CHECK (grades <@ ARRAY[6,7,8,9]::smallint[]);

-- SELECT * 的檢視表要重建，才會帶到新的 grades 欄位
CREATE OR REPLACE VIEW v_active_announcements AS
SELECT * FROM announcements
 WHERE starts_at <= now() AND (ends_at IS NULL OR ends_at > now());

COMMENT ON COLUMN announcements.grades IS '適用年級：6 國小、7~9 國一~國三，可以多個；空白 = 全部年級（「補習班資料」用年級資料夾設定）';

INSERT INTO schema_migrations (version) VALUES ('004') ON CONFLICT DO NOTHING;

COMMIT;
