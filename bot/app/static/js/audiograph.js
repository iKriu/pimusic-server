// Общий звуковой граф Web Audio:
//   <audio> ×2 (второй — для плавного перехода) → gain дорожки (нормализация, затухание)
//   → шина → [преамп → 10 BiquadFilter] → out (запас по громкости) → [лимитер] → master (таймер сна) → анализатор
//   → громкость пользователя (ползунок на десктопе) → выход. Анализатор — до громкости: визуализация не «тухнет» от ползунка.
// Эквалайзер и лимитер выключаются перекоммутацией (bypass), а не обнулением полос; перекоммутация — только когда
// меняется вкл/выкл, иначе плавно меняются параметры.
// AudioContext создаётся ТОЛЬКО в жесте пользователя: элемент, подключённый к «спящему» контексту, звучит тишиной.
// iOS WebView глушит AudioContext в фоне — там граф создаётся, только если пользователь сам включил эквалайзер.
export const iOS = /iP(hone|od|ad)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const AC = window.AudioContext || window.webkitAudioContext;

let ctx = null, failed = false;
let bus = null, preamp = null, bands = [], limiter = null, master = null, analyser = null, out = null, user = null;
let userVol = 1; // громкость пользователя (0…1, уже с кривой) — отдельный множитель поверх нормализации и переходов
const plain = new Map(); // <audio> без графа → громкость дорожки (её умножаем на громкость пользователя)
const inputs = new Map(); // <audio> → { src, gain }
let eq = null; // текущее состояние эквалайзера (eq.js)
let wantEq = false; // iOS: пользователь явно включил эквалайзер
let normDb = 0; // подъём громкости текущего трека нормализацией (дБ) — для запаса без лимитера
let wired = ""; // текущая коммутация (чтобы не дёргать граф без нужды)
const stateSubs = new Set();

export const hasGraph = () => !!ctx;
export const graphFailed = () => failed;
export const context = () => ctx;
export const ctxState = () => (ctx ? ctx.state : "none");
export const running = () => !!ctx && ctx.state === "running";
export function onGraphState(fn) { stateSubs.add(fn); return () => stateSubs.delete(fn); }
// Нужен ли граф: везде, кроме iOS; на iOS — только с включённым вручную эквалайзером
export const graphWanted = () => !!AC && !failed && (!iOS || wantEq);
export function allowOnIOS(on) { wantEq = !!on; }

// Есть ли сейчас пользовательская активация (жест). Без API (старые браузеры) — доверяем вызывающему.
const inGesture = (gesture) => gesture || !navigator.userActivation || navigator.userActivation.isActive;

// Создать граф (в жесте) и подключить переданные элементы. Без жеста — ничего не создаём: элемент играет напрямую.
export function ensureGraph(elements = [], { gesture = false } = {}) {
  if (!graphWanted()) return false;
  try {
    if (!ctx) {
      if (!inGesture(gesture)) return false;
      ctx = new AC({ latencyHint: "playback" });
      bus = ctx.createGain();
      preamp = ctx.createGain();
      bands = Array.from({ length: 10 }, () => ctx.createBiquadFilter());
      for (let i = 0; i < bands.length - 1; i++) bands[i].connect(bands[i + 1]);
      preamp.connect(bands[0]);
      limiter = ctx.createDynamicsCompressor();
      // мягкий «потолок»: срабатывает только у самой границы клиппинга
      limiter.threshold.value = -1.5; limiter.knee.value = 2; limiter.ratio.value = 20;
      limiter.attack.value = 0.003; limiter.release.value = 0.12;
      out = ctx.createGain(); // точка после EQ (сюда же приходит обход) — здесь и запас громкости
      master = ctx.createGain();
      analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0.8;
      user = ctx.createGain();
      user.gain.value = userVol;
      master.connect(analyser);
      analyser.connect(user);
      user.connect(ctx.destination);
      ctx.onstatechange = () => { for (const fn of stateSubs) { try { fn(ctx.state); } catch (e) { console.error(e); } } };
      if (eq) applyEq(eq); else rewire();
    }
    for (const el of elements) attach(el);
    resume();
    return true;
  } catch (e) {
    failed = true;
    console.warn("Web Audio недоступен:", e && e.message);
    return false;
  }
}

// Будить контекст: suspended (автозапуск, фон) и interrupted (iOS: звонок, Siri)
export function resume() {
  if (ctx && ctx.state !== "running" && ctx.state !== "closed") ctx.resume().catch(() => {});
}

export function attach(el) {
  if (!ctx || !el || inputs.has(el)) return inputs.get(el) || null;
  try {
    const src = ctx.createMediaElementSource(el);
    const gain = ctx.createGain();
    src.connect(gain);
    gain.connect(bus);
    const n = { src, gain };
    inputs.set(el, n);
    plain.delete(el);
    el.volume = 1;
    return n;
  } catch (e) {
    console.warn("MediaElementSource:", e && e.message);
    return null;
  }
}

const eqOn = () => !!(eq && eq.enabled);
// Лимитер нужен, только если громкость может подняться: EQ включён или нормализация поднимает трек
const limOn = () => (!eq || eq.limiter !== false) && (eqOn() || normDb > 0);

// Перекоммутация: шина → (EQ?) → out → (лимитер?) → master — только если что-то поменялось
function rewire() {
  if (!ctx) return;
  const want = `${eqOn() ? 1 : 0}${limOn() ? 1 : 0}`;
  if (want === wired) return;
  wired = want;
  for (const n of [bus, bands[bands.length - 1], limiter, out]) { try { n.disconnect(); } catch { /* */ } }
  bus.connect(eqOn() ? preamp : out);
  if (eqOn()) bands[bands.length - 1].connect(out);
  if (limOn()) { out.connect(limiter); limiter.connect(master); } else out.connect(master);
}

const dbToGain = (db) => Math.pow(10, db / 20);
const TYPES = { peaking: "peaking", lowshelf: "lowshelf", highshelf: "highshelf" };

// Запас громкости без лимитера: подъём полос + подъём нормализацией не должны клиповать
function headroomDb() {
  if (!eq || eq.limiter !== false) return 0; // лимитер включён пользователем — он и защищает
  const bandBoost = eqOn() ? Math.max(0, ...eq.bands.map((b) => b.g)) + Math.min(0, eq.preamp || 0) : 0;
  return -Math.max(0, bandBoost + normDb);
}

// Применить состояние эквалайзера (вызывает eq.js). Плавно — без щелчков.
export function applyEq(state) {
  eq = state;
  if (!ctx) return;
  const t = ctx.currentTime, tc = 0.015;
  preamp.gain.setTargetAtTime(dbToGain(state.preamp || 0), t, tc);
  state.bands.forEach((b, i) => {
    const f = bands[i];
    if (!f) return;
    const type = TYPES[b.type] || "peaking";
    if (f.type !== type) f.type = type;
    f.frequency.setTargetAtTime(b.f, t, tc);
    f.gain.setTargetAtTime(b.g, t, tc);
    f.Q.setTargetAtTime(b.q, t, tc);
  });
  rewire();
  out.gain.setTargetAtTime(dbToGain(headroomDb()), t, tc);
}

// Подъём нормализацией текущего трека (дБ, ≥ 0) — для лимитера/запаса
export function setNormBoost(db) {
  normDb = Math.max(0, db || 0);
  if (!ctx) return;
  rewire();
  out.gain.setTargetAtTime(dbToGain(headroomDb()), ctx.currentTime, 0.015);
}

// Громкость дорожки (нормализация/кроссфейд). Без графа — через audio.volume (только ослабление; на iOS volume
// только для чтения — там это просто ничего не делает).
export function setTrackGain(el, value, ramp = 0) {
  const n = inputs.get(el);
  if (!n) {
    if (!iOS && !ramp) { plain.set(el, Math.max(0, Math.min(1, value))); try { el.volume = plain.get(el) * userVol; } catch { /* */ } }
    return;
  }
  const g = n.gain.gain, t = ctx.currentTime;
  g.cancelScheduledValues(t);
  g.setValueAtTime(g.value, t);
  if (ramp > 0) g.linearRampToValueAtTime(value, t + ramp); else g.setValueAtTime(value, t);
}

// Общая громкость (таймер сна). Без графа затухания нет (плавность через volume на iOS невозможна) — только сброс.
export function setMaster(value, ramp = 0) {
  if (!ctx) return false;
  const g = master.gain, t = ctx.currentTime;
  g.cancelScheduledValues(t);
  g.setValueAtTime(g.value, t);
  if (ramp > 0) g.linearRampToValueAtTime(value, t + ramp); else g.setValueAtTime(value, t);
  return true;
}

// Громкость пользователя (0…1). В графе — отдельный узел после анализатора (плавно, без щелчков);
// без графа — audio.volume = громкость дорожки × громкость пользователя.
export function setUserVolume(value) {
  userVol = Math.max(0, Math.min(1, Number(value) || 0));
  for (const [el, g] of plain) { try { el.volume = g * userVol; } catch { /* */ } }
  if (ctx && user) user.gain.setTargetAtTime(userVol, ctx.currentTime, 0.03);
}
export const userVolume = () => userVol;

export const getAnalyser = () => (ctx && analyser && ctx.state === "running" ? analyser : null);
export const sampleRate = () => (ctx ? ctx.sampleRate : 48000);
