-- =====================================================================
-- 第一期公開資料（示範）：課程、每週課表、榜單。年級：國小（六年級）＝6、國一～國三＝7～9
-- 執行順序：sql/schema.sql → 本檔 → python scripts/import_kb.py data/kb_template.csv
-- 標「示範」的內容請換成補習班的真實資料：最方便是在後台（http://localhost:8000/admin）直接改。
-- 重跑本檔會先刪掉所有「（示範）」班級；示範班上已經加了學生的話會失敗，請先在後台把學生退班。
-- =====================================================================
BEGIN;

-- 先清掉舊的示範資料，這個檔可以重複執行
DELETE FROM course_schedules WHERE course_id IN (SELECT id FROM courses WHERE description LIKE '%（示範）');
DELETE FROM courses WHERE description LIKE '%（示範）';
DELETE FROM honor_roll WHERE achievement LIKE '%（示範）';

INSERT INTO courses (name, subject_id, grade, start_date, end_date, fee, is_public, description)
SELECT v.name, s.id, v.grade, DATE '2026-09-01', DATE '2027-01-20', v.fee, true, v.description
  FROM (VALUES
    ('國小數學班',     '數學', 6,  3600, '六年級先修國一數學，打好計算基礎（示範）'),
    ('國小英文班',     '英文', 6,  3400, '六年級銜接國中英文（示範）'),
    ('國一數學班',     '數學', 7,  4200, '銜接國小數學，重視觀念（示範）'),
    ('國二數學 A 班',  '數學', 8,  4800, '配合學校進度，每週小考（示範）'),
    ('國二英文 A 班',  '英文', 8,  4500, '文法與閱讀並重（示範）'),
    ('國三理化衝刺班', '理化', 9,  5200, '會考題型演練（示範）')
  ) AS v(name, subject, grade, fee, description)
  JOIN subjects s ON s.name = v.subject;

-- weekday：1 = 週一 … 7 = 週日
INSERT INTO course_schedules (course_id, weekday, start_time, end_time, room)
SELECT c.id, v.weekday, v.start_time::time, v.end_time::time, v.room
  FROM (VALUES
    ('國小數學班',     6, '10:00', '12:00', '101'),
    ('國小英文班',     3, '16:30', '18:00', '101'),
    ('國一數學班',     2, '18:30', '20:30', '102'),
    ('國一數學班',     4, '18:30', '20:30', '102'),
    ('國二數學 A 班',  1, '18:30', '20:30', '201'),
    ('國二數學 A 班',  3, '18:30', '20:30', '201'),
    ('國二英文 A 班',  2, '18:30', '20:30', '202'),
    ('國三理化衝刺班', 5, '18:30', '21:00', '301'),
    ('國三理化衝刺班', 6, '13:30', '16:30', '301')
  ) AS v(course, weekday, start_time, end_time, room)
  JOIN courses c ON c.name = v.course;

-- 榜單：一定要有家長同意（consent）才能公開，資料庫會擋
INSERT INTO honor_roll (school_year, category, display_name, school_name, achievement,
                        consent, is_published, sort_order) VALUES
  (114, '會考',     '張○安', '○○國中', '5A++（示範）',                true, true, 1),
  (114, '會考',     '劉○婷', '○○國中', '4A1B++（示範）',              true, true, 2),
  (114, '私中入學', '陳○宇', '○○國小', '錄取○○中學資優班（示範）',  true, true, 3);

COMMIT;
