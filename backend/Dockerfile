FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# Disable Python stdout buffering to ensure logs print immediately
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/app/backend/data/home
ENV HF_HOME=/app/backend/data/cache/huggingface
ENV MPLCONFIGDIR=/app/backend/data/cache/matplotlib

WORKDIR /app

# RHVoice and its Russian voices are distributed in Debian's non-free component.
RUN sed -Ei 's/Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    ffmpeg \
    nodejs \
    npm \
    rhvoice \
    rhvoice-russian \
    rhvoice-english \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies come from backend/uv.lock only — the exact versions CI
# tested — so a rebuild can never pick up a newer release than the one that
# passed. A hand-written ">=" list here used to let the prod image drift from CI.
# The dev group stays in: autonomy.py probes for pytest in this container.
# Installed into the system interpreter (not a venv), as before, so `python`,
# `pip` and console scripts behave exactly as they did.
COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv
COPY backend/pyproject.toml backend/uv.lock /tmp/uv-lock/
RUN cd /tmp/uv-lock \
    && uv export --quiet --frozen --no-emit-project --group dev --format requirements-txt -o requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r requirements.txt \
    && rm -rf /tmp/uv-lock

# Git is a required read-only workspace capability for planning and verification.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Copy source code and environment configuration
COPY backend/ /app/backend/

RUN useradd --uid 10001 --create-home hermes \
    && mkdir -p /app/backend/data/home /app/backend/data/cache \
    && chown -R 10001:10001 /app/backend/data

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-level", "warning"]
