"""
停車場預約 Agent v2 (Discord 版)
偵測 https://pcc.youparking.com.tw/parkingreserve/#/
當 TARGET_DATES 中的日期出現可預約按鈕時：
  1. 發送「可以預約」通知 (Discord)
  2. 自動填入資料並送出
  3. 發送「預約成功/失敗」通知 (Discord)

安裝：
    pip install playwright requests
    playwright install chromium

執行：
    python parking_agent_v2.py
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

# Discord Webhook URL（從環境變數讀取）
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# 個人資料（從環境變數讀取）
BOOKER_NAME  = os.environ["BOOKER_NAME"]    # 姓名
BOOKER_PLATE = os.environ["BOOKER_PLATE"]   # 車牌號碼

# ─────────────────────────────────────────────
# 反封鎖：隨機組合 User-Agent / Viewport
# ─────────────────────────────────────────────

_UA_OS = [
    "Windows NT 10.0; Win64; x64",
    "Windows NT 11.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
    "Macintosh; Intel Mac OS X 13_4",
    "Macintosh; Intel Mac OS X 14_0",
    "X11; Linux x86_64",
    "X11; Ubuntu; Linux x86_64",
]

_UA_CHROME_VERSIONS = list(range(118, 126))
_UA_FIREFOX_VERSIONS = list(range(118, 127))
_UA_SAFARI_VERSIONS = [
    ("605.1.15", "17.0"),
    ("605.1.15", "17.2"),
    ("605.1.15", "17.4.1"),
    ("605.1.15", "17.5"),
]
_UA_WEBKIT_BUILD = list(range(530, 538))

def _random_user_agent() -> str:
    browser = random.choices(["chrome", "firefox", "safari"], weights=[65, 25, 10])[0]
    os_str  = random.choice(_UA_OS)

    if browser == "chrome":
        major   = random.choice(_UA_CHROME_VERSIONS)
        minor   = random.randint(0, 9)
        webkit  = f"537.{random.choice(_UA_WEBKIT_BUILD)}"
        return (
            f"Mozilla/5.0 ({os_str}) "
            f"AppleWebKit/{webkit} (KHTML, like Gecko) "
            f"Chrome/{major}.0.{random.randint(5000,7000)}.{minor} "
            f"Safari/{webkit}"
        )
    elif browser == "firefox":
        major = random.choice(_UA_FIREFOX_VERSIONS)
        minor = random.randint(0, 3)
        return (
            f"Mozilla/5.0 ({os_str}; rv:{major}.{minor}) "
            f"Gecko/20100101 Firefox/{major}.{minor}"
        )
    else:
        mac_os = random.choice([s for s in _UA_OS if "Macintosh" in s])
        webkit_ver, safari_ver = random.choice(_UA_SAFARI_VERSIONS)
        return (
            f"Mozilla/5.0 ({mac_os}) "
            f"AppleWebKit/{webkit_ver} (KHTML, like Gecko) "
            f"Version/{safari_ver} Safari/{webkit_ver}"
        )

def _random_viewport():
    presets = [
        {"width": 1920, "height": 1080},
        {"width": 1440, "height": 900},
        {"width": 1366, "height": 768},
        {"width": 1280, "height": 800},
        {"width": 1536, "height": 864},
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
# Discord Webhook 通知函式
# ─────────────────────────────────────────────

def send_discord_notification(title: str, description: str, color: int = 3447003):
    """
    發送 Discord Webhook 通知
    color 參考值：藍色(處理中)=3447003, 綠色(成功)=3066993, 紅色/橙色(失敗)=15158332
    """
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
    title = f"🚗 停車場預約通知 | 發現車位"
    desc = f"【{target_date} 停車位可以預約！】\n偵測時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n正在自動填單送出，請稍候..."
    send_discord_notification(title, desc, color=3447003) # 藍色


def notify_booked_success(target_date: str):
    title = f"✅ 停車預約成功！"
    desc = f"【{target_date} 預約成功！】\n停放天數：{PARKING_DAYS} 天\n完成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    send_discord_notification(title, desc, color=3066993) # 綠色


def notify_booked_failed(target_date: str, reason: str):
    title = f"⚠️ 自動預約失敗，請手動操作！"
    desc = f"【{target_date} 自動預約失敗】\n原因：{reason}\n請立即手動前往：https://pcc.youparking.com.tw/parkingreserve/#/"
    send_discord_notification(title, desc, color=15158332) # 紅色


def notify_already_booked_confirmed(target_date: str):
    title = f"✅ 停車預約已確認存在！"
    desc = f"【{target_date} 已有預約記錄！】\n送出時提示已登記，查詢記錄確認存在。\n確認時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    send_discord_notification(title, desc, color=3066993) # 綠色

# ─────────────────────────────────────────────
# 預約記錄驗證
# ─────────────────────────────────────────────

async def verify_booking(page, target_date: str) -> bool:
    """
    預約完成後，前往查詢記錄確認是否真的成功。
    """
    try:
        log.info(f"開始驗證 {target_date} 的預約記錄...")
        await page.goto(
            "https://pcc.youparking.com.tw/parkingreserve/#/",
            wait_until="networkidle",
            timeout=20_000,
        )
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
        log.warning(f"⚠️ 查詢記錄中未找到 {date_fragment}，頁面片段：{page_text[:200]!r}")
        return False
    except Exception as e:
        log.error(f"驗證預約記錄例外：{e}", exc_info=True)
        return False

# ─────────────────────────────────────────────
# 核心檢查與自動預約
# ─────────────────────────────────────────────

async def check_and_book_for_date(target_date: str) -> str:
    """
    針對單一日期進行檢查與預約。
    回傳值：
      - "SUCCESS": 成功預約或已確認有預約記錄 (此日期搞定)
      - "FAILED": 當次流程判定失敗（但已發送失敗通知，不需要再進下一輪重試此日期）
      - "RETRY": 尚未開放、已滿或送出前逾時，需等待下一輪再試
    """
    ua       = _random_user_agent()
    viewport = _random_viewport()
    log.info(f"檢查日期 {target_date} | UA: ...{ua[-40:]} | {viewport['width']}x{viewport['height']}")

    submitted = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=_jitter(80))
        context = await browser.new_context(
            user_agent=ua, viewport=viewport, locale="zh-TW", timezone_id="Asia/Taipei"
        )
        page = await context.new_page()
        try:
            try:
                await page.goto(
                    "https://pcc.youparking.com.tw/parkingreserve/#/",
                    wait_until="networkidle",
                    timeout=30_000,
                )
            except AsyncPlaywrightTimeoutError:
                log.warning("導航逾時或被重新導向，本輪跳過重試")
                return "RETRY"

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

            # ── 找目標日期列 ──
            target_row = page.locator(f"tr:has(td:has-text('{target_date}'))").first
            if await target_row.count() == 0:
                log.warning(f"找不到 {target_date} 的列，頁面可能尚未開放")
                return "RETRY"

            is_full     = await target_row.locator(":has-text('已滿')").count() > 0
            is_bookable = await target_row.locator("button, a").filter(has_text="預約").count() > 0

            if is_full:
                log.info(f"❌ {target_date} 已滿")
                return "RETRY"

            if not is_bookable:
                log.warning(f"⚠️ {target_date} 狀態未知")
                return "RETRY"
            
            # ── 可預約：發送 Discord 通知 ──
            log.info(f"✅ {target_date} 可以預約！開始自動填單...")
            notify_available(target_date)

            # ── 填單流程 ──
            book_btn = target_row.locator("button, a").filter(has_text="預約").first
            await book_btn.click()
            await page.wait_for_timeout(_jitter(800))

            days_field = page.get_by_role("textbox", name="停放天數")
            await days_field.click()
            await days_field.fill(str(PARKING_DAYS))
            await page.wait_for_timeout(_jitter(200))

            name_field = page.get_by_role("textbox
