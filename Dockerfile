# ---------------------------------------------------------------------------
# WhatsApp -> OpenCode PoC image
# Slim Python base + Node toolchain (OpenCode CLI needs Node) + OpenCode itself.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# Avoid interactive prompts and keep Python output unbuffered for live logs.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# System utilities required by OpenCode and for cloning/fetching repos.
#   curl  -> fetch the OpenCode install script
#   git   -> OpenCode operates on git repos
#   nodejs/npm -> OpenCode is a Node CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Install the OpenCode CLI globally.
# NOTE: the install script lives at /install — the bare https://opencode.ai
# returns the marketing site HTML, which would silently fail when piped to bash.
RUN curl -fsSL https://opencode.ai/install | bash

# Make sure the OpenCode binary is on PATH (the installer drops it in ~/.opencode/bin).
ENV PATH="/root/.opencode/bin:${PATH}"

# ---- Python application setup -------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (WhatsApp adapter + shared core + Discord adapter).
COPY server.py core.py discord_bot.py ./

# OpenCode reads AGENTS.md, opencode.json, and .opencode/agent/*.md from its
# working directory. We run OpenCode inside /workspace, so they must live there.
COPY AGENTS.md /workspace/AGENTS.md
COPY opencode.json /workspace/opencode.json
COPY .opencode /workspace/.opencode

# Isolated, writable location for inbound WhatsApp documents.
RUN mkdir -p /workspace/downloads

EXPOSE 8000

# Railway sets $PORT; default to 8000 locally. uvicorn serves the FastAPI app
# that pywa has registered its webhook routes onto.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
