// Режим выбора (M3E): долгое нажатие на ряд или «Выбрать» в меню → у рядов появляются флажки, сверху —
// контекстная панель «Выбрано: N из M · Выбрать все», снизу — плавающая панель действий (зависит от экрана).
// Выбор хранится по ключам, поэтому переживает прокрутку, «Показать ещё» и перерисовку списка.
// Shift+клик — диапазон (мышь); Esc / «назад» — выход; число выбранных объявляется скринридеру.
import { el, icon, setText } from "./dom.js";
import { iconButton, announce, snackbar } from "./ui.js";
import { haptic } from "./tg.js";
import { pushBackHandler } from "./shell.js";

// rows() — ряды (li) в порядке на экране; key(row) — стабильный ключ; item(row) — объект для действий;
// actions(items, ctl) — [{icon, label, run, danger?}]; sticky — панели прилипают внутри прокрутки (лист очереди).
// Весь результат, а не только загруженные ряды: loadAll(onProgress) → Promise<items[] | {items}> (все страницы
// с текущей сортировкой/фильтром), itemKey(item) — тот же ключ, что key(row); total() — размер результата (для «N из M»).
export function selectable(host, { rows, key, item, actions, sticky = false, loadAll = null, itemKey = null, total = null, title = (r) => (r.querySelector(".li-headline") || {}).textContent || "" }) {
  const sel = new Set();
  // full — весь результат (ключ → объект) после «Выбрать все»; выбор дальше правится по ключам, не теряясь
  let active = false, last = null, releaseBack = null, mo = null, full = null, loading = 0;
  // заголовок контекстной панели (M3): число крупно, «Выбрано:» — для скринридера (на телефоне «N из M» не переносится)
  const countNum = el("span", { class: "sel-num" });
  const count = el("span", { class: "sel-count", "aria-live": "polite" }, el("span", { class: "sr-only", text: "Выбрано: " }), countNum);
  const allBtn = el("button", { type: "button", class: "btn btn-text sel-all" }, el("span", { class: "btn-label", text: "Выбрать все" }));
  const top = el("div", { class: "sel-top" + (sticky ? " is-sticky" : ""), role: "toolbar", "aria-label": "Выбор" },
    iconButton("close", "Отменить выбор", { onClick: () => stop() }), count, allBtn);
  const bar = el("div", { class: "sel-bar" + (sticky ? " is-sticky" : ""), role: "toolbar", "aria-label": "Действия с выбранными" });
  const whole = !!(loadAll && itemKey);
  const totalNow = () => { const n = total ? Number(total()) : NaN; return Number.isFinite(n) && n >= 0 ? n : null; };
  host.classList.add("sel-host");

  const rowsNow = () => rows().filter((r) => r.isConnected);
  const isSel = (r) => sel.has(key(r));
  // свои изменения DOM не должны будить наблюдатель (иначе бесконечный цикл)
  const observe = () => { if (mo && active) mo.observe(host, { childList: true, subtree: true }); };
  function paint() {
    if (mo) mo.disconnect();
    try { paintNow(); } finally { observe(); }
  }
  function paintNow() {
    for (const r of rowsNow()) {
      decorate(r);
      const on = isSel(r);
      r.classList.toggle("is-selected", on);
      r._selBox.setAttribute("aria-checked", on ? "true" : "false");
    }
    if (full) for (const [k, it] of full) if (it._gone) { full.delete(k); sel.delete(k); } // удалённые — из «всех»
    const n = selectedCount();
    const m = full ? full.size : totalNow();
    setText(countNum, m !== null && whole ? `${n} из ${m}` : String(n));
    if (!loading) setText(allBtn.querySelector(".btn-label"), allSelected() ? "Снять все" : whole && m ? `Выбрать все · ${m}` : "Выбрать все");
    renderBar();
  }
  // выбраны ли все: весь результат (если он известен) или все видимые ряды
  function allSelected() {
    if (full) { for (const [k, it] of full) if (!it._gone && !sel.has(k)) return false; return selectedCount() > 0; }
    const all = rowsNow();
    if (!all.length || !all.every(isSel)) return false;
    const m = whole ? totalNow() : null;
    return m === null || all.length >= m;
  }
  function decorate(r) {
    if (r._selBox) return;
    const box = el("button", { type: "button", class: "sel-box", role: "checkbox", "aria-checked": "false", "aria-label": `Выбрать: ${title(r)}` },
      el("span", { class: "sel-check", "aria-hidden": "true" }, icon("check")));
    box.addEventListener("click", (ev) => { ev.stopPropagation(); toggle(r, ev.shiftKey); });
    r.prepend(box);
    r._selBox = box;
  }
  function renderBar() {
    const items = selectedItems();
    bar.replaceChildren(...actions(items, ctl).filter(Boolean).map((a) => {
      const b = el("button", { type: "button", class: "sel-act" + (a.danger ? " is-danger" : ""), "aria-disabled": items.length ? "false" : "true" }, icon(a.icon), el("span", { text: a.label }));
      b.addEventListener("click", () => { if (!items.length) { announce("Ничего не выбрано"); return; } haptic("light"); a.run(selectedItems(), ctl, b); });
      return b;
    }));
  }
  function toggle(r, range = false) {
    const k = key(r);
    const add = (row) => { const kk = key(row); sel.add(kk); if (full && !full.has(kk)) { const it = item(row); if (it) full.set(kk, it); } };
    if (range && last !== null) {
      const list = rowsNow(), a = list.findIndex((x) => key(x) === last), b = list.indexOf(r);
      if (a >= 0 && b >= 0) { const [lo, hi] = a < b ? [a, b] : [b, a]; for (let i = lo; i <= hi; i++) add(list[i]); }
    } else if (sel.has(k)) sel.delete(k); else add(r);
    last = k;
    haptic("select");
    paint();
    announce(`Выбрано: ${selectedCount()}`);
  }
  allBtn.addEventListener("click", async () => {
    if (loading) return;
    if (allSelected()) { sel.clear(); full = null; paint(); announce("Выбор снят"); return; }
    if (!whole) {
      for (const r of rowsNow()) sel.add(key(r));
      paint();
      announce(`Выбрано: ${selectedCount()}`);
      return;
    }
    // весь результат: догружаем все страницы (с текущей сортировкой/жанром/фильтром), показываем ход
    const my = ++loading;
    const label = allBtn.querySelector(".btn-label");
    allBtn.setAttribute("aria-busy", "true");
    setText(label, "Загружаю…");
    try {
      // ход — в процентах: ширина кнопки не скачет (tabular-nums + min-width в CSS)
      const r = await loadAll((got, of) => { if (my === loading) setText(label, of ? `Загружаю… ${Math.min(100, Math.round((got / of) * 100))} %` : "Загружаю…"); });
      if (my !== loading || !active) return; // выбор отменили, пока грузили
      const items = Array.isArray(r) ? r : (r && r.items) || [];
      full = new Map();
      for (const it of items) { if (it && !it._gone) full.set(String(itemKey(it)), it); }
      for (const k of full.keys()) sel.add(k);
      for (const row of rowsNow()) if (!full.has(key(row))) { const it = item(row); if (it) { full.set(key(row), it); sel.add(key(row)); } }
      loading = 0;
      paint();
      announce(`Выбрано все: ${selectedCount()}`);
    } catch (e) {
      if (my !== loading) return;
      loading = 0;
      paint();
      snackbar(`Не удалось выбрать все: ${(e && e.message) || "ошибка сети"}`, { error: true });
    } finally {
      if (my === loading || !loading) allBtn.removeAttribute("aria-busy");
    }
  });
  // в режиме выбора тап по ряду — отметить, а не играть/открыть
  const capture = (ev) => {
    if (!active) return;
    const r = ev.target.closest && ev.target.closest(".li");
    if (!r || !rowsNow().includes(r) || ev.target.closest(".sel-box")) return;
    ev.preventDefault();
    ev.stopPropagation();
    toggle(r, ev.shiftKey);
  };
  host.addEventListener("click", capture, true);
  host.addEventListener("contextmenu", (ev) => { if (active && ev.target.closest(".li")) ev.preventDefault(); }, true);

  function start(fromRow) {
    if (!active) {
      active = true;
      host.classList.add("is-selecting");
      host.append(top, bar);
      releaseBack = pushBackHandler(() => { if (!host.isConnected || host.closest("[hidden]")) { stop(true); return false; } stop(); },
        { node: host, prune: () => stop(true) });
      // новые ряды («Показать ещё», перерисовка) — тоже с флажками
      let t = 0;
      mo = new MutationObserver(() => { clearTimeout(t); t = setTimeout(() => { if (active) paint(); }, 30); });
      haptic("medium");
    }
    if (fromRow) { const k = key(fromRow); sel.add(k); last = k; }
    paint();
    announce(`Режим выбора. Выбрано: ${sel.size}`);
    const first = (fromRow && fromRow._selBox) || top.querySelector("button");
    if (first) first.focus({ preventScroll: true });
  }
  // dropped — обработчик «назад» уже выброшен оболочкой (хост скрыт/удалён)
  function stop(dropped = false) {
    if (!active) return;
    active = false;
    full = null;
    loading = 0;
    allBtn.removeAttribute("aria-busy");
    sel.clear();
    last = null;
    if (mo) { mo.disconnect(); mo = null; }
    host.classList.remove("is-selecting");
    top.remove(); bar.remove();
    for (const r of rows()) { if (r._selBox) { r._selBox.remove(); r._selBox = null; } r.classList.remove("is-selected"); }
    if (releaseBack) { const rb = releaseBack; releaseBack = null; rb(); } // повторно безопасно (уже выброшенный — не найдётся)
    if (!dropped) announce("Выбор отменён");
  }
  // весь результат — в порядке списка (сервера); удалённые (_gone) не считаются
  function selectedItems() {
    if (full) { const out = []; for (const [k, it] of full) if (sel.has(k) && !it._gone) out.push(it); return out; }
    return rowsNow().filter(isSel).map(item).filter((it) => it && !it._gone);
  }
  const selectedCount = () => selectedItems().length;
  // ключи пропавших рядов (удалены) — убрать из выбора; во «всём результате» — только удалённые (_gone)
  function prune() {
    if (full) { for (const [k, it] of full) if (it._gone) { full.delete(k); sel.delete(k); } }
    else { const live = new Set(rowsNow().map(key)); for (const k of [...sel]) if (!live.has(k)) sel.delete(k); }
    paint();
  }
  const ctl = { start, stop, toggle, prune, get active() { return active; }, get size() { return selectedCount(); }, items: selectedItems, refresh: paint };
  host._sel = ctl;
  return ctl;
}

// ряд → его контроллер выбора (для долгого нажатия и пункта «Выбрать» в меню)
export const selectionFor = (row) => { const h = row && row.closest && row.closest(".sel-host"); return h ? h._sel : null; };
