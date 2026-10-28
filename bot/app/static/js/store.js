// Состояние приложения: пользователь, ключи медиатеки, состояние каждого трека, опрос /api/activity.
import { api, isAbort } from "./api.js";
import { storage } from "./dom.js";
import { haptic } from "./tg.js";

const listeners = new Map();
export function on(ev, fn) {
  if (!listeners.has(ev)) listeners.set(ev, new Set());
  listeners.get(ev).add(fn);
  return () => listeners.get(ev).delete(fn);
}
function emit(ev, data) { for (const fn of listeners.get(ev) || []) { try { fn(data); } catch (e) { console.error(e); } } }

export const state = {
  me: null,
  keys: new Set(),
  isrc: new Set(),
  keysLoaded: false,
  activity: null,
  imports: [],
  // key → "queued" | "active" | "error"  (owned вычисляется из keys)
  pending: new Map(),
  labels: new Map(), // key → «Артист — Название» для сопоставления с active
};

// ---------------------------------------------------------------- медиатека
export function isOwned(t) {
  return !!t && ((t.key && state.keys.has(t.key)) || (t.isrc && state.isrc.has(t.isrc)));
}
export function trackState(t) {
  if (!t) return "none";
  if (isOwned(t)) return "owned";
  return (t.key && state.pending.get(t.key)) || "none";
}

let keysInflight = null;
export function loadKeys() {
  if (keysInflight) return keysInflight;
  keysInflight = api("/library/keys").then((r) => {
    state.keys = new Set(r.keys || []);
    state.isrc = new Set(r.isrc || []);
    state.keysLoaded = true;
    // завершённые загрузки → «в медиатеке»
    let finished = 0;
    for (const k of [...state.pending.keys()]) {
      if (state.keys.has(k)) { state.pending.delete(k); finished++; }
    }
    if (finished) haptic("success");
    emit("tracks");
  }).catch((e) => { if (!isAbort(e)) console.warn("keys", e.message); })
    .finally(() => { keysInflight = null; });
  return keysInflight;
}

export function markQueued(tracks) {
  for (const t of tracks) {
    if (!t || !t.key || isOwned(t)) continue;
    state.pending.set(t.key, "queued");
    state.labels.set(t.key, `${t.artist} — ${t.title}`.toLowerCase());
  }
  emit("tracks");
  kick();
}
export function markFailed(tracks) {
  for (const t of tracks) if (t && t.key) state.pending.set(t.key, "error");
  emit("tracks");
}
export function forget(key) { state.pending.delete(key); emit("tracks"); }

// ---------------------------------------------------------------- история поиска
const HKEY = "pimusic.history.v1";
export function history() { const h = storage.get(HKEY, []); return Array.isArray(h) ? h.filter((x) => typeof x === "string").slice(0, 8) : []; }
export function pushHistory(q) {
  q = (q || "").trim();
  if (!q) return;
  const h = history().filter((x) => x.toLowerCase() !== q.toLowerCase());
  h.unshift(q);
  storage.set(HKEY, h.slice(0, 8));
}
export function removeHistory(q) { storage.set(HKEY, history().filter((x) => x !== q)); }
export function clearHistory() { storage.set(HKEY, []); }

// ---------------------------------------------------------------- опрос активности
// Быстро (3 с), пока что-то качается или открыт экран загрузок; иначе не опрашиваем вовсе.
// Только при видимой странице (visibilitychange).
let timer = null;
let watchers = 0;
let lastSig = "";
let lastJobs = 0;

export function watch() {
  watchers++;
  kick();
  return () => { watchers = Math.max(0, watchers - 1); };
}

function needFast() {
  const a = state.activity;
  return watchers > 0 || state.pending.size > 0 || (a && (a.active.length || a.jobs.length)) || state.imports.some((i) => i.state === "running");
}

export function kick() {
  clearTimeout(timer);
  timer = setTimeout(poll, 0);
}

async function poll() {
  clearTimeout(timer);
  if (document.hidden) return; // продолжим по visibilitychange
  try {
    const [act, imp] = await Promise.all([api("/activity"), api("/import").catch(() => ({ imports: state.imports }))]);
    state.activity = act;
    state.imports = imp.imports || [];
    const sig = (act.log[0] ? act.log[0].ts + act.log[0].text : "") + "|" + act.log.length;
    const jobs = act.jobs.length + act.active.length;
    // новое событие в журнале или завершилось задание → освежаем ключи медиатеки
    if ((sig !== lastSig && lastSig) || jobs < lastJobs) loadKeys();
    lastSig = sig;
    lastJobs = jobs;
    // что качается прямо сейчас
    const activeText = act.active.map((s) => s.toLowerCase()).join("\n");
    let changed = false;
    for (const [k, st] of state.pending) {
      if (st === "error") continue;
      const label = state.labels.get(k);
      const now = label && activeText.includes(label) ? "active" : "queued";
      if (now !== st) { state.pending.set(k, now); changed = true; }
    }
    // все задания закончились, а трек так и не появился → «не удалось»
    if (!jobs && state.pending.size) {
      await loadKeys();
      for (const [k, st] of state.pending) if (st !== "error" && !state.keys.has(k)) { state.pending.set(k, "error"); changed = true; }
      if (changed) haptic("warning");
    }
    if (changed) emit("tracks");
    emit("activity", act);
  } catch (e) {
    if (!isAbort(e)) emit("activity-error", e);
  }
  if (needFast()) timer = setTimeout(poll, 3000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden && (needFast() || watchers)) kick(); });

export function activeCount() {
  const a = state.activity;
  return a ? a.active.length + a.jobs.length : 0;
}
