// Встроенный плеер: один <audio> на всё приложение, два режима.
//  wave  — «Моя Волна», живой MP3-поток с того же origin.
//          idle → loading → playing ⇄ paused; reconnecting (обрыв, автоповтор с backoff); error (повторы кончились).
//          Пауза = остановка и освобождение потока (src="" + load()): живое радио не «копит» буфер, а при продолжении
//          подключаемся заново к живому краю (?t=… против кэша).
//  queue — очередь своих файлов (/api/stream/{id}, подписанные ссылки на 12 ч): next/prev, перемотка, shuffle,
//          repeat off/all/one, автопереход, предзагрузка следующего, Media Session, состояние в localStorage
//          (восстанавливается на паузе, без autoplay).
// Переходы: старт очереди останавливает волну; старт волны ставит очередь в сторону (она остаётся и вернётся на
// паузе, когда волну закроют). Фрагмент 30 с ставит на паузу то, что играет, и сам ничего не продолжает.
// Старт — только из жеста пользователя (без autoplay).
import { api, absUrl, remote } from "./api.js";
import { cacheOn, cacheUrl, cachePrefetch, isOfflineTrack } from "./offline.js";
import { setClosingConfirmation } from "./tg.js";
import { reducedMotion } from "./motion.js";
import * as graph from "./audiograph.js";
import { storage, parseDur } from "./dom.js";

const POLL_MS = 10000;
const BACKOFF = [1000, 2000, 4000, 8000, 15000]; // ~5 попыток, затем error
const STALL_MS = 9000; // «играет», а ни звук (currentTime), ни данные (progress) не идут столько времени → переподключение
// Холодный старт станции (сервер поднимает её с первым слушателем: каталог Jellyfin, ffmpeg) бывает дольше 9 с —
// до первого звука ждём дольше; сервер сам закрывает поток через 30 с без данных (NO_DATA_TIMEOUT в radio.py).
const START_MS = 25000;
const STALE_MS = 6 * 3600 * 1000; // сохранённой очереди больше 6 ч — ссылки обновляем заранее
const PRELOAD_S = 15; // за сколько секунд до конца грузим следующий трек

const listeners = new Set();
export const player = {
  mode: "wave", // wave | queue
  state: "idle", // idle | loading | playing | paused | error | reconnecting
  desired: "stopped", // playing | paused | stopped — чего хочет пользователь
  now: null, // ответ /api/wave/now
  error: null, // { kind: "stream" | "blocked" | "empty", message }
  attempt: 0,
  url: "",
  // очередь
  queue: [], index: -1, shuffle: false, repeat: "off", source: null,
  time: 0, duration: 0, buffering: false,
};

export function onPlayer(fn) { listeners.add(fn); return () => listeners.delete(fn); }
let queueDirty = false;
function emit(what, data) {
  if (what === "track" || what === "queue") prefetchNext();
  if (what === "queue") queueDirty = true; for (const fn of listeners) { try { fn(what, player, data); } catch (e) { console.error(e); } } }
function setState(s, what = "state") {
  if (player.state === s && what === "state") return;
  player.state = s;
  syncSession();
  setClosingConfirmation(player.desired === "playing");
  emit(what);
}

export const isActive = () => player.state !== "idle";
export const isOn = () => player.desired === "playing"; // играет что угодно (для мини-плеера)
export const isWaveOn = () => player.mode === "wave" && player.desired === "playing"; // кнопка «Слушать Мою Волну»
export const current = () => (player.mode === "queue" && player.queue[player.index]) || null;

let audio = null;
let retryTimer = 0, pollTimer = 0, stallTimer = 0, waveGen = 0;
let lastTime = -1, lastProgress = 0, lastNet = 0;
let ownPause = false; // pause-событие вызвано нами (а не системой / iOS при сворачивании)

const pair = [];
function mkAudio() {
  const a = document.createElement("audio");
  // поток с другого origin (локальный интерфейс, кэш оболочки) — CORS, иначе Web Audio (эквалайзер) молчит
  if (remote || cacheOn()) a.crossOrigin = "anonymous";
  a.preload = "none";
  a.setAttribute("aria-hidden", "true");
  a.hidden = true;
  a.preservesPitch = true;
  const on = (ev, fn) => a.addEventListener(ev, (e) => { if (e.currentTarget === audio) fn(e); });
  on("playing", () => {
    if (unlocking) return;
    if (player.mode === "queue") { qPlaying(); return; }
    graph.resume();
    watchdog();
    if (player.desired !== "playing") return;
    player.attempt = 0;
    player.error = null;
    lastProgress = performance.now();
    setState("playing");
    armStall();
    if (!player.now || !player.now.title) poll(); // станция поднимается вместе с первым слушателем — сразу узнаём трек
  });
  on("waiting", () => {
    if (unlocking) return;
    if (player.mode === "queue") { if (player.desired === "playing") { player.buffering = true; emit("state"); armQStall(); } return; }
    if (player.desired === "playing" && player.state === "playing") armStall();
  });
  on("timeupdate", () => {
    if (player.mode === "queue") { qTime(); return; }
    if (audio.currentTime !== lastTime) { lastTime = audio.currentTime; lastProgress = performance.now(); }
  });
  on("loadedmetadata", () => { if (player.mode === "queue") qMeta(); });
  on("durationchange", () => { if (player.mode === "queue") qMeta(); });
  on("seeked", () => { if (player.mode === "queue") { player.time = audio.currentTime; positionState(true); emit("time"); } });
  const broken = () => { if (player.desired === "playing" && audio.getAttribute("src")) reconnect(); };
  on("error", () => { if (unlocking) return; if (player.mode === "queue") { if (audio.getAttribute("src")) qFail(loadSeq); return; } broken(); });
  on("ended", () => { if (unlocking) return; if (player.mode === "queue") { qEnded(); return; } broken(); });
  on("stalled", () => { if (player.mode === "wave" && player.desired === "playing") armStall(); });
  // данные потока приходят (progress) — соединение живо, даже если звук ещё не пошёл (холодный старт, буферизация)
  on("progress", () => { if (player.mode === "wave") lastNet = performance.now(); });
  on("pause", () => {
    if (unlocking) return;
    if (player.mode === "queue") { qSystemPause(); return; }
    if (ownPause || player.desired !== "playing" || player.state !== "playing" || !audio.getAttribute("src") || audio.error) return;
    // Поток кончился (сервер оборвал соединение) — Chromium ставит паузу на конце данных: переподключаемся.
    // Иначе паузу поставила система (звонок, гарнитура, iOS при сворачивании) — не спорим: освобождаем поток,
    // а «Слушать» подключит к живому краю заново.
    let ahead = 0;
    try {
      const b = audio.buffered, t = audio.currentTime;
      for (let i = 0; i < b.length; i++) if (b.start(i) <= t && t <= b.end(i)) ahead = b.end(i) - t;
    } catch { /* */ }
    // Скрытая вкладка: на телефоне паузу в фоне ставит система — не спорим; на десктопе (окно в трее, свёрнутый
    // браузер) скрытость ничего не значит — конец данных там тоже обрыв, переподключаемся.
    const sysBg = document.visibilityState === "hidden" && MOBILE;
    if (!sysBg && (audio.ended || ahead < 0.75)) reconnect();
    else pause();
  });
  document.body.appendChild(a);
  pair.push(a);
  if (graph.hasGraph()) graph.attach(a); else graph.setTrackGain(a, 1); // без графа — сразу с громкостью пользователя
  return a;
}
function el() { return audio || (audio = mkAudio()); }
// второй элемент: следующий трек для плавного перехода / gapless
function other() { el(); return pair.find((x) => x !== audio) || mkAudio(); }
function clearEl(a) {
  if (!a) return;
  try { a.pause(); } catch { /* */ }
  a.removeAttribute("src");
  try { a.load(); } catch { /* */ }
  a._qid = 0;
}

function clearTimers() {
  clearTimeout(retryTimer); retryTimer = 0;
  waveGen++; // отложенная попытка переподключения (ждёт свежий адрес) больше не актуальна
  clearTimeout(stallTimer); stallTimer = 0;
}

const MOBILE = graph.iOS || /Android/i.test(navigator.userAgent);
// Сторож живого потока: «играет» — ни звука, ни данных 9 с; ещё не играет (старт/переподключение) — 25 с.
// Данные идут, а звук стоит дольше 25 с — тоже обрыв (декодер застрял).
function armStall() {
  clearTimeout(stallTimer);
  stallTimer = setTimeout(function check() {
    if (player.mode !== "wave" || player.desired !== "playing") return;
    const now = performance.now(), limit = player.state === "playing" ? STALL_MS : START_MS;
    const quiet = now - lastProgress > limit && now - lastNet > limit;
    if (quiet || now - lastProgress > START_MS) { reconnect(); return; }
    stallTimer = setTimeout(check, 3000);
  }, 3000);
}

function connect() {
  const a = el();
  clearTimeout(stallTimer);
  lastTime = -1;
  lastProgress = performance.now();
  lastNet = 0;
  ownPause = false;
  a.preload = "none";
  unlocking = false;
  cancelFade();
  clearEl(pair.find((x) => x !== a));
  a.playbackRate = 1;
  graph.setTrackGain(a, 1);
  a.src = player.url + (player.url.includes("?") ? "&" : "?") + "t=" + Date.now();
  let p;
  try { p = a.play(); } catch (e) { p = Promise.reject(e); }
  armStall();
  return Promise.resolve(p).catch((e) => {
    if (player.mode !== "wave" || player.desired !== "playing") return;
    if (e && e.name === "NotAllowedError") {
      // политика автозапуска: нужен новый тап пользователя
      release();
      player.desired = "paused";
      player.error = { kind: "blocked", message: "Браузер не дал включить звук — нажмите «Слушать» ещё раз." };
      setState("paused", "error");
      return;
    }
    if (e && e.name === "AbortError") return; // src сменили — новый connect уже идёт
    reconnect();
  });
}

function release() {
  if (!audio) return;
  unlocking = false;
  ownPause = true;
  try { audio.pause(); } catch { /* */ }
  audio.removeAttribute("src");
  try { audio.load(); } catch { /* */ }
  ownPause = false;
  srcQid = 0;
}

function reconnect() {
  if (player.mode !== "wave" || player.desired !== "playing" || retryTimer) return;
  clearTimeout(stallTimer);
  release();
  if (player.attempt >= BACKOFF.length) {
    player.desired = "paused";
    player.error = { kind: "stream", message: navigator.onLine === false ? "Нет связи с сервером." : "Поток «Моей Волны» сейчас недоступен." };
    stopPoll();
    setState("error");
    return;
  }
  const delay = navigator.onLine === false ? 3000 : BACKOFF[player.attempt];
  player.attempt++;
  setState("reconnecting");
  retryTimer = setTimeout(async () => {
    retryTimer = 0;
    if (player.mode !== "wave" || player.desired !== "playing") return;
    if (navigator.onLine === false) { player.attempt--; reconnect(); return; } // ждём сеть, попытки не тратим
    // со второй попытки — свежий адрес потока с сервера (токен потока могли сменить; приложение в трее живёт сутками)
    if (player.attempt >= 2) {
      const gen = waveGen;
      retryTimer = -1; // занято: повторный reconnect() не должен запустить вторую попытку
      await refreshWaveUrl();
      if (gen !== waveGen) return; // за это время нажали «Пауза»/«Слушать» — там уже свой connect()
      retryTimer = 0;
      if (player.mode !== "wave" || player.desired !== "playing") return;
    }
    connect();
  }, delay);
}

async function refreshWaveUrl() {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const me = await api("/me", { signal: ctrl.signal }).finally(() => clearTimeout(t));
    if (me && me.stream_url) player.url = absUrl(me.stream_url);
  } catch { /* нет связи — пробуем прежний адрес */ }
}

// ---------------------------------------------------------------- управление волной (вызывать из обработчиков тапа)
export function play(url) {
  if (url) player.url = absUrl(url);
  if (!player.url) return;
  stopPreview();
  if (player.mode === "queue") parkQueue();
  clearTimers();
  player.mode = "wave";
  player.desired = "playing";
  player.attempt = 0;
  player.error = null;
  ensureAudioGraph(); // AudioContext создаём/возобновляем строго в жесте пользователя
  syncHandlers();
  setState("loading");
  connect();
  startPoll();
}

export function pause() {
  if (player.mode === "queue") { qPause(); return; }
  clearTimers();
  player.desired = "paused";
  release();
  stopPoll();
  setState("paused");
}

// Стоп: волна — закрыть (если есть отложенная очередь — она возвращается на паузе); очередь — очистить
export function stop() {
  if (player.mode === "queue") { clearQueue(); return; }
  clearTimers();
  release();
  stopPoll();
  player.error = null;
  player.now = null;
  if (player.queue.length) { // отложенная очередь
    player.mode = "queue";
    player.desired = "paused";
    syncHandlers();
    setState("paused");
    emit("track");
    return;
  }
  player.desired = "stopped";
  setState("idle");
}

// Переключатель волны: очередь (если играла) уступает место эфиру
export function toggle(url) {
  if (isWaveOn()) pause(); else play(url);
}

// Общая кнопка ▶/❚❚ (мини-плеер, «Сейчас играет», гарнитура): то, что сейчас выбрано
export function togglePlay() {
  if (player.mode === "queue") { if (player.desired === "playing") qPause(); else resume(); return; }
  if (player.state === "error") retry(); else toggle();
}

export function retry() { play(); }

// ---------------------------------------------------------------- «сейчас в эфире»
let lastKey = "";
async function poll() {
  try {
    const now = await api("/wave/now");
    if (player.mode !== "wave" || player.state === "idle") return;
    const key = now && now.title ? `${now.artist}|${now.title}|${now.started_at}` : "";
    const changed = key !== lastKey;
    lastKey = key;
    player.now = now;
    syncSession();
    emit(changed ? "track" : "now");
  } catch { /* не критично: звук идёт и без метаданных */ }
}
function startPoll() {
  stopPoll();
  poll();
  pollTimer = setInterval(() => { if (document.visibilityState !== "hidden") poll(); }, POLL_MS);
}
function stopPoll() { clearInterval(pollTimer); pollTimer = 0; }

// ================================================================ очередь
const FIELDS = ["id", "artist", "title", "album", "duration", "seconds", "cover", "stream", "owned", "artist_key", "album_key", "gain", "peak"];
let qidN = 0;
let base = null; // порядок qid до перемешивания — чтобы выключить shuffle без потери исходного порядка
let loadSeq = 0, failedSeq = -1, srcQid = 0, pendingSeek = 0;
let qLogin = "";

function norm(t) {
  const o = {};
  for (const k of FIELDS) if (t[k] !== undefined && t[k] !== null) o[k] = t[k];
  if (!o.seconds) o.seconds = parseDur(t.duration);
  o.qid = ++qidN;
  return o;
}
const abs = (u) => absUrl(u); // потоки — от адреса сервера (API_BASE)
const shuffled = (arr) => { const a = arr.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };

// Играть список с трека start (null — с начала; при shuffle — случайный). source: {kind, id, title}
export function playQueue(tracks, start = 0, { shuffle = false, source = null } = {}) {
  let items = (tracks || []).filter((t) => t && t.id !== undefined && t.id !== null).map(norm);
  if (!items.length) return false;
  stopPreview();
  leaveWave();
  let i = start === null || start === undefined || start < 0 ? (shuffle ? Math.floor(Math.random() * items.length) : 0) : Math.min(start, items.length - 1);
  base = null;
  if (shuffle) {
    base = items.map((x) => x.qid);
    const first = items[i];
    items = [first, ...shuffled(items.filter((_, k) => k !== i))];
    i = 0;
  }
  Object.assign(player, { mode: "queue", queue: items, index: i, shuffle: !!shuffle, source, error: null });
  syncHandlers();
  emit("queue");
  startAt(i, { play: true });
  return true;
}

function leaveWave() {
  if (player.mode !== "wave") return;
  loadSeq++;
  clearTimers();
  stopPoll();
  if (player.state !== "idle") release();
  player.now = null;
  lastKey = "";
}

// Очередь уступает место волне: запоминаем позицию, трек выгружаем
function parkQueue() {
  loadSeq++; // отложенный refresh() не должен запустить трек поверх волны
  clearTimeout(qStallT);
  if (audio && srcQid) player.time = audio.currentTime || player.time;
  ownPause = true;
  try { if (audio) audio.pause(); } catch { /* */ }
  ownPause = false;
  srcQid = 0;
  resetPreload();
  saveNow();
}

function clearQueue() {
  cancelFade();
  loadSeq++;
  clearTimeout(qStallT);
  release();
  resetPreload();
  Object.assign(player, { queue: [], index: -1, source: null, time: 0, duration: 0, buffering: false, error: null, mode: "wave", desired: "stopped" });
  base = null;
  saveNow();
  syncHandlers();
  setState("idle");
  emit("queue");
}

function startAt(i, { play: wantPlay = true, at = 0 } = {}) {
  const it = player.queue[i];
  if (!it) return;
  const my = ++loadSeq;
  player.index = i;
  player.time = at;
  player.duration = it.seconds || 0;
  player.buffering = false;
  player.error = null;
  const twin = pair.find((x) => x !== audio);
  if (!(twin && twin._qid === it.qid)) resetPreload();
  clearTimeout(qStallT);
  cancelFade(true);
  sleepRestore();
  if (wantPlay) { player.desired = "playing"; ensureAudioGraph(); stopPreview(); }
  else player.desired = "paused";
  emit("track");
  setState(wantPlay ? "loading" : "paused");
  emit("state");
  save();
  if (!wantPlay) { // на паузе файл не грузим — загрузится по «Играть»
    if (srcQid && srcQid !== it.qid) release();
    return;
  }
  // пока ждём ссылку, прежний трек не доигрывает
  if (!it.stream && srcQid && srcQid !== it.qid) release();
  const go = (res) => {
    // пауза / волна / другой трек за время ожидания ссылки — ничего не запускаем
    if (my !== loadSeq || player.mode !== "queue" || player.desired !== "playing") return;
    if (res === "net") { netError(); return; }
    if (!it.stream && !(cacheOn() && isOfflineTrack(it.id))) { skipBroken(); return; }
    let a = el();
    unlocking = false;
    // следующий уже загружен во второй элемент (gapless / «Далее») — переключаемся на него
    const b = pair.find((x) => x !== a);
    if (b && b._qid === it.qid && b.getAttribute("src") && !at) { ownPause = true; clearEl(a); ownPause = false; audio = a = b; }
    else {
      ownPause = true;
      a.preload = "auto";
      a.src = srcFor(it);
      a._qid = it.qid;
      ownPause = false;
      pendingSeek = at;
    }
    srcQid = it.qid;
    prepTrack(a, it);
    doPlay(my);
  };
  if (!it.stream && cacheOn() && isOfflineTrack(it.id)) go(); // скачан на устройство — ссылка не нужна
  else if (!it.stream) refresh([it.id]).then(go); else go();
}

function doPlay(my) {
  const a = el();
  let p;
  try { p = a.play(); } catch (e) { p = Promise.reject(e); }
  Promise.resolve(p).catch((e) => {
    if (my !== loadSeq || player.mode !== "queue") return;
    if (e && e.name === "NotAllowedError") {
      player.desired = "paused";
      player.error = { kind: "blocked", message: "Браузер не дал включить звук — нажмите ▶ ещё раз." };
      setState("paused", "error");
      return;
    }
    if (e && e.name === "AbortError") return;
    qFail(my);
  });
}

// Трек не стартовал: один раз обновляем ссылку (могла истечь), затем пропускаем
function qFail(my) {
  if (my !== loadSeq || failedSeq === my || player.mode !== "queue") return;
  failedSeq = my;
  const it = player.queue[player.index];
  if (!it) return;
  const at = (audio && audio.currentTime) || player.time;
  // сеть пропала — трек не «недоступен»: ждём связь, очередь не трогаем
  if (navigator.onLine === false || (audio && audio.error && audio.error.code === 2)) { player.time = at; netError(); return; }
  if (!it._retried) {
    it._retried = true;
    refresh([it.id]).then((res) => {
      if (my !== loadSeq || player.mode !== "queue") return;
      if (res === "net") { player.time = at; netError(); return; }
      if (it.unavailable || !it.stream) skipBroken();
      else startAt(player.index, { play: player.desired === "playing", at });
    });
    return;
  }
  skipBroken();
}

// Нет связи: ошибка с «▶ — повторить»; вернулась сеть — продолжаем с той же позиции
let netWait = false, skipRun = 0, qStallT = 0;
function netError() {
  clearTimeout(qStallT);
  if (audio && srcQid) player.time = audio.currentTime || player.time;
  release();
  player.desired = "paused";
  player.buffering = false;
  player.error = { kind: "net", message: "Нет связи с сервером — нажмите ▶, чтобы повторить." };
  netWait = true;
  setState("error");
  saveNow();
}

// «Играет», а звук стоит дольше 15 с — обновляем ссылку и переподключаемся с той же позиции
function armQStall() {
  clearTimeout(qStallT);
  const my = loadSeq, t0 = audio ? audio.currentTime : 0;
  qStallT = setTimeout(() => {
    if (my !== loadSeq || player.mode !== "queue" || player.desired !== "playing" || !audio) return;
    if (!player.buffering || audio.currentTime > t0 + 0.5) return;
    if (navigator.onLine === false) { netError(); return; }
    const it = player.queue[player.index];
    const at = audio.currentTime || player.time;
    refresh([it.id]).then((res) => {
      if (my !== loadSeq || player.mode !== "queue" || player.desired !== "playing") return;
      if (res === "net") { player.time = at; netError(); return; }
      startAt(player.index, { play: true, at });
    });
  }, 15000);
}

function skipBroken() {
  const it = player.queue[player.index];
  if (it) { it.unavailable = true; emit("skip", it); }
  const j = nextIndex(player.index, true);
  if (j < 0 || ++skipRun >= 3) { // три недоступных подряд — останавливаемся, а не листаем всю очередь
    skipRun = 0;
    release();
    player.desired = "paused";
    player.error = { kind: "stream", message: "Не удалось включить треки подряд — нажмите ▶, чтобы повторить." };
    setState("error");
    emit("queue");
    return;
  }
  emit("queue");
  startAt(j, { play: player.desired === "playing" });
}

// Свежие подписанные ссылки: GET /api/tracks?ids= (≤ 500 за раз); не вернувшиеся треки недоступны
async function refresh(ids) {
  ids = [...new Set(ids)].filter((x) => x !== undefined && x !== null);
  let ok = false;
  for (let k = 0; k < ids.length; k += 500) {
    const chunk = ids.slice(k, k + 500);
    let r;
    try { r = await api("/tracks?ids=" + chunk.join(",")); } catch (e) {
      if (e && e.status === 404) return false; // старый сервер без /api/tracks — оставляем как есть
      if (e && (e.status === 0 || e.status >= 500)) return "net"; // сеть/сервер — недоступными не помечаем
      continue;
    }
    ok = true;
    const map = new Map((r.items || []).map((t) => [t.id, t]));
    const want = new Set(chunk);
    for (const it of player.queue) {
      if (!want.has(it.id)) continue;
      const t = map.get(it.id);
      if (!t) { it.unavailable = true; continue; }
      for (const f of FIELDS) if (t[f] !== undefined && t[f] !== null && f !== "id") it[f] = t[f];
      if (!it.seconds) it.seconds = parseDur(t.duration);
      it.unavailable = false;
    }
  }
  if (ok) save();
  return ok;
}

function nextIndex(i, auto = false) {
  const q = player.queue, n = q.length;
  for (let k = 1; k <= n; k++) {
    let j = i + k;
    if (j >= n) { if (player.repeat !== "all") return -1; j %= n; }
    if (j === i && !auto) return j;
    if (!q[j].unavailable) return j;
  }
  return -1;
}
function prevIndex(i) {
  const q = player.queue, n = q.length;
  for (let k = 1; k <= n; k++) {
    let j = i - k;
    if (j < 0) { if (player.repeat !== "all") return -1; j = ((j % n) + n) % n; }
    if (!q[j].unavailable) return j;
  }
  return -1;
}
export const hasNext = () => player.mode === "queue" && (player.repeat === "one" || nextIndex(player.index) >= 0);
export const hasPrev = () => player.mode === "queue" && player.queue.length > 0;
// соседний трек очереди (смахивание мини-плеера): d = 1 — следующий, −1 — предыдущий; нет — null
export function neighbor(d) {
  if (player.mode !== "queue" || player.index < 0) return null;
  const j = d > 0 ? nextIndex(player.index) : prevIndex(player.index);
  return j >= 0 && j !== player.index ? player.queue[j] : null;
}
// к соседнему треку без правила «назад в начало, если прошло > 3 с» (жест — всегда к другому треку)
export function stepTrack(d) {
  const t = neighbor(d);
  if (!t) return false;
  startAt(player.queue.indexOf(t), { play: player.desired === "playing" });
  return true;
}
const audioTime = () => (audio && srcQid ? audio.currentTime : player.time) || 0;

function qPlaying() {
  graph.resume();
  watchdog();
  const it = player.queue[player.index];
  if (it) it._retried = false;
  skipRun = 0;
  netWait = false;
  clearTimeout(qStallT);
  player.buffering = false;
  if (player.desired !== "playing") player.desired = "playing"; // продолжила система (гарнитура)
  setState("playing");
  emit("state");
  positionState(true);
}

function qSystemPause() {
  // смена src и наша пауза тоже присылают pause (асинхронно) — считаем системной только паузу во время игры
  if (ownPause || !audio || audio.ended || player.desired !== "playing" || player.state !== "playing") return;
  // паузу поставила система (звонок, гарнитура, другое приложение) — отражаем, но очередь не трогаем
  player.desired = "paused";
  player.time = audio.currentTime;
  setState("paused");
  saveNow();
}

function qPause() {
  clearTimeout(qStallT);
  cancelFade(true);
  const cur = current();
  if (!cur || srcQid !== cur.qid) loadSeq++; // ссылка ещё грузится — отменяем отложенный старт
  if (audio && srcQid) {
    ownPause = true;
    try { audio.pause(); } catch { /* */ }
    ownPause = false;
    player.time = audio.currentTime;
  }
  player.desired = "paused";
  player.buffering = false;
  setState("paused");
  emit("state");
  saveNow();
}

let unlocking = false;
const SILENT = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
// Вызывать синхронно в обработчике тапа перед await (iOS: play() после сетевого запроса без жеста запрещён)
export function unlockAudio() {
  ensureAudioGraph(true); // мы в жесте пользователя: создаём/будим AudioContext сейчас, а не после await
  const a = el();
  if (!a.paused || srcQid || (player.mode === "wave" && player.state !== "idle")) return;
  unlocking = true;
  try { a.src = SILENT; const p = a.play(); if (p && p.catch) p.catch(() => {}); } catch { /* */ }
}

export function resume() {
  if (player.mode !== "queue") return;
  const it = player.queue[player.index];
  if (!it) return;
  stopPreview();
  if (srcQid !== it.qid || !audio || !audio.getAttribute("src") || player.state === "error") {
    startAt(player.index, { play: true, at: player.time });
    return;
  }
  player.desired = "playing";
  player.error = null;
  ensureAudioGraph();
  if (audio.readyState < 3) setState("loading");
  doPlay(loadSeq);
}

function qMeta() {
  const d = audio.duration;
  if (isFinite(d) && d > 0) player.duration = d;
  if (pendingSeek > 0 && isFinite(d)) {
    try { audio.currentTime = Math.min(pendingSeek, Math.max(0, d - 1)); } catch { /* */ }
    pendingSeek = 0;
  }
  positionState(true);
  emit("time");
}

let lastSave = 0;
function qTime() {
  if (!srcQid) return;
  if (player.buffering && audio.currentTime > player.time + 0.2) { player.buffering = false; clearTimeout(qStallT); emit("state"); }
  player.time = audio.currentTime;
  const d = audio.duration;
  if (isFinite(d) && d > 0) player.duration = d;
  emit("time");
  positionState(false);
  // оставшееся время в реальных секундах (с учётом скорости)
  const left = player.duration ? (player.duration - player.time) / (opts.speed || 1) : Infinity;
  if (left < Math.max(PRELOAD_S, opts.crossfade + 6)) preloadNext();
  if (sleep && sleep.mode === "track" && left < SLEEP_FADE && !sleep.fading) { if (graph.setMaster(0, Math.max(0.5, left))) sleep.fading = true; }
  else if (opts.crossfade > 0 && !fade && left <= opts.crossfade && player.desired === "playing" && player.repeat !== "one" && !(sleep && sleep.mode === "track")
    && player.duration > opts.crossfade * 2.5 && graph.running() && !graph.iOS) startFade();
  if (performance.now() - lastSave > 5000) { lastSave = performance.now(); save(); }
}

function qEnded() {
  if (sleep && sleep.mode === "track") { sleepDone(); return; } // «до конца трека»: дальше не играем
  if (player.repeat === "one") {
    try { audio.currentTime = 0; } catch { /* */ }
    doPlay(loadSeq);
    return;
  }
  const j = nextIndex(player.index, true);
  if (j >= 0) { startAt(j, { play: true }); return; } // startAt подхватит второй элемент, если он готов
  // конец очереди: возвращаемся к первому треку на паузе — «Играть» начнёт сначала
  const first = player.queue.findIndex((x) => !x.unavailable);
  startAt(first >= 0 ? first : 0, { play: false });
}

export function next() {
  if (player.mode !== "queue") return;
  const j = nextIndex(player.index);
  if (j < 0) return;
  startAt(j, { play: player.desired === "playing" });
}

export function prev() {
  if (player.mode !== "queue") return;
  if (audioTime() > 3) { seek(0); return; }
  const j = prevIndex(player.index);
  if (j < 0) { seek(0); return; }
  startAt(j, { play: player.desired === "playing" });
}

export function seek(sec) {
  if (player.mode !== "queue" || !current() || !Number.isFinite(sec)) return;
  const d = player.duration || current().seconds || 0;
  sec = Math.max(0, d ? Math.min(sec, Math.max(0, d - 0.5)) : sec);
  player.time = sec;
  sleepRestore();
  if (audio && srcQid === current().qid && audio.readyState > 0) {
    try { audio.currentTime = sec; } catch { /* */ }
  } else pendingSeek = sec;
  emit("time");
  positionState(true);
  save();
}

export function jumpTo(i) {
  if (player.mode !== "queue" || !player.queue[i]) return;
  player.queue[i].unavailable = false;
  startAt(i, { play: true });
}

export function toggleShuffle() {
  if (player.mode !== "queue" || !player.queue.length) return;
  const cur = current();
  if (player.shuffle) {
    if (base) {
      const pos = new Map(base.map((q, k) => [q, k]));
      // добавленные во время перемешивания — в конец, в порядке добавления
      player.queue = player.queue.slice().sort((a, b) => (pos.has(a.qid) ? pos.get(a.qid) : 1e9 + a.qid) - (pos.has(b.qid) ? pos.get(b.qid) : 1e9 + b.qid));
    }
    base = null;
    player.shuffle = false;
  } else {
    base = player.queue.map((x) => x.qid);
    const rest = player.queue.filter((x) => x !== cur);
    player.queue = cur ? [cur, ...shuffled(rest)] : shuffled(rest);
    player.shuffle = true;
  }
  player.index = cur ? player.queue.indexOf(cur) : 0;
  resetPreload();
  emit("queue");
  save();
}

export function cycleRepeat() {
  player.repeat = player.repeat === "off" ? "all" : player.repeat === "all" ? "one" : "off";
  resetPreload();
  emit("queue");
  save();
}

// Пусто (или играет волна без очереди) — трек просто начинает играть
export function addNext(t) {
  if (!player.queue.length) { playQueue([t], 0); return "play"; }
  const it = norm(t);
  player.queue.splice(player.index + 1, 0, it);
  if (base) { const cur = current(); const k = cur ? base.indexOf(cur.qid) : -1; base.splice(k + 1, 0, it.qid); }
  resetPreload();
  emit("queue");
  save();
  return "next";
}

// Несколько треков разом: одна вставка, одно событие и одно сохранение (а не N)
export function addMany(tracks, { next = false } = {}) {
  const list = (tracks || []).filter((t) => t && t.id !== undefined);
  if (!list.length) return "none";
  if (!player.queue.length) { playQueue(list, 0); return "play"; }
  const items = list.map(norm);
  if (next) {
    player.queue.splice(player.index + 1, 0, ...items);
    if (base) { const cur = current(); const k = cur ? base.indexOf(cur.qid) : -1; base.splice(k + 1, 0, ...items.map((x) => x.qid)); }
  } else {
    player.queue.push(...items);
    if (base) base.push(...items.map((x) => x.qid));
  }
  resetPreload();
  emit("queue");
  save();
  return next ? "next" : "queued";
}

// Удалённые из медиатеки треки — из очереди за один проход (Set), одно событие и одно сохранение.
// Играющий не обрываем, если он уцелел; удалён — переходим к следующему уцелевшему (на паузе — без запуска).
export function removeTrackIds(ids) {
  const set = new Set([...ids].map(String));
  const q = player.queue;
  if (!set.size || !q.length) return 0;
  const hit = (x) => set.has(String(x.id));
  const cur = q[player.index];
  const curGone = !!cur && player.mode === "queue" && hit(cur);
  let before = 0; // уцелевших до текущего — это индекс следующего уцелевшего в новой очереди
  const keep = [];
  for (let k = 0; k < q.length; k++) {
    const x = q[k];
    if (x === cur && !curGone) { keep.push(x); continue; }
    if (hit(x)) continue;
    if (k < player.index) before++;
    keep.push(x);
  }
  const removed = q.length - keep.length;
  if (!removed) return 0;
  if (curGone && !keep.length) { clearQueue(); return removed; }
  if (base) { const alive = new Set(keep.map((x) => x.qid)); base = base.filter((qid) => alive.has(qid)); }
  player.queue = keep;
  if (curGone) {
    const wantPlay = player.desired === "playing";
    let ni = before;
    if (ni >= keep.length) ni = player.repeat === "all" ? 0 : keep.length - 1;
    const play = wantPlay && (before < keep.length || player.repeat === "all");
    player.index = ni;
    resetPreload();
    emit("queue");
    startAt(ni, { play });
    save();
    return removed;
  }
  player.index = cur ? keep.indexOf(cur) : Math.min(player.index, keep.length - 1);
  resetPreload();
  emit("queue");
  save();
  return removed;
}

// Убрать позиции очереди разом (по qid, кроме играющей): линейно, одна перерисовка и одно сохранение
export function removeQids(qids) {
  const set = new Set(qids);
  const cur = player.queue[player.index];
  const before = player.queue.length;
  player.queue = player.queue.filter((x) => x === cur || !set.has(x.qid));
  const n = before - player.queue.length;
  if (!n) return 0;
  if (base) base = base.filter((qid) => (cur && qid === cur.qid) || !set.has(qid));
  player.index = cur ? player.queue.indexOf(cur) : Math.min(player.index, player.queue.length - 1);
  resetPreload();
  emit("queue");
  save();
  return n;
}

// Выбранные позиции из «Далее» — сразу после играющей, в их порядке (сыгранные не трогаем)
export function moveQidsNext(qids) {
  const set = new Set(qids);
  const q = player.queue, i = player.index;
  const picked = [], rest = [];
  for (let k = 0; k < q.length; k++) (k > i && set.has(q[k].qid) ? picked : rest).push(q[k]);
  if (!picked.length) return 0;
  player.queue = [...rest.slice(0, i + 1), ...picked, ...rest.slice(i + 1)];
  resetPreload();
  emit("queue");
  save();
  return picked.length;
}

export function addToQueue(t) {
  if (!player.queue.length) { playQueue([t], 0); return "play"; }
  const it = norm(t);
  player.queue.push(it);
  if (base) base.push(it.qid);
  emit("queue");
  save();
  return "queued";
}

export function removeAt(i) {
  const q = player.queue;
  if (!q[i]) return;
  if (q.length === 1) { if (player.mode === "queue") clearQueue(); else { player.queue = []; player.index = -1; base = null; saveNow(); emit("queue"); } return; }
  const [gone] = q.splice(i, 1);
  if (base) base = base.filter((x) => x !== gone.qid);
  if (i === player.index) {
    const j = Math.min(i, q.length - 1);
    if (player.mode === "queue") startAt(j, { play: player.desired === "playing" });
    else player.index = j;
  } else if (i < player.index) player.index--;
  resetPreload();
  emit("queue");
  save();
}

export function moveItem(from, to) {
  const q = player.queue;
  if (!q[from] || to < 0 || to >= q.length || from === to) return;
  const cur = q[player.index];
  const [it] = q.splice(from, 1);
  q.splice(to, 0, it);
  player.index = q.indexOf(cur);
  resetPreload();
  emit("queue");
  save();
}

export function clearUpcoming() {
  if (player.index < 0) return;
  const gone = new Set(player.queue.slice(player.index + 1).map((x) => x.qid));
  player.queue = player.queue.slice(0, player.index + 1);
  if (base) base = base.filter((x) => !gone.has(x)); // сыгранные остаются в исходном порядке
  resetPreload();
  emit("queue");
  save();
}

// ---------------------------------------------------------------- предзагрузка следующего
function preloadNext() {
  if (fade) return;
  const j = player.repeat === "one" ? -1 : nextIndex(player.index, true);
  const t = j >= 0 ? player.queue[j] : null;
  if (!t || !t.stream) return;
  const b = other();
  if (b._qid === t.qid) return;
  b.preload = "auto";
  b.src = srcFor(t);
  b._qid = t.qid;
  graph.setTrackGain(b, 0);
  try { b.load(); } catch { /* */ }
}
// ---------------------------------------------------------------- полная предзагрузка следующего трека (blob)
// Следующий трек очереди скачивается целиком в память (blob URL) через несколько секунд после старта текущего —
// переход без задержки и без сетевых пауз. Держим только текущий и следующий, трек больше 80 МБ не берём,
// режим экономии трафика (Save-Data) — без предзагрузки.
const BLOB_MAX = 80 * 1048576;
const blobs = new Map(); // qid → { url, ctrl }
let blobT = 0;
// оболочка с кэшем (Windows 2.0): поток через её протокол — с диска или с докачкой в кэш
function srcFor(it) {
  if (cacheOn()) return cacheUrl(it.id, it.stream ? abs(it.stream) : "");
  const b = blobs.get(it.qid);
  return b && b.url ? b.url : abs(it.stream);
}
function pruneBlobs(keep) {
  for (const [q, b] of blobs) {
    if (keep.has(q)) continue;
    if (b.ctrl) b.ctrl.abort();
    if (b.url) URL.revokeObjectURL(b.url);
    blobs.delete(q);
  }
}
function prefetchNext() {
  clearTimeout(blobT);
  blobT = setTimeout(() => {
    if (player.mode !== "queue") { pruneBlobs(new Set()); return; }
    const cur = current();
    const j = player.repeat === "one" ? -1 : nextIndex(player.index, true);
    const nx = j >= 0 && j !== player.index ? player.queue[j] : null;
    if (cacheOn()) { pruneBlobs(new Set()); if (nx && nx.stream) cachePrefetch(nx.id, abs(nx.stream)); return; } // качает оболочка
    pruneBlobs(new Set([cur && cur.qid, nx && nx.qid].filter(Boolean)));
    if (!nx || !nx.stream || blobs.has(nx.qid) || (navigator.connection && navigator.connection.saveData)) return;
    const rec = { ctrl: new AbortController(), url: null };
    blobs.set(nx.qid, rec);
    fetch(abs(nx.stream), { signal: rec.ctrl.signal, credentials: "omit" }).then(async (r) => {
      if (!r.ok || (Number(r.headers.get("content-length")) || 0) > BLOB_MAX) throw new Error("skip");
      const b = await r.blob();
      if (b.size > BLOB_MAX || blobs.get(nx.qid) !== rec) throw new Error("skip");
      rec.url = URL.createObjectURL(b);
      rec.ctrl = null;
    }).catch(() => { if (blobs.get(nx.qid) === rec) blobs.delete(nx.qid); });
  }, 4000);
}

function resetPreload() {
  if (fade) return;
  const b = pair.find((x) => x !== audio);
  if (b && b._qid) clearEl(b);
}

// ---------------------------------------------------------------- громкость пользователя (ползунок на десктопе)
// Отдельный множитель поверх нормализации, плавного перехода и таймера сна (свой узел графа). Хранится на устройстве.
// Кривая квадратичная: середина ползунка ≈ −12 дБ — ближе к тому, как громкость слышит человек.
const VKEY = "pimusic.volume.v1";
const vol = (() => {
  const s = storage.get(VKEY, null);
  const level = s && Number.isFinite(s.level) ? Math.max(0, Math.min(1, s.level)) : 1;
  return { level, muted: !!(s && s.muted) };
})();
const volGain = () => (vol.muted ? 0 : vol.level * vol.level);
graph.setUserVolume(volGain());
export const getVolume = () => ({ level: vol.level, muted: vol.muted || vol.level === 0 });
function applyVolume() {
  graph.setUserVolume(volGain());
  if (pvAudio) { try { pvAudio.volume = volGain(); } catch { /* */ } }
  storage.set(VKEY, { level: Math.round(vol.level * 1000) / 1000, muted: vol.muted });
  for (const fn of volSubs) { try { fn(getVolume()); } catch (e) { console.error(e); } }
}
// отдельная подписка: ползунок двигают часто — общий onPlayer (перерисовка плеера) не трогаем
const volSubs = new Set();
export function onVolume(fn) { volSubs.add(fn); return () => volSubs.delete(fn); }
// level 0…1; ненулевая громкость снимает «без звука»
export function setVolume(level) {
  const v = Math.max(0, Math.min(1, Number(level) || 0));
  vol.level = v;
  vol.muted = v === 0 ? vol.muted : false;
  applyVolume();
}
// «Без звука» ⇄ прежняя громкость (с нуля — на 50 %, иначе включать нечего)
export function toggleMute() {
  if (vol.muted || vol.level === 0) { vol.muted = false; if (vol.level === 0) vol.level = 0.5; }
  else vol.muted = true;
  applyVolume();
}

// ---------------------------------------------------------------- скорость, плавный переход, таймер сна
const opts = { normalize: true, crossfade: 0, speed: 1 };
export const playerOptions = () => ({ ...opts });
// нормализация по gain/peak трека (цель −14 LUFS считает сервер); подъём не выше +6 дБ и не выше пика
function normGain(it) {
  if (!opts.normalize || !it || typeof it.gain !== "number") return 1;
  let g = Math.pow(10, Math.min(6, it.gain) / 20);
  if (typeof it.peak === "number" && it.peak > 0) g = Math.min(g, Math.max(1, 0.99 / it.peak));
  if (!graph.hasGraph()) g = Math.min(1, g); // без Web Audio — только ослабление через volume
  return g;
}
function prepTrack(a, it) {
  try { a.preservesPitch = true; a.playbackRate = opts.speed; } catch { /* */ }
  const g = normGain(it);
  graph.setTrackGain(a, g);
  graph.setNormBoost(g > 1 ? 20 * Math.log10(g) : 0); // запас громкости, если лимитер выключен
}
export function setPlayerOptions(o = {}) {
  if (typeof o.normalize === "boolean") opts.normalize = o.normalize;
  if (Number.isFinite(o.crossfade)) opts.crossfade = Math.max(0, Math.min(12, o.crossfade));
  if (Number.isFinite(o.speed)) opts.speed = Math.max(0.5, Math.min(2, o.speed));
  if (player.mode === "queue" && audio && srcQid) prepTrack(audio, current());
  emit("options");
}

let fade = null;
function startFade() {
  const j = nextIndex(player.index, true);
  const it = j >= 0 ? player.queue[j] : null;
  if (!it || !it.stream) return;
  const a = audio, b = other();
  if (b._qid !== it.qid) { b.preload = "auto"; b.src = srcFor(it); b._qid = it.qid; }
  const dur = Math.min(opts.crossfade, Math.max(1, (player.duration - player.time) / (opts.speed || 1)));
  graph.setTrackGain(b, 0);
  try { b.preservesPitch = true; b.playbackRate = opts.speed; b.currentTime = 0; } catch { /* */ }
  const my = loadSeq;
  fade = { pending: true, from: a };
  Promise.resolve(b.play()).then(() => {
    if (!fade || !fade.pending || my !== loadSeq || audio !== a) return;
    promoteFade(a, b, it, j, dur);
  }, () => {
    // второй не стартовал — остаёмся на текущем, дальше обычный переход по ended
    if (fade && fade.pending) fade = null;
    clearEl(b);
  });
}
function promoteFade(a, b, it, j, dur) {
  graph.setTrackGain(b, normGain(it), dur);
  graph.setTrackGain(a, 0, dur);
  fade = { from: a, timer: setTimeout(() => cancelFade(), dur * 1000 + 150) };
  // логически следующий трек начинается сейчас
  loadSeq++;
  audio = b;
  srcQid = it.qid;
  player.index = j;
  player.time = 0;
  player.duration = it.seconds || 0;
  skipRun = 0;
  emit("track");
  emit("queue");
  save();
  positionState(true);
}
// quick — пропуск посреди перехода: короткое затухание старого вместо щелчка
function cancelFade(quick = false) {
  if (!fade) return;
  const f = fade;
  fade = null;
  clearTimeout(f.timer);
  if (f.pending) { const b = pair.find((x) => x !== audio); if (b) clearEl(b); return; }
  const done = () => { ownPause = true; clearEl(f.from); ownPause = false; };
  if (quick && graph.hasGraph()) { graph.setTrackGain(f.from, 0, 0.08); setTimeout(done, 100); } else done();
}

// Таймер сна: минуты или «до конца трека»; последние секунды — плавное затухание
const SLEEP_FADE = 8;
let sleep = null, sleepTick = 0;
export const sleepState = () => (sleep ? { mode: sleep.mode, at: sleep.at, left: sleep.mode === "time" ? Math.max(0, sleep.at - Date.now()) : null } : null);
export function setSleep(v) { // число минут | "track" | null
  clearInterval(sleepTick);
  if (sleep && sleep.fading) graph.setMaster(1, 0.3);
  sleep = null;
  if (v === "track") sleep = { mode: "track" };
  else if (Number.isFinite(v) && v > 0) {
    sleep = { mode: "time", at: Date.now() + v * 60000 };
    sleepTick = setInterval(() => {
      const left = (sleep.at - Date.now()) / 1000;
      if (left <= SLEEP_FADE && !sleep.fading) { sleep.fading = true; graph.setMaster(0, Math.max(0.5, left)); }
      if (left <= 0) sleepDone(); else emit("sleep");
    }, 1000);
  }
  emit("sleep");
}
function sleepRestore() {
  if (sleep && sleep.mode === "track" && sleep.fading) { sleep.fading = false; graph.setMaster(1, 0.25); }
}
function sleepDone() {
  clearInterval(sleepTick);
  sleep = null;
  if (player.desired === "playing") pause();
  setTimeout(() => graph.setMaster(1, 0), 300);
  emit("sleep");
  emit("sleep-done");
}

// ---------------------------------------------------------------- сохранение очереди (localStorage, try/catch в storage)
const qkey = () => "pimusic.queue.v2:" + qLogin;
const pkey = () => "pimusic.qpos.v2:" + qLogin;
const KEEP = FIELDS.filter((k) => k !== "stream"); // подписанные ссылки не храним — через 12 ч они всё равно мертвы
let saveT = 0;
function save() { clearTimeout(saveT); saveT = setTimeout(saveNow, 400); }
function drop(key) { try { localStorage.removeItem(key); } catch { /* */ } }
function put(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; } }
function saveNow() {
  clearTimeout(saveT);
  if (!qLogin) return;
  drop("pimusic.queue.v1:" + qLogin); // формат прошлой версии
  if (!player.queue.length) { drop(qkey()); drop(pkey()); queueDirty = false; return; }
  if (queueDirty) {
    queueDirty = false;
    const pick = (it) => { const o = {}; for (const k of KEEP) if (it[k] !== undefined) o[k] = it[k]; o.qid = it.qid; return o; };
    const data = { v: 2, savedAt: Date.now(), items: player.queue.map(pick), shuffle: player.shuffle, repeat: player.repeat, base, source: player.source };
    if (!put(qkey(), data)) {
      // квота: окно вокруг текущего трека
      const lo = Math.max(0, player.index - 100);
      data.items = data.items.slice(lo, lo + 500);
      data.base = null;
      if (!put(qkey(), data)) drop(qkey());
    }
  }
  const cur = player.queue[player.index];
  put(pkey(), { qid: cur ? cur.qid : 0, time: Math.round((srcQid && audio ? audio.currentTime : player.time) * 10) / 10, savedAt: Date.now() });
}
window.addEventListener("pagehide", saveNow);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden" && player.queue.length) saveNow(); });

// Восстановить очередь пользователя (после /api/me) — на паузе, без автозапуска
export async function restoreQueue(login) {
  qLogin = login || "";
  const s = storage.get(qkey(), null) || storage.get("pimusic.queue.v1:" + qLogin, null);
  if (!s || (s.v !== 1 && s.v !== 2) || !Array.isArray(s.items) || !s.items.length) return false;
  const pos = storage.get(pkey(), null) || {};
  const items = s.items.filter((x) => x && (typeof x.id === "number" || typeof x.id === "string") && x.title);
  if (!items.length) return false;
  for (const it of items) { if (!Number.isInteger(it.qid)) it.qid = ++qidN; qidN = Math.max(qidN, it.qid); }
  if (player.queue.length) return false; // уже что-то запустили, пока ждали /me
  player.queue = items;
  const byQid = pos.qid ? items.findIndex((x) => x.qid === pos.qid) : -1;
  player.index = byQid >= 0 ? byQid : Math.max(0, Math.min(items.length - 1, Number(s.index) || 0));
  player.time = Math.max(0, Number(byQid >= 0 ? pos.time : s.time) || 0);
  player.duration = items[player.index].seconds || 0;
  player.shuffle = !!s.shuffle;
  player.repeat = ["off", "all", "one"].includes(s.repeat) ? s.repeat : "off";
  base = Array.isArray(s.base) ? s.base.filter(Number.isInteger) : null;
  player.source = s.source && typeof s.source === "object" ? s.source : null;
  if (player.mode === "wave" && player.state !== "idle") { emit("queue"); return true; } // волна играет — очередь ждёт
  player.mode = "queue";
  player.desired = "paused";
  syncHandlers();
  emit("queue");
  emit("track");
  setState("paused");
  queueDirty = false;
  if (Date.now() - (Number(s.savedAt) || 0) <= STALE_MS) {
    // ссылки не хранятся — подтягиваем для текущего и ближайших (остальные — по мере игры)
    refresh(player.queue.slice(player.index, player.index + 20).map((x) => x.id));
  } else {
    const cur = player.queue[player.index];
    await refresh(player.queue.map((x) => x.id));
    // недоступные (удалённые из медиатеки, выход из плейлиста) — из очереди долой
    if (player.queue.some((x) => x.unavailable) && srcQid === 0) {
      player.queue = player.queue.filter((x) => !x.unavailable);
      if (!player.queue.length) { clearQueue(); return false; }
      const k = player.queue.indexOf(cur);
      if (k < 0) { player.index = 0; player.time = 0; } else player.index = k;
      emit("queue");
      emit("track");
      save();
    }
  }
  return true;
}

// ---------------------------------------------------------------- Media Session (экран блокировки, гарнитура)
const ms = "mediaSession" in navigator ? navigator.mediaSession : null;
const setAction = (a, fn) => { try { ms.setActionHandler(a, fn); } catch { /* действие не поддерживается */ } };
let handlersQ = null;
function syncHandlers() {
  if (!ms) return;
  const q = player.mode === "queue";
  if (q === handlersQ) return;
  handlersQ = q;
  setAction("previoustrack", q ? () => prev() : null);
  setAction("nexttrack", q ? () => next() : null);
  setAction("seekto", q ? (d) => { if (d && typeof d.seekTime === "number") seek(d.seekTime); } : null);
  setAction("seekbackward", q ? (d) => seek(audioTime() - ((d && d.seekOffset) || 10)) : null);
  setAction("seekforward", q ? (d) => seek(audioTime() + ((d && d.seekOffset) || 10)) : null);
  if (!q) { try { ms.setPositionState && ms.setPositionState(); } catch { /* */ } }
}
if (ms) {
  setAction("play", () => { if (player.mode === "queue") resume(); else play(); });
  setAction("pause", () => pause());
  setAction("stop", () => { if (player.mode === "queue") qPause(); else stop(); });
  syncHandlers();
}
let lastPos = 0;
function positionState(force) {
  if (!ms || !ms.setPositionState || player.mode !== "queue") return;
  const now = performance.now();
  if (!force && now - lastPos < 1000) return;
  lastPos = now;
  const d = player.duration;
  if (!(d > 0) || !isFinite(d)) return;
  try { ms.setPositionState({ duration: d, playbackRate: (audio && audio.playbackRate) || 1, position: Math.min(Math.max(0, player.time), d) }); } catch { /* */ }
}
let lastMeta = "";
function syncSession() {
  if (!ms) return;
  try {
    ms.playbackState = player.state === "playing" || player.state === "loading" || player.state === "reconnecting" ? "playing"
      : player.state === "idle" ? "none" : "paused";
    if (player.state === "idle") { ms.metadata = null; lastMeta = ""; return; }
    let meta;
    if (player.mode === "queue") {
      const t = current() || {};
      meta = { title: t.title || "PiMusic", artist: t.artist || "", album: t.album || (player.source && player.source.title) || "", cover: t.cover };
    } else {
      const n = player.now || {};
      meta = { title: n.title || "Моя Волна", artist: n.title ? (n.artist || "") : "PiMusic · в эфире", album: n.album || "Моя Волна", cover: n.cover };
    }
    const cover = meta.cover ? abs(meta.cover) : "";
    const key = [meta.title, meta.artist, meta.album, cover].join("|");
    if (key === lastMeta || typeof MediaMetadata === "undefined") return;
    lastMeta = key;
    ms.metadata = new MediaMetadata({ title: meta.title, artist: meta.artist, album: meta.album,
      artwork: cover ? [{ src: cover, sizes: "512x512", type: "image/jpeg" }] : [] });
  } catch { /* */ }
}
onPlayer((what) => { if (what === "track") syncSession(); });

// ---------------------------------------------------------------- сеть и видимость (волна)
window.addEventListener("online", () => {
  if (player.mode === "queue" && netWait && player.state === "error") { netWait = false; resume(); return; }
  if (player.mode === "wave" && player.desired === "playing" && (player.state === "reconnecting" || player.state === "error")) {
    clearTimers(); player.attempt = 0; setState("loading"); connect();
  }
});
window.addEventListener("offline", () => { if (player.mode === "wave" && player.desired === "playing" && player.state === "playing") reconnect(); });
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (player.desired === "playing") graph.resume();
  if (player.mode === "queue" && player.desired === "playing" && audio && audio.paused && srcQid) resume();
  if (player.mode === "wave" && player.desired === "playing") {
    poll();
    // вернулись после сворачивания, а звук стоит (iOS) — к живому краю
    if (audio && audio.paused && !retryTimer) { player.attempt = 0; setState("loading"); connect(); }
  }
});

// ---------------------------------------------------------------- Web Audio: анализатор для визуализации
// На iOS WebView MediaElementSource уводит звук в AudioContext, который система глушит в фоне, —
// там остаётся спокойная CSS-волна, а <audio> играет напрямую.
// Граф создаётся в жесте пользователя (play/resume); эквалайзер, нормализация, переходы и таймер сна — в нём.
export const analyserSupported = () => graph.graphWanted() && !reducedMotion();
export function ensureAudioGraph(gesture = false) {
  el();
  if (graph.ensureGraph(pair, { gesture })) return true;
  graph.resume();
  return false;
}
// Сторож: «играет», а контекст не запущен (система приостановила) дольше 1,5 с — просим тап, чтобы включить звук
let wdT = 0;
function watchdog() {
  clearTimeout(wdT);
  if (!graph.hasGraph()) return;
  wdT = setTimeout(() => {
    if (player.desired === "playing" && graph.hasGraph() && !graph.running()) { graph.resume(); emit("audio-blocked"); }
  }, 1500);
}
graph.onGraphState((st) => { if (st !== "running" && player.desired === "playing") { graph.resume(); watchdog(); } });
export function wakeAudio() { graph.resume(); }
export const getAnalyser = () => graph.getAnalyser();
export const hasAnalyser = () => graph.hasGraph();

// Для тестов и отладки
export const _audio = () => audio;
export const _pair = () => pair;

// ---------------------------------------------------------------- 30-секундные фрагменты (поиск, каталог)
// Отдельный лёгкий <audio> (чужой CDN — без Web Audio). Фрагмент ставит на паузу то, что играет (волну или
// очередь; само не продолжится — в мини-плеере видно «Пауза»), одновременно играет только один фрагмент.
const pvListeners = new Set();
export const preview = { url: "", state: "idle", progress: 0, title: "" }; // idle | loading | playing
export function onPreview(fn) { pvListeners.add(fn); return () => pvListeners.delete(fn); }
function pvEmit(what) { for (const fn of pvListeners) { try { fn(what, preview); } catch (e) { console.error(e); } } }
let pvAudio = null;
function pvEl() {
  if (pvAudio) return pvAudio;
  pvAudio = document.createElement("audio");
  pvAudio.preload = "none";
  pvAudio.hidden = true;
  pvAudio.setAttribute("aria-hidden", "true");
  try { pvAudio.volume = volGain(); } catch { /* */ }
  pvAudio.addEventListener("playing", () => { if (preview.url) { preview.state = "playing"; pvEmit("state"); } });
  pvAudio.addEventListener("timeupdate", () => {
    if (!preview.url) return;
    const d = isFinite(pvAudio.duration) && pvAudio.duration > 0 ? pvAudio.duration : 30;
    preview.progress = Math.min(1, pvAudio.currentTime / d);
    pvEmit("progress");
  });
  pvAudio.addEventListener("ended", () => stopPreview());
  pvAudio.addEventListener("error", () => { if (preview.url && pvAudio.getAttribute("src")) stopPreview("error"); });
  document.body.appendChild(pvAudio);
  return pvAudio;
}

export function playPreview(url, title = "") {
  if (!url) return;
  if (preview.url === url) { stopPreview(); return; }
  stopPreview();
  if (player.desired === "playing") pause();
  const a = pvEl();
  Object.assign(preview, { url, title, state: "loading", progress: 0 });
  pvEmit("state");
  a.src = url;
  Promise.resolve(a.play()).catch((e) => {
    if (preview.url !== url || (e && e.name === "AbortError")) return;
    stopPreview("error");
  });
}

export function stopPreview(reason) {
  if (!preview.url) return;
  Object.assign(preview, { url: "", title: "", state: "idle", progress: 0 });
  if (pvAudio) {
    try { pvAudio.pause(); } catch { /* */ }
    pvAudio.removeAttribute("src");
    try { pvAudio.load(); } catch { /* */ }
  }
  pvEmit(reason === "error" ? "error" : "state");
}

document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") stopPreview(); });
