// PiMusic для Windows — оболочка Tauri 2 (WebView2). С 2.0 интерфейс (Mini App, bot/app/static) лежит внутри
// приложения (frontendDist = dist-ui, собирает build/prepare-webui.mjs), с сервера — только API, обложки и поток.
// Адрес сервера задаётся при первом запуске (встроенная страница setup/), в страницу он приходит как
// window.PIMUSIC_API_BASE. Мост window.PiMusicNative внедряется скриптом инициализации (../../shared/bridge.js).
// Поток и офлайн-треки — через протокол pimusic:// с кэшем на диске (cache.rs).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod cache;

use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_updater::{Update, UpdaterExt};
use tauri_plugin_window_state::StateFlags;
use url::Url;

/// Адреса по умолчанию нет: при первом запуске пользователь вводит адрес своего сервера (страница setup/).
const DEFAULT_SERVER: &str = "";
const BRIDGE_JS: &str = include_str!("../../../shared/bridge.js");
const MAIN: &str = "main";
/// Встроенные страницы (frontendDist = dist-ui): интерфейс и страница выбора сервера
const LOCAL_ORIGIN: &str = if cfg!(windows) { "http://tauri.localhost" } else { "tauri://localhost" };
const UI_PAGE: &str = "index.html";
const SETUP_PAGE: &str = "setup/index.html";
/// Проверка обновлений: через минуту после запуска и затем каждые 6 часов (адрес — plugins.updater в tauri.conf.json)
const UPDATE_FIRST: Duration = Duration::from_secs(60);
const UPDATE_EVERY: Duration = Duration::from_secs(6 * 3600);
/// Заглушка pubkey в tauri.conf.json; настоящий ключ подставляет сборка (build/windows-inner.sh) или правка конфига
const UPDATER_PUBKEY_PLACEHOLDER: &str = "PIMUSIC_UPDATER_PUBKEY";
const MEDIA_CODES: [Code; 4] = [Code::MediaPlayPause, Code::MediaTrackNext, Code::MediaTrackPrevious, Code::MediaStop];

// ------------------------------------------------------------------------------------------- настройки
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default, rename_all = "camelCase")]
struct Settings {
    server_url: Option<String>,
    close_to_tray: bool,
    notifications: bool,
    /// Глобальный перехват медиаклавиш (RegisterHotKey). По умолчанию выключен: клавиши и SMTC
    /// обслуживает сам WebView2 через Media Session (включено флагом HardwareMediaKeyHandling).
    media_keys: bool,
}
impl Default for Settings {
    fn default() -> Self {
        Self { server_url: None, close_to_tray: true, notifications: true, media_keys: false }
    }
}

#[derive(Default, Clone)]
struct Media {
    title: String,
    artist: String,
    state: String,
}

struct TrayItems {
    toggle: MenuItem<Wry>,
    playpause: MenuItem<Wry>,
    close_to_tray: CheckMenuItem<Wry>,
    autostart: CheckMenuItem<Wry>,
    notifications: CheckMenuItem<Wry>,
    media_keys: CheckMenuItem<Wry>,
}

struct AppState {
    settings: Mutex<Settings>,
    media: Mutex<Media>,
    tray: Mutex<Option<TrayItems>>,
    /// идёт проверка/диалог/загрузка обновления — вторую не начинать
    update_busy: AtomicBool,
    /// версия, на которую ответили «Позже»: фоновая проверка о ней больше не спрашивает (ручная — спрашивает)
    update_later: Mutex<Option<String>>,
    /// найденное обновление, ждёт ответа «Обновить/Позже»
    update_pending: Mutex<Option<Update>>,
    /// идёт загрузка/установка
    update_installing: AtomicBool,
    /// веб-приложение само показывает диалог обновления (метод моста setUpdateUi; сбрасывается при загрузке страницы)
    web_update_ui: AtomicBool,
}

fn settings_path(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("settings.json"))
}
fn load_settings(app: &AppHandle) -> Settings {
    settings_path(app)
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}
fn save_settings(app: &AppHandle, s: &Settings) {
    if let Some(p) = settings_path(app) {
        if let Some(dir) = p.parent() {
            let _ = fs::create_dir_all(dir);
        }
        if let Ok(txt) = serde_json::to_string_pretty(s) {
            let _ = fs::write(p, txt);
        }
    }
}
fn settings(app: &AppHandle) -> Settings {
    app.state::<AppState>().settings.lock().unwrap().clone()
}

/// Адрес сервера: http(s), без пути → /app/
fn normalize_server(raw: &str) -> Result<Url, String> {
    let raw = raw.trim();
    let mut u = Url::parse(raw).map_err(|_| "Неверный адрес".to_string())?;
    if u.scheme() != "http" && u.scheme() != "https" {
        return Err("Нужен адрес http:// или https://".into());
    }
    if u.host_str().unwrap_or("").is_empty() {
        return Err("В адресе нет сервера".into());
    }
    if u.path().is_empty() || u.path() == "/" {
        u.set_path("/app/");
    }
    Ok(u)
}

// ------------------------------------------------------------------------------------------- окно
fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(MAIN)
}

fn emit_js(app: &AppHandle, name: &str, detail: Value) {
    if let Some(w) = main_window(app) {
        let js = format!(
            "window.PiMusicNative&&window.PiMusicNative._emit({},{})",
            serde_json::to_string(name).unwrap_or_default(),
            detail
        );
        let _ = w.eval(&js);
    }
}

fn media_action(app: &AppHandle, action: &str) {
    emit_js(app, "media", json!({ "action": action }));
}

fn show_window(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
    emit_js(app, "visibility", json!({ "visible": true }));
    update_tray(app);
}
fn hide_window(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.hide();
    }
    emit_js(app, "visibility", json!({ "visible": false }));
    update_tray(app);
}
fn window_shown(app: &AppHandle) -> bool {
    main_window(app)
        .map(|w| w.is_visible().unwrap_or(false) && !w.is_minimized().unwrap_or(false))
        .unwrap_or(false)
}
fn toggle_window(app: &AppHandle) {
    if window_shown(app) {
        hide_window(app)
    } else {
        show_window(app)
    }
}

/// origin scheme://host[:port] (порт по умолчанию опускается); для не-http схем — None
fn origin_of(u: &Url) -> Option<String> {
    match u.scheme() {
        "http" | "https" => Some(u.origin().ascii_serialization()),
        _ => None,
    }
}

fn server_origin(app: &AppHandle) -> Option<String> {
    settings(app).server_url.and_then(|s| Url::parse(&s).ok()).and_then(|u| origin_of(&u))
}

/// Навигация внутри окна — только встроенные страницы (и about:/data:/blob:); всё остальное, включая страницы
/// сервера, открывается во внешнем браузере.
fn nav_allowed(app: &AppHandle, u: &Url) -> bool {
    match u.scheme() {
        "about" | "data" | "blob" => true,
        _ if is_local_page(u) => true,
        _ => {
            let _ = open_external(app, u.as_str());
            false
        }
    }
}

/// Origin, с которого грузится протокол кэша (pimusic://): в WebView2 — http://pimusic.localhost
fn cache_origin() -> String {
    if cfg!(windows) { format!("http://{}.localhost", cache::SCHEME) } else { format!("{}://localhost", cache::SCHEME) }
}

fn build_main_window(app: &AppHandle, start_hidden: bool) -> tauri::Result<WebviewWindow> {
    let s = settings(app);
    let api_base = server_origin(app);
    let page = if api_base.is_some() { UI_PAGE } else { SETUP_PAGE };
    let url = WebviewUrl::App(page.into());
    let cfg = json!({
        "platform": "windows",
        "shell": "tauri",
        "version": app.package_info().version.to_string(),
        "serverUrl": s.server_url.clone().unwrap_or_default(),
        "apiBase": api_base.clone().unwrap_or_default(),
        "cacheBase": cache_origin(),
        "localUi": true,
        "features": {
            "tray": true, "closeToTray": true, "autostart": true, "notifications": true,
            "globalMediaKeys": true, "smtc": true, "serverSettings": true,
            "updates": updater_configured(app), "cache": true, "offline": true, "fileDrop": true
        }
    });
    // PIMUSIC_API_BASE — до загрузки модулей интерфейса: api.js ходит на `${PIMUSIC_API_BASE}/api/…`
    let init = format!(
        "window.PIMUSIC_API_BASE={};\nwindow.__PIMUSIC_NATIVE_CONFIG__={};\n{}",
        serde_json::to_string(&api_base.unwrap_or_default()).unwrap_or_default(),
        cfg,
        BRIDGE_JS
    );
    let nav_app = app.clone();
    let w = WebviewWindowBuilder::new(app, MAIN, url)
        .title("PiMusic")
        .inner_size(1180.0, 780.0)
        .min_inner_size(380.0, 560.0)
        .visible(false)
        .initialization_script(&init)
        // HardwareMediaKeyHandling: Media Session веб-приложения → системная панель мультимедиа (SMTC)
        // и аппаратные медиаклавиши; первые три флага — значения Tauri по умолчанию.
        .additional_browser_args(
            "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
             --enable-features=HardwareMediaKeyHandling \
             --autoplay-policy=no-user-gesture-required",
        )
        // перетаскивание файлов — веб-интерфейсу (DnD в «Загрузках»), а не обработчику Tauri
        .disable_drag_drop_handler()
        .on_navigation(move |u| nav_allowed(&nav_app, u))
        .on_page_load(|w, p| {
            // новая страница ещё не подписалась на обновления; брошенный ею диалог — как «не ответили»
            if matches!(p.event(), PageLoadEvent::Started) {
                let app = w.app_handle();
                let st = app.state::<AppState>();
                st.web_update_ui.store(false, Ordering::SeqCst);
                if !st.update_installing.load(Ordering::SeqCst) && st.update_pending.lock().unwrap().is_some() {
                    update_done(app);
                }
            }
        })
        .build()?;

    let ev_app = app.clone();
    w.on_window_event(move |e| match e {
        WindowEvent::CloseRequested { api, .. } => {
            if settings(&ev_app).close_to_tray {
                api.prevent_close();
                hide_window(&ev_app);
            } else {
                ev_app.exit(0);
            }
        }
        WindowEvent::Focused(_) => update_tray(&ev_app),
        _ => {}
    });
    if !start_hidden {
        let _ = w.show();
        let _ = w.set_focus();
    }
    Ok(w)
}

// ------------------------------------------------------------------------------------------- внешние ссылки
/// https://t.me/<bot>?start=X → tg://resolve?domain=<bot>&start=X (если установлен Telegram Desktop)
fn telegram_native(u: &Url) -> Option<String> {
    let host = u.host_str()?.to_ascii_lowercase();
    if host != "t.me" && host != "telegram.me" && host != "www.t.me" {
        return None;
    }
    let segs: Vec<&str> = u.path_segments()?.filter(|s| !s.is_empty()).collect();
    if segs.len() != 1 {
        return None;
    }
    let name = segs[0];
    if name.starts_with('+') || matches!(name, "share" | "joinchat" | "addstickers" | "proxy" | "socks") {
        return None;
    }
    if !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return None;
    }
    let mut out = format!("tg://resolve?domain={}", name);
    for (k, v) in u.query_pairs() {
        if matches!(k.as_ref(), "start" | "startapp" | "startgroup" | "text") {
            let enc: String = url::form_urlencoded::byte_serialize(v.as_bytes()).collect();
            out.push_str(&format!("&{}={}", k, enc));
        }
    }
    Some(out)
}

#[cfg(windows)]
fn tg_protocol_registered() -> bool {
    use winreg::enums::{HKEY_CLASSES_ROOT, HKEY_CURRENT_USER};
    use winreg::RegKey;
    RegKey::predef(HKEY_CLASSES_ROOT).open_subkey("tg\\shell\\open\\command").is_ok()
        || RegKey::predef(HKEY_CURRENT_USER)
            .open_subkey("Software\\Classes\\tg\\shell\\open\\command")
            .is_ok()
}
#[cfg(not(windows))]
fn tg_protocol_registered() -> bool {
    false
}

fn open_external(app: &AppHandle, raw: &str) -> Result<(), String> {
    let u = Url::parse(raw).map_err(|e| e.to_string())?;
    if !matches!(u.scheme(), "http" | "https" | "tg" | "mailto") {
        return Err(format!("Схема {} не поддерживается", u.scheme()));
    }
    let target = match telegram_native(&u) {
        Some(tg) if tg_protocol_registered() => tg,
        _ => u.to_string(),
    };
    app.opener().open_url(target, None::<&str>).map_err(|e| e.to_string())
}

// ------------------------------------------------------------------------------------------- трей
fn update_tray(app: &AppHandle) {
    let st = app.state::<AppState>();
    let media = st.media.lock().unwrap().clone();
    if let Some(items) = st.tray.lock().unwrap().as_ref() {
        let _ = items.toggle.set_text(if window_shown(app) { "Скрыть окно" } else { "Показать окно" });
        let _ = items.playpause.set_text(if media.state == "playing" { "Пауза" } else { "Играть" });
    }
    if let Some(tray) = app.tray_by_id("main") {
        let tip = if media.title.is_empty() {
            "PiMusic".to_string()
        } else if media.artist.is_empty() {
            format!("PiMusic — {}", media.title)
        } else {
            format!("PiMusic — {} · {}", media.artist, media.title)
        };
        // подсказка трея в Windows ограничена 127 символами
        let tip: String = tip.chars().take(120).collect();
        let _ = tray.set_tooltip(Some(tip));
    }
}

fn set_media_keys(app: &AppHandle, on: bool) {
    let gs = app.global_shortcut();
    for code in MEDIA_CODES {
        let sc = Shortcut::new(None, code);
        if on {
            if !gs.is_registered(sc) {
                if let Err(e) = gs.register(sc) {
                    eprintln!("media key {code:?}: {e}");
                }
            }
        } else if gs.is_registered(sc) {
            let _ = gs.unregister(sc);
        }
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let s = settings(app);
    let autostart_on = app.autolaunch().is_enabled().unwrap_or(false);
    let toggle = MenuItem::with_id(app, "toggle", "Показать окно", true, None::<&str>)?;
    let playpause = MenuItem::with_id(app, "playpause", "Играть", true, None::<&str>)?;
    let next = MenuItem::with_id(app, "next", "Следующий трек", true, None::<&str>)?;
    let prev = MenuItem::with_id(app, "prev", "Предыдущий трек", true, None::<&str>)?;
    let close_to_tray =
        CheckMenuItem::with_id(app, "close_to_tray", "Сворачивать в трей при закрытии", true, s.close_to_tray, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(app, "autostart", "Запускать при входе в Windows", true, autostart_on, None::<&str>)?;
    let notifications =
        CheckMenuItem::with_id(app, "notifications", "Уведомлять о смене трека", true, s.notifications, None::<&str>)?;
    let media_keys =
        CheckMenuItem::with_id(app, "media_keys", "Перехватывать медиаклавиши", true, s.media_keys, None::<&str>)?;
    let server = MenuItem::with_id(app, "server", "Сменить сервер…", true, None::<&str>)?;
    let updates = MenuItem::with_id(app, "updates", "Проверить обновления", true, None::<&str>)?;
    let reload = MenuItem::with_id(app, "reload", "Перезагрузить", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Выход", true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let sep3 = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &toggle, &sep1, &playpause, &next, &prev, &sep2, &close_to_tray, &autostart, &notifications, &media_keys,
            &server, &updates, &reload, &sep3, &quit,
        ],
    )?;

    let icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))?;
    TrayIconBuilder::with_id("main")
        .icon(icon)
        .tooltip("PiMusic")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, ev| on_menu(app, ev.id().as_ref()))
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                toggle_window(tray.app_handle());
            }
        })
        .build(app)?;

    *app.state::<AppState>().tray.lock().unwrap() =
        Some(TrayItems { toggle, playpause, close_to_tray, autostart, notifications, media_keys });
    update_tray(app);
    Ok(())
}

fn check_state(app: &AppHandle, f: impl Fn(&TrayItems) -> &CheckMenuItem<Wry>) -> bool {
    app.state::<AppState>().tray.lock().unwrap().as_ref().and_then(|t| f(t).is_checked().ok()).unwrap_or(false)
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "toggle" => toggle_window(app),
        "playpause" => media_action(app, "playpause"),
        "next" => media_action(app, "nexttrack"),
        "prev" => media_action(app, "previoustrack"),
        "close_to_tray" | "notifications" | "media_keys" => {
            let on = check_state(app, |t| match id {
                "close_to_tray" => &t.close_to_tray,
                "notifications" => &t.notifications,
                _ => &t.media_keys,
            });
            let patch = match id {
                "close_to_tray" => json!({ "closeToTray": on }),
                "notifications" => json!({ "notifications": on }),
                _ => json!({ "mediaKeys": on }),
            };
            apply_settings(app, &patch);
        }
        "autostart" => {
            let on = check_state(app, |t| &t.autostart);
            apply_settings(app, &json!({ "autostart": on }));
        }
        "server" => {
            show_window(app);
            open_server_settings(app, false);
        }
        "updates" => {
            if !updater_configured(app) {
                info_dialog(app, "Обновления", "Эта сборка без автообновления.".into(), MessageDialogKind::Info);
                return;
            }
            let app = app.clone();
            tauri::async_runtime::spawn(async move { check_update(&app, true).await });
        }
        "reload" => {
            if let Some(w) = main_window(app) {
                let _ = w.eval("location.reload()");
            }
        }
        "quit" => app.exit(0),
        _ => {}
    }
}

// ------------------------------------------------------------------------------------------- обновления
// latest.json (формат tauri-plugin-updater v2) — из последнего выпуска GitHub (plugins.updater.endpoints) (docs/design/APPS_API.md,
// «Обновления»). Установщик проверяется по подписи (pubkey в tauri.conf.json) и ставится NSIS в режиме passive;
// на время установки приложение закрывается, установщик запускает его снова.
// Диалог: если веб-приложение подписалось (setUpdateUi) и окно на экране — событие моста "update" (диалог M3 рисует
// веб-интерфейс, дальше installUpdate/dismissUpdate); иначе — системный диалог Windows.

fn info_dialog(app: &AppHandle, title: &str, text: String, kind: MessageDialogKind) {
    app.dialog().message(text).title(title).kind(kind).show(|_| {});
}

fn update_done(app: &AppHandle) {
    let st = app.state::<AppState>();
    *st.update_pending.lock().unwrap() = None;
    st.update_busy.store(false, Ordering::SeqCst);
}

fn update_json(u: &Update, manual: bool) -> Value {
    json!({
        "available": true,
        "version": u.version,
        "currentVersion": u.current_version,
        "notes": u.body.clone().unwrap_or_default(),
        "date": u.date.map(|d| d.unix_timestamp()),
        "manual": manual,
    })
}

async fn fetch_update(app: &AppHandle) -> Result<Option<Update>, String> {
    if !updater_configured(app) {
        return Err("эта сборка без автообновления".into());
    }
    app.updater().map_err(|e| e.to_string())?.check().await.map_err(|e| e.to_string())
}

/// Фоновая (manual=false) или ручная (трей) проверка с показом диалога.
async fn check_update(app: &AppHandle, manual: bool) {
    if app.state::<AppState>().update_busy.swap(true, Ordering::SeqCst) {
        return;
    }
    match fetch_update(app).await {
        Ok(Some(update)) => {
            let st = app.state::<AppState>();
            let later = st.update_later.lock().unwrap().clone();
            if !manual && later.as_deref() == Some(update.version.as_str()) {
                update_done(app);
                return;
            }
            *st.update_pending.lock().unwrap() = Some(update.clone());
            // ручная проверка из трея: окно показать, а диалог — веб-интерфейса, если он подписан
            if manual {
                show_window(app);
            }
            if st.web_update_ui.load(Ordering::SeqCst) && window_shown(app) {
                emit_js(app, "update", update_json(&update, manual));
            } else {
                ask_update(app, update);
            }
        }
        Ok(None) => {
            update_done(app);
            if manual {
                let v = app.package_info().version.to_string();
                info_dialog(app, "Обновления", format!("Установлена последняя версия PiMusic ({v})."), MessageDialogKind::Info);
            }
        }
        Err(e) => {
            update_done(app);
            eprintln!("updater: {e}");
            if manual {
                info_dialog(app, "Обновления", format!("Не удалось проверить обновления: {e}"), MessageDialogKind::Error);
            }
        }
    }
}

/// Системный диалог (запасной вариант, когда веб-интерфейс не показывает свой).
fn ask_update(app: &AppHandle, update: Update) {
    let mut text = format!("Доступна версия {} (установлена {}).", update.version, update.current_version);
    if let Some(notes) = update.body.as_deref().map(str::trim).filter(|n| !n.is_empty()) {
        let notes: String = notes.chars().take(600).collect();
        text.push_str("\n\n");
        text.push_str(&notes);
    }
    text.push_str("\n\nPiMusic закроется на время установки и запустится снова.");
    let app2 = app.clone();
    app.dialog()
        .message(text)
        .title("Обновление PiMusic")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom("Обновить".into(), "Позже".into()))
        .show(move |ok| {
            if ok {
                start_install(&app2, false);
            } else {
                dismiss_update(&app2);
            }
        });
}

fn dismiss_update(app: &AppHandle) {
    let st = app.state::<AppState>();
    if st.update_installing.load(Ordering::SeqCst) {
        return;
    }
    if let Some(u) = st.update_pending.lock().unwrap().as_ref() {
        *st.update_later.lock().unwrap() = Some(u.version.clone());
    }
    update_done(app);
}

/// Скачать и установить обновление из update_pending. web=true — прогресс и ошибки событиями моста.
fn start_install(app: &AppHandle, web: bool) -> bool {
    let st = app.state::<AppState>();
    let Some(update) = st.update_pending.lock().unwrap().clone() else {
        return false;
    };
    if st.update_installing.swap(true, Ordering::SeqCst) {
        return true;
    }
    st.update_busy.store(true, Ordering::SeqCst);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let (mut got, mut last_pct) = (0u64, u64::MAX);
        let app_p = app.clone();
        let res = update
            .download_and_install(
                move |chunk, total| {
                    got += chunk as u64;
                    let pct = total.filter(|t| *t > 0).map(|t| got * 100 / t);
                    // не чаще раза на процент
                    if web && pct.unwrap_or(u64::MAX - 1) != last_pct {
                        last_pct = pct.unwrap_or(u64::MAX - 1);
                        emit_js(&app_p, "updateProgress", json!({ "downloaded": got, "total": total, "percent": pct }));
                    }
                },
                || {},
            )
            .await;
        // на Windows install() запускает установщик и завершает процесс; сюда возвращаемся только с ошибкой
        app.state::<AppState>().update_installing.store(false, Ordering::SeqCst);
        if let Err(e) = res {
            eprintln!("updater install: {e}");
            if web {
                emit_js(&app, "updateError", json!({ "message": e.to_string() }));
            } else {
                info_dialog(&app, "Обновление PiMusic", format!("Не удалось установить обновление: {e}"), MessageDialogKind::Error);
            }
        }
        update_done(&app);
    });
    true
}

/// Сборка без ключа обновлений (pubkey — заглушка из репозитория) обновления не проверяет.
fn updater_configured(app: &AppHandle) -> bool {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|u| u.get("pubkey"))
        .and_then(Value::as_str)
        .map(|k| !k.is_empty() && k != UPDATER_PUBKEY_PLACEHOLDER)
        .unwrap_or(false)
}

fn spawn_update_checks(app: &AppHandle) {
    if !updater_configured(app) {
        eprintln!("updater: нет pubkey — проверка обновлений выключена");
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(UPDATE_FIRST);
        loop {
            tauri::async_runtime::block_on(check_update(&app, false));
            std::thread::sleep(UPDATE_EVERY);
        }
    });
}

// ------------------------------------------------------------------------------------------- мост
fn settings_json(app: &AppHandle) -> Value {
    let s = settings(app);
    json!({
        "serverUrl": s.server_url,
        "defaultServerUrl": DEFAULT_SERVER,
        "closeToTray": s.close_to_tray,
        "notifications": s.notifications,
        "mediaKeys": s.media_keys,
        "autostart": app.autolaunch().is_enabled().unwrap_or(false),
    })
}

fn apply_settings(app: &AppHandle, patch: &Value) -> Value {
    {
        let st = app.state::<AppState>();
        let mut s = st.settings.lock().unwrap();
        if let Some(v) = patch.get("closeToTray").and_then(Value::as_bool) {
            s.close_to_tray = v;
        }
        if let Some(v) = patch.get("notifications").and_then(Value::as_bool) {
            s.notifications = v;
        }
        if let Some(v) = patch.get("mediaKeys").and_then(Value::as_bool) {
            s.media_keys = v;
        }
        save_settings(app, &s);
    }
    if let Some(v) = patch.get("autostart").and_then(Value::as_bool) {
        let al = app.autolaunch();
        let _ = if v { al.enable() } else { al.disable() };
    }
    let s = settings(app);
    set_media_keys(app, s.media_keys);
    if let Some(items) = app.state::<AppState>().tray.lock().unwrap().as_ref() {
        let _ = items.close_to_tray.set_checked(s.close_to_tray);
        let _ = items.notifications.set_checked(s.notifications);
        let _ = items.media_keys.set_checked(s.media_keys);
        let _ = items.autostart.set_checked(app.autolaunch().is_enabled().unwrap_or(false));
    }
    let out = settings_json(app);
    emit_js(app, "settings", out.clone());
    out
}

/// Встроенные страницы (frontendDist): ровно http://tauri.localhost (Windows) или tauri://localhost, без порта.
fn is_local_page(u: &Url) -> bool {
    match u.scheme() {
        "tauri" => u.host_str() == Some("localhost") && u.port().is_none(),
        "http" | "https" => u.host_str() == Some("tauri.localhost") && u.port().is_none(),
        _ => false,
    }
}

/// Новый сервер: сохранить и перезапустить приложение — CSP (connect/img/media на origin сервера), адрес API
/// и каталог кэша задаются при запуске.
fn apply_server(app: &AppHandle, u: Url) -> Result<(), String> {
    let same = {
        let st = app.state::<AppState>();
        let mut s = st.settings.lock().unwrap();
        let old = s.server_url.as_deref().and_then(|x| Url::parse(x).ok()).and_then(|x| origin_of(&x));
        s.server_url = Some(u.to_string());
        save_settings(app, &s);
        old.is_some() && old == origin_of(&u)
    };
    if same {
        // тот же сервер (другой путь) — достаточно открыть интерфейс
        if let (Some(w), Ok(page)) = (main_window(app), Url::parse(&format!("{LOCAL_ORIGIN}/{UI_PAGE}"))) {
            w.navigate(page).map_err(|e| e.to_string())?;
        }
        return Ok(());
    }
    app.restart();
}

fn open_server_settings(app: &AppHandle, error: bool) {
    if let Some(w) = main_window(app) {
        let q = if error { "?error=1" } else { "" };
        if let Ok(u) = Url::parse(&format!("{LOCAL_ORIGIN}/{SETUP_PAGE}{q}")) {
            let _ = w.navigate(u);
        }
    }
}

fn on_media_update(app: &AppHandle, args: &Value) {
    let get = |k: &str| args.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let new = Media { title: get("title"), artist: get("artist"), state: get("state") };
    let (track_changed, notify) = {
        let st = app.state::<AppState>();
        let mut m = st.media.lock().unwrap();
        let changed = !new.title.is_empty() && (m.title != new.title || m.artist != new.artist);
        *m = new.clone();
        (changed, settings(app).notifications)
    };
    update_tray(app);
    if track_changed && notify && new.state == "playing" && !window_shown(app) {
        let _ = app
            .notification()
            .builder()
            .title(&new.title)
            .body(if new.artist.is_empty() { "PiMusic".to_string() } else { new.artist.clone() })
            .show();
    }
}

fn parse_hex_color(c: &str) -> Option<(u8, u8, u8)> {
    let h = c.trim().trim_start_matches('#');
    let h = if h.len() == 3 { h.chars().flat_map(|c| [c, c]).collect::<String>() } else { h.to_string() };
    if h.len() < 6 {
        return None;
    }
    let p = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
    Some((p(0)?, p(2)?, p(4)?))
}

#[tauri::command]
async fn pimusic_native(
    app: AppHandle,
    webview: tauri::Webview,
    method: String,
    args: Option<Value>,
) -> Result<Value, String> {
    // вызывать мост может только встроенная страница (интерфейс или выбор сервера); остальным — отказ,
    // даже если capability их пропустила
    let caller = webview.url().map_err(|e| e.to_string())?;
    if !is_local_page(&caller) {
        return Err(format!("PiMusicNative: вызов с {} запрещён", caller.origin().ascii_serialization()));
    }
    let args = args.unwrap_or(Value::Null);
    if let Some(res) = cache::command(&app, &method, &args) {
        return res;
    }
    match method.as_str() {
        "getInfo" => Ok(json!({
            "platform": "windows",
            "shell": "tauri",
            "version": app.package_info().version.to_string(),
            "tauri": tauri::VERSION,
            "serverUrl": settings(&app).server_url,
            "visible": window_shown(&app),
        })),
        "getSettings" => Ok(settings_json(&app)),
        "setSettings" => Ok(apply_settings(&app, &args)),
        "setServerUrl" => {
            let raw = args.get("url").and_then(Value::as_str).unwrap_or("");
            let u = normalize_server(raw)?;
            // мост доступен только встроенным страницам — подтверждение не нужно; смена origin — перезапуск
            apply_server(&app, u.clone())?;
            Ok(json!({ "serverUrl": u.to_string() }))
        }
        "openServerSettings" => {
            open_server_settings(&app, args.get("error").and_then(Value::as_bool).unwrap_or(false));
            Ok(Value::Null)
        }
        "openExternal" => {
            let url = args.get("url").and_then(Value::as_str).unwrap_or("");
            open_external(&app, url)?;
            Ok(Value::Null)
        }
        "mediaUpdate" => {
            on_media_update(&app, &args);
            Ok(Value::Null)
        }
        "notify" => {
            let title = args.get("title").and_then(Value::as_str).unwrap_or("PiMusic");
            let body = args.get("body").and_then(Value::as_str).unwrap_or("");
            app.notification().builder().title(title).body(body).show().map_err(|e| e.to_string())?;
            Ok(Value::Null)
        }
        "setThemeColor" => {
            if let (Some(w), Some((r, g, b))) =
                (main_window(&app), args.get("color").and_then(Value::as_str).and_then(parse_hex_color))
            {
                let lum = 0.2126 * r as f32 + 0.7152 * g as f32 + 0.0722 * b as f32;
                let _ = w.set_theme(Some(if lum < 128.0 { tauri::Theme::Dark } else { tauri::Theme::Light }));
            }
            Ok(Value::Null)
        }
        "show" => {
            show_window(&app);
            Ok(Value::Null)
        }
        "hide" => {
            hide_window(&app);
            Ok(Value::Null)
        }
        "quit" => {
            app.exit(0);
            Ok(Value::Null)
        }
        // ---- обновления (docs/design/APPS_API.md, «Обновления»)
        "setUpdateUi" => {
            let on = args.get("enabled").and_then(Value::as_bool).unwrap_or(true);
            app.state::<AppState>().web_update_ui.store(on, Ordering::SeqCst);
            Ok(json!({ "enabled": on, "configured": updater_configured(&app) }))
        }
        "checkUpdate" => {
            let st = app.state::<AppState>();
            if st.update_installing.load(Ordering::SeqCst) {
                return Ok(json!({ "available": true, "installing": true }));
            }
            if let Some(u) = st.update_pending.lock().unwrap().as_ref() {
                return Ok(update_json(u, true));
            }
            if st.update_busy.swap(true, Ordering::SeqCst) {
                return Err("Проверка обновлений уже идёт".into());
            }
            match fetch_update(&app).await {
                Ok(Some(u)) => {
                    let out = update_json(&u, true);
                    *st.update_pending.lock().unwrap() = Some(u);
                    Ok(out)
                }
                Ok(None) => {
                    update_done(&app);
                    Ok(json!({ "available": false, "currentVersion": app.package_info().version.to_string() }))
                }
                Err(e) => {
                    update_done(&app);
                    Err(format!("Не удалось проверить обновления: {e}"))
                }
            }
        }
        "installUpdate" => {
            if start_install(&app, true) {
                Ok(json!({ "started": true }))
            } else {
                Err("Нет доступного обновления — сначала checkUpdate".into())
            }
        }
        "dismissUpdate" => {
            dismiss_update(&app);
            Ok(Value::Null)
        }
        _ => Err(format!("Неизвестный метод: {method}")),
    }
}

// ------------------------------------------------------------------------------------------- запуск
/// Origin сервера из settings.json ещё до запуска Tauri (нужен для CSP встроенных страниц).
fn saved_server_origin(identifier: &str) -> Option<String> {
    let dir = std::env::var_os("APPDATA").map(PathBuf::from)?.join(identifier);
    let s: Settings = serde_json::from_str(&fs::read_to_string(dir.join("settings.json")).ok()?).ok()?;
    Url::parse(s.server_url.as_deref()?).ok().and_then(|u| origin_of(&u))
}

/// CSP встроенных страниц: скрипты только свои; API, обложки и поток — только с настроенного сервера и протокола
/// кэша; картинки обложек внешних каталогов — любой https:; шрифты Google.
fn ui_csp(server: Option<&str>) -> String {
    let srv = server.unwrap_or("");
    let cache = cache_origin();
    [
        "default-src 'self'".to_string(),
        "script-src 'self'".to_string(),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com".to_string(),
        "font-src 'self' https://fonts.gstatic.com data:".to_string(),
        format!("img-src 'self' data: blob: https: {srv} {cache}"),
        format!("media-src 'self' blob: data: {srv} {cache} https://*.dzcdn.net"),
        format!("connect-src 'self' ipc: http://ipc.localhost {srv} {cache}"),
        "worker-src 'self'".to_string(),
        "frame-src 'none'".to_string(),
        "object-src 'none'".to_string(),
        "base-uri 'self'".to_string(),
        "form-action 'none'".to_string(),
    ]
    .join("; ")
}

fn main() {
    let start_hidden = std::env::args().any(|a| a == "--minimized");
    // TLS для reqwest (кэш потока): тот же провайдер rustls, что у updater
    let _ = rustls::crypto::ring::default_provider().install_default();
    let mut context = tauri::generate_context!();
    let server = saved_server_origin(&context.config().identifier);
    context.config_mut().app.security.csp = Some(tauri::utils::config::Csp::Policy(ui_csp(server.as_deref())));
    let app = tauri::Builder::default()
        // single-instance — первым: второй запуск просто показывает окно
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_window(app)))
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(StateFlags::all() & !StateFlags::VISIBLE)
                .build(),
        )
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--minimized"])))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, sc, ev| {
                    if ev.state() != ShortcutState::Pressed {
                        return;
                    }
                    let action = if sc.matches(Modifiers::empty(), Code::MediaPlayPause) {
                        "playpause"
                    } else if sc.matches(Modifiers::empty(), Code::MediaTrackNext) {
                        "nexttrack"
                    } else if sc.matches(Modifiers::empty(), Code::MediaTrackPrevious) {
                        "previoustrack"
                    } else if sc.matches(Modifiers::empty(), Code::MediaStop) {
                        "pause"
                    } else {
                        return;
                    };
                    media_action(app, action);
                })
                .build(),
        )
        .manage(AppState {
            settings: Mutex::new(Settings::default()),
            media: Mutex::default(),
            tray: Mutex::new(None),
            update_busy: AtomicBool::new(false),
            update_later: Mutex::new(None),
            update_pending: Mutex::new(None),
            update_installing: AtomicBool::new(false),
            web_update_ui: AtomicBool::new(false),
        })
        .invoke_handler(tauri::generate_handler![pimusic_native])
        // pimusic:// — поток и офлайн-треки с кэшем на диске (cache.rs)
        .register_asynchronous_uri_scheme_protocol(cache::SCHEME, |ctx, req, responder| {
            let app = ctx.app_handle().clone();
            tauri::async_runtime::spawn(async move { responder.respond(cache::handle(app, req).await) });
        })
        .setup(move |app| {
            let handle = app.handle().clone();
            let s = load_settings(&handle);
            *app.state::<AppState>().settings.lock().unwrap() = s.clone();
            app.manage(cache::Cache::new(&handle, server_origin(&handle).as_deref()));
            build_tray(&handle)?;
            build_main_window(&handle, start_hidden)?;
            set_media_keys(&handle, s.media_keys);
            update_tray(&handle);
            spawn_update_checks(&handle);
            Ok(())
        })
        .build(context)
        .expect("error while building PiMusic");

    app.run(|_app, ev| {
        // окно скрыто в трей — приложение продолжает жить; выход только через «Выход» (app.exit)
        if let RunEvent::ExitRequested { api, code, .. } = ev {
            if code.is_none() {
                api.prevent_exit();
            }
        }
    });
}
