# База — образ Jellyfin с ghcr.io (Debian + jellyfin-ffmpeg). Он уже скачан для сервера, слои общие,
# поэтому сборка бота почти ничего не тянет из реестров. Docker Hub и mcr.microsoft.com не используются:
# MCR в некоторых сетях отдаёт слои со скоростью ~0.2 МБ/с.
ARG BASE_IMAGE=ghcr.io/jellyfin/jellyfin:10.11.11

# ---------------------------------------------------------------- sockseek (бывш. sldl / slsk-batchdl)
# Официальных сборок linux-arm64 нет (только 32-битный linux-arm), поэтому собираем из исходников.
FROM ${BASE_IMAGE} AS sockseek
ARG SOCKSEEK_REF=v3.0.6
ARG TARGETARCH
ARG REQUIRE_SOCKSEEK=0
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 NUGET_XMLDOC_MODE=skip \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 DOTNET_ROOT=/opt/dotnet PATH=/opt/dotnet:$PATH
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# .NET SDK — официальным скриптом с builds.dotnet.microsoft.com (быстрый CDN), с повторами
RUN set -eux; for i in 1 2 3 4 5; do \
      curl -fsSL --retry 5 --retry-delay 5 https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
      && bash /tmp/dotnet-install.sh --channel 10.0 --install-dir /opt/dotnet --no-path && break; sleep 15; done; \
    dotnet --info >/dev/null || echo "!!! .NET SDK не установлен"
WORKDIR /src
RUN set -eux; mkdir -p /out; \
    ARCH="${TARGETARCH:-$(uname -m)}"; \
    case "$ARCH" in arm64|aarch64) RID=linux-arm64;; amd64|x86_64) RID=linux-x64;; arm|armv7l) RID=linux-arm;; *) RID=linux-arm64;; esac; \
    ok=0; for i in 1 2 3; do git clone --depth 1 --branch "${SOCKSEEK_REF}" https://github.com/fiso64/sockseek.git . && ok=1 && break; rm -rf ./* ./.[!.]* || true; sleep 10; done; \
    if [ "$ok" = 1 ] && command -v dotnet >/dev/null; then \
      PROJ="$(ls Sockseek.Cli/*.csproj 2>/dev/null || find . -name '*.Cli.csproj' -o -name 'sldl.csproj' | head -n1)"; \
      built=0; for i in 1 2 3; do \
        { [ ! -f Sockseek.HelpGenerator/Sockseek.HelpGenerator.csproj ] || \
          dotnet restore Sockseek.HelpGenerator/Sockseek.HelpGenerator.csproj --disable-parallel; } && \
        dotnet publish "$PROJ" -c Release -r "$RID" --self-contained true -p:RestoreDisableParallel=true \
          -p:PublishSingleFile=true -p:PublishTrimmed=false -p:OpenApiGenerateDocuments=false \
          -p:InvariantGlobalization=true -p:DebugType=none -o /out && built=1 && break; \
        sleep 15; done; \
      [ "$built" = 1 ] || echo "!!! dotnet publish не удался"; \
    fi; \
    BIN="$(find /out -maxdepth 1 -type f \( -iname 'sockseek' -o -iname 'sldl' -o -iname 'Sockseek.Cli' \) | head -n1)"; \
    [ -n "$BIN" ] || BIN="$(find /out -maxdepth 1 -type f ! -name '*.so' ! -name '*.dbg' ! -name '*.pdb' ! -name '*.json' \
                            -printf '%s %p\n' | sort -rn | head -n1 | cut -d' ' -f2-)"; \
    if [ -n "$BIN" ] && [ "$(basename "$BIN")" != "sockseek" ]; then mv "$BIN" /out/sockseek; fi; \
    [ -f /out/sockseek ] && chmod +x /out/sockseek || true; \
    if [ ! -x /out/sockseek ]; then \
      echo "############ WARNING: sockseek НЕ собран — второй Soulseek-движок отключён ############"; \
      if [ "${REQUIRE_SOCKSEEK}" = "1" ]; then exit 1; fi; \
    fi; \
    ls -la /out

# ---------------------------------------------------------------- бот (Python из Debian + jellyfin-ffmpeg)
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10 \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 HOME=/state/home XDG_CACHE_HOME=/state/cache \
    PATH=/opt/venv/bin:$PATH
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends python3 python3-venv ca-certificates tzdata curl libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv \
 && for t in ffmpeg ffprobe; do \
      if ! command -v $t >/dev/null; then ln -s /usr/lib/jellyfin-ffmpeg/$t /usr/local/bin/$t; fi; done \
 && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame
WORKDIR /opt/pimusic
COPY requirements.txt .
RUN pip install --retries 10 --timeout 60 -r requirements.txt
COPY --from=sockseek /out/ /usr/local/sockseek/
RUN if [ -x /usr/local/sockseek/sockseek ]; then ln -s /usr/local/sockseek/sockseek /usr/local/bin/sockseek; fi
COPY app ./app
EXPOSE 8097
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s CMD curl -fsS http://127.0.0.1:8097/health || exit 1
ENTRYPOINT []
CMD ["python", "-m", "app"]
