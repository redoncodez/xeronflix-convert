import os
import re
import io
import json
import time
import html
import zipfile
import logging
import threading
import requests
import httpx
import psycopg2
from urllib.parse import unquote
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Xerodel Redeem Service Config (supports both COOKIE_BOT_API_KEY and REDEEM_API_KEY)
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

# Optional Global HTTP Proxy (format: http://user:pass@host:port)
GLOBAL_PROXY = os.environ.get("GLOBAL_PROXY", "")

# ========== MASTER TELEGRAM PREMIUM EMOJI MAP ==========
# Paste your copied custom_emoji_id strings next to each standard emoji.
# Any entry left as "" will automatically display its normal emoji fallback.
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
}

# Custom Emoji IDs for Inline Keyboard Buttons (PC Login / Phone Login)
BTN_EMOJI_PC = "5235785929502761186"     # Paste custom_emoji_id for 💻 here
BTN_EMOJI_PHONE = "6026092115631543342"  # Paste custom_emoji_id for 📱 here

def pe(text: str) -> str:
    """Automatically converts every mapped standard emoji in an HTML string into a <tg-emoji> tag."""
    for std_emoji, custom_id in PREMIUM_EMOJI_MAP.items():
        if custom_id:
            text = text.replace(std_emoji, f'<tg-emoji emoji-id="{custom_id}">{std_emoji}</tg-emoji>')
    return text

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
    """Verifies a redeem code and returns {"ok": True, "cookie": "...", ...} or {"ok": False, "error": "..."}."""
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
                cur.execute(
                    "INSERT INTO approved_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                    (OWNER_ID,),
                )
        conn.close()
        logger.info("✅ Supabase tables initialized successfully!")
    except Exception as e:
        logger.error(f"❌ Supabase initialization failed: {e}")

def db_is_authorized(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    if not SUPABASE_URI:
        return user_id in MEM_APPROVED_USERS
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM approved_users WHERE user_id = %s;", (user_id,))
            found = cur.fetchone() is not None
        conn.close()
        return found
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
    if not SUPABASE_URI:
        return
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM approved_users WHERE user_id = %s;", (user_id,))
        conn.close()
    except Exception as e:
        logger.error(f"DB remove approved error: {e}")

# ========== COOKIE PARSING & NETFLIX CHECKER ==========
def parse_cookies_from_text(raw_text: str) -> dict:
    cookies = {}
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return cookies

    # 1. Try JSON format (Cookie-Editor / EditThisCookie)
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

    # 2. Try Netscape format or Header string (NetflixId=...; SecureNetflixId=...)
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

    proxies = {"http": GLOBAL_PROXY, "https": GLOBAL_PROXY} if GLOBAL_PROXY else None
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
    """Fallback builder if NetflixId is present even if YourAccount scrape was rate-limited."""
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

async def send_results(update: Update, status_msg, valid_accounts: list, total_checked: int):
    if not valid_accounts:
        await status_msg.edit_text(
            pe(f"❌ <b>No Valid Accounts Found</b>\nChecked: {total_checked} cookie(s)."),
            parse_mode="HTML",
        )
        return

    # Single valid account -> Send formatted Premium Emoji HTML Card with Login Buttons
    if len(valid_accounts) == 1:
        card_text, keyboard = build_single_card(valid_accounts[0])
        await status_msg.edit_text(
            card_text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # Multiple valid accounts (2+) -> Send downloadable TXT report
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

    await status_msg.delete()
    await update.message.reply_document(
        document=bio,
        caption=pe(
            f"✅ <b>Bulk Check Complete!</b>\n"
            f"Valid: <b>{len(valid_accounts)}</b> / {total_checked}"
        ),
        parse_mode="HTML",
    )

# ========== SHARED REDEEM PROCESSOR ==========
async def process_redeem_code(update: Update, raw_code: str):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(
        pe("🔄 Verifying order code..."),
        parse_mode="HTML",
    )

    result = await verify_redeem_code(raw_code, user_id)

    if not result.get("ok"):
        err_msg = failure_text(result.get("error", "unknown"))
        await status_msg.edit_text(pe(f"❌ {err_msg}"), parse_mode="HTML")
        return

    raw_cookie = result.get("cookie", "")
    order_code = result.get("order_code") or normalize_code(raw_code)
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

    acct = check_netflix_cookie(cookies)
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
        else "🔒 <b>Checker Mode:</b> Approved Users Only"
    )

    welcome_text = pe(
        f"🎬 <b>Xeronflix Cookie Checker & Order Redeemer</b>\n\n"
        f"{status_line}\n"
        f"🆔 <b>Your ID:</b> <code>{user_id}</code>\n\n"
        f"<b>How to use:</b>\n"
        f"• Redeem an order code: <code>/redeem XERO-XXXX-XXXX</code> (or paste the code directly)\n"
        f"• Approved users can also paste raw cookies or upload <code>.txt</code> / <code>.zip</code> files"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

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

# ========== TEXT & FILE HANDLERS ==========
CODE_PATTERN = re.compile(r"^XERO-[A-Z0-9]{4}-[A-Z0-9]{4}$", re.IGNORECASE)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = (update.message.text or "").strip()

    # 1. Anyone can paste a XERO-XXXX-XXXX order code directly without typing /redeem
    if CODE_PATTERN.match(raw_text):
        await process_redeem_code(update, raw_text)
        return

    # 2. Raw cookie pasting is restricted to approved users
    user_id = update.effective_user.id
    if not db_is_authorized(user_id):
        await update.message.reply_text(
            pe("🔒 Send your order code using <code>/redeem XERO-XXXX-XXXX</code>."),
            parse_mode="HTML",
        )
        return

    cookies = parse_cookies_from_text(raw_text)
    if not cookies or "NetflixId" not in cookies:
        await update.message.reply_text(
            pe("⚠️ No valid <code>NetflixId</code> cookie or <code>XERO-XXXX-XXXX</code> code detected."),
            parse_mode="HTML",
        )
        return

    status_msg = await update.message.reply_text(pe("🔄 Checking Netflix cookie..."), parse_mode="HTML")
    res = check_netflix_cookie(cookies)
    valid = []
    if res:
        res["source"] = "Direct Paste"
        valid.append(res)

    await send_results(update, status_msg, valid, 1)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db_is_authorized(user_id):
        await update.message.reply_text(
            pe("🔒 File checking is restricted to approved users. Use <code>/redeem XERO-XXXX-XXXX</code> for order codes."),
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
        pe(f"🔄 Processing <code>{html.escape(filename)}</code>..."),
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

    for source_name, content in entries:
        cookies = parse_cookies_from_text(content)
        if cookies and "NetflixId" in cookies:
            checked_count += 1
            res = check_netflix_cookie(cookies)
            if res:
                res["source"] = source_name
                valid_accounts.append(res)

    await send_results(update, status_msg, valid_accounts, checked_count)

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

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Xeronflix Bot started polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
