-- =====================================================================
-- 從 schema v0.1 升級到 v0.2（第一期需要）
-- 如果你是直接用 v0.2 的 sql/schema.sql 建立資料庫，就不需要執行這個檔案。
-- =====================================================================
BEGIN;

-- 家長按「找真人客服」後，AI 暫停自動回覆到這個時間
ALTER TABLE line_users ADD COLUMN IF NOT EXISTS bot_paused_until TIMESTAMPTZ;

-- 「常見問題」按鈕用標題比對答案，所以標題不可重複；匯入 CSV 時也靠它判斷新增或更新
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_title ON kb_articles (title);

COMMIT;
