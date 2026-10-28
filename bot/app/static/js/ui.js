// Компоненты M3 / M3 Expressive на ванильном DOM.
import { el, svg, icon, append, mount, clamp } from "./dom.js";
import { radii, pathFromRadii } from "./shapes.js";
import { reducedMotion, animate } from "./motion.js";
import { haptic } from "./tg.js";

// ---------------------------------------------------------------- модальность ввода
// <html data-input="kbd|pointer">: кольца фокуса у слайдеров — только при навигации с клавиатуры
(() => {
  const root = document.documentElement;
  const NAV = new Set(["Tab", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);
  document.addEventListener("keydown", (e) => { if (NAV.has(e.key)) root.dataset.input = "kbd"; }, true);
  document.addEventListener("pointerdown", () => { root.dataset.input = "pointer"; }, true);
})();

// ---------------------------------------------------------------- кнопки
export function button({ label, icon: ic, trailing, variant = "filled", size = "s", onClick, disabled, aria, cls, type = "button" } = {}) {
  const b = el("button", {
    type, class: `btn btn-${variant} btn-${size}${ic ? " has-icon" : ""}${cls ? " " + cls : ""}`,
    "aria-label": aria, disabled,
  });
  if (ic) b.appendChild(icon(ic, { class: "btn-icon" }));
  if (label) b.appendChild(el("span", { class: "btn-label", text: label }));
  if (trailing) b.appendChild(icon(trailing, { class: "btn-icon btn-trailing" }));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

export function iconButton(ic, label, { variant = "standard", onClick, fill, cls, toggle, selected, size } = {}) {
  const b = el("button", {
    type: "button", class: `ibtn ibtn-${variant}${size ? " ibtn-" + size : ""}${cls ? " " + cls : ""}`, "aria-label": label, title: label,
  }, icon(ic, { fill }));
  if (toggle) b.setAttribute("aria-pressed", selected ? "true" : "false");
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

// Замена иконки кнопки на индикатор, пока идёт действие
export function setBusy(btn, busy, label) {
  if (!btn) return;
  btn.disabled = !!busy;
  btn.classList.toggle("is-busy", !!busy);
  btn.setAttribute("aria-busy", busy ? "true" : "false");
  const lab = btn.querySelector(".btn-label");
  if (lab && label !== undefined) {
    if (busy && !btn.dataset.label) btn.dataset.label = lab.textContent;
    lab.textContent = busy ? label : (btn.dataset.label || lab.textContent);
    if (!busy) delete btn.dataset.label;
  }
  let sp = btn.querySelector(":scope > .btn-spin");
  if (busy && !sp) {
    btn.prepend(sp = wavyCircular({ size: 20, cls: "btn-spin", thin: true }));
    sp.setAttribute("aria-hidden", "true"); // состояние кнопки передаёт aria-busy, а не вложенный progressbar
  }
  if (!busy && sp) sp.remove();
}

let uidN = 0;
// Связь tablist ↔ tabpanel: aria-controls у вкладок, aria-labelledby у панели указывает на активную вкладку
function linkPanel(box, btns, getActive) {
  box.link = (panel) => {
    if (!panel.id) panel.id = `tp-${++uidN}`;
    panel.setAttribute("role", "tabpanel");
    box._panel = panel;
    for (const b of btns.values()) b.setAttribute("aria-controls", panel.id);
    const a = btns.get(getActive());
    if (a) panel.setAttribute("aria-labelledby", a.id);
  };
}

// Segmented buttons (single select) с галочкой у выбранного
export function segmented(items, active, onChange, { label = "Раздел" } = {}) {
  const box = el("div", { class: "segmented", role: "tablist", "aria-label": label });
  const btns = new Map();
  let cur = active;
  const set = (key, fire) => {
    cur = key;
    for (const [k, b] of btns) {
      const on = k === key;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle("is-selected", on);
    }
    if (box._panel && btns.get(key)) box._panel.setAttribute("aria-labelledby", btns.get(key).id);
    if (fire) onChange(key);
  };
  const sid = ++uidN;
  for (const it of items) {
    const b = el("button", { type: "button", role: "tab", id: `tab-${sid}-${it.key}`, class: "seg-btn", disabled: it.disabled },
      icon("check", { class: "seg-check" }),
      el("span", { class: "seg-label", text: it.label }),
      it.count !== undefined && it.count !== null ? el("span", { class: "seg-count", text: String(it.count) }) : null);
    b.addEventListener("click", () => { if (b.getAttribute("aria-selected") === "true") return; haptic("select"); set(it.key, true); });
    btns.set(it.key, b);
    box.appendChild(b);
  }
  rovingArrows(box, ".seg-btn:not([disabled])", (b) => b.click());
  linkPanel(box, btns, () => cur);
  set(active, false);
  box.set = (k) => set(k, false);
  return box;
}

// M3E connected button group: выбранная кнопка «раскрывается» в полную пилюлю
export function connectedGroup(items, active, onChange, { label = "Раздел" } = {}) {
  const box = el("div", { class: "cgroup", role: "tablist", "aria-label": label });
  const btns = new Map();
  let cur = active;
  const set = (key, fire) => {
    cur = key;
    for (const [k, b] of btns) {
      const on = k === key;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    }
    if (box._panel && btns.get(key)) box._panel.setAttribute("aria-labelledby", btns.get(key).id);
    if (fire) onChange(key);
  };
  const sid = ++uidN;
  for (const it of items) {
    const b = el("button", { type: "button", role: "tab", id: `tab-${sid}-${it.key}`, class: "cg-btn", disabled: it.disabled },
      icon("check", { class: "cg-check" }), it.icon ? icon(it.icon, { class: "cg-icon" }) : null, el("span", { class: "cg-label", text: it.label }),
      it.count !== undefined && it.count !== null ? el("span", { class: "cg-count", text: String(it.count) }) : null);
    b.addEventListener("click", () => { if (b.getAttribute("aria-selected") === "true") return; haptic("select"); set(it.key, true); });
    btns.set(it.key, b);
    box.appendChild(b);
  }
  rovingArrows(box, ".cg-btn:not([disabled])", (b) => b.click());
  linkPanel(box, btns, () => cur);
  set(active, false);
  box.set = (k) => set(k, false);
  return box;
}

// Стандартная button group: при нажатии кнопка расширяется, соседи сжимаются (CSS)
export const buttonGroup = (...children) => el("div", { class: "bgroup", role: "group" }, ...children);

function rovingArrows(box, selector, activate) {
  box.addEventListener("keydown", (ev) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(ev.key)) return;
    const list = [...box.querySelectorAll(selector)];
    const i = list.indexOf(document.activeElement);
    if (i < 0) return;
    ev.preventDefault();
    const j = ev.key === "Home" ? 0 : ev.key === "End" ? list.length - 1 : clamp(i + (ev.key === "ArrowRight" ? 1 : -1), 0, list.length - 1);
    list[j].focus();
    activate(list[j]);
  });
}

// Split button: основное действие + меню «▾»
export function splitButton({ label, icon: ic, onClick, menu: items, variant = "filled", menuLabel = "Другие варианты" }) {
  const main = button({ label, icon: ic, variant, onClick, cls: "split-main" });
  const more = el("button", { type: "button", class: `btn btn-${variant} btn-s split-more`, "aria-label": menuLabel, "aria-haspopup": "menu", "aria-expanded": "false" },
    icon("keyboard_arrow_down", { class: "split-chev" }));
  more.addEventListener("click", () => {
    haptic("light");
    more.setAttribute("aria-expanded", "true");
    openMenu(more, typeof items === "function" ? items() : items, () => more.setAttribute("aria-expanded", "false"));
  });
  const wrap = el("div", { class: "split", role: "group", "aria-label": label }, main, more);
  wrap.main = main;
  wrap.more = more;
  return wrap;
}

// ---------------------------------------------------------------- меню
let openMenuEl = null;
export function closeMenu() {
  if (!openMenuEl) return false;
  const m = openMenuEl;
  openMenuEl = null;
  m._onClose && m._onClose();
  const a = animate(m, [{ opacity: 1 }, { opacity: 0 }], "effects-fast");
  if (a) a.finished.then(() => m.remove(), () => m.remove()); else m.remove();
  if (m._anchor && m._anchor.isConnected) m._anchor.focus();
  return true;
}
export function openMenu(anchor, items, onClose) {
  closeMenu();
  const menu = el("div", { class: "menu", role: "menu", "aria-orientation": "vertical", "aria-label": anchor.getAttribute("aria-label") || undefined });
  menu._anchor = anchor;
  menu._onClose = onClose;
  for (const it of items) {
    if (it === "divider") { menu.appendChild(el("div", { class: "menu-div", role: "separator" })); continue; }
    const mi = el("button", { type: "button", role: "menuitem", class: "menu-item" + (it.danger ? " is-danger" : ""), disabled: it.disabled },
      it.icon ? icon(it.icon) : null,
      el("span", { class: "menu-text" }, el("span", { class: "menu-label", text: it.label }), it.supporting ? el("span", { class: "menu-sup", text: it.supporting }) : null));
    mi.addEventListener("click", () => { closeMenu(); haptic("light"); it.onClick && it.onClick(); });
    menu.appendChild(mi);
  }
  document.body.appendChild(menu);
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  const vw = innerWidth, vh = innerHeight;
  let left = clamp(r.right - mw, 8, vw - mw - 8);
  let top = r.bottom + 4;
  if (top + mh > vh - 8) top = Math.max(8, r.top - mh - 4);
  menu.style.left = left + "px";
  menu.style.top = top + "px";
  menu.style.transformOrigin = top < r.top ? "bottom right" : "top right";
  animate(menu, [{ opacity: 0, transform: "scaleY(.6)" }, { opacity: 1, transform: "none" }], "spatial-fast");
  openMenuEl = menu;
  const first = menu.querySelector(".menu-item:not([disabled])");
  first && first.focus();
  menu.addEventListener("keydown", (ev) => {
    const list = [...menu.querySelectorAll(".menu-item:not([disabled])")];
    const i = list.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (list.length) list[(i + (ev.key === "ArrowDown" ? 1 : list.length - 1)) % list.length].focus();
    } else if (ev.key === "Home" || ev.key === "End") {
      ev.preventDefault();
      if (list.length) list[ev.key === "Home" ? 0 : list.length - 1].focus();
    } else if (ev.key === "Escape" || ev.key === "Tab") { ev.preventDefault(); ev.stopPropagation(); closeMenu(); }
  });
  setTimeout(() => document.addEventListener("pointerdown", function off(ev) {
    if (!menu.contains(ev.target)) { closeMenu(); }
    if (!openMenuEl || openMenuEl !== menu) document.removeEventListener("pointerdown", off, true);
  }, true), 0);
  return menu;
}

// ---------------------------------------------------------------- FAB
export function extendedFab({ icon: ic, label, onClick, variant = "primary" }) {
  const b = el("button", { type: "button", class: `fab fab-ext fab-${variant}`, "aria-label": label },
    icon(ic, { class: "fab-icon" }), el("span", { class: "fab-label", text: label }));
  if (onClick) b.addEventListener("click", onClick);
  b.collapse = (c) => b.classList.toggle("is-collapsed", !!c);
  b.hide = (h) => b.classList.toggle("is-hidden", !!h);
  return b;
}

// ---------------------------------------------------------------- чипы
export function chip({ label, icon: ic, avatar, onClick, onRemove, variant = "assist", selected, aria }) {
  const c = el("button", { type: "button", class: `chip chip-${variant}${avatar ? " has-avatar" : ""}`, "aria-pressed": variant === "filter" ? String(!!selected) : undefined, "aria-label": aria },
    avatar || (ic ? icon(ic, { class: "chip-icon" }) : null), el("span", { class: "chip-label", text: label }));
  if (onClick) c.addEventListener("click", onClick);
  if (!onRemove) return c;
  const x = iconButton("close", `Убрать «${label}»`, { cls: "chip-x", onClick: (ev) => { ev.stopPropagation(); onRemove(); } });
  return el("span", { class: "chip-wrap" }, c, x);
}

// ---------------------------------------------------------------- списки и карточки
export function listItem({ leading, headline, supporting, meta, trailing, onClick, cls, label, lines = 2 }) {
  const body = [
    leading ? el("span", { class: "li-leading" }, leading) : null,
    el("span", { class: "li-text" },
      el("span", { class: "li-headline", text: headline }),
      supporting ? el("span", { class: "li-supporting", text: supporting }) : null),
    meta ? el("span", { class: "li-meta", text: meta }) : null,
  ];
  const main = onClick
    ? el("button", { type: "button", class: "li-main", "aria-label": label }, body)
    : el("div", { class: "li-main" }, body);
  if (onClick) main.addEventListener("click", onClick);
  return el("li", { class: `li li-${lines}${onClick ? " is-action" : ""}${cls ? " " + cls : ""}` }, main,
    trailing ? el("span", { class: "li-trailing" }, trailing) : null);
}

export const list = (items, cls = "") => el("ul", { class: "list " + cls, role: "list" }, items);

export function card({ media, title, subtitle, onClick, round, variant = "filled", label, cls }) {
  const c = el(onClick ? "button" : "div", { type: onClick ? "button" : undefined, class: `card card-${variant}${round ? " card-round" : ""}${cls ? " " + cls : ""}`, "aria-label": label },
    media ? el("span", { class: "card-media" }, media) : null,
    el("span", { class: "card-text" },
      el("span", { class: "card-title", text: title }),
      subtitle ? el("span", { class: "card-sub", text: subtitle }) : null));
  if (onClick) c.addEventListener("click", onClick);
  return c;
}

// Сетка с навигацией стрелками (roving focus по фактическим колонкам).
// Элементы — кнопки-карточки: роль listitem на <button> убрала бы у него роль button, поэтому роли списка не ставим.
export function grid(items, cls = "") {
  const g = el("div", { class: "grid " + cls }, items);
  g.addEventListener("keydown", (ev) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(ev.key)) return;
    const cells = [...g.children];
    const i = cells.indexOf(document.activeElement);
    if (i < 0) return;
    const top = cells[0].offsetTop;
    let cols = cells.findIndex((c) => c.offsetTop !== top);
    if (cols < 0) cols = cells.length;
    const d = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[ev.key];
    const j = i + d;
    if (j >= 0 && j < cells.length) { ev.preventDefault(); cells[j].focus(); cells[j].scrollIntoView({ block: "nearest", inline: "nearest" }); }
  });
  return g;
}

export function carousel(items, { label = "Карусель" } = {}) {
  const track = el("div", { class: "carousel", role: "list", "aria-label": label },
    items.map((it) => el("div", { class: "carousel-item", role: "listitem" }, it)));
  track.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    const cells = [...track.querySelectorAll(".carousel-item > *")];
    const i = cells.indexOf(document.activeElement);
    const j = i + (ev.key === "ArrowRight" ? 1 : -1);
    if (i >= 0 && cells[j]) { ev.preventDefault(); cells[j].focus(); cells[j].scrollIntoView({ inline: "nearest", block: "nearest" }); }
  });
  return track;
}

export function badge(n) {
  const b = el("span", { class: "badge", "aria-hidden": "true" });
  b.set = (v) => { b.hidden = !v; b.textContent = v > 99 ? "99+" : String(v || ""); };
  b.set(n);
  return b;
}

export function sectionTitle(text, trailing) {
  return el("div", { class: "section-head" }, el("h2", { class: "section-title", text }), trailing || null);
}

// ---------------------------------------------------------------- прогресс
// M3E loading indicator: морфинг 7 форм с пружиной + вращение
const LI_SEQ = ["softBurst", "cookie9", "pentagon", "pill", "sunny", "cookie4", "oval"].map(radii);
const springStep = (f) => { const t = f * 0.55, w = Math.sqrt(800), z = 0.6, wd = w * Math.sqrt(1 - z * z); return 1 - Math.exp(-z * w * t) * (Math.cos(wd * t) + (z * w / wd) * Math.sin(wd * t)); };

export function loadingIndicator({ size = 48, contained = false, label = "Загрузка" } = {}) {
  const path = svg("path", { class: "lind-shape" });
  const box = el("span", { class: "lind" + (contained ? " lind-contained" : ""), role: "progressbar", "aria-label": label, style: { width: size + "px", height: size + "px" } });
  const s = svg("svg", { viewBox: "0 0 48 48", width: "100%", height: "100%", "aria-hidden": "true" }, path);
  box.appendChild(s);
  const R = contained ? 15 : 19;
  const rr = new Float32Array(LI_SEQ[0].length);
  if (reducedMotion()) { path.setAttribute("d", pathFromRadii(LI_SEQ[1], 24, 24, R)); return box; }
  let attached = false;
  const t0 = performance.now();
  const frame = (now) => {
    if (box.isConnected) attached = true;
    else if (attached || now - t0 > 5000) return; // удалён из DOM — стоп
    const t = Math.max(0, now - t0); // метка rAF может быть раньше t0
    const phase = t / 650;
    const i = Math.floor(phase) % LI_SEQ.length;
    const f = springStep(phase % 1);
    const a = LI_SEQ[i], b = LI_SEQ[(i + 1) % LI_SEQ.length];
    for (let k = 0; k < rr.length; k++) rr[k] = a[k] + (b[k] - a[k]) * f;
    path.setAttribute("d", pathFromRadii(rr, 24, 24, R, t * 0.0021 + i * 0.6));
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  return box;
}

// Wavy circular progress: value 0..1 или null (неопределённый)
export function wavyCircular({ value = null, size = 40, label = "Загрузка", cls = "", thin = false } = {}) {
  const r = 17, amp = thin ? 0 : 1.4, waves = 9;
  let d = "";
  for (let i = 0; i <= 180; i++) {
    const t = (i / 180) * Math.PI * 2 - Math.PI / 2;
    const rr = r + amp * Math.sin(waves * t);
    d += (i ? "L" : "M") + (24 + Math.cos(t) * rr).toFixed(2) + " " + (24 + Math.sin(t) * rr).toFixed(2);
  }
  const track = svg("circle", { class: "wc-track", cx: 24, cy: 24, r, "stroke-width": thin ? 4 : 4 });
  const active = svg("path", { class: "wc-active", d, pathLength: 100, "stroke-width": thin ? 5 : 4 });
  const s = svg("svg", { viewBox: "0 0 48 48", width: "100%", height: "100%", "aria-hidden": "true" }, track, active);
  const box = el("span", { class: `wcirc${value === null ? " is-indet" : ""}${thin ? " is-thin" : ""} ${cls}`, role: "progressbar", "aria-label": label, style: { width: size + "px", height: size + "px" } }, s);
  box.set = (v) => {
    box.classList.toggle("is-indet", v === null);
    if (v === null) { active.removeAttribute("stroke-dasharray"); box.removeAttribute("aria-valuenow"); return; }
    const p = clamp(v, 0, 1) * 100;
    active.setAttribute("stroke-dasharray", `${p} ${100 - p}`);
    box.setAttribute("aria-valuenow", String(Math.round(p)));
    box.setAttribute("aria-valuemin", "0");
    box.setAttribute("aria-valuemax", "100");
  };
  box.set(value);
  return box;
}

// Wavy linear progress: активная часть — волна (mask), остаток — трек с зазором и stop-индикатором
export function wavyLinear({ value = null, label = "Прогресс" } = {}) {
  const active = el("span", { class: "wl-active" });
  const track = el("span", { class: "wl-track" });
  const box = el("span", { class: "wlin" + (value === null ? " is-indet" : ""), role: "progressbar", "aria-label": label }, track, active, el("span", { class: "wl-stop" }));
  box.set = (v) => {
    box.classList.toggle("is-indet", v === null);
    if (v === null) { box.removeAttribute("aria-valuenow"); return; }
    const p = clamp(v, 0, 1) * 100;
    box.style.setProperty("--p", p.toFixed(2) + "%");
    box.classList.toggle("is-full", p >= 99.5);
    box.setAttribute("aria-valuenow", String(Math.round(p)));
    box.setAttribute("aria-valuemin", "0");
    box.setAttribute("aria-valuemax", "100");
  };
  box.set(value);
  return box;
}

// ---------------------------------------------------------------- snackbar
let snackQueue = [];
let snackShowing = false;
let snackCur = null; // {error, close, busy}: показываемый снекбар
// Модальные слои (диалог, шторка) регистрируют обработчик «назад» (кнопка Telegram / системный жест). Подключает shell.
let modalBack = null;
export function setModalBackRegistrar(fn) { modalBack = fn; }

// Объявление для скринридера: отдельные постоянные live-области (в index.html), а не role на самом снекбаре —
// вставленный динамически узел с role=status часто не озвучивается, а кнопки внутри читались бы как часть сообщения.
export function announce(text, error) {
  const node = document.getElementById(error ? "sb-alert" : "sb-live");
  if (!node) return;
  node.textContent = "";
  setTimeout(() => { node.textContent = text; }, 60);
}

export function snackbar(text, { action, onAction, duration, error = false } = {}) {
  // WCAG 2.2.1: сообщения с действием и ошибки висят дольше; пауза при наведении/фокусе
  const d = duration || (action || error ? 9000 : 5000);
  snackQueue.push({ text, action, onAction, duration: d, error });
  // M3: новое сообщение заменяет текущее информационное (ошибку дочитываем)
  if (snackShowing && snackCur && !snackCur.error && !snackCur.busy()) snackCur.close();
  if (!snackShowing) nextSnack();
}
function nextSnack() {
  const item = snackQueue.shift();
  const host = document.getElementById("snackbar-host");
  if (!item || !host) { snackShowing = false; return; }
  snackShowing = true;
  const sb = el("div", { class: "snackbar" + (item.error ? " is-error" : "") },
    el("span", { class: "sb-text", text: item.text }));
  let timer;
  const arm = () => { clearTimeout(timer); timer = setTimeout(close, item.duration); };
  const hold = () => clearTimeout(timer);
  sb.addEventListener("pointerenter", hold);
  sb.addEventListener("pointerleave", () => { if (!sb.contains(document.activeElement)) arm(); });
  sb.addEventListener("focusin", hold);
  sb.addEventListener("focusout", (ev) => { if (!sb.contains(ev.relatedTarget)) arm(); });
  sb.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); close(); } });
  const prevFocus = document.activeElement;
  let closed = false;
  const close = () => {
    clearTimeout(timer);
    if (closed || !sb.isConnected) return;
    closed = true;
    // фокус был внутри снекбара — вернуть туда, откуда пришёл, а не терять в body
    if (sb.contains(document.activeElement) && prevFocus && prevFocus.isConnected && !prevFocus.closest("[hidden]")) prevFocus.focus({ preventScroll: true });
    const a = animate(sb, [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(8px)" }], "effects-default");
    const done = () => { sb.remove(); setTimeout(nextSnack, 120); };
    if (a) a.finished.then(done, done); else done();
  };
  // не закрываем досрочно, пока пользователь с ним работает (фокус/наведение)
  snackCur = { error: item.error, close: () => close(), busy: () => sb.matches(":hover") || sb.contains(document.activeElement) };
  if (item.action) {
    sb.appendChild(button({ label: item.action, variant: "text", cls: "sb-action", onClick: () => { close(); item.onAction && item.onAction(); } }));
    // M3: длинный текст с действием — раскладка «в столбик» (действие под текстом)
    if (item.text.length + item.action.length > 44) sb.classList.add("is-stacked");
  }
  sb.appendChild(iconButton("close", "Закрыть уведомление", { cls: "sb-close", onClick: close }));
  host.appendChild(sb);
  announce(item.text, item.error);
  animate(sb, [{ opacity: 0, transform: "translateY(16px) scale(.96)" }, { opacity: 1, transform: "none" }], "spatial-fast");
  arm();
}

// ---------------------------------------------------------------- диалог
// Формы: validate() → "" (всё верно) или текст ошибки — он показывается под полем (role=alert), поле aria-invalid;
// onConfirm() → null (успех, диалог закрывается) или текст ошибки — диалог остаётся открытым, введённое не теряется.
// locked() → true — диалог нельзя закрыть (идёт необратимое действие): Esc, фон, «назад» и первые кнопки не работают.
// spinner: false — без индикатора на кнопке подтверждения (свой индикатор в содержимом).
export function dialog({ icon: ic, headline, body, actions = [{ label: "OK", value: true }], dismissValue = false, content, validate, onConfirm, locked = null, spinner = true }) {
  return new Promise((resolve) => {
    const uid = ++uidN;
    const danger = actions.some((a) => a.variant === "danger");
    const d = el("dialog", {
      class: "dialog", "aria-labelledby": `dlg-h-${uid}`, "aria-describedby": body ? `dlg-b-${uid}` : undefined,
      role: danger ? "alertdialog" : undefined,
    });
    let opener = document.activeElement;
    const fromSheet = opener && opener.closest && opener.closest("dialog.sheet"); // диалог из пункта шторки → вернуться к кнопке шторки
    if (fromSheet && fromSheet._opener) opener = fromSheet._opener;
    const isLocked = () => !!(locked && locked());
    const releaseBack = modalBack ? modalBack(() => { if (!isLocked()) done(dismissValue); }) : null;
    const done = (v) => {
      if (!d.open) return;
      if (releaseBack) releaseBack();
      const a = animate(d, [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(.94)" }], "effects-fast");
      const fin = () => {
        d.close(); d.remove();
        // вернуть фокус на элемент, открывший диалог (если он ещё в дереве и виден)
        if (opener && opener !== document.body && opener.isConnected && !opener.closest("[hidden]") && opener.getClientRects().length) opener.focus({ preventScroll: true });
      };
      if (a) a.finished.then(fin, fin); else fin();
      resolve(v);
    };
    append(d, [
      ic ? el("span", { class: "dlg-icon", "aria-hidden": "true" }, icon(ic)) : null,
      el("h2", { class: "dlg-headline", id: `dlg-h-${uid}`, text: headline }),
      // M3: заголовок и кнопки закреплены, прокручивается только содержимое
      el("div", { class: "dlg-content" },
        body ? el("p", { class: "dlg-body", id: `dlg-b-${uid}`, text: body }) : null,
        content || null,
        (validate || onConfirm) ? el("p", { class: "dlg-error", id: `dlg-e-${uid}`, role: "alert" }) : null),
      el("div", { class: "dlg-actions" }, actions.map((a, k) => button({ label: a.label, variant: a.variant || "text",
        onClick: () => ((validate || onConfirm) && k === actions.length - 1 ? submit() : isLocked() ? null : done(a.value)) }))),
    ]);
    let busy = false;
    const verdict = () => { const v = validate ? validate() : ""; return v === true || v === "" || v == null ? "" : typeof v === "string" ? v : "Заполните поле"; };
    const showErr = (msg) => {
      const box = d.querySelector(".dlg-error");
      const field = d.querySelector("input, textarea");
      box.textContent = "";
      setTimeout(() => { box.textContent = msg; }, 30);
      if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
    };
    async function submit() {
      if (busy) return;
      const err = verdict();
      if (err) { showErr(err); return; }
      if (onConfirm) {
        const okBtn = d.querySelector(".dlg-actions .btn:last-child");
        busy = true;
        if (spinner) setBusy(okBtn, true); else okBtn.setAttribute("aria-disabled", "true");
        let r;
        try { r = await onConfirm(); } catch (e) { r = (e && e.message) || "Не удалось сохранить"; }
        busy = false;
        if (okBtn.isConnected) { if (spinner) setBusy(okBtn, false); else okBtn.setAttribute("aria-disabled", "false"); }
        if (r) { showErr(r); return; }
      }
      done(actions[actions.length - 1].value);
    }
    d.addEventListener("cancel", (ev) => { ev.preventDefault(); if (!isLocked()) done(dismissValue); });
    d.addEventListener("click", (ev) => { if (ev.target === d && !isLocked()) done(dismissValue); });
    document.body.appendChild(d);
    d.showModal();
    animate(d, [{ opacity: 0, transform: "scale(.9)" }, { opacity: 1, transform: "none" }], "spatial-fast");
    if (validate || onConfirm) {
      // кнопка остаётся фокусируемой (aria-disabled, а не disabled) — нажатие объясняет, что не так
      const ok = d.querySelector(".dlg-actions .btn:last-child");
      const field = d.querySelector("input, textarea");
      const check = () => { ok.setAttribute("aria-disabled", verdict() ? "true" : "false"); };
      if (field) {
        const ids = [field.getAttribute("aria-describedby"), `dlg-e-${uid}`].filter(Boolean).join(" ");
        field.setAttribute("aria-describedby", ids);
      }
      d.addEventListener("input", () => {
        check();
        if (field && !verdict()) { field.removeAttribute("aria-invalid"); d.querySelector(".dlg-error").textContent = ""; }
      });
      d.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && ev.target.tagName === "INPUT" && !ev.isComposing) { ev.preventDefault(); submit(); }
      });
      check();
      if (field) { field.focus(); field.select && field.select(); return; }
    }
    // разрушающее действие не получает фокус по умолчанию — безопасный выбор «Отмена» (первая кнопка)
    const primary = d.querySelector(danger ? ".dlg-actions .btn:first-child" : ".dlg-actions .btn:last-child");
    primary && primary.focus();
  });
}

// ---------------------------------------------------------------- M3 slider (role=slider, клавиатура, указатель)
// toPos/fromPos — нелинейная шкала (лог для частоты); format(v) — подпись значения и aria-valuetext.
// round(x) — своё округление вместо шага step; keyStep(v, dir, big) — значение после стрелки (big — Shift/PageUp/PageDown).
// input — поле ручного ввода рядом с ползунком (M3 outlined, плотное): {parse(text) → число|NaN, fmt(v) → текст поля,
//   unit, unitName (для скринридера), example} — Enter/blur применяют, Escape откатывает, выход за границы — ошибка.
export function slider({ label, min, max, step = 1, value, format = (v) => String(v), onInput, onChange, toPos, fromPos, ticks = 0, cls = "", id, round, keyStep, input } = {}) {
  const sid = id || `sl-${++uidN}`;
  const pos = toPos || ((v) => (v - min) / (max - min));
  const val = fromPos || ((p) => min + p * (max - min));
  const out = el("span", { class: "msl-value", "aria-hidden": "true" });
  // M3E: сегменты дорожки с зазором 6dp вокруг ручки, точки-стопы 4dp на концах неактивной дорожки;
  // с нулём посередине (ticks) — активный сегмент от нуля до значения, слева/справа — неактивные
  const segL = ticks ? el("span", { class: "msl-seg msl-inl" }) : null;
  const segA = el("span", { class: "msl-seg msl-act" });
  const segR = el("span", { class: "msl-seg msl-in" });
  const stopL = ticks ? el("span", { class: "msl-stop is-start" }) : null;
  const stopR = el("span", { class: "msl-stop is-end" });
  const track = el("div", { class: "msl-track", role: "slider", tabindex: "0", "aria-labelledby": `${sid}-l`, "aria-valuemin": String(min), "aria-valuemax": String(max) },
    segL, segA, segR, stopL, stopR, el("span", { class: "msl-handle" }));
  if (ticks) track.classList.add("has-zero");
  const field = input ? numberField() : null;
  const box = el("div", { class: "msl " + cls + (input ? " has-input" : "") },
    el("div", { class: "msl-head" }, el("span", { class: "msl-label", id: `${sid}-l`, text: label }), field ? field.el : out, field ? field.err : null), track);
  let v = value, dragging = false;
  const snap = (x) => { const s = round ? round(x) : Math.round((x - min) / step) * step + min; return clamp(Number(Number(s).toFixed(4)), min, max); };
  const G = 8; // от центра ручки (4dp) до сегмента: 2 + зазор 6dp
  const seg = (node, a, b) => { if (!node) return; node.style.left = a; node.style.width = `max(0px, calc(${b} - ${a}))`; };
  const paintTrack = (p) => {
    const P = (p * 100).toFixed(3) + "%", lo = `calc(${P} - ${G}px)`, hi = `calc(${P} + ${G}px)`;
    if (!ticks) { seg(segA, "0%", lo); seg(segR, hi, "100%"); }
    else {
      const z = clamp(pos(0), 0, 1), Z = (z * 100).toFixed(3) + "%";
      if (p >= z) { seg(segL, "0%", `min(${Z}, ${lo})`); seg(segA, Z, lo); seg(segR, hi, "100%"); }
      else { seg(segL, "0%", lo); seg(segA, hi, Z); seg(segR, `max(${Z}, ${hi})`, "100%"); }
    }
    // стоп прячется, когда ручка подходит к нему
    const w = track.clientWidth || 300;
    stopR.classList.toggle("is-hidden", (1 - p) * w < 18);
    if (stopL) stopL.classList.toggle("is-hidden", p * w < 18);
  };
  const paint = () => {
    const p = clamp(pos(v), 0, 1);
    box.style.setProperty("--p", (p * 100).toFixed(2) + "%");
    paintTrack(p);
    out.textContent = format(v);
    if (field) field.show(v);
    track.setAttribute("aria-valuenow", String(v));
    track.setAttribute("aria-valuetext", format(v));
  };
  const setV = (x, fire) => { const n = snap(x); if (n === v && !fire) return; v = n; paint(); onInput && onInput(v); };
  const fromX = (x) => { const r = track.getBoundingClientRect(); return val(clamp((x - r.left) / r.width, 0, 1)); };
  track.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    try { track.setPointerCapture(ev.pointerId); } catch { /* */ }
    dragging = true; track.classList.add("is-dragging");
    if (field) field.clearError();
    if (ev.pointerType === "mouse") track.focus({ preventScroll: true });
    setV(fromX(ev.clientX));
  });
  track.addEventListener("pointermove", (ev) => { if (dragging) setV(fromX(ev.clientX)); });
  const end = () => { if (!dragging) return; dragging = false; track.classList.remove("is-dragging"); onChange && onChange(v); };
  track.addEventListener("pointerup", end);
  track.addEventListener("pointercancel", end);
  track.addEventListener("keydown", (ev) => {
    const dir = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -1, PageUp: 1 }[ev.key];
    let to = null;
    if (dir !== undefined) {
      const big = ev.shiftKey || ev.key === "PageUp" || ev.key === "PageDown";
      if (keyStep) to = keyStep(v, dir, big);
      // нелинейная шкала: шаг — по позиции (1/100 хода, крупный — 1/10)
      else if (toPos) to = val(clamp(pos(v) + dir * (big ? 0.1 : 0.01), 0, 1));
      else to = v + dir * (big ? Math.max(step, (max - min) / 10) : step);
      // округление не должно «съедать» мелкий шаг — хотя бы на один шаг в нужную сторону
      if (snap(to) === v && to !== v) to = v + dir * step;
    } else if (ev.key === "Home") to = min;
    else if (ev.key === "End") to = max;
    if (to === null) return;
    ev.preventDefault();
    if (field) field.clearError();
    setV(to, true);
    onChange && onChange(v);
  });

  // поле ручного ввода: живой предпросмотр и запись — те же onInput/onChange, что у ползунка, только при применении
  function numberField() {
    const inp = el("input", { class: "msl-input", type: "text", inputmode: "decimal", autocomplete: "off", spellcheck: "false", enterkeyhint: "done",
      "aria-labelledby": `${sid}-l`, "aria-describedby": `${sid}-u ${sid}-e` });
    const unit = el("span", { class: "msl-unit", id: `${sid}-u` }, el("span", { "aria-hidden": "true", text: input.unit || "" }),
      el("span", { class: "sr-only", text: input.unitName || "" }));
    const errIc = icon("error", { class: "msl-err-ic", fill: true });
    errIc.setAttribute("aria-hidden", "true");
    const wrap = el("span", { class: "msl-field" + (input.unit ? "" : " no-unit") }, inp, unit, errIc);
    wrap.addEventListener("click", (ev) => { if (ev.target !== inp) inp.focus(); }); // тап по суффиксу — тоже в поле
    // supporting text под полем: «info» (значение зажато в границы) — on-surface-variant; «error» (не число) — ошибка
    const err = el("p", { class: "msl-sup", id: `${sid}-e`, "aria-live": "polite" });
    let editing = false;
    const fmt = (x) => (input.fmt ? input.fmt(x) : String(x));
    const setMsg = (text, kind = "error") => {
      const bad = !!text && kind === "error";
      err.textContent = text;
      err.classList.toggle("is-error", bad);
      wrap.classList.toggle("is-error", bad);
      if (bad) inp.setAttribute("aria-invalid", "true"); else inp.removeAttribute("aria-invalid");
    };
    const setError = (text) => setMsg(text, "error");
    const limits = () => `от ${fmt(min)} до ${fmt(max)}${input.unit ? " " + input.unit : ""}`;
    function apply() {
      const raw = inp.value.trim();
      if (raw === fmt(v)) { setError(""); return true; }
      const n = input.parse ? input.parse(raw) : Number(raw.replace(",", "."));
      if (!raw || !Number.isFinite(n)) { setError(`Введите число${input.example ? `, например ${input.example}` : ""} — допустимо ${limits()}`); return false; }
      const c = clamp(n, min, max);
      setV(c, true);
      onChange && onChange(v);
      setMsg(c !== n ? `Допустимо ${limits()} — установлено ${fmt(v)}` : "", "info");
      inp.value = fmt(v);
      return true;
    }
    inp.addEventListener("focus", () => { editing = true; inp.select(); });
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); if (apply()) inp.select(); }
      else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); inp.value = fmt(v); setError(""); inp.select(); }
    });
    inp.addEventListener("blur", () => {
      editing = false;
      if (!apply()) inp.value = fmt(v); // не число — оставляем прежнее значение, подсказка ошибки остаётся
    });
    return { el: wrap, err, input: inp, show: (x) => { if (!editing) inp.value = fmt(x); }, clearError: () => setError("") };
  }

  box.set = (x) => { if (!dragging) { v = snap(x); paint(); } };
  box.get = () => v;
  box.track = track;
  if (field) box.input = field.input;
  paint();
  return box;
}

// ---------------------------------------------------------------- поля формы
// M3 outlined text field: плавающая метка, счётчик символов, supporting text
export function textField({ label, value = "", maxlength, supporting, multiline = false, name } = {}) {
  const id = `tf-${++uidN}`;
  const input = el(multiline ? "textarea" : "input", {
    id, class: "tf-input", name, maxlength: maxlength ? String(maxlength) : undefined, placeholder: " ", autocomplete: "off",
    type: multiline ? undefined : "text", rows: multiline ? "3" : undefined, "aria-describedby": supporting || maxlength ? `${id}-s` : undefined,
  });
  input.value = value;
  const count = maxlength ? el("span", { class: "tf-count", "aria-hidden": "true" }) : null;
  const limit = maxlength ? el("span", { class: "sr-only", text: ` Не более ${maxlength} символов.` }) : null;
  const sync = () => { if (count) count.textContent = `${input.value.length}/${maxlength}`; };
  input.addEventListener("input", sync);
  sync();
  const box = el("div", { class: "tf" + (multiline ? " tf-multi" : "") },
    el("div", { class: "tf-box" }, input, el("label", { class: "tf-label", for: id, text: label })),
    supporting || count ? el("div", { class: "tf-support", id: `${id}-s` }, el("span", { text: supporting || "" }, limit), count) : null);
  box.input = input;
  return box;
}

// M3 switch: вся строка — цель нажатия, сам переключатель — role=switch
export function switchRow({ label, supporting, checked = false, onChange, disabled } = {}) {
  const id = `sw-${++uidN}`;
  const thumb = el("span", { class: "sw-thumb" }, icon("check", { class: "sw-icon" }));
  const sw = el("button", { type: "button", role: "switch", class: "sw", "aria-checked": checked ? "true" : "false", "aria-labelledby": `${id}-l`, "aria-describedby": supporting ? `${id}-s` : undefined, disabled },
    el("span", { class: "sw-track" }, thumb));
  const row = el("div", { class: "sw-row" + (disabled ? " is-disabled" : "") },
    el("div", { class: "sw-text" }, el("span", { class: "sw-label", id: `${id}-l`, text: label }), supporting ? el("span", { class: "sw-sup", id: `${id}-s`, text: supporting }) : null), sw);
  const set = (v, fire) => { sw.setAttribute("aria-checked", v ? "true" : "false"); if (fire && onChange) onChange(v); };
  sw.addEventListener("click", (ev) => { ev.stopPropagation(); haptic("select"); set(sw.getAttribute("aria-checked") !== "true", true); });
  row.addEventListener("click", (ev) => { if (!sw.disabled && (ev.target === row || ev.target.closest(".sw-text"))) sw.click(); });
  Object.defineProperty(row, "checked", { get: () => sw.getAttribute("aria-checked") === "true", set: (v) => set(!!v, false) });
  row.control = sw;
  return row;
}

// ---------------------------------------------------------------- bottom sheet
// scope — узел с контент-схемой (например, «Сейчас играет»): лист открывается в её цветах
export function sheet({ title, build, label, cls, scope }) {
  const d = el("dialog", { class: "sheet" + (cls ? " " + cls : ""), "aria-label": label || title });
  if (scope && scope.style) for (const prop of Array.from(scope.style)) if (prop.startsWith("--")) d.style.setProperty(prop, scope.style.getPropertyValue(prop));
  const body = el("div", { class: "sheet-body" });
  // ручка — только для жеста; альтернатива свайпу (WCAG 2.5.7): Esc, тап по затемнению, «назад» Telegram
  const handle = el("div", { class: "sheet-handle-zone" }, el("span", { class: "sheet-handle", "aria-hidden": "true" }));
  const opener = document.activeElement;
  d._opener = opener;
  let closed = false;
  const releaseBack = modalBack ? modalBack(() => close()) : null;
  function close() {
    if (closed) return;
    closed = true;
    if (releaseBack) releaseBack();
    const a = animate(d, [{ transform: getComputedStyle(d).transform === "none" ? "none" : getComputedStyle(d).transform }, { transform: "translateY(100%)" }], "effects-slow");
    const fin = () => {
      d.close(); d.remove();
      if (opener && opener !== document.body && opener.isConnected && !opener.closest("[hidden]") && opener.getClientRects().length) opener.focus({ preventScroll: true });
    };
    if (a) a.finished.then(fin, fin); else fin();
  }
  append(d, [handle, title ? el("h2", { class: "sheet-title", text: title }) : null, body]);
  d.addEventListener("cancel", (ev) => { ev.preventDefault(); close(); });
  d.addEventListener("click", (ev) => { if (ev.target === d) close(); });
  // свайп вниз за ручку
  let y0 = null;
  handle.addEventListener("pointerdown", (ev) => { y0 = ev.clientY; handle.setPointerCapture(ev.pointerId); });
  handle.addEventListener("pointermove", (ev) => { if (y0 !== null) d.style.transform = `translateY(${Math.max(0, ev.clientY - y0)}px)`; });
  const end = (ev) => {
    if (y0 === null) return;
    const dy = ev.clientY - y0;
    y0 = null;
    if (dy > 80) close(); else d.style.transform = "";
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  build(body, close);
  document.body.appendChild(d);
  d.showModal();
  animate(d, [{ transform: "translateY(100%)" }, { transform: "none" }], "spatial-default");
  return { close, body, el: d };
}

// ---------------------------------------------------------------- состояния экранов
export function emptyState({ shape = "cookie9", icon: ic = "music_note", title, text, action, compact = false }) {
  return el("div", { class: "state" + (compact ? " state-compact" : ""), role: "status" },
    el("span", { class: "state-art", style: { "--state-shape": `var(--shape-${shape})` } }, icon(ic, { fill: true })),
    title ? el("h2", { class: "state-title", text: title }) : null,
    text ? el("p", { class: "state-text", text }) : null,
    action ? el("div", { class: "state-actions" }, action) : null);
}

export function errorState(err, retry) {
  const offline = err && err.status === 0;
  const expired = err && err.code === "expired";
  return emptyState({
    shape: "softBurst", icon: offline ? "wifi_off" : expired ? "history" : "cloud_off",
    title: offline ? "Нет связи" : expired ? "Результаты устарели" : "Что-то пошло не так",
    text: (err && err.message) || String(err),
    action: retry ? button({ label: expired ? "Обновить" : "Повторить", icon: "refresh", variant: "tonal", onClick: retry }) : null,
  });
}

export function loadingState(text) {
  return el("div", { class: "state state-loading", "aria-busy": "true" }, loadingIndicator({ size: 56, contained: true }),
    text ? el("p", { class: "state-text", text }) : null);
}

export function skeletonList(n = 6, { thumb = true, round = false } = {}) {
  const rows = [];
  for (let i = 0; i < n; i++) {
    rows.push(el("li", { class: "li li-2 skel-row", "aria-hidden": "true" },
      el("span", { class: "li-main" },
        thumb ? el("span", { class: "li-leading" }, el("span", { class: "skel skel-thumb" + (round ? " round" : "") })) : null,
        el("span", { class: "li-text" }, el("span", { class: "skel skel-line", style: { width: `${55 + ((i * 37) % 35)}%` } }),
          el("span", { class: "skel skel-line sm", style: { width: `${30 + ((i * 23) % 30)}%` } })))));
  }
  return el("div", { class: "skel-wrap", role: "status", "aria-busy": "true" }, el("span", { class: "sr-only", text: "Загрузка…" }), el("ul", { class: "list skel-list", "aria-hidden": "true" }, rows));
}

export function skeletonGrid(n = 6, round = false) {
  const items = [];
  for (let i = 0; i < n; i++) {
    items.push(el("div", { class: "card card-filled skel-card" + (round ? " card-round" : ""), "aria-hidden": "true" },
      el("span", { class: "card-media" }, el("span", { class: "skel skel-media" })),
      el("span", { class: "card-text" }, el("span", { class: "skel skel-line" }), el("span", { class: "skel skel-line sm" }))));
  }
  return el("div", { class: "grid", "aria-busy": "true" }, items);
}

export { mount };
