// Тема: базовая схема M3 из seed-цвета + контент-схемы из обложек.
// Выбор пользователя (Настройки → Оформление) приоритетнее Telegram/системы:
//   mode — "system" (Telegram colorScheme, вне Telegram — prefers-color-scheme) | "light" | "dark";
//   accent — "dynamic" (янтарь + цвет из обложек на альбоме/исполнителе/мини-плеере) | id фиксированного seed.
// Хранится локально (сразу при запуске, до входа) и в настройках по ключу ui.theme (синхронизация между устройствами).
import { schemeFrom, seedSource, applyScheme, clearScheme, sourceFromUrl, rgbToOklch, hexToRgb, SEED } from "./color.js";
import { colorScheme, setChrome } from "./tg.js";
import * as settings from "./settings.js";

export const theme = { dark: true, scheme: null };
const targets = new Map(); // элемент → источник цвета (обложка)

export const MODES = [
  { key: "system", label: "Как в системе", icon: "brightness_auto" },
  { key: "light", label: "Светлая", icon: "light_mode" },
  { key: "dark", label: "Тёмная", icon: "dark_mode" },
];
// фиксированные seed-цвета (M3: тон схемы строится из seed, контраст ролей — по алгоритму)
export const ACCENTS = [
  { id: "dynamic", name: "Из обложки", seed: SEED },
  { id: "amber", name: "Янтарь", seed: SEED },
  { id: "coral", name: "Коралл", seed: "#E8794A" }, // hue ~45: не путается с error
  { id: "rose", name: "Роза", seed: "#D24D7E" },
  { id: "violet", name: "Сирень", seed: "#8A6BD1" },
  { id: "ocean", name: "Океан", seed: "#2F7FD0" },
  { id: "teal", name: "Бирюза", seed: "#1E9A8F" },
  { id: "green", name: "Зелень", seed: "#4E9A3A" },
];
const LKEY = "pimusic.theme.v1";
const SKEY = "ui.theme";
const clean = (v) => {
  const o = v && typeof v === "object" ? v : {};
  return { mode: MODES.some((m) => m.key === o.mode) ? o.mode : "system", accent: ACCENTS.some((a) => a.id === o.accent) ? o.accent : "dynamic" };
};
const readLocal = () => { try { return clean(JSON.parse(localStorage.getItem(LKEY) || "null")); } catch { return clean(null); } };
const writeLocal = (p) => { try { localStorage.setItem(LKEY, JSON.stringify(p)); } catch { /* приватный режим */ } };

// настройки вошедшего пользователя (синхронизированы) приоритетнее локального кэша
export function themePref() {
  const s = settings.get(SKEY, null);
  return s ? clean(s) : readLocal();
}
export function setThemePref(patch) {
  const p = clean({ ...themePref(), ...patch });
  writeLocal(p);
  try { settings.set(SKEY, p); } catch { /* до входа — только локально */ }
  applyTheme();
  return p;
}
// пришло с другого устройства / после входа — запомнить локально и применить
settings.onSetting((key, value) => { if (key === SKEY && value) { writeLocal(clean(value)); applyTheme(); } });

const dynamic = () => themePref().accent === "dynamic";
const seedOf = (id) => { const a = ACCENTS.find((x) => x.id === id) || ACCENTS[0]; return a.id === "dynamic" || a.id === "amber" ? seedSource() : rgbToOklch(hexToRgb(a.seed)); };
// цвет образца в палитре выбора: primary схемы этого seed в текущем режиме
export const accentPreview = (id) => schemeFrom(seedOf(id), theme.dark).primary;

export function applyTheme() {
  const pref = themePref();
  theme.dark = pref.mode === "system" ? colorScheme() === "dark" : pref.mode === "dark";
  const root = document.documentElement;
  root.dataset.theme = theme.dark ? "dark" : "light";
  root.style.colorScheme = theme.dark ? "dark" : "light";
  root.dataset.accent = pref.accent;
  theme.scheme = schemeFrom(seedOf(pref.accent), theme.dark);
  applyScheme(root, theme.scheme);
  for (const [node, src] of targets) {
    if (!node.isConnected) { targets.delete(node); continue; }
    if (pref.accent === "dynamic") { applyScheme(node, schemeFrom(src, theme.dark, "content")); node.classList.add("has-content-color"); }
    else { clearScheme(node); node.classList.remove("has-content-color"); } // фиксированный акцент — без цвета обложек
  }
  // цвет статус-бара: meta theme-color (браузер, PWA, мост оболочки следит за ним) и шапка/фон Telegram
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme.scheme.surface);
  setChrome(theme.scheme.surface, theme.scheme["surface-container"]);
}

// Контент-цвет из обложки; если CORS «испортил» холст или обложка серая — остаётся базовая схема.
// С фиксированным акцентом обложка запоминается (вернётся при «Из обложки»), но не красит экран.
export async function contentColor(node, url) {
  const src = await sourceFromUrl(url);
  if (!src) return false;
  targets.set(node, src);
  if (!dynamic()) return true;
  node.classList.add("has-content-color");
  applyScheme(node, schemeFrom(src, theme.dark, "content"));
  return true;
}

export function forgetContent(node) { targets.delete(node); }
