# 高明補習班 LINE AI 助理 — 第一期：家長自動回覆

家長在 LINE 官方帳號問課程、上課時間、行事曆、學費、交通、榜單、停課公告等**公開資訊**，bot 自動回覆；
答不出來或家長要求時轉給櫃台老師，AI 自動暫停 30 分鐘，讓老師在官方帳號後台聊天室接手。

完整的一步一步教學（含截圖與案例）請看教學文件；這份 README 是速查版。

## 資料怎麼更新（v0.6）

全部都在**後台** http://localhost:8000/admin 改（只能在跑 bot 的這台電腦開）。

| 要改的東西 | 後台的哪一頁 | 存到哪裡 |
| --- | --- | --- |
| 停課、考試、活動、招生公告、行事曆 | 公告與行事曆（選類型 → 填幾格 → 自動產生標題和內容，右邊看得到家長收到的樣子） | 「補習班資料」資料夾的檔案，再立刻同步 |
| 常見問題（交通、電話、收費、規定…） | 常見問題 | 同上 |
| 榜單 | 榜單 | 同上（`榜單/榜單.csv`） |
| 學生、家長、班級名單、上課時段 | 學生與家長、班級與課表 | 直接寫進資料庫 |
| 家長的 LINE 綁定 | 學生與家長 → 家長旁邊的「直接綁定」或「發綁定碼」 | 資料庫 |
| 用 LINE 推播給全體好友、某個年級或某一班的家長 | 廣播 | 資料庫（`broadcasts`） |
| 需要真人時通知哪些員工、歡迎訊息 | 設定 | 資料庫 |
| 回覆規則（語言、口氣、長度） | — | `app/ai.py` 的 `RULES`；固定回覆的文字在 `app/replies.py` |

- 「補習班資料」資料夾一直是公告、常見問題、榜單的唯一來源：後台存檔就是幫你寫檔案，所以也可以照舊直接丟檔案、改 .txt（見資料夾裡的 `使用說明.txt`），兩種方法可以混著用。
- 資料庫在 `.env` 的 `DATABASE_URL`（預設是這台電腦上的 PostgreSQL：`localhost:5432/gaoming`）。圖片也存在資料庫裡，備份資料庫就會一起備份。
- 年級只有 **國小（六年級）＝6、國一～國三＝7～9**；每年 8/1 自動升一級，國三之後顯示「已畢業」。
- 公告、行事曆可以只給某些年級：後台選年級；用資料夾的話，放進「國小／國一／國二／國三／國中」資料夾、檔名寫年級（`國一行事曆.png` 就只給國一），或 .txt 第一行寫 `年級：國一、國二`。
  家長說了年級，就只收到那個年級和全部年級適用的；**那個年級沒有的話，直接說「目前沒有國二的考試公告」**，不會拿別的年級或別的主題的資料回答。
- 改了什麼、誰改的，都記在 `admin_audit`（後台「設定」頁看得到）；每次資料夾同步的結果寫在資料夾裡的 `_同步結果.txt`。
- 資料庫有哪些表、每個欄位是什麼：`docs/資料庫結構.md`。

## 家長綁定 LINE（學生資料怎麼和家長的 LINE 連起來）

後台新增學生、家長時，資料是**直接存進資料庫**的。家長的 LINE 有兩種綁法；綁好後 `guardians.line_user_id` 就是這位家長的 LINE，
後台會顯示「已綁定 LINE」和他的 LINE 名稱。家長可以問「孩子的課表」；成績、請假、餐費等之後開放的功能也是靠這個綁定。

**櫃台直接綁定（家長不用輸入任何碼）**

1. 請家長在補習班的 LINE 官方帳號傳一句話，例如「我是王小明的媽媽」（也可以先按「找真人客服」再留言，AI 會暫停，不會搶著回答）。
2. 後台「學生與家長」→ 點學生 → 家長旁邊按「直接綁定」：會列出加過好友、傳過訊息的 LINE，最近的在上面；訊息提到孩子名字或家長電話的會標出來。確認是本人後按「綁定」。
3. 可以在官方帳號的聊天室跟家長說一聲已經綁好了。

LINE 不提供「用電話號碼或 LINE ID 找人」：家長要在 bot 上線後加好友，或傳過訊息，bot 才知道他的 LINE。所以家長完全沒動作之前，沒辦法先幫他綁好。

**綁定碼（家長自己輸入）**

1. 家長旁邊按「發綁定碼」，會出現 8 碼的碼（14 天內有效、只能用一次），按「複製給家長的說明」貼給家長。
2. 家長在官方帳號輸入 `綁定 AB12CD34`（或按圖文選單的「家長綁定」看說明）。

換手機或綁錯人：後台按「解除綁定」，再重新綁定。直接綁定後，這位家長之前發、還沒用的綁定碼會作廢。

## 廣播、通知員工（會用到 LINE 的訊息額度）

bot **回覆**家長的訊息不算額度；**主動傳**出去的（廣播、通知員工）會用掉官方帳號每月的訊息額度，傳給幾個人就用掉幾則。
後台「廣播」和「設定」頁會顯示這個月已用幾則、上限幾則（從 LINE 查的）。

**廣播**（後台「廣播」）

- 對象：全體好友（所有加官方帳號好友的人，包含還沒綁定的），或某個年級、某一班的家長。
- 年級、班級只會傳給**已經綁定 LINE** 的家長；還沒綁定的會列出學生名字，收不到。
- 可以「從公告帶入」內容；公告編輯畫面也有「用 LINE 推播」按鈕。按「傳送」前會顯示這次大約用掉幾則額度。
- 按兩下、網路重送都不會傳兩次；傳過的都記在下面的「傳過的廣播」。

**需要真人時通知員工**（後台「設定」）

1. 員工用自己的 LINE 加官方帳號好友，傳一句話（例如「我是小美」）。
2. 後台「設定」→「新增員工」→ 填姓名、選這個 LINE。可以按「測試」傳一則測試通知。
3. 家長按「找真人客服」或機器人答不出來時，員工的 LINE 會收到：哪位家長（有綁定的寫家長和孩子、年級；沒綁定的寫 LINE 名稱）、最近幾句話。
   員工還是要到**官方帳號的聊天室**回覆家長（從自己的 LINE 回，家長收不到）。
4. 同一位家長 30 分鐘內只通知一次；不想在「答不出來」時通知，就把「機器人答不出來時也通知」取消勾選，只留「找真人客服」。

## LINE 官方帳號後台要怎麼設定（避免家長收到兩次回覆）

如果按圖文選單、打某些字會收到兩則回覆，是因為 LINE 官方帳號後台（manager.line.biz）的「自動回應訊息」和 bot（Webhook）都在回。
到官方帳號後台「設定 → 回應設定」：

- **Webhook**：開（bot 才收得到訊息）
- **聊天**：開（櫃台在官方帳號的聊天室回覆「找真人客服」的家長）
- **自動回應訊息**：關（圖文選單的按鈕 bot 都會回）。新開的官方帳號預設會用「感謝您的訊息！很抱歉，本帳號無法個別回覆…」回每一則訊息，那也是自動回應訊息，一樣要關。
- **加入好友的歡迎訊息**：和後台「設定」頁的「加好友時，機器人傳歡迎訊息」二選一，不要兩個都開。

## bot 怎麼決定回答方式

1. **圖文選單按鈕**（課程介紹、行事曆、榜單、交通資訊、聯絡我們、常見問題、家長綁定）→ 固定回覆，直接從資料庫產生，又快又免費
2. **綁定碼**（`綁定 AB12CD34`）→ 把這個 LINE 綁定到後台的家長；已綁定的家長問孩子的課表 → 從資料庫直接回
3. **自由提問** → Claude（預設 `claude-haiku-5-5`），只根據補習班資料回答，資料沒有就請家長找真人
4. **Claude 不能用**（沒填 API key、網路斷線、逾時）→ 關鍵字比對：先認出家長在問哪一類（停課、行事曆、考試公告、課程、交通…），只從那一類找答案；那一類沒有資料就直接說沒有、附「找真人客服」按鈕
5. **找真人客服** → AI 暫停 30 分鐘；按選單仍會立即回覆，打字的訊息交給櫃台
6. **需要真人**（按了找真人客服、答不出來）→ 先回覆家長，再用 LINE 通知後台「設定」裡勾了「收通知」的員工
7. 用到的常見問題或公告**有附圖**（例如行事曆、交通地圖）→ 文字後面接著傳圖片（LINE 只收 https 圖片網址，要透過 tunnel 或正式網域）
8. 每則訊息都記在 `message_logs`（後台「對話紀錄」看得到）；LINE 重送的同一事件只處理一次

## 快速開始（Python 3.10 以上）

```bash
# 1. 安裝
python -m venv .venv
source .venv/bin/activate            # Windows：.venv\Scripts\activate
pip install -r requirements-dev.txt

# 2. 設定
cp .env.example .env                 # 填入 LINE 與 Claude 的金鑰、DATABASE_URL、BOT_ADMIN_PASSWORD

# 3. 資料庫（PostgreSQL 14 以上；本機或雲端託管都可以）
#    空的資料庫：bot 第一次啟動會自動建立全部資料表；舊版資料庫：啟動時自動升級（也可以手動跑 python scripts/upgrade_db.py）
python scripts/upgrade_db.py --demo  # 想先放示範課程與榜單的話
python scripts/import_kb.py data/kb_template.csv   # 示範的常見問題（第一次啟動會匯出到「補習班資料」資料夾）

# 4. 先在本機展示（不需要任何金鑰，只要資料庫）
python demo/run_demo.py              # 跑 6 個家長情境（用 .env 的資料庫；展示用的颱風公告跑完會自動刪掉）
python demo/run_demo.py --chat       # 自己打字跟 bot 聊
python demo/render_screenshots.py    # 產生截圖（第一次要先 playwright install chromium）
python demo/build_web_demo.py        # 網頁版展示 demo/output/web_demo.html，瀏覽器打開就能點選單、打字問

# 5. 啟動 bot 並接上 LINE
uvicorn app.main:app --port 8000
cloudflared tunnel --url http://localhost:8000    # 或 ngrok http 8000，取得 https 網址
#    把 https://你的網址/callback 填進 LINE Developers Console 的 Webhook URL，按 Verify
#    後台：瀏覽器開 http://localhost:8000/admin（只能在這台電腦開，從 tunnel 網址連進來會被擋）

# 6. 圖文選單
python scripts/make_rich_menu_image.py           # 產生 assets/richmenu_guest.png（已附一份）
python scripts/create_rich_menu.py --replace     # 「家長綁定」按鈕的小字改成「輸入綁定碼」了，用過舊選單的要重新上傳一次
```

## 檔案

| 路徑 | 用途 |
| --- | --- |
| `app/main.py` | FastAPI 入口：`POST /callback` 接收 LINE webhook；啟動時自動升級資料庫、開始監看資料夾 |
| `app/bot.py` | 判斷每則訊息要怎麼回（`decide()`、沒有 AI 時的 `_offline_decide()`），綁定碼，附圖片 |
| `app/ai.py` | 呼叫 Claude（structured outputs、prompt caching）；回覆規則 `RULES` |
| `app/knowledge.py` | 把資料庫的公開資料整理給 AI；關鍵字比對用的工具（年級、科目、問題類型） |
| `app/replies.py` | 固定格式的回覆內容 |
| `app/content.py` | 「補習班資料」資料夾 → 資料庫（丟檔案就更新） |
| `app/admin.py`、`app/static/admin.html` | 後台：學生／家長（含 LINE 綁定）、班級名單、上課時段、設定、對話紀錄、機器人測試 |
| `app/admin_content.py` | 後台的公告、行事曆、常見問題、榜單表單：寫進「補習班資料」資料夾再同步 |
| `app/admin_messaging.py` | 後台的廣播、收通知的員工、LINE 訊息額度 |
| `app/broadcast.py` | 廣播：找出要傳的家長，用 LINE 推播 |
| `app/staff_alerts.py` | 家長需要真人時，用 LINE 通知員工（同一位家長 30 分鐘內只通知一次） |
| `app/bot_settings.py` | 後台「設定」頁的機器人設定（歡迎訊息、答不出來時要不要通知員工） |
| `app/media.py` | 圖片：轉正、壓縮、預覽圖，`/files/代號.jpg` 給 LINE 讀 |
| `app/vision.py` | 只丟圖片時，用 AI 把圖片轉成文字草稿 |
| `app/migrate.py` | 資料庫自動升級 |
| `app/line_api.py` | LINE Messaging API（回覆、推播、訊息額度、載入動畫、圖文選單） |
| `app/db.py` | bot 用到的 SQL |
| `sql/` | `schema.sql`（v0.6）、`002_phase1.sql`～`006_broadcast_staff_alerts.sql`（升級檔，bot 啟動時自動執行）、`phase1_seed.sql`（示範課程與榜單） |
| `docs/資料庫結構.md` | 每張表、每個欄位的說明 |
| `data/kb_template.csv` | 知識庫範本（舊方法；現在建議直接改「補習班資料／常見問題」） |
| `scripts/` | 升級資料庫、同步資料夾、匯入知識庫、產生與建立圖文選單 |
| `demo/` | 模擬 LINE 與 Claude 的展示工具、預錄 AI 回覆、截圖產生器、網頁版展示（`web_demo_template.html`） |
| `tests/` | 自動測試 |

## 設定（.env）

| 變數 | 說明 |
| --- | --- |
| `LINE_CHANNEL_SECRET` | Channel secret（Basic settings 分頁） |
| `LINE_CHANNEL_ACCESS_TOKEN` | Channel access token（Messaging API 分頁） |
| `DATABASE_URL` | PostgreSQL 連線字串 |
| `ANTHROPIC_API_KEY` | Claude API key；留空就只用關鍵字比對，也不能用 AI 讀圖 |
| `BOT_AI_MODEL` | 預設 `claude-haiku-5-5` |
| `BOT_AI_EFFORT` | 預設 `low`（聊天最快、最便宜） |
| `BOT_VISION_MODEL` | AI 讀圖用的模型，預設 `claude-sonnet-5-5` |
| `BOT_ADMIN_PASSWORD` | 後台密碼；沒設定 = 後台關閉 |
| `BOT_ADMIN_ALLOW_REMOTE` | `true` = 允許從別台電腦或 tunnel 網址開後台（預設只能在本機開） |
| `BOT_CONTENT_DIR` | 「補習班資料」資料夾位置，預設是專案裡的 `補習班資料`；`off` = 不用資料夾同步 |
| `BOT_CONTENT_POLL_SECONDS` | 每幾秒檢查一次資料夾，預設 30 |
| `BOT_AI_DRAFTS` | 只丟圖片時自動產生 AI 文字草稿，預設 `true`（要有 API key） |
| `BOT_PUBLIC_BASE_URL` | bot 對外的 https 網址；留空會從 LINE 的請求自動判斷 |
| `BOT_AUTO_MIGRATE` | 啟動時自動升級資料庫，預設 `true` |
| `SCHOOL_NAME`、`FEE_UNIT`、`HUMAN_PAUSE_MINUTES` | 補習班名稱、學費單位、轉真人暫停幾分鐘 |

bot 自己的設定都用 `BOT_` 開頭，避免和電腦上其他工具的環境變數撞名。

## 測試

```bash
createdb gaoming_test                 # 名稱一定要有 test，測試會清空對話紀錄、學生資料
TEST_DATABASE_URL=postgresql://postgres:密碼@localhost:5432/gaoming_test pytest
```

以前建立的測試資料庫會自動升級到最新版。
網頁版展示把判斷邏輯和固定回覆用 JavaScript 重寫了一份；`tests/test_web_demo.py` 會在 Chromium 裡逐題比對兩邊的回覆。
改了 `app/replies.py`、`app/knowledge.py` 或 `app/bot.py` 的關鍵字比對時，記得一起改 `demo/web_demo_template.html`。
`tests/test_admin_ui.py` 會在 Chromium 裡把後台每一頁打開、發一則公告、發一組綁定碼。沒裝 Chromium 時這兩個測試會自動略過
（要跑的話：`playwright install chromium`）。
