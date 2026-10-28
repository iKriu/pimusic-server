# PiMusic — приложения Windows и Android

```
apps/
  shared/bridge.js        мост window.PiMusicNative (внедряется в каждую страницу до её скриптов)
  shared/setup/           страница выбора сервера / ошибки подключения (встроена в приложение Windows)
  shared/icon/gen-icons.mjs  генератор иконок (M3E «печенька» 10 волн + волна-эквалайзер, #F5A524 на #17130E)
  windows/                Tauri 2 (WebView2), интерфейс Mini App встроен в приложение, установщик NSIS
  android-native/         Kotlin + Jetpack Compose (M3 Expressive) + Media3, интерфейс в APK
  build/                  Containerfile'ы и скрипты сборки (Linux-хост с Podman)
```

Оба приложения берут с сервера только API, обложки и потоки. **Адреса сервера по умолчанию нет**: при первом запуске
пользователь вводит адрес своего сервера PiMusic (`https://music.example.com` или в локальной сети
`http://192.168.1.20:8097`; `http://` — только для адресов локальной сети). Меняется позже: Windows — трей →
«Сменить сервер…», Android — экран входа или «Настройки → Сервер».

## Мост `window.PiMusicNative` (для веб-приложения)

Веб-приложение **не обязано** знать о мосте — без изменений оно уже работает в оболочках:

* платформа определяется в `device.js`: Windows — есть `window.__TAURI_INTERNALS__`; вход по deep-link (`platform: "windows" | "android"`), токен в localStorage;
* ссылки на чужой origin (`<a href>` и `window.open`) мост перехватывает и открывает в системе;
  `https://t.me/<бот>?start=…` открывается **приложением Telegram** (`tg://resolve?domain=…&start=…`), если оно
  установлено, иначе — в браузере. Кнопка «Войти через Telegram» работает как есть;
* мост подслушивает `navigator.mediaSession` (`metadata`, `playbackState`, `setActionHandler`, `setPositionState`) и
  передаёт состояние оболочке (трей и уведомление Windows, уведомление/MediaSession Android), а команды оболочки
  (медиаклавиши, трей, кнопки уведомления, гарнитура, потеря аудиофокуса, отключение наушников) вызывает через те же
  обработчики `mediaSession`, которые веб-приложение уже ставит в `player.js`;
* на Android веб-страница рисуется под системными панелями (edge-to-edge); отступы приходят через
  `env(safe-area-inset-*)` (новые WebView) или отступы самого WebView (старые) — `tokens.css` это уже учитывает.

Что можно добавить в веб-приложение (по желанию):

```js
const N = window.PiMusicNative;            // undefined в браузере / Telegram — проверять всегда
if (N) {
  N.platform;        // "windows" | "android"
  N.shell;           // "tauri" | "capacitor"
  N.version;         // версия оболочки, напр. "1.0.0"
  N.serverUrl;       // адрес сервера на момент открытия окна
  N.features;        // { tray, closeToTray, autostart, notifications, globalMediaKeys, smtc, serverSettings } (Windows)
                     // { backgroundAudio, mediaNotification, audioFocus: false, backButton, serverSettings, notifications } (Android)
  N.media;           // последнее состояние, отправленное оболочке: {title, artist, album, artwork, state, duration, position, rate}

  await N.getInfo();                 // {platform, shell, version, serverUrl, …}
  await N.getSettings();             // Windows: {serverUrl, defaultServerUrl, closeToTray, notifications, mediaKeys, autostart}
                                     // Android: {serverUrl, defaultServerUrl}
  await N.setSettings({ closeToTray: true, notifications: false, mediaKeys: false, autostart: true }); // Windows
  await N.openServerSettings();      // показать страницу выбора сервера (пункт «Сервер…» в Настройках)
  await N.openExternal(url);         // открыть ссылку в системе (t.me → Telegram)
  await N.notify("Заголовок", "Текст"); // системное уведомление
  // Windows (features.updates): свой диалог обновления M3 — см. docs/design/APPS_API.md, «Обновления»
  N.on("update", (u) => {/* {version, currentVersion, notes, date, manual} */ return true; }); // подписка = свой диалог
  await N.checkUpdate(); await N.installUpdate(); await N.dismissUpdate();
  N.on("updateProgress", ({ downloaded, total, percent }) => {}); N.on("updateError", ({ message }) => {});
  N.call(method, args);              // низкоуровневый вызов (то же, что выше)

  // События оболочки. Слушатель может вернуть true — «обработано», тогда мост ничего не делает сам.
  const off = N.on("media", ({ action, seekTime }) => { /* play|pause|playpause|nexttrack|previoustrack|stop|seekto|seekforward|seekbackward */ });
  N.on("back", () => { if (sheetOpen) { closeSheet(); return true; } });   // Android «Назад»: true — поглотить
  N.on("visibility", ({ visible }) => {});  // окно показано/скрыто в трей (Windows), приложение на экране/в фоне (Android)
  N.on("settings", (s) => {});              // настройки изменились (трей Windows)
  N.on("ready", ({ platform, version }) => {});
  // те же события — DOM: window.addEventListener("pimusic-native:back", e => e.preventDefault())
}
```

Порядок обработки `media`: сначала слушатели `on("media")`/`pimusic-native:media`; если никто не вернул true —
вызывается обработчик `navigator.mediaSession` для этого действия (`playpause` → `play`/`pause` по `playbackState`).
На Windows двойное срабатывание (SMTC WebView2 + глобальная клавиша) отсекается окном 600 мс.

Рекомендации для веб-агента:
1. «Назад» на Android: закрывать листы/диалоги/полноэкранный плеер по `on("back")` (вернуть true), иначе мост делает
   `history.back()` WebView, а на первой странице сворачивает приложение (музыка продолжает играть).
2. В «Настройках» при наличии моста показать пункт «Сервер: <адрес>» → `openServerSettings()`, а на Windows —
   переключатели «Сворачивать в трей», «Автозапуск», «Уведомления о треке», «Перехватывать медиаклавиши»
   (`getSettings`/`setSettings`, событие `settings`).
3. Виджет Telegram Login в оболочках не показывать (его всплывающее окно уходит во внешний браузер) — только deep-link.
4. Service worker в оболочках не мешает, но регистрировать его там не обязательно (`platform()` ≠ `pwa`).
5. Обложки для уведомления Android скачиваются нативно без заголовка Authorization (только cookies WebView) —
   если `/api/cover` требует Bearer, уведомление будет без обложки (не ошибка).

## Windows (Tauri 2)

**С 2.0 интерфейс встроен в приложение.** `bot/app/static` (Mini App) при сборке копируется в `windows/dist-ui`
(`build/prepare-webui.mjs`: `index.html` без telegram-web-app.js, `static/`, `setup/`, `webui.json` с sha256
дерева) и открывается как `http://tauri.localhost/index.html`; с сервера — только API, обложки и поток. Обновить
интерфейс = выпустить новую версию приложения (автообновление), выкладка бота его не меняет.

* Адрес API: оболочка до загрузки модулей ставит `window.PIMUSIC_API_BASE` = origin настроенного сервера
  (например, `https://music.example.com`); первый запуск — страница выбора сервера (`setup/`), адреса по умолчанию нет. Смена сервера перезапускает
  приложение: CSP (`connect-src`/`img-src`/`media-src` = сервер + `http://pimusic.localhost`) и каталог кэша
  задаются при запуске. Контракт для фронтенда и бэкенда — `docs/design/APPS_API.md`, «Кэш и офлайн (Windows)».
* Кэш (`src-tauri/src/cache.rs`, протокол `http://pimusic.localhost/stream/<id>?src=…`): текущий и следующий трек
  качаются **целиком** в кэш потока (LRU, 1 ГиБ по умолчанию), играют с диска без подгрузок; скачанные треки
  («офлайн») — полный файл до ручного удаления. `%LOCALAPPDATA%\io.github.ikriu.pimusic\media\<сервер>\`.
* Окно показывает только встроенные страницы; любые другие адреса (включая страницы сервера) — во внешнем браузере.
  Мост и capability — только для `http://tauri.localhost` (удалённых origin'ов в capability больше нет).
* Одно окно (повторный запуск показывает его), размер/положение запоминаются (`tauri-plugin-window-state`).
* Трей: левый клик — показать/скрыть; меню: Показать/Скрыть окно · Играть/Пауза · Следующий · Предыдущий ·
  «Сворачивать в трей при закрытии» (вкл.) · «Запускать при входе в Windows» (выкл.; запуск свёрнутым, `--minimized`) ·
  «Уведомлять о смене трека» (вкл.; только когда окно скрыто) · «Перехватывать медиаклавиши» (выкл.) ·
  Сменить сервер… · Перезагрузить · Выход. Подсказка трея — текущий трек.
* Медиаклавиши и системная панель мультимедиа (SMTC, оверлей громкости Windows) — через Media Session самого
  WebView2: окно запускается с `--enable-features=HardwareMediaKeyHandling`. Если на конкретной машине клавиши не
  доходят, включить «Перехватывать медиаклавиши» (глобальные RegisterHotKey для Play/Pause/Next/Prev/Stop —
  перехватывает их и у других плееров, поэтому по умолчанию выключено).
* Заголовок окна следует теме (`<meta name="theme-color">` → светлая/тёмная рамка).
* Безопасность моста: команда `pimusic_native` проверяет origin вызывающей страницы — только встроенные страницы
  `http://tauri.localhost`; CSP без inline-скриптов и без внешних скриптов (шрифты — Google Fonts). Протокол кэша
  качает только с настроенного сервера (`src` другого origin — 403).
* Внешние ссылки — в браузере по умолчанию, t.me — в Telegram Desktop (если зарегистрирован протокол `tg:`).
* Настройки: `%APPDATA%\io.github.ikriu.pimusic\settings.json`. Установщик NSIS — для текущего пользователя, без прав
  администратора; WebView2 Runtime докачивается установщиком при отсутствии (в Windows 11 он уже есть).

## Android (`android-native/`)

Kotlin + Jetpack Compose (Material 3 Expressive), интерфейс в APK, с сервера — только API и потоки. Архитектура,
экраны, кэш и соответствие API — `docs/design/ANDROID_NATIVE.md`. Пакет `io.github.ikriu.pimusic`, minSdk 26
(Android 8), targetSdk 36. Версия — `versionName`/`versionCode` в `android-native/app/build.gradle.kts`
(`versionCode` = major·10000 + minor·100 + patch, увеличивать на каждый выпуск — по нему приложение видит обновление).

* Воспроизведение — Media3 ExoPlayer + MediaSessionService (уведомление, экран блокировки, гарнитура/Bluetooth,
  аудиофокус, пауза при отключении наушников); музыка продолжает играть после смахивания из «Недавних».
* Кэш потока (`cacheDir/media`, LRU, лимит в Настройках, по умолчанию 2 ГБ) — трек и 2 следующих качаются целиком;
  «Скачанное» (`filesDir/offline`) — DownloadManager, не вытесняется, удаляется только пользователем; офлайн-режим.
* Эквалайзер — свой AudioProcessor (10 биквадов RBJ, как Web Audio), профили/пресеты — те же ключи `eq.*`, что Mini App.
* Журнал: `adb logcat -s PiMusic`.
* Версии закреплены в `android-native/gradle/libs.versions.toml`; Gradle 8.14.3 (wrapper с `distributionSha256Sum`, jar
  сверяется в скрипте). Зависимости проверяются по `android-native/gradle/verification-metadata.xml` (sha256). После
  смены версий: `TASKS="--write-verification-metadata sha256 testDebugUnitTest assembleRelease" build-android-native.sh`
  на чистом каталоге (`rm -rf android-native/{app/build,build,.gradle}`), затем забрать файл в репозиторий.

## Автообновление

Обновления приходят из **выпусков GitHub** этого репозитория (`iKriu/pimusic-server`). Приложения читают манифесты
последнего выпуска — `https://github.com/<репозиторий>/releases/latest/download/latest.json` (Windows) и
`…/android.json` (Android); их пишет `build/release-manifests.sh`, контракт — `docs/design/APPS_API.md`, «Обновления».
**В своём форке** поменяйте репозиторий: `plugins.updater.endpoints` в `windows/src-tauri/tauri.conf.json` и
`ServerUrl.UPDATE_REPO` в `android-native/.../core/ServerUrl.kt` (и свои ключи подписи, см. ниже).

* **Windows** (`tauri-plugin-updater`): проверка через минуту после запуска, затем каждые 6 часов и по пункту трея
  «Проверить обновления». Установщик проверяется по подписи minisign (pubkey в `tauri.conf.json`), ставится NSIS в
  режиме passive, приложение закрывается на время установки и запускается снова. Если веб-интерфейс подписался на
  событие моста `update` и окно на экране — диалог M3 рисует веб-интерфейс, иначе — системный диалог Windows.
* **Android**: при запуске, не чаще раза в сутки, и по кнопке в Настройках; если `versionCode` больше — диалог M3,
  загрузка APK в кэш с прогрессом, проверка sha256, установка через PackageInstaller. Только `https://` и только
  github.com и его хранилища файлов (каждое перенаправление проверяется). APK должен быть подписан тем же ключом.

### Ключи подписи

Ключи живут **только на хосте сборки** в `$PIMUSIC_BUILD_DIR/keys` (по умолчанию `/opt/pimusic-apps-build/keys`) и в
репозиторий не попадают. **Сохраните их копию**: потеряв ключ, установленные копии больше не обновятся (только
переустановкой вручную).

* **Android** — `pimusic-release.jks` + `pimusic-release.properties`: создаются автоматически при первой сборке
  `build-android-native.sh`.
* **Windows** (обновления, minisign) — создать один раз:

```bash
B=${PIMUSIC_BUILD_DIR:-/opt/pimusic-apps-build}
IMG=$(podman images --format '{{.Repository}}:{{.Tag}}' | grep -m1 '^localhost/pimusic-apps-windows:')   # после первой build-windows.sh
( umask 077; head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32 > $B/keys/updater.key.password )
podman run --rm -v $B/src:/src:z -v $B/cache/win:/cache:z -v $B/keys:/keys:z \
  $IMG bash -c 'source /src/build/env.sh && cd /src/windows && npm ci --no-audit --no-fund >/dev/null &&
    npx tauri signer generate --ci -w /keys/updater.key -p "$(cat /keys/updater.key.password)"'
chmod 600 $B/keys/updater.key $B/keys/updater.key.password
cat $B/keys/updater.key.pub     # публичный ключ → windows/src-tauri/tauri.conf.json, plugins.updater.pubkey
```

Если в `tauri.conf.json` стоит заглушка `PIMUSIC_UPDATER_PUBKEY`, `windows-inner.sh` берёт публичный ключ из
`/keys/updater.key.pub`; без ключа сборка идёт без `.sig`, и приложение не проверяет обновления.

## Установка на Windows: SmartScreen

Установщик не подписан сертификатом (платная подпись не планируется), поэтому при первом запуске скачанного из
браузера `PiMusic_<версия>_x64-setup.exe` Windows показывает синее окно «Система Windows защитила ваш компьютер»:

1. Нажать **Подробнее** (ссылка под текстом).
2. Появится «Издатель: Неизвестный издатель» и кнопка **Выполнить в любом случае** — нажать её.
3. Установщик ставит PiMusic для текущего пользователя, без прав администратора.

Если кнопки нет — файл заблокирован политикой (корпоративный ПК) или Smart App Control: Свойства файла →
«Разблокировать» → OK, затем запустить снова. Автообновления это окно не показывают (установщик качает само
приложение, у файла нет отметки «из интернета»). Текст для описания выпуска (`gh release create … --notes-file`):

```markdown
### Установка
**Windows:** скачайте `PiMusic_<версия>_x64-setup.exe` и запустите. Установщик не подписан, поэтому Windows покажет
«Система Windows защитила ваш компьютер» — нажмите **Подробнее → Выполнить в любом случае**. Права администратора не
нужны. Дальше приложение обновляется само.
**Android:** скачайте `PiMusic-<версия>.apk`, откройте и разрешите установку из браузера/файлового менеджера.
Дальше приложение предлагает обновления само.
Контрольные суммы — файлы `*.sha256`.
```

## Сборка (Linux-хост, Podman)

Сборка идёт в одноразовых контейнерах на любом Linux-хосте с Podman (Node/Rust/JDK ставить не нужно). Каталог сборки
`PIMUSIC_BUILD_DIR` (по умолчанию `/opt/pimusic-apps-build`): `src` — копия папки `apps/` (+ `webui`), `cache` —
тулчейны/SDK/кэши, `out` — результаты, `keys` — ключи подписи. Если наружу только через HTTP(S)-прокси — `PROXY=http://host:port`.

```bash
# 1) на машине с репозиторием: отметить коммит и скопировать интерфейс (apps/webui ← bot/app/static)
cd apps
bash build/stamp.sh
tar --exclude=node_modules --exclude=dist --exclude=windows/dist-ui --exclude=android-native/app/build \
    --exclude=android-native/.gradle -cf /tmp/apps.tar shared build windows android-native webui
# 2) на хосте сборки (если это другая машина — сначала scp /tmp/apps.tar туда)
B=${PIMUSIC_BUILD_DIR:-/opt/pimusic-apps-build}
mkdir -p $B/src && tar -xf /tmp/apps.tar -C $B/src
$B/src/build/build-android-native.sh   # JVM-тесты + APK → $B/out/android-native/PiMusic-<версия>.apk (+ .sha256, .buildinfo)
$B/src/build/build-windows.sh          # → $B/out/windows/PiMusic_<версия>_x64-setup.exe (+ .sig), PiMusic-portable.exe
TASKS="testDebugUnitTest" $B/src/build/build-android-native.sh   # только тесты Android
SCREENSHOTS=1 $B/src/build/build-android-native.sh               # скриншоты всех экранов → out/android-native/shots/
# 3) выпуск: манифесты обновлений → $B/out/release/{latest,android}.json
REPO=owner/name NOTES_FILE=notes.md $B/src/build/release-manifests.sh
```

К выпуску GitHub (`gh release create v<версия>`) прикладываются: установщик Windows + `.sig` + `.sha256`
(+ `PiMusic-portable.exe`), APK + `.sha256`, `.buildinfo`, `latest.json`, `android.json`.

* `build-android-native.sh` — образ `pimusic-apps-android` (Temurin JDK 21), в `/cache`: Android SDK
  (platform 36, build-tools 36.0.0), Gradle. Release APK без ключа не собирается (ключ создаётся при первой сборке).
* `build-windows.sh` — образ `pimusic-apps-windows` (Debian trixie: clang/lld/llvm, NSIS, rsvg), в `/cache`: rustup,
  cargo-xwin (MSVC CRT + Windows SDK, лицензия Microsoft принимается автоматически при скачивании), Node 22; сборка
  `tauri build --runner cargo-xwin --target x86_64-pc-windows-msvc`. Установщик **без подписи кода** (SmartScreen
  покажет «Неизвестный издатель», см. выше).
* Сборка прямо на Windows (альтернатива): Rust (MSVC), Node 22, затем в `apps/windows`:
  `npm ci && npm run icons && npm run build` (`build` сначала собирает `dist-ui` из `../../bot/app/static`).
* Версия Windows — `windows/package.json` + `windows/src-tauri/Cargo.toml` (`tauri.conf.json` берёт из package.json).
* Освободить место: `rm -rf $B/cache/*/{gradle,target-windows,xwin/dl}`; `podman images 'pimusic-apps-*'` →
  `podman rmi <старые теги>`.

### Воспроизводимость

Что закреплено (где менять):

| Что | Версия | Где |
|---|---|---|
| Базовый образ Android | `eclipse-temurin:21-jdk-noble@sha256:1072…e964` (JDK 21.0.12.1+1, Ubuntu 24.04) | `build/android.Containerfile` |
| Базовый образ Windows | `debian:trixie-slim@sha256:a292…f11f` | `build/windows.Containerfile` |
| Пакеты apt (clang/lld/llvm 19, NSIS 3.11, rsvg 2.60, unzip…) | снимок `snapshot.debian.org` / `snapshot.ubuntu.com` на `APT_SNAPSHOT` | Containerfile'ы |
| Node | 22.20.0 (sha256) | `build/env.sh` |
| rustup / Rust | 1.29.1 (sha256) / 1.99.1 | `build/env.sh` |
| cargo-xwin, MSVC CRT, Windows SDK | 0.23.1 (sha256), CRT 14.44.17.14, SDK 10.0.26100 | `build/env.sh` |
| Android cmdline-tools | 13114758 (sha256) | `build/env.sh` |
| Android platform / build-tools | android-36 / 36.0.0 | `build/env.sh` + `android-native/app/build.gradle.kts` |
| Gradle | 8.14.3 (`distributionSha256Sum`; `gradle-wrapper.jar` сверяется с официальным sha256) | `gradle-wrapper.properties`, `build/env.sh` |
| AGP, Kotlin, Compose, Media3 | версии в `libs.versions.toml`, sha256 в `verification-metadata.xml` | `android-native/gradle/` |
| npm-зависимости | `npm ci` по `package-lock.json` (точные версии в `package.json`) | `windows/` |
| crates | `cargo … --locked` по `Cargo.lock` | `windows/src-tauri/` |

NDK не используется (нативного кода нет), platform-tools (adb) для сборки не ставятся.

* Образ сборки помечается хэшем Containerfile'а (`pimusic-apps-android:<12 hex>`): после правки Containerfile'а образ
  пересобирается сам. Старые теги удалять вручную (`podman rmi`).
* `build/stamp.sh` (на ПК, перед заливкой) пишет `build/source.env` (в .gitignore): `SOURCE_COMMIT` (+ `-dirty`, если в
  `apps/` есть незакоммиченные правки) и `SOURCE_DATE_EPOCH` = время последнего коммита `apps/`. Скрипты передают их в
  контейнер; там же `TZ=UTC`, `LC_ALL=C.UTF-8`. Без файла сборка идёт, но с предупреждением.
* Рядом с каждым артефактом в `out/` пишутся `<файл>.sha256` (формат `sha256sum -c`) и `<файл>.buildinfo` (коммит,
  SOURCE_DATE_EPOCH, образ, версии тулчейнов; у APK — SHA-256 сертификата подписи). В релиз GitHub прикладывать оба.
* Windows: компоновка с `/Brepro` (в PE-заголовке хэш вместо времени) и `/DEBUG:NONE` (без PDB: его GUID менялся от
  сборки к сборке). Проверено: две сборки подряд с перекомпиляцией дают побитово одинаковые
  установщик и `PiMusic-portable.exe`. `.sig` (minisign) каждый раз новый — в подписи есть время.
* Обновить закрепления: digest — `podman images --digests` после `podman pull`; дату `APT_SNAPSHOT`;
  sha256 загрузок — с официальных страниц выпусков.
* Остаётся невоспроизводимым: см. «Что не закреплено» ниже.
