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
import subprocess
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient

# ---------------------------------------------------------------------------
# Configuration (env-driven; nothing sensitive is hardcoded)
# ---------------------------------------------------------------------------
MONGO_URL = os.environ["MONGO_URL"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace")
DOWNLOADS_DIR = os.path.join(WORKSPACE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Conversation context older than this is wiped and started fresh.
CONTEXT_TTL = timedelta(hours=24)

# How long a single OpenCode run may take before we give up (seconds).
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "300"))

# Multi-model per message via a primary 'orchestrator' agent (off by default).
USE_ORCHESTRATOR = os.environ.get("USE_ORCHESTRATOR", "false").lower() == "true"

# OpenRouter free-tier model IDs. OpenCode addresses them as `openrouter/<id>`.
MODEL_STRUCTURAL = "google/gemini-2.5-flash:free"           # parsing / structured work
MODEL_GENERAL = "meta-llama/llama-3.3-70b-instruct:free"    # general conversation

STRUCTURAL_KEYWORDS = ("xml", "json", "crm", "api", "parse", "document")
XML_AGENT_KEYWORDS = ("xml", "parse")
CRM_AGENT_KEYWORDS = ("crm", "sync", "api", "post")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
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

    Use exactly one of `agent` (uses that agent's own model) or `model_id`
    (raw model via `-m`); `-m` would override an agent's model otherwise.
    Auth uses OPENROUTER_API_KEY in the env. `--dangerously-skip-permissions`
    stops the headless run from hanging on permission prompts.
    """
    prompt = history_and_prompt
    if file_path:
        prompt += f"\n\nA document is available at: {file_path}\nRead, parse, and handle it."

    child_env = {**os.environ, "OPENROUTER_API_KEY": OPENROUTER_API_KEY}

    cmd = ["opencode", "run", prompt]
    if agent:
        cmd += ["--agent", agent]
    else:
        cmd += ["-m", f"openrouter/{model_id or MODEL_GENERAL}"]
    cmd += ["--dangerously-skip-permissions"]

    result = subprocess.run(
        cmd,
        cwd=WORKSPACE_DIR,           # so OpenCode picks up /workspace/AGENTS.md and agents
        env=child_env,
        capture_output=True,
        text=True,
        timeout=OPENCODE_TIMEOUT,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "OpenCode exited non-zero")

    return result.stdout.strip() or "(OpenCode produced no output.)"

# TODO: Consider adding local LLM model for basic conversations, to speed up simple chats and save OpenRouter calls for heavier lifting.
def dispatch(full_prompt: str, routing_text: str, file_path: str | None = None,
             default_agent: str | None = None) -> str:
    """Decide how a message is run (orchestrator vs keyword routing) and run it."""
    if USE_ORCHESTRATOR:
        return run_opencode(full_prompt, agent="orchestrator", file_path=file_path)

    agent = route_agent_automatically(routing_text) or default_agent
    if agent:
        return run_opencode(full_prompt, agent=agent, file_path=file_path)
    return run_opencode(full_prompt, model_id=route_model_automatically(routing_text),
                        file_path=file_path)


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
