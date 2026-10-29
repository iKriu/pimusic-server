#!/bin/bash
# Хост сборки: манифест автообновления нативного клиента Windows для выпуска GitHub. Клиент берёт его из последнего
# выпуска репозитория (…/releases/latest/download/windows-native.json, Update/Updater.cs), поэтому windows-native.json
# и windows-native.json.sig прикладываются к выпуску вместе с установщиком. Контракт — docs/design/APPS_API.md.
#   F=/путь/PiMusic-1.1.0-setup.exe REPO=owner/name NOTES_FILE=notes.md ./release-windows-native.sh
# Переменные: F (установщик из build-windows-native.ps1), REPO (по умолчанию iKriu/pimusic-server), VERSION (по
# умолчанию — из имени файла), NOTES или NOTES_FILE, KEY (закрытый ключ ed25519 в PEM, по умолчанию
# $PIMUSIC_BUILD_DIR/keys/windows-native-update.pem; публичная часть — ManifestPublicKey в Updater.cs),
# OUT (каталог результата, по умолчанию $PIMUSIC_BUILD_DIR/out/release).
# Создать ключ: openssl genpkey -algorithm ed25519 -out key.pem
#   публичный (base64, 32 байта): openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64 -w0
set -euo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
B=${PIMUSIC_BUILD_DIR:-/opt/pimusic-apps-build}
REPO=${REPO:-iKriu/pimusic-server}
KEY=${KEY:-$B/keys/windows-native-update.pem}
OUT=${OUT:-$B/out/release}
F=${F:?укажите F=/путь/PiMusic-<версия>-setup.exe}
NOTES=${NOTES:-}
if [ -n "${NOTES_FILE:-}" ]; then NOTES=$(cat "$NOTES_FILE"); fi
[ -f "$F" ] || { echo "!! нет $F" >&2; exit 1; }
[ -s "$KEY" ] || { echo "!! нет ключа подписи $KEY" >&2; exit 1; }
V=${VERSION:-$(basename "$F" | sed -n 's/^PiMusic-\(.*\)-setup\.exe$/\1/p')}
[ -n "$V" ] || { echo "!! не удалось определить версию из имени $F — задайте VERSION" >&2; exit 1; }
if [ -f "$F.sha256" ]; then
  (cd "$(dirname "$F")" && sha256sum -c --quiet "$(basename "$F").sha256") || { echo "!! sha256 не совпадает: $F" >&2; exit 1; }
fi
mkdir -p "$OUT"
M=$OUT/windows-native.json
URL="https://github.com/$REPO/releases/download/v$V/$(basename "$F")" SHA=$(sha256sum "$F" | cut -d' ' -f1) \
  SIZE=$(stat -c %s "$F") V="$V" NOTES="$NOTES" PUB_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) python3 -c '
import json, os
e = os.environ
print(json.dumps({"version": e["V"], "url": e["URL"], "sha256": e["SHA"], "size": int(e["SIZE"]),
                  "notes": e["NOTES"], "pub_date": e["PUB_DATE"]}, ensure_ascii=False, indent=2))' > "$M"
# подпись над точными байтами файла (base64 от 64 байт) и самопроверка публичной частью того же ключа
openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$M" | base64 -w0 > "$M.sig"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
openssl pkey -in "$KEY" -pubout -out "$TMP/pub.pem"
base64 -d < "$M.sig" > "$TMP/sig"
[ "$(wc -c < "$TMP/sig")" = 64 ] || { echo "!! подпись не получилась" >&2; exit 1; }
openssl pkeyutl -verify -pubin -inkey "$TMP/pub.pem" -rawin -in "$M" -sigfile "$TMP/sig" >/dev/null \
  || { echo "!! самопроверка подписи не прошла" >&2; exit 1; }
echo ">> $M (+ .sig)"; cat "$M"; echo
