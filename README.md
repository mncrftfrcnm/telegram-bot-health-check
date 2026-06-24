# Aiogram Bot-to-Bot Monitor

This is an async Python 3 / aiogram 3 bot that tests another Telegram bot by sending configured messages/commands, waiting for replies, checking optional expected output, and reporting status or full results to users.

## Features

- send test messages to another bot
- run checks on startup and every N seconds
- optional expected text / equals / regex checks
- report only command status or full command replies
- send reports to multiple users/chats
- optionally edit the previous report message instead of sending a new one
- manual `/run_now`
- `/my_id` helper for getting chat IDs

## Important Telegram requirement

Bot-to-bot communication must be enabled for both bots in BotFather.

## Setup

```bash
cp .env.example .env
cp config.example.yaml config.yaml
nano .env
nano config.yaml
```

Users who should receive reports must first open the monitoring bot and send `/start`.
Then add their shown `chat_id` to `config.yaml`.

## Run locally

```bash
pip install -r requirements.txt
python main.py
```

## Run with Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

## Config notes

`report.mode`:

- `status`: only OK/FAIL/TIMEOUT per command
- `full`: include actual target bot replies

Command expectations:

```yaml
expect_contains: "ok"
expect_equals: "exact reply"
expect_regex: "status:\s*ok"
```

If no expectation is configured, any reply from the target bot counts as OK.
