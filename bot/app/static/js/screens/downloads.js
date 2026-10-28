// Загрузки: карточки импорта (wavy linear, ETA, Пауза | Стоп, «Что не нашлось»), «Сейчас качается»
// (wavy circular), задания, лента событий. Тот же рендер — в supporting pane на large/xl.
import { el, icon, mount, fmtTime, fmtEta, nTracks } from "../dom.js";
import { api } from "../api.js";
import {
  button, buttonGroup, list, listItem, wavyLinear, wavyCircular, dialog, sheet, snackbar, emptyState, errorState,
  loadingState, sectionTitle, setBusy,
} from "../ui.js";
import { topBar, trackScroll } from "../header.js";
import { haptic } from "../tg.js";
import * as store from "../store.js";
import { go } from "../shell.js";
import { uploadsPanel, dropTarget } from "../uploads.js";

// скорость импорта по замерам между опросами (треков/мин)
const samples = new Map();
function rate(imp, processed) {
  const now = Date.now();
  const s = samples.get(imp.id) || [];
  if (!s.length || s[s.length - 1][1] !== processed) s.push([now, processed]);
  while (s.length > 2 && now - s[0][0] > 30 * 60e3) s.shift();
  samples.set(imp.id, s);
  const [t0, p0] = s[0];
  if (now - t0 < 45e3 || processed <= p0) return 0;
  return (processed - p0) / ((now - t0) / 60e3);
}

function importCard(imp, { compact }) {
  const c = imp.counts || {};
  const processed = (c.done || 0) + (c.cached || 0) + (c.failed || 0);
  const left = (c.pending || 0) + (c.active || 0);
  const pct = imp.total ? Math.min(1, processed / imp.total) : 1;
  const paused = imp.state === "paused";
  const r = paused ? 0 : rate(imp, processed);
  const eta = r ? `осталось ≈ ${fmtEta(left / r)} · ${r.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ${r < 2 ? "трека" : "трека"}/мин` : paused ? "на паузе — новые треки не берутся" : "оцениваю скорость…";
  const prog = wavyLinear({ value: pct, label: `Импорт ${imp.filename}` });
  prog.classList.toggle("is-paused", paused);
  const stat = (label, n, cls) => el("div", { class: "stat " + (cls || "") }, el("span", { class: "stat-n", text: String(n) }), el("span", { class: "stat-l", text: label }));
  const toggle = button({ label: paused ? "Продолжить" : "Пауза", icon: paused ? "play_arrow" : "pause", variant: "tonal", onClick: async () => {
    haptic("medium");
    setBusy(toggle, true);
    try {
      await api(paused ? "/import/resume" : "/import/pause", { method: "POST", body: { id: imp.id } });
      snackbar(paused ? "Импорт продолжен" : "Импорт на паузе");
      store.kick();
    } catch (e) { haptic("error"); snackbar(e.message, { error: true }); setBusy(toggle, false); }
  } });
  const stop = button({ label: "Стоп", icon: "stop", variant: "outlined", cls: "btn-danger-outline", onClick: async () => {
    haptic("warning");
    const ok = await dialog({ icon: "stop", headline: "Остановить импорт?", body: "Уже скачанное останется в медиатеке, оставшиеся треки из очереди будут удалены.",
      actions: [{ label: "Отмена", value: false }, { label: "Остановить", value: true, variant: "danger" }] });
    if (!ok) return;
    setBusy(stop, true);
    try { await api("/import/cancel", { method: "POST", body: { id: imp.id } }); haptic("success"); snackbar("Импорт остановлен"); store.kick(); }
    catch (e) { haptic("error"); snackbar(e.message, { error: true }); setBusy(stop, false); }
  } });
  const failedBtn = c.failed ? button({ label: `Что не нашлось · ${c.failed}`, icon: "report", variant: "text", onClick: () => failedSheet(imp) }) : null;
  return el("article", { class: "card card-filled imp-card" + (compact ? " is-compact" : ""), "aria-label": `Импорт ${imp.filename}` },
    el("div", { class: "imp-head" },
      el("span", { class: "imp-shape", "aria-hidden": "true" }, icon("queue_music")),
      el("div", { class: "imp-title" }, el("h3", { text: imp.filename }), el("p", { text: `${processed} из ${imp.total} · ${Math.round(pct * 100)}%` })),
      el("span", { class: "imp-state" + (paused ? " is-paused" : ""), text: paused ? "Пауза" : "Идёт" })),
    prog,
    el("p", { class: "imp-eta", text: eta }),
    compact ? null : el("div", { class: "stats" }, stat("скачано", c.done || 0, "ok"), stat("уже было", c.cached || 0), stat("не нашлось", c.failed || 0, c.failed ? "bad" : ""), stat("в очереди", left)),
    el("div", { class: "imp-actions" }, buttonGroup(toggle, stop), failedBtn));
}

function failedSheet(imp) {
  sheet({
    title: "Что не нашлось", label: `Не нашлось в импорте ${imp.filename}`,
    async build(body) {
      mount(body, loadingState());
      try {
        const r = await api("/import/failed?id=" + encodeURIComponent(imp.id));
        if (!r.items.length) { mount(body, emptyState({ icon: "check_circle", title: "Всё нашлось", compact: true })); return; }
        mount(body, [el("p", { class: "note", text: `${nTracks(r.items.length)} не удалось найти в источниках. Их можно поискать вручную.` }),
          list(r.items.map((it) => listItem({ leading: icon("report", { class: "li-icon err" }), headline: it.title, supporting: [it.artist, it.album, it.note].filter(Boolean).join(" · ") })))]);
      } catch (e) { mount(body, errorState(e)); }
    },
  });
}

function renderSections(box, act, imports, { compact }) {
  const out = [];
  if (imports.length) {
    if (!compact) out.push(sectionTitle("Импорт медиатеки"));
    for (const imp of imports) out.push(importCard(imp, { compact }));
  }
  const nothing = !act.active.length && !act.jobs.length && !imports.length;
  if (act.active.length) {
    out.push(sectionTitle("Сейчас качается"));
    out.push(list(act.active.map((a) => listItem({ leading: wavyCircular({ size: 40, label: "Качается" }), headline: a, lines: 1, cls: "dl-active" }))));
  }
  if (act.jobs.length) {
    out.push(sectionTitle("Задания"));
    out.push(list(act.jobs.map((j) => listItem({ leading: el("span", { class: "li-icon-box" }, icon("queue_music")), headline: j.title, supporting: `${nTracks(j.total)} · с ${fmtTime(j.started)}` }))));
  }
  if (nothing) {
    out.push(emptyState({ shape: "sunny", icon: "download", title: "Сейчас ничего не качается", compact,
      text: act.queue ? `В общей очереди сервера: ${act.queue}` : "Нажмите «Скачать» в поиске — прогресс появится здесь.",
      action: compact ? null : button({ label: "К поиску", icon: "search", variant: "tonal", onClick: () => go("search", { focus: true }) }) }));
  }
  if (!compact) {
    out.push(sectionTitle("Журнал"));
    out.push(act.log.length
      ? list(act.log.map((it) => listItem({ headline: it.text, supporting: fmtTime(it.ts), cls: "log-item" + (/^❌|ошибк/i.test(it.text) ? " is-error" : "") })), "log")
      : el("p", { class: "note", text: "Здесь появятся события загрузок из этого приложения." }));
  }
  keepFocus(box, () => mount(box, out));
}

// Опрос перерисовывает блок целиком: если фокус был на кнопке внутри, возвращаем его на такую же (по имени, иначе по номеру)
function keepFocus(box, render) {
  const a = document.activeElement;
  const nameOf = (b) => (b.getAttribute("aria-label") || b.textContent || "").trim();
  let key = null;
  if (a && a !== document.body && box.contains(a) && a.tagName === "BUTTON") {
    key = { i: [...box.querySelectorAll("button")].indexOf(a), name: nameOf(a) };
  }
  render();
  if (!key) return;
  const btns = [...box.querySelectorAll("button:not(:disabled)")];
  const next = btns.find((b) => nameOf(b) === key.name) || btns[Math.min(key.i, btns.length - 1)];
  if (next) next.focus({ preventScroll: true });
}

export function createDownloads() {
  const scroller = el("div", { class: "scroll" });
  const view = el("section", { class: "view root root-downloads", "aria-label": "Загрузки" }, scroller);
  const queue = el("span", { class: "queue-chip", hidden: true });
  const bar = topBar({ title: "Загрузки", trailing: queue });
  const box = el("div", { class: "dl-content" }, loadingState());
  const ups = uploadsPanel();
  scroller.append(bar, ups.el, box);
  dropTarget(view); // файлы можно бросить на весь экран «Загрузок»
  trackScroll(view, scroller);
  let sig = "";
  let release = null;
  let visible = false;
  store.on("activity", (act) => {
    queue.hidden = !act.queue;
    queue.textContent = `в общей очереди: ${act.queue}`;
    bar.setSub(store.activeCount() ? `Активно: ${store.activeCount()}` : "");
    if (!visible) return;
    const s = JSON.stringify([act.active, act.jobs, act.log.slice(0, 50), store.state.imports]);
    if (s === sig) return;
    sig = s;
    const keep = scroller.scrollTop;
    renderSections(box, act, store.state.imports, { compact: false });
    scroller.scrollTop = keep;
  });
  store.on("activity-error", (e) => { if (visible && !store.state.activity) mount(box, errorState(e, () => store.kick())); });
  return {
    el: view, scroller,
    show() { visible = true; sig = ""; ups.start(); if (!release) release = store.watch(); if (store.state.activity) { sig = ""; store.kick(); } },
    hide() { visible = false; ups.stop(); if (release) { release(); release = null; } },
  };
}

// Supporting pane (large / xl): компактная очередь загрузок рядом с деталями
export function createSupport() {
  const box = el("div", { class: "support-content" });
  const head = el("header", { class: "support-head" }, el("h2", { text: "Загрузки" }),
    button({ label: "Все", variant: "text", trailing: "chevron_right", onClick: () => go("downloads") }));
  const view = el("div", { class: "support" }, head, box);
  let on = false, release = null, sig = "";
  store.on("activity", (act) => {
    if (!on) return;
    const s = JSON.stringify([act.active, act.jobs, store.state.imports]);
    if (s === sig) return;
    sig = s;
    renderSections(box, act, store.state.imports, { compact: true });
  });
  mount(box, loadingState());
  return {
    el: view,
    setVisible(v) {
      if (v === on) return;
      on = v;
      if (v) { sig = ""; release = store.watch(); } else if (release) { release(); release = null; }
    },
  };
}
