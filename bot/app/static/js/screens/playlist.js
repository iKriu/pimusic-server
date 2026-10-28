// Плейлисты: сетка (сегмент «Плейлисты» медиатеки), страница плейлиста (шапка-мозаика, участники, «Общий»,
// Слушать/Перемешать, меню, режим правки с перетаскиванием и «Выше/Ниже»), экран вступления по приглашению.
import { el, icon, mount, nTracks, fmtTotal } from "../dom.js";
import {
  button, iconButton, list, listItem, grid, card, sheet, dialog, snackbar, emptyState, errorState, skeletonGrid, skeletonList,
  loadingState, openMenu, switchRow, sectionTitle, announce,
} from "../ui.js";
import { flexHeader } from "../header.js";
import { contentColor } from "../theme.js";
import { open, back, go, shell } from "../shell.js";
import { haptic, startParam } from "../tg.js";
import { isAbort } from "../api.js";
import * as store from "../store.js";
import { libraryRow, playButtons, onLibraryDeleted, currentFab, saveOffline } from "../libtrack.js";
import { cacheOn } from "../offline.js";
import { sortable } from "../reorder.js";
import { selectable } from "../select.js";
import { sortLocal, sortChip, getSort, sortTracks } from "../libsort.js";
import { openTrackPicker } from "../picker.js";
import { addMany } from "../player.js";
import { addToPlaylist } from "../playlists.js";
import {
  pl, onPlaylists, changed, mosaic, avatars, sharedBadge, metaLine, nMembers, createPlaylistDialog, editPlaylistDialog,
  sharePlaylist, copyInvite,
} from "../playlists.js";

const me = () => (store.state.me && store.state.me.login) || "";
// ссылка-приглашение включена: invite_enabled (новый сервер), иначе — по shared/invite
const linkOn = (p) => ("invite_enabled" in p ? !!p.invite_enabled : !!p.shared && !!p.invite);
const unavailable = (e) => e && (e.status === 404 || e.status === 403);

// ---------------------------------------------------------------- сетка в медиатеке
export async function renderPlaylists(content, { needle = "", signal, setSub, sort = null }) {
  mount(content, skeletonGrid(6));
  let r;
  try { r = await pl.list(signal); } catch (e) {
    if (isAbort(e)) return;
    setSub("");
    mount(content, e.status === 404
      ? emptyState({ shape: "softBurst", icon: "cloud_off", title: "Плейлисты пока недоступны", text: "Сервер ещё не обновлён — попробуйте позже." })
      : errorState(e, () => renderPlaylists(content, { needle, signal, setSub })));
    announce(e.status === 404 ? "Плейлисты пока недоступны" : `Не удалось загрузить плейлисты: ${e.message}`, e.status !== 404);
    return;
  }
  const all = r.items || [];
  const sorted = sort ? sortLocal(all, sort.sort, sort.order, (p) => p.name) : all;
  const items = sorted.filter((p) => !needle || p.name.toLowerCase().includes(needle));
  const shared = all.filter((p) => p.shared).length;
  setSub((needle ? `Найдено: ${items.length} из ` : "") + `${all.length} ${all.length % 10 === 1 && all.length % 100 !== 11 ? "плейлист" : [2, 3, 4].includes(all.length % 10) && ![12, 13, 14].includes(all.length % 100) ? "плейлиста" : "плейлистов"}` + (shared && !needle ? ` · общих: ${shared}` : ""));
  if (!all.length) {
    announce("Плейлистов пока нет. Создайте первый плейлист.");
    mount(content, emptyState({ shape: "cookie4", icon: "queue_music", title: "Создайте первый плейлист",
      text: "Собирайте треки из медиатеки в подборки — личные или общие с друзьями.",
      action: button({ label: "Новый плейлист", icon: "add", variant: "filled", onClick: newPlaylist }) }));
    return;
  }
  const cards = items.map(playlistCard);
  if (!needle) cards.unshift(newCard());
  mount(content, items.length || !needle ? grid(cards, "grid-playlists") : emptyState({ shape: "pentagon", icon: "filter_list", title: "Ничего не нашлось", text: `Нет плейлистов с «${needle}».` }));
}

async function newPlaylist() {
  const p = await createPlaylistDialog();
  if (p) openPlaylist(p.id, p, { from: "list" });
}

function newCard() {
  const media = el("span", { class: "pl-new-media" }, icon("add"));
  return card({ media, title: "Новый плейлист", subtitle: "Личный или общий", cls: "card-pl card-pl-new", label: "Новый плейлист", onClick: newPlaylist });
}

function playlistCard(p) {
  const media = el("span", { class: "pl-media" }, mosaic(p.covers), p.shared ? el("span", { class: "pl-media-badge", title: "Общий" }, icon("group", { fill: true })) : null);
  const sub = [p.role === "member" ? `от ${p.owner}` : "", nTracks(p.count || 0)].filter(Boolean).join(" · ");
  return card({ media, title: p.name, subtitle: sub, cls: "card-pl", label: `Плейлист ${p.name}${p.shared ? ", общий" : ""}, ${sub}`,
    onClick: () => openPlaylist(p.id, p, { from: "list", origin: media }) });
}

// ---------------------------------------------------------------- страница плейлиста
export function openPlaylist(id, hint = {}, { from = "detail", origin = null } = {}) {
  open((page) => buildPlaylist(page, id, hint), { kind: "playlist", from, origin });
}

function buildPlaylist(page, id, hint) {
  const H = flexHeader(page, { kind: "playlist", overline: "Плейлист", title: hint.name || "Плейлист" });
  mount(H.cover, mosaic(hint.covers, { cls: "fh-mosaic" }));
  const body = el("div", { class: "page-body" });
  page.scroll.append(H.el, body);
  const nowFab = currentFab(page.el, page.scroll, body);
  page.onDestroy(() => nowFab.destroy());
  if (hint.covers && hint.covers[0]) contentColor(page.el, hint.covers[0]);
  let p = null, editing = false, colorFrom = hint.covers && hint.covers[0];

  const off = onPlaylists((pid, what) => { if (pid === id && what !== "local") load(true); });
  page.onDestroy(off);

  const source = () => ({ kind: "playlist", id, title: p ? p.name : hint.name || "Плейлист" });
  const canEdit = () => p && (p.role === "owner" || p.role === "member");
  // сортировка — своя у каждого плейлиста; перестановка (перетаскивание, «Переместить») — только в «Порядке плейлиста»
  const SV = "pl:" + id;
  const manual = () => getSort(SV).sort === "manual";
  let seed = 0; // «В случайном порядке» — один порядок до следующего выбора
  // сервер уже отсортировал (ответ с "sort") — как есть; старый сервер — сортируем на клиенте (все треки загружены)
  const shown = () => {
    const st = getSort(SV), list = p ? p.tracks || [] : [];
    return p && p.sort === st.sort && (p.order === undefined || p.order === st.order || st.sort === "random") ? list.slice() : sortTracks(list, st.sort, st.order);
  };
  const query = () => {
    const st = getSort(SV);
    if (st.sort === "manual") return "";
    const qs = new URLSearchParams({ sort: st.sort, order: st.order });
    if (st.sort === "random") qs.set("seed", String(seed || (seed = Math.floor(Math.random() * 1e9) + 1)));
    return "?" + qs;
  };
  const addTracks = () => { if (p) openTrackPicker(p, { onAdded: () => load(true) }); };

  // режим выбора: убрать, переместить, в другой плейлист, в очередь
  page.onDestroy(() => selCtl.stop(true));
  const selCtl = selectable(page.el, { rows: () => [...body.querySelectorAll(".pl-tracks .ltrack")], key: (r) => r.dataset.sel, item: (r) => r._track,
    actions: (items, ctl) => [
      canEdit() ? { icon: "remove_circle", label: "Убрать", danger: true, run: async (ts) => { ctl.stop(); await removeItems(ts); } } : null,
      canEdit() && manual() ? { icon: "swap_vert", label: "Переместить", run: (ts, c, btn) => {
        btn.setAttribute("aria-haspopup", "menu");
        openMenu(btn, [
          { icon: "vertical_align_top", label: "В начало", onClick: () => moveBlock(ts, "top") },
          { icon: "arrow_upward", label: "Выше на одну", onClick: () => moveBlock(ts, "up") },
          { icon: "arrow_downward", label: "Ниже на одну", onClick: () => moveBlock(ts, "down") },
          { icon: "vertical_align_bottom", label: "В конец", onClick: () => moveBlock(ts, "bottom") },
        ]);
      } } : null,
      { icon: "playlist_add", label: "В плейлист", run: (ts) => { addToPlaylist({ add: { track_ids: ts.map((t) => t.id) }, title: nTracks(ts.length) }); ctl.stop(); } },
      // одной вставкой: одна перерисовка очереди и одно сохранение (а не по треку)
      { icon: "add_to_queue", label: "В очередь", run: (ts) => { addMany(ts); snackbar(`В очереди: ${nTracks(ts.length)}`); ctl.stop(); } },
    ],
    // «Выбрать все» — весь плейлист, а не только показанные 200
    loadAll: async () => shown(), itemKey: (t) => String(t.item_id ?? t.id), total: () => (p && p.tracks ? p.tracks.length : null) });

  // трек удалили из медиатеки (здесь или на другом экране): в плейлисте он остаётся — перечитать, чтобы
  // показать «Не в медиатеке» (и вернуть ряд, если его убрало меню трека)
  page.onDestroy(onLibraryDeleted((ids) => { if (p && (p.tracks || []).some((t) => ids.has(String(t.id)))) load(true); }));

  // перестановка выбранных блоком: на сервер — последовательные move в порядке итоговых позиций
  function moveBlock(ts, where) {
    const ids = new Set(ts.map((t) => t.item_id));
    const cur = p.tracks.slice();
    let next = cur.slice();
    if (where === "top") next = [...cur.filter((t) => ids.has(t.item_id)), ...cur.filter((t) => !ids.has(t.item_id))];
    else if (where === "bottom") next = [...cur.filter((t) => !ids.has(t.item_id)), ...cur.filter((t) => ids.has(t.item_id))];
    else if (where === "up") { for (let i = 1; i < next.length; i++) if (ids.has(next[i].item_id) && !ids.has(next[i - 1].item_id)) [next[i - 1], next[i]] = [next[i], next[i - 1]]; }
    else for (let i = next.length - 2; i >= 0; i--) if (ids.has(next[i].item_id) && !ids.has(next[i + 1].item_id)) [next[i], next[i + 1]] = [next[i + 1], next[i]];
    const sim = cur.slice();
    p.tracks = next;
    render();
    haptic("select");
    announce(`Перемещено: ${nTracks(ts.length)}`);
    // в начало/в конец — одним запросом move-many (старый сервер — по одному)
    if (where === "top" || where === "bottom") {
      const sel = next.filter((t) => ids.has(t.item_id)).map((t) => t.item_id);
      const one = () => { for (let k = 0; k < next.length; k++) { const t = next[k], at = sim.indexOf(t); if (at === k) continue; sim.splice(at, 1); sim.splice(k, 0, t); moveChain = moveChain.then(() => pl.move(id, t.item_id, k)); } };
      moveChain = moveChain.then(() => pl.moveMany(id, sel, where === "top" ? 0 : 1e6)).catch((e) => { if (e.status === 404 && e.code !== "not_found") one(); else throw e; })
        .then(() => changed(id, "local"), (e) => { snackbar(e.message || "Не удалось переставить", { error: true }); moveChain = Promise.resolve(); load(true); });
      return;
    }
    for (let k = 0; k < next.length; k++) {
      const t = next[k], at = sim.indexOf(t);
      if (at === k) continue;
      sim.splice(at, 1); sim.splice(k, 0, t);
      moveChain = moveChain.then(() => pl.move(id, t.item_id, k));
    }
    moveChain = moveChain.then(() => changed(id, "local"), (e) => { snackbar(e.message || "Не удалось переставить", { error: true }); moveChain = Promise.resolve(); load(true); });
  }

  let loadSeq = 0, pendingRender = false;
  async function load(quiet = false) {
    const my = ++loadSeq;
    if (!quiet || !p) {
      H.setActions(el("div", { class: "skel skel-btn", "aria-hidden": "true" }));
      mount(body, skeletonList(6));
    }
    let r;
    try {
      try { r = await page.sc.get(`/playlists/${id}${query()}`); } catch (e) {
        if (e.status !== 400 || !query()) throw e;
        r = await page.sc.get(`/playlists/${id}`); // старый сервер без sort — порядок плейлиста, сортировка на клиенте
      }
    } catch (e) {
      if (isAbort(e) || my !== loadSeq) return;
      if (quiet && p && !unavailable(e)) return; // фоновое обновление не удалось — оставляем то, что видно
      p = null;
      H.setActions(null);
      H.setBarActions(null);
      if (unavailable(e)) {
        // без обложки-заглушки и без дубля заголовка: только состояние с действием
        page.el.classList.add("is-state");
        H.setTitle("Плейлист");
        mount(body, emptyState({ shape: "softBurst", icon: "link_off", title: "Плейлист недоступен",
          text: "Его удалили, выключили общий доступ или убрали вас из участников.",
          action: button({ label: "К плейлистам", icon: "queue_music", variant: "tonal", onClick: () => { back(); changed(id, "gone"); } }) }));
        announce("Плейлист недоступен. Его удалили, выключили общий доступ или убрали вас из участников.");
      } else { mount(body, errorState(e, () => load())); announce(`Не удалось открыть плейлист: ${e.message}`, true); }
      return;
    }
    if (my !== loadSeq) return;
    p = r;
    page.el.classList.remove("is-state");
    if (body.querySelector(".is-sorting")) { pendingRender = true; return; }
    render();
  }
  body.addEventListener("pointerup", () => setTimeout(() => { if (pendingRender) { pendingRender = false; render(); } }, 0));

  const PAGE = 200;
  function render() {
    H.setTitle(p.name);
    H.setOverline("Плейлист");
    mount(H.cover, mosaic(p.covers, { cls: "fh-mosaic" }));
    if (p.covers && p.covers[0] && p.covers[0] !== colorFrom) { colorFrom = p.covers[0]; contentColor(page.el, colorFrom); }
    if (editing && !manual()) editing = false;
    const tracks = editing ? p.tracks || [] : shown(); // порядок на экране = порядок воспроизведения
    const people = [p.owner, ...(p.members || []).map((m) => m.login)];
    H.setSub([
      p.description ? el("p", { class: "fh-desc", text: p.description }) : null,
      el("div", { class: "pl-meta" },
        p.shared ? sharedBadge() : null,
        p.shared ? el("button", { type: "button", class: "pl-people", "aria-label": `Участники: ${people.join(", ")}`, onclick: () => membersSheet() }, avatars(people), el("span", { text: nMembers(people.length) })) : null,
        el("span", { class: "fh-meta", text: [p.role === "member" ? `от ${p.owner}` : "", metaLine({ count: tracks.length, seconds: tracks.reduce((s, t) => s + (t.seconds || 0), 0) })].filter(Boolean).join(" · ") })),
    ]);
    const more = iconButton("more_vert", "Действия с плейлистом", { onClick: () => menu(more) });
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    const edit = editing
      ? button({ label: "Готово", icon: "done", variant: "filled", cls: "pl-done", onClick: () => setEditing(false) })
      : canEdit() && tracks.length && manual() ? iconButton("edit", "Изменить порядок и состав", { onClick: () => setEditing(true) }) : null;
    H.setBarActions([edit, more]);
    const addBtn = canEdit() ? button({ label: "Добавить треки", icon: "add", variant: tracks.length ? "outlined" : "filled", cls: "pl-add", onClick: addTracks }) : null;
    H.setActions(el("div", { class: "pl-actions" }, tracks.length ? playButtons(() => p.tracks, source) : null, addBtn));
    page.el.classList.toggle("is-empty", !tracks.length);
    if (!tracks.length) {
      announce("В плейлисте пока пусто");
      mount(body, emptyState({ shape: "clover", icon: "playlist_add", title: "В плейлисте пока пусто",
        text: "Добавьте треки из медиатеки или найдите новые в сети — они скачаются и появятся здесь.",
        action: canEdit() ? button({ label: "Добавить треки", icon: "add", variant: "filled", onClick: addTracks })
          : button({ label: "Открыть медиатеку", icon: "library_music", variant: "tonal", onClick: () => { go("library"); shell.roots.library.select("tracks"); } }) }));
      return;
    }
    const notOwned = tracks.filter((t) => t.owned === false).length;
    const parts = [];
    if (notOwned && !editing) {
      parts.push(el("div", { class: "pl-partial" }, icon("info"), el("p", { text: `${nTracks(notOwned)} нет в вашей медиатеке — слушать можно, а сохранить к себе — через меню «Сохранить в медиатеку».` })));
    }
    const row = (t, i) => libraryRow(t, {
      thumb: true, tracks, index: i, source: source(), deletable: t.owned !== false,
      sub: [t.artist, p.shared && t.added_by && t.added_by !== me() ? `добавил(а) ${t.added_by}` : t.album].filter(Boolean).join(" · "),
      menu: canEdit() ? () => [{ icon: "remove_circle", label: "Убрать из плейлиста", onClick: () => removeItems([t]) }] : null,
    });
    if (editing) parts.push(editList(tracks));
    else {
      if (tracks.length > 1) parts.push(el("div", { class: "lib-sortbar pl-sortbar" }, sortChip(SV, () => {
        seed = 0;
        render(); // сразу — на клиенте, затем порядок сервера (тот же, с учётом его правил)
        load(true);
        announce(manual() ? "Порядок плейлиста" : "Отсортировано");
      })));
      const ul = list(tracks.slice(0, PAGE).map(row), "pl-tracks");
      parts.push(ul);
      if (tracks.length > PAGE) {
        const more = button({ label: `Показать ещё · ${tracks.length - PAGE}`, icon: "expand_more", variant: "tonal", onClick: () => {
          const n = ul.children.length;
          const next = tracks.slice(n, n + PAGE).map((t, k) => row(t, n + k));
          ul.append(...next);
          const left = tracks.length - ul.children.length;
          if (left > 0) more.querySelector(".btn-label").textContent = `Показать ещё · ${left}`; else more.parentNode.remove();
          const f = next[0] && next[0].querySelector(".li-main");
          if (f) f.focus();
        } });
        parts.push(el("div", { class: "row-actions pl-more" }, more));
      }
    }
    mount(body, parts);
  }

  function setEditing(on) {
    editing = on;
    page.el.classList.toggle("is-editing", on);
    haptic("light");
    render();
    announce(on ? "Режим правки: перетаскивайте ручки или нажимайте «Выше» и «Ниже». «Готово» — выйти." : "Правка завершена");
    // вход — на первую ручку; выход — на кнопку «Изменить», откуда пришли
    const first = on ? body.querySelector(".drag-handle") : page.el.querySelector(".fh-bar-actions .ibtn");
    if (first) first.focus({ preventScroll: true });
  }

  // режим правки: ручка (перетаскивание; ↑/↓ с клавиатуры), «Выше», «Ниже», «Убрать».
  // Перестановка меняет DOM на месте (без перерисовки) — фокус и позиция прокрутки не теряются.
  function editList(tracks) {
    const ul = list(tracks.map((t) => editRow(t)), "pl-edit");
    sortable(ul, { scroller: page.scroll, onMove: (from, to) => moveItem(from, to), label: (i, n) => `Позиция ${i + 1} из ${n}` });
    syncEdit(ul);
    return ul;
  }
  const rowIndex = (li) => [...li.parentNode.children].indexOf(li);
  function editRow(t) {
    const handle = el("button", { type: "button", class: "drag-handle", title: "Перетащите, чтобы переставить" }, icon("drag_indicator"));
    let li;
    const up = iconButton("arrow_upward", `Выше: ${t.title}`, { cls: "pl-up", onClick: () => { const i = rowIndex(li); moveItem(i, i - 1); refocus(li, ".pl-up"); } });
    const down = iconButton("arrow_downward", `Ниже: ${t.title}`, { cls: "pl-down", onClick: () => { const i = rowIndex(li); moveItem(i, i + 1); refocus(li, ".pl-down"); } });
    const rm = iconButton("remove_circle", `Убрать «${t.title}» из плейлиста`, { cls: "pl-rm", onClick: () => removeItems([t]) });
    li = listItem({ leading: handle, headline: t.title, supporting: t.artist, trailing: el("span", { class: "pl-edit-btns" }, up, down, rm), cls: "track pl-edit-row" });
    li.dataset.key = String(t.item_id);
    li._title = t.title;
    return li;
  }
  function syncEdit(ul) {
    const rows = [...ul.children], n = rows.length;
    rows.forEach((li, i) => {
      li.querySelector(".pl-up").disabled = i === 0;
      li.querySelector(".pl-down").disabled = i === n - 1;
      li.querySelector(".drag-handle").setAttribute("aria-label", `Переместить «${li._title}», позиция ${i + 1} из ${n}. Стрелки вверх и вниз — переставить`);
    });
  }
  function refocus(li, sel) {
    const b = li.querySelector(sel);
    (b && !b.disabled ? b : li.querySelector(".drag-handle")).focus();
    const live = document.getElementById("sb-live");
    if (live) { live.textContent = ""; setTimeout(() => { live.textContent = `Позиция ${rowIndex(li) + 1} из ${li.parentNode.children.length}`; }, 60); }
  }

  let moveChain = Promise.resolve();
  void selCtl;
  async function moveItem(from, to) {
    const tracks = p.tracks;
    if (to < 0 || to >= tracks.length || from === to) return;
    const [t] = tracks.splice(from, 1);
    tracks.splice(to, 0, t);
    const ul = body.querySelector(".pl-edit");
    if (ul) {
      const rows = [...ul.children];
      const node = rows[from];
      ul.insertBefore(node, to > from ? rows[to].nextSibling : rows[to]);
      syncEdit(ul);
    }
    haptic("select");
    moveChain = moveChain.then(() => pl.move(id, t.item_id, to)).then(() => changed(id, "local"), (e) => {
      if (isAbort(e)) return;
      haptic("error");
      snackbar(e.message || "Не удалось переставить", { error: true });
      moveChain = Promise.resolve();
      load(true);
    });
  }

  async function removeItems(ts) {
    const ids = new Set(ts.map((t) => t.item_id));
    const removed = p.tracks.map((t, i) => [i, t]).filter(([, t]) => ids.has(t.item_id));
    p.tracks = p.tracks.filter((t) => !ids.has(t.item_id));
    // фокус: на тот же ряд-соседа (или заголовок, если список опустел)
    const focusIdx = removed.length ? removed[0][0] : 0;
    render();
    const rowsNow = body.querySelectorAll(".li-main");
    const target = rowsNow[Math.min(focusIdx, rowsNow.length - 1)] || page.el.querySelector(".fh-title");
    if (target) { if (!target.hasAttribute("tabindex") && target.tagName === "H1") target.setAttribute("tabindex", "-1"); target.focus({ preventScroll: true }); }
    try {
      await pl.remove(id, [...ids]);
      haptic("light");
      snackbar(ts.length === 1 ? `Убрано: ${ts[0].title}` : `Убрано: ${nTracks(ts.length)}`);
      changed(id, "local");
    } catch (e) {
      for (const [i, t] of removed) if (!p.tracks.some((x) => x.item_id === t.item_id)) p.tracks.splice(Math.min(i, p.tracks.length), 0, t);
      render();
      haptic("error");
      snackbar(e.message, { error: true });
    }
  }

  function menu(anchor) {
    const owner = p.role === "owner";
    const notOwned = (p.tracks || []).filter((t) => t.owned === false).length;
    const items = [
      canEdit() ? { icon: "edit_note", label: "Переименовать", supporting: "Название и описание", onClick: async () => { const r = await editPlaylistDialog(p); if (r) load(true); } } : null,
      owner ? { icon: "share", label: "Поделиться", supporting: linkOn(p) ? "Ссылка-приглашение" : "Включить ссылку и отправить", onClick: async () => { await sharePlaylist(p); load(true); } } : null,
      p.shared || owner ? { icon: "group", label: "Участники", supporting: p.shared ? nMembers(1 + (p.members || []).length) : "Личный плейлист", onClick: () => membersSheet() } : null,
      notOwned ? { icon: "library_add", label: "Сохранить в медиатеку", supporting: `${nTracks(notOwned)} нет у вас`, onClick: saveToLibrary } : null,
      canEdit() && (p.tracks || []).length && manual() ? { icon: "reorder", label: "Изменить порядок", onClick: () => setEditing(true) } : null,
      cacheOn() && (p.tracks || []).length ? { icon: "download_for_offline", label: "Скачать на устройство", supporting: `${nTracks(p.tracks.length)} — слушать без сети`, onClick: () => saveOffline(p.tracks, `«${p.name}»`) } : null,
      "divider",
      owner ? { icon: "delete", label: "Удалить плейлист", danger: true, onClick: removePlaylist } : { icon: "logout", label: "Выйти из плейлиста", danger: true, onClick: leave },
    ].filter(Boolean);
    haptic("light");
    anchor.setAttribute("aria-expanded", "true");
    openMenu(anchor, items, () => anchor.setAttribute("aria-expanded", "false"));
  }

  async function saveToLibrary() {
    try {
      const r = await pl.save(id);
      haptic("success");
      const extra = [r.missing ? `нет файла: ${r.missing}` : "", r.failed ? `не удалось: ${r.failed}` : ""].filter(Boolean).join(", ");
      snackbar((r.linked ? `В медиатеку добавлено: ${nTracks(r.linked)}` : extra ? "Ничего не добавлено" : "Все треки уже в медиатеке") + (extra ? ` · ${extra}` : ""), { error: !!r.failed && !r.linked });
      store.loadKeys();
      shell.roots.library.invalidate();
      load(true);
    } catch (e) { haptic("error"); snackbar(e.message, { error: true }); }
  }

  async function removePlaylist() {
    haptic("warning");
    const ok = await dialog({ icon: "delete", headline: "Удалить плейлист?", body: `«${p.name}» пропадёт у вас${p.shared ? " и у всех участников" : ""}. Треки останутся в медиатеке.`,
      actions: [{ label: "Отмена", value: false }, { label: "Удалить", value: true, variant: "danger" }] });
    if (!ok) return;
    try { await pl.del(id); haptic("success"); snackbar(`Плейлист «${p.name}» удалён`); changed(id, "gone"); back(); } catch (e) { haptic("error"); snackbar(e.message, { error: true }); }
  }

  async function leave() {
    haptic("warning");
    const ok = await dialog({ icon: "logout", headline: "Выйти из плейлиста?", body: `«${p.name}» пропадёт из ваших плейлистов. Вернуться можно по новой ссылке от ${p.owner}.`,
      actions: [{ label: "Отмена", value: false }, { label: "Выйти", value: true, variant: "danger" }] });
    if (!ok) return;
    try { await pl.leave(id); haptic("success"); snackbar(`Вы вышли из «${p.name}»`); changed(id, "gone"); back(); } catch (e) { haptic("error"); snackbar(e.message, { error: true }); }
  }

  // участники и доступ по ссылке
  function membersSheet() {
    const owner = p.role === "owner";
    sheet({
      title: "Участники", label: `Участники плейлиста «${p.name}»`,
      build(sbody, close) {
        const render = () => {
          const rows = [listItem({ leading: avatars([p.owner], { max: 1 }), headline: p.owner + (p.owner === me() ? " (вы)" : ""), supporting: "Владелец", lines: 1 })];
          for (const m of p.members || []) {
            rows.push(listItem({ leading: avatars([m.login], { max: 1 }), headline: m.login + (m.login === me() ? " (вы)" : ""), supporting: "Может добавлять и убирать треки", lines: 1,
              trailing: owner ? iconButton("person_remove", `Убрать участника ${m.login}`, { onClick: () => kick(m.login) }) : null }));
          }
          const parts = [list(rows, "sheet-list members")];
          if (owner) {
            parts.push(sectionTitle("Доступ по ссылке"));
            const sw = switchRow({ label: "Вступление по ссылке", supporting: linkOn(p) ? "Кто откроет ссылку в боте — станет участником" : (p.members || []).length ? "Выключено: новые участники не смогут вступить" : "Выключено: плейлист видите только вы", checked: linkOn(p),
              onChange: (v) => toggleShare(v) });
            parts.push(el("div", { class: "sheet-pad" }, sw));
            if (linkOn(p) && p.invite) {
              parts.push(el("div", { class: "sheet-pad invite" },
                el("p", { class: "url", title: p.invite.link || p.invite.app_link }, el("bdi", { text: p.invite.link || p.invite.app_link })),
                el("div", { class: "row-actions invite-btns" },
                  button({ label: "Поделиться", icon: "share", variant: "filled", onClick: () => sharePlaylist(p) }),
                  button({ label: "Скопировать", icon: "content_copy", variant: "tonal", onClick: () => copyInvite(p.invite) }),
                  button({ label: "Новая ссылка", icon: "refresh", variant: "text", onClick: regenerate }))));
            }
          } else {
            parts.push(el("div", { class: "row-actions" }, button({ label: "Выйти из плейлиста", icon: "logout", variant: "text", cls: "btn-danger-text", onClick: () => { close(); leave(); } })));
          }
          mount(sbody, parts);
        };
        const kick = async (login) => {
          const ok = await dialog({ icon: "person_remove", headline: `Убрать ${login}?`, body: "Плейлист пропадёт у участника. Вернуться он сможет только по ссылке.", actions: [{ label: "Отмена", value: false }, { label: "Убрать", value: true, variant: "danger" }] });
          if (!ok) return;
          // сервер при этом меняет ссылку (старая перестаёт работать) и отдаёт новое приглашение
          try { const r = await pl.kick(id, login); p.members = r.members || (p.members || []).filter((m) => m.login !== login); if ("invite" in r) p.invite = r.invite; if ("invite_enabled" in r) p.invite_enabled = r.invite_enabled; if ("shared" in r) p.shared = r.shared; haptic("success"); snackbar(`${login} больше не участник${linkOn(p) ? " · ссылка обновлена" : ""}`); render(); changed(id, "local"); } catch (e) { snackbar(e.message, { error: true }); }
        };
        const toggleShare = async (on) => {
          if (!on && (p.members || []).length) {
            const ok = await dialog({ icon: "link_off", headline: "Выключить ссылку?", body: "По ссылке больше нельзя будет вступить. Нынешние участники останутся — убрать их можно здесь же.", actions: [{ label: "Отмена", value: false }, { label: "Выключить", value: true, variant: "danger" }] });
            if (!ok) { render(); return; }
          }
          const refocus = () => { const s = sbody.querySelector(".sw"); if (s) s.focus({ preventScroll: true }); };
          setTimeout(refocus, 0);
          try { const r = await pl.share(id, on); p.invite = r.invite; p.invite_enabled = "invite_enabled" in r ? r.invite_enabled : on; p.shared = on || (p.members || []).length > 0; haptic("success"); render(); changed(id, "local"); load(true); } catch (e) { snackbar(e.message, { error: true }); render(); }
        };
        const regenerate = async () => {
          const ok = await dialog({ icon: "refresh", headline: "Новая ссылка?", body: "Старая ссылка перестанет работать. Участники останутся.", actions: [{ label: "Отмена", value: false }, { label: "Создать", value: true }] });
          if (!ok) return;
          try { const r = await pl.share(id, true, true); p.invite = r.invite; haptic("success"); snackbar("Новая ссылка готова"); render(); } catch (e) { snackbar(e.message, { error: true }); }
        };
        render();
      },
    });
  }

  load();
}

// ---------------------------------------------------------------- вступление по приглашению
const TOKEN_RE = /^[A-Za-z0-9_-]{4,64}$/;

export function openJoin(token, { from = "list" } = {}) {
  open((page) => buildJoin(page, token), { kind: "join", from });
}

function buildJoin(page, token) {
  const H = flexHeader(page, { kind: "playlist", overline: "Приглашение в плейлист", title: "Плейлист" });
  mount(H.cover, mosaic([], { cls: "fh-mosaic" }));
  const body = el("div", { class: "page-body" });
  page.scroll.append(H.el, body);
  const fail = (e) => {
    page.el.classList.toggle("is-state", unavailable(e) || !e);
    H.setTitle("Приглашение");
    H.setActions(null);
    announce(unavailable(e) || !e ? "Приглашение недействительно. Ссылку отключили или создали новую." : `Ошибка: ${e.message}`, !!e && !unavailable(e));
    mount(body, unavailable(e) || !e
      ? emptyState({ shape: "softBurst", icon: "link_off", title: "Приглашение недействительно", text: (e && e.message) || "Ссылку отключили или создали новую. Попросите владельца прислать свежее приглашение.",
        action: button({ label: "К плейлистам", icon: "queue_music", variant: "tonal", onClick: () => back() }) })
      : errorState(e, load));
  };
  async function load() {
    if (!TOKEN_RE.test(token)) { fail(null); return; }
    H.setActions(el("div", { class: "skel skel-btn", "aria-hidden": "true" }));
    mount(body, loadingState());
    let v;
    try { v = await pl.joinPreview(token, page.sc.signal); } catch (e) { if (!isAbort(e)) fail(e); return; }
    H.setTitle(v.name);
    mount(H.cover, mosaic(v.covers, { cls: "fh-mosaic" }));
    if (v.covers && v.covers[0]) contentColor(page.el, v.covers[0]);
    H.setSub(el("div", { class: "pl-meta" }, sharedBadge(), avatars([v.owner], { max: 1 }), el("span", { class: "fh-meta", text: `от ${v.owner} · ${nTracks(v.count || 0)}` })));
    const openIt = () => { back(); openPlaylist(v.id, { name: v.name, covers: v.covers, shared: true }, { from: "list" }); };
    if (v.already) {
      H.setActions(button({ label: "Открыть плейлист", icon: "queue_music", variant: "filled", size: "m", onClick: openIt }));
      mount(body, el("p", { class: "note join-note", text: "Вы уже участник этого плейлиста." }));
      return;
    }
    const join = button({ label: "Присоединиться", icon: "group_add", variant: "filled", size: "m", onClick: async () => {
      haptic("medium");
      join.disabled = true;
      try {
        const p = await pl.join(token);
        haptic("success");
        snackbar(`Вы в плейлисте «${p.name}»`);
        changed(p.id, "join");
        back();
        openPlaylist(p.id, p, { from: "list" });
      } catch (e) { join.disabled = false; haptic("error"); if (unavailable(e)) fail(e); else snackbar(e.message, { error: true }); }
    } });
    H.setActions(el("div", { class: "pl-actions" }, join, button({ label: "Не сейчас", variant: "text", onClick: () => back() })));
    mount(body, el("p", { class: "note join-note", text: "Участники общего плейлиста могут слушать, добавлять и убирать треки. Выйти можно в любой момент." }));
  }
  load();
}

// ---------------------------------------------------------------- глубокие ссылки: #join=<token>, #playlists, start_param pl_<token>
let spUsed = false;
export function handleDeepLink() {
  const hash = (location.hash || "").replace(/^#/, "");
  let params;
  try { params = new URLSearchParams(hash); } catch { params = new URLSearchParams(); }
  const sp = spUsed ? "" : startParam();
  spUsed = true;
  let token = params.get("join") || "";
  if (!token && sp.startsWith("pl_")) token = sp.slice(3);
  const lib = shell.roots.library;
  if (token) { lib.select("playlists"); go("library"); openJoin(token, { from: "list" }); return true; }
  if (params.has("playlists") || sp === "playlists") { lib.select("playlists"); go("library"); return true; }
  if (params.has("wave") || sp === "wave") { go("wave"); return true; } // ярлык PWA «Моя Волна»
  if (params.has("search")) { go("search", { focus: true }); return true; } // ярлык PWA «Поиск»
  return false;
}

export { fmtTotal };
