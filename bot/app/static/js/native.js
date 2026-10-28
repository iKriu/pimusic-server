// Нативные оболочки (Windows · Tauri, Android · Capacitor): мост window.PiMusicNative из apps/shared/bridge.js.
// Без моста (браузер, Telegram) всё работает как раньше — каждое обращение через ?. и проверку.
export const bridge = () => (typeof window !== "undefined" && window.PiMusicNative) || null;
export const inShell = () => !!bridge();
export const nativePlatform = () => { const n = bridge(); return n && (n.platform === "windows" || n.platform === "android") ? n.platform : ""; };
export const nativeFeatures = () => (bridge() && bridge().features) || {};

const subs = new Set();
export const onNativeSettings = (fn) => { subs.add(fn); return () => subs.delete(fn); };

// back() — общий «назад» оболочки (shell.back): закрывает меню/лист/диалог/плеер/страницу; true — поглощено
let nativeInited = false;
export function initNative({ back } = {}) {
  const n = bridge();
  if (!n || typeof n.on !== "function" || nativeInited) return; // повторный вызов не плодит подписки
  nativeInited = true;
  try {
    n.on("back", () => (back ? back() === true : false));
    n.on("settings", (s) => { for (const fn of subs) { try { fn(s); } catch (e) { console.error(e); } } });
  } catch (e) { console.warn("PiMusicNative:", e && e.message); }
}

export async function getNativeSettings() { try { return (await bridge()?.getSettings?.()) || null; } catch { return null; } }
export async function setNativeSettings(patch) { try { await bridge()?.setSettings?.(patch); return true; } catch { return false; } }
export async function openServerSettings() { try { await bridge()?.openServerSettings?.(); return true; } catch { return false; } }
export async function nativeInfo() { try { return (await bridge()?.getInfo?.()) || bridge(); } catch { return bridge(); } }
