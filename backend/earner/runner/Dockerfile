# Test runner for the Autonomous Earner's repository mode (backend/earner/sandbox.py, run_repo).
#
# The repository's tests run in gVisor with no network, so dependencies cannot
# be installed at test time. This image is the prepared cache: common test and
# library packages, installed once, here, by the operator — pinned versions,
# wheels only (no sdist builds, so no package code runs during the build).
#
# Build on the server (needs network once):
#   docker build -t hermes-earner-runner:local backend/earner/runner
# Then set EARNER_REPO_SANDBOX_IMAGE=hermes-earner-runner:local for the backend.
# Without it, repository runs use EARNER_SANDBOX_IMAGE (hermes-web-dev-runner:local,
# which has pytest but few libraries) — tests that import a missing package fail
# on the base commit too and are reported as such ("нет в образе").
#
# Pin the base by digest when building for production:
#   docker pull python:3.11-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.11-slim
# python:3.11-slim as pulled on the server on 2026-10-20 (image created 2026-10-06).
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --no-compile -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin earner

USER 10001:10001
WORKDIR /tmp
