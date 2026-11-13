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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
REDEEM_TIMEOUT = 20

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
}

BTN_EMOJI_PC = "5235785929502761186"     # Paste custom_emoji_id for 💻 here
BTN_EMOJI_PHONE = "6026092115631543342"  # Paste custom_emoji_id for 📱 here
def pe(text: str) -> str:
    """Automatically converts every mapped standard emoji in an HTML string into a <tg-emoji> tag."""
    for std_emoji, custom_id in PREMIUM_EMOJI_MAP.items():
        if custom_id:
            text = text.replace(std_emoji, f'<tg-emoji emoji-id="{custom_id}">{std_emoji}</tg-emoji>')
    return text

# ========== ACTIVE CHECK TRACKING (/stop) ==========
ACTIVE_CHECKS = set()   # user_ids currently running a batch check
STOP_REQUESTED = set()  # user_ids that triggered /stop

# ========== XERODEL REDEEM API & ERROR MESSAGES ==========
FAILURE_TEXT = {
    "code_required": "Send the code: <code>/redeem XERO-XXXX-XXXX</code>",
    "code_not_found": "That code was not found. Copy it exactly from your order message.",
    "code_already_used": "This code has already been used. Each code works once.",
    "code_expired": "This code expired after 24 hours. Please contact support.",
    "unauthorized": "Server key mismatch — please tell the admin.",
    "network": "Could not reach the server. Please try again in a moment.",
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
MEM_TEMP_USERS = {}    # {user_id: expires_at}
MEM_PROMO_KEYS = {}    # {key_code: duration_hours}
MEM_SETTINGS = {"global_proxy": GLOBAL_PROXY}

def get_db_conn():
    if not SUPABASE_URI:
        return None
    return psycopg2.connect(SUPABASE_URI, sslmode="require")

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
    if not SUPABASE_URI:
        if user_id in MEM_APPROVED_USERS:
            return True
        return MEM_TEMP_USERS.get(user_id, 0) > time.time()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM approved_users WHERE user_id = %s;", (user_id,))
            if cur.fetchone():
                conn.close()
                return True
            cur.execute("SELECT expires_at FROM temp_users WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
            conn.close()
            if row and row[0] > time.time():
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
    MEM_PROMO_KEYS[key_code] = hours
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO promo_keys (key_code, duration_hours) VALUES (%s, %s);",
                    (key_code, hours),
                )
        conn.close()
    except Exception as e:
        logger.error(f"DB create promo key error: {e}")

def db_burn_promo_key(key_code: str) -> int | None:
    key_code = normalize_code(key_code)
    if not SUPABASE_URI:
        return MEM_PROMO_KEYS.pop(key_code, None)
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
        return row[0] if row else None
    except Exception as e:
        logger.error(f"DB burn promo key error: {e}")
    return None

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
    card_text = pe(
        f"📁 <b>Source:</b> <code>{html.escape(acct['source'])}</code>\n"
        f"✅ <b>Status:</b> Valid Account\n\n"
        f"🎬 <b>Plan:</b> {html.escape(acct['plan'])}\n"
        f"🌍 <b>Country:</b> {html.escape(acct['country'])}\n"
        f"📅 <b>Expiry:</b> {html.escape(acct['expiry'])}\n"
        f"📧 <b>Email:</b> <code>{html.escape(acct['email'])}</code>"
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
        await status_msg.edit_text(
            pe(f"{stop_banner}❌ <b>No Valid Accounts Found</b>\nChecked: {total_checked} cookie(s)."),
            parse_mode="HTML",
        )
        return

    if len(valid_accounts) == 1:
        card_text, keyboard = build_single_card(valid_accounts[0])
        await status_msg.edit_text(
            pe(stop_banner) + card_text,
            reply_markup=keyboard,
            parse_mode="HTML",
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
    await status_msg.delete()
    await update.message.reply_document(
        document=bio,
        caption=pe(
            f"{header}\n"
            f"Valid: <b>{len(valid_accounts)}</b> / {total_checked}"
        ),
        parse_mode="HTML",
    )

# ========== SHARED REDEEM PROCESSOR ==========
async def process_redeem_code(update: Update, raw_code: str):
    user_id = update.effective_user.id
    norm_code = normalize_code(raw_code)
    status_msg = await update.message.reply_text(
        pe("🔄 Verifying code..."),
        parse_mode="HTML",
    )

    local_hours = db_burn_promo_key(norm_code)
    if local_hours is not None:
        exp_time = time.time() + (local_hours * 3600)
        db_set_temp_access(user_id, exp_time)
        await status_msg.edit_text(
            pe(
                f"🎉 <b>Promo Key Redeemed!</b>\n\n"
                f"Granted <b>{local_hours}-Hour Checker Access</b> to your account (<code>{user_id}</code>)."
            ),
            parse_mode="HTML",
        )
        return

    result = await verify_redeem_code(norm_code, user_id)

    if not result.get("ok"):
        err_msg = failure_text(result.get("error", "unknown"))
        await status_msg.edit_text(pe(f"❌ {err_msg}"), parse_mode="HTML")
        return

    raw_cookie = result.get("cookie", "")
    order_code = result.get("order_code") or norm_code
    cookies = parse_cookies_from_text(raw_cookie)

    if not cookies or "NetflixId" not in cookies:
        await status_msg.edit_text(
            pe(f"❌ {failure_text('cookie_missing')}"),
            parse_mode="HTML",
        )
        return

    await status_msg.edit_text(
        pe("🔄 Order verified! Preparing your login links..."),
        parse_mode="HTML",
    )

    acct = await asyncio.to_thread(check_netflix_cookie, cookies)
    if acct:
        acct["source"] = f"Order {order_code}"
    else:
        acct = build_login_links_fallback(cookies, f"Order {order_code}")

    card_text, keyboard = build_single_card(acct)
    await status_msg.edit_text(
        pe("🎉 <b>Order Redeemed Successfully!</b>\n\n") + card_text,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ========== TELEGRAM COMMAND HANDLERS ==========
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    auth = db_is_authorized(user_id)
    status_line = (
        "✅ <b>Checker Mode:</b> Active"
        if auth
        else "🔒 <b>Checker Mode:</b> Locked (Redeem a key or order code)"
    )

    welcome_text = pe(
        f"🎬 <b>Xeronflix Cookie Checker</b>\n\n"
        f"{status_line}\n"
        f"🆔 <b>Your ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Quick Start:</b>\n"
        f"• Redeem order or promo key: <code>/redeem <code></code>\n"
        f"• View all commands: <code>/help</code>"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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

    if user_id == OWNER_ID:
        proxy_status = html.escape(get_active_proxy()) if get_active_proxy() else "None (Direct)"
        help_text += (
            f"\n\n🔑 <b>Owner Admin Commands:</b>\n"
            f"• <code>/approve <user_id></code> — Grant permanent access\n"
            f"• <code>/revoke <user_id></code> — Remove user access\n"
            f"• <code>/genkey [hours]</code> — Generate a one-time promo key (default 24h)\n"
            f"• <code>/addproxy <url></code> — Set global HTTP proxy (<code>off</code> to clear)\n"
            f"🌍 <b>Current Proxy:</b> <code>{proxy_status}</code>"
        )

    await update.message.reply_text(pe(help_text), parse_mode="HTML")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ACTIVE_CHECKS:
        STOP_REQUESTED.add(user_id)
        await update.message.reply_text(
            pe("🛑 Stopping current check immediately..."),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            pe("⚠️ You have no active check running right now."),
            parse_mode="HTML",
        )

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            pe(f"⚠️ {failure_text('code_required')}"),
            parse_mode="HTML",
        )
        return
    await process_redeem_code(update, context.args[0])

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            pe("⚠️ Usage: <code>/approve <user_id></code>"),
            parse_mode="HTML",
        )
        return
    target_id = int(context.args[0])
    db_add_approved(target_id)
    await update.message.reply_text(pe(f"✅ Approved user <code>{target_id}</code>."), parse_mode="HTML")

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            pe("⚠️ Usage: <code>/revoke <user_id></code>"),
            parse_mode="HTML",
        )
        return
    target_id = int(context.args[0])
    db_remove_approved(target_id)
    await update.message.reply_text(pe(f"🚫 Revoked access for <code>{target_id}</code>."), parse_mode="HTML")

async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    hours = 24
    if context.args and context.args[0].isdigit():
        hours = max(1, int(context.args[0]))

    key_code = f"KEY-{secrets.token_hex(4).upper()}"
    db_create_promo_key(key_code, hours)

    await update.message.reply_text(
        pe(
            f"🔑 <b>Promo Key Generated!</b>\n\n"
            f"• <b>Key:</b> <code>{key_code}</code>\n"
            f"• <b>Duration:</b> {hours} Hour(s)\n\n"
            f"User can claim it with:\n<code>/redeem {key_code}</code>"
        ),
        parse_mode="HTML",
    )

async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        current = html.escape(get_active_proxy()) if get_active_proxy() else "None"
        await update.message.reply_text(
            pe(
                f"🌍 <b>Global Proxy Settings</b>\n"
                f"• <b>Active:</b> <code>{current}</code>\n\n"
                f"<b>Usage:</b>\n"
                f"• Set: <code>/addproxy http://user:pass@ip:port</code>\n"
                f"• Clear: <code>/addproxy off</code>"
            ),
            parse_mode="HTML",
        )
        return

    val = context.args[0].strip()
    if val.lower() in ("off", "none", "clear", "remove", "delete"):
        db_set_proxy("")
        await update.message.reply_text(pe("✅ Global proxy cleared. Bot will use direct connection."), parse_mode="HTML")
        return

    if not (val.startswith("http://") or val.startswith("https://") or val.startswith("socks5://")):
        val = f"http://{val}"

    db_set_proxy(val)
    await update.message.reply_text(
        pe(f"✅ <b>Global Proxy Updated!</b>\n<code>{html.escape(val)}</code>"),
        parse_mode="HTML",
    )

# ========== TEXT & FILE HANDLERS ==========
CODE_PATTERN = re.compile(r"^(XERO|KEY)-[A-Z0-9]{4,12}(-[A-Z0-9]{4})?$", re.IGNORECASE)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = (update.message.text or "").strip()

    if CODE_PATTERN.match(raw_text):
        await process_redeem_code(update, raw_text)
        return

    user_id = update.effective_user.id
    if not db_is_authorized(user_id):
        await update.message.reply_text(
            pe("🔒 You are not authorized. Redeem a code with <code>/redeem <code></code> or check <code>/help</code>."),
            parse_mode="HTML",
        )
        return

    cookies = parse_cookies_from_text(raw_text)
    if not cookies or "NetflixId" not in cookies:
        await update.message.reply_text(
            pe("⚠️ No valid <code>NetflixId</code> cookie or redeem code detected."),
            parse_mode="HTML",
        )
        return

    status_msg = await update.message.reply_text(pe("🔄 Checking Netflix cookie..."), parse_mode="HTML")
    res = await asyncio.to_thread(check_netflix_cookie, cookies)
    valid = []
    if res:
        res["source"] = "Direct Paste"
        valid.append(res)

    await send_results(update, status_msg, valid, 1)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db_is_authorized(user_id):
        await update.message.reply_text(
            pe("🔒 File checking is restricted to approved users. Use <code>/redeem <code></code> first."),
            parse_mode="HTML",
        )
        return

    if user_id in ACTIVE_CHECKS:
        await update.message.reply_text(
            pe("⚠️ You already have a check running! Send <code>/stop</code> first to cancel it."),
            parse_mode="HTML",
        )
        return

    doc = update.message.document
    filename = doc.file_name or "upload.txt"
    lower_name = filename.lower()

    if not (lower_name.endswith(".txt") or lower_name.endswith(".zip")):
        await update.message.reply_text(
            pe("⚠️ Please upload only <code>.txt</code> or <code>.zip</code> files."),
            parse_mode="HTML",
        )
        return

    status_msg = await update.message.reply_text(
        pe(f"🔄 Processing <code>{html.escape(filename)}</code>... (Send <code>/stop</code> to cancel)"),
        parse_mode="HTML",
    )
    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()

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
            await status_msg.edit_text(
                pe(f"❌ Failed to read ZIP archive: {html.escape(str(e))}"),
                parse_mode="HTML",
            )
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
                # Update live progress every 5 files so user sees progress and can /stop
                if checked_count == 1 or checked_count % 5 == 0:
                    try:
                        await status_msg.edit_text(
                            pe(
                                f"🔄 Checking <code>{html.escape(filename)}</code>...\n"
                                f"Progress: <b>{idx}/{total_entries}</b> | Valid: <b>{len(valid_accounts)}</b>\n"
                                f"Send <code>/stop</code> to cancel anytime."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

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

# ========== TELEGRAM COMMAND MENU REGISTRATION ==========
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Check bot status & your ID"),
        BotCommand("help", "Show all commands & usage guide"),
        BotCommand("redeem", "Redeem an order code or promo key"),
        BotCommand("stop", "Stop an ongoing ZIP/batch check"),
        BotCommand("approve", "Owner: Approve a user ID"),
        BotCommand("revoke", "Owner: Revoke a user ID"),
        BotCommand("genkey", "Owner: Generate a promo access key"),
        BotCommand("addproxy", "Owner: Set or clear global HTTP proxy"),
    ])

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

    # concurrent_updates=True allows /stop to be handled while handle_document is running!
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
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
