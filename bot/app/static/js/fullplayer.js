// «Сейчас играет» (режим очереди): крупная обложка с формой (на паузе «отдыхает» — скругление больше, масштаб меньше),
// схема из цвета обложки, волнистый слайдер перемотки (role=slider, клавиатура), shuffle/prev/play/next/repeat,
// «В плейлист», «Очередь» (лист: «Играет сейчас», «Далее» с перестановкой и удалением), переход к исполнителю/альбому.
// Раскладки (DESIGN §7): compact — полноэкранный лист (свайп вниз / «назад» закрывают), medium/expanded — модальный
// боковой лист справа, large/xl — закреплённая правая панель вместо supporting pane (мини-плеер при этом скрыт).
import { el, icon, pic, mount, setText, clamp } from "./dom.js";
import { iconButton, button, sheet, list, listItem, openMenu, snackbar, dialog } from "./ui.js";
import { animate, reducedMotion } from "./motion.js";
import { haptic } from "./tg.js";
import { pushBackHandler, refreshShell } from "./shell.js";
import {
  player, onPlayer, current, next, prev, seek, toggleShuffle, cycleRepeat, hasNext, jumpTo, removeAt, moveItem,
  clearUpcoming, stop, removeQids, moveQidsNext,
} from "./player.js";
import { coverColor, playToggle, fmtClock, say } from "./nowplaying.js";
import { addToPlaylist } from "./playlists.js";
import { gotoArtist, gotoAlbum, eqGlyph, currentFab } from "./libtrack.js";
import { sortable } from "./reorder.js";
import { selectable } from "./select.js";
import { longPress } from "./track.js";
import { lyricsView } from "./lyrics.js";
import { eqView } from "./eqview.js";
import { sleepSheet, speedSheet, crossfadeSheet, toggleNormalize, opts as popts, fmtSpeed, fmtXf, fmtSleep } from "./playerctl.js";
import { sleepState } from "./player.js";
import { volumeControl } from "./volume.js";
import { go, shell } from "./shell.js";

const app = document.getElementById("app");
let np = null; // корневой узел
let isOpen = false, layout = "full", releaseBack = null, opener = null, closedByUser = false;

const layoutFor = () => {
  const w = app.dataset.wsc || "compact";
  return w === "large" || w === "xl" ? "pane" : w === "medium" || w === "expanded" ? "side" : "full";
};
const SRC_KIND = { playlist: "Плейлист", album: "Альбом", artist: "Исполнитель", library: "" };

// «Играет из»: подпись источника очереди (плейлист, альбом, исполнитель, медиатека…) и переход к нему.
// Источник приходит из playQueue({ source }) и хранится вместе с очередью (localStorage).
export const sourceText = (s = player.source) => (s && s.title ? [SRC_KIND[s.kind], s.title].filter(Boolean).join(" · ") : "");
function sourceNav(s = player.source) {
  if (!s) return null;
  const lib = (fn) => () => { go("library"); fn(); };
  switch (s.kind) {
    case "playlist": return s.id ? lib(() => import("./screens/playlist.js").then((m) => m.openPlaylist(s.id, { name: s.title }, { from: "list" }))) : null;
    case "album": {
      let ak = s.artist_key, k = s.album_key;
      if (!ak || !k) { const i = String(s.id || "").indexOf("/"); if (i > 0) { ak = s.id.slice(0, i); k = s.id.slice(i + 1); } }
      return ak && k ? lib(() => import("./screens/album.js").then((m) => m.openLibAlbum(ak, k, { title: s.title, cover: s.cover }, { from: "list" }))) : null;
    }
    case "artist": return s.id ? lib(() => import("./screens/artist.js").then((m) => m.openLibArtist(s.id, { name: s.title }, { from: "list" }))) : null;
    case "library": return lib(() => { if (shell.roots.library && shell.roots.library.select) shell.roots.library.select("tracks"); });
    case "offline": return lib(() => import("./screens/offline.js").then((m) => m.openOffline({ from: "list" })));
    case "upload": return () => go("downloads");
    case "search": return () => go("search");
    default: return null;
  }
}
// Ссылка «Играет из: …» (текстовая кнопка M3): есть куда перейти — кнопка с шевроном (имя = видимый текст,
// полный текст — в подсказке для обрезанного); нет — простой текст («3 из 12»), а не недоступная кнопка.
function sourceLink(cls) {
  const txt = el("span", { class: "src-t" });
  const b = el("button", { type: "button", class: "src-link" }, txt, icon("chevron_right", { class: "src-chev" }));
  const plain = el("span", { class: "src-plain src-t" });
  const box = el("span", { class: `src-box ${cls}` }, b, plain);
  b.addEventListener("click", () => {
    const nav = sourceNav();
    if (!nav) return;
    haptic("light");
    leaveFor();
    nav();
  });
  box.update = (fallback, pre = "") => {
    const s = player.source, t = sourceText(s);
    const nav = t ? sourceNav(s) : null;
    const text = t ? pre + t : fallback;
    b.hidden = !nav; plain.hidden = !!nav;
    setText(txt, nav ? text : "");
    setText(plain, nav ? "" : text);
    b.classList.toggle("is-link", !!nav);
    box.classList.toggle("is-link", !!nav);
    if (nav) b.title = `${text} — открыть`; else b.removeAttribute("title");
  };
  return box;
}

// ---------------------------------------------------------------- слайдер перемотки
function seekSlider(onScrub) {
  const s = el("div", { class: "seek", role: "slider", tabindex: "0", "aria-label": "Позиция в треке", "aria-valuemin": "0", "aria-valuemax": "0", "aria-valuenow": "0" },
    el("span", { class: "sk-track" }, el("span", { class: "sk-rest" }), el("span", { class: "sk-flat" }), el("span", { class: "sk-wave" }), el("span", { class: "sk-handle" }), el("span", { class: "sk-stop" })));
  let dragging = false, val = 0, dur = 0;
  let ariaAt = 0;
  const paint = (cur, forceAria) => {
    const p = dur ? clamp(cur / dur, 0, 1) : 0;
    s.style.setProperty("--p", (p * 100).toFixed(3) + "%");
    // в фокусе — обновляем значение для скринридера не чаще раза в 10 с (кроме действий пользователя)
    const now = performance.now();
    if (!forceAria && document.activeElement === s && now - ariaAt < 10000) return;
    ariaAt = now;
    s.setAttribute("aria-valuemax", String(Math.round(dur)));
    s.setAttribute("aria-valuenow", String(Math.round(cur)));
    s.setAttribute("aria-valuetext", `${fmtClock(cur)} из ${fmtClock(dur)}`);
    if (dur) s.removeAttribute("aria-disabled"); else s.setAttribute("aria-disabled", "true");
  };
  s.set = (cur, d) => { const was = dur; dur = d || 0; if (!dragging) { val = cur; paint(cur, was !== dur); } };
  const fromX = (x) => { const r = s.getBoundingClientRect(); return clamp((x - r.left) / r.width, 0, 1) * dur; };
  s.addEventListener("pointerdown", (ev) => {
    if (!dur || ev.button !== 0) return;
    ev.preventDefault();
    try { s.setPointerCapture(ev.pointerId); } catch { /* */ }
    dragging = true;
    s.classList.add("is-dragging");
    if (ev.pointerType === "mouse") s.focus({ preventScroll: true });
    val = fromX(ev.clientX); paint(val); onScrub(val);
  });
  s.addEventListener("pointermove", (ev) => { if (dragging) { val = fromX(ev.clientX); paint(val, true); onScrub(val); } });
  const end = () => { if (!dragging) return; dragging = false; s.classList.remove("is-dragging"); haptic("select"); seek(val); paint(val, true); onScrub(null); };
  s.addEventListener("pointerup", end);
  s.addEventListener("pointercancel", () => { if (!dragging) return; dragging = false; s.classList.remove("is-dragging"); paint(player.time, true); onScrub(null); });
  s.addEventListener("keydown", (ev) => {
    if (!dur) return;
    const step = { ArrowLeft: -5, ArrowDown: -5, ArrowRight: 5, ArrowUp: 5, PageDown: -30, PageUp: 30 }[ev.key];
    let to = null;
    if (step !== undefined) to = player.time + step;
    else if (ev.key === "Home") to = 0;
    else if (ev.key === "End") to = dur - 1;
    if (to === null) return;
    ev.preventDefault();
    seek(clamp(to, 0, dur));
    paint(clamp(to, 0, dur), true);
  });
  return s;
}

// ---------------------------------------------------------------- сборка
function build() {
  const coverBox = el("div", { class: "np2-cover" });
  const over = el("span", { class: "np2-over", text: "Сейчас играет" });
  const srcLine = sourceLink("np2-src");
  const collapse = iconButton("expand_more", "Свернуть", { cls: "np2-collapse", onClick: () => { if (vName !== "main") closeView(); else close(true); } });
  const more = iconButton("more_vert", "Ещё", { cls: "np2-more", onClick: () => menu(more) });
  more.setAttribute("aria-haspopup", "menu");
  more.setAttribute("aria-expanded", "false");
  const top = el("div", { class: "np2-top" }, collapse, el("div", { class: "np2-top-text" }, over, srcLine), more);
  const sleepRow = el("div", { class: "np2-sleep-row" });
  const title = el("h2", { class: "np2-title", id: "np2-title" });
  const artistBtn = button({ label: "Исполнитель", variant: "text", cls: "np2-artist", onClick: () => { const t = current(); if (t) { leaveFor(); gotoArtist(t); } } });
  const albumBtn = button({ label: "Альбом", variant: "text", cls: "np2-album", icon: "album", onClick: () => { const t = current(); if (t) { leaveFor(); gotoAlbum(t); } } });
  const scrubTime = { v: null };
  const tCur = el("span", { class: "np2-time" }), tDur = el("span", { class: "np2-time np2-dur" });
  const slider = seekSlider((v) => { scrubTime.v = v; renderTime(); });
  const shuffle = iconButton("shuffle", "Перемешивание", { cls: "np2-shuffle", toggle: true, onClick: () => { haptic("select"); toggleShuffle(); } });
  const repeat = iconButton("repeat", "Повтор: выкл.", { cls: "np2-repeat", onClick: () => { haptic("select"); cycleRepeat(); say(repeat.getAttribute("aria-label")); } });
  const prevB = iconButton("skip_previous", "Предыдущий трек", { cls: "np2-prev", fill: true, onClick: () => { haptic("light"); prev(); } });
  const nextB = iconButton("skip_next", "Следующий трек", { cls: "np2-next", fill: true, onClick: () => { if (nextB.getAttribute("aria-disabled") === "true") return; haptic("light"); next(); } });
  const playB = playToggle({ kind: "any" });
  const tool = (ic, label, cls, view, onClick) => {
    const b = el("button", { type: "button", class: `np2-tool ${cls}`, "data-view": view || undefined }, icon(ic, { class: "np2-tool-ic" }), el("span", { class: "btn-label", text: label }));
    b.addEventListener("click", () => { haptic("light"); onClick(); });
    return b;
  };
  const lyrB = tool("lyrics", "Текст", "np2-lyrics", "lyrics", () => openView("lyrics"));
  const eqB = tool("equalizer", "Эквалайзер", "np2-eq", "eq", () => openView("eq"));
  const toPl = tool("playlist_add", "В плейлист", "np2-addto", null, () => { const t = current(); if (t) addToPlaylist({ track: t, scope: np }); });
  const queueB = tool("queue_music", "Очередь", "np2-queue", "queue", () => openQueue());
  const sleepChip = el("button", { type: "button", class: "np2-sleep", hidden: true }, icon("bedtime"), el("span", { class: "np2-sleep-t" }));
  sleepChip.addEventListener("click", () => sleepSheet(np));
  const sheetEl = el("section", { class: "np2-sheet" },
    el("span", { class: "np2-grab", "aria-hidden": "true" }),
    top,
    sleepRow,
    el("div", { class: "np2-body" },
      el("div", { class: "np2-art" }, coverBox),
      el("div", { class: "np2-info" },
        el("div", { class: "np2-titles" }, title, el("div", { class: "np2-links" }, artistBtn, albumBtn)),
        el("div", { class: "np2-seek" }, slider, el("div", { class: "np2-times" }, tCur, tDur)),
        el("div", { class: "np2-ctrls", role: "group", "aria-label": "Управление воспроизведением" }, shuffle, prevB, playB, nextB, repeat),
        el("div", { class: "np2-extra np2-tools", role: "group", "aria-label": "Ещё" }, lyrB, eqB, toPl, queueB),
        volumeControl({ cls: "np2-vol" }))),
    el("div", { class: "np2-qview" }));
  const scrim = el("div", { class: "np2-scrim", "aria-hidden": "true" });
  scrim.addEventListener("click", () => close(true));
  np = el("div", { class: "np2", role: "dialog", "aria-label": "Сейчас играет", hidden: true, "data-view": "main" }, scrim, sheetEl);
  document.body.appendChild(np);

  // свайп вниз за верх листа (compact) — закрыть; альтернатива жесту: «Свернуть», «назад», Esc
  let y0 = null, dy = 0;
  top.addEventListener("pointerdown", (ev) => { if (layout !== "full" || ev.target.closest("button")) return; y0 = ev.clientY; dy = 0; top.setPointerCapture(ev.pointerId); });
  top.addEventListener("pointermove", (ev) => { if (y0 === null) return; dy = Math.max(0, ev.clientY - y0); sheetEl.style.transform = `translateY(${dy}px)`; });
  const endDrag = () => { if (y0 === null) return; y0 = null; if (dy > 110) close(true); else { sheetEl.style.transform = ""; } };
  top.addEventListener("pointerup", endDrag);
  top.addEventListener("pointercancel", endDrag);

  const memo = { url: undefined };
  let coverUrl;
  function renderTime() {
    const t = current(), sec = t && t.seconds;
    const d = sec && player.duration && Math.abs(sec - player.duration) < 2 ? sec : (player.duration || sec || 0);
    const cur = scrubTime.v !== null ? scrubTime.v : player.time;
    slider.set(player.time, d);
    setText(tCur, fmtClock(cur));
    setText(tDur, d ? fmtClock(d) : "–:––");
  }
  function render(what) {
    if (what === "time") { if (isOpen) renderTime(); return; }
    const t = current();
    if (!t) { if (isOpen) close(false); return; }
    np.dataset.state = player.state;
    np.dataset.playing = player.desired === "playing" ? "1" : "0";
    setText(title, t.title);
    setText(artistBtn.querySelector(".btn-label"), t.artist || "Исполнитель");
    if (t.artist) artistBtn.setAttribute("aria-label", `К исполнителю: ${t.artist}`); else artistBtn.removeAttribute("aria-label");
    albumBtn.hidden = !t.album;
    setText(albumBtn.querySelector(".btn-label"), t.album || "");
    if (t.album) albumBtn.setAttribute("aria-label", `К альбому: ${t.album}`); else albumBtn.removeAttribute("aria-label");
    const s = player.source;
    srcLine.update(`${player.index + 1} из ${player.queue.length}`);
    if (vName === "main") setText(over, s && s.title ? "Играет из" : "Сейчас играет");
    shuffle.setAttribute("aria-pressed", player.shuffle ? "true" : "false");
    const rep = player.repeat;
    setText(repeat.querySelector(".ms"), rep === "one" ? "repeat_one" : "repeat");
    repeat.classList.toggle("is-on", rep !== "off");
    shuffle.classList.toggle("is-on", !!player.shuffle);
    const rl = rep === "off" ? "Повтор: выкл." : rep === "all" ? "Повтор: вся очередь" : "Повтор: один трек";
    repeat.setAttribute("aria-label", rl); repeat.title = rl;
    nextB.setAttribute("aria-disabled", hasNext() ? "false" : "true");
    const upcoming = Math.max(0, player.queue.length - player.index - 1);
    setText(queueB.querySelector(".btn-label"), upcoming ? `Очередь · ${upcoming}` : "Очередь");
    const c = t.cover || null;
    if (c !== coverUrl) {
      coverUrl = c;
      const p = pic(c, { cls: "np2-pic", ph: "music_note", eager: true, alt: "" });
      coverBox.replaceChildren(p);
      if (isOpen && what === "track") animate(coverBox, [{ opacity: 0.4, transform: "scale(.94)" }, { opacity: 1, transform: "none" }], "spatial-default");
    }
    coverColor(np, c, memo);
    renderTime();
    if (player.state === "error" && player.error) np.dataset.err = "1"; else delete np.dataset.err;
  }
  // таймер сна — чип под шапкой
  sleepRow.appendChild(sleepChip);
  const renderSleep = () => {
    const st = sleepState();
    sleepChip.hidden = !st;
    if (st) { setText(sleepChip.querySelector(".np2-sleep-t"), `Сон · ${fmtSleep(st)}`); sleepChip.setAttribute("aria-label", `Таймер сна: ${fmtSleep(st)}. Изменить`); }
  };
  onPlayer((w) => { if (w === "sleep") renderSleep(); });
  renderSleep();
  onPlayer(render);
  render();
  np.render = render;
}

// пункты меню «Ещё»
function menu(anchor) {
  const t = current();
  if (!t) return;
  anchor.setAttribute("aria-expanded", "true");
  openMenu(anchor, [
    { icon: "person", label: "К исполнителю", supporting: t.artist, onClick: () => { leaveFor(); gotoArtist(t); } },
    t.album ? { icon: "album", label: "К альбому", supporting: t.album, onClick: () => { leaveFor(); gotoAlbum(t); } } : null,
    { icon: "playlist_add", label: "Добавить в плейлист…", onClick: () => addToPlaylist({ track: t, scope: np }) },
    { icon: "queue_music", label: "Очередь", onClick: () => openQueue() },
    "divider",
    { icon: "bedtime", label: "Таймер сна", supporting: fmtSleep(), onClick: () => sleepSheet(np) },
    { icon: "speed", label: "Скорость", supporting: fmtSpeed(popts().speed), onClick: () => speedSheet(np) },
    { icon: "join", label: "Плавный переход", supporting: fmtXf(popts().crossfade), onClick: () => crossfadeSheet(np) },
    { icon: "volume_up", label: "Нормализация громкости", supporting: popts().normalize ? "Включена" : "Выключена", onClick: toggleNormalize },
    "divider",
    { icon: "close", label: "Остановить и очистить очередь", danger: true, onClick: async () => { if (!(await confirmClear("all"))) return; close(false); stop(); snackbar("Очередь очищена"); } },
  ].filter(Boolean), () => anchor.setAttribute("aria-expanded", "false"));
}

// переход к исполнителю/альбому: модальный лист сворачиваем, закреплённая панель остаётся
function leaveFor() { if (layout !== "pane") close(false); }

// ---------------------------------------------------------------- открыть / закрыть
export function openNowPlaying({ auto = false } = {}) {
  if (player.mode !== "queue" || !current()) return;
  if (!np) build();
  if (auto && (closedByUser || layoutFor() !== "pane")) return; // сам открывается только как панель, и не после закрытия
  if (isOpen) { if (!auto) focusIn(); return; }
  isOpen = true;
  opener = document.activeElement;
  np.render();
  apply(true);
  if (!auto) focusIn();
}

function focusIn() {
  const b = np.querySelector(layout === "pane" ? ".np2-title" : ".np2-collapse");
  if (b) { if (!b.hasAttribute("tabindex") && b.tagName === "H2") b.setAttribute("tabindex", "-1"); b.focus({ preventScroll: true }); }
}

function apply(entering) {
  const was = np.dataset.layout;
  layout = layoutFor();
  np.dataset.layout = layout;
  np.hidden = false;
  const modal = layout !== "pane";
  np.setAttribute("aria-modal", modal ? "true" : "false");
  np.setAttribute("role", modal ? "dialog" : "region");
  app.inert = modal;
  const skip = document.querySelector(".skip-link");
  if (skip) skip.inert = modal;
  app.dataset.np = layout === "pane" ? "pane" : "off";
  if (vName === "main") {
    const c = np.querySelector(".np2-collapse");
    setText(c.querySelector(".ms"), layout === "full" ? "expand_more" : "close");
    c.setAttribute("aria-label", layout === "full" ? "Свернуть" : "Закрыть плеер"); c.title = c.getAttribute("aria-label");
  }
  if (modal && !releaseBack) releaseBack = pushBackHandler(() => close(true));
  if (!modal && releaseBack) { releaseBack(); releaseBack = null; }
  refreshShell();
  const sh = np.querySelector(".np2-sheet");
  sh.style.transform = "";
  if (entering || was !== layout) {
    const from = layout === "full" ? "translateY(100%)" : layout === "side" ? "translateX(100%)" : "translateX(24px)";
    animate(sh, [{ transform: from, opacity: layout === "pane" ? 0 : 1 }, { transform: "none", opacity: 1 }], "spatial-default");
    if (modal) animate(np.querySelector(".np2-scrim"), [{ opacity: 0 }, { opacity: 1 }], "effects-default");
  }
}

export function closeNowPlaying() { close(false); }

function close(byUser) {
  if (!isOpen || !np) return;
  closeView(true);
  isOpen = false;
  if (byUser) { closedByUser = true; haptic("light"); }
  if (releaseBack) { releaseBack(); releaseBack = null; }
  app.inert = false;
  const skipL = document.querySelector(".skip-link");
  if (skipL) skipL.inert = false;
  app.dataset.np = "off";
  refreshShell();
  const sh = np.querySelector(".np2-sheet");
  const to = layout === "full" ? "translateY(100%)" : layout === "side" ? "translateX(100%)" : "translateX(24px)";
  const fin = () => { if (!isOpen) { np.hidden = true; sh.style.transform = ""; } };
  const a = animate(sh, [{ transform: getComputedStyle(sh).transform === "none" ? "none" : getComputedStyle(sh).transform, opacity: 1 }, { transform: to, opacity: layout === "pane" ? 0 : 1 }], "effects-default");
  if (a) a.finished.then(() => { a.cancel(); fin(); }, fin); else fin();
  const mini = app.querySelector(".mini-main");
  const back = opener && opener.isConnected && !opener.closest("[hidden]") && opener.getClientRects().length ? opener : mini;
  if (back && back.isConnected) back.focus({ preventScroll: true });
}

export const nowPlayingOpen = () => isOpen;

// смена размера окна (поворот, складной экран): раскладка листа следует за классом окна
if (app) {
  new MutationObserver(() => { if (isOpen && np && layoutFor() !== layout) apply(false); })
    .observe(app, { attributes: true, attributeFilter: ["data-wsc"] });
}
// очередь кончилась / включили волну — лист закрываем; новая очередь на широком экране — панель открывается сама
onPlayer((what) => {
  if (what === "time") return;
  if (isOpen && (player.mode !== "queue" || !current())) close(false);
  if (what === "track" && player.mode === "queue" && player.desired === "playing") openNowPlaying({ auto: true });
});

// ---------------------------------------------------------------- «Очередь»
// compact — bottom sheet в контент-схеме плеера; side sheet и закреплённая панель — внутри самого «Сейчас играет»
export function openQueue() {
  if (player.mode !== "queue") return;
  haptic("light");
  if (isOpen && layout !== "full" && np) { openView("queue"); return; }
  sheet({
    title: "Очередь", label: "Очередь воспроизведения", cls: "sheet-queue", scope: np,
    build(body, closeSheet) {
      buildQueue(body, { onEmpty: closeSheet, scroller: () => body.closest(".sheet") });
      // «К текущему»: док с нулевой высотой прилипает к низу прокрутки листа, FAB — внутри него
      const dock = el("div", { class: "fab-dock" });
      body.appendChild(dock);
      requestAnimationFrame(() => {
        const sc = body.closest(".sheet");
        if (!sc) return;
        const qf = currentFab(dock, sc, body);
        sc.addEventListener("close", () => qf.destroy(), { once: true });
      });
    },
  });
}

// Внутренние виды «Сейчас играет»: очередь (на широких), текст, эквалайзер — вместо тела плеера, «←» возвращает
let vOff = null, vBack = null, vName = "main";
const VIEW_TITLE = { main: "Сейчас играет", queue: "Очередь", lyrics: "Текст", eq: "Эквалайзер" };
function topMode(name) {
  const c = np.querySelector(".np2-collapse");
  const inner = name !== "main";
  c.setAttribute("aria-label", inner ? "Назад к плееру" : "Свернуть");
  c.title = c.getAttribute("aria-label");
  setText(c.querySelector(".ms"), inner ? "arrow_back" : layout === "full" ? "expand_more" : "close");
  setText(np.querySelector(".np2-over"), name === "main" && sourceText() ? "Играет из" : VIEW_TITLE[name] || VIEW_TITLE.main);
  np.querySelector(".np2-src").hidden = inner; // во внутренних видах шапка — заголовок вида (источник есть в «Очереди»)
}
export function openView(name) {
  if (!np || !isOpen) return;
  const view = np.querySelector(".np2-qview");
  if (vName !== "main") closeView(true);
  vName = name;
  np.dataset.view = name;
  topMode(name);
  view.replaceChildren();
  if (name === "queue") {
    const offQ = buildQueue(view, { onEmpty: () => closeView(), scroller: () => view });
    const qf = currentFab(view.parentElement, view, view); // «К текущему» — к «Играет сейчас», если прокрутили вниз
    vOff = () => { if (offQ) offQ(); qf.destroy(); };
  }
  else if (name === "lyrics") { const l = lyricsView(); view.append(l); l.start(); }
  else if (name === "eq") view.append(eqView());
  vBack = pushBackHandler(() => closeView());
  animate(view, [{ opacity: 0, transform: "translateX(32px)" }, { opacity: 1, transform: "none" }], "spatial-default");
  const h = view.querySelector(".q-sec, .lyr, .eqv-head .cg-btn[aria-selected=true]");
  if (h) h.focus({ preventScroll: true });
}
function closeView(silent) {
  if (!np || vName === "main") return;
  const was = vName;
  vName = "main";
  np.dataset.view = "main";
  topMode("main");
  if (vOff) { vOff(); vOff = null; }
  if (vBack) { vBack(); vBack = null; }
  np.querySelector(".np2-qview").replaceChildren();
  const b = np.querySelector(`.np2-tool[data-view="${was}"]`);
  if (b && isOpen && !silent) b.focus({ preventScroll: true });
}

async function confirmClear(what) {
  return dialog({ icon: "clear_all", headline: what === "all" ? "Остановить и очистить очередь?" : "Очистить «Далее»?",
    body: what === "all" ? "Музыка остановится, очередь пропадёт. Медиатека и плейлисты не изменятся." : "Из очереди уберутся все треки после играющего.",
    actions: [{ label: "Отмена", value: false }, { label: "Очистить", value: true, variant: "danger" }] });
}

const MAX_ROWS = 300;
// Содержимое очереди. Фокус не теряется при перерисовке: тот же ряд и та же кнопка (или соседний ряд).
function buildQueue(box, { onEmpty, scroller }) {
  let local = false, pending = false;
  const src = sourceLink("q-src");
  const shBtn = iconButton("shuffle", "Перемешивание", { toggle: true, cls: "q-shuffle", onClick: () => { haptic("select"); toggleShuffle(); say(player.shuffle ? "Перемешивание включено" : "Перемешивание выключено"); } });
  const clr = button({ label: "Очистить", icon: "clear_all", variant: "text", cls: "q-clear", onClick: async () => {
    if (clr.getAttribute("aria-disabled") === "true") return;
    if (!(await confirmClear("next"))) return;
    haptic("light");
    clearUpcoming();
    say("«Далее» очищено");
    nextHead.focus({ preventScroll: true });
  } });
  const nowLead = el("span", { class: "li-lead li-lead-thumb" });
  const nowItem = listItem({ leading: nowLead, headline: " ", supporting: " ", cls: "track ltrack is-current" });
  const nowList = list([nowItem], "q-now");
  const nextHead = el("h3", { class: "q-sec", tabindex: "-1" });
  const listBox = el("div");
  // выбор в очереди: убрать, играть следующими
  let qmap = null;
  const byQid = () => qmap || (qmap = new Map(player.queue.map((x) => [String(x.qid), x])));
  const qsel = selectable(box, { sticky: true, rows: () => [...listBox.querySelectorAll(".q-row")], key: (r) => r.dataset.key,
    // qid → позиция: одна карта на версию очереди (а не поиск по всей очереди на каждый ряд)
    item: (r) => byQid().get(r.dataset.key),
    actions: (items, ctl) => [
      { icon: "close", label: "Убрать", danger: true, run: (its) => {
        local = true; const n = removeQids(its.map((it) => it.qid)); local = false;
        ctl.stop(); all(); say(`Убрано из очереди: ${n}`);
      } },
      { icon: "queue_play_next", label: "Следующими", run: (its) => {
        local = true; moveQidsNext(its.map((it) => it.qid)); local = false;
        ctl.stop(); all(); say(`Следующими: ${its.length}`);
      } },
    ] });
  const selBtn = iconButton("checklist", "Выбрать несколько", { onClick: () => qsel.start() });
  // «Играет из: …» — своей строкой (длинное название не теснит кнопки), под ней — действия очереди
  box.append(el("div", { class: "q-srcrow" }, src), el("div", { class: "q-head" }, selBtn, shBtn, clr), el("h3", { class: "q-sec", text: "Играет сейчас", tabindex: "-1" }), nowList, nextHead, listBox);
  let nowCover;

  const renderHead = () => {
    src.update(`${player.queue.length} в очереди`, "Играет из: ");
    shBtn.setAttribute("aria-pressed", player.shuffle ? "true" : "false");
    const up = player.queue.length - player.index - 1;
    clr.setAttribute("aria-disabled", up > 0 ? "false" : "true");
    setText(nextHead, up > 0 ? `Далее · ${up}` : "Далее");
  };
  const renderNow = () => {
    const t = current();
    if (!t) return false;
    setText(nowItem.querySelector(".li-headline"), t.title);
    setText(nowItem.querySelector(".li-supporting"), t.artist || "");
    nowItem.classList.toggle("is-paused", player.desired !== "playing");
    if (nowCover !== t.cover) { nowCover = t.cover; nowLead.replaceChildren(pic(t.cover, { cls: "li-thumb", ph: "music_note" }), eqGlyph()); }
    return true;
  };

  const findIdx = (key) => player.queue.findIndex((x) => String(x.qid) === String(key));
  const moveRel = (it, d, sel) => {
    const i = player.queue.indexOf(it), j = i + d;
    if (i < 0 || j <= player.index || j >= player.queue.length) { say(d < 0 ? "Это первая позиция" : "Это последняя позиция"); return; }
    haptic("select");
    moveItem(i, j);
    say(`Позиция ${j - player.index} из ${player.queue.length - player.index - 1}`);
    const row = listBox.querySelector(`[data-key="${it.qid}"]`);
    const b = row && row.querySelector(sel);
    if (b) b.focus();
  };

  function renderList() {
    // запоминаем фокус: ряд + какая кнопка
    const ae = document.activeElement;
    let fKey = null, fSel = null, fIdx = -1;
    if (ae && listBox.contains(ae)) {
      const row = ae.closest("[data-key]");
      if (row) {
        fKey = row.dataset.key;
        fIdx = [...row.parentNode.children].indexOf(row);
        fSel = [".drag-handle", ".q-up", ".q-down", ".q-rm"].find((c) => ae.matches(c)) || ".li-main";
      }
    }
    const upcoming = player.queue.slice(player.index + 1);
    if (!upcoming.length) {
      mount(listBox, el("p", { class: "note", text: player.repeat === "all" ? "Дальше очередь начнётся сначала." : "Дальше ничего. Добавляйте треки: ⋮ у трека → «В очередь» или «Играть следующим»." }));
      if (fSel) nextHead.focus({ preventScroll: true });
      return;
    }
    const ul = list(upcoming.slice(0, MAX_ROWS).map((it) => {
      const handle = el("button", { type: "button", class: "drag-handle", "aria-label": `Переместить «${it.title}»`, "aria-describedby": "q-hint", title: "Перетащите, чтобы переставить" }, icon("drag_indicator"));
      const up = iconButton("arrow_upward", `Выше: ${it.title}`, { cls: "q-mv q-up", onClick: () => moveRel(it, -1, ".q-up") });
      const down = iconButton("arrow_downward", `Ниже: ${it.title}`, { cls: "q-mv q-down", onClick: () => moveRel(it, 1, ".q-down") });
      const rm = iconButton("close", `Убрать из очереди: ${it.title}`, { cls: "q-rm", onClick: () => { haptic("light"); removeAt(player.queue.indexOf(it)); say(`Убрано из очереди: ${it.title}`); } });
      const li = listItem({ leading: pic(it.cover, { cls: "q-thumb", ph: "music_note" }), headline: it.title, supporting: [it.artist, it.unavailable ? "недоступен" : ""].filter(Boolean).join(" · "),
        trailing: el("span", { class: "q-btns" }, up, down, handle, rm), cls: "track q-row" + (it.unavailable ? " is-unavailable" : ""),
        onClick: () => { haptic("light"); jumpTo(player.queue.indexOf(it)); } });
      li.dataset.key = String(it.qid);
      longPress(li, () => qsel.start(li));
      return li;
    }), "q-list");
    sortable(ul, { scroller: scroller(), onMove: (from, to) => {
      const row = ul.children[from];
      const a = findIdx(row.dataset.key), b = player.index + 1 + to;
      if (a < 0) return;
      local = true;
      moveItem(a, b);
      local = false;
      const rows = [...ul.children];
      ul.insertBefore(rows[from], to > from ? rows[to].nextSibling : rows[to]);
    }, label: (i, n) => `Позиция ${i + 1} из ${n}` });
    // во время перетаскивания не перерисовываем — догоняем после
    ul.addEventListener("pointerup", () => setTimeout(() => { if (pending) { pending = false; renderList(); } }, 0));
    mount(listBox, [ul, upcoming.length > MAX_ROWS ? el("p", { class: "note", text: `И ещё ${upcoming.length - MAX_ROWS}` }) : null]);
    if (fSel) {
      const row = ul.querySelector(`[data-key="${fKey}"]`) || ul.children[Math.min(fIdx, ul.children.length - 1)];
      const target = row && (row.querySelector(fSel) || row.querySelector(".li-main"));
      (target || nextHead).focus({ preventScroll: true });
    }
  }

  const all = () => { if (!renderNow()) { onEmpty(); return false; } renderHead(); renderList(); return true; };
  if (!document.getElementById("q-hint")) document.body.appendChild(el("span", { id: "q-hint", class: "sr-only", text: "Стрелки вверх и вниз — переставить" }));
  all();
  const off = onPlayer((what) => {
    if (what === "queue") qmap = null; // и при своих правках (local) — карта qid устарела
    if (!box.isConnected) { off(); return; }
    if (what === "time" || local) return;
    if (what === "state") { nowItem.classList.toggle("is-paused", player.desired !== "playing"); return; }
    if (what !== "queue" && what !== "track") return;
    if (listBox.querySelector(".is-sorting")) { pending = true; renderHead(); return; }
    all();
  });
  return off;
}

export { reducedMotion };
