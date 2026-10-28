#!/bin/bash
# Хост сборки, после build-windows.sh и build-android-native.sh: манифесты автообновления для выпуска GitHub.
# Приложения берут их из последнего выпуска репозитория (…/releases/latest/download/{latest,android}.json),
# поэтому latest.json и android.json прикладываются к выпуску вместе с установщиком, .sig и APK.
# Контракт файлов — docs/design/APPS_API.md, «Обновления».
#   REPO=owner/name NOTES_FILE=notes.md ./release-manifests.sh
# Переменные: REPO (по умолчанию iKriu/pimusic-server), VERSION (по умолчанию — из src/windows/package.json),
# NOTES или NOTES_FILE (текст «Что нового»), OUT (каталог результата, по умолчанию $PIMUSIC_BUILD_DIR/out/release).
set -euo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
B=${PIMUSIC_BUILD_DIR:-/opt/pimusic-apps-build}
REPO=${REPO:-iKriu/pimusic-server}
V=${VERSION:-$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' $B/src/windows/package.json)}
OUT=${OUT:-$B/out/release}
NOTES=${NOTES:-}
if [ -n "${NOTES_FILE:-}" ]; then NOTES=$(cat "$NOTES_FILE"); fi
BASE_URL="https://github.com/$REPO/releases/download/v$V"
PUB_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$OUT"

# check_sha <файл> — сверка с <файл>.sha256 из сборки
check_sha() {
  (cd "$(dirname "$1")" && sha256sum -c --quiet "$(basename "$1").sha256") || { echo "!! sha256 не совпадает: $1" >&2; exit 1; }
}

# Windows: установщик + подпись minisign (без ключа обновлений сборка .sig не делает)
WIN=$B/out/windows/PiMusic_${V}_x64-setup.exe
[ -f "$WIN" ] || { echo "!! нет $WIN" >&2; exit 1; }
[ -s "$WIN.sig" ] || { echo "!! нет $WIN.sig — сборка без ключа обновлений ($B/keys/updater.key)" >&2; exit 1; }
check_sha "$WIN"
URL="$BASE_URL/$(basename "$WIN")" SIG=$(cat "$WIN.sig") V="$V" NOTES="$NOTES" PUB_DATE="$PUB_DATE" python3 -c '
import json, os
e = os.environ
p = {"url": e["URL"], "signature": e["SIG"].strip()}
print(json.dumps({"version": e["V"], "notes": e["NOTES"], "pub_date": e["PUB_DATE"],
                  "platforms": {"windows-x86_64": p, "windows-x86_64-nsis": p}}, ensure_ascii=False, indent=2))' > "$OUT/latest.json"

# Android: APK той же версии; versionCode — из .buildinfo сборки
APK=$B/out/android-native/PiMusic-$V.apk
[ -f "$APK" ] || { echo "!! нет $APK" >&2; exit 1; }
check_sha "$APK"
VC=$(sed -n 's/^version_code=//p' "$APK.buildinfo" | head -1)
[ -n "$VC" ] || { echo "!! нет version_code в $APK.buildinfo" >&2; exit 1; }
URL="$BASE_URL/$(basename "$APK")" SHA=$(cut -d' ' -f1 "$APK.sha256") SIZE=$(stat -c %s "$APK") VC="$VC" V="$V" \
  NOTES="$NOTES" PUB_DATE="$PUB_DATE" python3 -c '
import json, os
e = os.environ
print(json.dumps({"versionCode": int(e["VC"]), "versionName": e["V"], "url": e["URL"], "sha256": e["SHA"],
                  "size": int(e["SIZE"]), "notes": e["NOTES"], "pub_date": e["PUB_DATE"]}, ensure_ascii=False, indent=2))' > "$OUT/android.json"

cat "$OUT/latest.json" "$OUT/android.json"
echo ">> $OUT: приложить к выпуску v$V вместе с $(basename "$WIN"){,.sig,.sha256} и $(basename "$APK"){,.sha256}"
