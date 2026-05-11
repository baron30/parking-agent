# 停車場預約偵測 Agent

自動偵測 [pcc.youparking.com.tw](https://pcc.youparking.com.tw/parkingreserve/#/) 的指定日期是否開放預約，  
一旦出現可預約按鈕，立即透過 **LINE** 和 **Gmail** 通知。

透過 **GitHub Actions** 免費雲端排程，每分鐘偵測一次，本機不需要一直開著。

---

## 運作原理

```
GitHub Actions cron（每 5 分鐘觸發）
  └─ 程式內部執行 5 輪，每輪間隔 ~60 秒
       └─ 實質每分鐘偵測一次
```

每次偵測流程：
1. 開啟 Playwright 無頭瀏覽器（隨機 User-Agent + 解析度，避免被封鎖）
2. 點擊「前往」→ 勾選同意條款 → 點擊「前往預約」
3. 找到目標日期那一列，判斷狀態欄是「已滿」還是預約按鈕
4. 若可預約 → 發送 LINE push message + Gmail 給所有收件人

---

## 檔案結構

```
.
├── .github/workflows/parking.yml   # GitHub Actions 排程設定
├── parking_book/
│   ├── parking_agent_v2.py         # 主程式（正式執行用）
│   ├── parking_agent.ipynb         # 偵錯用 notebook
│   ├── parking_agent.py            # 舊版（保留備用）
│   └── .env.example                # 環境變數範本
└── README.md
```

---

## 快速部署（GitHub Actions）

### 1. Fork 或 clone 此 repo

```bash
git clone https://github.com/k0341055/parking-agent.git
```

### 2. 修改目標日期

編輯 [parking_book/parking_agent_v2.py](parking_book/parking_agent_v2.py) 第 32 行：

```python
TARGET_DATE = "05-23"   # 改成你要偵測的月-日，需符合頁面格式 "2026-05-23 (六)"
```

### 3. 設定 GitHub Secrets

進入 repo → **Settings → Secrets and variables → Actions → New repository secret**，新增以下 5 個：

| Secret 名稱 | 說明 |
|-------------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API Channel Access Token |
| `LINE_USER_ID` | 接收通知的 LINE User ID（U 開頭） |
| `GMAIL_SENDER` | 寄件 Gmail |
| `GMAIL_PASSWORD` | Gmail 應用程式密碼（非登入密碼） |
| `GMAIL_RECIPIENTS` | 收件人，多人以逗號分隔，例如 `a@gmail.com,b@gmail.com` |

### 4. 確認 Actions 已啟用

進入 repo → **Actions** → 確認 workflow 已啟用（若顯示 disabled 請點 Enable）

---

## 本機偵錯

### 安裝環境

```bash
pip install playwright requests python-dotenv
playwright install chromium
```

### 建立 .env

```bash
cp parking_book/.env.example parking_book/.env
# 編輯 .env 填入真實值
```

### 執行單次檢查

```bash
cd parking_book
python parking_agent_v2.py
```

### 互動式逐步偵錯

開啟 `parking_book/parking_agent.ipynb`，依序執行 Step 1 → 2 → 2b → 3 → 4 → 4b，  
可以看到瀏覽器畫面、每個 selector 的命中狀況。

---

## LINE Messaging API 申請

1. 前往 [LINE Developers Console](https://developers.line.biz/console/) 登入
2. 建立 Provider → 建立新 Channel → 選「Messaging API」
3. Channel → **Messaging API** 頁籤 → 拉到最底 → **Issue** Channel access token → 複製
4. 用 LINE 掃 QR code 加入自己的官方帳號，傳一則訊息給它
5. 同頁面底部 **Your user ID** 即為 `LINE_USER_ID`（格式：`U` + 32 碼英數）

> 免費方案每月 200 則 Push Message，每分鐘偵測一次每天最多 1440 次，請注意用量。

---

## Gmail 應用程式密碼申請

1. Google 帳號 → 安全性 → 搜尋「應用程式密碼」（需先開啟兩步驟驗證）
2. 新增應用程式密碼，名稱自定 → 取得 16 碼密碼
3. 填入 `GMAIL_PASSWORD` Secret

---

## 注意事項

- `parking_agent.ipynb` 含本機偵錯用的明文設定，**請勿上傳到 GitHub**
- `.env` 已在 `.gitignore` 中，不會被上傳
- GitHub Actions cron 排程有時會延遲 5～30 分鐘，屬正常現象
