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

## Integrations: GitHub PRs & Org API

### GitHub (bot identity → PRs)
The bot opens PRs as whatever `GITHUB_TOKEN` belongs to, and supports **two auth modes**.

**PoC (current): fine-grained PAT.** Generate a fine-grained PAT with **Contents: R/W**
+ **Pull requests: R/W** and set `GITHUB_TOKEN`. For the PoC you can use **your own
account** (PRs show as you) — fastest, no new accounts. Per-user attribution is
preserved in the PR body: every PR the bot opens ends with **`Requested by: <chat
user>`** (the Discord display name / WhatsApp profile name of whoever asked), injected
from the message context per `AGENTS.md`.

**Production: dedicated identity — DECISION FOR LAUNCH (open).** Pick one:
- a dedicated **machine user** + its own fine-grained PAT (separate identity, simple), or
- your **own GitHub App** (`[bot]` identity, higher rate limits, no extra seat). The bot
  already mints installation tokens automatically — set `GITHUB_APP_ID`,
  `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` (App ID + Installation ID from
  the App settings; PEM from "Generate a private key"; Railway-mangled newlines may be
  given as literal `\n`) and leave `GITHUB_TOKEN` unset.
- Note: a **third-party** installed app (the existing `opencode-agent`) can't be used —
  you don't have its private key. It must be your own App.

Either mode is **API-only** via the GitHub MCP: `create_branch` → `push_files` →
`create_pull_request`, no repo clone. The PR/issue *procedures* live in the
`github-pr` / `github-issues` / `github-fix-issue` skills; the org/default-repo
convention is in `AGENTS.md`.

**On/off via env — `GITHUB_MCP_ENABLED`.** The remote GitHub MCP must be reached on
**every** run and dumps many tool schemas into the prompt — on a small local model that
bloats context, and a missing/blocked `GITHUB_TOKEN` makes OpenCode **hang at MCP init
before the model even starts**. So it's **off by default** and toggled by the
`GITHUB_MCP_ENABLED` Railway variable (`true`/`false`). OpenCode's `{env:…}` only
interpolates *string* values, not the boolean `enabled`, so `core.py` regenerates the
effective config from `opencode.jsonc` with this flag applied (`_build_runtime_config`) —
flip it in Railway, no image rebuild. Set `GITHUB_MCP_ENABLED=true` (with a valid
`GITHUB_TOKEN`) only when you want PR/issue work; leave it off for the sermon/local-model
flow.

### Org API (authenticated POST/GET via a machine-user account)
Handled by the **`org-api` agent** (`.opencode/agent/org-api.md`), auto-routed on
keywords like *sync / post / submit / upload / crm / api*. The org has no app-client
infrastructure, so the bot authenticates as a **dedicated machine-user account**
(least-privilege, MFA-exempt). It reads secrets only from env, logs in, then calls the
endpoints. Set on Railway:
- `ORG_API_BASE_URL` — base URL of the org API
- `ORG_LOGIN_URL` — the login endpoint, i.e. `<base>/api/auth/login`
- `ORG_USERNAME` — the machine-user **email** (sent as the `emailAddress` field)
- `ORG_PASSWORD` — its password
- `ORG_SERMON_CREATE_PATH` — optional; sermon create path (default `/api/sermon-notes-parent/create`)

Flow (verified against the org's Sails backend):
`POST /api/auth/login` with `{ emailAddress, password }` → the **response body is the
raw JWT string** (not `{token: …}`) → send it on every call as the header
**`Authorisation: Bearer <jwt>`**. ⚠️ Note the **British spelling `Authorisation`**
(with an "s") and the literal `Bearer` scheme — the server rejects the standard
`Authorization` (with a "z") with a 401. (`crm-sync-mock.md` remains for offline mock testing.)

Sermon submission is wired end-to-end: the converted sermon-notes JSON is POSTed to
**`$ORG_API_BASE_URL/api/sermon-notes-parent/create`** (the body is the JSON object
as-is; a duplicate `sermonId` returns `409`).

Security note: a machine user with a password is a bigger blast radius than scoped
client credentials — keep it least-privilege, MFA-exempt (so login isn't blocked),
and rotate the password. Revisit for production.

### Skills (the way to add capabilities)
Capabilities are added as **skills**, not agents. A skill is a folder with a `SKILL.md`:
```
.opencode/skills/<name>/SKILL.md
```
Skills are **global** and use **progressive disclosure** — the model only sees each
skill's name + description (cheap) and loads the full body **on demand** when relevant.
That keeps per-request context lean (important for the local model) and makes skills
reusable across agents. They're registered via `skills.paths` in `opencode.jsonc`
(absolute path, since the bot's cwd isn't reliably `/workspace`).

Prefer a **skill** for a new *procedure* (e.g. "convert sermon notes", "fetch a member
record"); reserve an **agent** for a distinct *tool/permission/model profile*.

First skill: **`sermon-notes-parser`** — converts a sermon-prep doc (`.docx`) → the org's
sermon-notes JSON (tiptap `originalContent` + metadata). The full mapping lives in its
`SKILL.md`, with a worked example in `reference.json`. Runs via the bash-capable
**`sermon-notes`** agent (it unzips the `.docx` to read which runs are highlighted).
End-to-end (implemented, auto-submit): a sermon doc routes to the `sermon-notes` agent,
and `core.run_sermon_pipeline` chains the two steps — `sermon-notes-parser` converts the
doc → JSON, the JSON is handed to `org-api` via a file (so a big payload isn't re-typed
through the model), and `org-api` POSTs it to the create endpoint and replies with the
status.

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
3. **List your model(s) in `opencode.jsonc`** under `provider.local.models` — OpenCode
   only accepts model ids declared there. Use the exact ids from `GET /v1/models`
   (e.g. `google/gemma-4-31b-qat`, `qwen/qwen3.6-27b`). *(Editing `opencode.jsonc`
   requires a redeploy — it's baked into the image, unlike env vars.)*
4. Set Railway env vars:
   - `LOCAL_LLM_URL` = `https://<tunnel-host>/v1`  (must end in `/v1`; auto-normalized)
   - `LOCAL_LLM_MODEL` = a listed id for general/chat/parsing
   - `LOCAL_LLM_MODEL_STRUCTURAL` = a listed id for coding/tools (defaults to `LOCAL_LLM_MODEL`)
   - `LOCAL_LLM_API_KEY` = anything (most local servers ignore it)
   - `LOCAL_LLM_MODEL=auto` discovers the served model, but it **still must be one of
     the listed ids** — so `auto` is best when you list+load a single chat model.
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

## Production Hardening (when this stops being a PoC)

Everything below is fine for the PoC but should be revisited before real/wider use.
Consolidated from the whole build.

### Hosting & infrastructure
- **Local LLM via home PC + ngrok is PoC-only.** It relies on your machine being on and
  an obscurity-only tunnel. For production, host the model on a real GPU server (the
  planned DigitalOcean box) or use a paid API — and put it behind stable, authenticated
  ingress (Cloudflare Tunnel + Access, or same-network hosting), not a throwaway tunnel.
- **Stay single-worker / single-replica** as built — Discord allows one gateway
  connection per token and LM Studio serves one slot. To scale you need Discord
  sharding, a **job queue**, and `opencode serve` + worker processes; naively running
  multiple replicas will break the Discord connection and the single tunnel.
- **OpenCode cold-boots per message.** Optimize latency with `opencode serve` +
  `opencode run --attach` instead of spawning a fresh process each time.

### Security
- **`--dangerously-skip-permissions` auto-approves ALL tool actions** (including `bash`
  and `edit`). The bot can run arbitrary shell in its container. Acceptable for a trusted
  PoC; before wider exposure, scope permissions per agent and sandbox/limit the shell.
- **Lock down the local-LLM endpoint** — never leave an open LLM on a public URL. Use a
  bearer-checking proxy (Caddy) or Cloudflare Access.
- **Abuse & input controls** — per-user rate limits, input validation, and cost caps
  (token budgets). Today any allow-listed user can trigger arbitrary agentic work.
- **MongoDB (if re-enabled):** replace the `0.0.0.0/0` access list with restricted
  access (static egress IP / VPC peering) once you have a stable egress IP.

### Identity & secrets
- **WhatsApp:** move from the **test number + 24h token + allow-list** to a real phone
  number, **Meta Business verification**, and a permanent **System User token**.
- **GitHub identity — OPEN DECISION for launch.** PoC uses a **personal-account PAT**
  (fine short-term, but PRs come from a human and it's not a real bot identity). Before
  launch, choose:
  - a dedicated **machine user** + fine-grained PAT (separate identity, minimal setup), or
  - your **own GitHub App** (`[bot]` identity, higher rate limits, no extra seat) — the
    token-minting code already supports it (`GITHUB_APP_*`).
  (The existing third-party `opencode-agent` app can't be used — no access to its key.)
  Either way, per-user attribution stays in the PR body (`Requested by: <chat user>`).
- **Secret hygiene:** rotate `GITHUB_TOKEN`/`OPENROUTER_API_KEY`/`ORG_CLIENT_SECRET`,
  apply least-privilege scopes, and consider a secrets manager over raw env vars.

### Reliability
- **Add retries/backoff** for model and org-API rate limits instead of surfacing the
  raw error to the user.
- **Observability:** structured logging, monitoring, and alerting (right now failures
  only show up in Railway logs / the chat reply).

### Models, memory & tools
- **Move off free OpenRouter** (rate-limited, models churn) to paid models or a properly
  hosted local model — see the *Going Paid* playbook above.
- **Add conversation memory** if needed (tier #2 sessions / #3 distilled) — see
  *Future Improvements*.
- **Scope MCP tools per agent.** Enabling big MCP toolsets (e.g. GitHub) globally adds
  many tools to every request, which strains weaker/local models' tool loops. Restrict
  GitHub tools to a dedicated PR agent rather than enabling them for all messages.

### Data handling
- **Uploaded documents** land in `/workspace/downloads` — add cleanup, size limits, and
  scanning before processing untrusted files.
- **org-api:** minimize OAuth scopes and rotate the client secret.
