"""
Discord adapter.

Behaviour:
  - In a server channel: the bot replies only when it is @mentioned (so it can
    sit in a group without responding to everything).
  - In a DM: it replies to every message (1:1, like WhatsApp).

It reuses core.handle_message() — all logic (history, routing, OpenCode) is shared
with the WhatsApp adapter. The blocking work is run in a thread so it never stalls
the Discord event loop.

Requires:
  - DISCORD_BOT_TOKEN in the environment.
  - The "Message Content Intent" enabled in the Discord Developer Portal
    (Bot settings) — without it, message.content is empty.
"""

import asyncio
import re

import discord

import core

# Strips Discord mention tokens like "<@123>" / "<@!123>" from the message text.
_MENTION_RE = re.compile(r"<@!?\d+>")

# Discord hard limit on a single message.
_DISCORD_LIMIT = 2000

intents = discord.Intents.default()
intents.message_content = True  # privileged: must also be enabled in the portal

client = discord.Client(intents=intents)


def _chunks(text: str, size: int = _DISCORD_LIMIT):
    """Yield `text` split into <=size pieces (Discord rejects longer messages)."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


@client.event
async def on_ready():
    print(f"Discord: logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    # Never respond to ourselves or other bots (avoids loops).
    if message.author == client.user or message.author.bot:
        return

    is_dm = message.guild is None
    mentioned = client.user in message.mentions

    # In a server, only act when tagged. In a DM, always act.
    if not is_dm and not mentioned:
        return

    text = _MENTION_RE.sub("", message.content).strip()
    if not text:
        return

    user_key = f"discord:{message.author.id}"

    async with message.channel.typing():
        try:
            # core.handle_message is blocking -> run off the event loop.
            reply = await asyncio.to_thread(core.handle_message, user_key, text)
        except asyncio.TimeoutError:
            reply = "⏳ That took too long and timed out. Try a smaller request."
        except Exception as exc:  # noqa: BLE001 - report any failure to the user
            reply = f"⚠️ Something went wrong:\n{exc}"

    # Reply (threaded) for the first chunk, then follow-ups in the channel.
    chunks = list(_chunks(reply))
    if chunks:
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


async def start(token: str) -> None:
    """Start the bot inside an already-running asyncio loop (FastAPI lifespan)."""
    await client.start(token)


async def close() -> None:
    """Gracefully disconnect the bot on shutdown."""
    if not client.is_closed():
        await client.close()
