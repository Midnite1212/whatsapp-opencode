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
