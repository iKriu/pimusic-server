// «Настройки»: воспроизведение (нормализация, плавный переход, скорость, таймер сна), эквалайзер,
// это устройство (имя), устройства (EQ на каждом — режим/пресет, удалённая правка; входы по токенам — отзыв),
// аккаунт (выход вне Telegram), о сервере.
import { el, icon, mount, setText, nTracks, fmtTime } from "../dom.js";
import { api, authMode, setToken, API_BASE } from "../api.js";
import { button, iconButton, list, listItem, sectionTitle, slider, dialog, snackbar, textField, skeletonList, announce, wavyCircular } from "../ui.js";
import { topBar, trackScroll } from "../header.js";
import { open, back } from "../shell.js";
import { haptic, openExternal } from "../tg.js";
import * as store from "../store.js";
import * as eq from "../eq.js";
import * as settings from "../settings.js";
import { deviceId, PLATFORM_ICON } from "../device.js";
import { eqView } from "../eqview.js";
import { normalizeRow, opts, setOpt, fmtSpeed, fmtXf, fmtSleep, sleepSheet } from "../playerctl.js";
import { setPlayerOptions, onPlayer } from "../player.js";
import { copyText } from "./wave.js";
import { inShell, bridge, getNativeSettings, setNativeSettings, openServerSettings, onNativeSettings, nativeInfo } from "../native.js";
import { switchRow, connectedGroup, openMenu } from "../ui.js";
import { MODES, ACCENTS, themePref, setThemePref, accentPreview } from "../theme.js";
import { updatesSupported, checkForUpdates } from "../updates.js";
import { cacheOn, cacheInfo, cacheClear, setCacheLimit, fmtBytes } from "../offline.js";
import { openOffline } from "./offline.js";

const MODE = { sync: "синхронизирован", local: "только это устройство" };
const PLATFORM = { telegram: "Telegram", windows: "Windows", android: "Android", pwa: "Приложение", web: "Браузер" };

function pageBar(page, title) {
  const bar = topBar({ title, leading: iconButton("arrow_back", "Назад", { cls: "tbar-back", onClick: () => back() }) });
  page.scroll.appendChild(bar);
  trackScroll(page.el, page.scroll);
  return bar;
}

export function openSettings({ from = "list" } = {}) {
  open((page) => buildSettings(page), { kind: "settings", from });
}

export function openEqPage(target = deviceId(), { from = "detail" } = {}) {
  open((page) => {
    const own = target === deviceId();
    const d = eq.device(target) || {};
    pageBar(page, own ? "Эквалайзер" : `Эквалайзер · ${d.name || "устройство"}`);
    const v = eqView({ target });
    page.scroll.appendChild(el("div", { class: "page-body eq-page" }, v));
  }, { kind: "eq", from });
}

function buildSettings(page) {
  pageBar(page, "Настройки");
  const body = el("div", { class: "page-body set" });
  page.scroll.appendChild(body);
  const me = store.state.me || {};

  // --- воспроизведение
  const o = opts();
  const xf = slider({ label: "Плавный переход", min: 0, max: 12, step: 1, value: o.crossfade, format: fmtXf, onChange: (v) => setOpt("crossfade", v) });
  const sp = slider({ label: "Скорость", min: 0.5, max: 2, step: 0.05, value: o.speed, format: fmtSpeed, onInput: (v) => setPlayerOptions({ speed: v }), onChange: (v) => setOpt("speed", v) });
  const sleepRow = listItem({ leading: el("span", { class: "li-icon-box" }, icon("bedtime")), headline: "Таймер сна", supporting: fmtSleep(), lines: 2, onClick: () => sleepSheet() });
  const offSleep = onPlayer((w) => { if (!sleepRow.isConnected) { offSleep(); return; } if (w === "sleep") setText(sleepRow.querySelector(".li-supporting"), fmtSleep()); });

  // --- эквалайзер
  const eqRow = listItem({ leading: el("span", { class: "li-icon-box" }, icon("equalizer")), headline: "Эквалайзер", supporting: " ", lines: 2, onClick: () => openEqPage() });
  const eqSup = () => { const e = eq.effective(); return `${e.enabled ? eq.presetName(e.active) : "Выключен"} · ${MODE[e.mode] || ""}`; };

  // --- это устройство
  const meDev = listItem({ leading: el("span", { class: "li-icon-box" }, icon(PLATFORM_ICON[eq.device().platform] || "devices")), headline: eq.device().name, supporting: "Имя видно на других ваших устройствах", lines: 2,
    trailing: iconButton("edit", "Переименовать это устройство", { onClick: renameMe }) });
  async function renameMe() {
    const f = textField({ label: "Имя устройства", value: eq.device().name, maxlength: 60 });
    const ok = await dialog({ icon: "devices", headline: "Имя устройства", content: el("div", { class: "dlg-form" }, f),
      validate: () => (f.input.value.trim() ? "" : "Введите имя"), actions: [{ label: "Отмена", value: false }, { label: "Сохранить", value: true }] });
    if (ok) { eq.rename(f.input.value); haptic("success"); }
  }

  // --- устройства: EQ на других устройствах
  const eqDevBox = el("div");
  const renderEqDevices = () => {
    const devs = eq.devices().filter((d) => d.id !== deviceId());
    mount(eqDevBox, devs.length ? list(devs.map((d) => listItem({
      leading: el("span", { class: "li-icon-box" }, icon(PLATFORM_ICON[d.platform] || "devices")),
      headline: d.name, supporting: `${PLATFORM[d.platform] || "Устройство"} · EQ: ${d.enabled ? eq.presetName(eq.effective(d.id).active) : "выкл."} · ${MODE[d.mode] || ""}`, lines: 2,
      trailing: icon("chevron_right", { class: "li-chev" }), label: `${d.name}: настроить эквалайзер удалённо`,
      onClick: () => openEqPage(d.id),
    })), "set-list") : el("p", { class: "note", text: "Другие устройства появятся здесь, когда вы откроете PiMusic на них." }));
  };

  // --- входы по токенам (/api/devices)
  const loginBox = el("div");
  async function loadLogins() {
    mount(loginBox, skeletonList(2, { thumb: false }));
    let r;
    try { r = await page.sc.get("/devices"); } catch (e) {
      mount(loginBox, el("p", { class: "note", text: e.status === 404 ? "Сервер пока не поддерживает вход с других устройств." : e.message }));
      return;
    }
    const items = r.items || [];
    if (!items.length) { mount(loginBox, el("p", { class: "note", text: "Нет входов вне Telegram. Откройте PiMusic в браузере или приложении и войдите через Telegram." })); return; }
    const all = button({ label: "Выйти на всех устройствах", icon: "logout", variant: "outlined", cls: "btn-danger-outline set-all", onClick: () => logoutAll(items) });
    mount(loginBox, list(items.map((d) => listItem({
      leading: el("span", { class: "li-icon-box" }, icon(PLATFORM_ICON[d.platform] || "devices")),
      headline: d.name + (d.current ? " · это устройство" : ""),
      supporting: `${PLATFORM[d.platform] || "Устройство"} · был(о) в сети ${d.last_seen ? fmtTime(d.last_seen) : "—"}`, lines: 2,
      trailing: d.current ? null : iconButton("logout", `Отозвать вход: ${d.name}`, { onClick: () => revoke(d) }),
    })), "set-list"), el("div", { class: "row-actions" }, all));
  }
  // все входы по токенам: сначала чужие, затем (если вошли не через Telegram) — это устройство
  async function logoutAll(items) {
    const ok = await dialog({ icon: "logout", headline: "Выйти на всех устройствах?", body: `Отзовём все входы вне Telegram (${items.length}). ${authMode() === "token" ? "Это устройство тоже выйдет." : "Telegram-клиенты продолжат работать."}`,
      actions: [{ label: "Отмена", value: false }, { label: "Выйти везде", value: true, variant: "danger" }] });
    if (!ok) return;
    let failed = 0;
    // сервер может уметь это одним запросом; иначе — по одному
    try { await api("/auth/logout", { method: "POST", body: { all: true } }); } catch { /* */ }
    for (const d of items) {
      if (d.current) continue;
      try { await api(`/devices/${encodeURIComponent(d.id)}/revoke`, { method: "POST", body: {} }); } catch (e) { if (e.status !== 404) failed++; }
    }
    if (authMode() === "token") { setToken(""); location.reload(); return; }
    haptic(failed ? "warning" : "success");
    snackbar(failed ? `Не удалось отозвать: ${failed}` : "Все входы вне Telegram отозваны", { error: !!failed });
    loadLogins();
  }
  async function revoke(d) {
    const ok = await dialog({ icon: "logout", headline: `Отозвать «${d.name}»?`, body: "Устройство выйдет из PiMusic; войти снова можно через Telegram.",
      actions: [{ label: "Отмена", value: false }, { label: "Отозвать", value: true, variant: "danger" }] });
    if (!ok) return;
    try { await api(`/devices/${encodeURIComponent(d.id)}/revoke`, { method: "POST", body: {} }); haptic("success"); snackbar(`Вход «${d.name}» отозван`); loadLogins(); } catch (e) { snackbar(e.message, { error: true }); }
  }

  // --- приложение (оболочка Windows/Android): сервер, версия, настройки окна/уведомлений
  const appBox = el("div");
  async function renderApp() {
    const n = bridge();
    if (!n) return;
    const info = (await nativeInfo()) || n;
    const s = (await getNativeSettings()) || {};
    const server = s.serverUrl || info.serverUrl || n.serverUrl || API_BASE + "/app/"; // интерфейс может быть локальным — адрес сервера, не location
    const feats = n.features || {};
    const shellName = (info.platform || n.platform) === "windows" ? "Windows" : "Android";
    const rows = [
      listItem({ leading: el("span", { class: "li-icon-box" }, icon("dns")), headline: "Сервер", supporting: server, lines: 2,
        trailing: icon("chevron_right", { class: "li-chev" }), label: `Сервер: ${server}. Сменить`,
        onClick: async () => { haptic("light"); if (!(await openServerSettings())) snackbar("Не удалось открыть настройки сервера", { error: true }); } }),
      listItem({ leading: el("span", { class: "li-icon-box" }, icon(shellName === "Windows" ? "desktop_windows" : "phone_android")), headline: `PiMusic для ${shellName}`,
        supporting: `Версия ${info.version || n.version || "—"}`, lines: 2 }),
      // только Windows с автообновлением (features.updates); найденная версия — M3-диалог
      updatesSupported() ? updRow() : null,
      ...(cacheOn() ? await cacheRows() : []),
    ].filter(Boolean);
    const toggles = [];
    if ((info.platform || n.platform) === "windows") {
      const t = (key, label, sup, on) => {
        const sw = switchRow({ label, supporting: sup, checked: !!s[key], onChange: async (v) => {
          if (!(await setNativeSettings({ [key]: v }))) { sw.checked = !v; snackbar("Не удалось сохранить настройку приложения", { error: true }); }
        } });
        sw.dataset.key = key;
        if (on === false) { sw.classList.add("is-disabled"); sw.control.disabled = true; }
        return el("li", { class: "li set-sw" }, sw);
      };
      toggles.push(
        t("closeToTray", "Сворачивать в трей при закрытии", "Музыка продолжит играть, окно — в области уведомлений", feats.closeToTray !== false && feats.tray !== false),
        t("notifications", "Уведомления о треке", "Когда окно скрыто", feats.notifications !== false),
        t("mediaKeys", "Перехватывать медиаклавиши", "Если клавиши ⏯ ⏭ не доходят до приложения (перехватит их и у других плееров)", feats.globalMediaKeys !== false),
        t("autostart", "Запускать при входе в Windows", "Свёрнутым в трей", feats.autostart !== false),
      );
    }
    mount(appBox, [list(rows, "set-list"), toggles.length ? list(toggles, "set-list") : null]);
  }
  // кэш и офлайн (Windows 2.0): «Скачанное», кэш прослушивания (размер/лимит/очистка)
  const LIMITS = [256, 512, 1024, 2048, 5120].map((m) => m * 1048576);
  async function cacheRows() {
    const info = (await cacheInfo()) || {};
    const offN = (info.offline && info.offline.count) || 0, offB = (info.offline && info.offline.bytes) || 0;
    const st = info.stream || {};
    const offRow = listItem({ leading: el("span", { class: "li-icon-box" }, icon("download_done")), headline: "Скачанное", supporting: offN ? `${nTracks(offN)} · ${fmtBytes(offB)}` : "Пока ничего — «Скачать на устройство» в меню трека",
      lines: 2, trailing: icon("chevron_right", { class: "li-chev" }), onClick: () => { haptic("light"); openOffline(); } });
    const clear = button({ label: "Очистить", variant: "text", onClick: async () => {
      try { await cacheClear(); haptic("success"); snackbar("Кэш прослушивания очищен"); renderApp(); } catch (e) { snackbar(e.message || "Не удалось очистить", { error: true }); }
    } });
    const cacheRow = listItem({ leading: el("span", { class: "li-icon-box" }, icon("cached")), headline: "Кэш прослушивания",
      supporting: `${fmtBytes(st.bytes)} из ${fmtBytes(st.limit)} · треки играют без задержек и повторно — без сети`, lines: 2, trailing: clear });
    const limRow = listItem({ leading: el("span", { class: "li-icon-box" }, icon("data_usage")), headline: "Размер кэша", supporting: `Не больше ${fmtBytes(st.limit)}`, lines: 2,
      trailing: icon("arrow_drop_down", { class: "li-chev" }), onClick: (ev) => {
        const anchor = ev.currentTarget;
        openMenu(anchor, LIMITS.map((b) => ({ icon: b === st.limit ? "radio_button_checked" : "radio_button_unchecked", label: fmtBytes(b),
          onClick: async () => { try { await setCacheLimit(b); haptic("select"); renderApp(); } catch (e) { snackbar(e.message || "Не удалось изменить", { error: true }); } } })));
      } });
    return [offRow, cacheRow, limRow];
  }
  function updRow() {
    const IDLE = "Автоматически — раз в 6 часов";
    let busy = false;
    const row = listItem({ leading: el("span", { class: "li-icon-box" }, icon("system_update")), headline: "Проверить обновления", supporting: IDLE, lines: 2,
      cls: "set-upd", onClick: () => {
        if (busy) return;
        haptic("light");
        const main = row.querySelector(".li-main"), sup = row.querySelector(".li-supporting");
        // aria-disabled (не disabled) — фокус остаётся на пункте; справа — маленький круговой индикатор
        checkForUpdates((on) => {
          busy = on;
          main.setAttribute("aria-disabled", on ? "true" : "false");
          main.setAttribute("aria-busy", on ? "true" : "false");
          if (sup) sup.textContent = on ? "Проверяю…" : IDLE;
          const old = row.querySelector(".set-upd-spin");
          if (on && !old) { const w = wavyCircular({ size: 24, cls: "set-upd-spin", thin: true }); w.setAttribute("aria-hidden", "true"); main.appendChild(w); }
          if (!on && old) old.remove();
        });
      } });
    return row;
  }
  // настройки поменялись в трее Windows — обновить переключатели
  const offNative = onNativeSettings((s) => {
    if (!appBox.isConnected) { offNative(); return; }
    for (const row of appBox.querySelectorAll(".sw-row[data-key]")) if (s && row.dataset.key in s) row.checked = !!s[row.dataset.key];
  });
  if (inShell()) renderApp();

  // --- аккаунт и сервер
  const acct = [listItem({ leading: el("span", { class: "li-icon-box" }, icon("person")), headline: me.login || "—", supporting: `${me.is_admin ? "Администратор" : "Пользователь"} · ${nTracks(me.tracks || 0)}`, lines: 2 })];
  if (authMode() === "token") acct.push(listItem({ leading: el("span", { class: "li-icon-box" }, icon("logout")), headline: "Выйти", supporting: "Только на этом устройстве", lines: 2, cls: "is-danger", onClick: logout }));
  async function logout() {
    const ok = await dialog({ icon: "logout", headline: "Выйти из PiMusic?", body: "На этом устройстве понадобится снова войти через Telegram.", actions: [{ label: "Отмена", value: false }, { label: "Выйти", value: true, variant: "danger" }] });
    if (!ok) return;
    try { await api("/auth/logout", { method: "POST", body: {} }); } catch { /* токен всё равно стираем */ }
    setToken("");
    location.reload();
  }
  const server = [
    me.jellyfin_url ? listItem({ leading: el("span", { class: "li-icon-box" }, icon("open_in_new")), headline: "Jellyfin", supporting: me.jellyfin_url, lines: 2, onClick: () => openExternal(me.jellyfin_url) }) : null,
    me.stream_url ? listItem({ leading: el("span", { class: "li-icon-box" }, icon("radio")), headline: "Поток «Моя Волна»", supporting: "Скопировать ссылку для VLC или колонки", lines: 2,
      onClick: async () => { const ok = await copyText(me.stream_url); snackbar(ok ? "Ссылка скопирована" : "Не удалось скопировать", { error: !ok }); } }) : null,
  ].filter(Boolean);

  // --- оформление: тема (M3 segmented) и акцент (радио-образцы seed-цветов)
  const modeGroup = connectedGroup(MODES.map((m) => ({ key: m.key, label: m.label, icon: m.icon })), themePref().mode,
    (k) => { setThemePref({ mode: k }); paintAccents(); announce(`Тема: ${MODES.find((m) => m.key === k).label}`); }, { label: "Тема" });
  const accentBox = el("div", { class: "set-accents", role: "radiogroup", "aria-label": "Цвет акцента" });
  const accentSup = el("p", { class: "set-accent-sup" });
  let painted = "";
  // образцы создаются один раз; выбор меняет только состояние (is-on, aria-checked, tabindex) — морфинг формы проигрывается
  const swatches = ACCENTS.map((a) => {
    const dot = el("span", { class: "acc-dot" + (a.id === "dynamic" ? " is-dynamic" : ""), "aria-hidden": "true" },
      a.id === "dynamic" ? icon("auto_awesome") : null, icon("check", { class: "acc-check" }));
    const b = el("button", { type: "button", class: "acc", role: "radio", "aria-label": a.name }, dot, el("span", { class: "acc-name", "aria-hidden": "true", text: a.name }));
    b.addEventListener("click", () => {
      if (b.classList.contains("is-on")) return;
      haptic("select");
      setThemePref({ accent: a.id });
      paintAccents();
      announce(`Акцент: ${a.name}`);
      b.focus({ preventScroll: true });
    });
    b._a = a; b._dot = dot;
    return b;
  });
  accentBox.append(...swatches);
  function paintAccents() {
    const cur = themePref().accent;
    painted = cur + themePref().mode;
    for (const b of swatches) {
      const on = b._a.id === cur;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      if (b._a.id !== "dynamic") b._dot.style.background = accentPreview(b._a.id); // цвет — под текущий режим
    }
    setText(accentSup, cur === "dynamic" ? "Альбомы, исполнители и мини-плеер окрашиваются в цвет обложки" : "Один цвет во всём приложении");
  }
  // стрелки — по образцам (радиогруппа)
  accentBox.addEventListener("keydown", (ev) => {
    const list = [...accentBox.querySelectorAll(".acc")], i = list.indexOf(document.activeElement);
    const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key];
    if (i < 0 || !d) return;
    ev.preventDefault();
    list[(i + d + list.length) % list.length].click();
  });
  paintAccents();
  const look = el("div", { class: "set-look" }, el("p", { class: "set-look-h", text: "Тема" }), modeGroup, el("p", { class: "set-look-h", text: "Цвет акцента" }), accentBox, accentSup);

  mount(body, [
    sectionTitle("Оформление"),
    look,
    sectionTitle("Воспроизведение"),
    list([el("li", { class: "li set-sw" }, normalizeRow())], "set-list"),
    el("div", { class: "set-sliders" }, xf, sp),
    list([sleepRow], "set-list"),
    sectionTitle("Звук"),
    list([eqRow], "set-list"),
    sectionTitle("Это устройство"),
    list([meDev], "set-list"),
    sectionTitle("Эквалайзер на других устройствах"),
    eqDevBox,
    sectionTitle("Входы вне Telegram"),
    loginBox,
    inShell() ? sectionTitle("Приложение") : null,
    inShell() ? appBox : null,
    sectionTitle("Аккаунт"),
    list(acct, "set-list"),
    server.length ? sectionTitle("О сервере") : null,
    server.length ? list(server, "set-list") : null,
    el("p", { class: "note set-sync", text: settings.serverAvailable() ? "Настройки синхронизируются между вашими устройствами." : "Сервер пока не хранит настройки — они сохраняются только на этом устройстве." }),
  ]);
  const refresh = () => {
    // тема поменялась на другом устройстве; образцы перерисовываем только при смене (не терять фокус)
    const tp = themePref();
    modeGroup.set(tp.mode);
    if (tp.accent + tp.mode !== painted) paintAccents();
    setText(eqRow.querySelector(".li-supporting"), eqSup());
    setText(meDev.querySelector(".li-headline"), eq.device().name);
    const p = opts(); xf.set(p.crossfade); sp.set(p.speed);
    renderEqDevices();
  };
  const offS = settings.onSetting(() => { if (!body.isConnected) { offS(); return; } refresh(); });
  refresh();
  loadLogins();
  announce("Настройки");
}
