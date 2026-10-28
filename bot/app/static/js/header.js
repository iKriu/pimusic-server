// Top app bars: small (корни вкладок, с подъёмом при прокрутке) и large flexible (альбом/исполнитель):
// шапка сворачивается при прокрутке, обложка уменьшается и «морфится» (круг ↔ скруглённый квадрат).
import { el, pic, clamp, mount, setText } from "./dom.js";
import { iconButton } from "./ui.js";
import { back } from "./shell.js";

// Прокрутка контейнера → data-scrolled (тональный подъём шапки) и колбэк с направлением
export function trackScroll(view, scroller, fn) {
  let last = 0, raf = 0;
  scroller.addEventListener("scroll", () => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const y = scroller.scrollTop;
      view.toggleAttribute("data-scrolled", y > 4);
      if (fn) fn(y, y > last ? 1 : y < last ? -1 : 0);
      last = y;
    });
  }, { passive: true });
}

export function topBar({ title, subtitle, trailing, leading }) {
  const sub = el("p", { class: "tbar-sub", text: subtitle || "" });
  const h = el("h1", { class: "tbar-title", text: title });
  const bar = el("header", { class: "tbar" },
    leading || null,
    el("div", { class: "tbar-text" }, h, sub),
    trailing ? el("div", { class: "tbar-actions" }, trailing) : null);
  bar.setSub = (t) => { sub.textContent = t || ""; sub.hidden = !t; };
  bar.setSub(subtitle);
  return bar;
}

// kind: "album" — квадрат → диск; "artist" — круг → скруглённый квадрат
export function flexHeader(page, { kind = "album", cover, ph = "album", overline, title, actions }) {
  const coverBox = el("div", { class: "fh-cover" });
  const setCover = (url) => mount(coverBox, pic(url, { cls: "fh-pic", ph, eager: true, alt: "" }));
  setCover(cover);
  // дублирует h1 для зрячих при сворачивании шапки — скринридеру не нужен
  const barTitle = el("span", { class: "fh-bar-title", "aria-hidden": "true", text: title || "" });
  const titleEl = el("h1", { class: "fh-title", text: title || "" });
  const over = el("p", { class: "fh-overline", text: overline || "" });
  const sub = el("div", { class: "fh-sub" });
  const acts = el("div", { class: "fh-actions" }, actions || null);
  const backBtn = iconButton("arrow_back", "Назад", { cls: "fh-back", onClick: () => back() });
  const barActions = el("div", { class: "fh-bar-actions" });
  const bar = el("div", { class: "fh-bar" }, backBtn, barTitle, barActions);
  const hero = el("div", { class: "fh-hero" }, coverBox, el("div", { class: "fh-text" }, over, titleEl, sub, acts));
  const header = el("header", { class: `fh fh-${kind}` }, hero);
  // верхняя панель — поверх прокрутки (не внутри шапки), чтобы оставаться видимой на всей странице
  page.el.classList.add(`page-fh-${kind}`);
  page.el.appendChild(bar);
  page.hero = coverBox;

  // прогресс сворачивания 0..1 → CSS-переменная --p на странице
  const update = () => {
    const range = Math.max(120, hero.offsetHeight - 64);
    const p = clamp(page.scroll.scrollTop / range, 0, 1);
    page.el.style.setProperty("--p", p.toFixed(3));
    page.el.toggleAttribute("data-collapsed", p > 0.82);
  };
  let raf = 0;
  page.scroll.addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; update(); }); }, { passive: true });
  requestAnimationFrame(update);

  return {
    el: header, cover: coverBox, setCover,
    setTitle(t) { setText(titleEl, t); setText(barTitle, t); },
    setOverline(t) { over.textContent = t || ""; },
    setSub(nodes) { mount(sub, nodes); },
    setActions(nodes) { mount(acts, nodes); },
    setBarActions(nodes) { mount(barActions, nodes); },
  };
}
