// Свои треки (медиатека, альбом, исполнитель, плейлист): тап по ряду = играть этот список с этого трека;
// ⋮, долгое нажатие и контекстное меню — действия трека; играющий трек подсвечен эквалайзером (aria-current).
// Ряды каталога (поиск/Deezer) сюда не относятся: там 30-секундный фрагмент и кнопка «Скачать» (track.js).
import { el, icon, pic, nTracks } from "./dom.js";
import { iconButton, listItem, list, sheet, dialog, snackbar, openMenu, button, setBusy, extendedFab } from "./ui.js";
import { reducedMotion } from "./motion.js";
import { cacheOn, isOfflineTrack, onOffline, downloadTracks, deleteOffline } from "./offline.js";
import { api } from "./api.js";
import { haptic } from "./tg.js";
import * as store from "./store.js";
import { go, shell } from "./shell.js";
import { player, onPlayer, current, playQueue, addNext, addToQueue, resume, unlockAudio, removeTrackIds } from "./player.js";
import { invalidateGenres } from "./libsort.js";
import { addToPlaylist } from "./playlists.js";
import { openNowPlaying } from "./fullplayer.js";
import { openLibAlbum } from "./screens/album.js";
import { openLibArtist } from "./screens/artist.js";
import { markLibraryDirty } from "./screens/library.js";
import { selectionFor } from "./select.js";

// ---------------------------------------------------------------- эквалайзер-глиф (статичен под reduced motion / на паузе)
export const eqGlyph = () => el("span", { class: "eq", "aria-hidden": "true" }, el("i"), el("i"), el("i"));

// ---------------------------------------------------------------- подсветка играющего трека во всех списках
const rows = new Set();
function syncRow(r) {
  const c = current();
  const on = !!c && c.id === r.id;
  r.li.classList.toggle("is-current", on);
  r.li.classList.toggle("is-offline", isOfflineTrack(r.id));
  r.li.classList.toggle("is-paused", on && player.desired !== "playing");
  if (on) r.main.setAttribute("aria-current", "true"); else r.main.removeAttribute("aria-current");
  const txt = on ? (player.desired === "playing" ? "Сейчас играет. " : "На паузе. ") : "";
  if (r.sr.textContent !== txt) r.sr.textContent = txt;
}
onPlayer((what) => {
  if (what === "time" || what === "now") return;
  for (const r of rows) {
    if (r.li.isConnected) { r.seen = true; syncRow(r); } else if (r.seen || performance.now() - r.t0 > 10000) rows.delete(r);
  }
});
// скачанные на устройство (оболочка) — значок у рядов
onOffline(() => { for (const r of rows) if (r.li.isConnected) r.li.classList.toggle("is-offline", isOfflineTrack(r.id)); });
function track(li, id) {
  const text = li.querySelector(".li-text");
  const sr = el("span", { class: "sr-only" });
  if (text) text.prepend(sr);
  const r = { li, id, main: li.querySelector(".li-main"), sr, seen: false, t0: performance.now() };
  rows.add(r);
  syncRow(r);
}

// Играть список с трека; повторный тап по играющему — открыть «Сейчас играет» (на паузе — продолжить)
export function startList(all, index, source) {
  const t = all[index];
  const tracks = all.filter((x) => !x._gone);
  index = Math.max(0, tracks.indexOf(t));
  const c = current();
  if (c && t && c.id === t.id && sameSource(source)) {
    if (player.desired !== "playing") resume(); else openNowPlaying();
    return;
  }
  haptic("light");
  playQueue(tracks, index, { source });
}
const sameSource = (s) => { const p = player.source; return !!s && !!p && s.kind === p.kind && String(s.id || "") === String(p.id || ""); };

let runSeq = 0;
// «Слушать» / «Перемешать» в шапке. getTracks — массив или Promise<массив>
export function playButtons(getTracks, source, { label = "Воспроизведение" } = {}) {
  const run = async (shuffle, btn) => {
    haptic("medium");
    const my = ++runSeq;
    let ts = typeof getTracks === "function" ? getTracks(shuffle) : getTracks;
    if (ts && typeof ts.then === "function") {
      unlockAudio(); // iOS: после await жеста уже нет
      setBusy(btn, true);
      try { ts = await ts; } catch (e) { if (my === runSeq) { haptic("error"); snackbar(e.message || "Не удалось загрузить треки", { error: true }); } return; } finally { setBusy(btn, false); }
      if (my !== runSeq || !btn.isConnected) return; // нажали другое или ушли с экрана
    }
    ts = (ts || []).filter((x) => !x._gone);
    if (!ts.length) { haptic("warning"); snackbar("Здесь пока нечего слушать"); return; }
    playQueue(ts, shuffle ? null : 0, { shuffle, source: typeof source === "function" ? source() : source });
  };
  const p = button({ label: "Слушать", icon: "play_arrow", variant: "filled", cls: "btn-play", onClick: () => run(false, p) });
  const s = button({ label: "Перемешать", icon: "shuffle", variant: "tonal", onClick: () => run(true, s) });
  return el("div", { class: "play-actions", role: "group", "aria-label": label }, p, s);
}

// ---------------------------------------------------------------- FAB «К текущему»
// Появляется, когда играющий трек есть в списке (ряд .ltrack.is-current уже на экране в DOM), но не виден в области
// прокрутки; нажатие — прокрутка к ряду по центру, короткая подсветка (motion-токены) и фокус на ряд.
// host — контейнер для FAB (position: relative/absolute), scroller — прокручиваемая область, root — где искать ряды.
export function currentFab(host, scroller, root = scroller, { cls = "" } = {}) {
  const fab = extendedFab({ icon: "my_location", label: "К текущему", onClick: () => jump() });
  fab.classList.add("fab-now");
  if (cls) fab.classList.add(cls);
  fab.hide(true);
  host.appendChild(fab);
  const row = () => root.querySelector(".ltrack.is-current");
  let raf = 0;
  const check = () => {
    raf = 0;
    const r = row();
    // ряда нет или он скрыт (лист очереди боком прячет «Играет сейчас») — FAB не нужен
    if (!r || !r.getClientRects().length || !scroller.isConnected || scroller.closest("[hidden]")) { fab.hide(true); return; }
    const a = r.getBoundingClientRect(), b = scroller.getBoundingClientRect();
    fab.hide(a.bottom > b.top + 24 && a.top < b.bottom - 24);
  };
  const kick = () => { if (!raf) raf = requestAnimationFrame(check); };
  scroller.addEventListener("scroll", kick, { passive: true });
  const off = onPlayer((what) => { if (!fab.isConnected) { off(); return; } if (what !== "time") kick(); });
  // ряды перерисовываются (подгрузка страниц, сортировка) — перепроверить
  const mo = new MutationObserver(kick);
  mo.observe(root, { childList: true, subtree: true });
  function jump() {
    const r = row();
    if (!r) return;
    haptic("light");
    // прокрутка именно контейнера списка (а не страницы); подгрузка страниц может сбить плавную — досводим
    const to = () => { const a = r.getBoundingClientRect(), s = scroller.getBoundingClientRect(); return scroller.scrollTop + (a.top - s.top) - (s.height - a.height) / 2; };
    scroller.scrollTo({ top: to(), behavior: reducedMotion() ? "auto" : "smooth" });
    setTimeout(() => { if (!r.isConnected) return; const a = r.getBoundingClientRect(), s = scroller.getBoundingClientRect(); if (a.top < s.top || a.bottom > s.bottom) scroller.scrollTo({ top: to() }); }, 700);
    r.classList.remove("is-flash"); void r.offsetWidth; r.classList.add("is-flash");
    setTimeout(() => r.classList.remove("is-flash"), 1200);
    const m = r.querySelector(".li-main");
    if (m) m.focus({ preventScroll: true });
  }
  fab.refresh = kick;
  fab.destroy = () => { off(); mo.disconnect(); fab.remove(); };
  kick();
  return fab;
}

// ---------------------------------------------------------------- ряд
// opts: thumb | num, tracks + index + source (что играть по тапу), menu: доп. пункты, onRemoved, deletable
export function libraryRow(t, opts = {}) {
  const { thumb = false, num, onRemoved, tracks, index = 0, source = null, sub } = opts;
  let row, suppress = false;
  const removeRow = () => {
    const fin = () => { row.remove(); onRemoved && onRemoved(); };
    const a = row.animate && !matchMedia("(prefers-reduced-motion: reduce)").matches
      ? row.animate([{ opacity: 1, height: row.offsetHeight + "px" }, { opacity: 0, height: "0px" }], { duration: 300, easing: "ease", fill: "both" }) : null;
    if (a) a.finished.then(fin, fin); else fin();
  };
  const more = iconButton("more_vert", `Действия: ${t.title}`, { cls: "li-more", onClick: (ev) => { ev.stopPropagation(); trackActions(t, more, { ...opts, removeRow, row }); } });
  more.setAttribute("aria-haspopup", "dialog");
  more.addEventListener("focus", () => more.setAttribute("aria-haspopup", shell.twoPane ? "menu" : "dialog"));
  const leading = num !== undefined
    ? el("span", { class: "li-lead li-lead-num" }, el("span", { class: "li-num", text: String(num) }), eqGlyph())
    : thumb ? el("span", { class: "li-lead li-lead-thumb" }, pic(t.cover, { cls: "li-thumb", ph: "music_note" }), eqGlyph())
      : el("span", { class: "li-lead li-lead-dot" }, eqGlyph());
  const playable = Array.isArray(tracks);
  const supporting = sub !== undefined ? sub : num !== undefined ? t.artist : [t.artist, t.album].filter(Boolean).join(" · ");
  row = listItem({
    leading, headline: t.title, supporting: (t.broken ? "Файл проблемный · " : "") + (t.owned === false ? "Не в медиатеке · " : "") + (supporting || ""),
    meta: t.duration, trailing: more, cls: "track ltrack" + (num !== undefined ? " is-numbered" : "") + (t.owned === false ? " is-foreign" : ""),
    onClick: playable ? () => { if (suppress) { suppress = false; return; } startList(tracks, index, source); } : undefined,
  });
  row.dataset.id = String(t.id);
  row.dataset.sel = String(t.item_id ?? t.id);
  // значки в supporting text: проблемный файл (error) и «скачан на устройство»
  const supEl = row.querySelector(".li-supporting");
  if (supEl) {
    if (cacheOn()) supEl.prepend(icon("download_done", { class: "li-off", fill: true }));
    if (t.broken) { row.classList.add("is-broken"); supEl.prepend(icon("error", { class: "li-broken", fill: true })); }
  }
  row._track = t;
  const main = row.querySelector(".li-main");
  // долгое нажатие (сенсор) → действия; контекстное меню (мышь/клавиатура Shift+F10) → действия
  let lp = 0, x0 = 0, y0 = 0;
  main.addEventListener("pointerdown", (ev) => {
    lastType = ev.pointerType;
    if (ev.pointerType === "mouse") return;
    x0 = ev.clientX; y0 = ev.clientY;
    clearTimeout(lp);
    lp = setTimeout(() => { suppress = true; haptic("medium"); const s = selectionFor(row); if (s) s.start(row); else trackActions(t, more, { ...opts, removeRow, row }); }, 520);
  });
  let lastType = "";
  const cancel = () => clearTimeout(lp);
  const release = () => { cancel(); if (suppress) setTimeout(() => { suppress = false; }, 350); };
  main.addEventListener("pointermove", (ev) => { if (Math.abs(ev.clientX - x0) > 10 || Math.abs(ev.clientY - y0) > 10) cancel(); });
  main.addEventListener("pointerup", release);
  main.addEventListener("pointercancel", release);
  main.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    cancel();
    if (suppress) return;
    if (lastType && lastType !== "mouse") { suppress = true; setTimeout(() => { suppress = false; }, 700); }
    trackActions(t, more, { ...opts, removeRow, row });
  });
  if (t.id !== undefined) track(row, t.id);
  return row;
}

// ---------------------------------------------------------------- действия трека: bottom sheet (compact) / меню (широкий экран)
export function trackActions(t, anchor, ctx = {}) {
  const items = [
    { icon: "queue_play_next", label: "Играть следующим", onClick: () => { const r = addNext(t); haptic("success"); snackbar(r === "play" ? `Играет: ${t.title}` : `Следующим: ${t.title}`); } },
    { icon: "add_to_queue", label: "В очередь", onClick: () => { const r = addToQueue(t); haptic("success"); snackbar(r === "play" ? `Играет: ${t.title}` : `В очереди: ${t.title}`); } },
    { icon: "playlist_add", label: "Добавить в плейлист…", onClick: () => addToPlaylist({ track: t }) },
    "divider",
    { icon: "person", label: "К исполнителю", supporting: t.artist, onClick: () => gotoArtist(t) },
    t.album ? { icon: "album", label: "К альбому", supporting: t.album, onClick: () => gotoAlbum(t) } : null,
    ctx.row && selectionFor(ctx.row) ? { icon: "checklist", label: "Выбрать", supporting: "Несколько треков сразу", onClick: () => selectionFor(ctx.row).start(ctx.row) } : null,
    { icon: "search", label: "Найти в каталоге", supporting: "Дискография и похожее", onClick: () => { go("search"); shell.roots.search.searchFor(t.artist); } },
    ...(ctx.menu ? ["divider", ...ctx.menu(t)] : []),
    cacheOn() && t.id !== undefined ? (isOfflineTrack(t.id)
      ? { icon: "file_download_off", label: "Удалить с устройства", supporting: "Скачанная копия для прослушивания без сети", onClick: () => unsaveOffline(t) }
      : { icon: "download_for_offline", label: "Скачать на устройство", supporting: "Слушать без сети", onClick: () => saveOffline([t], `«${t.title}»`) }) : null,
    t.owned !== false && t.id !== undefined ? { icon: "sync", label: "Перекачать трек", supporting: t.broken ? "Файл проблемный — скачать заново" : "Если файл битый или не тот", onClick: () => redownload(t) } : null,
    t.owned !== false && ctx.deletable !== false ? "divider" : null,
    t.owned !== false && ctx.deletable !== false ? { icon: "delete", label: "Удалить из медиатеки", danger: true, onClick: () => deleteTrack(t, ctx.removeRow) } : null,
  ].filter(Boolean);
  // разделитель не должен идти первым/последним/подряд
  const clean = items.filter((it, i, a) => it !== "divider" || (i > 0 && i < a.length - 1 && a[i - 1] !== "divider"));
  haptic("light");
  if (shell.twoPane && anchor && anchor.isConnected) {
    anchor.setAttribute("aria-expanded", "true");
    openMenu(anchor, clean, () => anchor.setAttribute("aria-expanded", "false"));
    return;
  }
  sheet({
    title: t.title, label: `Действия: ${t.title}`,
    build(body, close) {
      body.append(
        el("div", { class: "sheet-track" }, pic(t.cover, { cls: "li-thumb", ph: "music_note" }),
          el("div", { class: "li-text" }, el("span", { class: "li-headline", text: t.artist }), el("span", { class: "li-supporting", text: [t.album, t.duration].filter(Boolean).join(" · ") }))),
        list(clean.filter((it) => it !== "divider").map((it) => listItem({
          leading: icon(it.icon), headline: it.label, supporting: it.supporting, lines: 1, cls: it.danger ? "is-danger" : "",
          onClick: () => { close(); it.onClick(); },
        })), "sheet-list"));
    },
  });
}

async function deleteTrack(t, removeRow) {
  haptic("warning");
  const ok = await dialog({ icon: "delete", headline: "Удалить трек?", body: `${t.artist} — ${t.title} пропадёт из вашей медиатеки и плейлистов Jellyfin. В общих плейлистах трек останется.`,
    actions: [{ label: "Отмена", value: false }, { label: "Удалить", value: true, variant: "danger" }] });
  if (!ok) return;
  try {
    const r = await api("/delete", { method: "POST", body: { track_id: t.id } }) || {};
    haptic("success");
    snackbar(r.text ? r.text.replace(/^\S*\s*/, "") : "Удалено"); // «Отменить» не поддерживается сервером
    t._gone = true;
    if (removeRow) removeRow();
    libraryDeleted([t.id], { gone: typeof r.gone === "boolean" ? (r.gone ? [t.id] : []) : null });
  } catch (e) { haptic("error"); snackbar(e.message, { error: true }); }
}

// ---------------------------------------------------------------- «Скачать на устройство» (оболочка Windows 2.0)
export async function saveOffline(tracks, what = "") {
  try {
    const r = await downloadTracks(tracks);
    haptic(r.started ? "success" : "warning");
    const msg = !r.started ? "Уже на устройстве" : tracks.length === 1 && what ? `Скачиваю на устройство: ${what}` : `Скачиваю на устройство${what ? " " + what : ""}: ${nTracks(r.started)}`;
    snackbar(msg,
      { action: "Скачанное", onAction: () => import("./screens/offline.js").then((m) => m.openOffline()) });
  } catch (e) { haptic("error"); snackbar(`Не удалось скачать: ${e.message || "нет связи"}`, { error: true }); }
}
async function unsaveOffline(t) {
  try { await deleteOffline(t.id); haptic("light"); snackbar(`Удалено с устройства: ${t.title}`); } catch (e) { snackbar(e.message || "Не удалось удалить", { error: true }); }
}

// «Перекачать трек»: битый/неверный файл скачивается заново (POST /api/tracks/{id}/redownload), трек остаётся на месте
async function redownload(t) {
  const ok = await dialog({ icon: "sync", headline: "Перекачать трек?",
    body: `«${t.artist} — ${t.title}» скачается заново из источников, файл заменится, когда новый будет готов. В медиатеке и плейлистах трек останется.`,
    actions: [{ label: "Отмена", value: false }, { label: "Перекачать", value: true }] });
  if (!ok) return;
  try {
    const r = await api(`/tracks/${encodeURIComponent(t.id)}/redownload`, { method: "POST", body: {} }) || {};
    haptic("success");
    snackbar(r.message || r.text || `Перекачиваю: ${t.title}`, shell.tab === "downloads" ? {} : { action: "Загрузки", onAction: () => go("downloads") });
    store.kick();
  } catch (e) {
    haptic("error");
    snackbar(e.status === 404 && e.code !== "not_found" ? "Сервер пока не умеет перекачивать треки" : e.message || "Не удалось перекачать", { error: true });
  }
}

// ---------------------------------------------------------------- после удаления из медиатеки
// Одна точка для одиночного и массового удаления: очередь плеера, кэши медиатеки/жанров/указателей и открытые
// страницы (альбомы, исполнители, плейлисты, сама медиатека — через подписку onLibraryDeleted(set id-строк)).
const delSubs = new Set();
export const onLibraryDeleted = (fn) => { delSubs.add(fn); return () => delSubs.delete(fn); };
// gone — id, недоступные совсем (сервер: gone / gone_ids, поток 404) — только их убираем из очереди; остальные
// удалённые остаются в плейлистах (owned=false) и играют. Старый сервер без признака — все удалённые, кроме
// очереди, играющей плейлист (из плейлистов треки не удаляются).
export function libraryDeleted(ids, { gone = null } = {}) {
  const set = new Set([...ids].map(String)); // пусто — удалено неизвестно что (частичный ответ): только сброс кэшей
  const src = player.source;
  if (Array.isArray(gone)) { if (gone.length) removeTrackIds(gone); }
  else if (set.size && !(src && src.kind === "playlist")) removeTrackIds(set);
  invalidateIndex();
  invalidateGenres();
  markLibraryDirty();
  store.loadKeys();
  if (store.state.me) store.state.me.tracks = Math.max(0, (store.state.me.tracks || 0) - set.size);
  for (const fn of [...delSubs]) { try { fn(set); } catch (e) { console.error(e); } }
}
// убрать ряды удалённых треков в контейнере; их объекты помечаются _gone (не играть, не выбирать)
export function dropDeletedRows(root, set) {
  let n = 0;
  for (const r of [...root.querySelectorAll(".ltrack[data-id]")]) {
    if (!set.has(r.dataset.id)) continue;
    if (r._track) r._track._gone = true;
    r.remove();
    n++;
  }
  return n;
}

// ---------------------------------------------------------------- «К исполнителю» / «К альбому»
// Сервер пока не отдаёт ключи папок у трека (artist_key/album_key) — сопоставляем по названиям через списки медиатеки.
let artistsP = null, albumsP = null;
export function invalidateIndex() { artistsP = null; albumsP = null; }
store.on("tracks", () => { const n = store.state.keys.size; if (n !== lastKeys) { lastKeys = n; invalidateIndex(); } });
let lastKeys = -1;
const cf = (s) => String(s || "").trim().toLocaleLowerCase("ru");

let gotoSeq = 0;
export async function gotoArtist(t) {
  const my = ++gotoSeq;
  try {
    if (t.artist_key) { go("library"); openLibArtist(t.artist_key, { name: t.artist }, { from: "list" }); return; }
    artistsP = artistsP || api("/library/artists").catch((e) => { artistsP = null; throw e; });
    const r = await artistsP;
    if (my !== gotoSeq) return;
    const a = (r.items || []).find((x) => cf(x.name) === cf(t.artist));
    if (a) { go("library"); openLibArtist(a.key, { name: a.name, image: a.image }, { from: "list" }); return; }
  } catch { /* ниже — поиск в каталоге */ }
  snackbar(`«${t.artist}» нет в медиатеке — ищу в каталоге`);
  go("search");
  shell.roots.search.searchFor(t.artist);
}

export async function gotoAlbum(t) {
  const my = ++gotoSeq;
  try {
    if (t.artist_key && t.album_key) { go("library"); openLibAlbum(t.artist_key, t.album_key, { title: t.album, cover: t.cover }, { from: "list" }); return; }
    albumsP = albumsP || api("/library/albums").catch((e) => { albumsP = null; throw e; });
    const r = await albumsP;
    if (my !== gotoSeq) return;
    const items = r.items || [];
    const a = items.find((x) => cf(x.title) === cf(t.album) && cf(x.artist) === cf(t.artist)) || items.find((x) => cf(x.title) === cf(t.album));
    if (a) { go("library"); openLibAlbum(a.artist_key, a.key, { title: a.title, cover: a.cover }, { from: "list" }); return; }
  } catch { /* */ }
  snackbar(`Альбома «${t.album}» нет в медиатеке`);
}
