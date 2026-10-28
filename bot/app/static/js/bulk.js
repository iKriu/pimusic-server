// Массовые действия для режима выбора: медиатека, каталог/поиск, плейлист, очередь.
// Каталог: ряды несут ctx.body() → {token, kind:"track", i}; одной выдаче (токену) — один запрос kind:"tracks".
import { nTracks } from "./dom.js";
import { api } from "./api.js";
import { dialog, snackbar } from "./ui.js";
import { haptic } from "./tg.js";
import { requestDownload } from "./track.js";
import { playQueue, addMany } from "./player.js";
import { libraryDeleted, saveOffline } from "./libtrack.js";
import { cacheOn } from "./offline.js";
import { addToPlaylist, pickPlaylist, changed } from "./playlists.js";

// ---------------------------------------------------------------- каталог
// items: [{t, ctx}] → скачать (по желанию — сразу в плейлист: playlist_id; сервер добавит по мере загрузки)
export async function downloadItems(items, { playlist = null, quiet = false } = {}) {
  const groups = new Map();
  for (const it of items) {
    if (!it || !it.ctx || !it.ctx.body) continue;
    const b = it.ctx.body();
    const k = b.token;
    if (!groups.has(k)) groups.set(k, { ctx: it.ctx, idx: [], tracks: [] });
    const g = groups.get(k);
    if (b.kind === "track" && Number.isInteger(b.i)) g.idx.push(b.i);
    g.tracks.push(it.t);
  }
  let total = 0;
  for (const g of groups.values()) {
    if (!g.idx.length) continue;
    const one = g.idx.length === 1;
    const r = await requestDownload({
      body: () => {
        const b = g.ctx.body();
        const base = one ? { token: b.token, kind: "track", i: g.idx[0] } : { token: b.token, kind: "tracks", idx: g.idx };
        return playlist ? { ...base, playlist_id: playlist.id } : base;
      },
      refresh: g.ctx.refresh, tracks: g.tracks, signal: g.ctx.signal,
      message: quiet ? () => "" : playlist ? (res) => `Скачиваю и добавлю в «${playlist.name}»: ${nTracks(res.queued || g.idx.length)}` : undefined,
    });
    if (r) total += r.queued || g.idx.length;
  }
  if (playlist && total) changed(playlist.id);
  return total;
}

export async function downloadToPlaylist(items, opts = {}) {
  const p = await pickPlaylist({ note: `${nTracks(items.length)} скачаются и появятся в плейлисте по мере загрузки.`, ...opts });
  if (!p) return null;
  await downloadItems(items, { playlist: p });
  return p;
}

// выбор снимается только при успехе — при ошибке остаётся, чтобы повторить
export const catalogActions = (items, ctl) => [
  { icon: "download", label: "Скачать", run: async (its) => { if (await downloadItems(its)) ctl.stop(); } },
  { icon: "playlist_add", label: "Скачать в плейлист", run: async (its) => { const p = await pickPlaylist({ note: `${nTracks(its.length)} скачаются и появятся в плейлисте по мере загрузки.` }); if (p && await downloadItems(its, { playlist: p })) ctl.stop(); } },
];

// ---------------------------------------------------------------- медиатека
// Массовое удаление: POST /api/library/delete {track_ids} (до 500 за раз); старый сервер — по одному /api/delete.
// Возвращает {deleted, failed, ids, gone} — ids точно удалённых (для частичного успеха); gone — недоступные совсем
// (новый сервер: deleted_ids / gone_ids; старый — gone: null, решает libraryDeleted)
export async function deleteTracks(tracks) {
  const ids = tracks.map((t) => t.id);
  let deleted = 0, failed = 0, legacy = false;
  const done = [];
  let gone = [];
  for (let k = 0; k < ids.length && !legacy; k += 500) {
    const chunk = ids.slice(k, k + 500);
    try {
      const r = await api("/library/delete", { method: "POST", body: { track_ids: chunk } });
      const d = Number(r.deleted ?? chunk.length);
      deleted += d; failed += Number(r.skipped || 0);
      if (Array.isArray(r.deleted_ids)) done.push(...r.deleted_ids);
      else if (!r.skipped) done.push(...chunk); // без пропусков — удалены все из пачки
      if (gone && Array.isArray(r.gone_ids)) gone.push(...r.gone_ids); else gone = null;
    } catch (e) {
      if (e.status === 404 && e.code !== "not_found" && k === 0) { legacy = true; break; }
      failed += chunk.length;
    }
  }
  if (legacy) {
    for (const id of ids) {
      try {
        const r = await api("/delete", { method: "POST", body: { track_id: id } }) || {};
        deleted++; done.push(id);
        if (gone && typeof r.gone === "boolean") { if (r.gone) gone.push(id); } else gone = null;
      } catch { failed++; }
    }
  }
  return { deleted, failed, ids: done, gone };
}

export async function confirmDelete(tracks) {
  haptic("warning");
  return dialog({ icon: "delete", headline: `Удалить ${nTracks(tracks.length)}?`,
    body: "Треки пропадут из вашей медиатеки и плейлистов Jellyfin. В общих плейлистах они останутся.",
    actions: [{ label: "Отмена", value: false }, { label: "Удалить", value: true, variant: "danger" }] });
}

// ряды удалённых убирают сами экраны (подписка onLibraryDeleted); onDeleted(tracks) — дополнительно, по желанию
export const libraryActions = (items, ctl, { source = null, onDeleted } = {}) => [
  { icon: "play_arrow", label: "Слушать", run: (ts) => { playQueue(ts, 0, { source }); ctl.stop(); } },
  { icon: "queue_play_next", label: "Следующими", run: (ts) => { addMany(ts, { next: true }); snackbar(`Следующими: ${nTracks(ts.length)}`); ctl.stop(); } },
  { icon: "add_to_queue", label: "В очередь", run: (ts) => { addMany(ts); snackbar(`В очереди: ${nTracks(ts.length)}`); ctl.stop(); } },
  { icon: "playlist_add", label: "В плейлист", run: (ts) => { addToPlaylist({ add: { track_ids: ts.map((t) => t.id) }, title: nTracks(ts.length) }); ctl.stop(); } },
  cacheOn() ? { icon: "download_for_offline", label: "На устройство", run: async (ts) => { await saveOffline(ts); ctl.stop(); } } : null,
  { icon: "delete", label: "Удалить", danger: true, run: async (ts) => {
    const own = ts.filter((t) => t.owned !== false);
    if (!own.length) { snackbar("Этих треков нет в вашей медиатеке"); return; }
    if (!(await confirmDelete(own))) return;
    let r;
    try { r = await deleteTracks(own); } catch (e) { haptic("error"); snackbar(e.message, { error: true }); return; } // выбор остаётся
    const done = new Set(r.ids.map(String));
    const removed = own.filter((t) => done.has(String(t.id)));
    for (const t of removed) t._gone = true;
    // очередь, кэши и открытые страницы (медиатека, альбомы, исполнители, плейлисты) — через одну точку
    if (r.deleted) libraryDeleted([...done], { gone: r.gone });
    if (r.failed && !r.deleted) { haptic("error"); snackbar(`Не удалось удалить ${nTracks(r.failed)}`, { error: true }); return; } // выбор остаётся
    haptic(r.failed ? "warning" : "success");
    snackbar(r.failed ? `Удалено: ${r.deleted} из ${own.length} · не удалось: ${r.failed}` : `Удалено: ${nTracks(r.deleted)}`, { error: !!r.failed });
    if (onDeleted && removed.length) onDeleted(removed);
    if (r.failed) ctl.refresh(); else ctl.stop();
  } },
];
