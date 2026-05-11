from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timezone
from typing import Optional, Union

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

API_BASE = "https://tempmail-worker.fattanafif02.workers.dev/api/public"

# In-memory storage: current active email per user
user_current_email: dict[int, str] = {}

# Cookie per email address (each email has its own session cookie)
email_cookies: dict[str, str] = {}

# In-memory history: telegram user_id -> list of previously generated emails
user_history: dict[int, list[str]] = {}

# In-memory state for custom email flow: telegram user_id -> {"domain": str | None}
user_pending_custom: dict[int, dict] = {}

# In-memory state for claim flow: telegram user_id -> True when waiting for email input
user_pending_claim: dict[int, bool] = {}


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the main menu inline keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("Generate Email", callback_data="generate"),
            InlineKeyboardButton("Custom Email", callback_data="custom"),
        ],
        [
            InlineKeyboardButton("My Email", callback_data="mymail"),
            InlineKeyboardButton("Inbox", callback_data="inbox"),
        ],
        [
            InlineKeyboardButton("Search Inbox", callback_data="claim"),
            InlineKeyboardButton("History", callback_data="history"),
        ],
        [
            InlineKeyboardButton("Domains", callback_data="domains"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def after_generate_keyboard() -> InlineKeyboardMarkup:
    """Build the keyboard shown after generating an email."""
    keyboard = [
        [
            InlineKeyboardButton("Check Inbox", callback_data="inbox"),
            InlineKeyboardButton("Generate New", callback_data="generate"),
        ],
        [
            InlineKeyboardButton("Back to Menu", callback_data="menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def after_inbox_keyboard() -> InlineKeyboardMarkup:
    """Build the keyboard shown after checking inbox."""
    keyboard = [
        [
            InlineKeyboardButton("Refresh Inbox", callback_data="inbox"),
            InlineKeyboardButton("Back to Menu", callback_data="menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def api_request(
    method: str,
    endpoint: str,
    cookie: Optional[str] = None,
    json_body: Optional[dict] = None,
) -> tuple[Optional[Union[dict, list]], Optional[str]]:
    """Make a request to the TempMail API.

    Returns a tuple of (response_json, session_cookie).
    """
    url = f"{API_BASE}{endpoint}"
    headers = {}
    if cookie:
        headers["Cookie"] = f"tm_sid={cookie}"

    try:
        async with aiohttp.ClientSession() as session:
            if method.upper() == "GET":
                async with session.get(url, headers=headers) as resp:
                    new_cookie = _extract_cookie(resp)
                    data = await resp.json()
                    if isinstance(data, dict) and "error" in data:
                        logger.warning("API error response for %s: %s", endpoint, data)
                    return data, new_cookie or cookie
            else:
                async with session.post(url, headers=headers, json=json_body) as resp:
                    new_cookie = _extract_cookie(resp)
                    data = await resp.json()
                    if isinstance(data, dict) and "error" in data:
                        logger.warning("API error response for %s: %s", endpoint, data)
                    return data, new_cookie or cookie
    except Exception as e:
        logger.error("API request failed: %s", e)
        return None, cookie


def _extract_cookie(resp: aiohttp.ClientResponse) -> Optional[str]:
    """Extract the tm_sid cookie from the response."""
    cookies = resp.cookies
    if "tm_sid" in cookies:
        return cookies["tm_sid"].value
    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    welcome = (
        "Welcome to the Temp Mail Bot!\n\n"
        "I can generate temporary email addresses for you and check their inbox.\n\n"
        "Use the buttons below or type commands directly:\n"
        "/generate - Generate a new temporary email\n"
        "/newmail - Same as /generate\n"
        "/custom - Create email with custom username\n"
        "/claim - Search/recover inbox by email address\n"
        "/domains - Show available email domains\n"
        "/inbox - Check inbox for your current email\n"
        "/mymail - Show your current active email\n"
        "/history - Show previously generated emails\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(welcome, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command."""
    help_text = (
        "Available commands:\n\n"
        "/generate [domain] - Generate a new temporary email address. "
        "Optionally specify a domain.\n"
        "/newmail [domain] - Same as /generate\n"
        "/custom - Create email with custom username and domain selection\n"
        "/claim - Search/recover inbox by email address\n"
        "/domains - List all available email domains\n"
        "/inbox - Check the inbox for your current email\n"
        "/mymail - Show your current active email address\n"
        "/history - Show previously generated emails\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(help_text, reply_markup=main_menu_keyboard())


async def domains_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /domains command."""
    data, _ = await api_request("GET", "/domains")
    if data is None:
        await update.message.reply_text(
            "Failed to fetch domains. The API might be unavailable. Please try again later."
        )
        return

    domains = data.get("domains", [])
    if not domains:
        await update.message.reply_text("No domains available at the moment.")
        return

    domain_list = "\n".join(f"  - {d}" for d in domains)
    await update.message.reply_text(
        f"Available domains:\n{domain_list}",
        reply_markup=main_menu_keyboard(),
    )


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /generate and /newmail commands."""
    user_id = update.effective_user.id

    # Check if user specified a domain
    body: dict = {}
    if context.args:
        body["domain"] = context.args[0]

    data, cookie = await api_request("POST", "/generate", json_body=body)
    if data is None:
        await update.message.reply_text(
            "Failed to generate an email. The API might be unavailable. Please try again later."
        )
        return

    address = data.get("address")
    if not address:
        await update.message.reply_text(
            "Unexpected response from the API. Please try again later."
        )
        return

    # Store the session
    user_current_email[user_id] = address
    email_cookies[address] = cookie or ""

    # Store in history
    if user_id not in user_history:
        user_history[user_id] = []
    if address not in user_history[user_id]:
        user_history[user_id].append(address)

    await update.message.reply_text(
        f"Your new temporary email address:\n\n<code>{address}</code>\n\n"
        "Use /inbox to check for incoming messages.",
        parse_mode="HTML",
        reply_markup=after_generate_keyboard(),
    )


async def mymail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /mymail command."""
    user_id = update.effective_user.id
    email = user_current_email.get(user_id)

    if not email:
        await update.message.reply_text(
            "You don't have an active email address yet.\n"
            "Use /generate to create one.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        f"Your current email address:\n\n<code>{email}</code>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /inbox command."""
    user_id = update.effective_user.id
    email = user_current_email.get(user_id)

    if not email:
        await update.message.reply_text(
            "You don't have an active email address yet.\n"
            "Use /generate to create one first.",
            reply_markup=main_menu_keyboard(),
        )
        return

    cookie = email_cookies.get(email)

    text = await _fetch_inbox(email, cookie)
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=after_inbox_keyboard()
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /history command."""
    user_id = update.effective_user.id
    history = user_history.get(user_id, [])

    if not history:
        await update.message.reply_text(
            "You haven't generated any emails yet.\n"
            "Use /generate to create one.",
            reply_markup=main_menu_keyboard(),
        )
        return

    keyboard = []
    for email in history:
        keyboard.append(
            [InlineKeyboardButton(email, callback_data=f"history_inbox:{email}")]
        )
    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="menu")])

    await update.message.reply_text(
        "Your previously generated emails:\n\nTap an email to check its inbox.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _extract_links(text: str, html: str) -> list[dict[str, str]]:
    """Extract links from email text and HTML content."""
    links = []
    seen_urls: set[str] = set()

    # Extract from HTML: <a href="...">label</a>
    if html:
        for match in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            url = match.group(1).strip()
            label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if url.startswith(("http://", "https://")) and url not in seen_urls:
                seen_urls.add(url)
                if not label or len(label) > 60:
                    label = _guess_link_label(url)
                links.append({"url": url, "label": label})

    # Extract bare URLs from plain text
    if text:
        for match in re.finditer(r'(https?://[^\s<>"\')\]]+)', text):
            url = match.group(1).strip().rstrip(".,;:")
            if url not in seen_urls:
                seen_urls.add(url)
                links.append({"url": url, "label": _guess_link_label(url)})

    return links


def _guess_link_label(url: str) -> str:
    """Guess a friendly label for a link based on URL patterns."""
    url_lower = url.lower()
    if any(
        kw in url_lower
        for kw in ["confirm", "verify", "activate", "validation", "signup"]
    ):
        return "Confirm / Verify"
    if any(kw in url_lower for kw in ["reset", "password"]):
        return "Reset Password"
    if any(kw in url_lower for kw in ["unsubscribe"]):
        return "Unsubscribe"
    if any(kw in url_lower for kw in ["login", "signin", "sign-in", "magic"]):
        return "Login Link"
    return "Open Link"


def _extract_otp(text: str, subject: str) -> Optional[str]:
    """Try to find OTP/verification code in email."""
    combined = f"{subject} {text}"
    patterns = [
        r"(?:code|kode|otp|pin|token)[:\s=]+(\d{4,8})",
        r"(\d{4,8})\s*(?:is your|adalah)",
        r"(?:enter|masukkan|use|gunakan)[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return match.group(1)

    if any(
        kw in subject.lower()
        for kw in ["otp", "code", "verify", "kode", "verifikasi"]
    ):
        match = re.search(r"\b(\d{4,8})\b", combined)
        if match:
            return match.group(1)

    return None


async def _fetch_inbox(email: str, cookie: Optional[str]) -> str:
    """Fetch inbox for a given email and return formatted text."""
    data, new_cookie = await api_request("GET", f"/history/{email}", cookie=cookie)

    # Update cookie if changed
    if new_cookie:
        email_cookies[email] = new_cookie

    if data is None:
        return "Failed to fetch inbox. The API might be unavailable. Please try again later."

    # Handle API error responses like {"error": "forbidden"}
    if isinstance(data, dict) and "error" in data:
        return (
            f"Cannot access inbox for <code>{_escape_html(email)}</code>.\n"
            f"Error: {_escape_html(data['error'])}\n\n"
            "The session may have expired. Try generating a new email."
        )

    if not isinstance(data, list):
        return "Unexpected response format from the API. Please try again later."

    if not data:
        return f"Inbox for <code>{_escape_html(email)}</code> is empty.\n\nNo messages yet. Try again later."

    # Format messages - compact version with clickable links
    messages_text = (
        f"Inbox for <code>{_escape_html(email)}</code> ({len(data)} message"
        f"{'s' if len(data) > 1 else ''}):\n\n"
    )
    for i, msg in enumerate(data, 1):
        subject = msg.get("subject", "(No subject)")
        sender = msg.get("from", "Unknown")

        # receivedAt is Unix timestamp in milliseconds
        received_at = msg.get("receivedAt")
        if received_at:
            try:
                dt = datetime.fromtimestamp(received_at / 1000, tz=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError, OSError):
                date_str = "Unknown"
        else:
            date_str = "Unknown"

        text_body = msg.get("text", "")
        html_body = msg.get("html", "")

        # Extract OTP if present
        otp = _extract_otp(text_body, subject)

        # Extract links from both text and HTML
        links = _extract_links(text_body, html_body)

        # Short preview of body (first 150 chars, no full dump)
        preview = text_body.strip().replace("\n", " ")[:150]
        if len(text_body.strip()) > 150:
            preview += "..."

        messages_text += f"--- Message {i} ---\n"
        messages_text += f"From: {_escape_html(sender)}\n"
        messages_text += f"Subject: {_escape_html(subject)}\n"
        messages_text += f"Date: {date_str}\n"

        if otp:
            messages_text += f"\n<b>OTP Code: {otp}</b>\n"

        if preview:
            messages_text += f"\nPreview: {_escape_html(preview)}\n"

        if links:
            messages_text += "\nLinks:\n"
            for link in links[:5]:  # Max 5 links per message to keep it light
                messages_text += f'  <a href="{_escape_html(link["url"])}">{_escape_html(link["label"])}</a>\n'

        messages_text += "\n"

    # Telegram has a 4096 char limit for messages
    if len(messages_text) > 4000:
        messages_text = messages_text[:4000] + "\n\n(Truncated)"

    return messages_text


async def custom_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /custom command - start custom email flow."""
    user_id = update.effective_user.id

    # Fetch available domains
    data, _ = await api_request("GET", "/domains")
    if data is None:
        await update.message.reply_text(
            "Failed to fetch domains. The API might be unavailable. Please try again later.",
            reply_markup=main_menu_keyboard(),
        )
        return

    domains = data.get("domains", [])
    if not domains:
        await update.message.reply_text(
            "No domains available at the moment.",
            reply_markup=main_menu_keyboard(),
        )
        return

    keyboard = [
        [InlineKeyboardButton("Random Domain", callback_data="custom_domain:random")]
    ]
    for domain in domains:
        keyboard.append(
            [InlineKeyboardButton(domain, callback_data=f"custom_domain:{domain}")]
        )
    keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="menu")])

    await update.message.reply_text(
        "Choose a domain for your custom email:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /claim command - search/recover inbox by email address."""
    user_id = update.effective_user.id
    user_pending_claim[user_id] = True
    await update.message.reply_text(
        "Type the email address you want to search/recover:\n\n"
        "This can be an email you used before, even from a previous session."
    )


async def handle_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text input from users (for claim or custom email username)."""
    user_id = update.effective_user.id

    # Check if user has a pending claim request (takes priority)
    if user_id in user_pending_claim:
        user_pending_claim.pop(user_id)
        email_input = update.message.text.strip()

        if not email_input or "@" not in email_input:
            await update.message.reply_text(
                "Please provide a valid email address (e.g. user@domain.com).\n"
                "Try again with /claim.",
                reply_markup=main_menu_keyboard(),
            )
            return

        data, cookie = await api_request("POST", "/claim", json_body={"address": email_input})
        if data is None:
            await update.message.reply_text(
                "Failed to search for that email. The API might be unavailable. "
                "Please try again later.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if isinstance(data, dict) and "error" in data:
            await update.message.reply_text(
                f"Error: {_escape_html(data['error'])}\n\n"
                "The email address might not exist. Please check and try again.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
            return

        address = data.get("address", email_input)
        known = data.get("known", False)
        history_count = data.get("historyCount", 0)

        # Store the session
        user_current_email[user_id] = address
        if cookie:
            email_cookies[address] = cookie

        # Store in history
        if user_id not in user_history:
            user_history[user_id] = []
        if address not in user_history[user_id]:
            user_history[user_id].append(address)

        if history_count > 0:
            await update.message.reply_text(
                f"Found <code>{_escape_html(address)}</code> with {history_count} message(s)!\n\n"
                "Fetching inbox...",
                parse_mode="HTML",
            )
            # Auto-check inbox
            inbox_cookie = email_cookies.get(address)
            text = await _fetch_inbox(address, inbox_cookie)
            await update.message.reply_text(
                text, parse_mode="HTML", reply_markup=after_inbox_keyboard()
            )
        elif known:
            await update.message.reply_text(
                f"Address <code>{_escape_html(address)}</code> is ready to receive emails.\n\n"
                "No messages yet. Check back later with /inbox.",
                parse_mode="HTML",
                reply_markup=after_inbox_keyboard(),
            )
        else:
            await update.message.reply_text(
                f"Address <code>{_escape_html(address)}</code> has been claimed.\n\n"
                "It's ready to receive emails. Check back later with /inbox.",
                parse_mode="HTML",
                reply_markup=after_inbox_keyboard(),
            )
        return

    # Check if user has a pending custom email request
    if user_id not in user_pending_custom:
        return

    pending = user_pending_custom.pop(user_id)
    username = update.message.text.strip()

    if not username:
        await update.message.reply_text(
            "Username cannot be empty. Please try again with /custom.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Build request body
    body: dict = {"localPart": username}
    if pending.get("domain"):
        body["domain"] = pending["domain"]

    data, cookie = await api_request("POST", "/generate", json_body=body)
    if data is None:
        await update.message.reply_text(
            "Failed to generate the email. The API might be unavailable. "
            "Please try again later.",
            reply_markup=main_menu_keyboard(),
        )
        return

    address = data.get("address")
    if not address:
        await update.message.reply_text(
            "Unexpected response from the API. Please try again later.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Store the session
    user_current_email[user_id] = address
    email_cookies[address] = cookie or ""

    # Store in history
    if user_id not in user_history:
        user_history[user_id] = []
    if address not in user_history[user_id]:
        user_history[user_id].append(address)

    await update.message.reply_text(
        f"Your new custom email address:\n\n<code>{address}</code>\n\n"
        "Use /inbox or tap Check Inbox to see incoming messages.",
        parse_mode="HTML",
        reply_markup=after_generate_keyboard(),
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if data == "menu":
        await query.edit_message_text(
            "What would you like to do?",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "generate":
        api_data, cookie = await api_request("POST", "/generate", json_body={})
        if api_data is None:
            await query.edit_message_text(
                "Failed to generate an email. The API might be unavailable. "
                "Please try again later.",
                reply_markup=main_menu_keyboard(),
            )
            return

        address = api_data.get("address")
        if not address:
            await query.edit_message_text(
                "Unexpected response from the API. Please try again later.",
                reply_markup=main_menu_keyboard(),
            )
            return

        # Store the session
        user_current_email[user_id] = address
        email_cookies[address] = cookie or ""

        # Store in history
        if user_id not in user_history:
            user_history[user_id] = []
        if address not in user_history[user_id]:
            user_history[user_id].append(address)

        await query.edit_message_text(
            f"Your new temporary email address:\n\n<code>{address}</code>\n\n"
            "Use /inbox or tap Check Inbox to see incoming messages.",
            parse_mode="HTML",
            reply_markup=after_generate_keyboard(),
        )

    elif data == "mymail":
        email = user_current_email.get(user_id)
        if not email:
            await query.edit_message_text(
                "You don't have an active email address yet.\n"
                "Tap 'Generate Email' to create one.",
                reply_markup=main_menu_keyboard(),
            )
            return

        await query.edit_message_text(
            f"Your current email address:\n\n<code>{email}</code>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "inbox":
        email = user_current_email.get(user_id)
        if not email:
            await query.edit_message_text(
                "You don't have an active email address yet.\n"
                "Tap 'Generate Email' to create one first.",
                reply_markup=main_menu_keyboard(),
            )
            return

        cookie = email_cookies.get(email)
        text = await _fetch_inbox(email, cookie)
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=after_inbox_keyboard()
        )

    elif data == "domains":
        api_data, _ = await api_request("GET", "/domains")
        if api_data is None:
            await query.edit_message_text(
                "Failed to fetch domains. The API might be unavailable. "
                "Please try again later.",
                reply_markup=main_menu_keyboard(),
            )
            return

        domains = api_data.get("domains", [])
        if not domains:
            await query.edit_message_text(
                "No domains available at the moment.",
                reply_markup=main_menu_keyboard(),
            )
            return

        domain_list = "\n".join(f"  - {d}" for d in domains)
        await query.edit_message_text(
            f"Available domains:\n{domain_list}",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "history":
        history = user_history.get(user_id, [])
        if not history:
            await query.edit_message_text(
                "You haven't generated any emails yet.\n"
                "Tap 'Generate Email' to create one.",
                reply_markup=main_menu_keyboard(),
            )
            return

        keyboard = []
        for email in history:
            keyboard.append(
                [InlineKeyboardButton(email, callback_data=f"history_inbox:{email}")]
            )
        keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="menu")])

        await query.edit_message_text(
            "Your previously generated emails:\n\nTap an email to check its inbox.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("history_inbox:"):
        email = data[len("history_inbox:"):]
        # Use the cookie associated with this specific email
        cookie = email_cookies.get(email)
        text = await _fetch_inbox(email, cookie)
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=after_inbox_keyboard()
        )

    elif data == "custom":
        # Custom email flow: fetch domains and show selection
        api_data, _ = await api_request("GET", "/domains")
        if api_data is None:
            await query.edit_message_text(
                "Failed to fetch domains. The API might be unavailable. "
                "Please try again later.",
                reply_markup=main_menu_keyboard(),
            )
            return

        domains = api_data.get("domains", [])
        if not domains:
            await query.edit_message_text(
                "No domains available at the moment.",
                reply_markup=main_menu_keyboard(),
            )
            return

        keyboard = [
            [InlineKeyboardButton("Random Domain", callback_data="custom_domain:random")]
        ]
        for domain in domains:
            keyboard.append(
                [InlineKeyboardButton(domain, callback_data=f"custom_domain:{domain}")]
            )
        keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="menu")])

        await query.edit_message_text(
            "Choose a domain for your custom email:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("custom_domain:"):
        chosen = data[len("custom_domain:"):]
        if chosen == "random":
            user_pending_custom[user_id] = {"domain": None}
            await query.edit_message_text(
                "You chose a random domain.\n\n"
                "Now type the username you want (the part before @):"
            )
        else:
            user_pending_custom[user_id] = {"domain": chosen}
            await query.edit_message_text(
                f"You chose domain: {chosen}\n\n"
                "Now type the username you want (the part before @):"
            )

    elif data == "claim":
        user_pending_claim[user_id] = True
        await query.edit_message_text(
            "Type the email address you want to search/recover:\n\n"
            "This can be an email you used before, even from a previous session."
        )


def main() -> None:
    """Start the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set.")
        print("Error: Please set the TELEGRAM_BOT_TOKEN environment variable.")
        print("See .env.example for reference.")
        return

    application = Application.builder().token(token).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("domains", domains_command))
    application.add_handler(CommandHandler("generate", generate_command))
    application.add_handler(CommandHandler("newmail", generate_command))
    application.add_handler(CommandHandler("custom", custom_command))
    application.add_handler(CommandHandler("claim", claim_command))
    application.add_handler(CommandHandler("inbox", inbox_command))
    application.add_handler(CommandHandler("mymail", mymail_command))
    application.add_handler(CommandHandler("history", history_command))

    # Register callback query handler for inline keyboard buttons
    application.add_handler(CallbackQueryHandler(button_callback))

    # Register message handler for text input (custom email username)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
