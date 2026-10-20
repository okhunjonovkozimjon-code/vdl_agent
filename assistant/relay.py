"""HTTP relay for relay mode.

The mini app backend (Lovable) owns the bot's Telegram webhook. It forwards the updates it does not handle
itself (plain private messages) to `POST /relay` with `Authorization: Bearer <RELAY_SECRET>`; the body is the
raw Telegram update JSON. The update is fed into the same aiogram dispatcher in the background, so the
backend gets `200` immediately and the reply is sent to the customer through the Bot API.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Set

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import Update

log = logging.getLogger("assistant.relay")

MAX_BODY_BYTES = 1024 * 1024
RELAY_TASKS = web.AppKey("relay_tasks", set)


def build_relay_app(dispatcher: Dispatcher, bot: Bot, secret: str) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    tasks: Set["asyncio.Task[None]"] = set()
    app[RELAY_TASKS] = tasks
    expected = f"Bearer {secret}".encode()

    async def process(update: Update) -> None:
        try:
            await dispatcher.feed_update(bot, update)
        except Exception:  # a failed update must not stop the server
            log.exception("Processing relayed update %s failed", update.update_id)

    async def relay(request: web.Request) -> web.Response:
        given = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
            update = Update.model_validate(data, context={"bot": bot})
        except Exception:
            return web.json_response({"ok": False, "error": "body must be a Telegram update"}, status=400)
        task = asyncio.create_task(process(update))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.json_response({"ok": True})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def drain(app: web.Application) -> None:
        if tasks:
            await asyncio.wait(set(tasks), timeout=10)

    app.router.add_post("/relay", relay)
    app.router.add_get("/health", health)
    app.on_shutdown.append(drain)
    return app
