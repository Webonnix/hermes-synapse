#!/bin/sh
# Deploy Hermes services on the production host, with automatic rollback.
#
#   ops/deploy.sh <release-id> <service>...      e.g. ops/deploy.sh 8b35e83 backend frontend
#
# Run from /mnt/fast_storage/hermes-web/src after the release's files were
# synced into src/ (git archive <commit>, rsync without --delete). It:
#   1. checks the compose file and that src/deploy/compose.prod.yml matches it,
#   2. backs up src/.env + compose and the SQLite DB (online .backup),
#   3. tags each service's current image :pre-<release>-<ts>,
#   4. builds and recreates only the named services,
#   5. waits for every one to be healthy, and on any failure restores the
#      previous images and compose, then exits non-zero.
# It records the release in .deploy_backups/RELEASE for ops/drift_check.sh.
set -eu

ROOT=/mnt/fast_storage/hermes-web
RELEASE=${1:?release id (git commit) required}
shift
[ "$#" -gt 0 ] || { echo "name at least one service" >&2; exit 2; }
TS=$(date -u +%Y%m%d-%H%M)
BACKUP=$ROOT/.deploy_backups/$RELEASE-$TS
COMPOSE="docker compose --env-file $ROOT/src/.env -f $ROOT/docker-compose.yml"

cd "$ROOT"
cmp -s src/deploy/compose.prod.yml docker-compose.yml || {
  echo "docker-compose.yml differs from src/deploy/compose.prod.yml; commit the server's copy first" >&2; exit 3; }
$COMPOSE config -q

mkdir -p "$BACKUP"
cp docker-compose.yml "$BACKUP/docker-compose.yml"
cp src/.env "$BACKUP/env"
chmod 600 "$BACKUP/env"
if docker inspect hermes-web-backend >/dev/null 2>&1; then
  docker exec hermes-web-backend python -c "import sqlite3; s=sqlite3.connect('/app/backend/data/hermes.db'); d=sqlite3.connect('/app/backend/data/hermes.pre-$RELEASE-$TS.db'); s.backup(d); d.close()"
fi

image_of() { $COMPOSE config --format json | python3 -c "import json,sys; print(json.load(sys.stdin)['services']['$1'].get('image',''))"; }
container_of() { $COMPOSE ps -a --format '{{.Name}}' "$1" 2>/dev/null | head -n1; }

for service in "$@"; do
  image=$(image_of "$service")
  [ -n "$image" ] && docker image inspect "$image" >/dev/null 2>&1 && docker tag "$image" "${image%:*}:pre-$RELEASE-$TS"
done

rollback() {
  echo "deploy failed; rolling back $*" >&2
  for service in "$@"; do
    image=$(image_of "$service")
    docker image inspect "${image%:*}:pre-$RELEASE-$TS" >/dev/null 2>&1 && docker tag "${image%:*}:pre-$RELEASE-$TS" "$image"
  done
  cp "$BACKUP/docker-compose.yml" docker-compose.yml
  $COMPOSE up -d --no-deps "$@" || true
  exit 1
}

$COMPOSE build "$@" || rollback "$@"
$COMPOSE up -d --no-deps "$@" || rollback "$@"

deadline=$(( $(date +%s) + 300 ))
for service in "$@"; do
  name=$(container_of "$service")
  while :; do
    state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)
    case "$state" in healthy|running) break ;; unhealthy|exited|dead|missing) rollback "$@" ;; esac
    [ "$(date +%s)" -lt "$deadline" ] || rollback "$@"
    sleep 5
  done
done

printf '%s %s %s\n' "$RELEASE" "$TS" "$*" >> .deploy_backups/RELEASE
echo "deployed $RELEASE ($*); rollback images tagged :pre-$RELEASE-$TS, backup in $BACKUP"
