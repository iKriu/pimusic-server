#!/bin/bash
# Хост сборки (Linux + Podman): собрать установщик Windows. Результат: $PIMUSIC_BUILD_DIR/out/windows/
# Каталог сборки PIMUSIC_BUILD_DIR (по умолчанию /opt/pimusic-apps-build), PROXY — как в build-android-native.sh.
set -euo pipefail
B=${PIMUSIC_BUILD_DIR:-/opt/pimusic-apps-build}
PROXY=${PROXY:-}
# образ помечается хэшем Containerfile: правка Containerfile → новый образ, старый тег не переиспользуется
IMG=pimusic-apps-windows:$(sha256sum $B/src/build/windows.Containerfile | cut -c1-12)
podman image exists $IMG || podman build --pull=missing -t $IMG -f $B/src/build/windows.Containerfile $B/src/build
# коммит и SOURCE_DATE_EPOCH (build/stamp.sh на ПК)
SOURCE_COMMIT=unknown SOURCE_DATE_EPOCH= WEBUI_COMMIT=unknown
[ -f $B/src/build/source.env ] && . $B/src/build/source.env
mkdir -p $B/cache/win $B/out/windows $B/keys
podman run --rm --name pimusic-apps-windows-build --cpus=3 --memory=5g \
  ${PROXY:+-e HTTP_PROXY=$PROXY -e HTTPS_PROXY=$PROXY -e http_proxy=$PROXY -e https_proxy=$PROXY -e NO_PROXY=localhost,127.0.0.1} \
  -e SOURCE_COMMIT="$SOURCE_COMMIT" -e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" -e WEBUI_COMMIT="$WEBUI_COMMIT" \
  -e BUILDER_IMAGE="$IMG $(podman image inspect $IMG --format '{{.Id}}' | cut -c1-12)" \
  -v $B/src:/src:z -v $B/cache/win:/cache:z -v $B/out/windows:/out:z -v $B/keys:/keys:ro,z \
  $IMG bash /src/build/windows-inner.sh
