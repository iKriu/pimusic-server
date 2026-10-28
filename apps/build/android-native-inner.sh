#!/bin/bash
# Запускается ВНУТРИ контейнера pimusic-apps-android (см. build-android-native.sh). /src = apps/, /cache, /out, /keys.
# Node не нужен: иконки (векторные drawable) уже лежат в android-native/app/src/main/res.
set -euo pipefail
source /src/build/env.sh
export ANDROID_HOME=/cache/android-sdk ANDROID_SDK_ROOT=/cache/android-sdk GRADLE_USER_HOME=/cache/gradle
export PATH=$ANDROID_HOME/cmdline-tools/latest/bin:$PATH
if [ ! -x $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager ]; then
  echo ">> Android cmdline-tools ($CMDLINE_TOOLS_ZIP)"
  mkdir -p $ANDROID_HOME/cmdline-tools
  fetch https://dl.google.com/android/repository/$CMDLINE_TOOLS_ZIP "$CMDLINE_TOOLS_SHA256" /tmp/clt.zip
  unzip -q /tmp/clt.zip -d /tmp/clt && mv /tmp/clt/cmdline-tools $ANDROID_HOME/cmdline-tools/latest && rm -rf /tmp/clt /tmp/clt.zip
fi
PROXY_ARGS=""
if [ -n "${HTTPS_PROXY:-}" ]; then
  PH=$(echo "$HTTPS_PROXY" | sed -E 's#^https?://##; s#:.*##'); PP=$(echo "$HTTPS_PROXY" | sed -E 's#.*:([0-9]+)/?$#\1#')
  PROXY_ARGS="--proxy=http --proxy_host=$PH --proxy_port=$PP"
  mkdir -p $GRADLE_USER_HOME
  printf 'systemProp.http.proxyHost=%s\nsystemProp.http.proxyPort=%s\nsystemProp.https.proxyHost=%s\nsystemProp.https.proxyPort=%s\nsystemProp.http.nonProxyHosts=localhost|127.0.0.1\norg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8\norg.gradle.workers.max=3\norg.gradle.daemon=false\n' $PH $PP $PH $PP > $GRADLE_USER_HOME/gradle.properties
fi
yes | sdkmanager $PROXY_ARGS --licenses >/dev/null || true
sdkmanager $PROXY_ARGS --install "$ANDROID_PLATFORM" "build-tools;$ANDROID_BUILD_TOOLS" | grep -v "^\[" || true

# ключ подписи (один раз; хранится только в /keys = $PIMUSIC_BUILD_DIR/keys на хосте, не в репозитории).
# Сохраните копию: обновление APK поверх установленного возможно только тем же ключом.
if [ ! -f /keys/pimusic-release.properties ] && [ -z "${TASKS:-}${SCREENSHOTS:-}" ]; then
  echo ">> генерирую ключ подписи /keys/pimusic-release.jks"
  PASS=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  keytool -genkeypair -keystore /keys/pimusic-release.jks -storetype PKCS12 -alias pimusic -keyalg RSA -keysize 3072     -validity 10000 -storepass "$PASS" -keypass "$PASS" -dname "CN=PiMusic, O=PiMusic" >/dev/null 2>&1
  ( umask 077; printf 'storeFile=/keys/pimusic-release.jks
storePassword=%s
keyAlias=pimusic
keyPassword=%s
' "$PASS" "$PASS" > /keys/pimusic-release.properties )
  chmod 600 /keys/pimusic-release.*
  unset PASS
fi

cd /src/android-native
chmod +x gradlew
echo "$GRADLE_WRAPPER_JAR_SHA256  gradle/wrapper/gradle-wrapper.jar" | sha256sum -c - >/dev/null \
  || { echo "!! gradle-wrapper.jar не совпадает с официальным" >&2; exit 1; }

if [ -n "${TASKS:-}" ]; then
  ./gradlew --no-daemon $TASKS
  exit 0
fi
# скриншоты всех экранов (SCREENSHOTS=1): Roborazzi на Robolectric → /out/shots/<конфигурация>/<экран>.png
# Каждая конфигурация — отдельная JVM (куча); нативная графика Robolectric изредка падает при старте JVM
# («JniConstants … Class not found: java/nio/…») — такую конфигурацию повторяем.
if [ -n "${SCREENSHOTS:-}" ]; then
  rm -rf app/build/shots
  N=$(grep -c '@Test fun s' app/src/test/java/io/github/ikriu/pimusic/shots/ScreenshotTest.kt)
  for size in 360x800 800x360 1280x800; do for theme in light dark; do for font in 1.0 1.3; do
    cfg="$size-$theme-font$font"
    for try in 1 2 3; do
      ./gradlew --no-daemon -q testDebugUnitTest -Pscreenshots=true -Pshotcfg=$cfg --tests 'io.github.ikriu.pimusic.shots.ScreenshotTest' >/tmp/shots-$cfg.log 2>&1 || true
      got=$( (ls app/build/shots/$cfg/*.png 2>/dev/null || true) | wc -l)
      [ "$got" -ge "$N" ] && break
      echo ">> $cfg: $got из $N, повтор ($try)"
    done
    echo "$cfg: $( (ls app/build/shots/$cfg/*.png 2>/dev/null || true) | wc -l) из $N"
  done; done; done
  rm -rf /out/shots && mkdir -p /out && cp -r app/build/shots /out/shots
  exit 0
fi
# JVM-тесты (логика + Robolectric/Compose UI) — на debug-варианте (ui-test-manifest), APK — release
./gradlew --no-daemon testDebugUnitTest assembleRelease
APK=app/build/outputs/apk/release/app-release.apk
BT=$ANDROID_HOME/build-tools/$ANDROID_BUILD_TOOLS
VER=$(grep -m1 'versionName = ' app/build.gradle.kts | cut -d'"' -f2)
OUT=/out/PiMusic-$VER.apk
mkdir -p /out
cp -v $APK $OUT
$BT/apksigner verify --verbose --print-certs $OUT | grep -E "Verified|Signer #1 certificate (DN|SHA-256)" || true
$BT/aapt2 dump badging $OUT | grep -E "^package|sdkVersion|targetSdk|application-label:|uses-permission" || true
BUILD_EXTRA_INFO="version_code=$(grep -m1 'versionCode = ' app/build.gradle.kts | sed -E 's/[^0-9]*([0-9]+).*/\1/')
java=$(java -version 2>&1 | head -1)
android_platform=$ANDROID_PLATFORM build_tools=$ANDROID_BUILD_TOOLS cmdline_tools=$CMDLINE_TOOLS_ZIP
gradle=$(grep -m1 distributionUrl gradle/wrapper/gradle-wrapper.properties | sed -E 's#.*/##')
signer_sha256=$($BT/apksigner verify --print-certs $OUT | sed -n 's/.*certificate SHA-256 digest: //p' | head -1)" \
  write_checksums $OUT
# отчёт тестов — рядом (для проверки)
rm -rf /out/tests && cp -r app/build/test-results/testDebugUnitTest /out/tests 2>/dev/null || true
ls -la /out
