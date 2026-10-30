FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93
COPY backend/pyproject.toml backend/uv.lock /tmp/deps/
COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv
RUN cd /tmp/deps && uv export --frozen --no-emit-project --no-dev --format requirements-txt -o requirements.txt \
    && uv pip install --system --require-hashes -r requirements.txt && rm -rf /tmp/deps
COPY sandbox_launcher /app/sandbox_launcher
WORKDIR /app
CMD ["uvicorn", "sandbox_launcher.server:app", "--host", "0.0.0.0", "--port", "2375", "--no-access-log"]
