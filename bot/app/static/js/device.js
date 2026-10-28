// Это устройство: стабильный id (localStorage), платформа и имя по умолчанию («Telegram · Android», «Windows»…).
import { tg } from "./tg.js";
import { storage } from "./dom.js";
import { nativePlatform } from "./native.js";

const IDKEY = "pimusic.device.id";

function uuid() {
  try { if (crypto.randomUUID) return crypto.randomUUID(); } catch { /* */ }
  const b = new Uint8Array(16);
  try { crypto.getRandomValues(b); } catch { for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256); }
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

let cached = "", persistent = false;
const ok = (id) => typeof id === "string" && /^[a-z0-9-]{8,40}$/.test(id);
function readCookie() { const m = document.cookie.match(/(?:^|; )pimusic_dev=([a-z0-9-]{8,40})/); return m ? m[1] : ""; }
// id для ключа eq.device.<id>: только [a-z0-9-]. Хранилище: localStorage → sessionStorage → cookie
export function deviceId() {
  if (cached) return cached;
  let id = storage.get(IDKEY, "");
  if (ok(id)) { persistent = true; return (cached = id); }
  try { id = sessionStorage.getItem(IDKEY) || ""; } catch { id = ""; }
  if (!ok(id)) id = readCookie();
  if (!ok(id)) id = uuid().toLowerCase();
  storage.set(IDKEY, id);
  persistent = ok(storage.get(IDKEY, ""));
  if (!persistent) {
    try { sessionStorage.setItem(IDKEY, id); } catch { /* */ }
    try { document.cookie = `pimusic_dev=${id}; max-age=${3600 * 24 * 400}; path=/; SameSite=Lax`; persistent = readCookie() === id; } catch { /* */ }
  }
  return (cached = id);
}
// id переживёт перезапуск — можно показывать устройство в списке у других
export const deviceIdPersistent = () => { deviceId(); return persistent; };

const ua = navigator.userAgent || "";
const os = () => (/Android/i.test(ua) ? "Android" : /iPhone|iPod/.test(ua) ? "iPhone" : /iPad/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1) ? "iPad"
  : /Windows/i.test(ua) ? "Windows" : /Mac OS/i.test(ua) ? "macOS" : /Linux/i.test(ua) ? "Linux" : "");

// web | pwa | windows | android | ios | macos | linux | telegram
export function platform() {
  if (nativePlatform()) return nativePlatform(); // мост оболочки знает точно
  if (window.__TAURI__ || window.__TAURI_INTERNALS__) return "windows";
  if (window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform()) return "android";
  if (tg && tg.initData) return "telegram";
  try { if (matchMedia("(display-mode: standalone)").matches || navigator.standalone) return "pwa"; } catch { /* */ }
  return "web";
}
// Платформа для входа на сервере: web | pwa | windows | android | ios | linux | macos
export function authPlatform() {
  if (nativePlatform()) return nativePlatform();
  const p = platform();
  if (p === "windows" || p === "android" || p === "pwa") return p;
  const o = os();
  return o === "iPhone" || o === "iPad" ? "ios" : o === "macOS" ? "macos" : o === "Linux" ? "linux" : o === "Android" ? "android" : o === "Windows" ? "windows" : "web";
}

export function defaultName() {
  const o = os();
  switch (platform()) {
    case "telegram": return `Telegram · ${o || (tg && tg.platform) || "устройство"}`;
    case "windows": return "Windows";
    case "android": return "Android";
    case "pwa": return `Приложение · ${o || "браузер"}`;
    default: return `Браузер${o ? " · " + o : ""}`;
  }
}

export const PLATFORM_ICON = { telegram: "send", windows: "desktop_windows", android: "phone_android", pwa: "install_desktop", web: "language" };
