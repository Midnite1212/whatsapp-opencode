# WhatsApp → OpenCode Automation Backend (PoC)

A Proof of Concept that lets people message a WhatsApp number to get small coding /
admin tasks done by [OpenCode](https://opencode.ai) running headlessly against
free-tier [OpenRouter](https://openrouter.ai) models. Per-user conversation state
is tracked in MongoDB. Hosted on Railway for the PoC (DigitalOcean later).

```
WhatsApp user → Meta Cloud API webhook → pywa (server.py)
   → MongoDB (rolling per-user history, 24h reset)
   → opencode run -m openrouter/<model>  → reply over WhatsApp
```

## Files
| File | Purpose |
|------|---------|
| `server.py` | FastAPI app + pywa webhook handlers + OpenCode subprocess wrapper |
| `AGENTS.md` | Instructions OpenCode loads (XML parser + mock CRM sync skills) |
| `Dockerfile` | python:3.11-slim + Node + OpenCode CLI |
| `requirements.txt` | Python deps |
| `.env.example` | Template for required environment variables |

## Model routing
`route_model_automatically()` picks the model from keywords in the message:
- Contains `xml / json / crm / api / parse / document` → `google/gemini-2.5-flash:free`
- Otherwise → `meta-llama/llama-3.3-70b-instruct:free`

## Prerequisites
1. **MongoDB** — a free [MongoDB Atlas](https://www.mongodb.com/atlas) cluster; copy its connection string (include a db name in the path).
2. **OpenRouter** — a free API key from https://openrouter.ai/keys.
3. **Meta WhatsApp** — a [Meta for Developers](https://developers.facebook.com) app with the *WhatsApp* product added. From **API Setup** you get the Phone Number ID and a token; from **App Settings → Basic** you get the App ID and App Secret.

## Environment variables
See `.env.example`. Set these as Railway service variables (do not commit `.env`):
`MONGO_URL`, `OPENROUTER_API_KEY`, `WA_PHONE_ID`, `WA_TOKEN`, `WA_APP_ID`,
`WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `WA_CALLBACK_URL`.

## Deploy to Railway
1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo**. Railway auto-detects the `Dockerfile`.
3. Add all env vars above. Set `WA_CALLBACK_URL` to your Railway public URL (e.g. `https://your-app.up.railway.app`, no trailing slash). Railway exposes `$PORT` automatically; the container honors it.
4. Deploy and confirm `GET /` returns `{"status":"ok"}`.

## Register the Meta webhook
pywa can self-register if `callback_url` + `verify_token` (+ `app_id`/`app_secret`)
are set — it answers Meta's verification challenge on startup. If you register
manually instead, in the Meta App dashboard → **WhatsApp → Configuration → Webhook**:
- **Callback URL:** `https://your-app.up.railway.app/whatsapp`
- **Verify token:** the same string as `WA_VERIFY_TOKEN`
- **Subscribe** to the `messages` field.

## Testing
WhatsApp test numbers only deliver to recipients you've added to the allowed list
in **API Setup** until the number is published. Message the number; the bot routes
a model, runs OpenCode, and replies. Documents are saved to `/workspace/downloads`
and handed to OpenCode.

## Notes & limitations (PoC)
- Each message spawns a fresh `opencode run` (cold boot). To speed up later, run
  `opencode serve` and use `opencode run --attach`.
- History is a growing string per user, reset after 24h of inactivity. OpenCode's
  native `--session`/`--continue` is a cleaner long-term path.
- `--dangerously-skip-permissions` is enabled so headless runs don't hang on
  permission prompts. Fine for a trusted PoC; revisit before wider exposure.
- Free OpenRouter models are rate-limited; the handlers report errors over WhatsApp.
- **`openrouter/free` does NOT guarantee tool use** — it routes to random free models,
  some of which (e.g. image models) can't call tools, breaking agents that need `bash`/
  `edit` (like `crm-sync-mock`). So `MODEL_STRUCTURAL`/agent paths are pinned to a free
  *tool-capable* model (`qwen/qwen3-coder:free`); only plain chat uses `openrouter/free`.
  If you enable `USE_ORCHESTRATOR` on free, pin its model to a tool-capable one too.

## Future Improvements

### Conversation memory (the ladder)
The bot is **stateless by default** (`HISTORY_ENABLED=false`) — each message is
self-contained. That's the right default for one-shot "fix this / parse that"
requests, and it avoids context pollution, token cost, and a flaky DB dependency.
When memory is actually needed, climb this ladder in order:

1. **Stateless** *(current)* — no DB, no context bloat. Best for task-style requests.
2. **OpenCode native sessions** *(planned)* — let OpenCode own the context (it trims/
   summarises intelligently) instead of replaying a raw transcript. Track one
   session id per conversation and resume with `opencode run --session <id>`.
   See "How #2 works" below.
3. **Distilled (PAI-style) memory** *(later, once on paid)* — store extracted *facts/
   summaries* rather than raw turns (e.g. coding conventions, project context). Most
   powerful for context quality, but costs extra LLM calls to summarise and is real
   engineering. **Plan: persist in MongoDB, one document per session-user** (reuse the
   `user_sessions` collection behind `HISTORY_ENABLED` + `MONGO_URL`), storing the
   distilled facts/summary instead of the raw transcript. Only build it with a
   concrete cross-session need.

### How #2 (OpenCode sessions) will work
- **Get the session id:** run with `opencode run ... --format json`. Output becomes
  newline-delimited JSON events, each carrying a `sessionID` field (and `text` events
  carry the reply). Capture the `sessionID` on the first message of a conversation.
- **Resume it:** on later messages, pass `opencode run --session <id> ...`. Do **NOT**
  use `--continue` — it resumes the *last* session globally, so in a shared Discord
  channel one person would hijack another's conversation.
- **Scope it per conversation (avoids group chaos):** map `sessionID` to a
  conversation key, recommended in order of cleanliness:
  - **Per Discord thread** — bot replies in a thread; the thread *is* the conversation.
    Naturally isolates each person/task. Best UX for a busy channel.
  - **Per `(channel_id, user_id)`** — each person continues their own thread within a
    channel. Simpler, but replies interleave in the channel.
- **Persist the map:** the `key → sessionID` map is tiny. An in-memory dict works but
  resets on redeploy; for durability use a small store (a Railway volume, or the
  existing Mongo behind `HISTORY_ENABLED`).

### Tooling (MCPs)
`opencode.jsonc` ships with `context7` and `github` MCPs defined but `enabled: false`.
Flip them to `true` (and set `GITHUB_TOKEN`) once on a paid model that handles tools
well — `github` lets the bot open real PRs; `context7` keeps generated code current.
`serena` (semantic code edits) is stubbed in comments; it's a local MCP and needs
`uv`/`uvx` added to the Dockerfile before enabling.

### Local LLM (self-hosted) — implemented, opt-in
Use your own machine's LLM (e.g. Ollama / LM Studio) instead of OpenRouter, with
**automatic fallback to OpenRouter when your machine is unreachable**. The bot health-
checks your local endpoint (cached 30s) and routes accordingly.

**Hard requirement:** the bot runs on Railway (cloud), so it **cannot** reach
`localhost`/your home IP. Expose your local server through a **tunnel** and use that
public URL:
- Cloudflare Tunnel: `cloudflared tunnel --url http://localhost:11434` → gives an
  `https://….trycloudflare.com` URL.
- or ngrok / Tailscale Funnel.

**Setup:**
1. Run an OpenAI-compatible server locally (Ollama `http://localhost:11434/v1`;
   LM Studio `http://localhost:1234/v1`; **KoboldCpp `http://localhost:5001/v1`**).
   Note on model choice: general chat + `xml-parser` work on most models (e.g.
   Gemma 4), but the **`crm-sync-mock`/agent paths need reliable tool-calling** —
   Gemma's is weak/inconsistent, so use a tool-capable coder (e.g. `qwen2.5-coder`)
   for those, or let them fall back to cloud.
2. Start a tunnel to it; note the public URL.
3. In `opencode.jsonc`, rename the `provider.local.models` key from `local-model` to
   your served model name.
4. Set Railway env vars:
   - `LOCAL_LLM_URL` = `https://<tunnel-host>/v1`  (must end in `/v1`)
   - `LOCAL_LLM_MODEL` = general/chat/parsing model (matches a key in step 3)
   - `LOCAL_LLM_MODEL_STRUCTURAL` = coding/tools model (defaults to `LOCAL_LLM_MODEL`)
   - `LOCAL_LLM_API_KEY` = anything (most local servers ignore it)
5. Deploy. When your machine + tunnel are up, messages use the local models (general
   vs coding routed automatically); when they're down, the bot silently falls back to
   OpenRouter.

**Multiple local models:** use a server that serves many models from one endpoint —
**Ollama** (CLI; hot-swaps on demand) or **LM Studio** (GUI; enable *JIT model loading*).
KoboldCpp is one model per process, so it's not ideal for this. Get each model's exact
identifier — Ollama: `ollama pull <model>` then `ollama list` (e.g. `gemma3:27b`,
`qwen2.5-coder:32b`); LM Studio: the model id shown in the app (e.g.
`google/gemma-3-27b`). Use that exact string as both the key under
`provider.local.models` in `opencode.jsonc` AND the env var value. Point
`LOCAL_LLM_MODEL` at your general/parsing model (e.g. a Gemma) and
`LOCAL_LLM_MODEL_STRUCTURAL` at your code/tools model (e.g. a Qwen-coder). VRAM note:
a 24GB GPU holds ~one 27–32B model at a time,
so Ollama swaps them per request (a few seconds on switch) — don't expect two big
models resident at once. DeepSeek V4 is too large for 24GB; keep it as a cloud
(OpenRouter) option, not local.

**Caveats:** home upload bandwidth adds latency per turn; agent paths needing tools
require a tool-capable local model; on first local use OpenCode fetches the
`@ai-sdk/openai-compatible` provider package (one-time, needs network). Each reply is
tagged "— via local LLM" or "— via OpenRouter (cloud)" so you can see which backend
answered (toggle with `SHOW_MODEL_SOURCE=false`).

### Organization knowledge base / specialized model
Give the bot Bowtie-/org-specific knowledge (product facts, coding conventions, API
schemas, past decisions) so it answers more specifically **and uses fewer tokens** —
instead of stuffing context into every prompt, it retrieves only the relevant pieces.
Options, roughly increasing effort:
- **RAG over a vector store** — index org docs/code into a vector DB; on each request
  retrieve the top-k relevant chunks and inject just those. Cheapest path to
  specificity + token savings. Can be wired as an MCP so OpenCode queries it as a tool.
- **A dedicated knowledge MCP** — expose the knowledge base as an MCP server (like
  `context7` but for *your* docs); the agent pulls facts on demand.
- **A fine-tuned / domain-specialized small model** — fine-tune a small local model on
  org data for a specialised, cheap, fast model handling the common cases, falling back
  to a larger model for the rest. Most effort; best token economics at scale.
- Pairs naturally with memory tier #3 (distilled facts in Mongo) — the knowledge base
  is the *static* org knowledge; #3 is the *per-user* learned context.

## Going Paid: Migration Playbook

The single source of truth for what to change when moving off the free tier.
Each step is independent — do them in any order, or only the ones you want.

### 1. Pin paid models (env only, no code change)
Models are already env-driven in `core.py`. Set on Railway:
- `MODEL_GENERAL=deepseek/deepseek-v4-flash` — cheap, fast, for chat/understanding.
- `MODEL_STRUCTURAL=qwen/qwen3-coder-30b-a3b-instruct` — best cost/quality coder for
  XML/CRM/PR work (~5× cheaper than qwen-2.5-coder and newer).
- Leaving them unset keeps `openrouter/free` (random free model — unreliable).
- Rough cost at a few users: **~US$3–25/month**. Skip reasoning models (R1) — their
  output pricing balloons. Enable provider **prompt caching** to cut agentic-loop cost.

### 2. Turn on the MCPs (config + env)
In `opencode.jsonc`, flip `"enabled": true` for:
- `context7` — no secret needed; keeps generated code's API usage current.
- `github` — set `GITHUB_TOKEN` on Railway; **this is what lets the bot open real PRs.**
- `serena` (optional) — add `uv` to the Dockerfile (`pip install uv` or the install
  script), uncomment the block, set `"enabled": true`. Gives semantic code edits.
- Note: each MCP adds tools = more tokens; paid models handle many tools far better
  than free ones, which is why this waits for paid.

### 3. Conversation memory #2 — OpenCode sessions (CODE WORK)
Goal: real multi-turn continuity, scoped per Discord thread (no group cross-talk).
Implementation, when you say go:
- In `core.run_opencode`: add `--format json`, and parse the newline-delimited events
  to extract (a) the assistant reply from `text` events and (b) the `sessionID`.
- Add a `key → sessionID` map. **Key = Discord thread id** (bot replies in a thread;
  the thread is the conversation). Fallback key: `(channel_id, user_id)`.
- First message in a thread: run normally, capture `sessionID`, store it.
  Later messages: run with `--session <stored id>`. **Never `--continue`** (it grabs
  the last session globally → cross-user hijacking).
- Persist the map: start with an in-memory dict (resets on redeploy); for durability
  use a Railway volume or the Mongo store.

### 4. Conversation memory #3 — distilled PAI-style memory (CODE WORK, later)
Goal: durable cross-session knowledge (conventions, project facts), not raw transcript.
Implementation:
- Set `HISTORY_ENABLED=true` + `MONGO_URL` (re-enables the Mongo connection in `core.py`).
- **Store one document per session-user in the `user_sessions` collection**, but replace
  the raw-transcript field with a *distilled summary*: after each turn, run a cheap model
  (e.g. `deepseek-v4-flash`) to update a short facts/summary blob; inject that blob
  (not the transcript) into the next prompt.
- This is the "memory format like PAI" — compact, durable, low context pollution.

### 5. Optional: multi-model per message
Set `USE_ORCHESTRATOR=true` to route every message through the `orchestrator` agent,
which delegates to `xml-parser` / `crm-sync-mock` subagents (each can use its own model).
More model calls; only worth it on paid.
