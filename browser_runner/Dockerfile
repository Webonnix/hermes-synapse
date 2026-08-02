# Headless-Chromium sidecar running a browser-use Agent loop against Hermes's
# own configured LLM. See src/backend/tools.py's browser_read/browser_task and
# src/backend/dev_runner_api.py for the sibling-container pattern this mirrors.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    # browser-use / playwright cache land on a named volume so a rebuild does
    # not re-download the Chromium binary every time.
    PLAYWRIGHT_BROWSERS_PATH=/browsers

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# NOTE: browser-use 0.13.x ships a platform-specific "core" extra
# (browser-use-core, linux/x86_64 wheel) that is expected to bundle its own
# Chromium; `playwright install --with-deps chromium` is kept alongside it as
# a belt-and-suspenders fallback (older/pinned browser-use versions manage
# Chromium via Playwright only). Verify against the pinned version's install
# docs on first build — if `browser-use-core` alone turns out to be
# sufficient, the playwright install step can be dropped.
RUN pip install --no-cache-dir \
    "browser-use[core]" \
    "playwright" \
    "fastapi>=0.100.0" \
    "uvicorn>=0.22.0" \
    && playwright install --with-deps chromium

COPY browser_runner/server.py /srv/server.py

EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=120s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/health', timeout=3).read()"

CMD ["uvicorn", "server:app", "--app-dir", "/srv", "--host", "0.0.0.0", "--port", "8800", "--no-access-log"]
