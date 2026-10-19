"""訪客圖文選單的版面：圖片與點擊區域共用同一份設定，確保按到哪裡就送出哪個字。"""

WIDTH, HEIGHT = 2500, 1686            # LINE 大尺寸圖文選單
COLS = [0, 833, 1667, 2500]           # 3 欄
ROWS = [0, 843, 1686]                 # 2 列

# (按鈕文字＝按下後替家長送出的文字, 小字說明, 主色)
BUTTONS = [
    [("課程介紹", "上課時間與學費", "#2F6FDB"),
     ("榜單", "歷屆亮眼成績", "#E59A12"),
     ("交通資訊", "地址・停車・接送", "#1F9D74")],
    [("聯絡我們", "電話與真人客服", "#0E8FB0"),
     ("常見問題", "請假・試聽・繳費", "#7B5BE6"),
     ("家長綁定", "輸入綁定碼", "#5B7083")],
]


def cells():
    """依序產生 (列, 欄, x, y, 寬, 高, 按鈕文字, 小字, 主色)。"""
    for r, row in enumerate(BUTTONS):
        for c, (label, sub, color) in enumerate(row):
            x, y = COLS[c], ROWS[r]
            yield r, c, x, y, COLS[c + 1] - x, ROWS[r + 1] - y, label, sub, color
