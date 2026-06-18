"""
WhatsApp adapter + process entry point.

  - WhatsApp (pywa, 1:1) receives messages over a Meta Cloud API webhook mounted
    on this FastAPI app, and replies via core.handle_message().
  - On startup, if DISCORD_BOT_TOKEN is set, the Discord bot (discord_bot.py) is
    launched as a background task in the same process, so one Railway service
    serves both channels.

All shared logic lives in core.py. This file is just the WhatsApp I/O + wiring.

Env vars: WA_PHONE_ID, WA_TOKEN, WA_APP_ID, WA_APP_SECRET, WA_VERIFY_TOKEN,
WA_CALLBACK_URL (+ MONGO_URL, OPENROUTER_API_KEY used by core; DISCORD_BOT_TOKEN optional).
"""

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pywa import WhatsApp, filters, types

import core
import discord_bot

# Discord 1:1 + group lives in the same process. WhatsApp text cap is 4096.
_WHATSAPP_LIMIT = 4096


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Discord bot alongside the web server, stop it on shutdown."""
    discord_task = None
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if token:
        discord_task = asyncio.create_task(discord_bot.start(token))
    try:
        yield
    finally:
        if discord_task:
            await discord_bot.close()
            discord_task.cancel()


app = FastAPI(title="WhatsApp x Discord x OpenCode PoC", lifespan=lifespan)

wa = WhatsApp(
    phone_id=os.environ["WA_PHONE_ID"],
    token=os.environ["WA_TOKEN"],
    server=app,                                  # pywa registers its webhook on our FastAPI app
    webhook_endpoint="/whatsapp",                # keep distinct from our own "/" routes
    verify_token=os.environ["WA_VERIFY_TOKEN"],
    app_id=os.environ.get("WA_APP_ID"),
    app_secret=os.environ.get("WA_APP_SECRET"),
    callback_url=os.environ.get("WA_CALLBACK_URL"),  # enables auto webhook registration
    validate_updates=True,                       # verify Meta signatures using app_secret
)


@app.get("/")
def health() -> dict:
    """Health check so Railway (and you) can confirm the service is up."""
    return {"status": "ok", "service": "whatsapp-discord-opencode-poc"}


@wa.on_message(filters.text)
def handle_text(_: WhatsApp, msg: types.Message) -> None:
    """Inbound WhatsApp text -> shared brain -> reply."""
    try:
        msg.mark_as_read()
        reply = core.handle_message(f"whatsapp:{msg.from_user.wa_id}", msg.text)
        msg.reply(reply[:_WHATSAPP_LIMIT])
    except subprocess.TimeoutExpired:
        msg.reply("⏳ That took too long and timed out. Try a smaller request.")
    except Exception as exc:  # noqa: BLE001 - PoC: report any failure to the user
        msg.reply(f"⚠️ Something went wrong:\n{exc}")


@wa.on_message(filters.document)
def handle_document(_: WhatsApp, msg: types.Message) -> None:
    """Inbound WhatsApp document -> download to isolated dir -> shared brain -> reply."""
    try:
        msg.mark_as_read()
        filename = msg.document.filename or f"{msg.document.id}"
        local_path = os.path.join(core.DOWNLOADS_DIR, filename)
        msg.document.download(path=local_path)

        caption = msg.caption or "Process this document."
        reply = core.handle_message(
            f"whatsapp:{msg.from_user.wa_id}",
            caption,
            file_path=local_path,
            default_agent="xml-parser",  # documents default to XML parsing
        )
        msg.reply(reply[:_WHATSAPP_LIMIT])
    except subprocess.TimeoutExpired:
        msg.reply("⏳ Processing that document timed out.")
    except Exception as exc:  # noqa: BLE001 - PoC: report any failure to the user
        msg.reply(f"⚠️ Couldn't process the document:\n{exc}")
