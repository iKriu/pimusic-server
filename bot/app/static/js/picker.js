// «Добавить треки» в плейлист: полноэкранный лист (на широких — диалог) с вкладками «Медиатека» и «Поиск в сети».
// Выбор общий для вкладок и поисков; снизу — «Добавить N». Медиатека → /api/playlists/{id}/add {track_ids};
// каталог → скачивание с playlist_id (сервер добавит треки по мере загрузки). Работает и для участников общего плейлиста.
import { el, icon, mount, pic, setText, nTracks, debounce, initial, fmtFans } from "./dom.js";
import { api, isAbort } from "./api.js";
import { sheet, button, connectedGroup, list, chip, skeletonList, emptyState, errorState, loadingState, snackbar, announce, wavyCircular } from "./ui.js";
import { haptic } from "./tg.js";
import { shell } from "./shell.js";
import { pl, changed } from "./playlists.js";
import { downloadItems } from "./bulk.js";

export function openTrackPicker(p, { onAdded } = {}) {
  const inPl = new Set((p.tracks || []).map((t) => t.id));
  const picked = new Map(); // ключ → { kind: "lib" | "cat", t, ctx }
  sheet({
    title: `Добавить в «${p.name}»`, label: `Добавить треки в плейлист «${p.name}»`,
    cls: "sheet-picker" + (shell.twoPane ? " sheet-dialog" : ""),
    build(body, close) {
      const tabBody = el("div", { class: "pick-body" });
      const libView = el("div", { class: "pick-tab" });
      const netView = el("div", { class: "pick-tab", hidden: true });
      const tabs = connectedGroup([{ key: "lib", label: "Медиатека", icon: "library_music" }, { key: "net", label: "Поиск в сети", icon: "travel_explore" }], "lib",
        (k) => { libView.hidden = k !== "lib"; netView.hidden = k !== "net"; (k === "lib" ? libInput : netInput).focus(); }, { label: "Откуда добавить" });
      tabs.link(tabBody);
      const addBtn = button({ label: "Добавить", icon: "playlist_add", variant: "filled", size: "m", cls: "pick-add", onClick: submit });
      const countEl = el("span", { class: "pick-count", "aria-live": "polite" });
      const bar = el("div", { class: "pick-bar" }, countEl, addBtn);
      tabBody.append(libView, netView);
      body.append(el("div", { class: "pick-tabs" }, tabs), tabBody, bar);
      const sync = () => {
        const n = picked.size;
        setText(countEl, n ? `Выбрано: ${n}` : "Отметьте треки");
        setText(addBtn.querySelector(".btn-label"), n ? `Добавить ${n}` : "Добавить");
        addBtn.setAttribute("aria-disabled", n ? "false" : "true");
        for (const r of body.querySelectorAll(".pick-row[data-k]")) {
          const on = picked.has(r.dataset.k) || r.classList.contains("is-in");
          r.classList.toggle("is-selected", on);
          r.querySelector(".pick-check").setAttribute("aria-checked", on ? "true" : "false");
        }
        if (lib) libLabels(lib);
      };

      // ряд с флажком
      function pickRow(k, t, data, { inPlaylist = false, sub } = {}) {
        const box = el("button", { type: "button", class: "pick-check", role: "checkbox", "aria-checked": inPlaylist ? "true" : "false",
          "aria-disabled": inPlaylist ? "true" : "false", "aria-label": `${inPlaylist ? "Уже в плейлисте" : "Выбрать"}: ${t.artist} — ${t.title}` },
          el("span", { class: "sel-check", "aria-hidden": "true" }, icon("check")));
        const row = el("li", { class: "li li-2 pick-row" + (inPlaylist ? " is-in" : ""), "data-k": k },
          box, pic(t.cover, { cls: "li-thumb pick-thumb", ph: "music_note" }),
          el("span", { class: "li-text" }, el("span", { class: "li-headline", text: t.title }),
            el("span", { class: "li-supporting", text: inPlaylist ? "уже в плейлисте" : sub || [t.artist, t.album, t.duration].filter(Boolean).join(" · ") })));
        const toggle = () => {
          if (inPlaylist) { announce("Этот трек уже в плейлисте"); return; }
          if (picked.has(k)) picked.delete(k); else picked.set(k, data);
          haptic("select");
          sync();
          announce(`Выбрано: ${picked.size}`);
        };
        row.addEventListener("click", toggle);
        box.addEventListener("click", (ev) => { ev.stopPropagation(); toggle(); });
        return row;
      }
      const selectAll = (rows) => {
        let added = 0;
        for (const r of rows) if (!r.classList.contains("is-in") && !picked.has(r.dataset.k)) { picked.set(r.dataset.k, r._data); added++; }
        if (!added) for (const r of rows) picked.delete(r.dataset.k); // всё уже было отмечено — снять
        sync();
        announce(`Выбрано: ${picked.size}`);
      };

      // ------------------------------------------------ медиатека: фильтр + быстрые чипы исполнителей
      const libInput = el("input", { type: "search", class: "filter-input", placeholder: "Поиск по медиатеке", "aria-label": "Поиск по медиатеке", autocomplete: "off", maxlength: "200" });
      const libChips = el("div", { class: "chips pick-chips" });
      const libList = el("div");
      libView.append(el("div", { class: "filter-box pick-search" }, icon("search"), libInput), libChips, libList);
      // без потолка в 300: страницы по 100 с бесконечной прокруткой; «Выбрать все» — весь результат поиска
      // (догружаются только данные страницами по 500, ряды рисуются по мере прокрутки)
      const LPAGE = 100;
      let libSeq = 0, lib = null, libIo = null;
      const libPath = (st, limit) => "/library?" + new URLSearchParams({ limit: String(limit), offset: String(st.offset), sort: "artist", order: "asc", q: st.q });
      async function loadLib() {
        const my = ++libSeq;
        const st = lib = { my, q: libInput.value.trim(), items: [], ids: new Set(), offset: 0, total: 0, done: false, busy: false, shown: 0, ul: null };
        if (libIo) libIo.disconnect();
        mount(libList, skeletonList(6));
        try { await libFetch(st, LPAGE); } catch (e) { if (st === lib && !isAbort(e)) mount(libList, errorState(e, loadLib)); return; }
        if (st === lib) renderLib(st);
      }
      // следующая страница данных: дубликаты отбрасываем; старый сервер без offset вернёт то же — конец
      async function libFetch(st, limit) {
        const r = await api(libPath(st, limit));
        if (st !== lib) return;
        const items = r.items || [];
        let fresh = 0;
        for (const t of items) if (!st.ids.has(t.id)) { st.ids.add(t.id); st.items.push(t); fresh++; }
        st.offset += items.length;
        st.total = Number(r.total) || st.items.length;
        if (!fresh || items.length < limit || st.items.length >= st.total) { st.done = true; st.total = Math.max(st.total, st.items.length); }
      }
      // занятая кнопка: вместо иконки — маленький неопределённый круговой индикатор, фокус не теряется
      const spin = (btn, on) => {
        btn.classList.toggle("is-busy", on);
        btn.setAttribute("aria-busy", on ? "true" : "false");
        btn.setAttribute("aria-disabled", on ? "true" : "false");
        const sp = btn.querySelector(":scope > .btn-spin");
        if (on && !sp) { const w = wavyCircular({ size: 20, cls: "btn-spin btn-icon", thin: true }); w.setAttribute("aria-hidden", "true"); btn.prepend(w); }
        if (!on && sp) sp.remove();
      };
      const libRow = (t) => { const row = pickRow("L" + t.id, t, { kind: "lib", t }, { inPlaylist: inPl.has(t.id) }); row._data = { kind: "lib", t }; return row; };
      const libFree = (st) => st.items.filter((t) => !inPl.has(t.id));
      const libAllOn = (st) => { if (!st.done) return false; const f = libFree(st); return f.length > 0 && f.every((t) => picked.has("L" + t.id)); };
      function libLabels(st) {
        if (!st.ul) return;
        // N — сколько добавится: без уже лежащих в плейлисте (пока не всё загружено и такие есть — без числа)
        const n = st.done ? libFree(st).length : inPl.size ? 0 : st.total;
        if (!st.allBusy) setText(st.allBtn.querySelector(".btn-label"), libAllOn(st) ? "Снять все" : n ? `Выбрать все · ${n}` : "Выбрать все");
        const left = st.total - st.shown;
        st.moreBox.hidden = left <= 0;
        setText(st.moreBtn.querySelector(".btn-label"), `Показать ещё · ${Math.max(0, left)}`);
      }
      function renderLib(st) {
        if (!st.items.length) {
          const q = st.q;
          mount(libList, emptyState({ icon: "filter_list", title: q ? "Ничего не нашлось" : "Медиатека пуста", text: q ? `Нет треков с «${q}».` : "Найдите музыку во вкладке «Поиск в сети».", compact: true }));
          return;
        }
        if (!st.ul) {
          st.allBtn = button({ label: "Выбрать все", icon: "done_all", variant: "text", onClick: () => pickAllLib(st) });
          st.ul = list([], "pick-list");
          st.moreBtn = button({ label: "Показать ещё", icon: "expand_more", variant: "tonal", onClick: () => moreLib(st) });
          st.moreBox = el("div", { class: "row-actions pick-more" }, st.moreBtn);
          mount(libList, [el("div", { class: "pick-allrow" }, st.allBtn), st.ul, st.moreBox]);
          // бесконечная прокрутка: «Показать ещё» подъезжает к краю листа — следующая порция
          if (!libIo && "IntersectionObserver" in window) libIo = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting) && lib && lib.ul && !lib.moreBox.hidden) moreLib(lib); }, { rootMargin: "400px 0px" });
        }
        const to = Math.min(st.items.length, st.shown + LPAGE);
        if (to > st.shown) { st.ul.append(...st.items.slice(st.shown, to).map(libRow)); st.shown = to; }
        libLabels(st);
        if (libIo) { libIo.disconnect(); if (!st.moreBox.hidden) libIo.observe(st.moreBox); }
        sync();
      }
      // ещё порция: из уже догруженных данных или следующая страница с сервера
      async function moreLib(st) {
        if (st !== lib || st.busy) return;
        if (st.shown < st.items.length || st.done) { renderLib(st); return; }
        st.busy = true;
        st.moreBtn.setAttribute("aria-disabled", "true"); // не disabled: фокус остаётся на кнопке
        try { await libFetch(st, LPAGE); if (st === lib) renderLib(st); } catch (e) {
          if (st === lib && !isAbort(e)) snackbar(`Не удалось загрузить ещё: ${e.message}`, { error: true, action: "Повторить", onAction: () => moreLib(st) });
        } finally { st.busy = false; st.moreBtn.setAttribute("aria-disabled", "false"); }
      }
      // «Выбрать все» — весь результат поиска: недостающие страницы данных по 500, без отрисовки всех рядов
      async function pickAllLib(st) {
        if (st.allBusy) return;
        if (libAllOn(st)) {
          for (const t of st.items) picked.delete("L" + t.id);
          sync(); libLabels(st); announce(`Выбрано: ${picked.size}`);
          return;
        }
        if (!st.done) {
          st.allBusy = true;
          spin(st.allBtn, true);
          const label = st.allBtn.querySelector(".btn-label");
          try {
            while (!st.done && st === lib) { setText(label, `Загружаю… ${st.items.length} из ${st.total}`); await libFetch(st, 500); }
          } catch (e) {
            if (st === lib && !isAbort(e)) snackbar(`Не удалось выбрать все: ${e.message}`, { error: true });
            return;
          } finally { st.allBusy = false; spin(st.allBtn, false); if (st === lib) libLabels(st); }
          if (st !== lib) return;
        }
        for (const t of libFree(st)) picked.set("L" + t.id, { kind: "lib", t });
        sync(); libLabels(st);
        announce(`Выбрано: ${picked.size}`);
      }
      libInput.addEventListener("input", debounce(loadLib, 300));
      api("/library/artists").then((r) => {
        mount(libChips, (r.items || []).slice(0, 14).map((a) => chip({ label: a.name, onClick: () => { libInput.value = a.name; loadLib(); } })));
      }).catch(() => {});

      // ------------------------------------------------ поиск в сети: треки; альбомы и исполнители открываются внутри
      const netInput = el("input", { type: "search", class: "filter-input", placeholder: "Исполнитель, трек или альбом", "aria-label": "Поиск в сети", autocomplete: "off", maxlength: "200" });
      const netList = el("div");
      const crumbs = el("div", { class: "pick-crumbs" });
      netView.append(el("div", { class: "filter-box pick-search" }, icon("travel_explore"), netInput), crumbs, netList);
      let netSeq = 0, lastRes = null;
      const catRows = (tracks, token, refresh) => tracks.map((t) => {
        const ctx = { body: () => ({ token: token(), kind: "track", i: t.i }), refresh };
        const k = "C" + (t.isrc || t.key || `${t.artist}|${t.title}`);
        const data = { kind: "cat", t, ctx };
        const row = pickRow(k, t, data);
        row._data = data;
        return row;
      });
      async function searchNet() {
        const my = ++netSeq;
        const q = netInput.value.trim();
        crumbs.replaceChildren();
        if (q.length < 2) { mount(netList, el("p", { class: "note", text: "Найдите треки в каталоге — они скачаются и появятся в плейлисте." })); return; }
        mount(netList, skeletonList(6));
        let res;
        try { res = await api("/search?q=" + encodeURIComponent(q)); } catch (e) { if (my === netSeq && !isAbort(e)) mount(netList, errorState(e, searchNet)); return; }
        if (my !== netSeq) return;
        lastRes = res;
        renderNet(res);
      }
      function renderNet(res) {
        crumbs.replaceChildren();
        const tok = () => res.token;
        const refresh = async () => { const r = await api("/search?q=" + encodeURIComponent(netInput.value.trim())); res.token = r.token; };
        const parts = [];
        if (res.tracks && res.tracks.length) {
          const rows = catRows(res.tracks, tok, refresh);
          parts.push(el("div", { class: "pick-allrow" }, el("h3", { class: "q-sec", text: "Треки" }), button({ label: "Выбрать все", icon: "done_all", variant: "text", onClick: () => selectAll(rows) })), list(rows, "pick-list"));
        }
        if (res.albums && res.albums.length) {
          parts.push(el("h3", { class: "q-sec", text: "Альбомы" }), list(res.albums.slice(0, 12).map((a) => navRow(pic(a.cover, { cls: "li-thumb", ph: "album" }), a.title, [a.artist, a.year].filter(Boolean).join(" · "), () => openAlbum(a))), "pick-list"));
        }
        if (res.artists && res.artists.length) {
          parts.push(el("h3", { class: "q-sec", text: "Исполнители" }), list(res.artists.slice(0, 8).map((a) => navRow(pic(a.picture, { cls: "li-avatar", ph: initial(a.name) }), a.name, fmtFans(a.fans) || "Исполнитель", () => openArtist(a))), "pick-list"));
        }
        mount(netList, parts.length ? parts : emptyState({ icon: "travel_explore", title: "Ничего не нашлось", compact: true }));
        sync();
      }
      const navRow = (lead, title, sub, onClick) => {
        const b = el("button", { type: "button", class: "li-main" }, el("span", { class: "li-leading" }, lead),
          el("span", { class: "li-text" }, el("span", { class: "li-headline", text: title }), el("span", { class: "li-supporting", text: sub })), icon("chevron_right", { class: "li-chev" }));
        b.addEventListener("click", onClick);
        return el("li", { class: "li li-2 is-action" }, b);
      };
      const back = (label) => {
        const b = button({ label: "К результатам", icon: "arrow_back", variant: "text", onClick: () => { if (lastRes) renderNet(lastRes); } });
        mount(crumbs, [b, el("span", { class: "pick-crumb", text: label })]);
      };
      async function openAlbum(a) {
        const my = ++netSeq;
        back(a.title);
        mount(netList, skeletonList(6, { thumb: false }));
        let al;
        try { al = await api("/catalog/album?id=" + encodeURIComponent(a.id)); } catch (e) { if (my === netSeq) mount(netList, errorState(e, () => openAlbum(a))); return; }
        if (my !== netSeq) return;
        const tok = () => al.token;
        const refresh = async () => { const r = await api("/catalog/album?id=" + encodeURIComponent(a.id)); al.token = r.token; };
        const rows = catRows(al.tracks.map((t) => ({ ...t, cover: t.cover || al.cover })), tok, refresh);
        mount(netList, [el("div", { class: "pick-allrow" }, el("h3", { class: "q-sec", text: `${al.artist} — ${al.title}` }), button({ label: `Все треки · ${rows.length}`, icon: "done_all", variant: "text", onClick: () => selectAll(rows) })), list(rows, "pick-list")]);
        sync();
      }
      async function openArtist(a) {
        const my = ++netSeq;
        back(a.name);
        mount(netList, skeletonList(6));
        let ar;
        try { ar = await api("/catalog/artist?id=" + encodeURIComponent(a.id)); } catch (e) { if (my === netSeq) mount(netList, errorState(e, () => openArtist(a))); return; }
        if (my !== netSeq) return;
        const tok = () => ar.token;
        const refresh = async () => { const r = await api("/catalog/artist?id=" + encodeURIComponent(a.id)); ar.token = r.token; };
        const rows = catRows(ar.tracks, tok, refresh);
        const allBtn = button({ label: "Все треки исполнителя", icon: "library_music", variant: "tonal", onClick: async () => {
          mount(netList, loadingState("Собираю треки со всех альбомов — это может занять до минуты…"));
          const my2 = ++netSeq;
          let all;
          try { all = await api("/catalog/artist/tracks?id=" + encodeURIComponent(a.id)); } catch (e) { if (my2 === netSeq) mount(netList, errorState(e, () => openArtist(a))); return; }
          if (my2 !== netSeq) return;
          const allRows = catRows(all.tracks, () => all.token, async () => { const r = await api("/catalog/artist/tracks?id=" + encodeURIComponent(a.id)); all.token = r.token; });
          mount(netList, [el("div", { class: "pick-allrow" }, el("h3", { class: "q-sec", text: `${ar.name}: все треки` }), button({ label: `Выбрать все · ${allRows.length}`, icon: "done_all", variant: "text", onClick: () => selectAll(allRows) })), list(allRows, "pick-list")]);
          sync();
        } });
        mount(netList, [el("div", { class: "pick-allrow" }, el("h3", { class: "q-sec", text: `${ar.name}: популярное` }), button({ label: "Выбрать все", icon: "done_all", variant: "text", onClick: () => selectAll(rows) })),
          list(rows, "pick-list"), el("div", { class: "row-actions" }, allBtn),
          ar.albums && ar.albums.length ? [el("h3", { class: "q-sec", text: "Альбомы" }), list(ar.albums.slice(0, 20).map((al) => navRow(pic(al.cover, { cls: "li-thumb", ph: "album" }), al.title, al.year || "", () => openAlbum(al))), "pick-list")] : null]);
        sync();
      }
      netInput.addEventListener("input", debounce(searchNet, 500));
      netInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); searchNet(); } });

      async function submit() {
        if (!picked.size) { announce("Ничего не выбрано"); return; }
        const items = [...picked.values()];
        const lib = items.filter((x) => x.kind === "lib").map((x) => x.t.id);
        const cat = items.filter((x) => x.kind === "cat");
        addBtn.setAttribute("aria-disabled", "true");
        let added = 0, skipped = 0, queued = 0, err = null;
        try {
          if (lib.length) { const r = await pl.add(p.id, { track_ids: lib }); added = r.added || 0; skipped = r.skipped || 0; for (const x of items) if (x.kind === "lib") picked.delete("L" + x.t.id); }
          if (cat.length) queued = await downloadItems(cat, { playlist: { id: p.id, name: p.name }, quiet: true });
        } catch (e) { err = e; } finally { if (added || queued) changed(p.id); }
        const msg = [lib.length && !err ? `Добавлено: ${nTracks(added)}${skipped ? ` · уже были: ${skipped}` : ""}` : "",
          queued ? `Скачиваю ${nTracks(queued)} — появятся в плейлисте по мере загрузки` : cat.length ? "Скачать не удалось" : ""].filter(Boolean).join(" · ");
        if (err || (cat.length && !queued)) {
          haptic("error");
          snackbar([msg, err && err.message].filter(Boolean).join(" · "), { error: true });
          addBtn.setAttribute("aria-disabled", "false");
          sync();
          return; // лист остаётся открытым, выбор (что не добавилось) — тоже
        }
        haptic("success");
        snackbar(msg);
        close();
        if (onAdded) onAdded();
      }
      loadLib();
      searchNet();
      sync();
      setTimeout(() => libInput.focus(), 60);
    },
  });
}
