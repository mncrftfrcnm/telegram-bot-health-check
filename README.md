# Aiogram Bot-to-Bot Monitor

Async Python 3 / aiogram 3 service that monitors another Telegram bot by sending configured messages or commands, waiting for replies, checking optional expectations, and reporting status to configured users or chats.

## Features

- Sends test messages to another Telegram bot.
- Runs checks on startup and on a fixed interval.
- Supports `contains`, exact-match, and regex expectations.
- Can report compact status or full command replies.
- Sends reports to multiple users/chats.
- Can edit the previous report message instead of sending a new one each cycle.
- Provides manual `/run_now`, `/status`, `/config`, and `/my_id` commands.
- Supports Docker Compose deployment.

## Important Telegram requirement

Bot-to-bot communication must be enabled for both bots in BotFather. Users who should receive reports must also open the monitoring bot and send `/start` once before the bot can message them.

## Quick start

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` and set your monitor bot token:

```env
MONITOR_BOT_TOKEN=123456:replace_me
```

Edit `config.yaml` and set:

- `target_bot` to the bot you want to test, for example `@YourTargetBot`
- `commands` to the messages/checks you want to run
- `report.user_chat_ids` to the recipients that should receive reports

To get a chat ID, start the monitor bot in Telegram and run `/my_id`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Run with Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

The Compose setup reads secrets from `.env` and mounts `config.yaml` as read-only.

## Configuration

The main configuration lives in `config.yaml`.

### Schedule

```yaml
schedule:
  run_on_start: true
  interval_seconds: 300
  timeout_seconds: 45
  delay_between_commands_seconds: 2
```

### Commands

```yaml
commands:
  - name: health
    message: "/health"
    enabled: true
    expect_contains: "ok"
```

If no expectation is configured, any reply from the target bot counts as OK.

Supported expectations:

```yaml
expect_contains: "ok"
expect_equals: "exact reply"
expect_regex: "status:\\s*ok"
```

### Reporting

`report.mode` can be:

- `status`: only OK/FAIL/TIMEOUT per command
- `full`: include actual target bot replies

```yaml
report:
  send_to_users: true
  user_chat_ids:
    - 123456789
  mode: "status"
  edit_previous_message: true
  also_write_to_target_chat: false
  max_reply_chars: 1000
```

## Security notes

- Keep real bot tokens in `.env` or environment variables, not in `config.yaml`.
- Do not commit `.env` or `config.yaml`; both are ignored by `.gitignore`.
- Only give report access to chat IDs you trust.
- Prefer running the container with the provided non-root Docker setup.

## Troubleshooting

If checks time out:

1. Confirm both bots have bot-to-bot communication enabled in BotFather.
2. Confirm `target_bot` is the correct Telegram username.
3. Increase `schedule.timeout_seconds` if the target bot is slow.
4. Check `docker compose logs -f` or local logs for Telegram API errors.

If reports are not delivered:

1. Open the monitoring bot in Telegram.
2. Send `/start`.
3. Run `/my_id`.
4. Add that chat ID to `report.user_chat_ids`.
5. Restart the service.