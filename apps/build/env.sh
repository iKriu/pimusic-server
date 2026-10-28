# Общие переменные/функции для скриптов внутри контейнеров (source /src/build/env.sh).
# Все внешние загрузки — фиксированные версии с проверкой sha256.
export NODE_VERSION=22.20.0
export NODE_SHA256=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc
export RUSTUP_VERSION=1.29.1
export RUSTUP_SHA256=dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71
export RUST_TOOLCHAIN=1.99.1
export CARGO_XWIN_VERSION=0.23.1
export CARGO_XWIN_SHA256=c492c6dfb7e5ac0eee586b796e0fc950ba13077e7f0bbb046f445d71790d5360
export CMDLINE_TOOLS_ZIP=commandlinetools-linux-13114758_latest.zip
export CMDLINE_TOOLS_SHA256=7ec965280a073311c339e571cd5de778b9975026cfcbe79f2b1cdcb1e15317ee
export ANDROID_PLATFORM="platforms;android-36" ANDROID_BUILD_TOOLS=36.0.0   # android-36 r2; build-tools — и в variables.gradle
# NDK не нужен (нативного кода нет); platform-tools (adb) для сборки не ставим — sdkmanager не умеет закреплять их версию.
export GRADLE_WRAPPER_JAR_SHA256=7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172   # gradle 8.14.3
# MSVC CRT и Windows SDK, которые качает cargo-xwin (иначе — «последние» из манифеста VS 17)
export XWIN_VERSION=17 XWIN_CRT_VERSION=14.44.17.14 XWIN_SDK_VERSION=10.0.26100

export PATH=/cache/node/bin:/cache/cargo/bin:$PATH
export RUSTUP_HOME=/cache/rustup CARGO_HOME=/cache/cargo
export npm_config_cache=/cache/npm

# Воспроизводимость: SOURCE_DATE_EPOCH (время последнего коммита apps/) приходит из build/source.env
# (пишет build/stamp.sh на ПК перед заливкой) через -e у podman run. Без него сборка идёт, но помечается.
export TZ=UTC LC_ALL=C.UTF-8
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  echo "!! SOURCE_DATE_EPOCH не задан (нет build/source.env) — сборка невоспроизводима" >&2
fi

# fetch <url> <sha256> <file>
fetch() {
  curl -fsSL "$1" -o "$3"
  echo "$2  $3" | sha256sum -c - >/dev/null || { echo "!! sha256 mismatch: $1" >&2; rm -f "$3"; exit 1; }
}
ensure_node() {
  if [ "$(/cache/node/bin/node --version 2>/dev/null)" != "v$NODE_VERSION" ]; then
    echo ">> node $NODE_VERSION"
    fetch "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" "$NODE_SHA256" /tmp/node.tar.xz
    rm -rf /cache/node && mkdir -p /cache/node && tar -xJf /tmp/node.tar.xz -C /cache/node --strip-components=1 && rm /tmp/node.tar.xz
  fi
  node --version
}

# write_checksums <файл...> — <файл>.sha256 (формат sha256sum) и <файл>.buildinfo рядом с артефактом
write_checksums() {
  local f
  for f in "$@"; do
    (cd "$(dirname "$f")" && sha256sum "$(basename "$f")" > "$(basename "$f").sha256")
    {
      echo "artifact=$(basename "$f")"
      echo "sha256=$(cut -d' ' -f1 "$f.sha256")"
      echo "source_commit=${SOURCE_COMMIT:-unknown}"
      echo "source_date_epoch=${SOURCE_DATE_EPOCH:-unset}"
      echo "builder_image=${BUILDER_IMAGE:-unknown}"
      echo "node=$NODE_VERSION"
      if [ -n "${BUILD_EXTRA_INFO:-}" ]; then printf '%s\n' "$BUILD_EXTRA_INFO"; fi
      # ревизию платформы Android sdkmanager закрепить не умеет (ставит последнюю) — хотя бы записываем её
      local plat=${ANDROID_HOME:-}/platforms/${ANDROID_PLATFORM#platforms;}/source.properties
      if [ -n "${ANDROID_HOME:-}" ] && [ -f "$plat" ]; then
        echo "android_platform_revision=$(sed -n 's/^Pkg.Revision=//p' "$plat" | head -1)"
      fi
    } > "$f.buildinfo"
    cat "$f.sha256"
  done
}
