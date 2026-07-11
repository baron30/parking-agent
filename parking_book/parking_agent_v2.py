"""
停車場預約 Agent v2 (Discord 精簡多日期版)
偵測 https://pcc.youparking.com.tw/parkingreserve/#/
當 TARGET_DATES 中的日期出現可預約按鈕時：
  1. 發送「可以預約」通知 (Discord Webhook)
  2. 自動填入資料並送出
  3. 發送「預約成功/失敗」通知 (Discord Webhook)
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime

import requests
from playwright.async_api import async_playwright, TimeoutError as AsyncPlaywrightTimeoutError

# ─────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────

# 多日期依序預約清單 (頁面日期格式 "2026-07-23 (四)"，模糊比對)
TARGET_DATES  = ["07-23", "07-24"]   
PARKING_DAYS  = int(os.environ.get("PARKING_DAYS", "5"))   # 停放天數

# GitHub Actions 模式：每次 workflow 執行幾輪（每輪間隔 ~60 秒）
ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))

# Discord Webhook URL 與個人預約資料（從環境變數讀取）
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
BOOKER_NAME         = os.environ["BOOKER_NAME"]    
BOOKER_PLATE        = os.environ["BOOKER_PLATE"]   

# ─────────────────────────────────────────────
# 反封鎖環境隨機優化
# ─────────────────────────────────────────────

_UA_OS = [
    "Windows NT 10.0; Win64; x64",
    "Windows NT 11.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
    "Macintosh; Intel Mac OS X 14_0",
    "X11; Linux x86_64",
]

_UA_CHROME_VERSIONS = list(range(118, 126))
_UA_FIREFOX_VERSIONS = list(range(118, 127))
_UA_WEBKIT_BUILD = list(range(530, 538))

def _random_user_agent() -> str:
    browser = random.choices(["chrome", "firefox"], weights=[80, 20])[0]
    os_str  = random.choice(_UA_OS)

    if browser == "chrome":
        major   = random.choice(_UA_CHROME_VERSIONS)
        minor   = random.randint(0, 9)
        webkit  = f"537.{random.choice(_UA_WEBKIT_BUILD)}"
        return f"Mozilla/5.0 ({os_str}) AppleWebKit/{webkit} (KHTML, like Gecko) Chrome/{major}.0.{random.randint(5000,7000)}.{minor} Safari/{webkit}"
    else:
        major = random.choice(_UA_FIREFOX_VERSIONS)
        minor = random.randint(0, 3)
        return f"Mozilla/5.0 ({os_str}; rv:{major}.{minor}) Gecko/20100101 Firefox/{major}.{minor}"

def _random_viewport():
    presets = [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1366, "height": 768},
    ]
    return random.choice(presets)

def _jitter(base_ms: int, pct: float = 0.3) -> int:
    delta = int(base_ms * pct)
    return base_ms + random.randint(-delta, delta)

# ─────────────────────────────────────────────
# Logger
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Discord Webhook 通知
# ─────────────────────────────────────────────

def send_discord_notification(title: str, description: str, color: int = 3447003):
    """發送 Discord Webhook 卡片訊息 (藍色=3447003, 綠色=3066993, 紅色=15158332)"""
    try:
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
            ]
        }
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=10
        )
        if resp.status_code in [200, 204]:
            log.info("Discord 通知發送成功")
            return True
        log.error(f"Discord 通知失敗：{resp.status_code} {resp.text}")
        return False
    except Exception as e:
        log.error(f"Discord 通知發生例外：{e}")
        return False

def notify_available(target_date: str):
    send_discord_notification(
        title="🚗 停車場預約通知 | 發現車位",
        description=f"【{target_date} 停車位可以預約！】\n偵測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n正在自動填單送出，請稍候...",
        color=3447003
    )

def notify_booked_success(target_date: str):
    send_discord_notification(
        title="✅ 停車預約成功！",
        description=f"【{target_date} 預約成功！】\n停放天數：{PARKING_DAYS} 天\n完成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        color=3066993
    )

def notify_booked_failed(target_date: str, reason: str):
    send_discord_notification(
        title="⚠️ 自動預約失敗，請手動操作！",
        description=f"【{target_date} 自動預約失敗】\n原因：{reason}\n請立即手動前往：https://pcc.youparking.com.tw/parkingreserve/#/",
        color=15158332
    )

def notify_already_booked_confirmed(target_date: str):
    send_discord_notification(
        title="✅ 停車預約已確認存在！",
        description=f"【{target_date} 已有預約記錄！】\n送出時提示已登記，查詢記錄確認存在。\n確認時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        color=3066993
    )

# ─────────────────────────────────────────────
# 預約記錄雙重驗證
# ─────────────────────────────────────────────

async def verify_booking(page, target_date: str) -> bool:
    """前往查詢記錄頁面，雙重確認預約是否存在"""
    try:
        log.info(f"開始驗證 {target_date} 的預約記錄...")
        await page.goto("https://pcc.youparking.com.tw/parkingreserve/#/", wait_until="networkidle", timeout=20_000)
        await page.wait_for_timeout(_jitter(800))

        await page.get_by_role("link", name="前往").first.click()
        await page.wait_for_timeout(_jitter(600))

        record_row = page.locator("tr, li, div").filter(has_text="預約記錄").first
        if await record_row.count() == 0:
            log.warning("⚠️ 找不到「預約記錄」入口")
            return False
        await record_row.get_by_role("link", name="前往").click()
        await page.wait_for_timeout(_jitter(800))
        
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except AsyncPlaywrightTimeoutError:
            pass

        plate_field = page.get_by_role("textbox", name="車號 (例: AA-1234)")
        await plate_field.click()
        await plate_field.fill(BOOKER_PLATE)
        await page.get_by_role("button", name="查 詢").click()
        await page.wait_for_timeout(_jitter(1500))

        page_text = await page.inner_text("body")
        date_fragment = target_date.replace("-", "/")
        if date_fragment in page_text:
            log.info(f"✅ 預約記錄確認：找到 {date_fragment}")
            return True
        log.warning(f"⚠️ 查詢記錄中未找到 {date_fragment}")
        return False
    except Exception as e:
        log.error(f"驗證預約記錄例外：{e}", exc_info=True)
        return False

# ─────────────────────────────────────────────
# 核心自動化預約邏輯
# ─────────────────────────────────────────────

async def check_and_book_for_date(target_date: str) -> bool:
    """檢查單一日期，若成功或已決定結果回傳 True，需重複檢查回傳 False"""
    ua = _random_user_agent()
    viewport = _random_viewport()
    log.info(f"開始檢查 {target_date} | UA: ...{ua[-40:]} | {viewport['width']}x{viewport['height']}")

    submitted = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=_jitter(80))
        context = await browser.new_context(user_agent=ua, viewport=viewport, locale="zh-TW", timezone_id="Asia/Taipei")
        page = await context.new_page()
        try:
            try:
                await page.goto("https://pcc.youparking.com.tw/parkingreserve/#/", wait_until="networkidle", timeout=30_000)
            except AsyncPlaywrightTimeoutError:
                log.warning("導航逾時，本輪跳過")
                return False

            await page.wait_for_timeout(_jitter(800))
            await page.get_by_role("link", name="前往").first.click()
            await page.wait_for_timeout(_jitter(500))
            await page.locator(".v-input--selection-controls__ripple").click()
            await page.wait_for_timeout(_jitter(300))
            await page.get_by_role("button", name="前往預約").click()
            await page.wait_for_timeout(_jitter(1000))
            
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except AsyncPlaywrightTimeoutError:
                pass

            target_row = page.locator(f"tr:has(td:has-text('{target_date}'))").first
            if await target_row.count() == 0:
                log.warning(f"找不到 {target_date} 的列，可能尚未開放")
                return False

            is_full     = await target_row.locator(":has-text('已滿')").count() > 0
            is_bookable = await target_row.locator("button, a").filter(has_text="預約").count() > 0

            if is_full:
                log.info(f"❌ {target_date} 已滿")
                return False

            if not is_bookable:
                log.warning(f"⚠️ {target_date} 狀態未知")
                return False
            
            log.info(f"✅ {target_date} 可以預約！開始填單...")
            notify_available(target_date)

            book_btn = target_row.locator("button, a").filter(has_text="預約").first
            await book_btn.click()
            await page.wait_for_timeout(_jitter(800))

            await page.get_by_role("textbox", name="停放天數").fill(str(PARKING_DAYS))
            await page.wait_for_timeout(_jitter(200))

            await page.get_by_role("textbox", name="姓名").fill(BOOKER_NAME)
            await page.wait_for_timeout(_jitter(200))

            await page.get_by_role("textbox", name="車牌號碼 (例: AA-1234)").fill(BOOKER_PLATE)
            await page.wait_for_timeout(_
