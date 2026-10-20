"""Entry point.

  python bot.py                       run the bot (settings from .env)
  python bot.py --settings-from-env   run in a container (Railway): settings from environment variables
  python bot.py --check-claude        check the Claude CLI setup without Telegram
  python bot.py --try "text"          show the reply to a made-up message without Telegram
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

from assistant.claude_cli import ClaudeError
from assistant.config import PROJECT_DIR, SETTING_KEYS, ConfigError, Settings, env_file_warning, load_settings
from assistant.language import detect_language
from assistant.prompts import build_system_prompt, build_user_prompt, parse_agent_reply
from assistant.service import (
    MAX_CARDS,
    SEARCH_LIMIT,
    build_assistant,
    card_summary,
    make_claude_cli,
    not_found_text,
    profile_is_filled,
    read_profile,
)
from assistant.store import ensure_private_dir
from assistant.vdl_search import make_vdl_search

ENV_FILE = PROJECT_DIR / ".env"
BAD_API_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}

log = logging.getLogger("bot")


def settings_from_process_env() -> Dict[str, str]:
    """Read the settings from os.environ and remove them, so `claude` never inherits them."""
    try:
        return {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


def check_init(init: Optional[dict]) -> List[str]:
    if not init:
        return ["no init event received from the Claude CLI"]
    problems = []
    for key in ("tools", "mcp_servers", "plugins", "skills"):
        if init.get(key):
            problems.append(f"{key} is not empty: {init.get(key)!r}")
    if init.get("apiKeySource") in BAD_API_KEY_SOURCES:
        problems.append(f"apiKeySource is {init.get('apiKeySource')!r}: an API key is being used, not the subscription")
    if init.get("apiProvider") not in ("firstParty", None):
        problems.append(f"apiProvider is {init.get('apiProvider')!r}, expected firstParty")
    return problems


async def check_claude(settings: Settings) -> int:
    cli = make_claude_cli(settings)
    try:
        report = await cli.inspect()
    except ClaudeError as exc:
        print(json.dumps({"auth": cli.auth_mode, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    problems = check_init(report.get("init"))
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"OK: Claude CLI works, auth: {report['auth']}", file=sys.stderr)
    return 0


async def try_reply(settings: Settings, text: str) -> int:
    cli = make_claude_cli(settings)
    searcher = make_vdl_search(settings)
    language = detect_language(text)
    system_prompt = build_system_prompt(
        read_profile(settings.profile_path), datetime.now().astimezone(), search_enabled=searcher is not None
    )
    user_prompt = build_user_prompt([{"role": "other", "name": "Friend", "text": text}], language)
    try:
        raw = await cli.generate(system_prompt, user_prompt)
    except ClaudeError as exc:
        print(f"Claude error: {exc}", file=sys.stderr)
        return 1
    reply, search = parse_agent_reply(raw)
    print(f"language: {language}\nmessage:  {text}\nreply:    {reply}")
    if search and searcher is not None:
        print(f"search:   {json.dumps(search, ensure_ascii=False)}")
        items = await searcher.search(search["q"], search["type"], SEARCH_LIMIT, search.get("max_price"))
        for item in items[:MAX_CARDS]:
            print(f"card:     {card_summary(item)} | {item.get('link') or '-'}")
        if not items:
            print(f"card:     (nothing found) -> {not_found_text(language)}")
    return 0


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    from assistant.handlers import ALLOWED_UPDATES, build_router

    assistant = build_assistant(settings)
    bot = Bot(settings.bot_token)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram rejected TELEGRAM_BOT_TOKEN. Check the token from @BotFather.")
            return 2
        dp = Dispatcher()
        dp.include_router(build_router(assistant))
        log.info("Bot @%s is running", me.username)
        if settings.auto_send:
            log.warning("Mode: AUTO-SEND — replies are sent on your behalf")
        else:
            log.info("Mode: PREVIEW — replies are only shown in the logs (set AUTO_SEND=true to send)")
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Claude binary: %s", settings.claude_bin)
        log.info("VDL Market search: %s", "on" if settings.vdl_search_enabled else "off (VDL_API_KEY not set)")
        if assistant.state.paused:
            log.warning("The assistant starts PAUSED (send /resume to the bot)")
        if not profile_is_filled(read_profile(settings.profile_path)):
            log.warning("%s is not filled in yet; replies will be generic", settings.profile_path)
        if settings.relay_mode:
            await serve_relay(dp, bot, settings)
        else:
            await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        return 0
    finally:
        await bot.session.close()


async def serve_relay(dp, bot, settings: Settings) -> None:
    """Relay mode: no polling (the mini app backend owns the webhook); updates arrive at POST /relay."""
    from aiohttp import web

    from assistant.relay import build_relay_app

    runner = web.AppRunner(build_relay_app(dp, bot, settings.relay_secret or ""))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.port).start()
    log.info("Relay mode: listening on port %s (POST /relay, GET /health); Telegram polling is off", settings.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram Business personal assistant")
    parser.add_argument("--settings-from-env", action="store_true", help="read settings from environment variables")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-claude", action="store_true", help="check the Claude CLI setup (no Telegram)")
    group.add_argument("--try", dest="try_text", metavar="TEXT", help="show a reply to TEXT (no Telegram)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    offline = args.check_claude or args.try_text is not None
    try:
        if args.settings_from_env:
            settings = load_settings(ENV_FILE, require_telegram=not offline, source=settings_from_process_env())
        else:
            settings = load_settings(ENV_FILE, require_telegram=not offline)
            warning = env_file_warning(ENV_FILE)
            if warning:
                log.warning(warning)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.claude_workdir)

    if args.check_claude:
        return asyncio.run(check_claude(settings))
    if args.try_text is not None:
        return asyncio.run(try_reply(settings, args.try_text))
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
