// Страница исполнителя: круглое фото (морф в скруглённый квадрат), «N слушателей»,
// connected button group «Популярное | Дискография | Все треки», карусель дискографии, extended FAB «Скачать всё».
import { el, mount, nTracks, nAlbums, fmtFans, initial, plural, pic } from "../dom.js";
import {
  button, connectedGroup, list, grid, carousel, card, extendedFab, dialog, emptyState, errorState,
  skeletonList, loadingState, sectionTitle, setBusy, snackbar,
} from "../ui.js";
import { flexHeader, trackScroll } from "../header.js";
import { trackRow, requestDownload } from "../track.js";
import { contentColor } from "../theme.js";
import { open, syncMainButton } from "../shell.js";
import { sharedAxis } from "../motion.js";
import { isAbort } from "../api.js";
import { haptic } from "../tg.js";
import { albumCard } from "./search.js";
import { openLibAlbum, moreMenu } from "./album.js";
import { libraryRow, playButtons, onLibraryDeleted, dropDeletedRows, currentFab, saveOffline } from "../libtrack.js";
import { cacheOn } from "../offline.js";
import { iconButton } from "../ui.js";
import { addToPlaylist } from "../playlists.js";
import { downloadToPlaylist } from "../bulk.js";
import { selectable } from "../select.js";
import { libraryActions, catalogActions } from "../bulk.js";

export function openCatalogArtist(hint, { from = "detail", origin = null } = {}) {
  if (!hint || !hint.id) return;
  open((page) => buildCatalogArtist(page, hint), { kind: "artist", from, origin });
}

function buildCatalogArtist(page, hint) {
  selectable(page.el, { rows: () => [...page.el.querySelectorAll(".ctrack")].filter((r) => !r.closest("[hidden]")), key: (r) => r.dataset.sel + "@" + (r.closest(".list") ? [...page.el.querySelectorAll(".list")].indexOf(r.closest(".list")) : 0),
    item: (r) => ({ t: r._track, ctx: r._ctx }), actions: catalogActions });
  const H = flexHeader(page, { kind: "artist", cover: hint.picture, ph: initial(hint.name), overline: "Исполнитель", title: hint.name || "Исполнитель" });
  const body = el("div", { class: "page-body" });
  page.scroll.append(H.el, body);
  if (hint.picture) contentColor(page.el, hint.picture).then(syncMainButton);
  const path = "/catalog/artist?id=" + encodeURIComponent(hint.id);
  const allPath = "/catalog/artist/tracks?id=" + encodeURIComponent(hint.id);
  const refresh = async () => { const d = await page.sc.get(path); page.token = d.token; };
  let all = null; // {token, tracks, albums, capped}
  let allPromise = null;
  const loadAll = () => {
    if (!allPromise) {
      allPromise = page.sc.get(allPath).then((r) => { all = r; return r; }).catch((e) => { allPromise = null; throw e; });
    }
    return allPromise;
  };
  const refreshAll = async () => { const r = await page.sc.get(allPath); all.token = r.token; };

  // FAB «Скачать всё» (прячется при прокрутке вниз)
  const fab = extendedFab({ icon: "download", label: "Скачать всё", onClick: () => downloadEverything() });
  fab.hidden = true;
  page.el.appendChild(fab);
  // MainButton — только пока FAB спрятан (не дублируем видимое действие)
  const fabHide = (h) => { fab.hide(h); page.setMain(h && !fab.hidden ? { text: "Скачать всё", onClick: () => downloadEverything() } : null); };
  trackScroll(page.el, page.scroll, (y, dir) => { if (dir > 0 && y > 80) fabHide(true); else if (dir < 0) fabHide(false); fab.collapse(y > 240); });

  let name = hint.name || "";
  async function downloadEverything() {
    haptic("warning");
    const ok = await dialog({
      icon: "download", headline: `Скачать всё «${name}»?`,
      body: "Соберу треки со всех альбомов без повторов и поставлю в очередь. Это может занять много времени и места.",
      actions: [{ label: "Отмена", value: false }, { label: "Скачать всё", value: true, variant: "filled" }],
    });
    if (!ok) return;
    fab.classList.add("is-busy");
    setBusy(fab, true);
    try {
      const r = await loadAll();
      if (!r.tracks.length) { haptic("error"); return; }
      await requestDownload({ body: () => ({ token: all.token, kind: "all" }), refresh: refreshAll, tracks: r.tracks, signal: page.sc.signal });
    } catch (e) {
      if (!isAbort(e)) snackbar(e.message, { error: true });
    } finally {
      fab.classList.remove("is-busy");
      setBusy(fab, false);
    }
  }

  const load = async () => {
    mount(body, skeletonList(6, { thumb: true }));
    let ar;
    try { ar = await page.sc.get(path); } catch (e) {
      if (isAbort(e)) return;
      mount(body, errorState(e, load));
      return;
    }
    page.token = ar.token;
    name = ar.name;
    H.setTitle(ar.name);
    if (ar.picture && !hint.picture) { H.setCover(ar.picture); contentColor(page.el, ar.picture).then(syncMainButton); }
    H.setSub(el("p", { class: "fh-meta", text: [fmtFans(ar.fans), ar.albums.length ? `${ar.albums.length} ${plural(ar.albums.length, "релиз", "релиза", "релизов")}` : ""].filter(Boolean).join(" · ") }));
    fab.hidden = false;

    const content = el("div", { class: "seg-content" });
    const order = ["top", "disco", "all"];
    let cur = "top";
    const group = connectedGroup([
      { key: "top", label: "Популярное" },
      { key: "disco", label: "Дискография", count: ar.albums.length || null },
      { key: "all", label: "Все треки" },
    ], cur, (k) => { const dir = order.indexOf(k) > order.indexOf(cur) ? 1 : -1; cur = k; show(k); sharedAxis(content, dir); }, { label: "Разделы исполнителя" });
    H.setActions(group);
    group.link(content);
    mount(body, content);
    const ctxTop = (t) => ({ body: () => ({ token: page.token, kind: "track", i: t.i }), refresh, signal: page.sc.signal });
    let allBox = null;

    function show(k) {
      if (k === "top") {
        const parts = [];
        if (ar.albums.length) {
          parts.push(sectionTitle("Дискография", button({ label: "Все", variant: "text", trailing: "chevron_right", onClick: () => { group.set("disco"); cur = "disco"; show("disco"); sharedAxis(content, 1); } })));
          parts.push(carousel(ar.albums.slice(0, 12).map((a) => albumCard(a, false, { from: "detail" })), { label: "Дискография" }));
        }
        parts.push(sectionTitle("Популярные треки"));
        if (!ar.tracks.length) parts.push(emptyState({ icon: "music_note", title: "Нет популярных треков", compact: true }));
        else {
          parts.push(list(ar.tracks.map((t, n) => trackRow(t, { num: n + 1, sub: t.album, ctx: ctxTop(t) }))));
          parts.push(el("div", { class: "row-actions" }, button({
            label: `Скачать популярное · ${ar.tracks.length}`, icon: "download", variant: "tonal",
            onClick: () => requestDownload({ body: () => ({ token: page.token, kind: "all" }), refresh, tracks: ar.tracks, signal: page.sc.signal }),
          }), button({ label: "В плейлист…", icon: "playlist_add", variant: "text", onClick: () => downloadToPlaylist(ar.tracks.map((t) => ({ t, ctx: ctxTop(t) }))) })));
        }
        mount(content, parts);
      } else if (k === "disco") {
        mount(content, ar.albums.length ? grid(ar.albums.map((a) => albumCard(a, false, { from: "detail" }))) : emptyState({ icon: "album", title: "Дискография пуста", compact: true }));
      } else {
        if (!allBox) { allBox = el("div"); renderAll(allBox); }
        mount(content, allBox);
      }
    }
    show(cur);
  };

  async function renderAll(box) {
    mount(box, loadingState("Собираю треки со всех альбомов — это может занять до минуты…"));
    try { await loadAll(); } catch (e) {
      if (isAbort(e)) return;
      mount(box, errorState(e, () => renderAll(box)));
      return;
    }
    if (!all.tracks.length) { mount(box, emptyState({ icon: "queue_music", title: "Треков не нашлось", compact: true })); return; }
    const ctx = (t) => ({ body: () => ({ token: all.token, kind: "track", i: t.i }), refresh: refreshAll, signal: page.sc.signal });
    mount(box, [
      el("p", { class: "note", text: `${nTracks(all.tracks.length)} без повторов · ${all.albums} ${plural(all.albums, "релиз", "релиза", "релизов")}` + (all.capped ? " · показана только часть дискографии" : "") }),
      el("div", { class: "row-actions" }, button({ label: "Добавить всё в плейлист", icon: "playlist_add", variant: "tonal", onClick: () => downloadToPlaylist(all.tracks.map((t) => ({ t, ctx: ctx(t) }))) })),
      list(all.tracks.map((t) => trackRow(t, { sub: [t.album, t.year].filter(Boolean).join(" · "), ctx: ctx(t), cover: false }))),
    ]);
  }
  load();
}

// ---------------------------------------------------------------- исполнитель из своей медиатеки
export function openLibArtist(key, hint = {}, { from = "detail", origin = null } = {}) {
  open((page) => buildLibArtist(page, key, hint), { kind: "artist", from, origin });
}

function buildLibArtist(page, key, hint) {
  selectable(page.el, { rows: () => [...page.el.querySelectorAll(".ltrack")], key: (r) => r.dataset.sel, item: (r) => r._track,
    actions: (items, ctl) => libraryActions(items, ctl) });
  const H = flexHeader(page, { kind: "artist", cover: hint.image, ph: initial(hint.name), overline: "В медиатеке", title: hint.name || key });
  const body = el("div", { class: "page-body" });
  // удалено здесь или на другом экране: ряды — на месте (без счётчиков альбомов), скрытая страница — перечитать
  page.onDestroy(onLibraryDeleted((ids) => {
    if (!dropDeletedRows(body, ids)) return;
    if (!body.querySelector(".ltrack")) { H.setActions(null); mount(body, emptyState({ icon: "person", title: "Треков не осталось", text: "Все треки исполнителя удалены из медиатеки." })); }
    else if (page.el.hidden) load();
    else for (const ul of [...body.querySelectorAll(".tracklist")]) if (!ul.querySelector(".ltrack")) { const h = ul.previousElementSibling; ul.remove(); if (h) h.remove(); } // пустые альбомы
  }));
  page.scroll.append(H.el, body);
  const nowFab = currentFab(page.el, page.scroll, body);
  page.onDestroy(() => nowFab.destroy());
  if (hint.image) contentColor(page.el, hint.image);
  const load = async () => {
    mount(body, skeletonList(6, { thumb: false }));
    let r;
    try { r = await page.sc.get("/library/artist?a=" + encodeURIComponent(key)); } catch (e) {
      if (isAbort(e)) return;
      mount(body, errorState(e, load));
      return;
    }
    H.setTitle(r.name);
    if (r.image && !hint.image) { H.setCover(r.image); contentColor(page.el, r.image); }
    else if (!r.image) { const c = r.albums.find((g) => g.cover); if (c) contentColor(page.el, c.cover); }
    H.setSub(el("p", { class: "fh-meta", text: `${nTracks(r.count)} · ${nAlbums(r.albums.length)}` }));
    const all = r.albums.flatMap((g) => g.tracks);
    const source = { kind: "artist", id: key, title: r.name };
    const addAll = iconButton("playlist_add", "Добавить всё в плейлист", { variant: "tonal", onClick: () => addToPlaylist({ add: { artist: key }, title: r.name }) });
    H.setActions(el("div", { class: "pl-actions" }, playButtons(all, source), addAll));
    if (cacheOn()) H.setBarActions(moreMenu([{ icon: "download_for_offline", label: "Скачать на устройство", supporting: `${nTracks(all.length)} — слушать без сети`, onClick: () => saveOffline(all, `«${r.name}»`) }]));
    let offset = 0;
    const parts = [];
    if (r.albums.length > 1) {
      parts.push(sectionTitle("Альбомы"));
      parts.push(carousel(r.albums.map((g) => {
        const media = pic(g.cover, { cls: "card-pic", ph: "album" });
        return card({ media, title: g.title, subtitle: nTracks(g.tracks.length), label: `Альбом ${g.title}`, onClick: () => openLibAlbum(key, g.key, { title: g.title, cover: g.cover }, { origin: media }) });
      }), { label: "Альбомы" }));
    }
    for (const g of r.albums) {
      parts.push(sectionTitle(g.title, button({ label: "Открыть", variant: "text", trailing: "chevron_right", onClick: () => openLibAlbum(key, g.key, { title: g.title, cover: g.cover }) })));
      const base = offset;
      parts.push(list(g.tracks.map((t, n) => libraryRow(t, { num: n + 1, tracks: all, index: base + n, source })), "tracklist"));
      offset += g.tracks.length;
    }
    mount(body, parts);
  };
  load();
}
