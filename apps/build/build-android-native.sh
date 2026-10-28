#!/bin/bash
# Хост сборки (Linux + Podman): собрать APK PiMusic (apps/android-native) и прогнать JVM-тесты.
#   ./build-android-native.sh            → $PIMUSIC_BUILD_DIR/out/android-native/PiMusic-<версия>.apk (+ .sha256, .buildinfo)
#   TASKS="testDebugUnitTest" ./build-android-native.sh     — только тесты
# Каталог сборки PIMUSIC_BUILD_DIR (по умолчанию /opt/pimusic-apps-build): src — копия apps/, cache — SDK и кэш Gradle,
# out — результаты, keys — ключ подписи (создаётся при первой сборке; храните копию, в репозиторий не кладётся).
# PROXY=http://host:port — если наружу только через HTTP(S)-прокси.
set -euo pipefail
B=${PIMUSIC_BUILD_DIR:-/opt/pimusic-apps-build}
PROXY=${PROXY:-}
IMG=pimusic-apps-android:$(sha256sum $B/src/build/android.Containerfile | cut -c1-12)
podman image exists $IMG || podman build --pull=missing -t $IMG -f $B/src/build/android.Containerfile $B/src/build
SOURCE_COMMIT=unknown SOURCE_DATE_EPOCH=
[ -f $B/src/build/source.env ] && . $B/src/build/source.env
mkdir -p $B/cache/android $B/out/android-native $B/keys
podman run --rm --name pimusic-apps-android-native-build --cpus=3 --memory=6g \
  ${PROXY:+-e HTTP_PROXY=$PROXY -e HTTPS_PROXY=$PROXY -e http_proxy=$PROXY -e https_proxy=$PROXY -e NO_PROXY=localhost,127.0.0.1} \
  -e SOURCE_COMMIT="$SOURCE_COMMIT" -e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" -e TASKS="${TASKS:-}" -e SCREENSHOTS="${SCREENSHOTS:-}" \
  -e BUILDER_IMAGE="$IMG $(podman image inspect $IMG --format '{{.Id}}' | cut -c1-12)" \
  -v $B/src:/src:z -v $B/cache/android:/cache:z -v $B/out/android-native:/out:z -v $B/keys:/keys:z \
  $IMG bash /src/build/android-native-inner.sh
