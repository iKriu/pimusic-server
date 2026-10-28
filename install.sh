#!/usr/bin/env bash
# =============================================================================
#  PiMusic — установка в одну команду на Linux-сервер (x86_64 или arm64)
#    sudo bash install.sh
#  Нужен Docker с compose v2 или Podman с docker-compose/podman-compose. На Debian/Ubuntu без Docker
#  скрипт поставит его сам (пакеты docker.io). Скрипт идемпотентен: его можно запускать повторно (обновление/починка).
# =============================================================================
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
cd "$PROJECT_DIR"

c_ok="\033[1;32m"; c_warn="\033[1;33m"; c_err="\033[1;31m"; c_b="\033[1m"; c_0="\033[0m"
step() { echo -e "\n${c_b}==> $*${c_0}"; }
ok()   { echo -e "${c_ok}✔${c_0} $*"; }
warn() { echo -e "${c_warn}!${c_0} $*"; }
die()  { echo -e "${c_err}✘ $*${c_0}" >&2; exit 1; }
trap 'die "Ошибка в строке $LINENO: $BASH_COMMAND"' ERR

[[ $EUID -eq 0 ]] || die "Запустите через sudo: sudo bash install.sh"
case "$(uname -m)" in x86_64|aarch64) ;; *) warn "Архитектура $(uname -m) не проверялась (рассчитано на x86_64 и arm64)";; esac

# ----------------------------------------------------------------------------- .env
env_get() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' ; }
env_set() {
  local key="$1" val="$2"
  if grep -qE "^$key=" "$ENV_FILE"; then
    # экранируем для sed
    local esc; esc=$(printf '%s' "$val" | sed -e 's/[\/&|]/\\&/g')
    sed -i "s|^$key=.*|$key=$esc|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}
gen() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-32}" || true; }

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 .env.example .env
  [[ -n "${SUDO_USER:-}" ]] && chown "$SUDO_USER": .env
  die "Создан файл .env — заполните блок «ОБЯЗАТЕЛЬНО» (nano $ENV_FILE) и запустите скрипт ещё раз."
fi
chmod 600 "$ENV_FILE"
sed -i 's/\r$//' "$ENV_FILE"   # на случай, если .env редактировали в Windows

step "Проверяю .env"
missing=()
for k in TELEGRAM_BOT_TOKEN INVITE_CODE JELLYFIN_ADMIN_USER JELLYFIN_ADMIN_PASSWORD SLSK_USERNAME SLSK_PASSWORD; do
  [[ -n "$(env_get "$k")" ]] || missing+=("$k")
done
((${#missing[@]} == 0)) || die "Не заполнены: ${missing[*]} (файл $ENV_FILE)"
[[ -n "$(env_get LASTFM_API_KEY)" ]] || warn "LASTFM_API_KEY пуст — команда /wave работать не будет"
[[ "$(env_get JELLYFIN_ADMIN_USER)" =~ ^[A-Za-z0-9._-]+$ ]] || die "JELLYFIN_ADMIN_USER: только латиница, цифры, . _ -"
# docker compose раскрывает $ в .env, а кавычки/пробелы/# парсятся по-разному — запрещаем их в секретах
for k in TELEGRAM_BOT_TOKEN INVITE_CODE JELLYFIN_ADMIN_PASSWORD SLSK_USERNAME SLSK_PASSWORD SLDL_SLSK_USERNAME \
         SLDL_SLSK_PASSWORD SLSKD_WEB_PASSWORD LASTFM_API_KEY SPOTIFY_CLIENT_ID SPOTIFY_CLIENT_SECRET; do
  v="$(env_get "$k")"
  if [[ "$v" == *'$'* || "$v" == *'"'* || "$v" == *"'"* || "$v" == *'\'* || "$v" == *'`'* || "$v" == *' '* || "$v" == *'#'* ]]; then
    die "$k содержит недопустимые символы (\$ \" ' \\ \` пробел #). Замените значение в .env"
  fi
done
invite="$(env_get INVITE_CODE)"
(( ${#invite} >= 8 )) || warn "INVITE_CODE короче 8 символов — его легко подобрать"

REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
if [[ -z "$REAL_USER" || "$REAL_USER" == root ]] || ! id "$REAL_USER" &>/dev/null; then
  # контейнеры не должны работать от root: заводим системного пользователя
  REAL_USER=pimusic
  id pimusic &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin pimusic
fi
[[ -n "$(env_get PUID)" ]] || env_set PUID "$(id -u "$REAL_USER")"
[[ -n "$(env_get PGID)" ]] || env_set PGID "$(id -g "$REAL_USER")"
[[ -n "$(env_get DATA_DIR)" ]] || env_set DATA_DIR /srv/pimusic
case "$(env_get DATA_DIR)" in
  /|/home|/home/|/etc*|/usr*|/var|/var/|/boot*|/root|/bin*|/sbin*|/lib*|/proc*|/sys*|/dev*) die "DATA_DIR=$(env_get DATA_DIR) — небезопасный путь";;
  /*) ;;
  *) die "DATA_DIR должен быть абсолютным путём";;
esac
if [[ -z "$(env_get PUBLIC_HOST)" ]]; then
  ip_addr=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)
  env_set PUBLIC_HOST "${ip_addr:-$(hostname -I | awk '{print $1}')}"
fi
[[ -n "$(env_get SLSKD_API_KEY)" ]]      || env_set SLSKD_API_KEY "$(gen 40)"
[[ -n "$(env_get SLSKD_WEB_PASSWORD)" ]] || env_set SLSKD_WEB_PASSWORD "$(gen 16)"
[[ -n "$(env_get SLSKD_WEB_USER)" ]]     || env_set SLSKD_WEB_USER admin
if [[ -z "$(env_get SLDL_SLSK_USERNAME)" ]]; then
  env_set SLDL_SLSK_USERNAME "$(env_get SLSK_USERNAME)_pm"
fi
[[ -n "$(env_get SLDL_SLSK_PASSWORD)" ]] || env_set SLDL_SLSK_PASSWORD "$(env_get SLSK_PASSWORD)"
if [[ -z "$(env_get TZ)" ]]; then env_set TZ "$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Moscow)"; fi
chmod 600 "$ENV_FILE"; chown "$REAL_USER": "$ENV_FILE" || true

PUID=$(env_get PUID); PGID=$(env_get PGID); DATA_DIR=$(env_get DATA_DIR); PUBLIC_HOST=$(env_get PUBLIC_HOST)
ok "Пользователь контейнеров: $REAL_USER ($PUID:$PGID), данные: $DATA_DIR, адрес: $PUBLIC_HOST"

# ----------------------------------------------------------------------------- пакеты и Docker
step "Проверяю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt_retry() {
  local i
  for i in 1 2 3 4 5; do
    apt-get -o Acquire::Retries=5 install -y -qq "$@" >/dev/null && return 0
    (( i < 5 )) || break
    sleep 10; apt-get -o Acquire::Retries=5 update -qq || true
  done
  return 1
}
pkgs=(); for c in curl jq; do command -v "$c" &>/dev/null || pkgs+=("$c"); done
if ((${#pkgs[@]})); then
  if command -v apt-get &>/dev/null; then
    for i in 1 2 3; do apt-get -o Acquire::Retries=5 update -qq && break; sleep 10; done
    apt_retry ca-certificates "${pkgs[@]}" || die "Не удалось установить ${pkgs[*]} (apt)"
  elif command -v dnf &>/dev/null; then
    dnf install -y -q "${pkgs[@]}" || die "Не удалось установить ${pkgs[*]} (dnf)"
  else
    die "Установите ${pkgs[*]} и запустите скрипт ещё раз"
  fi
fi
ok "curl и jq есть"

step "Docker / Podman"
# Движок контейнеров: Docker (compose v2 или docker-compose) или Podman (podman-compose / docker-compose через сокет)
if ! command -v docker &>/dev/null && ! command -v podman &>/dev/null; then
  if command -v apt-get &>/dev/null && apt-cache show docker.io &>/dev/null; then
    # Docker из репозитория дистрибутива: Docker Hub для установки не нужен вообще
    apt_retry docker.io docker-compose docker-buildx containerd runc || die "Не удалось установить docker.io"
  else
    die "Не найден Docker или Podman. Установите Docker Engine (https://docs.docker.com/engine/install/) или Podman с podman-compose и запустите скрипт ещё раз"
  fi
fi
COMPOSE=()
if command -v docker &>/dev/null && docker compose version &>/dev/null; then COMPOSE=(docker compose)
elif command -v docker-compose &>/dev/null; then COMPOSE=(docker-compose)
elif command -v podman &>/dev/null && podman compose version &>/dev/null; then COMPOSE=(podman compose)
elif command -v podman-compose &>/dev/null; then COMPOSE=(podman-compose)
elif command -v apt-get &>/dev/null; then
  for p in docker-compose docker-compose-plugin docker-compose-v2; do
    apt-cache show "$p" &>/dev/null && apt_retry "$p" && break
  done
  if docker compose version &>/dev/null; then COMPOSE=(docker compose)
  elif command -v docker-compose &>/dev/null; then COMPOSE=(docker-compose); fi
fi
((${#COMPOSE[@]})) || die "Не найден compose: установите docker compose v2, docker-compose или podman-compose"
compose() { "${COMPOSE[@]}" "$@"; }

# Настройки демона — только для настоящего Docker (у Podman их нет)
if systemctl list-unit-files docker.service &>/dev/null && [[ -n "$(systemctl list-unit-files docker.service --no-legend 2>/dev/null)" ]]; then
  docker buildx version &>/dev/null || { command -v apt-get &>/dev/null && apt_retry docker-buildx; } || warn "buildx не установлен — сборка пойдёт классическим сборщиком"
  [[ "$REAL_USER" == pimusic ]] || ! getent group docker &>/dev/null || usermod -aG docker "$REAL_USER" 2>/dev/null || true
  mkdir -p /etc/docker
  # Меньше параллельных загрузок слоёв и больше попыток — меньше обрывов TLS на медленных каналах
  DAEMON_CFG='{"log-driver":"local","log-opts":{"max-size":"5m","max-file":"2"},"live-restore":true,
               "max-concurrent-downloads":2,"max-concurrent-uploads":1,"max-download-attempts":10}'
  restart_docker=0
  tmp=$(mktemp)
  if [[ -f /etc/docker/daemon.json ]] && jq -e 'type == "object"' /etc/docker/daemon.json >/dev/null 2>&1; then
    jq --argjson add "$DAEMON_CFG" '. + $add' /etc/docker/daemon.json > "$tmp"   # чужие настройки сохраняются
  else
    jq -n --argjson add "$DAEMON_CFG" '$add' > "$tmp"
  fi
  if ! cmp -s "$tmp" /etc/docker/daemon.json 2>/dev/null; then
    mv "$tmp" /etc/docker/daemon.json && chmod 644 /etc/docker/daemon.json && restart_docker=1
  else
    rm -f "$tmp"
  fi
  systemctl enable --now docker >/dev/null
  if (( restart_docker )); then systemctl restart docker; fi
  for _ in $(seq 1 30); do docker info &>/dev/null && break; sleep 2; done
fi
ok "Compose: ${COMPOSE[*]} ($(compose version 2>/dev/null | head -n1))"
export COMPOSE_PARALLEL_LIMIT=1   # compose тянет образы по одному

# Jellyfin следит за медиатекой в реальном времени (inotify): лимиты по умолчанию малы для большой коллекции
cat > /etc/sysctl.d/90-pimusic.conf <<'CONF'
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=1024
CONF
sysctl --system >/dev/null || true

# ----------------------------------------------------------------------------- каталоги
step "Создаю каталоги в $DATA_DIR"
mkdir -p "$DATA_DIR"/{jellyfin/config,jellyfin/cache,slskd,state/home,state/cache} \
         "$DATA_DIR"/media/{library,users,radio,share,incoming/slskd,incoming/work}
chown "$PUID:$PGID" "$DATA_DIR"
for d in jellyfin slskd state media; do
  # рекурсивно — только если владелец не тот (повторный запуск не обходит всю медиатеку)
  [[ "$(stat -c %u "$DATA_DIR/$d")" == "$PUID" ]] || chown -R "$PUID:$PGID" "$DATA_DIR/$d"
done
chmod 750 "$DATA_DIR/state"
for f in "$DATA_DIR/state/jellyfin_api_key" "$DATA_DIR/state/pimusic.db" "$DATA_DIR/state/CREDENTIALS.txt"; do
  [[ -f "$f" ]] && chmod 600 "$f"
done
# проверка: жёсткие ссылки внутри media должны работать
touch "$DATA_DIR/media/library/.hl_test" && ln -f "$DATA_DIR/media/library/.hl_test" "$DATA_DIR/media/users/.hl_test" \
  && ok "Жёсткие ссылки поддерживаются" || warn "Жёсткие ссылки НЕ работают — файлы будут копироваться"
rm -f "$DATA_DIR/media/library/.hl_test" "$DATA_DIR/media/users/.hl_test"

# ----------------------------------------------------------------------------- контейнеры
step "Скачиваю образы и собираю бота (первый раз это 5–25 минут — сборка sockseek из исходников)"
retry() { local n=1 max=$1; shift; until "$@"; do (( n >= max )) && return 1; warn "Попытка $n не удалась, повтор через $((n*20)) с…"; sleep $((n*20)); ((n++)); done; }
for svc in jellyfin slskd; do   # по одному образу, с повторами
  retry 6 compose pull "$svc" || die "Не удалось скачать образ $svc (проверьте доступ к ghcr.io)"
done
retry 3 compose build bot || die "Сборка образа бота не удалась: ${COMPOSE[*]} build bot"
ok "Образы готовы"

step "Запускаю Jellyfin и slskd"
# Пока мастер не пройден, Jellyfin слушает только localhost — никто из сети не перехватит первичную настройку.
# На уже настроенном сервере (есть API-ключ) Jellyfin из сети не отключаем.
if [[ -s "$DATA_DIR/state/jellyfin_api_key" ]]; then
  compose up -d jellyfin slskd
else
  JELLYFIN_BIND=127.0.0.1 compose up -d jellyfin slskd
fi

step "Автоматическая первичная настройка Jellyfin (мастер, API-ключ, библиотеки, радио)"
for attempt in 1 2 3; do
  if compose run --rm --no-deps bot python -m app bootstrap; then ok "Jellyfin настроен"; break; fi
  if [[ $attempt -ge 3 ]]; then
    compose up -d jellyfin >/dev/null 2>&1 || true   # вернуть Jellyfin в сеть
    die "bootstrap не удался — смотрите: ${COMPOSE[*]} logs jellyfin"
  fi
  warn "Повтор через 15 с…"; sleep 15
done

step "Открываю Jellyfin для локальной сети и запускаю Telegram-бота"
compose up -d jellyfin bot

# ждём slskd
for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:5030/health" >/dev/null 2>&1 && break; sleep 3
done
slsk_state="$(curl -fsS -H "X-API-Key: $(env_get SLSKD_API_KEY)" http://127.0.0.1:5030/api/v0/server 2>/dev/null | jq -r '.state // empty' || true)"

# ----------------------------------------------------------------------------- автозапуск
cat > /etc/systemd/system/pimusic.service <<UNIT
[Unit]
Description=PiMusic (Jellyfin + slskd + Telegram bot)
After=docker.service podman.socket network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
${DOCKER_HOST:+Environment=DOCKER_HOST=$DOCKER_HOST}
ExecStart=$(command -v "${COMPOSE[0]}") ${COMPOSE[*]:1} up -d
ExecStop=$(command -v "${COMPOSE[0]}") ${COMPOSE[*]:1} stop
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pimusic.service >/dev/null 2>&1 || true

# ----------------------------------------------------------------------------- итог
BOT_NAME="$(curl -fsS "https://api.telegram.org/bot$(env_get TELEGRAM_BOT_TOKEN)/getMe" 2>/dev/null | jq -r '.result.username // empty' || true)"
JF_PUBLIC="$(env_get JELLYFIN_PUBLIC_URL)"
[ -z "$JF_PUBLIC" ] && case "$(env_get STREAM_PUBLIC_URL)" in https://*) JF_PUBLIC="$(env_get STREAM_PUBLIC_URL)";; esac
JF_PUBLIC="${JF_PUBLIC%/}"
[ -z "$JF_PUBLIC" ] && JF_PUBLIC="http://$PUBLIC_HOST:8096"
CREDS="$DATA_DIR/state/CREDENTIALS.txt"
umask 077
cat > "$CREDS" <<TXT
================ PiMusic готов ================

Jellyfin (веб-плеер и админка)
  URL:    $JF_PUBLIC  (в локальной сети: http://$PUBLIC_HOST:8096)
  Логин:  $(env_get JELLYFIN_ADMIN_USER)
  Пароль: $(env_get JELLYFIN_ADMIN_PASSWORD)
  Мобильные клиенты (Finamp, Symfonium, Jellyfin): сервер $JF_PUBLIC

slskd (веб-панель Soulseek, загрузки)
  URL:    http://$PUBLIC_HOST:5030
  Логин:  $(env_get SLSKD_WEB_USER)
  Пароль: $(env_get SLSKD_WEB_PASSWORD)
  Soulseek-аккаунт: $(env_get SLSK_USERNAME)   (sockseek: $(env_get SLDL_SLSK_USERNAME))
  Статус: ${slsk_state:-неизвестно}

Telegram-бот: ${BOT_NAME:+https://t.me/$BOT_NAME}
  Регистрация друзей: /register $(env_get INVITE_CODE) <логин> <пароль>

Живые потоки «Моя Волна»: http://$PUBLIC_HOST:8097/stream/<логин>/<токен>.mp3 (ссылку выдаёт /mywave)
Интернет-радио: Jellyfin → Live TV → Каналы

Данные:  $DATA_DIR/media  (library — общий кэш, users/<логин> — личные медиатеки)
Логи:    cd $PROJECT_DIR && ${COMPOSE[*]} logs -f bot
Порты для проброса на роутере (по желанию): 50300/tcp (slskd), $(env_get SLDL_LISTEN_PORT)/tcp (sockseek)
================================================
TXT
chmod 600 "$CREDS"; chown "$PUID:$PGID" "$CREDS"
echo; cat "$CREDS"
echo -e "\n${c_ok}Сводка сохранена в $CREDS${c_0}"
