// Поиск: search bar → search view (история, «Популярное у вас»), результаты: hero-исполнитель,
// segmented buttons со счётчиками, списки/сетки.
import { el, icon, mount, pic, debounce, fmtFans, initial, nTracks } from "../dom.js";
import { api, isAbort } from "../api.js";
import {
  iconButton, button, chip, connectedGroup, list, listItem, card, grid, emptyState, errorState, skeletonList,
  wavyCircular, sectionTitle, snackbar, dialog, setBusy,
} from "../ui.js";
import { openPlaylist } from "./playlist.js";
import { selectable } from "../select.js";
import { libraryActions, catalogActions } from "../bulk.js";
import { changed as playlistsChanged } from "../playlists.js";
import { trackRow, requestDownload } from "../track.js";
import { trackScroll } from "../header.js";
import { sharedAxis } from "../motion.js";
import { contentColor } from "../theme.js";
import { haptic } from "../tg.js";
import * as store from "../store.js";
import { go, pushBackHandler, shell } from "../shell.js";
import { openCatalogAlbum, KIND } from "./album.js";
import { openCatalogArtist } from "./artist.js";

const LINK_RE = /^https?:\/\/\S+$/i;

export function createSearch({ onSettings }) {
  const scroller = el("div", { class: "scroll" });
  const view = el("section", { class: "view root root-search", "aria-label": "Поиск" }, scroller);
  selectable(view, { rows: () => [...view.querySelectorAll(".search-results .ctrack")], key: (r) => r.dataset.sel, item: (r) => ({ t: r._track, ctx: r._ctx }), actions: catalogActions });

  const input = el("input", {
    type: "search", class: "sb-input", placeholder: "Артист, трек, альбом или ссылка", "aria-label": "Поиск музыки",
    autocomplete: "off", enterkeyhint: "search", inputmode: "search", spellcheck: "false", maxlength: "200",
  });
  const leadIcon = el("span", { class: "sb-lead" }, icon("search"));
  const backBtn = iconButton("arrow_back", "Закрыть поиск", { cls: "sb-back", onClick: () => closeView(true) });
  const clearBtn = iconButton("close", "Очистить запрос", { cls: "sb-clear", onClick: () => { input.value = ""; onInput(); input.focus(); } });
  const actRing = wavyCircular({ size: 24, label: "Идут загрузки" });
  actRing.setAttribute("aria-hidden", "true"); // имя и смысл — у кнопки-обёртки
  const actBtn = el("button", { type: "button", class: "ibtn ibtn-standard sb-activity", "aria-label": "Идут загрузки — открыть", hidden: true, onclick: () => go("downloads") }, actRing);
  const avatar = el("button", { type: "button", class: "sb-avatar", "aria-label": "О сервере", title: "О сервере", onclick: () => onSettings() }, icon("person"));
  const bar = el("div", { class: "searchbar", role: "search" }, leadIcon, backBtn, input, clearBtn, actBtn, avatar);
  const header = el("header", { class: "search-top" }, el("h1", { class: "sr-only", text: "Поиск" }), bar);
  const suggest = el("div", { class: "search-view", hidden: true, role: "region", "aria-label": "Подсказки поиска" });
  // Результаты не live-область (иначе скринридер зачитал бы весь список) — сводка идёт в отдельный статус
  const results = el("div", { class: "search-results" });
  const status = el("div", { class: "sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
  const say = (t) => { status.textContent = ""; setTimeout(() => { status.textContent = t; }, 60); };
  scroller.append(header, suggest, results, status);
  trackScroll(view, scroller);

  let current = { q: "", res: null, ctrl: null, seg: "tracks" };
  let releaseBack = null;
  let popular = null;

  // ------------------------------------------------------------ search view
  function openView() {
    if (view.classList.contains("is-searching")) return;
    view.classList.add("is-searching");
    releaseBack = pushBackHandler(() => closeView(true));
    renderSuggest();
  }
  function closeView(blur) {
    if (!view.classList.contains("is-searching")) return;
    view.classList.remove("is-searching");
    suggest.hidden = true;
    if (releaseBack) { releaseBack(); releaseBack = null; }
    if (blur) {
      input.blur(); // убираем экранную клавиатуру…
      // …но фокус не теряем в body: клавиатурный пользователь продолжает с блока результатов
      results.tabIndex = -1;
      results.focus({ preventScroll: true });
    }
  }
  input.addEventListener("focus", openView);
  input.addEventListener("blur", () => setTimeout(() => {
    const a = document.activeElement;
    if (!view.contains(a) || (!bar.contains(a) && !suggest.contains(a))) {
      if (!input.value.trim() || current.res) closeView(false);
    }
  }, 180));

  function renderSuggest() {
    const empty = !input.value.trim();
    suggest.hidden = !empty || !view.classList.contains("is-searching");
    if (suggest.hidden) return;
    mount(suggest, suggestions());
  }

  function suggestions() {
    const out = [];
    const hist = store.history();
    if (hist.length) {
      out.push(sectionTitle("Недавние", button({ label: "Очистить", variant: "text", onClick: () => { store.clearHistory(); renderSuggest(); idleIfNeeded(); } })));
      out.push(el("div", { class: "chips" }, hist.map((q) => chip({
        label: q, icon: "history", variant: "suggestion",
        onClick: () => { input.value = q; run(q, { push: true }); closeView(true); },
        onRemove: () => { store.removeHistory(q); renderSuggest(); idleIfNeeded(); },
      }))));
    }
    const popBox = el("div", { class: "chips" });
    out.push(sectionTitle("Популярное у вас"), popBox);
    loadPopular().then((items) => {
      if (!items) { mount(popBox, el("p", { class: "hint", text: "Не удалось загрузить медиатеку." })); return; }
      if (!items.length) { mount(popBox, el("p", { class: "hint", text: "Скачайте пару треков — здесь появятся ваши исполнители." })); return; }
      mount(popBox, items.map((a) => chip({
        label: a.name, variant: "suggestion", avatar: pic(a.image, { cls: "chip-avatar", ph: initial(a.name) }),
        onClick: () => { input.value = a.name; run(a.name, { push: true }); closeView(true); },
      })));
    });
    return out;
  }

  function loadPopular() {
    if (!popular) {
      popular = api("/library/artists").then((r) => [...r.items].sort((a, b) => b.count - a.count).slice(0, 8))
        .catch(() => { popular = null; return null; });
    }
    return popular;
  }

  // ------------------------------------------------------------ запрос
  const live = debounce(() => { const q = input.value.trim(); if (q.length >= 2) run(q); }, 500);
  function onInput() {
    clearBtn.hidden = !input.value;
    renderSuggest();
    if (!input.value.trim()) { live.cancel(); abort(); current.q = ""; current.res = null; idle(); return; }
    live();
  }
  input.addEventListener("input", onInput);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      live.cancel();
      const q = input.value.trim();
      if (q) { run(q, { push: true, force: true }); closeView(true); }
    }
  });

  function abort() { if (current.ctrl) current.ctrl.abort(); current.ctrl = null; }

  async function run(q, { push = false, force = false } = {}) {
    clearBtn.hidden = !input.value;
    if (LINK_RE.test(q)) { abort(); current = { ...current, q, res: null }; renderLink(q); return; } // ссылка — не поиск
    if (push) store.pushHistory(q);
    if (q === current.q && current.res && !force) return;
    abort();
    const ctrl = new AbortController();
    current = { ...current, q, res: null, ctrl };
    mount(results, [el("div", { class: "skel-hero", "aria-hidden": "true" }), skeletonList(7)]);
    try {
      const res = await api("/search?q=" + encodeURIComponent(q), { signal: ctrl.signal });
      if (ctrl.signal.aborted) return;
      current.res = res;
      renderResults(res);
    } catch (e) {
      if (isAbort(e)) return;
      mount(results, errorState(e, () => run(q, { force: true })));
      say((e && e.message) || "Ошибка поиска");
    }
  }

  // ------------------------------------------------------------ ссылка (Spotify / YouTube / Яндекс / SoundCloud…)
  function renderLink(url) {
    let host = "";
    try { host = new URL(url).hostname.replace(/^www\./, ""); } catch { /* */ }
    const go1 = button({ label: "Скачать по ссылке", icon: "download", variant: "filled", size: "m", onClick: async () => {
      haptic("medium");
      setBusy(go1, true, "Разбираю ссылку…");
      let r;
      try { r = await api("/download/link", { method: "POST", body: { url } }); } catch (e) {
        setBusy(go1, false);
        haptic("error");
        const msg = e.code === "bad_url" ? "Это не похоже на ссылку на музыку." : e.status === 404 && e.code !== "nothing_to_download"
          ? "Сервер пока не умеет качать по ссылке." : e.message;
        snackbar(msg, { error: true });
        say(msg);
        return;
      }
      setBusy(go1, false);
      haptic("success");
      store.kick();
      const label = `В очереди: ${r.title || url}${r.queued > 1 ? ` · ${nTracks(r.queued)}` : ""}`;
      const pl = r.playlist;
      if (pl) playlistsChanged(pl.id, "create");
      const openPl = () => { go("library"); shell.roots.library.select("playlists"); openPlaylist(pl.id, { name: pl.name }, { from: "list" }); };
      snackbar(label, pl ? { action: "Открыть плейлист", onAction: openPl } : { action: "Загрузки", onAction: () => go("downloads") });
      if (r.note) await dialog({ icon: "info", headline: "Обратите внимание", body: r.note, actions: [{ label: "Понятно", value: true }] });
    } });
    mount(results, el("div", { class: "card card-filled link-card" },
      el("span", { class: "li-icon-box" }, icon("link")),
      el("div", { class: "link-text" },
        el("h2", { class: "link-title", text: "Скачать по ссылке" }),
        el("p", { class: "link-sub", text: (host ? host + " · " : "") + "трек, альбом или плейлист — как если бы вы прислали ссылку боту. Для плейлиста сразу появится плейлист в медиатеке." })),
      go1));
    say("Это ссылка. Можно скачать по ссылке.");
  }

  async function refreshToken() {
    const r = await api("/search?q=" + encodeURIComponent(current.q));
    if (current.res) current.res.token = r.token;
  }

  // ------------------------------------------------------------ результаты
  function renderResults(res) {
    res.artists = res.artists || [];
    const counts = { tracks: res.tracks.length, albums: res.albums.length, artists: res.artists.length };
    say(counts.tracks || counts.albums || counts.artists
      ? `Найдено: ${counts.tracks} треков, ${counts.albums} альбомов, ${counts.artists} исполнителей`
      : "Ничего не нашлось");
    if (!counts.tracks && !counts.albums && !counts.artists) {
      mount(results, emptyState({
        shape: "pentagon", icon: "travel_explore", title: "Ничего не нашлось",
        text: "Попробуйте написать иначе: «Исполнитель — Название» или только исполнителя.",
        action: button({ label: "Новый запрос", icon: "search", variant: "tonal", onClick: () => { input.value = ""; onInput(); input.focus(); } }),
      }));
      return;
    }
    const out = [];
    if (res.artist && res.artist_id) out.push(heroArtist(res));
    let seg = counts[current.seg] ? current.seg : ["tracks", "albums", "artists"].find((k) => counts[k]);
    const content = el("div", { class: "seg-content" });
    const segs = connectedGroup([
      { key: "tracks", label: "Треки", count: counts.tracks, disabled: !counts.tracks },
      { key: "albums", label: "Альбомы", count: counts.albums, disabled: !counts.albums },
      { key: "artists", label: "Артисты", count: counts.artists, disabled: !counts.artists },
    ], seg, (k) => { const dir = order(k) > order(current.seg) ? 1 : -1; current.seg = k; show(k); sharedAxis(content, dir); }, { label: "Тип результатов" });
    segs.link(content);
    out.push(el("div", { class: "seg-wrap" }, segs), content);
    mount(results, out);
    current.seg = seg;
    show(seg);

    function show(key) {
      if (key === "tracks") {
        const ctx = (t) => ({ body: () => ({ token: res.token, kind: "track", i: t.i }), refresh: refreshToken });
        mount(content, [
          res.tracks.length > 1 ? el("div", { class: "row-actions" }, button({
            label: `Скачать все · ${res.tracks.length}`, icon: "download", variant: "outlined",
            onClick: () => requestDownload({ body: () => ({ token: res.token, kind: "all" }), refresh: refreshToken, tracks: res.tracks }),
          })) : null,
          list(res.tracks.map((t) => trackRow(t, { ctx: ctx(t) }))),
        ]);
      } else if (key === "albums") {
        mount(content, grid(res.albums.map((a) => albumCard(a, true))));
      } else {
        mount(content, list(res.artists.map((a) => listItem({
          leading: pic(a.picture, { cls: "li-avatar", ph: initial(a.name) }), headline: a.name, supporting: fmtFans(a.fans) || "Исполнитель",
          trailing: icon("chevron_right", { class: "li-chev" }), label: `Исполнитель ${a.name}`,
          onClick: (ev) => openCatalogArtist(a, { from: "list", origin: ev.currentTarget.querySelector(".pic") }),
        }))));
      }
    }
  }
  const order = (k) => ["tracks", "albums", "artists"].indexOf(k);

  function heroArtist(res) {
    const a = { id: res.artist_id, name: res.artist, picture: res.picture, fans: res.fans };
    const media = pic(res.picture, { cls: "hero-avatar", ph: initial(res.artist), eager: true });
    const c = el("button", { type: "button", class: "hero-artist", "aria-label": `Исполнитель ${res.artist}: открыть` },
      media,
      el("span", { class: "hero-text" },
        el("span", { class: "hero-over", text: "Исполнитель" }),
        el("span", { class: "hero-name", text: res.artist }),
        el("span", { class: "hero-sub", text: [fmtFans(res.fans), "дискография и все треки"].filter(Boolean).join(" · ") })),
      el("span", { class: "hero-go" }, icon("arrow_forward")));
    c.addEventListener("click", () => openCatalogArtist(a, { from: "list", origin: media }));
    if (res.picture) contentColor(c, res.picture);
    return c;
  }

  // ------------------------------------------------------------ пустой экран
  function idle() {
    mount(results, [
      el("div", { class: "idle-hero" },
        el("span", { class: "idle-shape", "aria-hidden": "true" }, icon("graphic_eq")),
        el("h2", { class: "idle-title", text: "Что послушаем?" }),
        el("p", { class: "idle-text", text: "Исполнитель, альбом или трек — скачаю в вашу медиатеку Jellyfin." })),
      el("div", { class: "idle-suggest" }, suggestions()),
    ]);
  }
  function idleIfNeeded() { if (!current.q) idle(); }

  // мини-индикатор загрузок в строке поиска
  store.on("activity", () => { const n = store.activeCount(); actBtn.hidden = !n; actBtn.setAttribute("aria-label", `Идут загрузки: ${n} — открыть`); });

  clearBtn.hidden = true;
  idle();
  return {
    el: view, scroller,
    show({ focus } = {}) { if (focus) { input.focus(); input.select(); } if (!current.q) idle(); },
    hide() { closeView(false); },
    focus() { input.focus(); },
    searchFor(q) { input.value = q; clearBtn.hidden = false; closeView(true); run(q, { push: true, force: true }); },
  };
}

export function albumCard(a, showArtist, { from = "list" } = {}) {
  const media = pic(a.cover, { cls: "card-pic", ph: "album" });
  const sub = [showArtist ? a.artist : "", KIND[a.kind] || "", a.year].filter(Boolean).join(" · ");
  return card({
    media, title: a.title, subtitle: sub, label: `Альбом ${a.title}${showArtist && a.artist ? ", " + a.artist : ""}`,
    onClick: () => openCatalogAlbum(a, { from: shell.twoPane ? from : "detail", origin: media }),
  });
}

