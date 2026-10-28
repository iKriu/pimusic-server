#!/bin/bash
# Ежедневная уборка PiMusic: старые незавершённые загрузки slskd и висячие образы Docker/Podman.
# Настройки (необязательно) — /etc/pimusic-cleanup.conf: INCOMPLETE_DIR=…, INCOMPLETE_DAYS=…, PRUNE_IMAGES=0|1.
# Удаляет только обычные файлы и пустые папки ВНУТРИ INCOMPLETE_DIR: find не идёт по симлинкам (-P) и не
# выходит за раздел (-xdev), имена с пробелами/переводами строк безопасны (-delete, без xargs и разбора вывода).
set -euo pipefail
[ -f /etc/pimusic-cleanup.conf ] && . /etc/pimusic-cleanup.conf
# по умолчанию — <DATA_DIR>/slskd/incomplete при DATA_DIR=/srv/pimusic
INCOMPLETE_DIR=${INCOMPLETE_DIR:-/srv/pimusic/slskd/incomplete}
# загрузка slskd обрывается ботом через SOULSEEK_DOWNLOAD_TIMEOUT (10 мин); всё старше 3 дней уже не докачается
INCOMPLETE_DAYS=${INCOMPLETE_DAYS:-3}
PRUNE_IMAGES=${PRUNE_IMAGES:-1}

case "$INCOMPLETE_DAYS" in ''|*[!0-9]*) echo "INCOMPLETE_DAYS должно быть целым числом" >&2; exit 1 ;; esac
[ "$INCOMPLETE_DAYS" -ge 1 ] || { echo "INCOMPLETE_DAYS должно быть >= 1" >&2; exit 1; }

# защита от опечаток в настройке: только настоящая папка (не симлинк) с именем incomplete, не корень
if [ -L "$INCOMPLETE_DIR" ] || [ ! -d "$INCOMPLETE_DIR" ]; then
  echo "нет папки $INCOMPLETE_DIR (или это симлинк) — пропуск" >&2
else
  real=$(realpath -e -- "$INCOMPLETE_DIR")
  if [ "$(basename -- "$real")" != incomplete ] || [ "$real" = / ]; then
    echo "подозрительный путь $real — пропуск" >&2
  else
    freed=$({ find -P "$real" -xdev -mindepth 1 -type f -mtime "+$INCOMPLETE_DAYS" -printf '%s\n' -delete \
      || echo "часть файлов не удалось удалить" >&2; } | awk '{n++; s+=$1} END {printf "%d файлов, %.1f МБ", n, s/1048576}')
    # пустые папки (пользователи Soulseek) — только давно не менявшиеся, чтобы не мешать текущим загрузкам
    find -P "$real" -xdev -mindepth 1 -depth -type d -empty -mmin +360 -delete || true
    echo "incomplete: удалено $freed (старше $INCOMPLETE_DAYS дн.), осталось $(du -sh -- "$real" | cut -f1)"
  fi
fi

# висячие образы (<none> без потомков): остаются после сборок бота и приложений; кэш слоёв сборки
# и тегированные образы (pimusic-apps-*) не трогаем
if [ "$PRUNE_IMAGES" = 1 ]; then
  for eng in podman docker; do
    if command -v $eng >/dev/null; then
      n=$($eng image prune -f | grep -c . || true)
      echo "$eng: удалено висячих образов: $n"
      break
    fi
  done
fi
