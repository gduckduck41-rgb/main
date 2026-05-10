from __future__ import annotations

import os
import logging
from typing import Optional, Union

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

API_BASE = "https://tempmail-worker.fattanafif02.workers.dev/api/public"

# In-memory storage: telegram user_id -> {"cookie": str, "email": str}
user_sessions: dict[int, dict[str, str]] = {}


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
                    return data, new_cookie or cookie
            else:
                async with session.post(url, headers=headers, json=json_body) as resp:
                    new_cookie = _extract_cookie(resp)
                    data = await resp.json()
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
        "Commands:\n"
        "/generate - Generate a new temporary email\n"
        "/newmail - Same as /generate\n"
        "/domains - Show available email domains\n"
        "/inbox - Check inbox for your current email\n"
        "/mymail - Show your current active email\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command."""
    help_text = (
        "Available commands:\n\n"
        "/generate [domain] - Generate a new temporary email address. "
        "Optionally specify a domain.\n"
        "/newmail [domain] - Same as /generate\n"
        "/domains - List all available email domains\n"
        "/inbox - Check the inbox for your current email\n"
        "/mymail - Show your current active email address\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(help_text)


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
    await update.message.reply_text(f"Available domains:\n{domain_list}")


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
    user_sessions[user_id] = {"cookie": cookie or "", "email": address}

    await update.message.reply_text(
        f"Your new temporary email address:\n\n`{address}`\n\n"
        "Use /inbox to check for incoming messages.",
        parse_mode="Markdown",
    )


async def mymail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /mymail command."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    if not session:
        await update.message.reply_text(
            "You don't have an active email address yet.\n"
            "Use /generate to create one."
        )
        return

    await update.message.reply_text(
        f"Your current email address:\n\n`{session['email']}`",
        parse_mode="Markdown",
    )


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /inbox command."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    if not session:
        await update.message.reply_text(
            "You don't have an active email address yet.\n"
            "Use /generate to create one first."
        )
        return

    email = session["email"]
    cookie = session.get("cookie")

    data, new_cookie = await api_request("GET", f"/history/{email}", cookie=cookie)

    # Update cookie if changed
    if new_cookie:
        user_sessions[user_id]["cookie"] = new_cookie

    if data is None:
        await update.message.reply_text(
            "Failed to fetch inbox. The API might be unavailable. Please try again later."
        )
        return

    if not data:
        await update.message.reply_text(
            f"Inbox for `{email}` is empty.\n\n"
            "No messages yet. Try again later.",
            parse_mode="Markdown",
        )
        return

    # Format messages
    messages_text = f"Inbox for `{email}`:\n\n"
    for i, msg in enumerate(data, 1):
        subject = msg.get("subject", "(No subject)")
        sender = msg.get("from", "Unknown")
        date = msg.get("date", "Unknown date")
        body = msg.get("body", msg.get("text", ""))

        # Truncate body if too long
        if len(body) > 500:
            body = body[:500] + "..."

        messages_text += (
            f"--- Message {i} ---\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Date: {date}\n"
        )
        if body:
            messages_text += f"Body: {body}\n"
        messages_text += "\n"

    # Telegram has a 4096 char limit for messages
    if len(messages_text) > 4000:
        messages_text = messages_text[:4000] + "\n\n(Message truncated)"

    await update.message.reply_text(messages_text, parse_mode="Markdown")


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
    application.add_handler(CommandHandler("inbox", inbox_command))
    application.add_handler(CommandHandler("mymail", mymail_command))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
