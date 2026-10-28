# Образ для кросс-сборки Tauri 2 (Windows x64, MSVC ABI) на Linux: cargo-xwin + NSIS.
# libayatana-appindicator3-1 + заглушка .pc для pkg-config — только чтобы бандлер Tauri не падал на проверке
# трея (фича tray-icon проверяется и при кросс-сборке под Windows).
# Тулчейны (rustup, cargo, node, кэш xwin) лежат в смонтированном /cache, а не в образе.
# Базовый образ — по digest (debian:trixie-slim, linux/amd64); пакеты apt (clang/lld/llvm 19, NSIS 3.11, rsvg 2.60) —
# из snapshot.debian.org на дату APT_SNAPSHOT. Обновление: новый digest (podman images --digests) + новая дата.
FROM docker.io/library/debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ARG APT_SNAPSHOT=20261018T000000Z
RUN sed -i -E "s#http://deb\.debian\.org/(debian|debian-security)#http://snapshot.debian.org/archive/\1/${APT_SNAPSHOT}#" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils git clang lld llvm nsis librsvg2-bin file pkgconf libayatana-appindicator3-1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/lib/x86_64-linux-gnu/pkgconfig \
    && printf 'libdir=/usr/lib/x86_64-linux-gnu\nName: ayatana-appindicator3-0.1\nDescription: stub for tauri bundler\nVersion: 0.5.94\nLibs: -L${libdir} -layatana-appindicator3\n' \
       > /usr/lib/x86_64-linux-gnu/pkgconfig/ayatana-appindicator3-0.1.pc
