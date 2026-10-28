// Экран эквалайзера (M3E): режим синхронизации наверху, кривая АЧХ на canvas с перетаскиваемыми точками полос
// (x — частота, y — усиление, колесо/щипок — Q), спектр поверх, доступные слайдеры выбранной полосы, преамп,
// пресеты (встроенные + свои), A/B, лимитер. target — id устройства (по умолчанию — это устройство);
// для другого устройства — удалённая правка без звука здесь.
import { el, icon, mount, setText, clamp } from "./dom.js";
import { button, iconButton, connectedGroup, chip, slider, switchRow, dialog, snackbar, textField, openMenu, announce } from "./ui.js";
import { haptic } from "./tg.js";
import { reducedMotion } from "./motion.js";
import * as eq from "./eq.js";
import * as settings from "./settings.js";
import * as graph from "./audiograph.js";
import { deviceId } from "./device.js";
import { player, ensureAudioGraph } from "./player.js";

const FMIN = 20, FMAX = 20000, DBR = 15;
const lx = (f) => Math.log10(f / FMIN) / Math.log10(FMAX / FMIN); // 0..1
const fx = (p) => FMIN * Math.pow(FMAX / FMIN, p);
// точность: частота — 0,1 Гц до 100 Гц и 1 Гц выше (не грубее 0,5 %), усиление/преамп — 0,1 дБ, Q — 0,01
const nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const roundF = (f) => (f < 100 ? Math.round(f * 10) / 10 : Math.round(f));
const roundDb = (g) => Math.round(g * 10) / 10;
const numF = (f) => (f < 100 ? nf1 : nf0).format(roundF(f));
const numDb = (g) => `${g > 0 ? "+" : g < 0 ? "−" : ""}${nf1.format(Math.abs(g))}`;
const numQ = (q) => q.toFixed(2).replace(".", ",");
const fmtF = (f) => `${numF(f)} Гц`;
const fmtShort = (f) => (f >= 1000 ? `${Math.round(f / 100) / 10}k`.replace(".0k", "k") : String(Math.round(f)));
const fmtDb = (g) => `${numDb(g)} дБ`;
const fmtQ = (q) => `Q ${numQ(q)}`;
// ручной ввод: запятая и точка, минус любого вида, пробелы-разделители, единицы; частота — «1.5k», «1,5 кГц», «1500»
const clean = (t) => String(t).toLowerCase().replace(/[\s\u00a0\u202f]/g, "").replace(/[−–—]/g, "-").replace(",", ".");
const parseNum = (s) => (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(s) ? Number(s) : NaN);
const parseF = (t) => { let s = clean(t).replace(/(гц|hz)$/u, ""); let k = 1; if (/(k|к)$/u.test(s)) { k = 1000; s = s.slice(0, -1); } return parseNum(s) * k; };
const parseDb = (t) => parseNum(clean(t).replace(/(дб|db)$/u, ""));
const parseQ = (t) => parseNum(clean(t).replace(/^q/u, ""));

// офлайн-биквады — кривая считается и без живого графа (другое устройство, iOS без EQ)
let off = null;
function offline() {
  if (off !== null) return off;
  try {
    const OC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    const c = new OC(1, 128, 48000);
    off = { c, f: Array.from({ length: 10 }, () => c.createBiquadFilter()) };
  } catch { off = false; }
  return off;
}
const N = 220;
const FREQ = new Float32Array(N).map((_, i) => fx(i / (N - 1)));
let respKey = "", respVal = null;
function response(prof) {
  const key = JSON.stringify([prof.preamp, prof.bands]);
  if (key === respKey && respVal) return respVal;
  respKey = key;
  return (respVal = computeResponse(prof));
}
function computeResponse(prof) {
  const o = offline();
  const sum = new Float32Array(N).fill(prof.preamp || 0);
  if (!o) { // запасной путь: колокол по каждой полосе
    for (let i = 0; i < N; i++) for (const b of prof.bands) { const d = Math.log2(FREQ[i] / b.f) * b.q; sum[i] += b.g * Math.exp(-d * d * 1.4); }
    return sum;
  }
  const mag = new Float32Array(N), ph = new Float32Array(N);
  prof.bands.forEach((b, k) => {
    const f = o.f[k];
    f.type = b.type || "peaking";
    f.frequency.value = b.f; f.gain.value = b.g; f.Q.value = b.q;
    f.getFrequencyResponse(FREQ, mag, ph);
    for (let i = 0; i < N; i++) sum[i] += 20 * Math.log10(Math.max(1e-6, mag[i]));
  });
  return sum;
}

export function eqView({ target = deviceId(), onClose } = {}) {
  const own = target === deviceId();
  const root = el("div", { class: "eqv" + (own ? "" : " is-remote") });
  let sel = 3; // выбранная полоса
  let draft = null; // профиль во время перетаскивания (без записи в настройки)
  const prof = () => draft || eq.effective(target);

  // --- режим (наверху)
  const dev = () => eq.device(target) || {};
  const modeGroup = connectedGroup([
    { key: "sync", label: "Синхронизировать", icon: "sync" },
    { key: "local", label: own ? "Только это устройство" : "Только это", icon: "devices" },
  ], dev().mode || "sync", (k) => { haptic("select"); eq.setMode(k, target); announce(k === "sync" ? "Синхронизация включена: профиль общий для устройств" : "Свой профиль этого устройства"); }, { label: "Режим эквалайзера" });
  const modeNote = el("p", { class: "eqv-note" });
  const enable = switchRow({ label: "Эквалайзер", checked: !!dev().enabled, onChange: (v) => {
    if (own && v) { eq.setEnabled(v, target); ensureAudioGraph(true); } // в жесте: на iOS граф создаётся только сейчас
    eq.setEnabled(v, target);
    if (own && v && !graph.hasGraph() && player.state !== "idle") snackbar("Эквалайзер включится со следующего трека или после паузы");
  } });
  // A/B: временный обход (только своё устройство)
  const ab = own ? connectedGroup([{ key: "a", label: "A · с EQ" }, { key: "b", label: "B · оригинал" }], "a",
    (k) => { haptic("select"); eq.setBypass(k === "b"); renderCurve(); }, { label: "Сравнение A/B" }) : null;
  const head = el("div", { class: "eqv-head" },
    own ? null : el("p", { class: "eqv-remote" }, icon("devices"), el("span", { text: `Удалённая настройка: ${dev().name || "устройство"}` })),
    modeGroup, modeNote, el("div", { class: "eqv-row" }, enable, ab));

  // --- кривая
  const canvas = el("canvas", { class: "eqv-canvas", role: "img" });
  const tip = el("span", { class: "eqv-tip", "aria-hidden": "true", hidden: true });
  const curveBox = el("div", { class: "eqv-curve" }, canvas, tip);
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, dpr = 1, colors = {};
  const X = (f) => 12 + lx(f) * (W - 24);
  const Y = (db) => H / 2 - (db / DBR) * (H / 2 - 14);
  const toF = (x) => clamp(fx((x - 12) / (W - 24)), FMIN, FMAX);
  const toDb = (y) => clamp(roundDb(((H / 2 - y) / (H / 2 - 14)) * DBR), -12, 12);
  const readColors = () => {
    const cs = getComputedStyle(root);
    const v = (n) => cs.getPropertyValue(n).trim();
    colors = { p: v("--md-sys-color-primary"), op: v("--md-sys-color-on-primary"), pc: v("--md-sys-color-primary-container"), t: v("--md-sys-color-tertiary"),
      grid: v("--md-sys-color-outline-variant"), txt: v("--md-sys-color-on-surface-variant"), sec: v("--md-sys-color-secondary") };
  };
  let colorsAt = 0;
  function renderCurve() {
    if (!W) return;
    if (!colors.p || performance.now() - colorsAt > 2000) { readColors(); colorsAt = performance.now(); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    drawSpectrum();
    // сетка: 0, ±6, ±12 дБ; частоты 31…16k
    ctx.lineWidth = 1;
    ctx.font = "500 11px Roboto Flex, Roboto, system-ui, sans-serif";
    ctx.fillStyle = colors.txt;
    for (const db of [-12, -6, 0, 6, 12]) {
      ctx.strokeStyle = colors.grid;
      ctx.globalAlpha = db === 0 ? 1 : 0.55;
      ctx.beginPath(); ctx.moveTo(12, Y(db)); ctx.lineTo(W - 12, Y(db)); ctx.stroke();
      ctx.globalAlpha = 1;
      if (db) ctx.fillText((db > 0 ? "+" : "−") + Math.abs(db), 14, Y(db) - 3);
    }
    ctx.textAlign = "center";
    for (const f of [31, 125, 500, 2000, 8000]) ctx.fillText(fmtShort(f), X(f), H - 3);
    ctx.textAlign = "left";
    const p = prof();
    const off = eq.isBypassed() && own;
    const r = response(p);
    // заливка под кривой и сама кривая
    ctx.beginPath();
    for (let i = 0; i < N; i++) { const x = X(FREQ[i]), y = Y(clamp(r[i], -DBR, DBR)); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }
    ctx.strokeStyle = off ? colors.txt : colors.p;
    ctx.lineWidth = 3; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.globalAlpha = off || !dev().enabled ? 0.45 : 1;
    ctx.stroke();
    ctx.lineTo(X(FREQ[N - 1]), Y(0)); ctx.lineTo(X(FREQ[0]), Y(0)); ctx.closePath();
    ctx.fillStyle = colors.p; ctx.globalAlpha = (off || !dev().enabled ? 0.06 : 0.16);
    ctx.fill();
    ctx.globalAlpha = 1;
    // точки полос: выбранная — крупнее, с кольцом
    p.bands.forEach((b, i) => {
      const x = X(b.f), y = Y(b.g), on = i === sel;
      ctx.beginPath(); ctx.arc(x, y, on ? 11 : 7, 0, Math.PI * 2);
      ctx.fillStyle = on ? colors.p : colors.pc; ctx.fill();
      if (on) { ctx.lineWidth = 3; ctx.strokeStyle = colors.op; ctx.stroke(); }
      ctx.fillStyle = on ? colors.op : colors.p;
      ctx.font = "700 10px Roboto Flex, Roboto, system-ui, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(String(i + 1), x, y + 0.5);
      ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
    });
    canvas.setAttribute("aria-label", `Кривая эквалайзера: ${eq.presetName(p.active)}. ` + p.bands.map((b) => `${fmtShort(b.f)} ${fmtDb(b.g)}`).join(", "));
  }
  // спектр позади кривой (только своё устройство, когда играет, без reduced motion)
  let spec = null;
  function drawSpectrum() {
    const an = own && dev().spectrum !== false && !reducedMotion() ? graph.getAnalyser() : null;
    if (!an || player.state !== "playing") return;
    if (!spec || spec.length !== an.frequencyBinCount) spec = new Uint8Array(an.frequencyBinCount);
    an.getByteFrequencyData(spec);
    const sr = graph.sampleRate(), n = spec.length, cols = 64;
    ctx.fillStyle = colors.t;
    for (let c = 0; c < cols; c++) {
      const f0 = fx(c / cols), f1 = fx((c + 1) / cols);
      const i0 = Math.max(1, Math.floor((f0 / (sr / 2)) * n)), i1 = Math.max(i0 + 1, Math.ceil((f1 / (sr / 2)) * n));
      let m = 0; for (let i = i0; i < Math.min(n, i1); i++) m = Math.max(m, spec[i]);
      const h = (m / 255) * (H - 28);
      ctx.globalAlpha = 0.18;
      ctx.fillRect(X(f0) + 1, H - 16 - h, Math.max(1, X(f1) - X(f0) - 2), h);
    }
    ctx.globalAlpha = 1;
  }
  let raf = 0;
  let lastFrame = 0;
  const visible = () => root.isConnected && !document.hidden && root.offsetParent !== null;
  const loop = (now) => {
    raf = 0;
    if (!visible()) return; // вернётся по visibilitychange / kick
    if (now - lastFrame >= 33) { lastFrame = now; renderCurve(); }
    if (player.state === "playing" && own && dev().spectrum !== false && !reducedMotion()) raf = requestAnimationFrame(loop);
  };
  const onVis = () => { if (!root.isConnected) { document.removeEventListener("visibilitychange", onVis); return; } if (!document.hidden) kick(); };
  document.addEventListener("visibilitychange", onVis);
  const kick = () => { if (!raf) raf = requestAnimationFrame(loop); };
  const resize = () => {
    colorsAt = 0;
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = curveBox.clientWidth; H = curveBox.clientHeight;
    canvas.width = Math.max(1, Math.round(W * dpr)); canvas.height = Math.max(1, Math.round(H * dpr));
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    renderCurve();
  };
  if (window.ResizeObserver) new ResizeObserver(resize).observe(curveBox);

  // перетаскивание точек; колесо и щипок — Q
  const pts = new Map();
  let drag = null;
  const nearest = (x, y) => {
    const p = prof();
    let best = -1, bd = 1e9;
    p.bands.forEach((b, i) => { const d = Math.hypot(X(b.f) - x, (Y(b.g) - y) * 0.6); if (d < bd) { bd = d; best = i; } });
    return best;
  };
  const local = (ev) => { const r = canvas.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
  const showTip = (b) => { tip.textContent = `${fmtF(b.f)} · ${fmtDb(b.g)} · ${fmtQ(b.q)}`; tip.hidden = false; tip.style.left = clamp(X(b.f), 70, W - 70) + "px"; };
  canvas.addEventListener("pointerdown", (ev) => {
    pts.set(ev.pointerId, local(ev));
    try { canvas.setPointerCapture(ev.pointerId); } catch { /* */ }
    if (pts.size === 2) { const [a, b] = [...pts.values()]; drag = { pinch: Math.hypot(a[0] - b[0], a[1] - b[1]), q0: prof().bands[sel].q }; return; }
    const [x, y] = local(ev);
    sel = nearest(x, y);
    draft = structuredCloneSafe(eq.effective(target));
    drag = { i: sel };
    haptic("select");
    syncBand();
    showTip(draft.bands[sel]);
    renderCurve();
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!pts.has(ev.pointerId) || !drag) return;
    pts.set(ev.pointerId, local(ev));
    if (drag.pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const q = clamp(drag.q0 * (drag.pinch / Math.max(10, Math.hypot(a[0] - b[0], a[1] - b[1]))), 0.3, 8);
      if (!draft) draft = structuredCloneSafe(eq.effective(target));
      draft.bands[sel] = { ...draft.bands[sel], q: Math.round(q * 100) / 100 };
    } else if (drag.i !== undefined) {
      const [x, y] = local(ev);
      draft.bands[drag.i] = { ...draft.bands[drag.i], f: roundF(toF(x)), g: toDb(y) };
    }
    previewDraft();
    showTip(draft.bands[sel]);
    renderCurve();
  });
  const up = (ev) => {
    pts.delete(ev.pointerId);
    if (pts.size || !drag) return;
    drag = null;
    tip.hidden = true;
    if (draft) { const b = draft.bands[sel]; draft = null; eq.setBand(sel, { f: b.f, g: b.g, q: b.q }, target); }
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const b = eq.effective(target).bands[sel];
    eq.setBand(sel, { q: clamp(Math.round(b.q * (ev.deltaY < 0 ? 1.12 : 1 / 1.12) * 100) / 100, 0.3, 8) }, target);
  }, { passive: false });
  const previewDraft = () => { if (own) graph.applyEq({ enabled: dev().enabled && !eq.isBypassed(), preamp: draft.preamp, bands: draft.bands, limiter: dev().limiter !== false }); };

  // --- выбранная полоса: чипы 1…10 и слайдеры (доступная альтернатива перетаскиванию)
  const chips = el("div", { class: "eqv-bands", role: "radiogroup", "aria-label": "Полоса" });
  const chipEls = [];
  for (let i = 0; i < 10; i++) {
    const c = el("button", { type: "button", class: "eqv-band", role: "radio", "aria-checked": "false" });
    c.addEventListener("click", () => { sel = i; haptic("select"); syncBand(); renderCurve(); });
    chipEls.push(c);
    chips.appendChild(c);
  }
  chips.addEventListener("keydown", (ev) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(ev.key)) return;
    ev.preventDefault();
    sel = clamp(sel + (ev.key === "ArrowLeft" || ev.key === "ArrowUp" ? -1 : 1), 0, 9);
    syncBand(); renderCurve(); chipEls[sel].focus();
  });
  const commit = (patch) => { draft = null; eq.setBand(sel, patch, target); };
  // стрелки — мелкий шаг (частота 1 %, усиление 0,1 дБ, Q 2 %), Shift/PageUp/PageDown — крупный (⅓ октавы, 1 дБ, 25 %)
  const dbKeys = (v, dir, big) => v + dir * (big ? 1 : 0.1);
  const dbInput = { parse: parseDb, fmt: numDb, unit: "дБ", unitName: "в децибелах", example: "−3,5" };
  const sFreq = slider({ label: "Частота", min: 20, max: 20000, step: 0.1, value: 1000, format: fmtF, toPos: lx, fromPos: fx, round: roundF,
    keyStep: (v, dir, big) => v * Math.pow(big ? Math.cbrt(2) : 1.01, dir),
    input: { parse: parseF, fmt: numF, unit: "Гц", unitName: "в герцах", example: "1.5k или 1500" },
    onInput: (v) => live({ f: v }), onChange: (v) => commit({ f: v }) });
  const sGain = slider({ label: "Усиление", min: -12, max: 12, step: 0.1, value: 0, format: fmtDb, ticks: 1, keyStep: dbKeys, input: dbInput,
    onInput: (v) => live({ g: v }), onChange: (v) => commit({ g: v }) });
  const qPos = (q) => Math.log(q / 0.3) / Math.log(8 / 0.3), qVal = (p) => 0.3 * Math.pow(8 / 0.3, p);
  const sQ = slider({ label: "Добротность (Q)", min: 0.3, max: 8, step: 0.01, value: 1, format: fmtQ, toPos: qPos, fromPos: qVal,
    keyStep: (v, dir, big) => v * Math.pow(big ? 1.25 : 1.02, dir), input: { parse: parseQ, fmt: numQ, example: "1,4" },
    onInput: (v) => live({ q: v }), onChange: (v) => commit({ q: v }) });
  const typeBox = el("div", { class: "eqv-type" });
  const bandTitle = el("h3", { class: "eqv-h3" });
  const bandCard = el("section", { class: "eqv-card", "aria-label": "Выбранная полоса" }, el("div", { class: "eqv-card-head" }, bandTitle, typeBox), sFreq, sGain, sQ);
  function live(patch) {
    if (!draft) draft = structuredCloneSafe(eq.effective(target));
    draft.bands[sel] = { ...draft.bands[sel], ...patch };
    previewDraft();
    renderCurve();
    clearTimeout(liveT);
    liveT = setTimeout(() => { draft = null; }, 600); // клавиатура: onChange уже записал
  }
  let liveT = 0;
  function syncBand() {
    const p = prof();
    const b = p.bands[sel];
    chipEls.forEach((c, i) => { c.setAttribute("aria-checked", i === sel ? "true" : "false"); c.tabIndex = i === sel ? 0 : -1; setText(c, fmtShort(p.bands[i].f)); c.setAttribute("aria-label", `Полоса ${i + 1}: ${fmtF(p.bands[i].f)}, ${fmtDb(p.bands[i].g)}`); });
    setText(bandTitle, `Полоса ${sel + 1} · ${fmtF(b.f)}`);
    sFreq.set(b.f); sGain.set(b.g); sQ.set(b.q);
    // крайние полосы можно сделать полкой
    if (sel === 0 || sel === 9) {
      const shelf = sel === 0 ? "lowshelf" : "highshelf";
      mount(typeBox, connectedGroup([{ key: "peaking", label: "Пик" }, { key: shelf, label: "Полка" }], b.type === shelf ? shelf : "peaking",
        (k) => { haptic("select"); commit({ type: k }); }, { label: "Тип полосы" }));
    } else typeBox.replaceChildren();
  }

  // --- преамп, пресеты, опции
  const sPre = slider({ label: "Преамп", min: -12, max: 6, step: 0.1, value: 0, format: fmtDb, ticks: 1, keyStep: dbKeys, input: dbInput,
    onInput: (v) => { if (!draft) draft = structuredCloneSafe(eq.effective(target)); draft.preamp = v; previewDraft(); renderCurve(); clearTimeout(liveT); liveT = setTimeout(() => { draft = null; }, 600); },
    onChange: (v) => { draft = null; eq.setPreamp(v, target); } });
  const presetRow = el("div", { class: "eqv-presets", role: "radiogroup", "aria-label": "Пресеты" });
  const presetActions = el("div", { class: "eqv-pactions" });
  const lim = switchRow({ label: "Лимитер", supporting: "Не даёт звуку «трещать», когда полосы подняты", checked: dev().limiter !== false, onChange: (v) => eq.setOption("limiter", v, target) });
  const spc = switchRow({ label: "Спектр поверх кривой", supporting: reducedMotion() ? "Выключен: в системе включено «уменьшение движения»" : "Анимация того, что сейчас звучит", checked: dev().spectrum !== false, onChange: (v) => { eq.setOption("spectrum", v, target); kick(); } });
  const iosNote = own && graph.iOS ? el("p", { class: "eqv-warn" }, icon("info"), el("span", { text: "На iPhone и iPad эквалайзер работает через Web Audio: в фоне система может прерывать воспроизведение. Включайте, если слушаете с открытым приложением." })) : null;

  function renderPresets() {
    const p = prof();
    const custom = eq.customPresets();
    mount(presetRow, eq.allPresets().map((x) => {
      const c = chip({ label: x.name, variant: "filter", selected: p.active === x.id, onClick: () => { haptic("select"); eq.applyPreset(x.id, target); announce(`Пресет «${x.name}»`); } });
      c.setAttribute("role", "radio"); c.removeAttribute("aria-pressed"); c.setAttribute("aria-checked", p.active === x.id ? "true" : "false");
      if (!x.builtin) c.classList.add("is-custom");
      return c;
    }).concat(p.active === "custom" ? [chip({ label: "Свой (не сохранён)", variant: "filter", selected: true })] : []));
    const isCustom = custom.find((x) => x.id === p.active);
    mount(presetActions, [
      button({ label: "Сохранить как…", icon: "bookmark_add", variant: "tonal", onClick: saveAs }),
      isCustom ? button({ label: "Переименовать", icon: "edit_note", variant: "text", onClick: () => renamePreset(isCustom) }) : null,
      isCustom ? button({ label: "Удалить", icon: "delete", variant: "text", cls: "btn-danger-text", onClick: () => removePreset(isCustom) }) : null,
      button({ label: "Сбросить", icon: "restart_alt", variant: "text", onClick: () => { haptic("light"); eq.reset(target); announce("Эквалайзер сброшен: ровно"); } }),
    ]);
  }
  async function saveAs() {
    const f = textField({ label: "Название пресета", maxlength: 40 });
    let made = null;
    const ok = await dialog({ icon: "bookmark_add", headline: "Сохранить пресет", body: "Пресет появится на всех ваших устройствах.", content: el("div", { class: "dlg-form" }, f),
      validate: () => (f.input.value.trim() ? "" : "Введите название"),
      onConfirm: async () => { made = eq.savePreset(f.input.value, target); return null; },
      actions: [{ label: "Отмена", value: false }, { label: "Сохранить", value: true }] });
    if (ok && made) { haptic("success"); snackbar(`Пресет «${made.name}» сохранён`); }
  }
  async function renamePreset(x) {
    const f = textField({ label: "Название пресета", value: x.name, maxlength: 40 });
    const ok = await dialog({ icon: "edit_note", headline: "Переименовать пресет", content: el("div", { class: "dlg-form" }, f),
      validate: () => (f.input.value.trim() ? "" : "Введите название"), actions: [{ label: "Отмена", value: false }, { label: "Сохранить", value: true }] });
    if (ok) eq.renamePreset(x.id, f.input.value);
  }
  async function removePreset(x) {
    const ok = await dialog({ icon: "delete", headline: `Удалить «${x.name}»?`, body: "Пресет пропадёт на всех устройствах. Текущий звук не изменится.",
      actions: [{ label: "Отмена", value: false }, { label: "Удалить", value: true, variant: "danger" }] });
    if (ok) { eq.deletePreset(x.id); eq.editProfile({ active: "custom" }, target); }
  }

  function renderAll() {
    const d = dev();
    modeGroup.set(d.mode || "sync");
    setText(modeNote, d.mode === "local" ? "Свой пресет и полосы: не меняются от других устройств." : "Общий профиль: изменения здесь видны на всех устройствах с синхронизацией.");
    enable.checked = !!d.enabled;
    lim.checked = d.limiter !== false;
    spc.checked = d.spectrum !== false;
    if (!draft) { sPre.set(prof().preamp); syncBand(); }
    renderPresets();
    root.classList.toggle("is-off", !d.enabled);
    renderCurve();
  }

  root.append(...[head, iosNote, curveBox, chips, bandCard].filter(Boolean),
    el("section", { class: "eqv-card" }, sPre),
    el("h3", { class: "eqv-h3 eqv-sec", text: "Пресеты" }), presetRow, presetActions,
    el("section", { class: "eqv-card eqv-opts" }, lim, spc));
  const offS = settings.onSetting((key) => { if (!root.isConnected) { offS(); return; } if (key.startsWith("eq.")) renderAll(); });
  import("./player.js").then((m) => { const offP = m.onPlayer((w) => { if (!root.isConnected) { offP(); return; } if (w === "state") kick(); }); });
  requestAnimationFrame(() => { resize(); renderAll(); kick(); });
  root.refresh = () => { resize(); renderAll(); kick(); };
  return root;
}

function structuredCloneSafe(o) { return JSON.parse(JSON.stringify(o)); }

export { iconButton, openMenu };
