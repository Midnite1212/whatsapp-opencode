"""
Platform-agnostic core: state, routing, OpenCode execution.

Both adapters (WhatsApp in server.py, Discord in discord_bot.py) call
`handle_message()` — they only differ in how they receive a message and send a
reply. All the actual logic (MongoDB history, model/agent routing, OpenCode)
lives here so there is one brain, not two.

Secrets come from environment variables:
    MONGO_URL, OPENROUTER_API_KEY
"""

import json
import os
import re
import subprocess
import time
import urllib.request
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
AGENT_DIR = os.path.join(WORKSPACE_DIR, ".opencode", "agent")
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
# Structural/agent work needs TOOL USE (e.g. crm-sync-mock runs bash). openrouter/free
# randomly lands on models with no tool support (image models, etc.) -> "No endpoints
# found that support tool use". So default this to a free, tool-capable coder model.
# Other free tool-capable options if congested: openai/gpt-oss-120b:free,
# qwen/qwen3-next-80b-a3b-instruct:free, meta-llama/llama-3.3-70b-instruct:free.
MODEL_STRUCTURAL = os.environ.get("MODEL_STRUCTURAL", "openrouter/free")

STRUCTURAL_KEYWORDS = ("xml", "json", "crm", "api", "parse", "document", "sermon", "tiptap")
# Sermon docs -> the bash-capable sermon-notes agent (it unzips the .docx to read which
# runs are highlighted). Checked BEFORE xml-parser, since "parse this sermon" also matches
# the XML keywords but should use the sermon skill, not the generic XML parser.
SERMON_KEYWORDS = ("sermon", "tiptap")
XML_AGENT_KEYWORDS = ("xml", "parse")
# Real authenticated org-API calls (OAuth client-credentials) -> org-api agent.
# (crm-sync-mock.md is kept for offline mock testing; invoke it explicitly if needed.)
ORG_API_KEYWORDS = ("crm", "sync", "api", "post", "submit", "upload", "send to")

# Where a converted sermon-notes JSON is POSTed (relative to ORG_API_BASE_URL).
ORG_SERMON_CREATE_PATH = os.environ.get("ORG_SERMON_CREATE_PATH", "/api/sermon-notes-parent/create")

# --- Local LLM (self-hosted on your own PC) ---------------------------------
# When LOCAL_LLM_URL is set AND reachable, use your local model for everything;
# otherwise fall back to OpenRouter automatically. Because the bot runs on Railway
# (cloud), LOCAL_LLM_URL must be a PUBLIC url to your machine — a tunnel like
# Cloudflare Tunnel / ngrok / Tailscale — not http://localhost. It must point at an
# OpenAI-compatible endpoint ending in /v1 (Ollama: .../v1, LM Studio: .../v1).
# LOCAL_LLM_MODEL must match the model key declared under provider "local" in
# opencode.jsonc.
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL")              # e.g. https://abc.trycloudflare.com/v1
# Model names. Default "auto" -> the bot discovers whatever model your local server
# is serving (from GET /v1/models) and uses it, so you don't define anything: just
# load a model and set LOCAL_LLM_URL. To route different tasks to different local
# models instead, set these explicitly (must match keys served by your endpoint):
#   LOCAL_LLM_MODEL            -> general / chat / parsing
#   LOCAL_LLM_MODEL_STRUCTURAL -> coding / tools  (defaults to LOCAL_LLM_MODEL)
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "auto")
LOCAL_LLM_MODEL_STRUCTURAL = os.environ.get("LOCAL_LLM_MODEL_STRUCTURAL", LOCAL_LLM_MODEL)
LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "local")  # most local servers ignore this
LOCAL_LLM_HEALTH_TTL = int(os.environ.get("LOCAL_LLM_HEALTH_TTL", "30"))  # cache up/down (seconds)

_local_health = {"checked_at": 0.0, "up": False, "model": None}

# Append a small "— via local LLM / OpenRouter (cloud)" tag to each reply so you can
# see which backend answered. Set SHOW_MODEL_SOURCE=false to hide it.
SHOW_MODEL_SOURCE = os.environ.get("SHOW_MODEL_SOURCE", "true").lower() == "true"

# --- GitHub App (bot identity for PRs) ----------------------------------------
# A GitHub App has no static token. We mint a short-lived installation token
# (JWT -> exchange) and inject it as GITHUB_TOKEN for the GitHub MCP, cached ~50 min.
# Set GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, and GITHUB_APP_PRIVATE_KEY (the PEM).
# (Prefer a plain PAT instead? Just set GITHUB_TOKEN directly and leave these unset.)
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
GITHUB_APP_INSTALLATION_ID = os.environ.get("GITHUB_APP_INSTALLATION_ID")
GITHUB_APP_PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY")
_gh_token = {"token": None, "expires_at": 0.0}

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
def _is_structural(text: str) -> bool:
    """Does the message imply structural/coding/tool work (vs general chat)?"""
    lowered = text.lower()
    return any(keyword in lowered for keyword in STRUCTURAL_KEYWORDS)


def route_model_automatically(user_prompt: str) -> str:
    """Pick the OpenRouter model slug: structural model for code/parse, general otherwise."""
    return MODEL_STRUCTURAL if _is_structural(user_prompt) else MODEL_GENERAL


def route_agent_automatically(user_prompt: str) -> str | None:
    """Pick a specialised OpenCode agent, or None for general chat.

    When an agent is chosen, OpenCode uses the model declared in that agent's
    markdown — so model selection for specialised work lives in one place.
    """
    lowered = user_prompt.lower()
    if any(keyword in lowered for keyword in SERMON_KEYWORDS):
        return "sermon-notes"
    if any(keyword in lowered for keyword in XML_AGENT_KEYWORDS):
        return "xml-parser"
    if any(keyword in lowered for keyword in ORG_API_KEYWORDS):
        return "org-api"
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
# Model selection: local LLM first (if reachable), else OpenRouter
# ---------------------------------------------------------------------------
def _local_base_url() -> str:
    """LOCAL_LLM_URL normalized to end with /v1 — OpenAI-compatible servers (Ollama,
    LM Studio, KoboldCpp) expose the API under /v1, and forgetting it is the #1 setup
    mistake (POST hits /chat/completions -> 'unexpected endpoint' -> empty reply)."""
    base = (LOCAL_LLM_URL or "").rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    return base


def local_llm_up() -> bool:
    """Is the self-hosted LLM reachable? Cached for LOCAL_LLM_HEALTH_TTL seconds so
    we don't ping it on every message. Only a 2xx on /v1/models counts as UP — any HTTP
    error (incl. an offline ngrok tunnel's 502/404) or connection error means DOWN, so
    the bot falls back to OpenRouter."""
    if not LOCAL_LLM_URL:
        return False
    now = time.time()
    if now - _local_health["checked_at"] < LOCAL_LLM_HEALTH_TTL:
        return _local_health["up"]
    up = False
    model = None
    try:
        req = urllib.request.Request(
            _local_base_url() + "/models",
            headers={
                "Authorization": f"Bearer {LOCAL_LLM_API_KEY}",
                # ngrok free tier injects an HTML interstitial without this header,
                # which would break the JSON API. Harmless on other tunnels.
                "ngrok-skip-browser-warning": "true",
            },
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            up = True
            try:                          # discover the served model id for "auto"
                data = json.loads(resp.read().decode("utf-8"))
                ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                # Skip embedding models — they can't do chat/agent work. Picks the
                # first remaining model (load just ONE chat model for predictable auto,
                # or set LOCAL_LLM_MODEL explicitly when several are loaded).
                ids = [i for i in ids if "embed" not in i.lower()]
                model = ids[0] if ids else None
            except Exception:
                model = None
    except urllib.error.HTTPError:
        up = False      # e.g. offline ngrok tunnel returns 502/404 -> DOWN -> fall back
    except Exception:
        up = False      # connection refused / DNS / timeout -> down
    _local_health.update(checked_at=now, up=up, model=model)
    return up


def opencode_model_for(structural: bool) -> str:
    """Full OpenCode model string for this task tier. Prefer the local LLM when
    reachable (per-tier local model, or auto-discovered), else fall back to OpenRouter."""
    if local_llm_up():
        name = LOCAL_LLM_MODEL_STRUCTURAL if structural else LOCAL_LLM_MODEL
        if name in (None, "", "auto"):
            # Use whatever the local server is serving; "local-model" is a last resort
            # if discovery failed (server up but /models gave nothing usable).
            name = _local_health.get("model") or "local-model"
        return f"local/{name}"
    return f"openrouter/{MODEL_STRUCTURAL if structural else MODEL_GENERAL}"


def github_installation_token() -> str | None:
    """Mint (and cache ~50 min) a GitHub App installation token for the GitHub MCP.

    Returns None if the App isn't fully configured or minting fails — in which case
    the bot still works for everything except GitHub. Never logs the key or token.
    """
    if not (GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY):
        return None
    now = time.time()
    if _gh_token["token"] and now < _gh_token["expires_at"]:
        return _gh_token["token"]
    try:
        import jwt  # PyJWT; lazy import so the module loads even without it installed
        key = GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")  # tolerate escaped newlines
        assertion = jwt.encode(
            {"iat": int(now) - 60, "exp": int(now) + 540, "iss": GITHUB_APP_ID},
            key, algorithm="RS256",
        )
        req = urllib.request.Request(
            f"https://api.github.com/app/installations/{GITHUB_APP_INSTALLATION_ID}/access_tokens",
            method="POST",
            headers={
                "Authorization": f"Bearer {assertion}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "whatsapp-opencode-bot",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _gh_token.update(token=data["token"], expires_at=now + 3000)  # ~50 min cache
        return _gh_token["token"]
    except Exception:
        return None


def model_source_label() -> str:
    """Human-readable backend in use right now (uses the cached health check)."""
    if local_llm_up():
        served = _local_health.get("model")
        return f"local LLM ({served})" if served else "local LLM"
    return "OpenRouter (cloud)"


# ---------------------------------------------------------------------------
# OpenCode execution
# ---------------------------------------------------------------------------
def run_opencode(
    history_and_prompt: str,
    model: str | None = None,
    agent: str | None = None,
    file_path: str | None = None,
) -> str:
    """Run OpenCode headlessly (`opencode run`) and return its stdout.

    `model` is the FULL OpenCode model string (`provider/model`, e.g.
    `openrouter/openrouter/free` or `local/<model>`) — pinned via `-m` so OpenCode
    can't wander onto a default. `agent` optionally layers behaviour/tools on top.
    `--dangerously-skip-permissions` stops headless hangs on permission prompts.
    """
    prompt = history_and_prompt
    if file_path:
        prompt += f"\n\nA document is available at: {file_path}\nRead, parse, and handle it."

    # Point OpenCode at our config + agents EXPLICITLY so they load regardless of
    # the process's working directory (the bot's cwd isn't guaranteed to be /workspace).
    # OPENCODE_CONFIG_DIR -> dir containing agent/ ; OPENCODE_CONFIG -> the config file.
    # NO_COLOR strips ANSI escape codes from output.
    child_env = {
        **os.environ,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
        # Hand OpenCode the /v1-normalized URL so its provider baseURL is correct
        # even if LOCAL_LLM_URL was set without /v1.
        "LOCAL_LLM_URL": _local_base_url(),
        "LOCAL_LLM_API_KEY": LOCAL_LLM_API_KEY,  # consumed by opencode.jsonc {env:...}
        "NO_COLOR": "1",
        "OPENCODE_CONFIG_DIR": os.path.join(WORKSPACE_DIR, ".opencode"),
        "OPENCODE_CONFIG": os.path.join(WORKSPACE_DIR, "opencode.jsonc"),
    }
    # GitHub App: inject a fresh installation token as GITHUB_TOKEN for the MCP.
    # (No-op if you use a plain PAT — that GITHUB_TOKEN is already in os.environ.)
    gh = github_installation_token()
    if gh:
        child_env["GITHUB_TOKEN"] = gh

    # Pin the model via -m so OpenCode can't wander onto a default; --agent
    # (optional) only layers behaviour/tools on top.
    cmd = ["opencode", "run", prompt, "-m", model or opencode_model_for(MODEL_GENERAL)]
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

def _extract_json_object(text: str) -> str | None:
    """Pull the outermost {...} JSON object out of model output and validate it parses.

    The sermon-notes skill is told to emit JSON only, but a free model may still wrap it
    in prose or a ```json fence. We slice from the first '{' to the last '}' and confirm
    it's valid JSON, so the submit step never POSTs garbage. Returns None if not found.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    blob = text[start:end + 1]
    try:
        json.loads(blob)
    except ValueError:
        return None
    return blob


def _docx_to_outline(path: str) -> str | None:
    """Extract a COMPACT outline from a .docx for the model, instead of letting the agent
    read the raw `word/document.xml`. That XML is ~90% OOXML boilerplate and runs 15-19k
    tokens for a real sermon — on a small-context local model it overflows the window and
    truncation drops the user turn (LM Studio then errors "No user query found in
    messages"). The outline keeps only what conversion needs: one line per paragraph,
    tagged by kind, with `<hl>…</hl>` for yellow highlight and `*…*` for bold.

    Returns None if `path` isn't a readable .docx (caller falls back to reading the file).
    """
    import zipfile
    import xml.etree.ElementTree as ET
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def _val(elem):
        return elem.get(W + "val") if elem is not None else None

    try:
        with zipfile.ZipFile(path) as zf:
            doc_xml = zf.read("word/document.xml").decode("utf-8")
        body = ET.fromstring(doc_xml).find(f"{W}body")
    except (zipfile.BadZipFile, KeyError, OSError, ET.ParseError):
        return None
    if body is None:
        return None

    lines = []
    for para in body.findall(f"{W}p"):
        ppr = para.find(f"{W}pPr")
        style = depth = None
        if ppr is not None:
            style = _val(ppr.find(f"{W}pStyle"))
            numpr = ppr.find(f"{W}numPr")
            if numpr is not None:
                ilvl = _val(numpr.find(f"{W}ilvl"))
                depth = int(ilvl) if (ilvl or "").isdigit() else 0
            olvl = _val(ppr.find(f"{W}outlineLvl"))
            if olvl is not None:
                style = f"outline{olvl}"

        parts, max_sz = [], 0
        for run in para.findall(f"{W}r"):
            rpr = run.find(f"{W}rPr")
            hl = bold = False
            if rpr is not None:
                hi = rpr.find(f"{W}highlight")
                hl = hi is not None and _val(hi) == "yellow"
                bold = rpr.find(f"{W}b") is not None
                sz = _val(rpr.find(f"{W}sz"))
                if (sz or "").isdigit():
                    max_sz = max(max_sz, int(sz))
            text = "".join(t.text or "" for t in run.findall(f"{W}t"))
            if not text:
                continue
            if bold:
                text = f"*{text}*"
            if hl:
                text = f"<hl>{text}</hl>"
            parts.append(text)

        line = "".join(parts).strip()
        if not line:
            continue
        if style and ("Heading" in style or "Title" in style or style.startswith("outline")):
            tag = f"H[{style}]"
        elif max_sz >= 24:
            tag = f"H[sz{max_sz}]"
        elif depth is not None:
            tag = f"LI{depth}"
        else:
            tag = "P"
        lines.append(f"{tag}: {line}")
    return "\n".join(lines) or None


def run_sermon_pipeline(full_prompt: str, model: str, file_path: str | None = None) -> str:
    """Chain two agents: sermon-notes converts the doc -> sermon JSON, then org-api
    authenticates and POSTs it to the org's create endpoint.

    The JSON is handed off via a FILE, not re-typed through a model: a big payload sent
    back through a free model's prompt would risk truncation/mangling. org-api just curls
    the file verbatim.
    """
    # 1) Convert the document to sermon-notes JSON. For a .docx we feed a COMPACT outline
    #    inline (the model never reads the bloated raw XML); otherwise pass the file through.
    outline = _docx_to_outline(file_path) if file_path else None
    if outline:
        convert_prompt = (
            f"{full_prompt}\n\n"
            "The sermon document was extracted to this compact outline — one line per "
            "paragraph: `H[…]` heading, `LI<n>` list item at depth n, `P` paragraph; "
            "`<hl>…</hl>` = yellow highlight, `*…*` = bold. Convert it to the sermon-notes "
            f"JSON per the sermon-notes-parser skill.\n\n{outline}"
        )
        raw = run_opencode(convert_prompt, model=model, agent="sermon-notes")
    else:
        raw = run_opencode(full_prompt, model=model, agent="sermon-notes", file_path=file_path)
    blob = _extract_json_object(raw)
    if not blob:
        # No valid JSON to submit — surface the conversion output so the failure is visible.
        return ("⚠️ Couldn't produce valid sermon JSON to submit, so nothing was posted. "
                f"Conversion output was:\n{raw}")

    # 2) Persist the payload for a clean handoff (unique name avoids concurrent collisions).
    payload_path = os.path.join(DOWNLOADS_DIR, f"sermon_payload_{int(time.time() * 1000)}.json")
    with open(payload_path, "w", encoding="utf-8") as fh:
        fh.write(blob)

    # 3) Submit via org-api: authenticate, then POST the file's contents verbatim.
    try:
        submit_prompt = (
            "Submit a sermon note to the org system.\n"
            f"The JSON payload is in this file: {payload_path}\n"
            "Authenticate as the machine-user, then POST that file's contents as the JSON body to "
            f"`$ORG_API_BASE_URL{ORG_SERMON_CREATE_PATH}` with header `Content-Type: application/json`. "
            f"Send the body verbatim with `curl --data @{payload_path}` — do NOT retype the JSON. "
            "Report the HTTP status; on success include the created sermonId. A 409 means a sermon "
            "with that sermonId already exists."
        )
        return run_opencode(submit_prompt, model=model, agent="org-api")
    finally:
        # Don't leave sermon content sitting in the downloads dir.
        try:
            os.remove(payload_path)
        except OSError:
            pass


def dispatch(full_prompt: str, routing_text: str, file_path: str | None = None,
             default_agent: str | None = None) -> str:
    """Decide how a message is run and run it. Picks local-LLM-or-OpenRouter for the
    model, and an optional specialist agent on top."""
    model = opencode_model_for(_is_structural(routing_text))
    if USE_ORCHESTRATOR:
        return run_opencode(full_prompt, model=model, agent="orchestrator",
                            file_path=file_path)

    agent = route_agent_automatically(routing_text) or default_agent
    # Sermon docs are a two-step chain (convert -> submit), not a single agent run.
    if agent == "sermon-notes":
        return run_sermon_pipeline(full_prompt, model=model, file_path=file_path)
    return run_opencode(full_prompt, model=model, agent=agent, file_path=file_path)


# ---------------------------------------------------------------------------
# The one entry point both adapters call
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Help / introspection — answered directly in Python, NO model call. This is the
# only reliable way to list our custom agents: the LLM can't see them (it only
# knows OpenCode's built-ins), and it works even when the model is rate-limited.
# ---------------------------------------------------------------------------
def is_help_command(text: str) -> bool:
    """True if the message is asking what the bot can do / which agents exist."""
    t = text.strip().lower().lstrip("/")
    if t in ("help", "agents", "skills", "commands"):
        return True
    return any(phrase in t for phrase in (
        "what can you do", "what agents", "what skills", "which agents",
        "list agents", "list your agents", "your agents", "your skills",
        "available agents", "custom agents",
    ))


def _agent_description(path: str) -> str:
    """Pull the `description` value out of an agent .md's YAML frontmatter."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return ""
    for i, line in enumerate(lines):
        if line.strip().startswith("description:"):
            inline = line.split("description:", 1)[1].strip().lstrip(">").strip()
            if inline:
                return inline
            # Folded scalar: collect the following indented lines.
            collected = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and (nxt.startswith(" ") or nxt.startswith("\t")):
                    collected.append(nxt.strip())
                else:
                    break
            return " ".join(collected)
    return ""


def list_agents() -> list[tuple[str, str]]:
    """Return (name, description) for each custom agent .md, reflecting reality."""
    try:
        files = sorted(f for f in os.listdir(AGENT_DIR) if f.endswith(".md"))
    except OSError:
        files = []
    return [(f[:-3], _agent_description(os.path.join(AGENT_DIR, f))) for f in files]


def help_text() -> str:
    """Plain-text help (renders fine on both WhatsApp and Discord)."""
    parts = [
        "I'm a coding/admin bot. Describe a task and I'll route it to the right "
        "agent automatically.",
        "",
        "Custom agents:",
    ]
    agents = list_agents()
    if agents:
        for name, desc in agents:
            parts.append(f"• {name}" + (f": {desc}" if desc else ""))
    else:
        parts.append("• (none found)")
    parts += [
        "",
        "Examples:",
        "• parse this to xml: name=Bob, age=30",
        "• sync this contact to the crm: name=Bob, email=bob@x.com",
    ]
    return "\n".join(parts)


def handle_message(user_key: str, text: str, requester: str | None = None,
                   file_path: str | None = None, default_agent: str | None = None) -> str:
    """Full turn: load history -> dispatch to OpenCode -> persist -> return reply.

    This is BLOCKING (subprocess + pymongo). Async callers (Discord) must run it
    in a thread, e.g. `await asyncio.to_thread(core.handle_message, ...)`.
    Returns the untrimmed reply; each adapter trims to its own platform limit.
    """
    # Help/introspection is answered instantly here — no OpenCode, no model, no timeout.
    if file_path is None and is_help_command(text):
        return help_text()

    history = load_history(user_key)
    # Carry the chat requester so the model can attribute work (e.g. stamp PRs).
    context = f"[Requester: {requester}]\n" if requester else ""
    full_prompt = f"{context}{history}\nUser: {text}".strip()
    reply = dispatch(full_prompt, routing_text=text, file_path=file_path,
                     default_agent=default_agent)
    append_history(user_key, text, reply)  # store the clean reply, without the tag
    if SHOW_MODEL_SOURCE:
        reply = f"{reply}\n\n— via {model_source_label()}"
    return reply
