"""aiogram router.

business mode: business updates go to the Assistant; owner commands in the private chat.
relay mode: private messages to the bot (forwarded by the mini app backend) are answered too.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, Message

from .service import Assistant

ALLOWED_UPDATES = ["message", "business_connection", "business_message"]

STRANGER_START_TEXT = (
    "Salom! Bu shaxsiy yordamchi bot, u faqat o'z egasi uchun ishlaydi. "
    "Bu yerda suhbatlashib bo'lmaydi — iltimos, egasiga to'g'ridan-to'g'ri yozing."
)


def _is_command(message: Message) -> bool:
    return bool(message.text and message.text.startswith("/"))


def build_router(assistant: Assistant) -> Router:
    router = Router(name="assistant")
    owner_id = assistant.settings.owner_id
    relay = assistant.settings.relay_mode

    @router.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        await assistant.on_business_connection(connection)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        await assistant.on_business_message(bot, message)

    @router.message(CommandStart(), F.chat.type == "private")
    async def on_start(message: Message) -> None:
        if message.from_user is not None and message.from_user.id == owner_id:
            await message.answer(assistant.start_text())
        elif not relay:  # in relay mode /start belongs to the mini app backend
            await message.answer(STRANGER_START_TEXT)

    @router.message(Command("pause"), F.chat.type == "private", F.from_user.id == owner_id)
    async def on_pause(message: Message) -> None:
        assistant.pause()
        await message.answer("Paused. I will not reply to anyone until /resume.")

    @router.message(Command("resume"), F.chat.type == "private", F.from_user.id == owner_id)
    async def on_resume(message: Message) -> None:
        assistant.resume()
        await message.answer("Resumed. Pause, usage-limit wait and takeover timers are cleared.")

    @router.message(Command("status"), F.chat.type == "private", F.from_user.id == owner_id)
    async def on_status(message: Message) -> None:
        await message.answer(assistant.status_text())

    if relay:

        @router.message(F.chat.type == "private", lambda message: not _is_command(message))
        async def on_direct_message(message: Message, bot: Bot) -> None:
            await assistant.on_direct_message(bot, message)

    # Anything else sent directly to the bot is silently ignored.
    return router
