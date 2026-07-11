"""
停車場預約 Agent v3 (Discord 版 / 直接進入 reservedindex/1 / 全面重構版)

主要變更 (相較 v2)：
  - 不再從首頁出發，直接進入 https://pcc.youparking.com.tw/parkingreserve/#/reservedindex/1
  - Browser / Context / Page 僅建立一次，全程重複使用，只做 reload，不再每輪重開瀏覽器
  - 全面改用 get_by_text() / locator(filter) / xpath ancestor 尋找元素，不依賴 <tr>/<td> 結構
  - 新增 safe_click()：scroll_into_view → hover → click（重試3次）→ force click →
    dispatch_event("click") → JS evaluate click，逐層降級直到成功或全部失敗
  - Checkbox 尋找順序：input[type=checkbox] → label 文字 → CSS fallback
  - 「前往預約」按鈕具備多重 fallback（role button / role link / text / css）
  - 日期比對同時支援 07-23 / 07/23 / 2026-07-23 / 2026/07/23 等格式
  - 預約按鈕文字支援「立即預約」「我要預約」「預約」等變體
  - 遇到 Cloudflare「Checking your browser / Just a moment」會等待通過，不會直接失敗
  - 所有日期皆已滿時「不會結束程式」，而是 reload 並等待 18~25 秒亂數後重新搜尋
  - 任何 Timeout 不會直接判定失敗，而是導向下一輪 reload 重試
  - 送出後判斷成功／已預約，皆會呼叫 verify_booking() 雙重驗證，驗證失敗才會發送
    Discord 失敗通知，並自動截圖存到 screenshot/error_時間.png
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

# 多日期依序預約清單 (模糊比對，支援 MM-DD / MM/DD / YYYY-MM-DD / YYYY/MM/DD)
TARGET_DATES = ["07-23", "07-24"]
PARKING_DAYS = int(os.environ.get("PARKING_DAYS", "5"))   # 停放天數

# GitHub Actions 模式：本次執行最多跑幾輪 (每輪之間若無結果會等待 18~25 秒 reload)
ROUNDS = int(os.environ.get("CHECK_ROUNDS", "1"))

# Discord Webhook URL 與個人預約資料（從環境變數讀取）
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
BOOKER_NAME         = os.environ["BOOKER_NAME"]
BOOKER_PLATE        = os.environ["BOOKER_PLATE"]

# 直接進入的預約入口 (v3 新流程)
RESERVE_ENTRY_URL = "https://pcc.youparking.com.tw/parkingreserve/#/reservedindex/1"
HOME_URL          = "https://pcc.youparking.com.tw/parkingreserve/#/"

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
    os_str = random.choice(_UA_OS)

    if browser == "chrome":
        major = random.choice(_UA_CHROME_VERSIONS)
        minor = random.randint(0, 9)
        webkit = f"537.{random.choice(_UA_WEBKIT_BUILD)}"
        return (
            f"Mozilla/5.0 ({os_str}) AppleWebKit/{webkit} (KHTML, like Gecko) "
            f"Chrome/{major}.0.{random.randint(5000, 7000)}.{minor} Safari/{webkit}"
        )
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


def _random_refresh_wait_seconds() -> float:
    """已滿 / 找不到日期時，refresh 前等待 18~25 秒亂數"""
    return random.uniform(18, 25)


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
# Discord Webhook 通知 (格式維持不變)
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
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            ]
        }
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=10,
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
        color=3447003,
    )


def notify_booked_success(target_date: str):
    send_discord_notification(
        title="✅ 停車預約成功！",
        description=f"【{target_date} 預約成功！】\n停放天數：{PARKING_DAYS} 天\n完成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        color=3066993,
    )


def notify_booked_failed(target_date: str, reason: str, screenshot_path: str = None):
    desc = (
        f"【{target_date} 自動預約失敗】\n原因：{reason}\n"
        f"請立即手動前往：{HOME_URL}"
    )
    if screenshot_path:
        desc += f"\n錯誤截圖：{screenshot_path}"
    send_discord_notification(
        title="⚠️ 自動預約失敗，請手動操作！",
        description=desc,
        color=15158332,
    )


def notify_already_booked_confirmed(target_date: str):
    send_discord_notification(
        title="✅ 停車預約已確認存在！",
        description=f"【{target_date} 已有預約記錄！】\n送出時提示已登記，查詢記錄確認存在。\n確認時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        color=3066993,
    )


# ─────────────────────────────────────────────
# 共用例外 / 重試工具
# ─────────────────────────────────────────────

class LocatorTimeoutError(Exception):
    """locator 重試多次後仍逾時"""


async def wait_for_locator(locator, timeout_ms: int = 3000, retries: int = 3,
                            state: str = "visible", description: str = "") -> bool:
    """所有 locator 等待統一走這裡：timeout 3 秒，失敗 retry 最多 3 次"""
    for attempt in range(1, retries + 1):
        try:
            await locator.wait_for(state=state, timeout=timeout_ms)
            return True
        except AsyncPlaywrightTimeoutError:
            log.warning(f"⏳ 等待「{description or 'locator'}」逾時 (第 {attempt}/{retries} 次)")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.warning(f"⏳ 等待「{description or 'locator'}」發生例外 (第 {attempt}/{retries} 次): {e}")
            await asyncio.sleep(0.5)
    log.error(f"❌ 等待「{description or 'locator'}」已達重試上限，判定逾時")
    raise LocatorTimeoutError(description or "locator")


async def safe_click(locator, description: str = "") -> bool:
    """
    逐層降級點擊：
    scroll_into_view → hover → click()（重試3次）→ force=True → dispatch_event → JS click
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    try:
        await locator.hover(timeout=2000)
    except Exception:
        pass

    for attempt in range(1, 4):
        try:
            await locator.click(timeout=3000)
            log.info(f"✅ safe_click 成功「{description}」(click, 第{attempt}次)")
            return True
        except Exception as e:
            log.warning(f"⚠️ safe_click click() 失敗「{description}」第{attempt}次: {e}")
            await asyncio.sleep(0.5)

    try:
        await locator.click(force=True, timeout=3000)
        log.info(f"✅ safe_click 成功「{description}」(force click)")
        return True
    except Exception as e:
        log.warning(f"⚠️ safe_click force click 失敗「{description}」: {e}")

    try:
        await locator.dispatch_event("click")
        log.info(f"✅ safe_click 成功「{description}」(dispatch_event)")
        return True
    except Exception as e:
        log.warning(f"⚠️ safe_click dispatch_event 失敗「{description}」: {e}")

    try:
        await locator.evaluate("el => el.click()")
        log.info(f"✅ safe_click 成功「{description}」(JS evaluate click)")
        return True
    except Exception as e:
        log.error(f"❌ safe_click 完全失敗「{description}」: {e}")
        return False


CLOUDFLARE_MARKERS = [
    "Checking your browser",
    "Just a moment",
    "cf-browser-verification",
    "Verifying you are human",
]


async def wait_for_cloudflare(page, max_wait_sec: int = 30) -> bool:
    """若偵測到 Cloudflare 驗證頁面，等待通過再繼續，不直接判定失敗"""
    start = datetime.now()
    while (datetime.now() - start).total_seconds() < max_wait_sec:
        try:
            body_text = await page.inner_text("body", timeout=3000)
        except Exception:
            body_text = ""
        if any(marker in body_text for marker in CLOUDFLARE_MARKERS):
            log.info("🛡️ 偵測到 Cloudflare 驗證頁面，等待通過...")
            await page.wait_for_timeout(2000)
            continue
        return True
    log.warning("⚠️ Cloudflare 驗證等待逾時，仍嘗試繼續流程")
    return False


def generate_date_variants(target_date: str, year: int = None) -> list:
    """
    產生日期比對用的所有格式變體：
    07-23 / 07/23 / 2026-07-23 / 2026/07/23
    """
    year = year or datetime.now().year
    m_full = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", target_date)
    if m_full:
        yyyy, mm, dd = m_full.groups()
    else:
        m_short = re.search(r"(\d{1,2})[-/](\d{1,2})", target_date)
        if not m_short:
            return [target_date]
        mm, dd = m_short.groups()
        yyyy = str(year)

    mm = mm.zfill(2)
    dd = dd.zfill(2)

    variants = {
        f"{mm}-{dd}",
        f"{mm}/{dd}",
        f"{yyyy}-{mm}-{dd}",
        f"{yyyy}/{mm}/{dd}",
    }
    return list(variants)


async def take_error_screenshot(page, target_date: str):
    try:
        os.makedirs("screenshot", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_date = re.sub(r"[^\w-]", "_", target_date)
        path = f"screenshot/error_{ts}_{safe_date}.png"
        await page.screenshot(path=path, full_page=True)
        log.info(f"📸 已儲存錯誤截圖：{path}")
        return path
    except Exception as e:
        log.error(f"截圖失敗: {e}")
        return None


# ─────────────────────────────────────────────
# 頁面導覽：進入 reservedindex/1 → 同意條款 → 前往預約
# ─────────────────────────────────────────────

async def check_agreement_checkbox(page) -> bool:
    log.info("🔍 尋找「我已閱讀並同意」勾選框...")

    # 1. 優先 input[type=checkbox]
    try:
        checkbox = page.locator("input[type='checkbox']").first
        if await checkbox.count() > 0:
            await wait_for_locator(checkbox, description="同意勾選框(input)", state="attached")
            is_checked = False
            try:
                is_checked = await checkbox.is_checked()
            except Exception:
                pass
            if not is_checked:
                if await safe_click(checkbox, "同意勾選框(input)"):
                    log.info("✅ 勾選完成 (input[type=checkbox])")
                    return True
            else:
                log.info("✅ 勾選框本已勾選 (input[type=checkbox])")
                return True
    except Exception as e:
        log.warning(f"⚠️ input checkbox 方式失敗: {e}")

    # 2. 找 label 文字點擊
    try:
        label = page.get_by_text(re.compile("我已閱讀並同意|已閱讀並同意")).first
        if await label.count() > 0:
            await wait_for_locator(label, description="同意勾選框(label)")
            if await safe_click(label, "同意勾選框(label)"):
                log.info("✅ 勾選完成 (label 文字點擊)")
                return True
    except Exception as e:
        log.warning(f"⚠️ label 點擊方式失敗: {e}")

    # 3. 最後 CSS fallback（舊版 vuetify ripple 結構）
    try:
        fallback = page.locator(
            ".v-input--selection-controls__ripple, .v-selection-control__wrapper, .v-checkbox"
        ).first
        if await fallback.count() > 0:
            await wait_for_locator(fallback, description="同意勾選框(CSS fallback)")
            if await safe_click(fallback, "同意勾選框(CSS fallback)"):
                log.info("✅ 勾選完成 (CSS fallback)")
                return True
    except Exception as e:
        log.error(f"❌ CSS fallback 也失敗: {e}")

    log.error("❌ 無法找到或點擊同意勾選框")
    return False


async def click_proceed_to_booking(page) -> bool:
    log.info("🔍 尋找「前往預約」按鈕...")
    candidates = [
        ("role_button", lambda: page.get_by_role("button", name="前往預約")),
        ("role_link", lambda: page.get_by_role("link", name="前往預約")),
        ("text", lambda: page.get_by_text("前往預約")),
        ("css_button", lambda: page.locator("button:has-text('前往預約')")),
        ("css_a", lambda: page.locator("a:has-text('前往預約')")),
    ]
    for name, make_loc in candidates:
        try:
            loc = make_loc().first
            if await loc.count() > 0:
                await wait_for_locator(loc, description=f"前往預約({name})")
                if await safe_click(loc, f"前往預約({name})"):
                    log.info(f"✅ 前往預約按鈕點擊成功 ({name})")
                    return True
        except Exception as e:
            log.warning(f"⚠️ 前往預約 fallback [{name}] 失敗: {e}")
            continue
    log.error("❌ 所有前往預約按鈕嘗試均失敗")
    return False


async def navigate_to_reservation_list(page) -> bool:
    log.info(f"🌐 進入預約頁面 {RESERVE_ENTRY_URL} ...")
    try:
        await page.goto(RESERVE_ENTRY_URL, wait_until="domcontentloaded", timeout=30_000)
    except AsyncPlaywrightTimeoutError:
        log.warning("⚠️ 導航逾時")
        return False

    await wait_for_cloudflare(page)
    await page.wait_for_timeout(_jitter(1000))
    log.info("📄 DOM 已載入，開始尋找同意勾選框")

    if not await check_agreement_checkbox(page):
        return False
    await page.wait_for_timeout(_jitter(400))

    if not await click_proceed_to_booking(page):
        return False

    await wait_for_cloudflare(page)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except AsyncPlaywrightTimeoutError:
        pass

    log.info("✅ 已進入預約清單頁面")
    return True


# ─────────────────────────────────────────────
# 日期搜尋 / 狀態判斷 (不依賴 table 結構)
# ─────────────────────────────────────────────

async def _locate_date_text(page, variants: list, retries: int = 3, delay_ms: int = 1000):
    """對多個日期格式變體嘗試尋找文字節點，具重試機制"""
    for attempt in range(1, retries + 1):
        for variant in variants:
            try:
                loc = page.get_by_text(variant, exact=False).first
                if await loc.count() > 0:
                    return loc, variant
            except Exception:
                continue
        if attempt < retries:
            await page.wait_for_timeout(delay_ms)
    return None, None


async def find_date_status(page, target_date: str):
    """
    回傳 (status, book_button_locator)
    status: "bookable" / "full" / "notfound"
    """
    variants = generate_date_variants(target_date)
    log.info(f"🔍 搜尋日期 {target_date} (比對格式: {variants}) ...")

    date_loc, matched = await _locate_date_text(page, variants)
    if date_loc is None:
        log.warning(f"⚠️ 找不到 {target_date} 相關文字")
        return "notfound", None

    log.info(f"✅ 找到日期文字：{matched}")

    # 以 xpath ancestor 找出最近的「卡片/區塊」容器 (內含按鈕或連結)，不依賴 tr/td
    container = date_loc.locator("xpath=ancestor::*[.//button or .//a][1]")
    try:
        if await container.count() == 0:
            container = date_loc.locator("xpath=ancestor::div[1]")
    except Exception:
        container = date_loc.locator("xpath=ancestor::div[1]")

    try:
        container_text = await container.first.inner_text(timeout=3000)
    except Exception:
        container_text = ""

    if "已滿" in container_text or "額滿" in container_text:
        log.info(f"❌ {target_date} 已滿")
        return "full", None

    book_pattern = re.compile("立即預約|我要預約|預約")
    book_btn = container.first.locator("button, a").filter(has_text=book_pattern).first
    try:
        btn_count = await book_btn.count()
    except Exception:
        btn_count = 0

    if btn_count == 0:
        log.warning(f"⚠️ {target_date} 找不到可點擊的預約按鈕，狀態未知")
        return "notfound", None

    log.info(f"✅ {target_date} 可以預約！")
    return "bookable", book_btn


# ─────────────────────────────────────────────
# 填單送出 / 結果判斷
# ─────────────────────────────────────────────

async def _fill_textbox(page, name_options: list, value: str, field_desc: str) -> bool:
    for name in name_options:
        try:
            loc = page.get_by_role("textbox", name=name)
            if await loc.count() > 0:
                target = loc.first
                await wait_for_locator(target, description=field_desc)
                await safe_click(target, field_desc)
                await target.fill(value)
                log.info(f"✅ 已填寫「{field_desc}」")
                return True
        except Exception as e:
            log.warning(f"⚠️ 填寫「{field_desc}」失敗 (name={name}): {e}")
            continue
    log.error(f"❌ 無法填寫「{field_desc}」")
    return False


async def fill_and_submit_booking(page, book_btn, target_date: str) -> str:
    """回傳 'success' / 'already' / 'unknown'"""
    notify_available(target_date)

    log.info("🖱️ 點擊預約按鈕，開始填單...")
    if not await safe_click(book_btn, "預約按鈕"):
        return "unknown"

    await page.wait_for_timeout(_jitter(800))
    await wait_for_cloudflare(page)

    await _fill_textbox(page, ["停放天數"], str(PARKING_DAYS), "停放天數")
    await page.wait_for_timeout(_jitter(200))

    await _fill_textbox(page, ["姓名"], BOOKER_NAME, "姓名")
    await page.wait_for_timeout(_jitter(200))

    await _fill_textbox(
        page,
        ["車牌號碼 (例: AA-1234)", "車牌號碼"],
        BOOKER_PLATE,
        "車牌號碼",
    )
    await page.wait_for_timeout(_jitter(200))

    submit_btn = page.get_by_role("button", name="送出")
    try:
        if await submit_btn.count() == 0:
            submit_btn = page.locator("button:has-text('送出')")
    except Exception:
        submit_btn = page.locator("button:has-text('送出')")

    if not await safe_click(submit_btn.first, "送出按鈕"):
        log.error("❌ 送出按鈕點擊失敗")
        return "unknown"

    log.info("📨 已送出表單，等待結果彈窗...")
    for _kw in ["您已完成線上預約登記", "已登記預約", "登記預約"]:
        try:
            await page.wait_for_selector(f"text={_kw}", timeout=8_000)
            break
        except AsyncPlaywrightTimeoutError:
            pass

    page_text = await page.inner_text("body")

    try:
        close_btn = page.get_by_role("button").nth(2)
        if await close_btn.count() > 0 and await close_btn.is_visible(timeout=3000):
            await safe_click(close_btn, "結果彈窗關閉按鈕")
            await page.wait_for_timeout(_jitter(1000))
    except Exception:
        pass

    if "您已完成線上預約登記" in page_text:
        log.info("✅ 頁面顯示「您已完成線上預約登記」")
        return "success"
    elif re.search(rf"車號\s*\[{re.escape(BOOKER_PLATE)}\].*?已於.*?登記預約", page_text, re.DOTALL):
        log.info("✅ 頁面顯示車號已於先前登記預約")
        return "already"
    else:
        log.warning("⚠️ 送出後未偵測到明確結果")
        return "unknown"


# ─────────────────────────────────────────────
# 預約記錄雙重驗證
# ─────────────────────────────────────────────

async def verify_booking(page, target_date: str) -> bool:
    """前往查詢記錄頁面，雙重確認預約是否存在"""
    log.info(f"🔎 開始雙重驗證 {target_date} 的預約記錄...")
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20_000)
        await wait_for_cloudflare(page)
        await page.wait_for_timeout(_jitter(800))

        enter_link = page.get_by_role("link", name="前往").first
        try:
            if await enter_link.count() == 0:
                enter_link = page.get_by_text("前往").first
        except Exception:
            enter_link = page.get_by_text("前往").first

        await wait_for_locator(enter_link, description="首頁「前往」連結")
        await safe_click(enter_link, "首頁「前往」連結")
        await page.wait_for_timeout(_jitter(600))

        record_entry = page.get_by_text("預約記錄").first
        if await record_entry.count() == 0:
            log.warning("⚠️ 找不到「預約記錄」入口")
            return False

        record_container = record_entry.locator("xpath=ancestor::*[.//a or .//button][1]")
        record_link = record_container.get_by_role("link", name="前往").first
        try:
            if await record_link.count() == 0:
                record_link = record_container.locator("a, button").filter(has_text="前往").first
        except Exception:
            record_link = record_container.locator("a, button").filter(has_text="前往").first

        await wait_for_locator(record_link, description="預約記錄「前往」連結")
        await safe_click(record_link, "預約記錄「前往」連結")
        await page.wait_for_timeout(_jitter(800))

        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except AsyncPlaywrightTimeoutError:
            pass

        plate_field = page.get_by_role("textbox", name="車號 (例: AA-1234)")
        try:
            if await plate_field.count() == 0:
                plate_field = page.get_by_role("textbox", name="車號")
        except Exception:
            plate_field = page.get_by_role("textbox", name="車號")

        await wait_for_locator(plate_field.first, description="車號輸入框")
        await safe_click(plate_field.first, "車號輸入框")
        await plate_field.first.fill(BOOKER_PLATE)

        query_btn = page.get_by_role("button", name="查 詢")
        try:
            if await query_btn.count() == 0:
                query_btn = page.get_by_role("button", name="查詢")
        except Exception:
            query_btn = page.get_by_role("button", name="查詢")

        await safe_click(query_btn.first, "查詢按鈕")
        await page.wait_for_timeout(_jitter(1500))

        page_text = await page.inner_text("body")
        for variant in generate_date_variants(target_date):
            if variant in page_text:
                log.info(f"✅ 預約記錄確認：找到 {variant}")
                return True

        log.warning(f"⚠️ 查詢記錄中未找到任何日期格式")
        return False
    except LocatorTimeoutError as e:
        log.error(f"驗證預約記錄逾時: {e}")
        return False
    except Exception as e:
        log.error(f"驗證預約記錄例外：{e}", exc_info=True)
        return False


# ─────────────────────────────────────────────
# 單一日期檢查 + 預約
# ─────────────────────────────────────────────

async def check_and_book_for_date(page, target_date: str) -> bool:
    """
    回傳 True 代表此日期已有明確結果 (成功 / 已預約 / 失敗)，不需再重試。
    回傳 False 代表此日期本輪未成功處理 (已滿 / 找不到 / 逾時)，下一輪需重新檢查。
    """
    status, book_btn = await find_date_status(page, target_date)

    if status == "full":
        return False
    if status == "notfound":
        return False

    log.info(f"🚀 {target_date} 開始填單...")
    result = await fill_and_submit_booking(page, book_btn, target_date)

    if result in ("success", "already"):
        log.info(f"⏳ 等待雙重驗證 {target_date} ...")
        verified = await verify_booking(page, target_date)
        if verified:
            log.info(f"✅ {target_date} 驗證成功")
            if result == "success":
                notify_booked_success(target_date)
            else:
                notify_already_booked_confirmed(target_date)
        else:
            log.error(f"❌ {target_date} 雙重驗證失敗")
            shot = await take_error_screenshot(page, target_date)
            notify_booked_failed(
                target_date, "頁面顯示完成/已登記，但雙重驗證查詢失敗", shot
            )
        return True
    else:
        log.error(f"❌ {target_date} 送出後未偵測到明確結果")
        shot = await take_error_screenshot(page, target_date)
        notify_booked_failed(target_date, "送出後未偵測到明確結果，請手動確認", shot)
        return True


# ─────────────────────────────────────────────
# 主程式：Browser/Context/Page 只建立一次
# ─────────────────────────────────────────────

async def main():
    log.info(f"🚗 停車場預約 Agent v3 (Discord 版) | 目標日期：{TARGET_DATES} | 最大輪數：{ROUNDS}")
    completed_dates = set()

    async with async_playwright() as p:
        ua = _random_user_agent()
        viewport = _random_viewport()
        log.info(f"🧭 啟動瀏覽器 | UA: ...{ua[-40:]} | {viewport['width']}x{viewport['height']}")

        browser = await p.chromium.launch(headless=True, slow_mo=_jitter(80))
        context = await browser.new_context(
            user_agent=ua, viewport=viewport, locale="zh-TW", timezone_id="Asia/Taipei"
        )
        page = await context.new_page()

        try:
            round_num = 0
            while round_num < ROUNDS and len(completed_dates) < len(TARGET_DATES):
                round_num += 1
                active_dates = [d for d in TARGET_DATES if d not in completed_dates]
                log.info(f"── 第 {round_num}/{ROUNDS} 輪 ── (待處理: {active_dates})")

                entered = await navigate_to_reservation_list(page)
                if not entered:
                    log.warning("⚠️ 進入預約清單失敗，稍後於下一輪重試")
                    await page.wait_for_timeout(int(_random_refresh_wait_seconds() * 1000))
                    continue

                for d in active_dates:
                    try:
                        done = await check_and_book_for_date(page, d)
                    except LocatorTimeoutError as e:
                        log.warning(f"⚠️ {d} 檢查過程逾時，將於下一輪重試: {e}")
                        done = False
                    except Exception as e:
                        log.error(f"❌ {d} 檢查過程發生例外: {e}", exc_info=True)
                        done = False

                    if done:
                        completed_dates.add(d)

                if len(completed_dates) == len(TARGET_DATES):
                    log.info("🎉 所有目標日期皆已處理完畢！")
                    break

                if round_num < ROUNDS:
                    wait_sec = _random_refresh_wait_seconds()
                    log.info(f"😴 本輪目標日期皆已滿或未找到，等待 {wait_sec:.1f} 秒後重新整理頁面...")
                    await page.wait_for_timeout(int(wait_sec * 1000))
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=30_000)
                        await wait_for_cloudflare(page)
                    except AsyncPlaywrightTimeoutError:
                        log.warning("⚠️ Reload 逾時，下一輪將重新導航")

            log.info(f"🏁 所有輪次巡檢結束。最終完成狀態: {completed_dates}")
        finally:
            await browser.close()
            log.info("🧹 瀏覽器已關閉")


if __name__ == "__main__":
    asyncio.run(main())
