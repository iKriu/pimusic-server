// UI встроенного плеера: кнопка-переключатель с морфингом формы (cookie ↔ скруглённый квадрат, M3E),
// аудиореактивная волна (AnalyserNode / спокойная CSS-волна), мини-плеер «сейчас играет» (общий для волны и очереди),
// объявления для скринридера.
import { el, svg, icon, pic, setText } from "./dom.js";
import { iconButton, loadingIndicator, snackbar } from "./ui.js";
import { wakeAudio } from "./player.js";
import { radii, pathFromRadii } from "./shapes.js";
import { reducedMotion } from "./motion.js";
import { contentColor, forgetContent } from "./theme.js";
import { clearScheme } from "./color.js";
import { haptic } from "./tg.js";
import * as store from "./store.js";
import { volumeControl } from "./volume.js";
import {
  player, onPlayer, toggle, stop, retry, isOn, isWaveOn, isActive, current, togglePlay, next, hasNext, neighbor, stepTrack,
  getAnalyser, hasAnalyser, analyserSupported,
} from "./player.js";

export const LABEL_PLAY = "Слушать Мою Волну";
export const LABEL_PAUSE = "Пауза";
export const LABEL_RESUME = "Играть";

const busy = () => player.state === "loading" || player.state === "reconnecting" || (player.mode === "queue" && player.buffering && player.desired === "playing");
export const isBusy = busy;

// Тап «Слушать/Пауза» Волны: возвращает "empty", если играть нечего (медиатека пуста)
export function userToggle() {
  const me = store.state.me;
  if (!isWaveOn() && me && me.tracks === 0) { haptic("warning"); return "empty"; }
  haptic(isWaveOn() ? "light" : "medium");
  if (player.mode === "wave" && player.state === "error") retry(); else toggle(me && me.stream_url);
  return player.desired;
}

// Общий ▶/❚❚ (мини-плеер, «Сейчас играет»): волна — как userToggle, очередь — пауза/продолжить
export function anyToggle() {
  if (player.mode === "wave") return userToggle();
  haptic(isOn() ? "light" : "medium");
  togglePlay();
  return player.desired;
}

// «1:23» / «1:02:03»
export function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(s).padStart(2, "0");
}

// Строка состояния: «В эфире · Исполнитель», «Подключаюсь…», «Переподключаюсь… (2 из 5)»
export function statusText(p = player) {
  const n = p.now || {};
  switch (p.state) {
    case "loading": return "Подключаюсь к эфиру…";
    case "reconnecting": return `Связь прервалась — переподключаюсь… (${p.attempt} из 5)`;
    case "error": return (p.error && p.error.message) || "Поток недоступен";
    case "paused": return p.error && p.error.kind === "blocked" ? p.error.message : "Пауза — продолжите с живого эфира";
    case "playing": return n.title ? (n.artist || "Моя Волна") : "Подбираю следующий трек…";
    default: return "";
  }
}
export const titleText = (p = player) => (p.now && p.now.title) || "Моя Волна";

// ---------------------------------------------------------------- кнопка-переключатель с морфингом
// Пауза — волнистое «печенье», играет — ровный круг (выбор пользователя, M3E shape morph)
const COOKIE = radii("cookie9"), CIRCLE = radii("circle");
function spring(t) { // t в секундах; spatial-default (z≈0.8)
  const z = 0.72, w = Math.sqrt(380), wd = w * Math.sqrt(1 - z * z);
  return 1 - Math.exp(-z * w * t) * (Math.cos(wd * t) + (z * w / wd) * Math.sin(wd * t));
}

// kind: "wave" — кнопка «Слушать Мою Волну»; "any" — общий ▶/❚❚ («Сейчас играет»)
export function playToggle({ onEmpty, kind = "wave" } = {}) {
  const wave = kind === "wave";
  const LP = wave ? LABEL_PLAY : LABEL_RESUME;
  const path = svg("path", { class: "pt-shape" });
  const bg = svg("svg", { class: "pt-bg", viewBox: "0 0 100 100", "aria-hidden": "true" }, path);
  const ic = icon("play_arrow", { fill: true, class: "pt-icon" });
  const spin = el("span", { class: "pt-spin", "aria-hidden": "true" });
  const b = el("button", { type: "button", class: "play-toggle" + (wave ? "" : " play-toggle-any"), "aria-label": LP }, bg, ic, spin);
  const cur = Float32Array.from(COOKIE);
  let from = Float32Array.from(COOKIE), target = COOKIE, t0 = 0, raf = 0, rot = 0;
  const draw = () => path.setAttribute("d", pathFromRadii(cur, 50, 50, 50, rot - Math.PI / 2));
  draw();
  const morph = (to) => {
    if (to === target) return;
    target = to;
    cancelAnimationFrame(raf);
    if (reducedMotion()) { cur.set(to); rot = 0; draw(); return; }
    from = Float32Array.from(cur);
    t0 = performance.now();
    const step = (now) => {
      const t = (now - t0) / 1000;
      const f = spring(t);
      for (let i = 0; i < cur.length; i++) cur[i] = from[i] + (target[i] - from[i]) * f;
      rot = (target === CIRCLE ? 1 - f : f - 1) * 0.35; // лёгкий поворот во время морфинга
      draw();
      if (t < 0.9) raf = requestAnimationFrame(step); else { cur.set(target); rot = 0; draw(); }
    };
    raf = requestAnimationFrame(step);
  };
  const render = (what) => {
    if (what === "time") return;
    const on = wave ? isWaveOn() : isOn();
    b.setAttribute("aria-label", on ? LABEL_PAUSE : LP);
    b.title = on ? LABEL_PAUSE : LP;
    b.classList.toggle("is-on", on);
    b.dataset.state = player.state;
    setText(ic, on ? "pause" : "play_arrow");
    ic.dataset.fb = on ? "❚❚" : "▶";
    const isBusy = busy() && (!wave || player.mode === "wave");
    b.setAttribute("aria-busy", isBusy ? "true" : "false");
    if (isBusy && !spin.firstChild) { const li = loadingIndicator({ size: 56, label: "Подключение" }); li.setAttribute("aria-hidden", "true"); li.removeAttribute("role"); spin.appendChild(li); }
    if (!isBusy) spin.replaceChildren();
    morph(on ? CIRCLE : COOKIE);
  };
  b.addEventListener("click", () => { if (!wave) { anyToggle(); return; } if (userToggle() === "empty" && onEmpty) onEmpty(); });
  const off = onPlayer(render);
  render();
  b.destroy = off;
  return b;
}

// ---------------------------------------------------------------- аудиореактивная волна
export function waveViz() {
  const box = el("div", { class: "np-viz", "aria-hidden": "true" });
  const useCanvas = analyserSupported();
  if (!useCanvas) {
    box.classList.add("is-css");
    box.append(el("span", { class: "wv-line wv-1" }), el("span", { class: "wv-line wv-2" }), el("span", { class: "wv-line wv-3" }));
    const sync = () => box.classList.toggle("is-playing", player.mode === "wave" && player.state === "playing");
    onPlayer(sync);
    sync();
    return box;
  }
  const canvas = el("canvas", { class: "np-canvas" });
  box.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, dpr = 1, raf = 0, frameN = 0, colors = [];
  const lv = [0, 0, 0];
  let buf = null;
  const resize = () => {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = box.clientWidth; H = box.clientHeight;
    canvas.width = Math.max(1, Math.round(W * dpr)); canvas.height = Math.max(1, Math.round(H * dpr));
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    drawFrame(performance.now());
  };
  if (window.ResizeObserver) new ResizeObserver(resize).observe(box);
  const readColors = () => {
    const cs = getComputedStyle(box);
    colors = ["--md-sys-color-primary", "--md-sys-color-tertiary", "--md-sys-color-secondary"].map((v) => cs.getPropertyValue(v).trim() || "#f5a524");
  };
  // полосы заданы для 256 бинов (fftSize 512); у общего анализатора бинов больше — масштабируем
  const band = (a, b) => { const k = buf.length / 256; a = Math.round(a * k); b = Math.round(b * k); let s = 0; for (let i = a; i < b; i++) s += buf[i]; return s / ((b - a) * 255); };
  function drawFrame(now) {
    if (!W || !H) return;
    if (frameN++ % 60 === 0 || !colors.length) readColors();
    const an = player.mode === "wave" && player.state === "playing" ? getAnalyser() : null;
    let target = [0.05, 0.04, 0.03];
    if (an) {
      if (!buf || buf.length !== an.frequencyBinCount) buf = new Uint8Array(an.frequencyBinCount);
      an.getByteFrequencyData(buf);
      target = [band(1, 6), band(6, 28), band(28, 96)].map((x, i) => Math.max(0.04, Math.min(1, x * [1.1, 1.5, 2.4][i])));
    }
    for (let i = 0; i < 3; i++) lv[i] += (target[i] - lv[i]) * 0.25;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const mid = H / 2, t = now / 1000;
    const spec = [[lv[0], 1.6, 0.9, 5, 1], [lv[1], 2.6, -1.4, 3.5, 0.75], [lv[2], 4.2, 2.1, 2.5, 0.55]];
    spec.forEach(([amp, k, speed, width, alpha], i) => {
      ctx.beginPath();
      const A = amp * (H / 2 - 6);
      for (let x = 0; x <= W; x += 3) {
        const u = x / W;
        const env = Math.sin(Math.PI * u) ** 1.2; // к краям волна затухает
        const y = mid + A * env * Math.sin(u * Math.PI * 2 * k + t * speed + i) * (0.75 + 0.25 * Math.sin(t * 0.7 + u * 6));
        if (x) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      }
      ctx.strokeStyle = colors[i];
      ctx.globalAlpha = alpha;
      ctx.lineWidth = width;
      ctx.lineCap = "round"; ctx.lineJoin = "round";
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  }
  const loop = (now) => {
    raf = 0;
    if (player.mode !== "wave" || (player.state !== "playing" && player.state !== "loading" && player.state !== "reconnecting")) { drawFrame(now); return; }
    if (box.isConnected && box.offsetParent !== null) drawFrame(now);
    raf = requestAnimationFrame(loop);
  };
  let seen = false;
  const sync = (what) => {
    if (box.isConnected) seen = true; else if (seen) { offViz(); return; } // волна ушла со страницы — отписка
    if (what !== "time" && !raf) raf = requestAnimationFrame(loop);
  };
  const offViz = onPlayer(sync);
  sync();
  return box;
}

// ---------------------------------------------------------------- объявления скринридеру
let liveNode = null;
export function say(text) {
  if (!liveNode) {
    liveNode = el("div", { id: "player-live", class: "sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
    document.body.appendChild(liveNode);
  }
  liveNode.textContent = "";
  setTimeout(() => { liveNode.textContent = text; }, 80);
}
let skipN = 0, skipT = 0, skipFirst = "", blockedAt = -1e9;
export function installAnnouncer() {
  let prev = player.state;
  let lastQ = 0;
  onPlayer((what, p, data) => {
    if (what === "audio-blocked") {
      if (performance.now() - blockedAt > 10000) { blockedAt = performance.now(); snackbar("Звук приостановлен системой", { action: "Включить", onAction: () => wakeAudio() }); }
      return;
    }
    if (what === "skip" && data) {
      if (!skipN++) skipFirst = data.title;
      clearTimeout(skipT);
      skipT = setTimeout(() => { snackbar(skipN === 1 ? `Не удалось включить «${skipFirst}» — пропускаю` : `Пропущено недоступных треков: ${skipN}`); skipN = 0; }, 1500);
      return;
    }
    if (p.mode === "queue") {
      const t = current();
      if (what === "state" && p.state === "error" && prev !== "error") say((p.error && p.error.message) || "Не удалось включить трек");
      if (t && t.qid !== lastQ && (what === "track" || what === "state")) {
        const first = !lastQ;
        lastQ = t.qid;
        if (p.desired === "playing") say(`Сейчас играет: ${t.artist ? t.artist + " — " : ""}${t.title}`);
        else if (!first) say(`Выбран: ${t.artist ? t.artist + " — " : ""}${t.title}. На паузе`);
      }
      prev = p.state;
      return;
    }
    if (what === "track" && p.now && p.now.title && p.state === "playing") say(`Сейчас в эфире: ${p.now.artist ? p.now.artist + " — " : ""}${p.now.title}`);
    if (what === "state" && p.state !== prev) {
      if (p.state === "error") say(statusText(p));
      else if (p.state === "reconnecting" && prev === "playing") say("Связь прервалась, переподключаюсь");
      else if (p.state === "playing" && prev !== "playing") say(p.now && p.now.title ? `Сейчас в эфире: ${p.now.artist ? p.now.artist + " — " : ""}${p.now.title}` : "Моя Волна играет");
    }
    prev = p.state;
  });
}

// ---------------------------------------------------------------- мини-плеер
// Content color из обложки текущего трека; запасной вариант — базовая схема
export function coverColor(node, url, memo) {
  if (memo.url === url) return;
  memo.url = url;
  if (url) contentColor(node, url).then((ok) => { if (!ok && memo.url === url) reset(); });
  else reset();
  function reset() { forgetContent(node); clearScheme(node); node.classList.remove("has-content-color"); }
}

// Смахивание мини-плеера (очередь): влево — следующий, вправо — предыдущий. Порог — 30 % ширины (не больше 96px)
// или быстрый рывок; вертикальное движение отменяет жест (прокрутка). Под reduced motion — без анимации сдвига.
// Клавиатура и кнопки остаются прежними (жест — только ускоритель).
function swipeTracks(main, slide, onSwiped) {
  let g = null;
  const peek = (t, d) => {
    const p = el("span", { class: "mini-peek " + (d > 0 ? "is-next" : "is-prev"), "aria-hidden": "true" },
      el("span", { class: "mini-cover" }, pic(t.cover || null, { cls: "mini-pic", ph: "music_note", eager: true })),
      el("span", { class: "mini-text" }, el("span", { class: "mini-title", text: t.title || "" }), el("span", { class: "mini-sub-text", text: t.artist || "" })));
    slide.appendChild(p);
    return p;
  };
  const clearPeeks = () => { for (const p of slide.querySelectorAll(".mini-peek")) p.remove(); };
  // возврат — пружина spatial-fast; уход — effects-default (без перелёта)
  const setX = (x, anim) => {
    slide.style.transition = !anim || reducedMotion() ? "none" : anim === "out" ? "transform var(--md-motion-effects-default-dur) var(--md-motion-effects-default)"
      : "transform var(--md-motion-spatial-fast-dur) var(--md-motion-spatial-fast)";
    slide.style.transform = x ? `translateX(${x}px)` : "";
  };
  main.addEventListener("pointerdown", (ev) => {
    if (player.mode !== "queue" || ev.pointerType === "mouse" || g) return;
    g = { id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, t0: performance.now(), dx: 0, on: false, w: main.clientWidth || 300, next: neighbor(1), prev: neighbor(-1) };
  });
  main.addEventListener("pointermove", (ev) => {
    if (!g || ev.pointerId !== g.id) return;
    const dx = ev.clientX - g.x0, dy = ev.clientY - g.y0;
    if (!g.on) {
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { g = null; return; } // вертикаль — не наш жест
      if (Math.abs(dx) < 10 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
      g.on = true;
      try { main.setPointerCapture(ev.pointerId); } catch { /* */ }
      if (g.next) peek(g.next, 1);
      if (g.prev) peek(g.prev, -1);
    }
    g.dx = dx;
    const can = dx < 0 ? g.next : g.prev;
    setX(can ? dx : dx * 0.25, false); // соседа нет — «резиновый» упор
  });
  const end = (ev) => {
    if (!g || (ev && ev.pointerId !== g.id)) return;
    const s = g; g = null;
    if (!s.on) return;
    onSwiped();
    const dt = Math.max(1, performance.now() - s.t0), v = Math.abs(s.dx) / dt;
    const d = s.dx < 0 ? 1 : -1, target = d > 0 ? s.next : s.prev;
    const go = target && ev && ev.type === "pointerup" && (Math.abs(s.dx) > Math.min(96, s.w * 0.3) || v > 0.6);
    if (!go) { setX(0, "back"); setTimeout(clearPeeks, 360); return; }
    haptic("light");
    if (reducedMotion()) { clearPeeks(); setX(0, false); stepTrack(d); return; }
    // уезжает текущий, на его место — соседний (он уже виден сбоку), затем новое содержимое без анимации
    setX(-d * (s.w + 24), "out");
    setTimeout(() => { clearPeeks(); setX(0, false); stepTrack(d); }, 210);
  };
  main.addEventListener("pointerup", end);
  main.addEventListener("pointercancel", end);
}

// onOpen(mode): «wave» — к экрану Волны, «queue» — «Сейчас играет»
export function createMiniPlayer({ onOpen }) {
  const cover = el("span", { class: "mini-cover" });
  const title = el("span", { class: "mini-title" });
  const live = el("span", { class: "mini-live" }, el("span", { class: "live-dot", "aria-hidden": "true" }), el("span", { text: "В эфире" }));
  const sub = el("span", { class: "mini-sub-text" });
  // слайд (обложка + текст) двигается при смахивании; по бокам — «подглядывающий» соседний трек
  const slide = el("span", { class: "mini-slide" }, cover, el("span", { class: "mini-text" }, title, el("span", { class: "mini-sub" }, live, sub)));
  const main = el("button", { type: "button", class: "mini-main" }, slide);
  let swiped = false;
  main.addEventListener("click", (ev) => { if (swiped) { swiped = false; ev.preventDefault(); return; } haptic("light"); onOpen && onOpen(player.mode); });
  swipeTracks(main, slide, () => { swiped = true; });
  const tIcon = icon("pause", { fill: true });
  const tSpin = el("span", { class: "mini-spin", "aria-hidden": "true" });
  const tbtn = el("button", { type: "button", class: "ibtn ibtn-filled mini-toggle", "data-on": "1", "aria-label": LABEL_PAUSE, title: LABEL_PAUSE }, tIcon, tSpin);
  tbtn.addEventListener("click", () => anyToggle());
  const nextBtn = iconButton("skip_next", "Следующий трек", { cls: "mini-next", fill: true, onClick: () => { if (nextBtn.getAttribute("aria-disabled") === "true") return; haptic("light"); next(); } });
  const close = iconButton("close", "Остановить и закрыть плеер", { cls: "mini-close", onClick: () => { haptic("light"); stop(); } });
  // прогресс-линия трека очереди (у живого эфира прогресса нет)
  const bar = el("span", { class: "mini-progress", "aria-hidden": "true" }, el("span", { class: "mini-progress-fill" }));
  // громкость — только на широком экране с мышью (CSS), и для очереди, и для Волны
  const vol = volumeControl({ cls: "mini-vol" });
  const box = el("div", { class: "mini", role: "region", "aria-label": "Плеер", hidden: true }, main, vol, tbtn, nextBtn, close, bar);
  const memo = { url: undefined };
  let coverUrl;
  const progress = () => {
    const d = player.duration || 0;
    box.style.setProperty("--p", d ? Math.min(100, (player.time / d) * 100).toFixed(2) + "%" : "0%");
  };
  const render = (what) => {
    if (what === "time") { if (player.mode === "queue") progress(); return; }
    const q = player.mode === "queue";
    const t = q ? current() : null;
    const show = isActive() && (!q || !!t);
    box.hidden = !show;
    const app = document.getElementById("app");
    if (app) app.dataset.mini = show ? "on" : "off";
    measure();
    if (!show) return;
    box.dataset.state = player.state;
    box.dataset.mode = player.mode;
    nextBtn.hidden = !q;
    close.hidden = q;
    bar.hidden = !q;
    let c;
    if (q) {
      setText(title, t.title);
      const st = player.state === "error" ? (player.error && player.error.message) || "Не удалось включить"
        : player.error && player.error.kind === "blocked" ? player.error.message : t.artist;
      setText(sub, st);
      live.hidden = true;
      box.setAttribute("aria-label", "Плеер очереди");
      main.setAttribute("aria-label", `Открыть «Сейчас играет». ${t.title}${t.artist ? ", " + t.artist : ""}${player.desired === "playing" ? "" : ". Пауза"}`);
      nextBtn.setAttribute("aria-disabled", hasNext() ? "false" : "true");
      progress();
      c = t.cover || null;
    } else {
      const n = player.now || {};
      setText(title, titleText());
      setText(sub, statusText());
      live.hidden = player.state !== "playing";
      box.setAttribute("aria-label", "Плеер «Моя Волна»");
      main.setAttribute("aria-label", `Открыть Волну. ${titleText()}${n.title && n.artist ? ", " + n.artist : ""}. ${live.hidden ? statusText() : "В эфире"}`);
      c = n.cover || null;
    }
    const on = isOn();
    const err = player.state === "error";
    setText(tIcon, err ? "refresh" : on ? "pause" : "play_arrow");
    tIcon.dataset.fb = err ? "↻" : on ? "❚❚" : "▶";
    const lab = err ? (q ? "Повторить" : "Повторить подключение") : on ? LABEL_PAUSE : q ? LABEL_RESUME : LABEL_PLAY;
    tbtn.setAttribute("aria-label", lab); tbtn.title = lab;
    tbtn.dataset.on = on ? "1" : "0";
    const isBusy = busy();
    tbtn.setAttribute("aria-busy", isBusy ? "true" : "false");
    tbtn.classList.toggle("is-busy", isBusy);
    if (isBusy && !tSpin.firstChild) { const li = loadingIndicator({ size: 40, label: "Подключение" }); li.setAttribute("aria-hidden", "true"); li.removeAttribute("role"); tSpin.appendChild(li); }
    if (!isBusy) tSpin.replaceChildren();
    if (c !== coverUrl) { coverUrl = c; cover.replaceChildren(pic(c, { cls: "mini-pic", ph: q ? "music_note" : "waves", eager: true })); }
    coverColor(box, c, memo);
  };
  // высота мини-плеера от низа экрана → снекбары поднимаются над ним
  function measure() {
    const root = document.documentElement.style;
    if (box.hidden || !box.isConnected || !box.offsetParent) { root.removeProperty("--mini-h"); return; }
    requestAnimationFrame(() => {
      const r = box.getBoundingClientRect();
      if (r.height) root.setProperty("--mini-h", Math.max(0, Math.round(window.innerHeight - r.top)) + "px");
    });
  }
  window.addEventListener("resize", measure);
  onPlayer(render);
  render();
  box.measure = measure;
  box.render = render;
  return box;
}

export { hasAnalyser };
