// Настройки по ключам: мгновенно — в localStorage (отдельно для каждого логина), в фоне — на сервер
// (/api/settings, PUT по ключу с base). Ключи не затирают друг друга между устройствами.
//   200 → base = updated_at сервера; dirty снимаем, только если локально за это время ничего не менялось;
//   409 conflict → current с сервера выигрывает (current: null — ключа нет: base = 0 и повтор);
//   409 limit / 400 / 413 → ключ «паркуется» (не долбим сервер), остаётся локальным;
//   сеть → повтор позже. Нет сервера настроек (404) — работаем только локально.
import { api } from "./api.js";

const KEY_RE = /^[a-z0-9._-]{1,80}$/;
let login = "";
let items = {}; // key → { value, updated_at, base, dirty, parked }
let serverOk = true;
const subs = new Set();
const lkey = () => "pimusic.settings.v1:" + login;

function readStore() {
  let v = null;
  try { v = JSON.parse(localStorage.getItem(lkey()) || "null"); } catch { v = null; }
  const out = {};
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, it] of Object.entries(v)) {
      if (!KEY_RE.test(k) || !it || typeof it !== "object" || !("value" in it)) continue;
      out[k] = { value: it.value, updated_at: Number(it.updated_at) || 0, base: Number(it.base) || 0, dirty: !!it.dirty, parked: !!it.parked };
    }
  }
  return out;
}
function persist() { if (!login) return; try { localStorage.setItem(lkey(), JSON.stringify(items)); } catch { /* квота */ } }

// Хранилище — своё у каждого пользователя (на одном устройстве могут входить разные логины)
export function setLogin(l) {
  login = String(l || "");
  try { localStorage.removeItem("pimusic.settings.v1"); } catch { /* старый общий ключ */ }
  items = readStore();
}

export function onSetting(fn) { subs.add(fn); return () => subs.delete(fn); }
function emit(key, remote) { for (const fn of subs) { try { fn(key, items[key] ? items[key].value : undefined, remote); } catch (e) { console.error(e); } } }

export const get = (key, fallback = null) => (items[key] && items[key].value !== undefined && items[key].value !== null ? items[key].value : fallback);
export const keys = (prefix = "") => Object.keys(items).filter((k) => k.startsWith(prefix) && items[k].value !== null && items[k].value !== undefined);
export const updatedAt = (key) => (items[key] ? items[key].updated_at || 0 : 0);

export function set(key, value) {
  if (!KEY_RE.test(key)) throw new Error("bad key " + key);
  const cur = items[key] || {};
  items[key] = { value, updated_at: Date.now(), base: cur.base || 0, dirty: true, parked: false };
  persist();
  emit(key, false);
  schedule();
}

let pushT = 0;
function schedule(ms = 1200) { clearTimeout(pushT); pushT = setTimeout(pushAll, ms); }

// один поток отправки; если во время отправки появились изменения — ещё один проход
let pushing = false, rerun = false;
async function pushAll({ keepalive = false } = {}) {
  if (!serverOk || !login) return;
  if (pushing) { rerun = true; return; }
  pushing = true;
  let retryLater = false;
  try {
    for (const key of Object.keys(items)) {
      const it = items[key];
      if (!it || !it.dirty || it.parked) continue;
      const sent = it.updated_at;
      try {
        const r = await api("/settings/" + encodeURIComponent(key), { method: "PUT", body: { value: it.value, base: it.base || undefined }, keepalive });
        const cur = items[key];
        if (!cur) continue;
        cur.base = Number(r.updated_at) || cur.base;
        if (cur.updated_at === sent) cur.dirty = false;
      } catch (e) {
        const cur = items[key];
        if (e.status === 404 && e.code !== "not_found") { serverOk = false; return; } // старый сервер: только локально
        if (e.status === 409 && e.code === "conflict") {
          const c = e.data && "current" in e.data ? e.data.current : undefined;
          if (c === null) { cur.base = 0; rerun = true; } // ключа на сервере нет — пишем заново без base
          else if (c && typeof c === "object") { items[key] = { value: c.value, updated_at: Number(c.updated_at) || 0, base: Number(c.updated_at) || 0, dirty: false, parked: false }; emit(key, true); }
          else { await adoptFromServer(key); }
        } else if (e.status === 409 || e.status === 400 || e.status === 413) {
          cur.parked = true; // лимит ключей / размер / формат — не повторяем бесконечно
          console.warn("setting parked", key, e.code);
        } else if (e.status === 0 || e.status >= 500 || e.status === 429) retryLater = true;
      }
    }
  } finally {
    pushing = false;
    persist();
  }
  if (rerun) { rerun = false; pushAll(); } else if (retryLater) schedule(15000);
}

async function adoptFromServer(key) {
  try {
    const r = await api("/settings");
    const s = r.items && r.items[key];
    if (s) { items[key] = { value: s.value, updated_at: s.updated_at, base: s.updated_at, dirty: false, parked: false }; emit(key, true); }
  } catch { /* */ }
}

// Забрать изменения с сервера (старт, возврат на вкладку, раз в минуту)
export async function sync() {
  if (!serverOk || !login) return false;
  let r;
  try { r = await api("/settings"); } catch (e) {
    if (e.status === 404) serverOk = false;
    return false;
  }
  const remote = (r && r.items && typeof r.items === "object") ? r.items : {};
  for (const [key, s] of Object.entries(remote)) {
    if (!KEY_RE.test(key) || !s || typeof s !== "object") continue;
    const cur = items[key];
    if (!cur || (!cur.dirty && (Number(s.updated_at) || 0) > (cur.base || 0))) {
      items[key] = { value: s.value, updated_at: Number(s.updated_at) || 0, base: Number(s.updated_at) || 0, dirty: false, parked: false };
      emit(key, true);
    }
  }
  persist();
  if (Object.values(items).some((x) => x.dirty && !x.parked)) pushAll();
  return true;
}
export const serverAvailable = () => serverOk;

let timer = 0, started = false;
export function startSync() {
  sync();
  if (started) return;
  started = true;
  timer = setInterval(() => { if (document.visibilityState === "visible") sync(); }, 60000);
  // уходим со страницы — отправить несохранённое (keepalive переживает закрытие вкладки)
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") sync(); else { clearTimeout(pushT); pushAll({ keepalive: true }); } });
}
export const _timer = () => timer;
