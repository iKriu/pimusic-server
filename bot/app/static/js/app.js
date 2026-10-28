// PiMusic Mini App — точка входа (ES-модули, без сборки).
import { el, icon, mount, nTracks } from "./dom.js";
import { api, inTelegram, getToken } from "./api.js";
import { showLogin } from "./login.js";
import { openSettings } from "./screens/settings.js";
import * as settingsSync from "./settings.js";
import { initEq } from "./eq.js";
import { initPlayerOptions } from "./playerctl.js";
import { initNative, inShell } from "./native.js";
import { initUpdates } from "./updates.js";
import { initOffline } from "./offline.js";
import { refresh as refreshUploads } from "./uploads.js";
import { tg, initTelegram, closeApp, openExternal, haptic } from "./tg.js";
import { applyTheme } from "./theme.js";
import { installShapes } from "./shapes.js";
import { installMotion } from "./motion.js";
import { button, dialog, list, listItem, snackbar, loadingIndicator } from "./ui.js";
import { initShell, back, go } from "./shell.js";
import { createMiniPlayer, installAnnouncer } from "./nowplaying.js";
import { openNowPlaying } from "./fullplayer.js";
import { restoreQueue } from "./player.js";
import { handleDeepLink } from "./screens/playlist.js";
import * as store from "./store.js";
import { createSearch } from "./screens/search.js";
import { createLibrary } from "./screens/library.js";
import { createDownloads, createSupport } from "./screens/downloads.js";
import { createWave, copyText } from "./screens/wave.js";

const app = document.getElementById("app");
const gateEl = document.getElementById("gate");

// Шрифт иконок: пока не загрузился — ligature-текст скрыт; не загрузился за 4 с — запасные символы
function watchIconFont() {
  const root = document.documentElement;
  if (!document.fonts || !document.fonts.load) { root.classList.add("no-icon-font"); return; }
  root.classList.add("icons-pending");
  const done = (ok) => { root.classList.remove("icons-pending"); if (!ok) root.classList.add("no-icon-font"); };
  const t = setTimeout(() => done(false), 4000);
  document.fonts.load('24px "Material Symbols Rounded"', "download").then((f) => { clearTimeout(t); done(f && f.length > 0); }, () => { clearTimeout(t); done(false); });
}

function gate({ shape = "cookie9", ic, title, lines = [], actions = [] }) {
  app.hidden = true;
  mount(gateEl, el("div", { class: "gate-inner" },
    el("span", { class: "state-art gate-art", style: { "--state-shape": `var(--shape-${shape})` } }, ic ? icon(ic, { fill: true }) : loadingIndicator({ size: 64 })),
    el("h1", { class: "gate-title", text: title }),
    lines.map((l) => (typeof l === "string" ? el("p", { class: "gate-text", text: l }) : l)),
    actions.length ? el("div", { class: "gate-actions" }, actions) : null));
  gateEl.hidden = false;
}

function aboutDialog() {
  const me = store.state.me;
  if (!me) return;
  haptic("light");
  const row = (ic, head, sup, trailing) => listItem({ leading: el("span", { class: "li-icon-box" }, icon(ic)), headline: head, supporting: sup, trailing });
  dialog({
    icon: "info", headline: "О сервере",
    content: list([
      row("person", me.login, me.is_admin ? "Администратор" : "Пользователь"),
      row("library_music", "Медиатека", nTracks(me.tracks)),
      row("open_in_new", "Jellyfin", me.jellyfin_url, button({ label: "Открыть", variant: "text", onClick: () => openExternal(me.jellyfin_url) })),
      row("radio", "Поток «Моя Волна»", me.stream_url, button({ label: "Копировать", variant: "text", onClick: async () => { const ok = await copyText(me.stream_url); snackbar(ok ? "Ссылка скопирована" : "Не удалось скопировать", { error: !ok }); } })),
    ], "about-list"),
    actions: [{ label: "Закрыть", value: true }],
  });
}

async function boot() {
  installMotion();
  installShapes();
  applyTheme();
  watchIconFont();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (!tg || !tg.initData) applyTheme(); });
  let shellApi = null;
  initTelegram({
    onTheme: applyTheme,
    onViewport: () => shellApi && shellApi.relayout(),
    onBack: () => back(),
    onSettings: () => openSettingsSafe(),
  });

  // вне Telegram: PWA / приложения — вход по токену устройства
  if (!inTelegram()) {
    registerSW();
    window.addEventListener("pimusic:auth-lost", (ev) => { app.hidden = true; showLogin(gateEl, { reason: (ev.detail && ev.detail.reason) || "Вход на этом устройстве больше не действует. Войдите снова." }); });
    if (!getToken()) { showLogin(gateEl); return; }
  }
  gate({ title: "PiMusic", lines: ["Загрузка…"] });
  try {
    store.state.me = await api("/me");
  } catch (e) {
    if (e.code === "not_registered") {
      gate({ shape: "cookie4", ic: "person_add", title: "Сначала зарегистрируйтесь", lines: [
        "Ваш Telegram ещё не привязан к аккаунту PiMusic.",
        el("p", { class: "gate-text" }, "Отправьте боту: ", el("code", { text: "/register <инвайт> <логин> <пароль>" })),
        "После регистрации откройте приложение заново.",
      ], actions: [button({ label: "Открыть бота", icon: "arrow_back", variant: "filled", onClick: closeApp })] });
    } else if (e.status === 401 && !inTelegram()) {
      if (gateEl.querySelector(".login")) return; // экран входа уже показан событием pimusic:auth-lost
      showLogin(gateEl, { reason: "Вход на этом устройстве больше не действует. Войдите снова." });
    } else if (e.status === 401) {
      gate({ shape: "pentagon", ic: "history", title: "Сессия устарела", lines: ["Подпись Telegram устарела. Закройте и откройте мини-приложение заново из чата с ботом."],
        actions: [button({ label: "Открыть бота", icon: "arrow_back", variant: "filled", onClick: closeApp })] });
    } else {
      gate({ shape: "softBurst", ic: e.status === 0 ? "wifi_off" : "cloud_off", title: "Сервер недоступен", lines: [e.message],
        actions: [button({ label: "Повторить", icon: "refresh", variant: "filled", onClick: () => location.reload() })] });
    }
    return;
  }
  gateEl.hidden = true;
  app.hidden = false;
  const roots = {
    search: createSearch({ onSettings: () => openSettingsSafe() }),
    library: createLibrary(),
    downloads: createDownloads(),
    wave: createWave(),
  };
  shellApi = initShell({ roots, support: createSupport(), onSettings: () => openSettingsSafe() });
  // Android «Назад» из оболочки: тот же стек, что у кнопки «назад» Telegram (меню → лист/диалог → плеер → страница)
  initNative({ back: () => back() });
  initUpdates(); // Windows: свой M3-диалог обновления вместо системного (features.updates)
  initOffline(); // Windows 2.0: кэш потока и «Скачанное» (features.cache)
  refreshUploads(); // файлы, ждущие названия, видны сразу (бейдж «Загрузки»), а не только на вкладке
  // настройки по ключам (EQ, плеер) — у каждого логина свои; локально сразу, сервер в фоне
  settingsSync.setLogin(store.state.me.login);
  applyTheme(); // тема/акцент пользователя из его настроек (ui.theme) — приоритетнее локального кэша
  initEq();
  initPlayerOptions();
  settingsSync.startSync();
  // мини-плеер: над навигационной панелью (compact) / внизу области контента (rail); в DOM — перед панелью навигации
  const mini = createMiniPlayer({ onOpen: (mode) => (mode === "queue" ? openNowPlaying() : go("wave")) });
  app.insertBefore(mini, app.querySelector(".nav-bar"));
  installAnnouncer();
  restoreQueue(store.state.me.login).catch((e) => console.warn("queue restore", e && e.message));
  handleDeepLink();
  window.addEventListener("hashchange", () => handleDeepLink());
  store.loadKeys();
  store.kick();
}

// «Настройки» открываются поверх текущей вкладки (с «О сервере» внутри)
function openSettingsSafe() {
  if (!store.state.me) return;
  haptic("light");
  openSettings({ from: "list" });
}

// Service worker — только вне Telegram (PWA): кэш оболочки, офлайн-экран; /api и поток не кэшируются.
// Предпочтительно /app/sw.js (scope /app/); старый сервер его не отдаёт — тогда static/sw.js (scope static/).
async function registerSW() {
  if (!("serviceWorker" in navigator) || location.protocol === "file:" || inShell()) return; // оболочке SW не нужен
  // сначала проверяем, что файл есть (иначе браузер пишет ошибку регистрации в консоль)
  for (const [url, scope] of [["sw.js", "./"], ["static/sw.js", "static/"]]) {
    try {
      const r = await fetch(url, { method: "HEAD", cache: "no-store" });
      if (!r.ok || !/javascript/.test(r.headers.get("content-type") || "")) continue;
      await navigator.serviceWorker.register(url, { scope });
      return;
    } catch { /* */ }
  }
}

boot().catch((e) => {
  console.error(e);
  gate({ shape: "softBurst", ic: "error", title: "Не удалось запустить", lines: [String(e && e.message || e)],
    actions: [button({ label: "Повторить", icon: "refresh", onClick: () => location.reload() })] });
});
