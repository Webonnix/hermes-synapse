# One agent's own Linux desktop: an X display with a window manager, a visible
# Chromium and a terminal (watched and taken over from the dashboard over VNC),
# plus the CLI coding agents it can run in ACP mode. backend/agent_runtimes/
# desktop.py starts one container of this image per agent, under gVisor.
# Built only, never run by compose: `docker compose build agent-desktop`.
FROM python:3.11-slim-bookworm

ARG NODE_VERSION=22.20.0
# Pinned CLI agents (see RUNTIMES in agent_desktop/server.py). Bump deliberately:
# an agent's ACP behaviour is part of what the backend relies on.
ARG OPENCODE_VERSION=1.19.3
ARG GROK_VERSION=1.0.70

ENV PYTHONUNBUFFERED=1 \
    DISPLAY=:1 \
    HOME=/home/agent \
    AGENT_WORKSPACE=/home/agent/workspace \
    NPM_CONFIG_UPDATE_NOTIFIER=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git xz-utils procps \
    xvfb x11vnc fluxbox xterm chromium \
    fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /usr/local --strip-components=1 \
    && npm install -g "opencode-ai@${OPENCODE_VERSION}" "@xai-official/grok@${GROK_VERSION}" \
    && npm cache clean --force

RUN pip install --no-cache-dir "fastapi>=0.100.0" "uvicorn[standard]>=0.22.0"

# Same uid as the dev-runner sandbox (backend/dev_sandbox.py SANDBOX_UID): the
# backend chowns each desktop's bind-mounted home to it before the first start.
RUN useradd --create-home --uid 10001 --shell /bin/bash agent

COPY agent_desktop/server.py /desktop/server.py
COPY agent_desktop/start.sh /desktop/start.sh

USER agent
WORKDIR /home/agent

EXPOSE 8900

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/health', timeout=3).read()"

CMD ["/bin/bash", "/desktop/start.sh"]
