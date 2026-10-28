// Текст песни в «Сейчас играет»: синхронный (LRC) — подсветка строки, автопрокрутка, тап по строке = перемотка;
// без меток — обычный текст; нет текста — пустое состояние. Ручная прокрутка ставит автопрокрутку на паузу на 4 с.
// Внизу — «Найти текст заново» (POST /api/tracks/{id}/lyrics/refetch: неверный или сбитый текст); если лучшего
// не нашлось — снекбар предлагает «Убрать текст» (clear, только для своей медиатеки).
import { el, mount, setText } from "./dom.js";
import { api } from "./api.js";
import { emptyState, errorState, loadingState, announce, button, setBusy, snackbar, dialog } from "./ui.js";
import { haptic } from "./tg.js";
import { player, onPlayer, current, seek } from "./player.js";
import { reducedMotion } from "./motion.js";

const cache = new Map(); // track id → ответ сервера

const STATUS = {
  updated: "Нашёлся новый текст",
  same: "Нашёлся тот же текст — он и остаётся",
  cleared: "Подходящего текста нет — прежний убран",
  not_found: "Лучшего текста не нашлось",
};
const ERR = { 409: "Текст этого трека уже ищется — подождите", 429: "Слишком часто: не больше 20 поисков в час", 504: "Сервисы текстов не ответили — попробуйте позже, текст не изменён" };

export function lyricsView() {
  const box = el("div", { class: "lyr", tabindex: "-1" });
  let id = null, lines = [], cur = -1, userScroll = 0, req = 0, busy = false, hasText = false;
  const refetchB = button({ label: "Найти текст заново", icon: "refresh", variant: "tonal", cls: "lyr-refetch", onClick: () => refetch(false) });
  // «Убрать текст»: постоянная кнопка (а не только действие снекбара) — после «лучшего не нашлось», своя медиатека
  const clearB = button({ label: "Убрать текст", variant: "text", cls: "lyr-clear", onClick: () => askClear() });
  clearB.hidden = true;
  const bar = el("div", { class: "lyr-actions" }, clearB, refetchB);
  async function askClear() {
    const ok = await dialog({ icon: "lyrics", headline: "Убрать текст песни?", body: "Сервер ещё раз поищет текст; если подходящего нет — текущий будет удалён у этого трека.",
      actions: [{ label: "Отмена", value: false }, { label: "Убрать", value: true, variant: "danger" }] });
    if (ok) refetch(true);
  }
  const wrap = el("div", { class: "lyr-wrap" }, box, bar);

  async function refetch(clear) {
    const t = current();
    if (!t || busy) return;
    const tid = t.id;
    busy = true;
    haptic("light");
    setBusy(refetchB, true, "Ищу текст…");
    announce("Ищу текст заново");
    let r;
    try {
      r = await api(`/tracks/${encodeURIComponent(tid)}/lyrics/refetch`, { method: "POST", body: clear ? { clear: true } : {} });
    } catch (e) {
      haptic("error");
      snackbar(ERR[e.status] || (e.status === 404 ? "Трек не найден или недоступен" : e.message || "Не удалось найти текст"), { error: true });
      return;
    } finally {
      busy = false;
      setBusy(refetchB, false, "Найти текст заново");
    }
    cache.set(tid, r);
    if (current() && current().id === tid) { id = tid; cur = -1; render(r); }
    haptic(r.status === "updated" || r.status === "cleared" ? "success" : "light");
    // лучшего нет, а прежний текст остался — предложить убрать его (только своя медиатека: файлы текста общие)
    const same = !!current() && current().id === tid;
    const canClear = same && r.status === "not_found" && !clear && hasText && t.owned !== false;
    clearB.hidden = !canClear;
    snackbar(STATUS[r.status] || "Готово", canClear ? { action: "Убрать текст", onAction: () => askClear() } : {});
  }
  box.addEventListener("scroll", () => { if (!box._auto) userScroll = performance.now(); box._auto = false; }, { passive: true });

  async function load() {
    const t = current();
    if (!t) { mount(box, null); return; }
    if (t.id === id) return;
    id = t.id; cur = -1; lines = [];
    // другой трек: прежний поиск (если идёт) к нему не относится — кнопки в исходное состояние
    clearB.hidden = true;
    if (busy) { busy = false; setBusy(refetchB, false, "Найти текст заново"); }
    const my = ++req;
    let r = cache.get(id);
    if (!r) {
      mount(box, loadingState("Загружаю текст…"));
      try { r = await api(`/lyrics/${encodeURIComponent(id)}`); } catch (e) {
        if (my !== req) return;
        if (e.status === 404) { bar.hidden = true; mount(box, emptyState({ shape: "pentagon", icon: "lyrics", title: "Текст недоступен", text: "Сервер пока не умеет отдавать тексты песен.", compact: true })); return; }
        mount(box, errorState(e, () => { id = null; load(); }));
        return;
      }
      if (my !== req) return;
      cache.set(id, r);
    }
    render(r);
  }

  function render(r) {
    hasText = !!(r && ((Array.isArray(r.synced) && r.synced.length) || r.plain));
    bar.hidden = !r; // старый сервер без текстов — искать нечем
    if (r && Array.isArray(r.synced) && r.synced.length) {
      lines = r.synced.map((l) => ({ t: Number(l.t) || 0, text: l.text || "" }));
      const ol = el("ol", { class: "lyr-lines", "aria-label": "Синхронный текст" }, lines.map((l, i) => {
        const b = el("button", { type: "button", class: "lyr-line", text: l.text.trim() || "♪" });
        b.addEventListener("click", () => { seek(l.t); announce(`Перемотка: ${l.text}`); });
        lines[i].el = b;
        return el("li", null, b);
      }));
      mount(box, [ol, el("p", { class: "lyr-src", text: r.source === "lrc" ? "Текст с метками времени · нажмите строку, чтобы перейти" : "" })]);
      tick(true);
      return;
    }
    if (r && r.plain) {
      lines = [];
      mount(box, el("div", { class: "lyr-plain" }, r.plain.split(/\n{2,}/).map((p) => el("p", { text: p }))));
      return;
    }
    mount(box, emptyState({ shape: "clover", icon: "lyrics", title: "Текста нет", text: "Положите .lrc или .txt рядом с файлом трека — текст появится здесь.", compact: true }));
  }

  function tick(force) {
    if (!lines.length) return;
    const t = player.time + 0.15;
    let i = -1;
    for (let k = 0; k < lines.length; k++) { if (lines[k].t <= t) i = k; else break; }
    if (i === cur && !force) return;
    if (cur >= 0 && lines[cur]) { lines[cur].el.classList.remove("is-now"); lines[cur].el.removeAttribute("aria-current"); }
    cur = i;
    if (i < 0) return;
    const elx = lines[i].el;
    elx.classList.add("is-now");
    elx.setAttribute("aria-current", "true");
    for (let k = 0; k < lines.length; k++) lines[k].el.classList.toggle("is-past", k < i);
    if (performance.now() - userScroll > 4000 && box.isConnected && box.offsetParent) {
      box._auto = true;
      const top = elx.offsetTop - box.clientHeight * 0.38;
      box.scrollTo({ top, behavior: reducedMotion() ? "auto" : "smooth" });
    }
  }

  const off = onPlayer((what) => {
    if (!box.isConnected && box._started) { off(); return; }
    if (what === "time") tick(false);
    else if (what === "track") load();
  });
  wrap.start = () => { box._started = true; load(); };
  return wrap;
}

export { setText };
