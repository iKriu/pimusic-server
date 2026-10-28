// Сортировка и жанры медиатеки: варианты для каждого вида, состояние в localStorage (своё у каждого вида),
// чип «Сортировка» → лист с вариантами (радио) и направлением, ряд жанров (чипы + «Ещё…»), заголовки разделов.
import { el, icon, mount, setText, storage, nTracks } from "./dom.js";
import { api } from "./api.js";
import { sheet, list, listItem, chip, connectedGroup, announce, skeletonList } from "./ui.js";
import { haptic } from "./tg.js";
import { shell } from "./shell.js";

export const SORTS = {
  tracks: [
    ["added", "По дате добавления", "desc"], ["title", "По названию", "asc"], ["artist", "По исполнителю", "asc"], ["album", "По альбому", "asc"],
    ["genre", "По жанру", "asc"], ["year", "По году", "desc"], ["duration", "По длительности", "asc"], ["random", "В случайном порядке", "asc"],
  ],
  artists: [["name", "По имени", "asc"], ["count", "По числу треков", "desc"], ["added", "По дате добавления", "desc"], ["year", "По году", "desc"]],
  albums: [["name", "По названию", "asc"], ["count", "По числу треков", "desc"], ["added", "По дате добавления", "desc"], ["year", "По году", "desc"]],
  playlists: [["updated", "По изменению", "desc"], ["name", "По названию", "asc"], ["count", "По числу треков", "desc"]],
  // треки плейлиста (вид "pl:<id>" — своя сортировка у каждого плейлиста); manual — порядок плейлиста
  pltracks: [
    ["manual", "Порядок плейлиста", "asc"], ["added", "По дате добавления", "desc"], ["title", "По названию", "asc"], ["artist", "По исполнителю", "asc"],
    ["album", "По альбому", "asc"], ["genre", "По жанру", "asc"], ["year", "По году", "desc"], ["duration", "По длительности", "asc"],
    ["random", "В случайном порядке", "asc"],
  ],
};
const optsFor = (view) => SORTS[view] || (String(view).startsWith("pl:") ? SORTS.pltracks : []);
const noDir = (sort) => sort === "random" || sort === "manual"; // без направления
const KEY = "pimusic.libsort.v1";
const GKEY = "pimusic.libgenre.v1";

export function getSort(view) {
  const all = storage.get(KEY, {}) || {};
  const s = all[view];
  const opts = optsFor(view);
  const def = opts[0];
  if (s && opts.some((o) => o[0] === s.sort)) return { sort: s.sort, order: s.order === "asc" ? "asc" : "desc" };
  return { sort: def[0], order: def[2] };
}
export function setSort(view, sort, order) {
  const all = storage.get(KEY, {}) || {};
  all[view] = { sort, order };
  storage.set(KEY, all);
}
export const getGenre = (view) => ((storage.get(GKEY, {}) || {})[view] || "");
export function setGenre(view, g) { const all = storage.get(GKEY, {}) || {}; all[view] = g || ""; storage.set(GKEY, all); }

export const sortLabel = (view, s = getSort(view)) => {
  const o = optsFor(view).find((x) => x[0] === s.sort);
  return (o ? o[1] : "Сортировка") + (noDir(s.sort) ? "" : s.order === "asc" ? " ↑" : " ↓");
};

// Лист «Сортировка»: радио-варианты + «По возрастанию / По убыванию»
export function sortSheet(view, onChange) {
  const cur = getSort(view);
  sheet({ title: "Сортировка", label: "Сортировка", cls: "sheet-ctl" + (shell.twoPane ? " sheet-dialog" : ""), build(body, close) {
    let order = cur.order;
    const dir = connectedGroup([{ key: "asc", label: "По возрастанию", icon: "arrow_upward" }, { key: "desc", label: "По убыванию", icon: "arrow_downward" }], order,
      (k) => { order = k; setSort(view, getSort(view).sort, k); haptic("select"); onChange(); announce(k === "asc" ? "По возрастанию" : "По убыванию"); }, { label: "Направление" });
    const dirBox = el("div", { class: "sheet-pad sort-dir" }, dir);
    dirBox.hidden = noDir(cur.sort);
    const rows = optsFor(view).map(([k, label, def]) => {
      const on = k === cur.sort;
      const row = listItem({ leading: icon(on ? "radio_button_checked" : "radio_button_unchecked", { fill: on }), headline: label, lines: 1, cls: on ? "is-on" : "",
        onClick: () => {
          const prev = getSort(view);
          const ord = prev.sort === k ? prev.order : def;
          setSort(view, k, ord);
          haptic("select");
          close();
          onChange();
          announce(`Сортировка: ${label}`);
        } });
      const main = row.querySelector(".li-main");
      main.setAttribute("role", "radio");
      main.setAttribute("aria-checked", on ? "true" : "false");
      return row;
    });
    const ul = list(rows, "sheet-list sort-list");
    ul.setAttribute("role", "radiogroup");
    ul.setAttribute("aria-label", "Порядок");
    body.append(dirBox, ul);
  } });
}

// Чип сортировки для панели вида
export function sortChip(view, onChange) {
  const c = chip({ label: sortLabel(view), icon: "sort", onClick: () => sortSheet(view, () => { c.update(); onChange(); }) });
  c.classList.add("sort-chip");
  c.setAttribute("aria-haspopup", "dialog");
  c.update = () => { setText(c.querySelector(".chip-label"), sortLabel(view)); c.setAttribute("aria-label", `Сортировка: ${sortLabel(view).replace(" ↑", ", по возрастанию").replace(" ↓", ", по убыванию")}`); };
  c.update();
  return c;
}

// ---------------------------------------------------------------- жанры
let genresP = null;
export function loadGenres(force = false) {
  if (!genresP || force) {
    genresP = api("/library/genres").then((r) => {
      const arr = Array.isArray(r) ? r : (r && (r.items || r.genres)) || [];
      return arr.map((g) => (typeof g === "string" ? { name: g, count: 0 } : g)).filter((g) => g && g.name).sort((a, b) => (b.count || 0) - (a.count || 0));
    }).catch(() => { genresP = null; return null; }); // ошибка — не кэшируем (повторим при следующем показе)
  }
  return genresP;
}
export const invalidateGenres = () => { genresP = null; };

// Ряд чипов: «Все жанры» + самые частые + «Ещё…» (лист со всеми)
export function genreRow(view, onChange) {
  const row = el("div", { class: "genre-row", role: "group", "aria-label": "Жанр" });
  row.hidden = true;
  const render = (genres) => {
    let cur = getGenre(view);
    // список не загрузился, а фильтр сохранён — чип «жанр ✕», чтобы его всегда можно было снять
    if (!genres || !genres.length) {
      row.hidden = !cur;
      if (cur) mount(row, [chip({ label: "Все жанры", variant: "filter", selected: false, onClick: () => { setGenre(view, ""); render(genres); onChange(); } }),
        chip({ label: `${cur} ✕`, variant: "filter", selected: true, aria: `Снять фильтр «${cur}»`, onClick: () => { setGenre(view, ""); render(genres); onChange(); announce("Все жанры"); } })]);
      return;
    }
    // сохранённого жанра больше нет в медиатеке — сбрасываем на «Все жанры»
    if (cur && !genres.some((g) => g.name === cur)) { setGenre(view, ""); cur = ""; setTimeout(onChange, 0); }
    row.hidden = false;
    const top = genres.slice(0, 8);
    if (cur && !top.some((g) => g.name === cur)) top.unshift(genres.find((g) => g.name === cur) || { name: cur, count: 0 });
    const pick = (g) => { setGenre(view, g); haptic("select"); render(genres); onChange(); announce(g ? `Жанр: ${g}` : "Все жанры"); };
    mount(row, [
      chip({ label: "Все жанры", variant: "filter", selected: !cur, onClick: () => pick("") }),
      ...top.map((g) => chip({ label: g.name, variant: "filter", selected: cur === g.name, onClick: () => pick(cur === g.name ? "" : g.name) })),
      genres.length > top.length ? chip({ label: "Ещё…", icon: "expand_more", onClick: () => allGenres(genres, cur, pick) }) : null,
    ]);
  };
  loadGenres().then(render);
  row.refresh = () => loadGenres().then(render);
  return row;
}

function allGenres(genres, cur, pick) {
  sheet({ title: "Жанры", label: "Все жанры", cls: "sheet-ctl" + (shell.twoPane ? " sheet-dialog" : ""), build(body, close) {
    const rows = [["", "Все жанры", 0], ...genres.map((g) => [g.name, g.name, g.count])].map(([k, label, n]) => listItem({
      leading: icon(cur === k ? "radio_button_checked" : "radio_button_unchecked", { fill: cur === k }), headline: label, supporting: n ? nTracks(n) : undefined, lines: n ? 2 : 1,
      cls: cur === k ? "is-on" : "", onClick: () => { close(); pick(k); } }));
    body.append(list(rows, "sheet-list"));
  } });
}

// ---------------------------------------------------------------- заголовки разделов
const LETTER = /[A-Za-zА-Яа-яЁё]/;
export function sectionKey(sort, it) {
  if (sort === "title" || sort === "name" || sort === "artist" || sort === "album") {
    const s = String((sort === "artist" ? it.artist : sort === "album" ? it.album : it.title || it.name) || "").trim();
    const ch = s.charAt(0);
    return ch && LETTER.test(ch) ? ch.toLocaleUpperCase("ru").replace("Ё", "Е") : "#";
  }
  if (sort === "genre") return it.genre || "Без жанра";
  if (sort === "year") return it.year ? String(it.year) : "Без года";
  return null;
}
export const sectionHeader = (text, { grid = false } = {}) =>
  el(grid ? "div" : "li", { class: "lib-sec" + (grid ? " lib-sec-grid" : ""), role: grid ? "heading" : "presentation", "aria-level": grid ? "3" : undefined },
    el("span", { class: "lib-sec-t", role: grid ? undefined : "heading", "aria-level": grid ? undefined : "3", text }));

// Треки плейлиста на клиенте (все треки уже загружены): manual — как есть; added — по added_at, иначе по item_id
// (порядок добавления); пустые значения — всегда в конце; при равенстве — исполнитель, альбом, название.
export function sortTracks(tracks, sort, order) {
  if (!sort || sort === "manual") return tracks.slice();
  if (sort === "random") return sortLocal(tracks, "random", order);
  const str = (v) => String(v || "").trim().toLocaleLowerCase("ru").replace(/^the\s+/, "");
  const key = {
    added: (t) => Number(t.added_at) || Number(t.item_id) || null,
    title: (t) => str(t.title) || null, artist: (t) => str(t.artist) || null, album: (t) => str(t.album) || null, genre: (t) => str(t.genre) || null,
    duration: (t) => Number(t.seconds) || null, year: (t) => Number(t.year) || null,
  }[sort] || (() => null);
  const k = order === "desc" ? -1 : 1;
  const tie = (a, b) => str(a.artist).localeCompare(str(b.artist), "ru") || str(a.album).localeCompare(str(b.album), "ru") || str(a.title).localeCompare(str(b.title), "ru");
  return tracks.slice().sort((a, b) => {
    const x = key(a), y = key(b);
    if (x === null || y === null) return x === y ? tie(a, b) : x === null ? 1 : -1;
    const c = typeof x === "string" ? x.localeCompare(y, "ru") : x - y;
    return c ? c * k : tie(a, b);
  });
}

// Клиентская сортировка (запасной путь, если сервер ещё не сортирует): имя/число треков/дата/год
export function sortLocal(items, sort, order, field = (it) => it.name || it.title || "") {
  if (sort === "random") { const a = items.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
  const val = (it) => (sort === "count" ? it.count || 0 : sort === "added" ? it.added_at || 0 : sort === "updated" ? it.updated_at || 0 : sort === "year" ? Number(it.year) || 0 : String(field(it)).toLocaleLowerCase("ru"));
  const k = order === "desc" ? -1 : 1;
  return items.slice().sort((a, b) => { const x = val(a), y = val(b); return (x < y ? -1 : x > y ? 1 : 0) * k; });
}

export { skeletonList };
