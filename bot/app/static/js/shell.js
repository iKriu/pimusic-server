// Оболочка: window size classes, навигация (bar ↔ rail), canonical layouts (list-detail, supporting pane),
// стек страниц по вкладкам, BackButton/MainButton, клавиатура.
import { el, icon } from "./dom.js";
import { badge, closeMenu, emptyState, setModalBackRegistrar } from "./ui.js";
import { scope } from "./api.js";
import { tg, haptic, setBackButton, setMainButton } from "./tg.js";
import { sharedAxis, containerTransform, fadeThrough, animate, reducedMotion } from "./motion.js";
import * as store from "./store.js";
import { stopPreview } from "./player.js";

export const TABS = [
  { key: "search", label: "Поиск", icon: "search" },
  { key: "library", label: "Медиатека", icon: "library_music" },
  { key: "downloads", label: "Загрузки", icon: "download" },
  { key: "wave", label: "Волна", icon: "waves" },
];
const SINGLE = new Set(["downloads", "wave"]); // вкладки без панели деталей

const app = document.getElementById("app");
const paneList = document.getElementById("pane-list");
const paneDetail = document.getElementById("pane-detail");
const paneSupport = document.getElementById("pane-support");

export const shell = {
  tab: "search",
  wsc: "compact",
  twoPane: false,
  roots: {},
  stacks: { search: [], library: [], downloads: [], wave: [] },
  backHandlers: [],
  support: null,
  onSettings: null,
};

const badges = [];
const listeners = new Set();
export const onLayout = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };

// ---------------------------------------------------------------- навигация
function buildNav() {
  const mk = (kind) => {
    const nav = el("nav", { class: kind === "bar" ? "nav-bar" : "nav-rail", "aria-label": "Разделы" });
    if (kind === "rail") {
      nav.appendChild(el("button", { type: "button", class: "rail-brand", "aria-label": "PiMusic — о сервере", onclick: () => shell.onSettings && shell.onSettings() },
        el("span", { class: "rail-brand-shape" }, icon("graphic_eq")), el("span", { class: "rail-brand-text", text: "PiMusic" })));
    }
    const items = el("div", { class: "nav-items" });
    for (const t of TABS) {
      const b = badge(0);
      if (t.key === "downloads") badges.push(b);
      const btn = el("button", { type: "button", class: "nav-item", "data-tab": t.key, "aria-label": t.label },
        el("span", { class: "nav-ind" }, icon(t.icon, { class: "nav-icon" }), t.key === "downloads" ? b : null),
        el("span", { class: "nav-label", text: t.label }));
      btn.addEventListener("click", () => { if (shell.tab !== t.key) { haptic("select"); go(t.key); } else popToRoot(); });
      items.appendChild(btn);
    }
    nav.appendChild(items);
    return nav;
  };
  app.prepend(mk("rail"));
  app.appendChild(mk("bar"));
}

// Бейдж aria-hidden (визуальный), поэтому число активных загрузок попадает в имя вкладки
export function setDownloadsBadge(n) {
  for (const b of badges) b.set(n);
  const t = TABS.find((x) => x.key === "downloads");
  for (const btn of document.querySelectorAll('.nav-item[data-tab="downloads"]')) {
    btn.setAttribute("aria-label", n ? `${t.label}, требуют внимания: ${n}` : t.label);
  }
}

// Масштаб текста (системный/Telegram): проба в шрифте body-large; если строка выросла — включаем перенос вместо усечения
function detectTextScale() {
  let probe = document.getElementById("ts-probe");
  if (!probe) {
    probe = el("span", { id: "ts-probe", "aria-hidden": "true", text: "Mg" });
    probe.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;white-space:nowrap;font:var(--md-sys-typescale-body-large);line-height:normal";
    document.body.appendChild(probe);
  }
  const big = probe.getBoundingClientRect().height > 28;
  document.documentElement.dataset.textscale = big ? "large" : "normal";
}

// ---------------------------------------------------------------- size classes
// Экранная клавиатура уменьшает высоту окна, но класс окна от неё меняться не должен: высоту берём наибольшую,
// замеченную для этой ширины (поворот меняет ширину — и ключ), а пока поле ввода в фокусе и высота заметно
// просела при той же ширине — раскладку не трогаем (и прячем нижнюю навигацию и мини-плеер, как в других приложениях).
const maxH = new Map(); // ширина (с шагом 8px) → наибольшая высота без клавиатуры
const typingEl = () => {
  const a = document.activeElement;
  if (!a) return false;
  if (a.tagName === "TEXTAREA" || a.isContentEditable) return true;
  return a.tagName === "INPUT" && !/^(button|checkbox|radio|range|submit|reset|file|color|image)$/i.test(a.type || "");
};
function viewportH() {
  const vv = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  const tgH = tg && tg.initData && tg.viewportHeight > 100 ? tg.viewportHeight : Infinity;
  return Math.min(window.innerHeight, vv, tgH);
}
function computeLayout() {
  const w = window.innerWidth;
  const key = Math.round(w / 8);
  const cur = viewportH();
  const best = maxH.get(key) || 0;
  const kb = typingEl() && best > 0 && cur < best * 0.8;
  app.dataset.kbd = kb ? "1" : "0";
  if (kb && shell.wsc) { refresh(); return; } // клавиатура открыта — класс окна заморожен
  maxH.set(key, Math.max(best, cur));
  const stable = tg && tg.initData && tg.viewportStableHeight > 100 ? tg.viewportStableHeight : 0;
  const h = Math.max(maxH.get(key), Math.min(stable || 0, window.innerHeight));
  let wsc = w < 600 ? "compact" : w < 840 ? "medium" : w < 1200 ? "expanded" : w < 1600 ? "large" : "xl";
  if (h < 480 && w > h) wsc = "compact-landscape";
  const prevTwo = shell.twoPane;
  shell.wsc = wsc;
  shell.twoPane = ["expanded", "large", "xl"].includes(wsc);
  const ds = app.dataset;
  ds.wsc = wsc;
  ds.nav = wsc === "compact" ? "bar" : "rail";
  ds.rail = wsc === "compact-landscape" ? "icons" : wsc === "xl" ? "expanded" : "labels"; // DESIGN 6a.3
  ds.panes = shell.twoPane ? "2" : "1";
  detectTextScale();
  refresh();
  if (prevTwo !== shell.twoPane) for (const fn of listeners) fn();
}

function refresh() {
  const ds = app.dataset;
  const stack = shell.stacks[shell.tab];
  ds.tab = shell.tab;
  ds.single = SINGLE.has(shell.tab) && !stack.length ? "1" : "0";
  ds.detail = stack.length ? "open" : "empty";
  // закреплённая панель «Сейчас играет» занимает место supporting pane (DESIGN §7)
  const supportOn = shell.tab !== "downloads" && ds.np !== "pane" && (shell.wsc === "xl" || (shell.wsc === "large" && store.activeCount() > 0));
  ds.support = supportOn ? "on" : "off";
  if (shell.support) shell.support.setVisible(supportOn);
  for (const b of app.querySelectorAll(".nav-item")) {
    const on = b.dataset.tab === shell.tab;
    b.classList.toggle("is-active", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    b.querySelector(".nav-icon").classList.toggle("ms-fill", on);
  }
  for (const [k, r] of Object.entries(shell.roots)) r.el.hidden = k !== shell.tab;
  for (const [k, st] of Object.entries(shell.stacks)) st.forEach((p, i) => { p.el.hidden = !(k === shell.tab && i === st.length - 1); });
  placeholder.hidden = !!stack.length;
  pruneBack();
  setBackButton(stack.length > 0 || shell.backHandlers.length > 0);
  syncMainButton();
}

// ---------------------------------------------------------------- вкладки
let rootScroll = {};
export function go(tab, { focus } = {}) {
  if (!shell.roots[tab]) return;
  closeMenu();
  if (tab !== shell.tab) stopPreview(); // фрагмент живёт только на своём экране
  const prev = shell.tab;
  const prevRoot = shell.roots[prev];
  if (prevRoot && prevRoot.scroller) rootScroll[prev] = prevRoot.scroller.scrollTop;
  if (prevRoot && prevRoot.hide) prevRoot.hide();
  shell.tab = tab;
  refresh();
  const r = shell.roots[tab];
  if (r.scroller && rootScroll[tab] !== undefined) r.scroller.scrollTop = rootScroll[tab];
  if (r.show) r.show({ focus });
  if (prev !== tab) {
    const dir = TABS.findIndex((t) => t.key === tab) > TABS.findIndex((t) => t.key === prev) ? 1 : -1;
    sharedAxis(r.el, dir);
    const top = shell.stacks[tab][shell.stacks[tab].length - 1];
    if (top && shell.twoPane) sharedAxis(top.el, dir);
  }
}

// ---------------------------------------------------------------- страницы (стек)
const placeholder = emptyState({ shape: "clover", icon: "album", title: "Выберите альбом или исполнителя", text: "Здесь откроются подробности — треклист, дискография и кнопки загрузки.", compact: true });
placeholder.classList.add("detail-placeholder");

export function createPage(kind) {
  const scroller = el("div", { class: "scroll" });
  const view = el("section", { class: `view page page-${kind}`, "data-kind": kind }, scroller);
  const page = {
    el: view, scroll: scroller, sc: scope(), kind, main: null, hero: null, destroyers: [],
    onDestroy(fn) { this.destroyers.push(fn); },
    setMain(cfg) { this.main = cfg; syncMainButton(); },
    get alive() { return this.sc.alive; },
  };
  return page;
}

// from: "list" — выбор в списке (в list-detail заменяет детали), "detail" — переход внутри деталей (push)
export function open(build, { kind = "page", from = "detail", origin = null } = {}) {
  closeMenu();
  stopPreview();
  const stack = shell.stacks[shell.tab];
  if (from === "list" && shell.twoPane) { while (stack.length) destroy(stack.pop()); }
  const root = shell.roots[shell.tab];
  if (!stack.length && root && root.scroller) rootScroll[shell.tab] = root.scroller.scrollTop;
  const prevTop = stack[stack.length - 1];
  if (prevTop) prevTop.savedScroll = prevTop.scroll.scrollTop;
  const page = createPage(kind);
  const ae = document.activeElement;
  page.opener = ae && ae !== document.body ? ae : null; // куда вернуть фокус при «назад»
  const update = () => {
    stack.push(page);
    paneDetail.appendChild(page.el);
    build(page);
    refresh();
    page.scroll.scrollTop = 0;
    focusHeading(page.el); // новая страница — фокус на её заголовок (иначе он остаётся на скрытом элементе)
  };
  haptic("light");
  containerTransform(origin, update, (what) => (what === "page" ? page.el : page.hero));
  if (!origin) fadeThrough(page.el);
  return page;
}

// Фокус на заголовок контейнера (tabindex=-1 — программная цель, не пункт Tab-порядка)
export function focusHeading(root) {
  if (!root) return;
  const h = root.querySelector(".fh-title, .tbar-title, h1") || root.querySelector(".scroll") || root;
  if (!h.hasAttribute("tabindex")) h.setAttribute("tabindex", "-1");
  h.focus({ preventScroll: true });
}

const isShown = (n) => n && n.isConnected && !n.closest("[hidden]") && !n.closest("[inert]") && n.getClientRects().length > 0;

function destroy(page) {
  page.sc.abort();
  for (const fn of page.destroyers) { try { fn(); } catch { /* */ } }
  page.el.remove();
}

const stale = (fn) => fn._node && (!fn._node.isConnected || fn._node.closest("[hidden]"));
function pruneBack() {
  for (let i = shell.backHandlers.length - 1; i >= 0; i--) {
    const fn = shell.backHandlers[i];
    if (stale(fn)) { shell.backHandlers.splice(i, 1); try { fn._prune && fn._prune(); } catch { /* */ } }
  }
}
export function back() {
  if (closeMenu()) return true;
  pruneBack();
  const h = shell.backHandlers[shell.backHandlers.length - 1];
  if (h) { if (h() !== false) return true; }
  const stack = shell.stacks[shell.tab];
  if (!stack.length) return false;
  stopPreview();
  const page = stack.pop();
  haptic("light");
  const fin = () => destroy(page);
  const a = animate(page.el, [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateX(24px)" }], "effects-default");
  page.el.style.pointerEvents = "none";
  page.el.inert = true; // уходящая страница не должна ловить фокус/чтение
  // старую страницу держим поверх, пока анимируется уход
  if (a) a.finished.then(fin, fin); else fin();
  refresh();
  page.el.hidden = false;
  const top = stack[stack.length - 1];
  if (top) { top.scroll.scrollTop = top.savedScroll || 0; fadeThrough(top.el); }
  else {
    const r = shell.roots[shell.tab];
    if (r && r.scroller && rootScroll[shell.tab] !== undefined) requestAnimationFrame(() => { r.scroller.scrollTop = rootScroll[shell.tab]; });
    if (r && r.show) r.show({ back: true });
  }
  // фокус возвращаем туда, откуда страницу открыли; нет — на заголовок страницы под ней или раздела
  if (isShown(page.opener)) page.opener.focus({ preventScroll: true });
  else focusHeading(top ? top.el : shell.roots[shell.tab] && shell.roots[shell.tab].el);
  return true;
}

export function popToRoot() {
  const stack = shell.stacks[shell.tab];
  if (!stack.length) { const r = shell.roots[shell.tab]; if (r && r.scroller) r.scroller.scrollTo({ top: 0, behavior: reducedMotion() ? "auto" : "smooth" }); return; }
  stopPreview();
  while (stack.length) destroy(stack.pop());
  refresh();
  const r = shell.roots[shell.tab];
  if (r && r.show) r.show({ back: true });
}

// node — узел, без которого обработчик не нужен (снят со страницы/скрыт — обработчик выбрасывается, вызывается prune)
export function pushBackHandler(fn, { node = null, prune = null } = {}) {
  if (node) { fn._node = node; fn._prune = prune; }
  shell.backHandlers.push(fn);
  refresh();
  return () => { const i = shell.backHandlers.indexOf(fn); if (i >= 0) shell.backHandlers.splice(i, 1); refresh(); };
}

// MainButton Telegram — только на compact, дублирует главное действие верхней страницы
export function syncMainButton() {
  const stack = shell.stacks[shell.tab];
  const top = stack[stack.length - 1];
  const show = top && top.main && !shell.twoPane && shell.backHandlers.length === 0;
  if (!show) { setMainButton(null); return; }
  const cs = getComputedStyle(top.el);
  setMainButton({ ...top.main, color: cs.getPropertyValue("--md-sys-color-primary").trim() || undefined, textColor: cs.getPropertyValue("--md-sys-color-on-primary").trim() || undefined });
}

// ---------------------------------------------------------------- инициализация
export function initShell({ roots, support, onSettings }) {
  shell.roots = roots;
  shell.support = support;
  shell.onSettings = onSettings;
  setModalBackRegistrar(pushBackHandler);
  buildNav();
  for (const r of Object.values(roots)) paneList.appendChild(r.el);
  paneDetail.appendChild(placeholder);
  if (support) paneSupport.appendChild(support.el);
  computeLayout();
  let raf = 0;
  const relayout = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(computeLayout); };
  window.addEventListener("resize", relayout);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", relayout);
  // фокус в поле / уход из поля — клавиатура появляется/прячется; пересчёт с небольшой задержкой (анимация)
  document.addEventListener("focusin", () => setTimeout(relayout, 60));
  document.addEventListener("focusout", () => setTimeout(relayout, 160));
  // «К содержимому»: без смены location.hash (в нём живёт tgWebAppData) — фокус на заголовок видимой страницы
  const skip = document.querySelector(".skip-link");
  if (skip) skip.addEventListener("click", (ev) => {
    ev.preventDefault();
    const st = shell.stacks[shell.tab];
    const top = st[st.length - 1];
    focusHeading(top ? top.el : shell.roots[shell.tab].el);
  });
  // бейдж «Загрузки»: активные загрузки + файлы, ждущие названия
  const badgeN = () => store.activeCount() + (store.state.needInfo || 0);
  store.on("activity", () => { setDownloadsBadge(badgeN()); if (shell.wsc === "large") refresh(); });
  window.addEventListener("pimusic:need-info", () => setDownloadsBadge(badgeN()));

  document.addEventListener("keydown", (ev) => {
    const t = ev.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    if (ev.key === "/" && !typing && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !document.querySelector("dialog[open]")) {
      ev.preventDefault();
      if (shell.tab !== "search") go("search", { focus: true }); else shell.roots.search.show({ focus: true });
    } else if (ev.key === "Escape" && !document.querySelector("dialog[open]")) {
      if (typing && t.value && t.type === "search") return; // сначала очистка поля (нативно)
      if (back()) ev.preventDefault();
      else if (typing) t.blur();
    }
  });
  go("search");
  return { relayout: computeLayout };
}

export { refresh as refreshShell };
