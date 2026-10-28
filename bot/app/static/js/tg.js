// Интеграция с Telegram.WebApp: тема, viewport, safe areas, кнопки, haptics.
// Все вызовы защищены проверкой версии: старые клиенты просто не получают функцию.

export const tg = (window.Telegram && window.Telegram.WebApp) || null;

const atLeast = (v) => { try { return !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(v)); } catch { return false; } };

export function colorScheme() {
  // вне Telegram (PWA, браузер) у WebApp всегда «light» — берём системную тему
  if (tg && tg.initData && (tg.colorScheme === "dark" || tg.colorScheme === "light")) return tg.colorScheme;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function haptic(kind = "light") {
  try {
    if (!tg || !tg.HapticFeedback || !atLeast("6.1")) return;
    if (kind === "success" || kind === "error" || kind === "warning") tg.HapticFeedback.notificationOccurred(kind);
    else if (kind === "select") tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.impactOccurred(kind);
  } catch { /* нет поддержки */ }
}

// Отступы: safe area устройства + content safe area (fullscreen-режим Telegram с его кнопками сверху)
function applyInsets() {
  const root = document.documentElement.style;
  const sa = (tg && tg.safeAreaInset) || {};
  const ca = (tg && tg.contentSafeAreaInset) || {};
  for (const side of ["top", "right", "bottom", "left"]) {
    root.setProperty(`--tg-inset-${side}`, ((sa[side] || 0) + (ca[side] || 0)) + "px");
  }
}

function applyViewport() {
  const root = document.documentElement.style;
  const h = tg && (tg.viewportStableHeight || tg.viewportHeight);
  if (h && h > 100) root.setProperty("--app-h", h + "px");
  else root.removeProperty("--app-h");
}

export function initTelegram({ onTheme, onViewport, onBack, onSettings }) {
  applyInsets();
  if (!tg) return;
  try { tg.ready(); } catch { /* */ }
  try { tg.expand(); } catch { /* */ }
  try { if (atLeast("7.7") && tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch { /* */ }
  applyViewport();
  const on = (ev, fn) => { try { tg.onEvent(ev, fn); } catch { /* */ } };
  on("themeChanged", () => onTheme && onTheme());
  on("viewportChanged", (e) => { if (!e || e.isStateStable !== false) { applyViewport(); onViewport && onViewport(); } });
  on("safeAreaChanged", () => { applyInsets(); onViewport && onViewport(); });
  on("contentSafeAreaChanged", () => { applyInsets(); onViewport && onViewport(); });
  on("fullscreenChanged", () => { applyInsets(); onViewport && onViewport(); });
  if (tg.BackButton && atLeast("6.1")) tg.BackButton.onClick(() => onBack && onBack());
  if (tg.SettingsButton && atLeast("7.0")) {
    try { tg.SettingsButton.onClick(() => onSettings && onSettings()); tg.SettingsButton.show(); } catch { /* */ }
  }
}

export function setBackButton(visible) {
  if (!tg || !tg.BackButton || !atLeast("6.1")) return;
  try { visible ? tg.BackButton.show() : tg.BackButton.hide(); } catch { /* */ }
}

// Цвета шапки/фона Telegram под M3-поверхность
export function setChrome(surface, bar) {
  if (!tg) return;
  try { if (atLeast("6.1")) { tg.setHeaderColor(surface); tg.setBackgroundColor(surface); } } catch { /* */ }
  try { if (atLeast("7.10") && tg.setBottomBarColor) tg.setBottomBarColor(bar || surface); } catch { /* */ }
}

// MainButton дублирует главное действие страницы на compact
let mainHandler = null;
export function setMainButton(cfg) {
  if (!tg || !tg.MainButton) return false;
  const mb = tg.MainButton;
  try {
    if (mainHandler) { mb.offClick(mainHandler); mainHandler = null; }
    if (!cfg) { mb.hideProgress && mb.hideProgress(); mb.hide(); return true; }
    mainHandler = cfg.onClick;
    mb.onClick(mainHandler);
    const params = { text: cfg.text, is_visible: true, is_active: !cfg.disabled };
    if (cfg.color) params.color = cfg.color;
    if (cfg.textColor) params.text_color = cfg.textColor;
    mb.setParams(params);
    if (cfg.progress && mb.showProgress) mb.showProgress(false); else if (mb.hideProgress) mb.hideProgress();
    return true;
  } catch { return false; }
}

export function openExternal(url) {
  try { if (tg && tg.openLink) { tg.openLink(url); return; } } catch { /* */ }
  window.open(url, "_blank", "noopener");
}

// Ссылка t.me внутри Telegram (шаринг: t.me/share/url?…); вне Telegram — новая вкладка
export function openTelegramLink(url) {
  try { if (tg && tg.openTelegramLink && atLeast("6.1")) { tg.openTelegramLink(url); return true; } } catch { /* */ }
  return false;
}

// Параметр запуска t.me/<bot>/app?startapp=… (Direct Link Mini App)
export function startParam() {
  try { return (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || ""; } catch { return ""; }
}

export function closeApp() {
  try { if (tg) { tg.close(); return; } } catch { /* */ }
  history.back();
}

export function nativeConfirm(text) {
  return new Promise((resolve) => {
    if (tg && tg.showConfirm && atLeast("6.2")) { try { tg.showConfirm(text, (ok) => resolve(!!ok)); return; } catch { /* */ } }
    resolve(window.confirm(text));
  });
}

// Подтверждение закрытия: пока играет музыка, случайный свайп вниз не закрывает мини-приложение
let closingOn = false;
export function setClosingConfirmation(on) {
  on = !!on;
  if (!tg || closingOn === on || !atLeast("6.2")) return;
  try {
    if (on && tg.enableClosingConfirmation) tg.enableClosingConfirmation();
    else if (!on && tg.disableClosingConfirmation) tg.disableClosingConfirmation();
    closingOn = on;
  } catch { /* старый клиент */ }
}
