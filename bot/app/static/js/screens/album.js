// Страница альбома (каталог Deezer и своя медиатека): large flexible top app bar в контент-цвете обложки,
// split button «Скачать альбом ▾ — всё / только недостающие», треклист с номерами.
import { el, icon, mount, nTracks, parseDur, fmtTotal } from "../dom.js";
import { button, splitButton, list, emptyState, errorState, skeletonList } from "../ui.js";
import { flexHeader } from "../header.js";
import { trackRow, requestDownload } from "../track.js";
import { contentColor } from "../theme.js";
import { open, syncMainButton } from "../shell.js";
import * as store from "../store.js";
import { isAbort } from "../api.js";
import { openCatalogArtist, openLibArtist } from "./artist.js";
import { libraryRow, playButtons, onLibraryDeleted, dropDeletedRows, currentFab, saveOffline } from "../libtrack.js";
import { cacheOn } from "../offline.js";
import { iconButton, openMenu } from "../ui.js";
import { addToPlaylist } from "../playlists.js";
import { selectable } from "../select.js";
import { libraryActions, catalogActions } from "../bulk.js";
import { downloadToPlaylist } from "../bulk.js";

// кнопка ⋮ шапки с меню действий
export function moreMenu(items) {
  const b = iconButton("more_vert", "Ещё действия", { onClick: () => { b.setAttribute("aria-expanded", "true"); openMenu(b, items, () => b.setAttribute("aria-expanded", "false")); } });
  b.setAttribute("aria-haspopup", "menu");
  b.setAttribute("aria-expanded", "false");
  return b;
}

export const KIND = { album: "Альбом", ep: "EP", single: "Сингл", compile: "Сборник" };

export function openCatalogAlbum(hint, { from = "detail", origin = null } = {}) {
  if (!hint || !hint.id) return;
  open((page) => buildCatalogAlbum(page, hint), { kind: "album", from, origin });
}

function artistLink(name, onClick) {
  if (!onClick) return el("span", { class: "fh-artist", text: name });
  return button({ label: name, variant: "text", trailing: "chevron_right", cls: "fh-artist-btn", onClick });
}

function buildCatalogAlbum(page, hint) {
  const H = flexHeader(page, { kind: "album", cover: hint.cover, overline: KIND[hint.kind] || "Альбом", title: hint.title || "Альбом" });
  const body = el("div", { class: "page-body" });
  selectable(page.el, { rows: () => [...body.querySelectorAll(".ctrack")], key: (r) => r.dataset.sel, item: (r) => ({ t: r._track, ctx: r._ctx }), actions: catalogActions });
  page.scroll.append(H.el, body);
  if (hint.cover) contentColor(page.el, hint.cover).then(syncMainButton);
  const path = "/catalog/album?id=" + encodeURIComponent(hint.id);
  const refresh = async () => { const d = await page.sc.get(path); page.token = d.token; };
  let off = null;
  page.onDestroy(() => off && off());

  const load = async () => {
    H.setSub(hint.artist ? artistLink(hint.artist) : null);
    H.setActions(el("div", { class: "skel skel-btn", "aria-hidden": "true" }));
    mount(body, skeletonList(8, { thumb: false }));
    let al;
    try { al = await page.sc.get(path); } catch (e) {
      if (isAbort(e)) return;
      H.setActions(null);
      mount(body, errorState(e, load));
      return;
    }
    page.token = al.token;
    H.setTitle(al.title);
    H.setOverline(KIND[al.kind] || "Альбом");
    if (al.cover && al.cover !== hint.cover) { if (!hint.cover) H.setCover(al.cover); contentColor(page.el, al.cover).then(syncMainButton); }
    const total = al.tracks.reduce((s, t) => s + parseDur(t.duration), 0);
    H.setSub([
      artistLink(al.artist, al.artist_id ? () => openCatalogArtist({ id: al.artist_id, name: al.artist }) : null),
      el("p", { class: "fh-meta", text: [al.year, nTracks(al.tracks.length), fmtTotal(total)].filter(Boolean).join(" · ") }),
    ]);
    if (!al.tracks.length) {
      H.setActions(null);
      mount(body, emptyState({ icon: "album", title: "Треклист пуст", text: "Каталог не вернул список треков этого релиза." }));
      return;
    }
    const all = () => requestDownload({ body: () => ({ token: page.token, kind: "all" }), refresh, tracks: al.tracks, signal: page.sc.signal });
    const missing = () => al.tracks.filter((t) => store.trackState(t) === "none" || store.trackState(t) === "error");
    const onlyMissing = () => {
      const m = missing();
      if (!m.length) return all();
      return requestDownload({ body: () => ({ token: page.token, kind: "tracks", idx: m.map((t) => t.i) }), refresh, tracks: m, signal: page.sc.signal });
    };
    // MainButton Telegram показываем, только когда split button ушёл за край (DESIGN 6a.5)
    let mainCfg = null, actionsVisible = true;
    const setMainCfg = (cfg) => { mainCfg = cfg; page.setMain(actionsVisible ? null : cfg); };
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver(([en]) => { actionsVisible = en.isIntersecting; page.setMain(actionsVisible ? null : mainCfg); }, { root: page.scroll, rootMargin: "-64px 0px 0px 0px" });
      io.observe(H.el.querySelector(".fh-actions"));
      page.onDestroy(() => io.disconnect());
    }
    const actions = () => {
      const m = missing().length;
      const owned = al.tracks.filter((t) => store.isOwned(t)).length;
      if (owned === al.tracks.length) {
        setMainCfg(null);
        return el("div", { class: "pl-actions" }, button({ label: "Весь альбом в медиатеке", icon: "library_add_check", variant: "tonal", disabled: true, cls: "is-owned" }),
          button({ label: "В плейлист…", icon: "playlist_add", variant: "text",
            onClick: () => downloadToPlaylist(al.tracks.map((t) => ({ t, ctx: { body: () => ({ token: page.token, kind: "track", i: t.i }), refresh, signal: page.sc.signal } }))) }));
      }
      setMainCfg({ text: owned ? `Скачать недостающие · ${m}` : `Скачать альбом · ${al.tracks.length}`, onClick: owned ? onlyMissing : all });
      return splitButton({
        label: owned ? `Скачать недостающие · ${m}` : "Скачать альбом", icon: "download", onClick: owned ? onlyMissing : all,
        menu: () => [
          { label: "Скачать всё", supporting: nTracks(al.tracks.length), icon: "download", onClick: all },
          { label: "Только недостающие", supporting: m ? nTracks(m) : "всё уже есть", icon: "download_for_offline", onClick: onlyMissing, disabled: !m || m === al.tracks.length },
          { label: "Добавить всё в плейлист…", supporting: "Скачаю и добавлю", icon: "playlist_add",
            onClick: () => downloadToPlaylist(al.tracks.map((t) => ({ t, ctx: { body: () => ({ token: page.token, kind: "track", i: t.i }), refresh, signal: page.sc.signal } }))) },
        ],
      });
    };
    H.setActions(actions());
    let lastSig = "";
    off = store.on("tracks", () => {
      const sig = missing().length + "/" + al.tracks.filter((t) => store.isOwned(t)).length;
      if (sig !== lastSig) { lastSig = sig; H.setActions(actions()); }
    });
    const multi = al.tracks.some((t) => t.artist !== al.artist);
    mount(body, list(al.tracks.map((t, n) => trackRow(t, {
      num: t.no || n + 1, sub: multi && t.artist !== al.artist ? t.artist : "",
      ctx: { body: () => ({ token: page.token, kind: "track", i: t.i }), refresh, signal: page.sc.signal },
    })), "tracklist"));
  };
  load();
}

// ---------------------------------------------------------------- альбом из своей медиатеки
export function openLibAlbum(artistKey, key, hint = {}, { from = "detail", origin = null } = {}) {
  open((page) => buildLibAlbum(page, artistKey, key, hint), { kind: "album", from, origin });
}

function buildLibAlbum(page, artistKey, key, hint) {
  const H = flexHeader(page, { kind: "album", cover: hint.cover, overline: "Альбом в медиатеке", title: hint.title || key });
  const body = el("div", { class: "page-body" });
  selectable(page.el, { rows: () => [...body.querySelectorAll(".ltrack")], key: (r) => r.dataset.sel, item: (r) => r._track,
    actions: (items, ctl) => libraryActions(items, ctl) });
  page.scroll.append(H.el, body);
  const nowFab = currentFab(page.el, page.scroll, body);
  page.onDestroy(() => nowFab.destroy());
  if (hint.cover) contentColor(page.el, hint.cover);
  const emptyAlbum = () => mount(body, emptyState({ icon: "album", title: "Альбом пуст", text: "Все треки удалены из медиатеки." }));
  // удалено здесь или на другом экране: ряды — на месте; скрытая страница (в стеке) перечитывается целиком
  page.onDestroy(onLibraryDeleted((ids) => {
    if (!dropDeletedRows(body, ids)) return;
    if (!body.querySelector(".ltrack")) { H.setActions(null); emptyAlbum(); } else if (page.el.hidden) load();
  }));
  const load = async () => {
    mount(body, skeletonList(6, { thumb: false }));
    let r;
    try { r = await page.sc.get(`/library/album?a=${encodeURIComponent(artistKey)}&b=${encodeURIComponent(key)}`); } catch (e) {
      if (isAbort(e)) return;
      mount(body, errorState(e, load));
      return;
    }
    H.setTitle(r.title);
    if (r.cover && !hint.cover) { H.setCover(r.cover); contentColor(page.el, r.cover); }
    const total = r.tracks.reduce((s, t) => s + parseDur(t.duration), 0);
    H.setSub([
      artistLink(r.artist, () => openLibArtist(r.artist_key, { name: r.artist })),
      el("p", { class: "fh-meta", text: [nTracks(r.tracks.length), fmtTotal(total)].filter(Boolean).join(" · ") }),
    ]);
    const source = { kind: "album", id: `${artistKey}/${key}`, title: r.title, artist_key: artistKey, album_key: key, cover: r.cover || hint.cover || "" };
    const addAll = iconButton("playlist_add", "Добавить альбом в плейлист", { variant: "tonal", onClick: () => addToPlaylist({ add: { album: { a: artistKey, b: key } }, title: r.title }) });
    H.setActions(el("div", { class: "pl-actions" }, playButtons(r.tracks, source), addAll));
    // «Скачать на устройство» — в меню ⋮ шапки (ряд действий остаётся в одну строку)
    if (cacheOn()) H.setBarActions(moreMenu([{ icon: "download_for_offline", label: "Скачать на устройство", supporting: `${nTracks(r.tracks.length)} — слушать без сети`, onClick: () => saveOffline(r.tracks, `«${r.title}»`) }]));
    const rows = r.tracks.map((t, n) => libraryRow(t, { num: n + 1, tracks: r.tracks, index: n, source, onRemoved: () => { if (!body.querySelector(".li")) emptyAlbum(); } }));
    mount(body, list(rows, "tracklist"));
  };
  load();
}
