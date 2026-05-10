# Temp Mail Telegram Bot

A Telegram bot that generates temporary email addresses and checks their inbox using the TempMail API.

## Features

- Generate temporary email addresses
- Choose from multiple available domains
- Check inbox for incoming messages
- Per-user session management

## Setup

### Prerequisites

- Python 3.10 or higher
- A Telegram Bot Token (get one from [@BotFather](https://t.me/BotFather))

### Installation

1. Clone this repository:

```bash
git clone <repository-url>
cd main
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file from the example:

```bash
cp .env.example .env
```

4. Edit `.env` and add your Telegram bot token:

```
TELEGRAM_BOT_TOKEN=your-actual-bot-token
```

### Running the Bot

```bash
python bot.py
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message with instructions |
| `/generate [domain]` | Generate a new temporary email (optionally specify domain) |
| `/newmail [domain]` | Same as `/generate` |
| `/domains` | Show available email domains |
| `/inbox` | Check inbox for your current email |
| `/mymail` | Show your current active email address |
| `/help` | Show all available commands |

## How It Works

The bot uses the TempMail API to create disposable email addresses. Each Telegram user gets their own session, so multiple users can use the bot simultaneously without interfering with each other.

Sessions are stored in memory, so they will be lost when the bot restarts. Simply generate a new email address after a restart.

## API

This bot uses the TempMail API at:
`https://tempmail-worker.fattanafif02.workers.dev/api/public`
