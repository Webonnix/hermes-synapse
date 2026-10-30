#!/usr/bin/env bash
# Daily consistent Hermes backup (hermes-backup.timer runs it as root).
#
# 1. SQLite via the online .backup API, Qdrant snapshots, code + config, and the
#    rest of data/ (agent homes, Earner, content factory, Matrix and telephony
#    state) without caches, models, voices or old pre-deploy DB copies.
# 2. Verifies the copy: SHA256SUMS and an SQLite integrity_check of hermes.db.
# 3. Mirrors it to a second physical disk (HERMES_MIRROR_ROOT), so losing the
#    NVMe that holds the data does not lose the backups too.
# 4. Off-site: when deploy/restic.env exists (RESTIC_REPOSITORY, RESTIC_PASSWORD
#    and the backend's credentials) and restic is installed, the backup is sent
#    to that repository and pruned to 7 daily / 4 weekly / 6 monthly.
# Installed copy: /mnt/fast_storage/hermes-web/deploy/hermes-backup.sh.
set -Eeuo pipefail

PROJECT_ROOT=${HERMES_PROJECT_ROOT:-/mnt/fast_storage/hermes-web}
BACKUP_ROOT=${HERMES_BACKUP_ROOT:-/mnt/fast_storage/hermes-backups}
MIRROR_ROOT=${HERMES_MIRROR_ROOT:-/mnt/backup_storage/hermes-backups}
RETENTION_DAYS=${HERMES_BACKUP_RETENTION_DAYS:-14}
MIRROR_RETENTION_DAYS=${HERMES_MIRROR_RETENTION_DAYS:-30}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TARGET="$BACKUP_ROOT/$STAMP"
TARGET_TMP="$BACKUP_ROOT/.$STAMP.incomplete"
TMP_DB="$PROJECT_ROOT/data/.hermes-backup-$STAMP.db"

umask 077
mkdir -p "$TARGET_TMP/qdrant"

cleanup() {
  rm -f "$TMP_DB"
  if [[ -n ${TARGET_TMP:-} && -d $TARGET_TMP ]]; then
    rm -rf -- "$TARGET_TMP"
  fi
}
trap cleanup EXIT

docker exec hermes-web-backend python -c \
  "import sqlite3; source=sqlite3.connect('/app/backend/data/hermes.db'); target=sqlite3.connect('/app/backend/data/.hermes-backup-$STAMP.db'); source.backup(target); target.close(); source.close()"
install -m 600 "$TMP_DB" "$TARGET_TMP/hermes.db"

backup_paths=(src docker-compose.yml deploy)
[[ -d "$PROJECT_ROOT/obsidian/vaults" ]] && backup_paths+=(obsidian/vaults)
[[ -f "$PROJECT_ROOT/obsidian/.config/obsidian/obsidian.json" ]] && \
  backup_paths+=(obsidian/.config/obsidian/obsidian.json)

tar --exclude='.git' --exclude='node_modules' --exclude='dist' \
  --exclude='__pycache__' --exclude='backend/data' \
  -czf "$TARGET_TMP/config-and-code.tar.gz" \
  -C "$PROJECT_ROOT" "${backup_paths[@]}"

# Application state beyond the main DB. The live hermes.db (copied above),
# downloadable models and voices, caches and earlier pre-deploy copies stay out.
tar --exclude='./hermes.db' --exclude='./hermes.db-*' --exclude='./hermes.pre*' --exclude='./hermes-before*' \
  --exclude='./hermes.db.bak*' --exclude='./.hermes-backup-*' --exclude='./voice-models' --exclude='./piper-voices' \
  --exclude='./cache' --exclude='./home/.cache' --exclude='./dev-repo-runs' --exclude='*.sock' \
  -czf "$TARGET_TMP/data.tar.gz" -C "$PROJECT_ROOT/data" .

snapshots=$(docker exec hermes-web-backend python -c \
  "import json,urllib.request; base='http://qdrant:6333'; collections=json.load(urllib.request.urlopen(base+'/collections'))['result']['collections']; print(''.join(c['name']+'|'+json.load(urllib.request.urlopen(urllib.request.Request(base+'/collections/'+c['name']+'/snapshots',data=b'',method='POST')))['result']['name']+'\\n' for c in collections), end='')")

while IFS='|' read -r collection snapshot; do
  [[ -n $collection && -n $snapshot ]] || continue
  docker cp \
    "hermes-web-qdrant:/qdrant/snapshots/$collection/$snapshot" \
    "$TARGET_TMP/qdrant/$collection-$snapshot"
done <<<"$snapshots"

(
  cd "$TARGET_TMP"
  sha256sum hermes.db config-and-code.tar.gz data.tar.gz qdrant/* >SHA256SUMS
)

# A backup that cannot be restored is not one: check the DB copy opens cleanly.
python3 -c "import sqlite3,sys; r=sqlite3.connect('file:$TARGET_TMP/hermes.db?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchone()[0]; sys.exit(0 if r=='ok' else 'hermes.db integrity_check: '+r)"

mv "$TARGET_TMP" "$TARGET"
TARGET_TMP=
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION_DAYS" -exec rm -rf -- {} +

if [[ -d $(dirname "$MIRROR_ROOT") ]]; then
  mkdir -p "$MIRROR_ROOT"
  cp -a "$TARGET" "$MIRROR_ROOT/"
  (cd "$MIRROR_ROOT/$STAMP" && sha256sum -c --quiet SHA256SUMS)
  find "$MIRROR_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$MIRROR_RETENTION_DAYS" -exec rm -rf -- {} +
  printf 'Mirrored to %s\n' "$MIRROR_ROOT/$STAMP"
fi

if [[ -f $PROJECT_ROOT/deploy/restic.env ]] && command -v restic >/dev/null; then
  set -a; . "$PROJECT_ROOT/deploy/restic.env"; set +a
  restic snapshots >/dev/null 2>&1 || restic init
  restic backup --tag hermes --host hermes-prod "$TARGET"
  restic forget --tag hermes --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
  printf 'Off-site copy sent to %s\n' "$RESTIC_REPOSITORY"
else
  printf 'Off-site copy not configured (deploy/restic.env + restic)\n'
fi
printf 'Hermes backup completed: %s\n' "$TARGET"
