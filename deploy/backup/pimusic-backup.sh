#!/bin/bash
# Ночная резервная копия PiMusic: БД (консистентно), конфиги, .env, медиатека (снимок с жёсткими ссылками).
# Настройки — /etc/pimusic-backup.conf (все необязательны):
#   PROJECT_DIR=/opt/pimusic      папка с docker-compose.yml и .env
#   DATA_DIR=/srv/pimusic         DATA_DIR из .env
#   BACKUP_DIR=/srv/pimusic-backup  куда класть копии (лучше отдельный диск/том)
#   KEEP_DAYS=14                  сколько дневных копий хранить
#   BACKUP_REMOTE="user@host:/path"  внешняя копия (rsync по ssh-ключу /root/.ssh/pimusic_backup)
set -euo pipefail
[ -f /etc/pimusic-backup.conf ] && . /etc/pimusic-backup.conf
PROJECT_DIR=${PROJECT_DIR:-/opt/pimusic}; SRC=${DATA_DIR:-/srv/pimusic}; DST=${BACKUP_DIR:-/srv/pimusic-backup}
KEEP_DAYS=${KEEP_DAYS:-14}; DAY=$(date +%F)
mkdir -p "$DST/daily/$DAY" "$DST/media"
python3 - "$SRC/state/pimusic.db" "$DST/daily/$DAY/pimusic.db" <<"PY"
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
P=${PROJECT_DIR#/} S=${SRC#/}
tar czf "$DST/daily/$DAY/config.tar.gz" -C / --exclude="$S/state/pimusic.db*" "$P/.env" "$P/docker-compose.override.yml" \
  "$S/state" "$S/slskd/slskd.yml" "$S/jellyfin/config/config" "$S/jellyfin/config/data/jellyfin.db" 2>/dev/null || true
# медиатека: один снимок, обновляется инкрементально, жёсткие ссылки (cache -> users) сохраняются
rsync -aH --delete --exclude=incoming/ "$SRC/media/" "$DST/media/"
# храним KEEP_DAYS последних дневных копий
ls -1d "$DST"/daily/* | sort | head -n -"$KEEP_DAYS" | xargs -r rm -rf
if [ -n "${BACKUP_REMOTE:-}" ]; then
  rsync -aH --delete -e "ssh -i /root/.ssh/pimusic_backup -o BatchMode=yes -o ConnectTimeout=20" "$DST/" "$BACKUP_REMOTE/" || echo "внешняя копия не удалась" >&2
fi
echo "backup ok $(date -Is) $(du -sh $DST | cut -f1)"
