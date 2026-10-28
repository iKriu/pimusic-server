#!/bin/bash
# Запускается ВНУТРИ контейнера pimusic-apps-windows (см. build-windows.sh). /src = apps/, /cache, /out.
set -euo pipefail
source /src/build/env.sh
ensure_node
if [ "$(/cache/cargo/bin/rustup --version 2>/dev/null | awk '{print $2}')" != "$RUSTUP_VERSION" ]; then
  echo ">> rustup $RUSTUP_VERSION"
  fetch "https://static.rust-lang.org/rustup/archive/$RUSTUP_VERSION/x86_64-unknown-linux-gnu/rustup-init" "$RUSTUP_SHA256" /tmp/rustup-init
  chmod +x /tmp/rustup-init && /tmp/rustup-init -y --profile minimal --default-toolchain none --no-modify-path && rm /tmp/rustup-init
fi
rustup toolchain install "$RUST_TOOLCHAIN" --profile minimal --target x86_64-pc-windows-msvc
export RUSTUP_TOOLCHAIN=$RUST_TOOLCHAIN
if [ "$(/cache/cargo/bin/cargo-xwin --version 2>/dev/null | awk '{print $2}')" != "$CARGO_XWIN_VERSION" ]; then
  echo ">> cargo-xwin $CARGO_XWIN_VERSION"
  fetch "https://github.com/rust-cross/cargo-xwin/releases/download/v$CARGO_XWIN_VERSION/cargo-xwin-v$CARGO_XWIN_VERSION.x86_64-unknown-linux-musl.tar.gz" "$CARGO_XWIN_SHA256" /tmp/cargo-xwin.tgz
  tar -xzf /tmp/cargo-xwin.tgz -C /cache/cargo/bin && rm /tmp/cargo-xwin.tgz
fi
rustc --version; cargo xwin --version
export XWIN_CACHE_DIR=/cache/xwin XWIN_ARCH=x86_64
export CARGO_TARGET_DIR=/cache/target-windows
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-3}
# /Brepro: lld-link пишет в PE-заголовок хэш вместо текущего времени (cargo-xwin дописывает свои флаги к этим).
# /DEBUG:NONE: без PDB — иначе GUID PDB (в отладочной записи CodeView) меняется от сборки к сборке (временные
# пути объектников rustc) и тянет за собой хэш-время /Brepro: две сборки отличались ровно этими 20 байтами.
export CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS="-C link-arg=/Brepro -C link-arg=/DEBUG:NONE"

cd /src/windows
# иконки: shared/icon/gen-icons.mjs → svg → png (rsvg) → tauri icon (ico/png)
node ../shared/icon/gen-icons.mjs /tmp/icons
rsvg-convert -w 1024 -h 1024 /tmp/icons/icon.svg -o /tmp/icons/icon.png
rsvg-convert -w 64 -h 64 /tmp/icons/tray.svg -o src-tauri/icons/tray.png
npm ci --no-audit --no-fund
npx tauri icon /tmp/icons/icon.png -o src-tauri/icons >/dev/null
rm -rf src-tauri/icons/android src-tauri/icons/ios src-tauri/icons/Square* src-tauri/icons/StoreLogo.png src-tauri/icons/64x64.png 2>/dev/null || true
# встроенный интерфейс (2.0): /src/webui (копия bot/app/static, кладёт build/stamp.sh) → windows/dist-ui
WEBUI_SHA=$(node ../build/prepare-webui.mjs dist-ui)
echo ">> webui $WEBUI_SHA (${WEBUI_COMMIT:-?})"

VER=$(node -p "require('./package.json').version")
B=$CARGO_TARGET_DIR/x86_64-pc-windows-msvc/release
SETUP=PiMusic_${VER}_x64-setup.exe

# Автообновление (tauri-plugin-updater): ключ подписи — /keys/updater.key (+ пароль в /keys/updater.key.password),
# только на сервере, в контейнер — через env (не argv). pubkey: из tauri.conf.json, а если там заглушка —
# из /keys/updater.key.pub. Нет ключа или pubkey — сборка без .sig и без проверки обновлений в приложении.
CONF_PUBKEY=$(node -p "require('./src-tauri/tauri.conf.json').plugins.updater.pubkey")
EXTRA_CONF=/tmp/pimusic-build.conf.json
UPDATER=0
if [ -s /keys/updater.key ]; then
  if [ "$CONF_PUBKEY" != "PIMUSIC_UPDATER_PUBKEY" ]; then
    echo '{}' > $EXTRA_CONF; UPDATER=1
  elif [ -s /keys/updater.key.pub ]; then
    PUBKEY=$(tr -d '\r\n' < /keys/updater.key.pub) node -e \
      'require("fs").writeFileSync(process.argv[1], JSON.stringify({plugins:{updater:{pubkey:process.env.PUBKEY}}}))' $EXTRA_CONF
    UPDATER=1
  fi
fi
if [ $UPDATER = 1 ]; then
  TAURI_SIGNING_PRIVATE_KEY=$(cat /keys/updater.key)
  TAURI_SIGNING_PRIVATE_KEY_PASSWORD=$(cat /keys/updater.key.password 2>/dev/null || true)
  export TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD
else
  echo "!! нет /keys/updater.key или pubkey — сборка БЕЗ автообновления (нет .sig, приложение не проверяет обновления)" >&2
  echo '{"bundle":{"createUpdaterArtifacts":false}}' > $EXTRA_CONF
fi
# Воспроизводимость установщика: NSIS по умолчанию хранит время изменения каждого упакованного файла, а Tauri
# пересоздаёт/патчит их (PiMusic.exe — «bundle type», скрипт и ресурсы) прямо перед makensis, так что touch между
# `build --no-bundle` и `bundle` не помогает. Обёртка makensis выполняет `SetDateSave off` до скрипта: времена файлов
# в установщик не пишутся (при установке у файлов — время установки).
mkdir -p /tmp/nsis-wrap
cat > /tmp/nsis-wrap/makensis <<'EOF'
#!/bin/sh
case " $* " in *" -VERSION "*|*" /VERSION "*) exec /usr/bin/makensis "$@" ;; esac
exec /usr/bin/makensis "-XSetDateSave off" "$@"
EOF
chmod +x /tmp/nsis-wrap/makensis
export PATH=/tmp/nsis-wrap:$PATH
# старая подпись той же версии от прошлой сборки не должна уехать в /out
rm -f $B/bundle/nsis/$SETUP.sig

npx tauri build --runner cargo-xwin --target x86_64-pc-windows-msvc --config $EXTRA_CONF -- --locked
unset TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD
mkdir -p /out
rm -f /out/$SETUP.sig
cp -v $B/bundle/nsis/$SETUP /out/
[ $UPDATER = 1 ] && cp -v $B/bundle/nsis/$SETUP.sig /out/
cp -v $B/PiMusic.exe /out/PiMusic-portable.exe
BUILD_EXTRA_INFO="webui_sha256=$WEBUI_SHA webui_commit=${WEBUI_COMMIT:-unknown}
rust=$RUST_TOOLCHAIN rustup=$RUSTUP_VERSION cargo_xwin=$CARGO_XWIN_VERSION
msvc_crt=$XWIN_CRT_VERSION windows_sdk=$XWIN_SDK_VERSION
tauri_cli=$(npx tauri --version 2>/dev/null) clang=$(clang --version | head -1) nsis=$(makensis -VERSION 2>/dev/null)" \
  write_checksums /out/$SETUP /out/PiMusic-portable.exe
ls -la /out
