import os
import re
import io
import json
import time
import html
import asyncio
import secrets
import zipfile
import logging
import threading
import requests
import httpx
import psycopg2
from urllib.parse import unquote
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== LOGGING ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ========== CONFIGURATION (Loaded from VPS systemd Environment) ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "6420446828"))
PORT = int(os.environ.get("PORT", "8000"))

SUPABASE_URI = os.environ.get("SUPABASE_URI", "")

# Xerodel Redeem Service Config
REDEEM_API_KEY = (
    os.environ.get("COOKIE_BOT_API_KEY")
    or os.environ.get("REDEEM_API_KEY")
    or ""
).strip()
REDEEM_BASE_URL = (
    os.environ.get("SERVICE_BASE_URL")
    or os.environ.get("BASE_URL")
    or "https://xerodel.new.tg"
).strip().rstrip("/")
VERIFY_PATH = "/api/redeem/verify"
REDEEM_TIMEOUT = 8

# Default Global HTTP Proxy (can be updated live via /addproxy)
GLOBAL_PROXY = os.environ.get("GLOBAL_PROXY", "")

# ========== MASTER TELEGRAM PREMIUM EMOJI MAP ==========
PREMIUM_EMOJI_MAP = {
    "📁": "5431420628978641216",  # Source / Order tag
    "✅": "5980930633298350051",  # Valid / Success
    "❌": "6086748658852695826",  # Error / Invalid
    "🔄": "5974235702701853774",  # Loading / Verifying
    "🔒": "5296369303661067030",  # Locked / Unauthorized
    "⚠️": "5213205860498549992",  # Warning
    "🆔": "6023796962417970481",  # User ID
    "📧": "5253742260054409879",  # Email
    "🎬": "6026351531656223022",  # Plan / Header
    "🌍": "6023847192060499006",  # Country
    "📅": "6014861794059752708",  # Expiry
    "🎉": "6023832365833392885",  # Promo / Order Claimed
    "🚫": "6032903688949862892",  # Revoked
    "🔑": "6026218958900695642",  # Promo Key
    "📖": "6181322172263308706",  # Help
    "🛑": "6181277564732972292",  # Stopped
    "🔱": "5785265173266698665",  # Add this line for your XERON credit footer
}

BTN_EMOJI_PC = "5235785929502761186"     # Paste custom_emoji_id for 💻 here
BTN_EMOJI_PHONE = "6026092115631543342"  # Paste custom_emoji_id for 📱 here
def pe(text: str) -> str:
    """Automatically converts every mapped standard emoji in an HTML string into a <tg-emoji> tag."""
    for std_emoji, custom_id in PREMIUM_EMOJI_MAP.items():
        if custom_id:
            text = text.replace(std_emoji, f'<tg-emoji emoji-id="{custom_id}">{std_emoji}</tg-emoji>')
    return text

# ========== SAFE SEND / EDIT HELPERS (HTML & TIMEOUT RESILIENT) ==========
def strip_html_tags(text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean)

async def safe_reply(update: Update, html_text: str, **kwargs):
    """Sends an HTML message with pe(), falling back to plain text if Telegram rejects an emoji/tag."""
    try:
        return await update.message.reply_text(pe(html_text), parse_mode="HTML", **kwargs)
    except BadRequest as e:
        logger.warning(f"HTML/Emoji parse error in reply ({e}) — sending plain fallback.")
        return await update.message.reply_text(strip_html_tags(html_text), **kwargs)

async def safe_edit(message, html_text: str, **kwargs):
    """Edits an HTML message with pe(), falling back to plain text on BadRequest and ignoring TimedOut."""
    try:
        return await message.edit_text(pe(html_text), parse_mode="HTML", **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return message
        logger.warning(f"HTML/Emoji parse error in edit ({e}) — sending plain fallback.")
        try:
            return await message.edit_text(strip_html_tags(html_text), **kwargs)
        except Exception:
            return message
    except TimedOut:
        logger.warning("Telegram TimedOut during message edit — continuing.")
        return message

# ========== ACTIVE CHECK TRACKING (/stop) ==========
ACTIVE_CHECKS = set()
STOP_REQUESTED = set()

# ========== XERODEL REDEEM API & ERROR MESSAGES ==========
FAILURE_TEXT = {
    "code_required": "Send the code: <code>/redeem XERO-XXXX-XXXX</code>",
    "code_not_found": "That code was not found. Copy it exactly from your order message.",
    "code_already_used": "This code has already been used. Each code works once.",
    "code_expired": "This code expired after 24 hours. Please contact support.",
    "unauthorized": "Server key mismatch — please tell the admin.",
    "network": "Could not reach the order server right now. Please try again in a moment.",
    "cookie_missing": "The code is valid but the cookie is empty — please contact support.",
}

def normalize_code(code: str) -> str:
    return (code or "").strip().upper()

def failure_text(error: str) -> str:
    return FAILURE_TEXT.get(error, f"Redeem failed ({html.escape(str(error))}) — please contact support.")

async def verify_redeem_code(code: str, telegram_id: int) -> dict:
    code = normalize_code(code)
    if not code:
        return {"ok": False, "error": "code_required"}
    try:
        telegram_id = int(telegram_id or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "code_required"}

    try:
        async with httpx.AsyncClient(timeout=REDEEM_TIMEOUT) as client:
            resp = await client.get(
                REDEEM_BASE_URL + VERIFY_PATH,
                params={"code": code, "telegram_id": telegram_id},
                headers={"X-Api-Key": REDEEM_API_KEY},
            )
    except Exception as e:
        logger.error(f"Redeem network error: {e}")
        return {"ok": False, "error": "network"}

    if resp.status_code == 401:
        return {"ok": False, "error": "unauthorized"}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": f"http_{resp.status_code}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": f"http_{resp.status_code}"}

    if data.get("ok"):
        if not (data.get("cookie") or "").strip():
            return {"ok": False, "error": "cookie_missing"}
        return data

    result = {"ok": False, "error": data.get("error") or f"http_{resp.status_code}"}
    if data.get("expires_at"):
        result["expires_at"] = data["expires_at"]
    return result

# ========== IN-MEMORY FALLBACK & SUPABASE STORAGE ==========
MEM_APPROVED_USERS = {OWNER_ID}
MEM_TEMP_USERS = {}
MEM_PROMO_KEYS = {}
MEM_SETTINGS = {"global_proxy": GLOBAL_PROXY}

def get_db_conn():
    if not SUPABASE_URI:
        return None
    return psycopg2.connect(SUPABASE_URI, sslmode="require", connect_timeout=5)

def init_db():
    if not SUPABASE_URI:
        logger.warning("⚠️ SUPABASE_URI not set. Running in memory-only mode.")
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS approved_users (
                        user_id BIGINT PRIMARY KEY
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS temp_users (
                        user_id BIGINT PRIMARY KEY,
                        expires_at DOUBLE PRECISION NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS promo_keys (
                        key_code TEXT PRIMARY KEY,
                        duration_hours INTEGER NOT NULL DEFAULT 24
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_settings (
                        setting_key TEXT PRIMARY KEY,
                        setting_value TEXT NOT NULL
                    );
                """)
                cur.execute(
                    "INSERT INTO approved_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (OWNER_ID,),
                )
                cur.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'global_proxy';")
                row = cur.fetchone()
                if row:
                    MEM_SETTINGS["global_proxy"] = row[0]
        conn.close()
        logger.info("✅ Supabase tables initialized successfully!")
    except Exception as e:
        logger.error(f"❌ Supabase initialization failed: {e}")

def get_active_proxy() -> str:
    return (MEM_SETTINGS.get("global_proxy") or "").strip()

def db_set_proxy(proxy_url: str):
    MEM_SETTINGS["global_proxy"] = proxy_url.strip()
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                if proxy_url.strip():
                    cur.execute(
                        """
                        INSERT INTO bot_settings (setting_key, setting_value)
                        VALUES ('global_proxy', %s)
                        ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value;
                        """,
                        (proxy_url.strip(),),
                    )
                else:
                    cur.execute("DELETE FROM bot_settings WHERE setting_key = 'global_proxy';")
        conn.close()
    except Exception as e:
        logger.error(f"DB set proxy error: {e}")

def db_is_authorized(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    if user_id in MEM_APPROVED_USERS or MEM_TEMP_USERS.get(user_id, 0) > time.time():
        return True
    if not SUPABASE_URI:
        return False
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM approved_users WHERE user_id = %s;", (user_id,))
            if cur.fetchone():
                conn.close()
                MEM_APPROVED_USERS.add(user_id)
                return True
            cur.execute("SELECT expires_at FROM temp_users WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
            conn.close()
            if row and row[0] > time.time():
                MEM_TEMP_USERS[user_id] = row[0]
                return True
    except Exception as e:
        logger.error(f"DB auth check error: {e}")
    return False

def db_add_approved(user_id: int):
    MEM_APPROVED_USERS.add(user_id)
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO approved_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (user_id,),
                )
        conn.close()
    except Exception as e:
        logger.error(f"DB add approved error: {e}")

def db_remove_approved(user_id: int):
    MEM_APPROVED_USERS.discard(user_id)
    MEM_TEMP_USERS.pop(user_id, None)
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM approved_users WHERE user_id = %s;", (user_id,))
                cur.execute("DELETE FROM temp_users WHERE user_id = %s;", (user_id,))
        conn.close()
    except Exception as e:
        logger.error(f"DB remove approved error: {e}")

def db_set_temp_access(user_id: int, expires_at: float):
    MEM_TEMP_USERS[user_id] = expires_at
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO temp_users (user_id, expires_at)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at;
                    """,
                    (user_id, expires_at),
                )
        conn.close()
    except Exception as e:
        logger.error(f"DB set temp access error: {e}")

def db_create_promo_key(key_code: str, hours: int):
    key_code = normalize_code(key_code)
    MEM_PROMO_KEYS[key_code] = hours
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO promo_keys (key_code, duration_hours)
                    VALUES (%s, %s)
                    ON CONFLICT (key_code) DO UPDATE SET duration_hours = EXCLUDED.duration_hours;
                    """,
                    (key_code, hours),
                )
        conn.close()
    except Exception as e:
        logger.error(f"DB create promo key error: {e}")

def db_burn_promo_key(key_code: str) -> int | None:
    key_code = normalize_code(key_code)
    mem_hours = MEM_PROMO_KEYS.pop(key_code, None)
    if not SUPABASE_URI:
        return mem_hours
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM promo_keys WHERE key_code = %s RETURNING duration_hours;",
                    (key_code,),
                )
                row = cur.fetchone()
        conn.close()
        return row[0] if row else mem_hours
    except Exception as e:
        logger.error(f"DB burn promo key error: {e}")
        return mem_hours

# ========== COOKIE PARSING & NETFLIX CHECKER ==========
def parse_cookies_from_text(raw_text: str) -> dict:
    cookies = {}
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return cookies

    if raw_text.startswith("[") or raw_text.startswith("{"):
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, list):
                for item in data:
                    name = item.get("name")
                    val = item.get("value")
                    if name and val is not None:
                        cookies[name] = str(val)
                if cookies:
                    return cookies
            elif isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5].strip()] = parts[6].strip()
        elif "=" in line:
            for pair in line.split(";"):
                if "=" in pair:
                    k, v = pair.strip().split("=", 1)
                    cookies[k.strip()] = v.strip()
    return cookies

def check_netflix_cookie(cookie_dict: dict) -> dict | None:
    netflix_id = cookie_dict.get("NetflixId")
    if not netflix_id:
        return None

    proxy_url = get_active_proxy()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        session = requests.Session()
        session.cookies.update(cookie_dict)
        resp = session.get(
            "https://www.netflix.com/YourAccount",
            headers=headers,
            proxies=proxies,
            timeout=15,
            allow_redirects=True,
        )

        if "/login" in resp.url or resp.status_code != 200:
            return None

        page = resp.text
        if "membershipStatus" not in page and "CURRENT_MEMBER" not in page:
            return None

        plan_match = re.search(r'"planName"\s*:\s*"([^"]+)"', page)
        country_match = re.search(r'"countryOfSignup"\s*:\s*"([^"]+)"', page)
        billing_match = re.search(r'"nextBillingDate"\s*:\s*"([^"]+)"', page)
        email_match = re.search(r'"emailAddress"\s*:\s*"([^"]+)"', page)

        plan = plan_match.group(1) if plan_match else "Active Plan"
        country = country_match.group(1) if country_match else "Unknown"
        expiry = billing_match.group(1).replace("\\u002F", "/") if billing_match else "Auto-Renew"
        email = email_match.group(1).replace("\\x40", "@") if email_match else "Hidden"

        clean_id = unquote(netflix_id)
        pc_link = f"https://www.netflix.com/browse?nftoken={clean_id}"
        phone_link = f"https://www.netflix.com/?nftoken={clean_id}"

        return {
            "plan": plan,
            "country": country,
            "expiry": expiry,
            "email": email,
            "pc_link": pc_link,
            "phone_link": phone_link,
            "netflix_id": clean_id,
        }
    except Exception as e:
        logger.error(f"Netflix check error: {e}")
        return None

def build_login_links_fallback(cookie_dict: dict, source_label: str) -> dict | None:
    netflix_id = cookie_dict.get("NetflixId")
    if not netflix_id:
        return None
    clean_id = unquote(netflix_id)
    return {
        "source": source_label,
        "plan": "Active Order",
        "country": "Verified",
        "expiry": "24h Session",
        "email": "Protected",
        "pc_link": f"https://www.netflix.com/browse?nftoken={clean_id}",
        "phone_link": f"https://www.netflix.com/?nftoken={clean_id}",
        "netflix_id": clean_id,
    }

# ========== OUTPUT CARD & BULK REPORT BUILDER ==========
def build_single_card(acct: dict) -> tuple[str, InlineKeyboardMarkup]:
    card_text = (
        f"📁 <b>Source:</b> <code>{html.escape(acct['source'])}</code>\n"
        f"✅ <b>Status:</b> Valid Account\n\n"
        f"🎬 <b>Plan:</b> {html.escape(acct['plan'])}\n"
        f"🌍 <b>Country:</b> {html.escape(acct['country'])}\n"
        f"📅 <b>Expiry:</b> {html.escape(acct['expiry'])}\n"
        f"📧 <b>Email:</b> <code>{html.escape(acct['email'])}</code>\n\n"
        f"<b>🔱 XERON</b>"
    )

    pc_btn_kwargs = {"icon_custom_emoji_id": BTN_EMOJI_PC} if BTN_EMOJI_PC else {}
    phone_btn_kwargs = {"icon_custom_emoji_id": BTN_EMOJI_PHONE} if BTN_EMOJI_PHONE else {}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "PC Login" if BTN_EMOJI_PC else "💻 PC Login",
                url=acct["pc_link"],
                **pc_btn_kwargs,
            ),
            InlineKeyboardButton(
                "Phone Login" if BTN_EMOJI_PHONE else "📱 Phone Login",
                url=acct["phone_link"],
                **phone_btn_kwargs,
            ),
        ]
    ])
    return card_text, keyboard

async def send_results(
    update: Update,
    status_msg,
    valid_accounts: list,
    total_checked: int,
    was_stopped: bool = False,
):
    stop_banner = "🛑 <b>Check Stopped Early!</b>\n" if was_stopped else ""

    if not valid_accounts:
        await safe_edit(
            status_msg,
            f"{stop_banner}❌ <b>No Valid Accounts Found</b>\nChecked: {total_checked} cookie(s).",
        )
        return

    if len(valid_accounts) == 1:
        card_text, keyboard = build_single_card(valid_accounts[0])
        await safe_edit(
            status_msg,
            stop_banner + card_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

    lines = [f"=== XERONFLIX VALID ACCOUNTS ({len(valid_accounts)}/{total_checked}) ===\n"]
    for idx, acct in enumerate(valid_accounts, 1):
        lines.append(
            f"[{idx}] Source: {acct['source']}\n"
            f"Plan: {acct['plan']} | Country: {acct['country']} | Expiry: {acct['expiry']}\n"
            f"PC Login: {acct['pc_link']}\n"
            f"Phone Login: {acct['phone_link']}\n"
            + "-" * 50
        )

    report_bytes = "\n".join(lines).encode("utf-8")
    bio = io.BytesIO(report_bytes)
    bio.name = f"results_{int(time.time())}.txt"

    header = "🛑 <b>Check Stopped!</b>" if was_stopped else "✅ <b>Bulk Check Complete!</b>"
    try:
        await status_msg.delete()
    except Exception:
        pass

    caption_html = f"{header}\nValid: <b>{len(valid_accounts)}</b> / {total_checked}"
    try:
        await update.message.reply_document(
            document=bio,
            caption=pe(caption_html),
            parse_mode="HTML",
        )
    except BadRequest:
        bio.seek(0)
        await update.message.reply_document(
            document=bio,
            caption=strip_html_tags(caption_html),
        )

# ========== SHARED REDEEM PROCESSOR ==========
async def process_redeem_code(update: Update, raw_code: str):
    user_id = update.effective_user.id
    norm_code = normalize_code(raw_code)

    if not norm_code.startswith("XERO-"):
        await safe_reply(
            update,
            "⚠️ Invalid format. Please enter a valid code: <code>/redeem XERO-XXXX-XXXX</code>",
        )
        return

    status_msg = await safe_reply(update, "🔄 Verifying code...")

    # 1. Check local /genkey XERO- promo keys in Supabase + RAM first (non-blocking)
    local_hours = await asyncio.to_thread(db_burn_promo_key, norm_code)
    if local_hours is not None:
        exp_time = time.time() + (local_hours * 3600)
        await asyncio.to_thread(db_set_temp_access, user_id, exp_time)
        await safe_edit(
            status_msg,
            f"🎉 <b>Promo Key Redeemed!</b>\n\n"
            f"Granted <b>{local_hours}-Hour Checker Access</b> to your account (<code>{user_id}</code>).",
        )
        return

    # 2. Otherwise check Xerodel API for order session cookies
    result = await verify_redeem_code(norm_code, user_id)

    if not result.get("ok"):
        err_msg = failure_text(result.get("error", "unknown"))
        await safe_edit(status_msg, f"❌ {err_msg}")
        return

    raw_cookie = result.get("cookie", "")
    order_code = result.get("order_code") or norm_code
    cookies = parse_cookies_from_text(raw_cookie)

    if not cookies or "NetflixId" not in cookies:
        await safe_edit(status_msg, f"❌ {failure_text('cookie_missing')}")
        return

    await safe_edit(status_msg, "🔄 Order verified! Preparing your login links...")

    acct = await asyncio.to_thread(check_netflix_cookie, cookies)
    if acct:
        acct["source"] = f"Order {order_code}"
    else:
        acct = build_login_links_fallback(cookies, f"Order {order_code}")

    card_text, keyboard = build_single_card(acct)
    await safe_edit(
        status_msg,
        "🎉 <b>Order Redeemed Successfully!</b>\n\n" + card_text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

# ========== TELEGRAM COMMAND HANDLERS ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    auth = await asyncio.to_thread(db_is_authorized, user_id)
    status_line = (
        "✅ <b>Checker Mode:</b> Active"
        if auth
        else "🔒 <b>Checker Mode:</b> Locked (Redeem a key or order code)"
    )

    welcome_text = (
        f"🎬 <b>Xeronflix Cookie Checker & Order Redeemer</b>\n\n"
        f"{status_line}\n"
        f"🆔 <b>Your ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Quick Start:</b>\n"
        f"• Redeem order or promo key: <code>/redeem XERO-XXXX-XXXX</code>\n"
        f"• View all commands: <code>/help</code>"
    )
    await safe_reply(update, welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        f"📖 <b>Xeronflix Bot Commands & Guide</b>\n\n"
        f"<b>User Commands:</b>\n"
        f"• <code>/start</code> — Check your access status & Telegram ID\n"
        f"• <code>/help</code> — Show this command menu\n"
        f"• <code>/redeem <code></code> — Redeem a Xerodel order code or promo key\n"
        f"• <code>/stop</code> — Instantly stop an ongoing ZIP/batch check\n\n"
        f"<b>Cookie Checking (Authorized Users):</b>\n"
        f"• Paste raw Netflix cookies (JSON, Netscape, or Header format)\n"
        f"• Upload a single <code>.txt</code> or bulk <code>.zip</code> archive"
    )
    await safe_reply(update, help_text)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CHECKS:
        STOP_REQUESTED.add(user_id)
        await safe_reply(update, "🛑 Stopping current check immediately...")
    else:
        await safe_reply(update, "⚠️ You have no active check running right now.")

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, f"⚠️ {failure_text('code_required')}")
        return
    await process_redeem_code(update, context.args[0])

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await safe_reply(update, "⚠️ Usage: <code>/approve <user_id></code>")
        return
    target_id = int(context.args[0])
    await asyncio.to_thread(db_add_approved, target_id)
    await safe_reply(update, f"✅ Approved user <code>{target_id}</code>.")

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await safe_reply(update, "⚠️ Usage: <code>/revoke <user_id></code>")
        return
    target_id = int(context.args[0])
    await asyncio.to_thread(db_remove_approved, target_id)
    await safe_reply(update, f"🚫 Revoked access for <code>{target_id}</code>.")

async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    hours = 24
    if context.args and context.args[0].isdigit():
        hours = max(1, int(context.args[0]))

    part1 = secrets.token_hex(2).upper()
    part2 = secrets.token_hex(2).upper()
    key_code = f"XERO-{part1}-{part2}"
    await asyncio.to_thread(db_create_promo_key, key_code, hours)

    await safe_reply(
        update,
        f"🔑 <b>Promo Key Generated!</b>\n\n"
        f"• <b>Key:</b> <code>{key_code}</code>\n"
        f"• <b>Duration:</b> {hours} Hour(s)\n\n"
        f"User can claim it with:\n<code>/redeem {key_code}</code>",
    )

async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        current = html.escape(get_active_proxy()) if get_active_proxy() else "None"
        await safe_reply(
            update,
            f"🌍 <b>Global Proxy Settings</b>\n"
            f"• <b>Active:</b> <code>{current}</code>\n\n"
            f"<b>Usage:</b>\n"
            f"• Set: <code>/addproxy http://user:pass@ip:port</code>\n"
            f"• Clear: <code>/addproxy off</code>",
        )
        return

    val = context.args[0].strip()
    if val.lower() in ("off", "none", "clear", "remove", "delete"):
        await asyncio.to_thread(db_set_proxy, "")
        await safe_reply(update, "✅ Global proxy cleared. Bot will use direct connection.")
        return

    if not (val.startswith("http://") or val.startswith("https://") or val.startswith("socks5://")):
        val = f"http://{val}"

    await asyncio.to_thread(db_set_proxy, val)
    await safe_reply(update, f"✅ <b>Global Proxy Updated!</b>\n<code>{html.escape(val)}</code>")

# ========== TEXT & FILE HANDLERS ==========
CODE_PATTERN = re.compile(r"^XERO-[A-Z0-9]{4,12}(-[A-Z0-9]{4})?$", re.IGNORECASE)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = (update.message.text or "").strip()

    if CODE_PATTERN.match(raw_text):
        await process_redeem_code(update, raw_text)
        return

    user_id = update.effective_user.id
    if not await asyncio.to_thread(db_is_authorized, user_id):
        await safe_reply(
            update,
            "🔒 You are not authorized. Redeem a code with <code>/redeem XERO-XXXX-XXXX</code> or check <code>/help</code>.",
        )
        return

    cookies = parse_cookies_from_text(raw_text)
    if not cookies or "NetflixId" not in cookies:
        await safe_reply(update, "⚠️ No valid <code>NetflixId</code> cookie or redeem code detected.")
        return

    status_msg = await safe_reply(update, "🔄 Checking Netflix cookie...")
    res = await asyncio.to_thread(check_netflix_cookie, cookies)
    valid = []
    if res:
        res["source"] = "Direct Paste"
        valid.append(res)

    await send_results(update, status_msg, valid, 1)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await asyncio.to_thread(db_is_authorized, user_id):
        await safe_reply(
            update,
            "🔒 File checking is restricted to approved users. Use <code>/redeem XERO-XXXX-XXXX</code> first.",
        )
        return

    if user_id in ACTIVE_CHECKS:
        await safe_reply(
            update,
            "⚠️ You already have a check running! Send <code>/stop</code> first to cancel it.",
        )
        return

    doc = update.message.document
    filename = doc.file_name or "upload.txt"
    lower_name = filename.lower()

    if not (lower_name.endswith(".txt") or lower_name.endswith(".zip")):
        await safe_reply(update, "⚠️ Please upload only <code>.txt</code> or <code>.zip</code> files.")
        return

    status_msg = await safe_reply(
        update,
        f"🔄 Downloading <code>{html.escape(filename)}</code>... (Send <code>/stop</code> to cancel)",
    )

    try:
        tg_file = await doc.get_file(read_timeout=30.0, write_timeout=30.0, connect_timeout=30.0)
        file_bytes = await tg_file.download_as_bytearray(read_timeout=30.0, write_timeout=30.0, connect_timeout=30.0)
    except TimedOut:
        await safe_edit(status_msg, "❌ Telegram timed out downloading your file. Please try again.")
        return

    entries = []
    if lower_name.endswith(".txt"):
        entries.append((filename, file_bytes.decode("utf-8", errors="ignore")))
    elif lower_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                for member in zf.namelist():
                    if member.lower().endswith(".txt") and not member.startswith("__MACOSX"):
                        content = zf.read(member).decode("utf-8", errors="ignore")
                        entries.append((os.path.basename(member), content))
        except Exception as e:
            await safe_edit(status_msg, f"❌ Failed to read ZIP archive: {html.escape(str(e))}")
            return

    valid_accounts = []
    checked_count = 0
    was_stopped = False

    ACTIVE_CHECKS.add(user_id)
    STOP_REQUESTED.discard(user_id)

    try:
        total_entries = len(entries)
        for idx, (source_name, content) in enumerate(entries, 1):
            if user_id in STOP_REQUESTED:
                was_stopped = True
                break

            cookies = parse_cookies_from_text(content)
            if cookies and "NetflixId" in cookies:
                checked_count += 1
                if checked_count == 1 or checked_count % 15 == 0:
                    await safe_edit(
                        status_msg,
                        f"🔄 Checking <code>{html.escape(filename)}</code>...\n"
                        f"Progress: <b>{idx}/{total_entries}</b> | Valid: <b>{len(valid_accounts)}</b>\n"
                        f"Send <code>/stop</code> to cancel anytime.",
                    )

                res = await asyncio.to_thread(check_netflix_cookie, cookies)
                if res:
                    res["source"] = source_name
                    valid_accounts.append(res)

                if user_id in STOP_REQUESTED:
                    was_stopped = True
                    break
    finally:
        ACTIVE_CHECKS.discard(user_id)
        STOP_REQUESTED.discard(user_id)

    await send_results(update, status_msg, valid_accounts, checked_count, was_stopped=was_stopped)

# ========== TELEGRAM COMMAND MENU (HIDDEN FROM MENU BAR) ==========
async def post_init(application: Application):
    await application.bot.delete_my_commands()

# ========== FLASK HEALTH ENDPOINT ==========
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "Xeronflix Bot is Running!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ========== MAIN ENTRYPOINT ==========
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(CommandHandler("genkey", genkey_command))
    app.add_handler(CommandHandler("addproxy", addproxy_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Xeronflix Bot started polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
