from __future__ import annotations

import asyncio
import itertools
import json
import time

import httpx
import pytest
from aiogram.methods import GetBusinessConnection
from aiohttp.test_utils import TestClient, TestServer

from assistant.config import ConfigError, load_settings
from assistant.relay import RELAY_TASKS, build_relay_app
from assistant.service import Outcome
from tests.conftest import OWNER_ID

SECRET = "relay-secret-" + "x" * 24
AUTH = {"Authorization": f"Bearer {SECRET}"}
_ids = itertools.count(1)


def update(text, *, from_id=555, chat_id=None, message_id=None, extra=None):
    message = {
        "message_id": message_id if message_id is not None else next(_ids),
        "date": int(time.time()),
        "chat": {"id": chat_id if chat_id is not None else from_id, "type": "private", "first_name": "Aziz"},
        "from": {"id": from_id, "is_bot": False, "first_name": "Aziz"},
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if extra:
        message.update(extra)
    return {"update_id": next(_ids), "message": message}


class Relay:
    def __init__(self, harness):
        self.h = harness
        self.app = build_relay_app(harness.dp, harness.bot, SECRET)
        self.client = TestClient(TestServer(self.app))

    async def __aenter__(self):
        await self.client.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()

    async def post(self, payload, headers=AUTH):
        response = await self.client.post("/relay", data=json.dumps(payload), headers=headers)
        tasks = set(self.app[RELAY_TASKS])
        if tasks:
            await asyncio.wait(tasks)
        return response


@pytest.fixture
def relay_harness(make_harness):
    def factory(**overrides):
        return make_harness(AGENT_MODE="relay", RELAY_SECRET=SECRET, **overrides)

    return factory


# ------------------------------------------------------------------ endpoint


async def test_requires_secret(relay_harness):
    h = relay_harness()
    async with Relay(h) as relay:
        assert (await relay.post(update("Salom"), headers={})).status == 401
        assert (await relay.post(update("Salom"), headers={"Authorization": "Bearer wrong"})).status == 401
    assert h.generator.calls == [] and h.session.requests == []


async def test_rejects_bad_body_and_has_health(relay_harness):
    h = relay_harness()
    async with Relay(h) as relay:
        response = await relay.client.post("/relay", data="not json", headers=AUTH)
        assert response.status == 400
        assert (await relay.post({"hello": "world"})).status == 400
        assert (await relay.client.get("/health")).status == 200


# ------------------------------------------------------------- direct chats


async def test_preview_mode_sends_nothing(relay_harness):
    h = relay_harness()
    async with Relay(h) as relay:
        assert (await relay.post(update("Salom, somsa bormi?"))).status == 200
    assert h.outcome == Outcome.PREVIEWED
    assert h.session.requests == []
    assert h.previews[-1].chat_id == 555 and h.previews[-1].connection_id == "direct"


async def test_auto_send_replies_in_bot_chat(relay_harness):
    h = relay_harness(AUTO_SEND="true")
    h.generator.queue("Assalomu alaykum! Qanday yordam bera olaman?")
    async with Relay(h) as relay:
        await relay.post(update("Salom"))
    assert h.outcome == Outcome.SENT
    assert h.session.of(GetBusinessConnection) == []
    sent = h.sent()[0]
    assert (sent.chat_id, sent.business_connection_id) == (555, None)
    assert sent.text == "Assalomu alaykum! Qanday yordam bera olaman?"
    history = h.assistant.history.get("direct", 555)
    assert [item["role"] for item in history] == ["other", "owner"]


async def test_cards_in_bot_chat(relay_harness):
    h = relay_harness(AUTO_SEND="true")
    item = {
        "type": "food", "name": "Somsa", "price": 5000, "old_price": None, "discount_percent": 0,
        "installment": None, "image": "https://img.test/somsa.jpg",
        "link": "https://t.me/vdl_market_app_bot?startapp=f_1", "seller": {"name": "Fajr"},
    }
    h.use_search(lambda request: httpx.Response(200, json={"items": [item] * 5}))
    h.generator.queue(json.dumps({"reply": "Mana 👇", "search": {"q": "somsa", "type": "food"}}))
    async with Relay(h) as relay:
        await relay.post(update("Somsa bormi?"))
    photos = h.photos()
    assert len(photos) == 3
    assert all(p.chat_id == 555 and p.business_connection_id is None for p in photos)
    assert photos[0].reply_markup.inline_keyboard[0][0].text == "🍔 Ochish"


async def test_duplicates_and_history_per_chat(relay_harness):
    h = relay_harness()
    async with Relay(h) as relay:
        payload = update("Salom", message_id=42)
        await relay.post(payload)
        await relay.post(payload)
        await relay.post(update("Boshqa mijoz", from_id=777))
    assert list(h.assistant.outcomes) == [Outcome.PREVIEWED, Outcome.DUPLICATE, Outcome.PREVIEWED]
    assert "Salom" not in h.generator.calls[1][1]


async def test_commands_in_relay_mode(relay_harness):
    h = relay_harness()
    async with Relay(h) as relay:
        await relay.post(update("/start"))  # customer: /start belongs to the mini app backend
        await relay.post(update("/pause"))  # customer cannot pause
        assert h.sent() == [] and h.generator.calls == []

        await relay.post(update("/start", from_id=OWNER_ID))
        assert "relay" in h.sent()[-1].text
        await relay.post(update("/pause", from_id=OWNER_ID))
        assert h.assistant.state.paused is True
        await relay.post(update("Salom"))
        assert h.outcome == Outcome.PAUSED


async def test_business_mode_does_not_answer_direct_messages(make_harness):
    h = make_harness()
    await h.direct_message("Salom, somsa bormi?", from_id=555)
    assert h.generator.calls == [] and h.sent() == []


# ------------------------------------------------------------------- config


def test_relay_settings(tmp_path):
    env = tmp_path / ".env"
    base = "TELEGRAM_BOT_TOKEN=1:a\nOWNER_ID=1\n"
    env.write_text(base, encoding="utf-8")
    s = load_settings(env)
    assert s.agent_mode == "business" and not s.relay_mode and s.port == 8080

    env.write_text(base + "AGENT_MODE=relay\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="RELAY_SECRET is required"):
        load_settings(env)
    assert load_settings(env, require_telegram=False).relay_mode

    env.write_text(base + "AGENT_MODE=relay\nRELAY_SECRET=short\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="at least 24") as info:
        load_settings(env)
    assert "short" not in str(info.value).replace("at least", "")

    env.write_text(base + f"AGENT_MODE=relay\nRELAY_SECRET={SECRET}\nPORT=3000\n", encoding="utf-8")
    s = load_settings(env)
    assert s.relay_mode and s.port == 3000 and SECRET not in repr(s)

    env.write_text(base + "AGENT_MODE=webhook\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="AGENT_MODE"):
        load_settings(env)
