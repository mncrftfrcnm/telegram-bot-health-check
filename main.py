import asyncio
import html
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message


@dataclass
class CommandCheck:
    name: str
    message: str
    enabled: bool = True
    expect_contains: str | None = None
    expect_equals: str | None = None
    expect_regex: str | None = None
    timeout_seconds: int | None = None


@dataclass
class ScheduleConfig:
    run_on_start: bool = True
    interval_seconds: int = 300
    timeout_seconds: int = 45
    delay_between_commands_seconds: int = 2


@dataclass
class ReportConfig:
    send_to_users: bool = True
    user_chat_ids: list[int] = field(default_factory=list)
    mode: str = "status"  # "status" or "full"
    edit_previous_message: bool = True
    also_write_to_target_chat: bool = False
    max_reply_chars: int = 1000


@dataclass
class AppConfig:
    token: str
    target_bot: str
    schedule: ScheduleConfig
    commands: list[CommandCheck]
    report: ReportConfig


@dataclass
class CheckResult:
    name: str
    command: str
    status: str
    elapsed_seconds: float
    reply_text: str | None = None
    error: str | None = None
    expected: str | None = None


class MonitorState:
    def __init__(self, config: AppConfig):
        self.config = config
        self.reply_queue: asyncio.Queue[Message] = asyncio.Queue()
        self.last_results: list[CheckResult] = []
        self.last_run_started_at: float | None = None
        self.last_run_finished_at: float | None = None
        self.last_report_message_ids: dict[int | str, int] = {}
        self.shutdown_event = asyncio.Event()
        self.run_lock = asyncio.Lock()

    @property
    def target_username(self) -> str:
        return self.config.target_bot.lstrip("@").lower()


def load_config() -> AppConfig:
    load_dotenv()
    config_path = Path(os.getenv("CONFIG_PATH", "config.yaml"))
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config.example.yaml to config.yaml."
        )

    raw = yaml.safe_load(config_path.read_text()) or {}

    token_env = raw.get("bot_token_env", "MONITOR_BOT_TOKEN")
    token = os.getenv(token_env) or raw.get("bot_token")
    if not token:
        raise RuntimeError(
            f"Bot token is missing. Set {token_env}=... in .env/environment "
            "or put bot_token in config.yaml."
        )

    target_bot = raw.get("target_bot")
    if not target_bot or not str(target_bot).startswith("@"):
        raise RuntimeError('target_bot must be set and must look like "@SomeBot".')

    schedule_raw = raw.get("schedule", {}) or {}
    report_raw = raw.get("report", {}) or {}

    commands = []
    for item in raw.get("commands", []) or []:
        if not item.get("message"):
            continue
        commands.append(
            CommandCheck(
                name=str(item.get("name") or item["message"]),
                message=str(item["message"]),
                enabled=bool(item.get("enabled", True)),
                expect_contains=item.get("expect_contains"),
                expect_equals=item.get("expect_equals"),
                expect_regex=item.get("expect_regex"),
                timeout_seconds=item.get("timeout_seconds"),
            )
        )

    if not commands:
        raise RuntimeError("No commands configured. Add at least one item under commands:.")

    mode = str(report_raw.get("mode", "status")).lower()
    if mode not in {"status", "full"}:
        raise RuntimeError('report.mode must be either "status" or "full".')

    user_chat_ids = []
    for chat_id in report_raw.get("user_chat_ids", []) or []:
        try:
            user_chat_ids.append(int(chat_id))
        except (TypeError, ValueError):
            logging.warning("Ignoring invalid user_chat_id: %r", chat_id)

    return AppConfig(
        token=token,
        target_bot=str(target_bot),
        schedule=ScheduleConfig(
            run_on_start=bool(schedule_raw.get("run_on_start", True)),
            interval_seconds=int(schedule_raw.get("interval_seconds", 300)),
            timeout_seconds=int(schedule_raw.get("timeout_seconds", 45)),
            delay_between_commands_seconds=int(
                schedule_raw.get("delay_between_commands_seconds", 2)
            ),
        ),
        commands=commands,
        report=ReportConfig(
            send_to_users=bool(report_raw.get("send_to_users", True)),
            user_chat_ids=user_chat_ids,
            mode=mode,
            edit_previous_message=bool(report_raw.get("edit_previous_message", True)),
            also_write_to_target_chat=bool(
                report_raw.get("also_write_to_target_chat", False)
            ),
            max_reply_chars=int(report_raw.get("max_reply_chars", 1000)),
        ),
    )


def short_text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 20)] + "\n…[truncated]"


def expected_description(command: CommandCheck) -> str | None:
    if command.expect_contains:
        return f"contains: {command.expect_contains!r}"
    if command.expect_equals:
        return f"equals: {command.expect_equals!r}"
    if command.expect_regex:
        return f"regex: {command.expect_regex!r}"
    return None


def evaluate_reply(command: CommandCheck, reply_text: str) -> tuple[bool, str | None]:
    if command.expect_contains:
        if command.expect_contains.lower() not in reply_text.lower():
            return False, f"Expected text not found: {command.expect_contains!r}"
    if command.expect_equals:
        if reply_text.strip() != command.expect_equals.strip():
            return False, f"Reply did not equal expected text: {command.expect_equals!r}"
    if command.expect_regex:
        if not re.search(command.expect_regex, reply_text, flags=re.IGNORECASE | re.DOTALL):
            return False, f"Regex did not match: {command.expect_regex!r}"
    return True, None


def render_report(state: MonitorState, results: list[CheckResult]) -> str:
    cfg = state.config
    target = html.escape(cfg.target_bot)
    started = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.last_run_started_at))
        if state.last_run_started_at
        else "unknown"
    )

    ok_count = sum(1 for r in results if r.status == "ok")
    total_count = len(results)
    overall = "OK" if ok_count == total_count else "FAILED"

    lines = [
        f"<b>Bot monitor: {target}</b>",
        f"Overall: <b>{overall}</b> ({ok_count}/{total_count} OK)",
        f"Checked at: <code>{html.escape(started)}</code>",
        "",
    ]

    for result in results:
        icon = {
            "ok": "✅",
            "fail": "❌",
            "timeout": "⏱️",
            "error": "💥",
            "skipped": "⏭️",
        }.get(result.status, "•")

        lines.append(
            f"{icon} <b>{html.escape(result.name)}</b> "
            f"<code>{html.escape(result.command)}</code> "
            f"— <b>{html.escape(result.status.upper())}</b> "
            f"({result.elapsed_seconds:.1f}s)"
        )

        if result.expected and result.status != "ok":
            lines.append(f"   Expected: <code>{html.escape(result.expected)}</code>")

        if result.error:
            lines.append(f"   Error: <code>{html.escape(result.error)}</code>")

        if cfg.report.mode == "full" and result.reply_text:
            reply = short_text(result.reply_text, cfg.report.max_reply_chars)
            lines.append("   Reply:")
            lines.append(f"<blockquote>{html.escape(reply)}</blockquote>")

    return "\n".join(lines)


async def drain_reply_queue(state: MonitorState) -> None:
    while True:
        try:
            state.reply_queue.get_nowait()
        except asyncio.QueueEmpty:
            return


async def run_single_check(bot: Bot, state: MonitorState, command: CommandCheck) -> CheckResult:
    timeout = command.timeout_seconds or state.config.schedule.timeout_seconds
    start = time.monotonic()

    try:
        await drain_reply_queue(state)

        sent = await bot.send_message(
            chat_id=state.config.target_bot,
            text=command.message,
            disable_notification=True,
        )
        logging.info(
            "Sent command %s to %s, message_id=%s",
            command.name,
            state.config.target_bot,
            sent.message_id,
        )

        try:
            reply = await asyncio.wait_for(state.reply_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return CheckResult(
                name=command.name,
                command=command.message,
                status="timeout",
                elapsed_seconds=time.monotonic() - start,
                error=f"No reply within {timeout}s",
                expected=expected_description(command),
            )

        reply_text = reply.text or reply.caption or ""
        is_ok, error = evaluate_reply(command, reply_text)
        return CheckResult(
            name=command.name,
            command=command.message,
            status="ok" if is_ok else "fail",
            elapsed_seconds=time.monotonic() - start,
            reply_text=reply_text,
            error=error,
            expected=expected_description(command),
        )

    except Exception as exc:
        logging.exception("Check failed for %s", command.name)
        return CheckResult(
            name=command.name,
            command=command.message,
            status="error",
            elapsed_seconds=time.monotonic() - start,
            error=f"{type(exc).__name__}: {exc}",
            expected=expected_description(command),
        )


async def send_or_edit_report(bot: Bot, state: MonitorState, chat_id: int | str, text: str) -> None:
    if state.config.report.edit_previous_message:
        previous_id = state.last_report_message_ids.get(chat_id)
        if previous_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=previous_id,
                    text=text,
                )
                return
            except TelegramBadRequest as exc:
                logging.warning("Could not edit previous report for %s: %s", chat_id, exc)

    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, disable_notification=True)
        state.last_report_message_ids[chat_id] = msg.message_id
    except TelegramForbiddenError:
        logging.warning(
            "Cannot send to %s. User probably did not start the bot or blocked it.",
            chat_id,
        )
    except Exception:
        logging.exception("Could not send report to %s", chat_id)


async def publish_report(bot: Bot, state: MonitorState, results: list[CheckResult]) -> None:
    text = render_report(state, results)
    cfg = state.config

    if cfg.report.send_to_users:
        for chat_id in cfg.report.user_chat_ids:
            await send_or_edit_report(bot, state, chat_id, text)

    if cfg.report.also_write_to_target_chat:
        await send_or_edit_report(bot, state, cfg.target_bot, text)


async def run_all_checks(bot: Bot, state: MonitorState, publish: bool = True) -> list[CheckResult]:
    async with state.run_lock:
        state.last_run_started_at = time.time()
        results: list[CheckResult] = []

        enabled_commands = [cmd for cmd in state.config.commands if cmd.enabled]
        for index, command in enumerate(enabled_commands):
            result = await run_single_check(bot, state, command)
            results.append(result)

            if index < len(enabled_commands) - 1:
                await asyncio.sleep(state.config.schedule.delay_between_commands_seconds)

        state.last_results = results
        state.last_run_finished_at = time.time()

        if publish:
            await publish_report(bot, state, results)

        return results


async def monitoring_loop(bot: Bot, state: MonitorState) -> None:
    cfg = state.config.schedule

    if cfg.run_on_start:
        await run_all_checks(bot, state, publish=True)

    while not state.shutdown_event.is_set():
        try:
            await asyncio.wait_for(state.shutdown_event.wait(), timeout=cfg.interval_seconds)
        except asyncio.TimeoutError:
            await run_all_checks(bot, state, publish=True)


def build_router(state: MonitorState) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "Monitor bot is running.\n\n"
            f"Your chat_id: <code>{message.chat.id}</code>\n\n"
            "Commands:\n"
            "/my_id - show your chat id\n"
            "/run_now - run checks now\n"
            "/status - show last result\n"
            "/config - show current config summary"
        )

    @router.message(Command("my_id"))
    async def my_id(message: Message) -> None:
        await message.answer(f"Your chat_id: <code>{message.chat.id}</code>")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not state.last_results:
            await message.answer("No checks have run yet.")
            return
        await message.answer(render_report(state, state.last_results))

    @router.message(Command("config"))
    async def config_summary(message: Message) -> None:
        enabled = [cmd for cmd in state.config.commands if cmd.enabled]
        lines = [
            "<b>Config summary</b>",
            f"Target bot: <code>{html.escape(state.config.target_bot)}</code>",
            f"Interval: <code>{state.config.schedule.interval_seconds}s</code>",
            f"Timeout: <code>{state.config.schedule.timeout_seconds}s</code>",
            f"Report mode: <code>{html.escape(state.config.report.mode)}</code>",
            f"Recipients: <code>{len(state.config.report.user_chat_ids)}</code>",
            "",
            "<b>Enabled commands</b>",
        ]
        for cmd in enabled:
            lines.append(f"• <b>{html.escape(cmd.name)}</b>: <code>{html.escape(cmd.message)}</code>")
        await message.answer("\n".join(lines))

    @router.message(Command("run_now"))
    async def run_now(message: Message, bot: Bot) -> None:
        wait_msg = await message.answer("Running checks now...")
        results = await run_all_checks(bot, state, publish=False)
        await wait_msg.edit_text(render_report(state, results))

    @router.message()
    async def receive_any(message: Message) -> None:
        # Replies from the tested bot arrive here.
        if message.from_user and message.from_user.is_bot:
            username = (message.from_user.username or "").lower()
            if username == state.target_username:
                await state.reply_queue.put(message)
                logging.info("Queued reply from target bot @%s", username)
                return

        if message.text:
            await message.answer(
                "I received your message, but I only monitor the configured target bot.\n"
                "Use /run_now, /status, /config, or /my_id."
            )

    return router


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    state = MonitorState(config)

    bot = Bot(
        token=config.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(build_router(state))

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        logging.info("Shutdown requested")
        state.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass

    monitor_task = asyncio.create_task(monitoring_loop(bot, state))

    try:
        await dp.start_polling(bot)
    finally:
        state.shutdown_event.set()
        monitor_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
