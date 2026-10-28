// Перестановка рядов списка: перетаскивание за ручку (pointer, с автопрокруткой) и клавиатура —
// стрелки ↑/↓ на ручке (альтернатива жесту, WCAG 2.5.7). Ряды — дети list с data-key;
// onMove(from, to) перестраивает DOM сам (синхронно), после чего фокус возвращается на ручку того же ряда.
import { haptic } from "./tg.js";

function say(text) {
  const node = document.getElementById("sb-live");
  if (!node) return;
  node.textContent = "";
  setTimeout(() => { node.textContent = text; }, 60);
}

export function sortable(list, { handle = ".drag-handle", onMove, scroller = null, label = (i, n) => `Позиция ${i + 1} из ${n}` }) {
  let drag = null, raf = 0;
  const rowsOf = () => [...list.children].filter((r) => r.dataset.key);

  const refocus = (key) => {
    const r = list.querySelector(`[data-key="${CSS.escape(key)}"]`);
    const h = r && r.querySelector(handle);
    if (h) h.focus({ preventScroll: false });
    const rows = rowsOf();
    if (r) say(label(rows.indexOf(r), rows.length));
  };

  list.addEventListener("keydown", (ev) => {
    const h = ev.target.closest && ev.target.closest(handle);
    if (!h || !list.contains(h) || (ev.key !== "ArrowUp" && ev.key !== "ArrowDown")) return;
    const rows = rowsOf();
    const row = h.closest("[data-key]");
    const from = rows.indexOf(row);
    const to = from + (ev.key === "ArrowUp" ? -1 : 1);
    ev.preventDefault();
    if (from < 0 || to < 0 || to >= rows.length) return;
    const key = row.dataset.key;
    haptic("select");
    onMove(from, to);
    refocus(key);
  });

  list.addEventListener("pointerdown", (ev) => {
    const h = ev.target.closest && ev.target.closest(handle);
    if (!h || !list.contains(h) || ev.button !== 0) return;
    const rows = rowsOf();
    const row = h.closest("[data-key]");
    const from = rows.indexOf(row);
    if (from < 0) return;
    ev.preventDefault();
    try { h.setPointerCapture(ev.pointerId); } catch { /* */ }
    const rects = rows.map((r) => r.getBoundingClientRect());
    drag = { h, row, rows, from, to: from, y0: ev.clientY, y: ev.clientY, rects, hgt: rects[from].height, s0: scroller ? scroller.scrollTop : 0 };
    row.classList.add("is-dragging");
    list.classList.add("is-sorting");
    haptic("light");
    tick();
  });

  function layout() {
    const d = drag;
    const dy = d.y - d.y0 + (scroller ? scroller.scrollTop - d.s0 : 0);
    d.row.style.transform = `translateY(${dy}px)`;
    const center = d.rects[d.from].top + d.hgt / 2 + dy;
    let to = d.from;
    for (let i = d.from + 1; i < d.rows.length; i++) if (center > d.rects[i].top + d.rects[i].height / 2) to = i;
    for (let i = d.from - 1; i >= 0; i--) if (center < d.rects[i].top + d.rects[i].height / 2) to = i;
    if (to !== d.to) haptic("select");
    d.to = to;
    d.rows.forEach((r, i) => {
      if (r === d.row) return;
      const shift = d.from < to && i > d.from && i <= to ? -d.hgt : to < d.from && i >= to && i < d.from ? d.hgt : 0;
      r.style.transform = shift ? `translateY(${shift}px)` : "";
    });
  }

  // автопрокрутка у краёв контейнера
  function tick() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(function step() {
      if (!drag) return;
      if (scroller) {
        const r = scroller.getBoundingClientRect();
        const edge = 56;
        if (drag.y < r.top + edge) scroller.scrollTop -= Math.ceil((r.top + edge - drag.y) / 6);
        else if (drag.y > r.bottom - edge) scroller.scrollTop += Math.ceil((drag.y - (r.bottom - edge)) / 6);
      }
      layout();
      raf = requestAnimationFrame(step);
    });
  }

  list.addEventListener("pointermove", (ev) => { if (drag) { drag.y = ev.clientY; } });
  const end = () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    cancelAnimationFrame(raf);
    for (const r of d.rows) r.style.transform = "";
    d.row.classList.remove("is-dragging");
    list.classList.remove("is-sorting");
    if (d.to !== d.from) {
      const key = d.row.dataset.key;
      onMove(d.from, d.to);
      const rows = rowsOf();
      const r = list.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (r) say(label(rows.indexOf(r), rows.length));
    }
  };
  list.addEventListener("pointerup", end);
  list.addEventListener("pointercancel", end);
  list.addEventListener("lostpointercapture", end);
}
