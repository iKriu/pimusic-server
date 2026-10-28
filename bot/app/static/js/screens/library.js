// Медиатека: floating toolbar «Треки · Исполнители · Альбомы · Плейлисты» + фильтр, сетки карточек
// (альбомы — квадрат, исполнители — круг, плейлисты — мозаика 2×2), «Слушать всё / Перемешать всё».
import { el, icon, mount, pic, debounce, nTracks, initial, plural, setText } from "../dom.js";
import { api, isAbort } from "../api.js";
import { iconButton, button, list, grid, card, emptyState, errorState, skeletonList, skeletonGrid, snackbar } from "../ui.js";
import { topBar, trackScroll } from "../header.js";
import { axisSwap, fadeIn } from "../motion.js";
import { haptic } from "../tg.js";
import * as store from "../store.js";
import { go } from "../shell.js";
import { openLibAlbum } from "./album.js";
import { openLibArtist } from "./artist.js";
import { renderPlaylists } from "./playlist.js";
import { onPlaylists } from "../playlists.js";
import { libraryRow, playButtons, onLibraryDeleted, currentFab } from "../libtrack.js";
import { cacheOn } from "../offline.js";
import { pickFiles, dropTarget, onUploads } from "../uploads.js";
import { selectable } from "../select.js";
import { libraryActions, catalogActions } from "../bulk.js";
import { getSort, getGenre, sortChip, genreRow, sectionKey, sectionHeader, sortLocal, invalidateGenres } from "../libsort.js";

const MODES = [
  { key: "tracks", label: "Треки", icon: "music_note" },
  { key: "artists", label: "Исполнители", icon: "person" },
  { key: "albums", label: "Альбомы", icon: "album" },
  { key: "playlists", label: "Плейлисты", icon: "queue_music" }, // последним: не ломает привычный порядок (DESIGN §7)
];

// вся медиатека для очереди: /api/library/play с текущей сортировкой/жанром; старый сервер — order=shuffle|artist,
// совсем старый — последние 500 из /api/library
async function wholeLibrary(shuffle, { q = "", seed = 0 } = {}) {
  const st = getSort("tracks"), g = getGenre("tracks");
  const qs = new URLSearchParams({ sort: shuffle ? "random" : st.sort, order: st.order, limit: "3000" });
  if (g) qs.set("genre", g);
  if (q) qs.set("q", q);
  if (!shuffle && st.sort === "random" && seed) qs.set("seed", String(seed)); // тот же порядок, что на экране
  try { return (await api(`/library/play?${qs}`)).items || []; } catch (e) {
    if (e.status !== 400 && e.status !== 404) throw e;
  }
  try { return (await api(`/library/play?order=${shuffle ? "shuffle" : "artist"}&limit=3000`)).items || []; } catch (e) {
    if (e.status !== 404) throw e;
    return (await api("/library?limit=500")).items || [];
  }
}
const LIB_SOURCE = { kind: "library", id: "all", title: "Вся медиатека" };

let dirty = false;
export const markLibraryDirty = () => { dirty = true; };

export function createLibrary() {
  const scroller = el("div", { class: "scroll" });
  const view = el("section", { class: "view root root-library", "aria-label": "Медиатека" }, scroller);
  const bar = topBar({ title: "Медиатека", subtitle: "" });
  const filter = el("input", { type: "search", class: "filter-input", placeholder: "Фильтр по медиатеке", "aria-label": "Фильтр по медиатеке", autocomplete: "off", maxlength: "200" });
  const filterBox = el("div", { class: "filter-box", hidden: true }, icon("filter_list"), filter,
    iconButton("close", "Скрыть фильтр", { onClick: () => toggleFilter(false) }));
  const content = el("div", { class: "lib-content" });
  // «Слушать всё / Перемешать всё» — над списками медиатеки (в плейлистах у каждого свои кнопки)
  let shown = []; // треки, видимые сейчас в режиме «Треки» (с фильтром играем только их)
  const filtered = () => !!filter.value.trim() && mode === "tracks";
  // без фильтра — вся медиатека с сервера (перемешанная сервером, а не первые 200 из списка)
  // с фильтром тоже весь результат с сервера (q), а не только загруженные страницы
  const playAll = playButtons((shuffle) => wholeLibrary(shuffle, { q: mode === "tracks" ? filter.value.trim() : "", seed: pager && pager.seed }),
    () => (filtered() ? { kind: "library", id: "filter:" + filter.value.trim(), title: `Медиатека · «${filter.value.trim()}»` } : LIB_SOURCE), { label: "Вся медиатека" });
  // «Загрузить файлы»: выбор файлов; прогресс и форма «ждёт названия» — во вкладке «Загрузки»
  const upBtn = iconButton("upload_file", "Загрузить свои файлы: аудио или список треков TXT/CSV", { variant: "tonal", cls: "lib-upload", onClick: () => { pickFiles(); } });
  // оболочка с кэшем: «Скачанное» (треки на устройстве)
  const playRow = el("div", { class: "lib-play" }, playAll, upBtn);
  // оболочка с кэшем: «Скачанное» (треки на устройстве) — действие в шапке, чтобы не теснить «Слушать / Перемешать»
  if (cacheOn()) bar.appendChild(el("div", { class: "tbar-actions" }, iconButton("download_for_offline", "Скачанное на устройстве", { onClick: () => import("./offline.js").then((m) => m.openOffline({ from: "list" })) })));
  // сортировка и жанры — свои у каждого вида
  const sortBox = el("div", { class: "lib-sortbar" });
  const genreBox = el("div", { class: "lib-genres" });
  function buildSortBar() {
    mount(sortBox, sortChip(mode, () => load()));
    mount(genreBox, mode === "playlists" ? null : genreRow(mode, () => load()));
  }
  dropTarget(view);
  // режим выбора: долгое нажатие на ряд / «Выбрать» в меню трека
  const selCtl = selectable(view, { rows: () => [...content.querySelectorAll(".ltrack")], key: (r) => r.dataset.sel, item: (r) => r._track,
    // «Выбрать все»: весь текущий результат (сортировка/жанр/фильтр) — все страницы с сервера, а не только загруженные
    loadAll: (progress) => allTracks(progress), itemKey: (t) => String(t.item_id ?? t.id), total: () => (pager && mode === "tracks" ? pager.total : null),
    actions: (items, ctl) => libraryActions(items, ctl, { source: { kind: "library", id: "sel", title: "Медиатека" } }) });
  // весь результат текущего вида страницами по 500 (лимит /api/library) — тот же порядок, что на экране (seed)
  async function allTracks(progress) {
    const pg = pager;
    if (!pg || mode !== "tracks") return [];
    const out = [], seen = new Set();
    const signal = ctrl ? ctrl.signal : undefined;
    for (let offset = 0; ;) {
      const qs = new URLSearchParams({ limit: "500", offset: String(offset), q: pg.q, sort: pg.st.sort, order: pg.st.order });
      if (pg.genre) qs.set("genre", pg.genre);
      if (pg.seed) qs.set("seed", String(pg.seed));
      const r = await api("/library?" + qs, { signal });
      const items = r.items || [];
      const total = Number(r.total) || 0;
      let fresh = 0;
      for (const t of items) if (!seen.has(t.id)) { seen.add(t.id); out.push(t); fresh++; }
      offset += items.length;
      if (progress) progress(out.length, total);
      // старый сервер без offset вернёт ту же страницу — дальше не листаем
      if (!fresh || items.length < 500 || offset >= total) break;
    }
    if (pg !== pager) throw new Error("список изменился — повторите");
    return out;
  }
  // удалённые треки (здесь или на любом другом экране) — убрать ряды на месте, без перезагрузки всего списка
  onLibraryDeleted((ids) => {
    for (const k of Object.keys(cache)) delete cache[k]; // исполнители/альбомы — перечитать
    if (!loaded || view.hidden) return; // скрыта — перечитается при показе (markLibraryDirty)
    if (mode === "artists" || mode === "albums") { load(); return; }
    if (mode !== "tracks" || !pager) return;
    let loadedGone = 0;
    for (const r of [...content.querySelectorAll(".ltrack")]) if (ids.has(r.dataset.id)) { if (r._track) r._track._gone = true; r.remove(); }
    pager.items = pager.items.filter((t) => { if (!ids.has(String(t.id))) return true; loadedGone++; return false; });
    pager.offset = Math.max(0, pager.offset - loadedGone); // серверные страницы сдвинулись
    // заголовки разделов без рядов — убрать
    for (const h of [...content.querySelectorAll(".lib-sec")]) { const n = h.nextElementSibling; if (!n || n.classList.contains("lib-sec")) h.remove(); }
    recount(pager, loadedGone);
  });
  // точные «Найдено / всего» после удаления — одним лёгким запросом (удалённые могли быть и вне загруженного)
  async function recount(pg, gone = 0) {
    try {
      const qs = new URLSearchParams({ limit: "1", offset: "0", q: pg.q, sort: pg.st.sort, order: pg.st.order });
      if (pg.genre) qs.set("genre", pg.genre);
      const r = await api("/library?" + qs);
      if (pg !== pager) return;
      pg.total = Number(r.total) || 0;
      pg.libTotal = Number(r.library_total) || (!pg.q && !pg.genre ? pg.total : pg.libTotal);
      if (pg.items.length >= pg.total) pg.done = true;
    } catch { if (pg !== pager) return; pg.total = Math.max(pg.items.length, pg.total - gone); } // без сети — хотя бы загруженные
    setSubFor(pg);
    if (store.state.me && pg.libTotal) store.state.me.tracks = pg.libTotal;
    if (!pg.items.length && pg.done) { renderPage(pg, 0); return; }
    moreBox.hidden = pg.done;
    setText(more.querySelector(".btn-label"), `Показать ещё · ${Math.max(0, pg.total - pg.items.length)}`);
    if (selCtl.active) selCtl.refresh();
  }
  const setSubFor = (pg) => bar.setSub(pg.q || pg.genre ? `Найдено: ${pg.total}${pg.libTotal ? ` из ${pg.libTotal}` : ""}${pg.genre ? ` · ${pg.genre}` : ""}` : nTracks(pg.total));
  onUploads(() => {}); // подписка держит модуль загрузок живым
  bar.querySelector(".tbar-sub").setAttribute("aria-live", "polite"); // счётчик после фильтра/смены вида
  // всё, что меняется со сменой вида, — одним блоком: уходит и въезжает целиком (shared axis)
  const body = el("div", { class: "lib-body" }, playRow, sortBox, genreBox, content);
  scroller.append(bar, filterBox, body);
  // скелетон сменился данными (или ошибкой/пустым состоянием) — проявить, а не подменить рывком
  let wasSkel = false;
  new MutationObserver(() => {
    const skel = !!content.querySelector(':scope > [aria-busy="true"]');
    if (wasSkel && !skel) for (const c of content.children) fadeIn(c);
    wasSkel = skel;
  }).observe(content, { childList: true });
  // floating toolbar прячется при прокрутке вниз и возвращается при прокрутке вверх (M3)
  trackScroll(view, scroller, (y, dir) => { if (dir > 0 && y > 8) toolbar.classList.add("is-hidden"); else if (dir < 0 || y <= 8) toolbar.classList.remove("is-hidden"); });

  let mode = "tracks";
  let seq = 0;
  let ctrl = null;
  const cache = {};

  // floating toolbar
  const modeBtns = new Map();
  const tb = el("div", { class: "ftoolbar", role: "group", "aria-label": "Вид медиатеки" });
  for (const m of MODES) {
    const b = el("button", { type: "button", class: "ft-btn", "aria-pressed": "false", "aria-label": m.label, title: m.label },
      icon(m.icon), el("span", { class: "ft-label" }, el("span", { text: m.label })));
    b.addEventListener("click", () => {
      if (want === m.key) return;
      haptic("select");
      const dir = idx(m.key) > idx(want) ? 1 : -1;
      want = m.key;
      pressed(m.key); // подпись раскрывается сразу, вместе с уходом прежнего вида
      axisSwap(body, dir, () => { listTop(); setMode(m.key); });
    });
    modeBtns.set(m.key, b);
    tb.appendChild(b);
  }
  const fBtn = iconButton("filter_list", "Фильтр", { cls: "ft-filter", toggle: true, onClick: () => toggleFilter(filterBox.hidden) });
  tb.append(el("span", { class: "ft-div", "aria-hidden": "true" }), fBtn);
  const toolbar = el("div", { class: "ftoolbar-wrap" }, tb);
  view.appendChild(toolbar);
  currentFab(view, scroller, content, { cls: "fab-over-toolbar" }); // «К текущему» — над плавающей панелью
  const idx = (k) => MODES.findIndex((m) => m.key === k);
  let want = mode; // вид, к которому идёт переход (mode меняется, когда прежний уже ушёл)
  const pressed = (k) => { for (const [key, b] of modeBtns) b.setAttribute("aria-pressed", key === k ? "true" : "false"); };
  // прокручено глубже начала списка — новый вид с его начала (шапка остаётся свёрнутой)
  function listTop() {
    const top = body.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - bar.offsetHeight;
    if (scroller.scrollTop > top) scroller.scrollTop = Math.max(0, top);
  }

  function toggleFilter(on) {
    filterBox.hidden = !on;
    fBtn.setAttribute("aria-pressed", on ? "true" : "false");
    if (on) filter.focus();
    else if (filter.value) { filter.value = ""; load(); }
  }
  filter.addEventListener("input", debounce(() => load(), 300));
  filter.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !filter.value) { ev.stopPropagation(); toggleFilter(false); } });

  function setMode(k) {
    mode = want = k;
    pressed(k);
    playRow.hidden = k === "playlists";
    buildSortBar();
    filter.placeholder = k === "playlists" ? "Фильтр по плейлистам" : "Фильтр по медиатеке";
    load();
  }

  // ушли с вкладки посреди загрузки (запрос отменён) — при возвращении загрузить заново, а не висеть на скелетоне
  let inflight = false, loads = 0;
  async function load() {
    const n = ++loads;
    inflight = true;
    try { await loadInner(); } finally { if (n === loads) inflight = false; }
  }
  // ---------------------------------------------------------------- треки: страницы по 100 (offset), без потолка в 200
  const PAGE = 100;
  let pager = null, io = null;
  const more = button({ label: "Показать ещё", icon: "expand_more", variant: "tonal", onClick: () => nextPage() });
  // следующая страница: ошибка — снекбар, «Показать ещё» остаётся рабочей
  function nextPage() {
    if (!pager) return;
    loadPage(pager).catch((e) => {
      if (isAbort(e)) return;
      snackbar(`Не удалось загрузить ещё: ${e.message}`, { error: true, action: "Повторить", onAction: nextPage });
    });
  }
  const moreBox = el("div", { class: "row-actions lib-more" }, more);
  const trackSrc = (pg) => pg.q ? { kind: "library", id: "filter:" + pg.q, title: `Медиатека · «${pg.q}»` }
    : { kind: "library", id: `sort:${pg.st.sort}:${pg.st.order}:${pg.genre}`, title: pg.genre ? `Медиатека · ${pg.genre}` : "Медиатека" };
  async function loadPage(pg) {
    if (!pg || pg.busy || pg.done) return;
    pg.busy = true;
    more.disabled = true;
    if (!ctrl || ctrl.signal.aborted) ctrl = new AbortController(); // после ухода с вкладки запрос отменён — новый
    try {
      const qs = new URLSearchParams({ limit: String(PAGE), offset: String(pg.offset), q: pg.q, sort: pg.st.sort, order: pg.st.order });
      if (pg.genre) qs.set("genre", pg.genre);
      if (pg.seed) qs.set("seed", String(pg.seed));
      const r = await api("/library?" + qs, { signal: ctrl.signal });
      if (pg !== pager || pg.my !== seq) return;
      const fresh = (r.items || []).filter((t) => !pg.ids.has(t.id));
      // старый сервер не знает offset — вернул то же самое: дальше не листаем
      if (pg.offset > 0 && !fresh.length) pg.done = true;
      for (const t of fresh) pg.ids.add(t.id);
      pg.total = Number(r.total) || pg.total; // совпадения с фильтром (для страниц)
      pg.libTotal = Number(r.library_total) || (!pg.q && !pg.genre ? pg.total : pg.libTotal); // вся медиатека
      const start = pg.items.length;
      pg.items.push(...fresh);
      pg.offset += (r.items || []).length;
      if ((r.items || []).length < PAGE || pg.items.length >= pg.total) pg.done = true;
      renderPage(pg, start);
    } finally { pg.busy = false; more.disabled = false; }
  }
  function renderPage(pg, start) {
    const n = pg.total;
    setSubFor(pg);
    if (store.state.me && pg.libTotal) store.state.me.tracks = pg.libTotal;
    shown = pg.items;
    playRow.hidden = !pg.items.length && !pg.genre;
    if (!pg.items.length) { mount(content, pg.q ? noMatch(pg.q) : pg.genre ? emptyState({ icon: "filter_list", title: "В этом жанре пусто", compact: true }) : emptyLibrary()); return; }
    if (start === 0 || !pg.ul) {
      pg.ul = list([], "lib-tracks");
      pg.lastSec = null;
      mount(content, [pg.ul, moreBox]);
    }
    const src = trackSrc(pg);
    const frag = [];
    for (let i = start; i < pg.items.length; i++) {
      const t = pg.items[i];
      const sec = sectionKey(pg.st.sort, t);
      if (sec !== null && sec !== pg.lastSec) { pg.lastSec = sec; frag.push(sectionHeader(sec)); }
      frag.push(libraryRow(t, { thumb: true, tracks: pg.items, index: i, source: src }));
    }
    pg.ul.append(...frag);
    moreBox.hidden = pg.done;
    setText(more.querySelector(".btn-label"), `Показать ещё · ${Math.max(0, n - pg.items.length)}`);
    // бесконечная прокрутка: следующая страница, когда «Показать ещё» подъезжает к экрану
    if (!io && "IntersectionObserver" in window) io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting) && pager && !pager.done) nextPage(); }, { root: scroller, rootMargin: "800px 0px" });
    if (io) { io.disconnect(); if (!pg.done) io.observe(moreBox); }
  }

  async function loadInner() {
    const my = ++seq;
    selCtl.stop(true); // другой вид/сортировка/фильтр — прежний выбор (и «все») больше не про этот список
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    const q = filter.value.trim();
    const needle = q.toLowerCase();
    if (mode === "playlists") {
      await renderPlaylists(content, { needle, signal: ctrl.signal, sort: getSort("playlists"), setSub: (t) => { if (my === seq) bar.setSub(t); } });
      return;
    }
    if (mode === "tracks") {
      mount(content, skeletonList(8));
      const st = getSort("tracks"), genre = getGenre("tracks");
      // seed — случайный порядок одинаков на всех страницах
      pager = { my, q, st, genre, seed: st.sort === "random" ? Math.floor(Math.random() * 1e9) + 1 : 0, items: [], ids: new Set(), offset: 0, total: 0, libTotal: 0, done: false, busy: false, ul: null, lastSec: null };
      try { await loadPage(pager); } catch (e) {
        if (isAbort(e) || my !== seq) return;
        mount(content, errorState(e, load));
      }
      return;
    }
    const round = mode === "artists";
    const st = getSort(mode), genre = getGenre(mode);
    const ckey = `${mode}|${st.sort}|${st.order}|${genre}`;
    if (!cache[ckey]) mount(content, skeletonGrid(8, round));
    try {
      const qs = new URLSearchParams({ sort: st.sort, order: st.order });
      if (genre) qs.set("genre", genre);
      const r = cache[ckey] || await api(`/library/${mode}?${qs}`, { signal: ctrl.signal });
      if (my !== seq) return;
      cache[ckey] = r;
      // имя и число треков сортируем и на клиенте — так верно даже со старым сервером
      const sorted = st.sort === "name" || st.sort === "count" ? sortLocal(r.items, st.sort, st.order, (it) => it.name || it.title || "") : r.items;
      const items = sorted.filter((it) => !needle || [it.name, it.title, it.artist].join(" ").toLowerCase().includes(needle));
      const word = round ? ["исполнитель", "исполнителя", "исполнителей"] : ["альбом", "альбома", "альбомов"];
      bar.setSub((q ? `Найдено: ${items.length} из ` : "") + `${r.items.length} ${plural(r.items.length, ...word)}`);
      if (!items.length) { mount(content, q ? noMatch(q) : emptyLibrary()); return; }
      const cells = [];
      let last = null;
      for (const it of items) {
        const sec = st.sort === "name" ? sectionKey("name", { name: it.name || it.title }) : st.sort === "year" ? sectionKey("year", it) : null;
        if (sec !== null && sec !== last) { last = sec; cells.push(sectionHeader(sec, { grid: true })); }
        cells.push((round ? artistCard : albumCardLib)(it));
      }
      mount(content, grid(cells, round ? "grid-round" : ""));
    } catch (e) {
      if (isAbort(e) || my !== seq) return;
      mount(content, errorState(e, load));
    }
  }

  function artistCard(a) {
    const media = pic(a.image, { cls: "card-pic", ph: initial(a.name) });
    return card({ media, title: a.name, subtitle: nTracks(a.count), round: true, label: `Исполнитель ${a.name}`,
      onClick: () => openLibArtist(a.key, { name: a.name, image: a.image }, { from: "list", origin: media }) });
  }
  function albumCardLib(a) {
    const media = pic(a.cover, { cls: "card-pic", ph: "album" });
    return card({ media, title: a.title, subtitle: `${a.artist} · ${nTracks(a.count)}`, label: `Альбом ${a.title}`,
      onClick: () => openLibAlbum(a.artist_key, a.key, { title: a.title, cover: a.cover }, { from: "list", origin: media }) });
  }
  const noMatch = (q) => emptyState({ shape: "pentagon", icon: "filter_list", title: "Ничего не нашлось", text: `В медиатеке нет совпадений с «${q}».`,
    action: button({ label: "Сбросить фильтр", variant: "tonal", onClick: () => { filter.value = ""; load(); } }) });

  // свежие загрузки → при следующем показе перечитываем
  let lastCount = -1;
  store.on("tracks", () => { const n = store.state.keys.size; if (lastCount >= 0 && n !== lastCount) { dirty = true; for (const k of Object.keys(cache)) delete cache[k]; } lastCount = n; });

  // изменения плейлистов (создан, удалён, вступили) → сетка перечитывается, если открыта
  onPlaylists(() => { if (mode === "playlists" && loaded && !view.hidden) load(); else if (mode === "playlists") dirty = true; });

  let loaded = false;
  return {
    el: view, scroller,
    show() {
      if (!loaded || dirty) { if (loaded) invalidateGenres(); loaded = true; dirty = false; for (const k of Object.keys(cache)) delete cache[k]; setMode(mode); }
      else if (io && pager && !pager.done && moreBox.isConnected) { io.disconnect(); io.observe(moreBox); } // прокрутка продолжится
    },
    // выбрать вид (глубокая ссылка #playlists, «Открыть медиатеку» из пустого плейлиста)
    select(k) {
      if (!MODES.some((m) => m.key === k)) return;
      if (loaded) { if (mode !== k) setMode(k); } else { mode = want = k; pressed(k); }
    },
    hide() { if (inflight) dirty = true; if (ctrl) ctrl.abort(); if (pager) pager.busy = false; toolbar.classList.remove("is-hidden"); selCtl.stop(); },
    invalidate() { for (const k of Object.keys(cache)) delete cache[k]; dirty = true; },
  };
}

export function emptyLibrary() {
  return emptyState({
    shape: "cookie4", icon: "library_music", title: "Медиатека пуста",
    text: "Найдите музыку во вкладке «Поиск» — скачанное появится здесь и в Jellyfin.",
    action: button({ label: "К поиску", icon: "search", variant: "tonal", onClick: () => go("search", { focus: true }) }),
  });
}

// Ряды своих треков живут в libtrack.js (тап = играть, меню действий, подсветка играющего)
export { libraryRow } from "../libtrack.js";
