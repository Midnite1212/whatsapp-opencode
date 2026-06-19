"""
Platform-agnostic core: state, routing, OpenCode execution.

Both adapters (WhatsApp in server.py, Discord in discord_bot.py) call
`handle_message()` — they only differ in how they receive a message and send a
reply. All the actual logic (MongoDB history, model/agent routing, OpenCode)
lives here so there is one brain, not two.

Secrets come from environment variables:
    MONGO_URL, OPENROUTER_API_KEY
"""

import os
import re
import subprocess
from datetime import datetime, timezone, timedelta

# Strips ANSI escape sequences (colour codes) from OpenCode's terminal output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _clean(text) -> str:
    # On timeout, subprocess returns bytes even with text=True — decode defensively.
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text or "").strip()

from pymongo import MongoClient

# ---------------------------------------------------------------------------
# Configuration (env-driven; nothing sensitive is hardcoded)
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Conversation memory is OFF by default. Most "fix this" requests are self-contained,
# and replaying a growing transcript pollutes the context window, costs tokens, and
# (on Railway's dynamic IPs) makes MongoDB a flaky hard dependency. Turn it on only
# if you actually need multi-turn continuity: HISTORY_ENABLED=true (+ MONGO_URL).
HISTORY_ENABLED = os.environ.get("HISTORY_ENABLED", "false").lower() == "true"
MONGO_URL = os.environ.get("MONGO_URL")  # only required when HISTORY_ENABLED

WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace")
DOWNLOADS_DIR = os.path.join(WORKSPACE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Conversation context older than this is wiped and started fresh.
CONTEXT_TTL = timedelta(hours=24)

# How long a single OpenCode run may take before we give up (seconds).
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "300"))

# Multi-model per message via a primary 'orchestrator' agent (off by default).
USE_ORCHESTRATOR = os.environ.get("USE_ORCHESTRATOR", "false").lower() == "true"

# Model IDs are OpenRouter slugs (without the `openrouter/` provider prefix that
# run_opencode adds). Default is `openrouter/free` — OpenRouter's "Free Models
# Router", which picks an available free model at random and filters out ones
# that are down/rate-limited. That solves the free-model churn without env vars.
# (So OpenCode receives `-m openrouter/openrouter/free`: provider=openrouter,
#  model=openrouter/free — the double prefix is correct, not a typo.)
# Still env-overridable if you ever want to pin a specific model.
MODEL_GENERAL = os.environ.get("MODEL_GENERAL", "openrouter/free")
MODEL_STRUCTURAL = os.environ.get("MODEL_STRUCTURAL", MODEL_GENERAL)

STRUCTURAL_KEYWORDS = ("xml", "json", "crm", "api", "parse", "document")
XML_AGENT_KEYWORDS = ("xml", "parse")
CRM_AGENT_KEYWORDS = ("crm", "sync", "api", "post")

# ---------------------------------------------------------------------------
# Database — only connected when history is enabled; otherwise the bot is fully
# stateless and has NO MongoDB dependency (nothing to fail on Railway).
# ---------------------------------------------------------------------------
sessions = None
if HISTORY_ENABLED:
    if not MONGO_URL:
        raise RuntimeError("HISTORY_ENABLED=true but MONGO_URL is not set")
    mongo = MongoClient(MONGO_URL)
    db = mongo.get_default_database(default="whatsapp_opencode")
    sessions = db["user_sessions"]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def route_model_automatically(user_prompt: str) -> str:
    """Pick an OpenRouter model: Gemini for structured work, Llama otherwise."""
    lowered = user_prompt.lower()
    if any(keyword in lowered for keyword in STRUCTURAL_KEYWORDS):
        return MODEL_STRUCTURAL
    return MODEL_GENERAL


def route_agent_automatically(user_prompt: str) -> str | None:
    """Pick a specialised OpenCode agent, or None for general chat.

    When an agent is chosen, OpenCode uses the model declared in that agent's
    markdown — so model selection for specialised work lives in one place.
    """
    lowered = user_prompt.lower()
    if any(keyword in lowered for keyword in XML_AGENT_KEYWORDS):
        return "xml-parser"
    if any(keyword in lowered for keyword in CRM_AGENT_KEYWORDS):
        return "crm-sync-mock"
    return None


# ---------------------------------------------------------------------------
# Session state. `user_key` is namespaced per platform, e.g. "whatsapp:4915..."
# or "discord:12345", so the two channels never collide in one collection.
# ---------------------------------------------------------------------------
def load_history(user_key: str) -> str:
    """Return the user's cumulative chat history, resetting it if stale (>24h)."""
    if sessions is None:           # history disabled -> stateless
        return ""
    doc = sessions.find_one({"user_id": user_key})
    if not doc:
        return ""

    last_updated = doc.get("last_updated")
    if last_updated and last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    if last_updated and (datetime.now(timezone.utc) - last_updated) > CONTEXT_TTL:
        sessions.update_one(
            {"user_id": user_key},
            {"$set": {"chat_history": "", "last_updated": datetime.now(timezone.utc)}},
        )
        return ""

    return doc.get("chat_history", "")


def append_history(user_key: str, user_text: str, agent_text: str) -> None:
    """Append this turn to the user's history and persist it."""
    if sessions is None:           # history disabled -> nothing to persist
        return
    doc = sessions.find_one({"user_id": user_key})
    prior = doc.get("chat_history", "") if doc else ""
    updated = f"{prior}\nUser: {user_text}\nAssistant: {agent_text}".strip()
    sessions.update_one(
        {"user_id": user_key},
        {
            "$set": {
                "user_id": user_key,
                "chat_history": updated,
                "last_updated": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# OpenCode execution
# ---------------------------------------------------------------------------
def run_opencode(
    history_and_prompt: str,
    model_id: str | None = None,
    agent: str | None = None,
    file_path: str | None = None,
) -> str:
    """Run OpenCode headlessly (`opencode run`) and return its stdout.

    `model_id` (env-driven, `MODEL_GENERAL` default) is always pinned via
    `-m openrouter/<model>` so OpenCode can't fall back to a non-free default.
    `agent` optionally layers behaviour/tools on top. Auth uses OPENROUTER_API_KEY.
    `--dangerously-skip-permissions` stops headless hangs on permission prompts.
    """
    prompt = history_and_prompt
    if file_path:
        prompt += f"\n\nA document is available at: {file_path}\nRead, parse, and handle it."

    # NO_COLOR keeps OpenCode from wrapping output in ANSI escape codes.
    child_env = {**os.environ, "OPENROUTER_API_KEY": OPENROUTER_API_KEY, "NO_COLOR": "1"}

    # Always pin the model via -m so OpenCode can't wander onto a non-free
    # default; --agent (optional) only layers behaviour/tools on top.
    cmd = ["opencode", "run", prompt, "-m", f"openrouter/{model_id or MODEL_GENERAL}"]
    if agent:
        cmd += ["--agent", agent]
    cmd += ["--dangerously-skip-permissions"]

    try:
        result = subprocess.run(
            cmd,
            cwd=WORKSPACE_DIR,           # so OpenCode picks up /workspace/AGENTS.md, agents, opencode.json
            env=child_env,
            stdin=subprocess.DEVNULL,    # never block waiting for interactive input (TTY-less)
            capture_output=True,
            text=True,
            timeout=OPENCODE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # Surface whatever OpenCode printed before we killed it — turns a blind
        # 300s hang into an actionable error (e.g. an auth/network message).
        partial = _clean(exc.stderr or exc.stdout or "")
        tail = partial[-600:] if partial else "(no output captured before timeout)"
        raise RuntimeError(
            f"OpenCode timed out after {OPENCODE_TIMEOUT}s. Last output:\n{tail}"
        ) from exc

    if result.returncode != 0:
        # Include BOTH streams — OpenCode often writes the real error to stdout
        # while stderr only carries the run header.
        detail = _clean(f"{result.stdout}\n{result.stderr}") or "OpenCode exited non-zero"
        raise RuntimeError(detail[-800:])

    return _clean(result.stdout) or "(OpenCode produced no output.)"

# TODO: Consider adding local LLM model for basic conversations, to speed up simple chats and save OpenRouter calls for heavier lifting.
def dispatch(full_prompt: str, routing_text: str, file_path: str | None = None,
             default_agent: str | None = None) -> str:
    """Decide how a message is run and run it. One env-driven model is always
    pinned; routing only picks an optional specialist agent on top."""
    model_id = route_model_automatically(routing_text)
    if USE_ORCHESTRATOR:
        return run_opencode(full_prompt, model_id=model_id, agent="orchestrator",
                            file_path=file_path)

    agent = route_agent_automatically(routing_text) or default_agent
    return run_opencode(full_prompt, model_id=model_id, agent=agent, file_path=file_path)


# ---------------------------------------------------------------------------
# The one entry point both adapters call
# ---------------------------------------------------------------------------
def handle_message(user_key: str, text: str, file_path: str | None = None,
                   default_agent: str | None = None) -> str:
    """Full turn: load history -> dispatch to OpenCode -> persist -> return reply.

    This is BLOCKING (subprocess + pymongo). Async callers (Discord) must run it
    in a thread, e.g. `await asyncio.to_thread(core.handle_message, ...)`.
    Returns the untrimmed reply; each adapter trims to its own platform limit.
    """
    history = load_history(user_key)
    full_prompt = f"{history}\nUser: {text}".strip()
    reply = dispatch(full_prompt, routing_text=text, file_path=file_path,
                     default_agent=default_agent)
    append_history(user_key, text, reply)
    return reply
