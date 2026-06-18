"""
WhatsApp -> OpenCode -> OpenRouter automation backend (PoC).

Flow:
    Designer messages the WhatsApp number
      -> Meta Cloud API webhook -> pywa handler (this file)
      -> we look up / update the user's rolling conversation in MongoDB
      -> we run OpenCode headlessly (`opencode run`) against an OpenRouter model
      -> stdout is sent back to the user over WhatsApp.

Everything secret comes from environment variables (Railway service variables):
    WA_PHONE_ID, WA_TOKEN, WA_APP_ID, WA_APP_SECRET, WA_VERIFY_TOKEN, WA_CALLBACK_URL
    MONGO_URL
    OPENROUTER_API_KEY

This is a Proof of Concept: it favours clarity and defensive error handling over
throughput. The optimisation path (avoiding OpenCode cold boots) is `opencode serve`
plus `opencode run --attach`, noted inline below.
"""

import os
import subprocess
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI
from pymongo import MongoClient
from pywa import WhatsApp, filters, types

# ---------------------------------------------------------------------------
# Configuration (env-driven; nothing sensitive is hardcoded)
# ---------------------------------------------------------------------------
MONGO_URL = os.environ["MONGO_URL"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Where OpenCode runs and where inbound documents are isolated.
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace")
DOWNLOADS_DIR = os.path.join(WORKSPACE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Conversation context older than this is wiped and started fresh.
CONTEXT_TTL = timedelta(hours=24)

# How long a single OpenCode run may take before we give up (seconds).
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "300"))

# Multi-model-per-message: when true, every message goes to a single primary
# "orchestrator" agent that auto-delegates to subagents (each on its own model).
# Off by default — it makes extra model calls, which matters on free-tier limits.
# Flip to "true" once you are on paid OpenRouter and want true multi-model replies.
USE_ORCHESTRATOR = os.environ.get("USE_ORCHESTRATOR", "false").lower() == "true"

# OpenRouter free-tier model IDs. OpenCode addresses them as `openrouter/<id>`.
# The `:free` suffix is how OpenRouter exposes the no-cost variants.
MODEL_STRUCTURAL = "google/gemini-2.5-flash:free"           # parsing / structured work
MODEL_GENERAL = "meta-llama/llama-3.3-70b-instruct:free"    # general conversation

# Keywords that imply structured / document / integration work (model routing).
STRUCTURAL_KEYWORDS = ("xml", "json", "crm", "api", "parse", "document")

# Keywords -> specialised OpenCode agent. Each agent declares its OWN model in
# .opencode/agent/<name>.md, which is the single place to tune models when you
# move to paid OpenRouter. When no agent matches, we fall back to model routing.
XML_AGENT_KEYWORDS = ("xml", "parse")
CRM_AGENT_KEYWORDS = ("crm", "sync", "api", "post")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
mongo = MongoClient(MONGO_URL)
# Use the db named in MONGO_URL; fall back to a default if the URL omits one.
db = mongo.get_default_database(default="whatsapp_opencode")
sessions = db["user_sessions"]

# ---------------------------------------------------------------------------
# WhatsApp client mounted on FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="WhatsApp x OpenCode PoC")

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
    """Simple health check so Railway (and you) can confirm the service is up."""
    return {"status": "ok", "service": "whatsapp-opencode-poc"}


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------
def route_model_automatically(user_prompt: str) -> str:
    """Pick an OpenRouter model based on what the user is asking for.

    Structural / integration requests go to Gemini Flash (strong at structured
    output); everything else goes to the larger general-chat Llama model.
    Returns a bare OpenRouter model id (without the `openrouter/` provider prefix).
    """
    lowered = user_prompt.lower()
    if any(keyword in lowered for keyword in STRUCTURAL_KEYWORDS):
        return MODEL_STRUCTURAL
    return MODEL_GENERAL


def route_agent_automatically(user_prompt: str) -> str | None:
    """Pick a specialised OpenCode agent for the request, or None for general chat.

    Returns the agent name (matching a file in .opencode/agent/). When an agent is
    chosen, OpenCode uses the model declared in that agent's markdown — so model
    selection for specialised work lives in one place per agent, ready for paid
    OpenRouter. None means 'no specialist' -> the caller falls back to model routing.
    """
    lowered = user_prompt.lower()
    if any(keyword in lowered for keyword in XML_AGENT_KEYWORDS):
        return "xml-parser"
    if any(keyword in lowered for keyword in CRM_AGENT_KEYWORDS):
        return "crm-sync-mock"
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def load_history(user_id: str) -> str:
    """Return the user's cumulative chat history, resetting it if stale (>24h)."""
    doc = sessions.find_one({"user_id": user_id})
    if not doc:
        return ""

    last_updated = doc.get("last_updated")
    # Normalise to aware UTC for comparison.
    if last_updated and last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    if last_updated and (datetime.now(timezone.utc) - last_updated) > CONTEXT_TTL:
        # Stale: wipe the context window for a clean slate.
        sessions.update_one(
            {"user_id": user_id},
            {"$set": {"chat_history": "", "last_updated": datetime.now(timezone.utc)}},
        )
        return ""

    return doc.get("chat_history", "")


def append_history(user_id: str, user_text: str, agent_text: str) -> str:
    """Append this turn to the user's history and persist it. Returns new history."""
    doc = sessions.find_one({"user_id": user_id})
    prior = doc.get("chat_history", "") if doc else ""
    updated = f"{prior}\nUser: {user_text}\nAssistant: {agent_text}".strip()
    sessions.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "chat_history": updated,
                "last_updated": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return updated


# ---------------------------------------------------------------------------
# OpenCode execution wrapper
# ---------------------------------------------------------------------------
def run_opencode(
    history_and_prompt: str,
    model_id: str | None = None,
    agent: str | None = None,
    file_path: str | None = None,
) -> str:
    """Run OpenCode headlessly and return its stdout.

    Args:
        history_and_prompt: full text fed to OpenCode (rolling history + new turn).
        model_id: bare OpenRouter model id (we prefix `openrouter/`). Used only
            when `agent` is None — for general chat with no specialist.
        agent: name of a .opencode/agent/<name> agent. When set, OpenCode uses
            THAT agent (and the model declared in its markdown); we do not pass
            `-m`, because the `-m` flag would override the agent's own model.
        file_path: optional local document to make OpenCode aware of.

    Notes:
        * `opencode run` is the non-interactive command (NOT `--inline`).
        * `--agent <name>` selects a configured agent; `-m openrouter/<model>`
          selects a raw model. We use exactly one of them per call.
        * Auth uses `OPENROUTER_API_KEY` in the env (there is no `OPENROUTER_MODEL`).
        * `--dangerously-skip-permissions` prevents the headless run from hanging
          waiting for interactive permission approval.
    """
    prompt = history_and_prompt
    if file_path:
        prompt += f"\n\nA document is available at: {file_path}\nRead, parse, and handle it."

    # OpenRouter API key is read by OpenCode from the environment.
    child_env = {**os.environ, "OPENROUTER_API_KEY": OPENROUTER_API_KEY}

    cmd = ["opencode", "run", prompt]
    if agent:
        # Agent owns its model (declared in its markdown frontmatter).
        cmd += ["--agent", agent]
    else:
        cmd += ["-m", f"openrouter/{model_id or MODEL_GENERAL}"]
    cmd += ["--dangerously-skip-permissions"]

    result = subprocess.run(
        cmd,
        cwd=WORKSPACE_DIR,           # so OpenCode picks up /workspace/AGENTS.md
        env=child_env,
        capture_output=True,
        text=True,
        timeout=OPENCODE_TIMEOUT,
    )

    if result.returncode != 0:
        # Surface stderr to the caller's try/except for a WhatsApp-friendly error.
        raise RuntimeError(result.stderr.strip() or "OpenCode exited non-zero")

    output = result.stdout.strip()
    return output or "(OpenCode produced no output.)"


def dispatch(full_prompt: str, routing_text: str, file_path: str | None = None,
             default_agent: str | None = None) -> str:
    """Single place that decides how a message is run, for both text and documents.

    - USE_ORCHESTRATOR on  → one primary 'orchestrator' agent auto-delegates to
      subagents (multi-model per message). WhatsApp-first: users can't pick an
      agent, so the orchestrator decides for them.
    - USE_ORCHESTRATOR off → keyword-route to a specialist agent, else a `default_agent`
      if given (documents), else fall back to plain model routing.
    """
    if USE_ORCHESTRATOR:
        return run_opencode(full_prompt, agent="orchestrator", file_path=file_path)

    agent = route_agent_automatically(routing_text) or default_agent
    if agent:
        return run_opencode(full_prompt, agent=agent, file_path=file_path)
    return run_opencode(full_prompt, model_id=route_model_automatically(routing_text),
                        file_path=file_path)


# ---------------------------------------------------------------------------
# WhatsApp handlers
# ---------------------------------------------------------------------------
@wa.on_message(filters.text)
def handle_text(client: WhatsApp, msg: types.Message) -> None:
    """Handle inbound text: route a model, run OpenCode, reply with the result."""
    try:
        msg.mark_as_read()
        user_id = msg.from_user.wa_id
        user_text = msg.text

        history = load_history(user_id)
        full_prompt = f"{history}\nUser: {user_text}".strip()

        reply = dispatch(full_prompt, routing_text=user_text)
        append_history(user_id, user_text, reply)

        # WhatsApp text messages cap at 4096 chars; trim defensively.
        msg.reply(reply[:4096])
    except subprocess.TimeoutExpired:
        msg.reply("⏳ That took too long and timed out. Try a smaller request.")
    except Exception as exc:  # noqa: BLE001 - PoC: report any failure to the user
        msg.reply(f"⚠️ Something went wrong:\n{exc}")


@wa.on_message(filters.document)
def handle_document(client: WhatsApp, msg: types.Message) -> None:
    """Handle inbound documents: download to the isolated dir, then hand to OpenCode."""
    try:
        msg.mark_as_read()
        user_id = msg.from_user.wa_id

        # Download the binary into the isolated downloads directory.
        filename = msg.document.filename or f"{msg.document.id}"
        local_path = os.path.join(DOWNLOADS_DIR, filename)
        msg.document.download(path=local_path)

        caption = msg.caption or "Process this document."
        history = load_history(user_id)
        full_prompt = f"{history}\nUser: {caption}".strip()

        # Documents default to xml-parser unless the caption asks for CRM work.
        reply = dispatch(full_prompt, routing_text=caption,
                         file_path=local_path, default_agent="xml-parser")
        append_history(user_id, f"[document: {filename}] {caption}", reply)

        msg.reply(reply[:4096])
    except subprocess.TimeoutExpired:
        msg.reply("⏳ Processing that document timed out.")
    except Exception as exc:  # noqa: BLE001 - PoC: report any failure to the user
        msg.reply(f"⚠️ Couldn't process the document:\n{exc}")
