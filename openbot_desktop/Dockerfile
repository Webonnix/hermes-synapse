# OpenBot (github.com/nightly-labs/openbot, PolyForm Noncommercial 1.0.0) as an
# agent desktop: the upstream server image, pinned by digest, plus what Hermes
# needs to show and reach it — x11vnc on both of its screens (:0 the OpenBot
# window and its browser, :1 the agents' desktop), a terminal for
# `openbot login`, and the agent desktop bridge (/vnc, /health).
# backend/agent_runtimes/desktop.py starts it under gVisor like any desktop.
# Built only: `docker compose build openbot-desktop`.
ARG OPENBOT_IMAGE=ghcr.io/nightly-labs/openbot:0.54.0@sha256:46d86c48efdece9f0aae2338e96f6bfe01fa8c7c3a49075ce0fe42d362683039
FROM ${OPENBOT_IMAGE}

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    x11vnc xterm python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/hermes-bridge \
    && /opt/hermes-bridge/bin/pip install --no-cache-dir "fastapi>=0.100.0" "uvicorn[standard]>=0.22.0"

COPY agent_desktop/server.py /desktop/server.py
COPY openbot_desktop/start.sh /desktop/start.sh

# Back to the upstream image's own user (uid 1000): its entrypoint refuses root.
USER openbot

EXPOSE 8900

ENTRYPOINT ["/usr/bin/tini", "--", "/bin/bash", "/desktop/start.sh"]
