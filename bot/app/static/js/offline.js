// Кэш и офлайн оболочки Windows 2.0 (мост PiMusicNative, features.cache) — docs/design/APPS_API.md «Кэш и офлайн».
// Без моста всё выключено: cacheOn() = false, пункты «Скачать на устройство» не показываются.
// Состояние: список скачанных (offlineList), ход загрузок (offlineProgress/Done/Error) → подписчики onOffline().
import { bridge } from "./native.js";
import { api, absUrl } from "./api.js";

const N = () => bridge();
export const cacheOn = () => { const n = N(); return !!(n && n.features && n.features.cache && typeof n.cacheUrl === "function"); };

export function cacheUrl(id, url) {
  try { return N().cacheUrl(String(id), url || ""); } catch { return url || ""; }
}
export function cachePrefetch(id, url) {
  try { Promise.resolve(N().cachePrefetch(String(id), url)).catch(() => {}); } catch { /* */ }
}

// ---------------------------------------------------------------- состояние
const items = new Map(); // trackId → {trackId, size, addedAt, meta}
const progress = new Map(); // trackId → {downloaded, total}
const subs = new Set();
export const onOffline = (fn) => { subs.add(fn); return () => subs.delete(fn); };
const emit = (what, data) => { for (const fn of subs) { try { fn(what, data); } catch (e) { console.error(e); } } };
export const isOfflineTrack = (id) => items.has(String(id));
export const offlineState = (id) => (items.has(String(id)) ? "offline" : progress.has(String(id)) ? "downloading" : "none");
export const offlineProgress = (id) => progress.get(String(id)) || null;
export const offlineItems = () => [...items.values()];
// идущие загрузки: [{trackId, title, downloaded, total}]
export const offlinePending = () => [...progress.entries()].map(([trackId, p]) => ({ trackId, ...p }));

let inited = false;
export async function initOffline() {
  if (inited || !cacheOn()) return;
  inited = true;
  const n = N();
  n.on("offlineProgress", (p) => { if (!p) return; const k = String(p.trackId), was = progress.get(k) || {}; progress.set(k, { ...was, downloaded: p.downloaded || 0, total: p.total || null }); emit("progress", p); });
  n.on("offlineDone", (p) => { if (!p) return; progress.delete(String(p.trackId)); reload().then(() => emit("done", p)); });
  n.on("offlineError", (p) => { if (!p) return; progress.delete(String(p.trackId)); emit("error", p); });
  await reload();
}
export async function reload() {
  if (!cacheOn()) return [];
  try {
    const r = await N().offlineList();
    items.clear();
    for (const it of (r && r.items) || []) items.set(String(it.trackId), it);
  } catch { /* оболочка не ответила — оставляем прежнее */ }
  emit("list");
  return offlineItems();
}

// ---------------------------------------------------------------- действия
// Метаданные для экрана «Скачанное» и офлайн-воспроизведения (без сервера)
const metaOf = (t) => ({ id: t.id, title: t.title, artist: t.artist, album: t.album, duration: t.duration, seconds: t.seconds, cover: t.cover ? absUrl(t.cover) : null, artist_key: t.artist_key, album_key: t.album_key });

// Свежие подписанные ссылки потока (у треков из списков их может не быть или они устарели)
async function streams(tracks) {
  const need = tracks.filter((t) => !t.stream).map((t) => t.id);
  const map = new Map(tracks.filter((t) => t.stream).map((t) => [t.id, t.stream]));
  for (let k = 0; k < need.length; k += 500) {
    const r = await api("/tracks?ids=" + need.slice(k, k + 500).join(","));
    for (const t of r.items || []) if (t.stream) map.set(t.id, t.stream);
  }
  return map;
}

// Скачать треки на устройство → {started, skipped}; ход — события offlineProgress / offlineDone
export async function downloadTracks(tracks) {
  if (!cacheOn()) return { started: 0, skipped: tracks.length };
  const todo = tracks.filter((t) => t && t.id !== undefined && !isOfflineTrack(t.id) && !progress.has(String(t.id)));
  const urls = await streams(todo);
  let started = 0;
  for (const t of todo) {
    const u = urls.get(t.id);
    if (!u) continue;
    try {
      const r = await N().offlineDownload(String(t.id), absUrl(u), metaOf(t));
      if (r && r.offline) items.set(String(t.id), { trackId: String(t.id), size: 0, addedAt: Date.now(), meta: metaOf(t) });
      else progress.set(String(t.id), { downloaded: 0, total: null, title: t.title });
      started++;
    } catch (e) { emit("error", { trackId: String(t.id), message: e && e.message }); }
  }
  emit("list");
  return { started, skipped: tracks.length - started };
}

export async function deleteOffline(id) {
  if (!cacheOn()) return 0;
  const r = await N().offlineDelete(id === "all" ? "all" : String(id));
  if (id === "all") { items.clear(); progress.clear(); } else { items.delete(String(id)); progress.delete(String(id)); }
  emit("list");
  return (r && r.deleted) || 0;
}

export async function cacheInfo() { try { return await N().cacheInfo(); } catch { return null; } }
export async function cacheClear() { return N().cacheClear(); }
export async function setCacheLimit(bytes) { return N().setCacheLimit(bytes); }

export const fmtBytes = (b) => {
  b = Number(b) || 0;
  if (b >= 1073741824) return `${(b / 1073741824).toFixed(1).replace(".", ",")} ГБ`;
  if (b >= 1048576) return `${Math.round(b / 1048576)} МБ`;
  return `${Math.max(0, Math.round(b / 1024))} КБ`;
};
