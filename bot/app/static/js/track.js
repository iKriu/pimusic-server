// Ряд трека и кнопка состояния: download (outlined) → wavy circular → check_circle (tertiary, FILL 1).
import { el, svg, icon, pic, nTracks, setText } from "./dom.js";
import { iconButton, listItem, wavyCircular, snackbar, sheet, list, openMenu } from "./ui.js";
import { selectionFor } from "./select.js";
import { api, isAbort } from "./api.js";
import { haptic } from "./tg.js";
import * as store from "./store.js";
import { go, shell } from "./shell.js";
import { preview, onPreview, playPreview, stopPreview } from "./player.js";

const registry = new Set();
store.on("tracks", () => {
  for (const r of registry) {
    if (!r.wrap.isConnected) { registry.delete(r); continue; }
    r.render();
  }
});

// Отправка в загрузку. body() — тело запроса (токен страницы может обновиться), refresh() — новый токен
export async function requestDownload({ body, refresh, tracks = [], signal, message }) {
  haptic("medium");
  store.markQueued(tracks);
  const send = () => api("/download", { method: "POST", body: body(), signal });
  try {
    let r;
    try { r = await send(); } catch (e) {
      if (e.code === "expired" && refresh) { await refresh(); r = await send(); } else throw e;
    }
    haptic("success");
    const text = message ? message(r) : null;
    if (text !== "") snackbar(text || (r.queued > 1 ? `В очереди: ${r.title} · ${nTracks(r.queued)}` : `В очереди: ${r.title}`), { action: "Загрузки", onAction: () => go("downloads") });
    store.kick();
    return r;
  } catch (e) {
    for (const t of tracks) if (t && t.key) store.forget(t.key);
    if (isAbort(e)) return null;
    haptic("error");
    snackbar(e.code === "expired" ? "Результаты устарели — обновите страницу" : e.message, { error: true });
    return null;
  }
}

export function stateButton(t, ctx) {
  const wrap = el("span", { class: "tbtn" });
  let last = "";
  const render = () => {
    const st = store.trackState(t);
    if (st === last) return;
    last = st;
    let node;
    if (st === "owned") {
      node = el("span", { class: "tbtn-owned", role: "img", "aria-label": "В медиатеке", title: "В медиатеке" }, icon("check_circle", { fill: true }));
    } else if (st === "queued") {
      node = el("span", { class: "tbtn-queued", role: "img", "aria-label": `В очереди: ${t.title}`, title: "В очереди" }, icon("schedule"));
    } else if (st === "active") {
      node = el("span", { class: "tbtn-prog", title: "Качается" }, wavyCircular({ size: 32, label: `Качается: ${t.title}` }));
    } else if (st === "error") {
      node = iconButton("error", `Не удалось скачать «${t.title}» — повторить`, { cls: "tbtn-error", onClick: (ev) => { ev.stopPropagation(); store.forget(t.key); start(); } });
    } else {
      node = iconButton("download", `Скачать «${t.title}»`, { variant: "outlined", onClick: (ev) => { ev.stopPropagation(); start(); } });
    }
    wrap.replaceChildren(node);
    wrap.dataset.state = st;
  };
  const start = () => {
    if (!ctx || !ctx.body) return;
    requestDownload({ body: ctx.body, refresh: ctx.refresh, tracks: [t], signal: ctx.signal });
  };
  render();
  if (t.key) registry.add({ wrap, render });
  return wrap;
}

// 30-секундный фрагмент: обложка (или номер) становится переключателем ▶/■ с кольцом прогресса вокруг
const RING_R = 30; // окружность в viewBox 64×64 — кольцо чуть шире обложки 56dp
export function previewToggle(t, inner, { numbered = false } = {}) {
  const ring = svg("circle", { class: "pv-ring-active", cx: 32, cy: 32, r: RING_R, pathLength: 100, "stroke-dasharray": "0 100" });
  const ringSvg = svg("svg", { class: "pv-ring", viewBox: "0 0 64 64", "aria-hidden": "true" }, svg("circle", { class: "pv-ring-track", cx: 32, cy: 32, r: RING_R }), ring);
  const ic = icon("play_arrow", { fill: true, class: "pv-icon" });
  const label = `Прослушать 30 секунд: ${t.title}`;
  const b = el("button", { type: "button", class: "pv-btn" + (numbered ? " is-num" : ""), "aria-label": label, title: label, "aria-pressed": "false" },
    inner, el("span", { class: "pv-scrim", "aria-hidden": "true" }, ic), ringSvg);
  b.addEventListener("click", (ev) => { ev.stopPropagation(); haptic("light"); playPreview(t.preview, t.title); });
  let on = false, attached = false, off = null;
  const render = (what) => {
    const mine = preview.url === t.preview;
    if (!b.isConnected) { // ряд ушёл с экрана (новый поиск) — фрагмент останавливаем, подписку снимаем
      if (attached) { if (off) { off(); off = null; } if (mine) stopPreview(); }
      return;
    }
    attached = true;
    if (mine !== on || what === "state" || what === "error") {
      on = mine;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.classList.toggle("is-on", on);
      b.dataset.state = on ? preview.state : "idle";
      setText(ic, on ? "stop" : "play_arrow");
      ic.dataset.fb = on ? "■" : "▶";
      const lab = on ? `Остановить фрагмент: ${t.title}` : label;
      b.setAttribute("aria-label", lab); b.title = lab;
    }
    if (what === "error" && !mine && b.dataset.wasOn === "1") snackbar("Не удалось включить фрагмент", { error: true });
    b.dataset.wasOn = mine ? "1" : "0";
    const p = mine ? preview.progress : 0;
    ring.setAttribute("stroke-dasharray", `${(p * 100).toFixed(2)} 100`);
  };
  off = onPreview(render);
  render("state");
  return b;
}

// Ряд трека каталога: обложка 56dp (или номер), title M, «исполнитель · альбом · 3:42», trailing — состояние
// Ряд трека каталога: ⋮ / долгое нажатие — «Скачать», «Скачать в плейлист…», «Выбрать»
export function trackRow(t, { num, sub, ctx, cover = true } = {}) {
  const supporting = sub !== undefined ? [sub, t.duration].filter(Boolean).join(" · ") : [t.artist, t.album, t.duration].filter(Boolean).join(" · ");
  let leading = num !== undefined
    ? el("span", { class: "li-num", text: String(num) })
    : cover ? pic(t.cover, { cls: "li-thumb", ph: "music_note" }) : null;
  if (leading && t.preview) leading = previewToggle(t, leading, { numbered: num !== undefined });
  let row;
  const more = iconButton("more_vert", `Действия: ${t.title}`, { cls: "li-more", onClick: (ev) => { ev.stopPropagation(); catalogActions(t, ctx, more, row); } });
  more.setAttribute("aria-haspopup", "dialog");
  row = listItem({
    leading, headline: t.title, supporting, cls: "track ctrack" + (num !== undefined ? " is-numbered" : ""),
    trailing: el("span", { class: "ctrack-trail" }, stateButton(t, ctx), more),
  });
  row._track = t;
  row._ctx = ctx;
  row.dataset.sel = ctx && ctx.body ? String(ctx.body().i ?? t.key ?? t.title) : String(t.key || t.title);
  longPress(row, () => { const s = selectionFor(row); if (s) s.start(row); else catalogActions(t, ctx, more, row); });
  return row;
}

// долгое нажатие (сенсор): вход в режим выбора
export function longPress(row, fn) {
  const main = row.querySelector(".li-main") || row;
  let lp = 0, x0 = 0, y0 = 0, fired = false;
  main.addEventListener("pointerdown", (ev) => {
    if (ev.pointerType === "mouse") return;
    x0 = ev.clientX; y0 = ev.clientY; fired = false;
    clearTimeout(lp);
    lp = setTimeout(() => { fired = true; haptic("medium"); fn(); }, 520);
  });
  const cancel = () => clearTimeout(lp);
  main.addEventListener("pointermove", (ev) => { if (Math.abs(ev.clientX - x0) > 10 || Math.abs(ev.clientY - y0) > 10) cancel(); });
  main.addEventListener("pointerup", cancel);
  main.addEventListener("pointercancel", cancel);
  // клик после долгого нажатия не должен «играть»/открывать
  main.addEventListener("click", (ev) => { if (fired) { fired = false; ev.stopImmediatePropagation(); ev.preventDefault(); } }, true);
  main.addEventListener("contextmenu", (ev) => { if (ev.pointerType !== "mouse" && fired) ev.preventDefault(); });
}

function catalogActions(t, ctx, anchor, row) {
  const owned = store.isOwned(t);
  const items = [
    !owned ? { icon: "download", label: "Скачать", onClick: () => requestDownload({ body: ctx.body, refresh: ctx.refresh, tracks: [t], signal: ctx.signal }) } : null,
    { icon: "playlist_add", label: owned ? "В плейлист…" : "Скачать в плейлист…", supporting: owned ? "Трек уже в медиатеке" : "Добавится после загрузки",
      onClick: () => import("./bulk.js").then((m) => m.downloadToPlaylist([{ t, ctx }])) },
    selectionFor(row) ? { icon: "checklist", label: "Выбрать", onClick: () => selectionFor(row).start(row) } : null,
  ].filter(Boolean);
  haptic("light");
  if (shell.twoPane && anchor && anchor.isConnected) { openMenu(anchor, items); return; }
  sheet({ title: t.title, label: `Действия: ${t.title}`, build(body, close) {
    body.append(el("div", { class: "sheet-track" }, pic(t.cover, { cls: "li-thumb", ph: "music_note" }),
      el("div", { class: "li-text" }, el("span", { class: "li-headline", text: t.artist }), el("span", { class: "li-supporting", text: [t.album, t.duration].filter(Boolean).join(" · ") }))),
    list(items.map((it) => listItem({ leading: icon(it.icon), headline: it.label, supporting: it.supporting, lines: 1, onClick: () => { close(); it.onClick(); } })), "sheet-list"));
  } });
}
