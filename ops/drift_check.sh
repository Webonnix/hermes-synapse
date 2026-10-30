#!/bin/sh
# Report when production no longer matches what was deployed from git.
#
# Checks: the live compose equals src/deploy/compose.prod.yml; every running
# hermes-web-* container runs the image its :local tag names (a stale or
# hand-built container is drift); src/ has no files changed after the last
# recorded release. Prints findings; with --notify, sends them to the owner's
# Telegram chat (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from src/.env).
# Exit 0 = no drift, 1 = drift found. Intended for a daily cron.
set -u

ROOT=/mnt/fast_storage/hermes-web
cd "$ROOT" || exit 2
findings=""
add() { findings="$findings
- $1"; }

cmp -s src/deploy/compose.prod.yml docker-compose.yml || add "docker-compose.yml отличается от src/deploy/compose.prod.yml"

for name in $(docker ps --format '{{.Names}}' | grep '^hermes-web-'); do
  configured=$(docker inspect -f '{{.Config.Image}}' "$name")
  case "$configured" in *:local) ;; *) continue ;; esac
  running=$(docker inspect -f '{{.Image}}' "$name")
  tagged=$(docker image inspect -f '{{.Id}}' "$configured" 2>/dev/null || echo none)
  [ "$running" = "$tagged" ] || add "$name работает не на текущем образе $configured (пересобран, но не перезапущен?)"
done

last=$(tail -n1 .deploy_backups/RELEASE 2>/dev/null | cut -d' ' -f2)
if [ -n "$last" ]; then
  stamp=$(date -u -d "$(echo "$last" | sed 's/\(....\)\(..\)\(..\)-\(..\)\(..\)/\1-\2-\3 \4:\5/')" +%s 2>/dev/null || echo 0)
  changed=$(find src -path src/node_modules -prune -o -path '*/__pycache__' -prune -o -name .env -prune \
            -o -type f -newermt "@$((stamp + 600))" -print 2>/dev/null | head -n5)
  [ -z "$changed" ] || add "файлы в src/ изменены после релиза $last: $(echo "$changed" | tr '\n' ' ')"
fi

[ -z "$findings" ] && { echo "no drift"; exit 0; }
message="Hermes: прод разошёлся с git$findings"
echo "$message"
if [ "${1:-}" = "--notify" ]; then
  token=$(grep -E '^TELEGRAM_BOT_TOKEN=' src/.env | cut -d= -f2- | tr -d '"')
  chat=$(grep -E '^TELEGRAM_CHAT_ID=' src/.env | cut -d= -f2- | tr -d '"')
  [ -n "$token" ] && [ -n "$chat" ] && curl -fsS -o /dev/null "https://api.telegram.org/bot$token/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$message"
fi
exit 1
