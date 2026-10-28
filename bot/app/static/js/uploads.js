// Загрузка своих файлов: выбор файлов / перетаскивание, отправка по одному через XHR (прогресс по файлам),
// список загрузок с сервера (/api/uploads) с состояниями, карточка «ждёт названия» (need_info) с формой.
// Опрос — каждые 2,5 с, пока что-то в работе; затем останавливается.
import { el, icon, mount, setText, fmtTime, safeUrl, nTracks } from "./dom.js";
import { api, getToken, setToken, inTelegram, INIT_HEADER, API_BASE } from "./api.js";
import { tg, haptic } from "./tg.js";
import { bridge } from "./native.js";
import { button, iconButton, list, listItem, snackbar, textField, wavyCircular, announce, dialog } from "./ui.js";
import { playQueue } from "./player.js";
import * as store from "./store.js";
import { go, shell } from "./shell.js";
import { markLibraryDirty } from "./screens/library.js";
import { invalidateGenres } from "./libsort.js";
import { invalidateIndex } from "./libtrack.js";

export const ACCEPT = "audio/*,.mp3,.flac,.wav,.m4a,.aac,.ogg,.oga,.opus,.wma,.aiff,.aif,.alac,.ape,.wv,.txt,.csv,.tsv,text/plain,text/csv,text/tab-separated-values";
// списки треков (Tune My Music, Exportify, «Исполнитель — Название») — не в /api/upload, а в импорт
const isList = (f) => /\.(txt|csv|tsv)$/i.test(f.name) || /^text\/(plain|csv|tab-separated-values)$/.test(f.type);
const isAudio = (f) => /^audio\//.test(f.type) || /\.(mp3|flac|wav|m4a|aac|ogg|oga|opus|wma|aiff?|alac|ape|wv)$/i.test(f.name);
const MAX_FILES = 50;
const ACTIVE = new Set(["receiving", "queued", "analyzing", "processing"]);
const STATE = { receiving: "получаю", queued: "в очереди", analyzing: "опознаю", need_info: "ждёт названия", processing: "сохраняю",
  done: "сохранён", duplicate: "уже был — добавлен в медиатеку", failed: "не удалось", cancelled: "отменён" };

// ---------------------------------------------------------------- отправка
const local = []; // { key, name, size, loaded, state: "sending"|"error"|"sent", error }
const subs = new Set();
const emit = () => { for (const fn of subs) { try { fn(); } catch (e) { console.error(e); } } };
export const onUploads = (fn) => { subs.add(fn); return () => subs.delete(fn); };
let n = 0, running = 0;
const drafts = new Map(); // id загрузки → {artist, title, album}: введённое не теряется при обновлении списка

// один <input type=file> на всё приложение (не плодим сирот при каждом нажатии)
let fileInput = null;
export function pickFiles() {
  if (!fileInput) {
    fileInput = el("input", { type: "file", multiple: true, accept: ACCEPT, hidden: true, "aria-hidden": "true", tabindex: "-1" });
    fileInput.addEventListener("change", () => { if (fileInput.files && fileInput.files.length) uploadFiles([...fileInput.files]); fileInput.value = ""; });
    document.body.appendChild(fileInput);
  }
  fileInput.click();
}

export function uploadFiles(files) {
  const lists = files.filter((f) => isList(f) && !isAudio(f));
  for (const f of lists.slice(0, 5)) importList(f);
  const audio = files.filter(isAudio);
  const skipped = files.length - audio.length - lists.length;
  if (!audio.length) {
    if (!lists.length) { haptic("warning"); snackbar("Не подходит: нужен аудиофайл или список TXT/CSV", { error: true }); }
    return;
  }
  const take = audio.slice(0, MAX_FILES);
  for (const f of take) local.unshift({ key: "l" + ++n, file: f, name: f.name, size: f.size, loaded: 0, state: "waiting" });
  haptic("medium");
  snackbar(`Загружаю: ${take.length} ${take.length === 1 ? "файл" : take.length < 5 ? "файла" : "файлов"}` + (skipped ? ` · пропущено не-аудио: ${skipped}` : "") + (audio.length > MAX_FILES ? ` · за раз не больше ${MAX_FILES}` : ""),
    shell.tab === "downloads" ? {} : { action: "Загрузки", onAction: () => go("downloads") });
  emit();
  pump();
}

function pump() {
  while (running < 2) {
    const it = local.find((x) => x.state === "waiting" && !(x.notBefore > Date.now()));
    if (!it) return;
    running++;
    send(it).finally(() => { running--; pump(); });
  }
}

function fail(it, msg) { it.state = "error"; it.error = msg; emit(); }

// Список треков → POST /api/import/file (multipart, поле file); ход импорта — карточка импорта в «Загрузках»
async function importList(f) {
  const it = { key: "l" + ++n, file: f, name: f.name, size: f.size, loaded: 0, state: "sending", list: true };
  local.unshift(it);
  emit();
  if (f.size > 2 * 1048576) { haptic("error"); fail(it, "не удалось · список больше 2 МБ — разделите его на части"); return; }
  const fd = new FormData();
  fd.append("file", f, f.name);
  try {
    const r = await api("/import/file", { method: "POST", body: fd });
    it.state = "sent";
    it.file = null;
    local.splice(local.indexOf(it), 1); // дальше — карточка импорта
    emit();
    haptic("success");
    const pl = r.playlist && r.playlist.id ? r.playlist : null;
    // коротко (≤ 2 строк): сколько и куда; создан плейлист — предложить открыть его, иначе — к ходу импорта
    const dest = pl ? pl.name : r.name || f.name.replace(/\.[^.]+$/, "");
    snackbar(r.total ? `Импорт начат: ${nTracks(r.total)} → «${dest}»` : `Импорт «${dest}» начат`,
      pl ? { action: "Открыть плейлист", onAction: () => { go("library"); import("./screens/playlist.js").then((m) => m.openPlaylist(pl.id, { name: pl.name })); } }
        : shell.tab === "downloads" ? {} : { action: "Загрузки", onAction: () => go("downloads") });
    if (pl) import("./playlists.js").then((m) => m.changed(pl.id, "create")).catch(() => {});
    store.kick();
  } catch (e) {
    haptic("error");
    // текст ошибки сервера (message) — как есть; 404 — старый сервер без импорта файлов
    fail(it, "не удалось · " + (e.status === 404 && e.code !== "not_found" ? "сервер пока не принимает списки треков" : e.message || "импорт не начался"));
  }
}

function send(it) {
  return new Promise((resolve) => {
    it.state = "sending";
    it.loaded = 0;
    emit();
    const fd = new FormData();
    fd.append("file", it.file, it.name);
    const x = new XMLHttpRequest();
    x.open("POST", API_BASE + "/api/upload");
    if (inTelegram()) x.setRequestHeader(INIT_HEADER, tg.initData);
    else if (getToken()) x.setRequestHeader("Authorization", "Bearer " + getToken());
    // не меньше 10 минут; медленный канал — из расчёта 20 КБ/с
    x.timeout = Math.max(10 * 60e3, Math.round((it.size || 0) / 20e3) * 1000);
    let last = 0;
    x.upload.onprogress = (e) => { if (e.lengthComputable) { it.loaded = e.loaded; it.size = e.total; if (performance.now() - last > 150) { last = performance.now(); emit(); } } };
    x.onload = () => {
      let r = {};
      try { r = JSON.parse(x.responseText || "{}"); } catch { /* */ }
      if (x.status >= 200 && x.status < 300 && r.uploads && r.uploads.length) {
        it.state = "sent";
        it.file = null;
        it.ids = r.uploads.map((u) => u.id);
        emit();
      } else if ((x.status === 429 || x.status === 503) && (it.tries || 0) < 3) {
        // сервер занят — повторяем с паузой 3–5 с
        it.tries = (it.tries || 0) + 1;
        it.state = "waiting";
        const delay = 3000 + it.tries * 1000;
        it.notBefore = Date.now() + delay;
        emit();
        setTimeout(pump, delay + 50);
      } else if (x.status === 401 && !inTelegram() && getToken()) {
        setToken("");
        window.dispatchEvent(new CustomEvent("pimusic:auth-lost", { detail: { reason: "Вход на этом устройстве больше не действует. Войдите снова." } }));
        fail(it, "Нужно войти заново");
      } else {
        const rej = r.rejected && r.rejected[0];
        fail(it, (rej && rej.message) || r.message || (x.status === 413 ? "Файл слишком большой" : x.status === 503 ? "Загрузка файлов сейчас недоступна" : `Ошибка ${x.status}`));
      }
      refresh();
      resolve();
    };
    // обрыв посреди отправки — чаще всего сервер закрыл соединение (лимит размера, квота, место)
    x.onerror = () => { fail(it, it.loaded > 0 ? "Сервер отклонил файл (размер, квота или место на диске)" : "Нет связи с сервером"); if (it.loaded > 0) refresh(); resolve(); };
    x.ontimeout = () => { fail(it, "Слишком долго — проверьте связь и повторите"); resolve(); };
    x.onabort = () => { fail(it, "Отменено"); resolve(); };
    it.xhr = x;
    try { x.send(fd); } catch (e) { fail(it, (e && e.message) || "Не удалось отправить"); resolve(); }
  });
}

// ---------------------------------------------------------------- список с сервера
// Опрос списка: пока что-то в работе — независимо от открытой вкладки (готовые треки попадают в медиатеку
// без «Загрузок»), с нарастающей паузой; один запрос за раз (повторный вызов во время запроса — ещё один круг).
let server = [], loaded = false, failedLoad = null, pollT = 0, watchers = 0, fetching = false, again = false, idle = 0, lastSig = "";
const seenDone = new Set();
const seenNeed = new Set();
let doneInit = false;
export async function refresh() {
  if (fetching) { again = true; return; }
  fetching = true;
  clearTimeout(pollT);
  let ok = false;
  try { const r = await api("/uploads"); server = r.items || []; loaded = true; failedLoad = null; ok = true; } catch (e) { failedLoad = e; }
  fetching = false;
  emit();
  const busy = server.some((u) => ACTIVE.has(u.state)) || local.some((x) => x.state === "sending" || x.state === "waiting");
  // готовые → медиатека изменилась (первый удачный ответ — только запоминаем, без снекбаров)
  if (ok) {
    const done = server.filter((u) => (u.state === "done" || u.state === "duplicate") && !seenDone.has(u.id));
    if (doneInit && done.length) {
      for (const u of done.slice(0, 3)) snackbar(`Сохранён: ${u.artist ? u.artist + " — " : ""}${u.title || u.name}`);
      if (done.length > 3) snackbar(`Сохранено ещё: ${done.length - 3}`);
      markLibraryDirty();
      invalidateGenres();
      invalidateIndex();
      store.loadKeys();
    }
    for (const u of done) seenDone.add(u.id);
    // «ждёт названия»: сообщения в чат из приложения больше нет — снекбар и бейдж на вкладке «Загрузки»
    const need = server.filter((u) => u.state === "need_info");
    for (const u of need) {
      if (seenNeed.has(u.id)) continue;
      seenNeed.add(u.id);
      if (doneInit || shell.tab !== "downloads") snackbar(`Не опознан «${u.name}» — подскажите исполнителя и название`, shell.tab === "downloads" ? {} : { action: "Загрузки", onAction: () => go("downloads") });
    }
    if (store.state.needInfo !== need.length) { store.state.needInfo = need.length; window.dispatchEvent(new CustomEvent("pimusic:need-info")); }
    doneInit = true;
  }
  const sig = server.map((u) => u.id + u.state).join(",");
  idle = sig === lastSig ? idle + 1 : 0;
  lastSig = sig;
  if (again) { again = false; pollT = setTimeout(refresh, 300); return; }
  if (busy) pollT = setTimeout(refresh, Math.min(15000, 2500 * Math.pow(1.5, Math.min(idle, 5)))); // ничего не меняется — реже
  else if (watchers && !loaded) pollT = setTimeout(refresh, 8000);
}

// ---------------------------------------------------------------- UI
const fmtSize = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1).replace(".", ",")} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`);
const fmtDur = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "");

// Подсказка из имени файла: «Исполнитель - Название», «01. Исполнитель — Название»
export function guessFromName(name) {
  const stem = String(name || "").replace(/\.[a-z0-9]{2,5}$/i, "").replace(/_/g, " ").replace(/^\s*\d{1,3}[\s.\-)]+/, "").trim();
  const m = stem.split(/\s+[-–—]\s+/);
  return m.length >= 2 ? { artist: m[0].trim(), title: m.slice(1).join(" — ").trim() } : { artist: "", title: stem };
}

export function uploadButton({ variant = "tonal", label = "Загрузить файлы" } = {}) {
  return button({ label, icon: "upload_file", variant, cls: "up-btn", onClick: () => pickFiles() });
}

// Перетаскивание файлов на элемент (широкие экраны / мышь): весь экран-приёмник подсвечивается (M3: обводка primary
// + scrim), бросок вне приёмника гасится, чтобы браузер не открыл файл вместо приложения.
let guarded = false;
const hasFiles = (ev) => !!(ev.dataTransfer && [...(ev.dataTransfer.types || [])].includes("Files"));
function guardWindow() {
  if (guarded) return;
  guarded = true;
  window.addEventListener("dragover", (ev) => { if (hasFiles(ev) && !ev.defaultPrevented) { ev.preventDefault(); ev.dataTransfer.dropEffect = "none"; } });
  window.addEventListener("drop", (ev) => { if (hasFiles(ev)) ev.preventDefault(); });
}
// Windows-оболочка (Tauri) перехватывает перетаскивание сама — подсказку показываем, только если оболочка его пропускает
export const dropSupported = () => { const n = bridge(); return !(n && n.platform === "windows" && !(n.features && n.features.fileDrop)); };
export function dropTarget(node) {
  guardWindow();
  node.appendChild(el("div", { class: "drop-ov", "aria-hidden": "true" },
    el("span", { class: "drop-pill" }, icon("upload_file"), el("span", { text: "Отпустите, чтобы загрузить" }))));
  // Подсветку гасим по тишине dragover (браузер шлёт его каждые ~50 мс, пока файл над приёмником), а не счётчиком
  // dragenter/dragleave: список загрузок перерисовывается во время перетаскивания, и у удалённых узлов dragleave
  // не всплывает — счётчик «застревал», экран оставался под подложкой «Отпустите…», а dragend из Проводника не приходит.
  let quiet = 0;
  const off = () => { clearTimeout(quiet); node.classList.remove("is-drop"); };
  const on = () => { node.classList.add("is-drop"); clearTimeout(quiet); quiet = setTimeout(off, 400); };
  node.addEventListener("dragenter", (ev) => { if (!hasFiles(ev)) return; ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; on(); });
  node.addEventListener("dragover", (ev) => { if (!hasFiles(ev)) return; ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; on(); });
  node.addEventListener("dragleave", (ev) => {
    if (!hasFiles(ev)) return;
    // ушли за пределы приёмника (relatedTarget — куда); без relatedTarget — решит таймер тишины
    if (ev.relatedTarget && !node.contains(ev.relatedTarget)) off();
  });
  node.addEventListener("drop", (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.stopPropagation();
    off();
    const files = [...ev.dataTransfer.files];
    if (files.length) uploadFiles(files);
  });
  window.addEventListener("dragend", off);
  window.addEventListener("blur", off);
}

export function uploadsPanel() {
  const head = el("div", { class: "up-head" },
    el("div", { class: "up-head-text" }, el("h2", { class: "section-title", text: "Свои файлы" }),
      el("p", { class: "up-hint", text: "Аудио (mp3, flac, wav, m4a, ogg — до 300 МБ): сервер сам найдёт теги и обложку. Или список треков TXT/CSV: сервер найдёт и скачает треки." })),
    uploadButton({ variant: "filled" }));
  // зона для мыши: бросить файлы или щёлкнуть — выбор файлов (для клавиатуры и чтения с экрана — кнопка «Загрузить файлы»)
  const drop = dropSupported() ? el("div", { class: "up-drop", "aria-hidden": "true", onclick: () => pickFiles() },
    icon("upload_file"), el("span", { text: "Перетащите сюда аудио или список треков TXT/CSV или щёлкните, чтобы выбрать" })) : null;
  const box = el("div", { class: "up-list" });
  const root = el("section", { class: "up", "aria-label": "Загрузка своих файлов" }, head, drop, box);
  let openForm = null; // id загрузки с раскрытой формой (не перерисовывать под пальцами)

  function render() {
    const rows = [];
    // свои отправки (ещё не на сервере или с ошибкой)
    for (const it of local) {
      if (it.state === "sent" && server.some((u) => it.ids && it.ids.includes(u.id))) continue;
      const pct = it.size ? it.loaded / it.size : 0;
      rows.push(listItem({
        leading: it.state === "error" ? el("span", { class: "up-ic is-error" }, icon("error")) : wavyCircular({ value: it.state === "sending" && !it.list ? pct : null, size: 40, label: `Отправка ${it.name}` }),
        headline: it.name,
        supporting: it.state === "error" ? it.error : it.list ? "список треков · отправляю" : it.state === "waiting" ? "ждёт отправки" : it.state === "sent" ? "отправлен" : `отправка · ${Math.round(pct * 100)}% · ${fmtSize(it.loaded)} из ${fmtSize(it.size)}`,
        trailing: it.list && it.state === "sending" ? null : it.state === "sending" || it.state === "waiting" ? iconButton("close", `Отменить отправку ${it.name}`, { onClick: () => { if (it.xhr) it.xhr.abort(); it.state = "error"; it.error = "Отменено"; render(); } })
          : it.state === "error" ? el("span", { class: "up-err-btns" },
            it.file ? iconButton("refresh", `Повторить отправку: ${it.name}`, { onClick: () => {
              if (it.list) { local.splice(local.indexOf(it), 1); importList(it.file); return; }
              it.state = "waiting"; it.error = ""; it.tries = 0; emit(); pump();
            } }) : null,
            iconButton("close", `Убрать из списка: ${it.name}`, { onClick: () => { local.splice(local.indexOf(it), 1); render(); } })) : null,
        cls: "up-row is-" + it.state, lines: 2,
      }));
    }
    for (const u of server) {
      if (u.state === "need_info") { rows.push(needInfo(u)); continue; }
      const t = u.track;
      const lead = ACTIVE.has(u.state) ? wavyCircular({ value: null, size: 40, label: STATE[u.state] })
        : t && t.cover ? el("img", { class: "up-cover", src: safeUrl(t.cover), alt: "", loading: "lazy" })
          : el("span", { class: "up-ic is-" + u.state }, icon(u.state === "failed" ? "error" : u.state === "cancelled" ? "block" : u.state === "duplicate" ? "library_add_check" : "check_circle", { fill: u.state === "done" }));
      const title = t ? `${t.artist} — ${t.title}` : u.artist && u.title ? `${u.artist} — ${u.title}` : u.name;
      const sup = [u.state_text || STATE[u.state] || u.state, u.duration ? fmtDur(u.duration) : "", u.note || "", t ? "" : u.created_at ? fmtTime(u.created_at) : ""].filter(Boolean).join(" · ");
      rows.push(listItem({ leading: lead, headline: title, supporting: sup, lines: 2, cls: "up-row is-" + u.state,
        onClick: t ? () => { haptic("light"); playQueue([t], 0, { source: { kind: "upload", id: u.id, title: "Загрузки" } }); } : undefined,
        label: t ? `Слушать: ${title}` : undefined }));
    }
    if (!rows.length) {
      mount(box, failedLoad && failedLoad.status !== 404 ? el("p", { class: "note", text: failedLoad.message })
        : failedLoad ? el("p", { class: "note", text: "Сервер пока не принимает файлы." }) : loaded ? el("p", { class: "note up-empty", text: "Загруженных файлов пока нет." }) : null);
      return;
    }
    // форма need_info под пальцами не пересоздаётся
    const keep = openForm ? box.querySelector(`[data-up="${openForm}"]`) : null;
    if (keep && keep.contains(document.activeElement)) {
      const fresh = rows.find((r) => r.dataset && r.dataset.up === String(openForm));
      if (fresh) rows[rows.indexOf(fresh)] = keep;
    }
    mount(box, list(rows, "up-items"));
  }

  function needInfo(u) {
    const g = guessFromName(u.name);
    const d = drafts.get(u.id) || {};
    const fa = textField({ label: "Исполнитель", value: d.artist ?? (u.artist || g.artist), maxlength: 200 });
    const ft = textField({ label: "Название", value: d.title ?? ((u.title && u.title !== u.name.replace(/\.[^.]+$/, "") ? u.title : "") || g.title), maxlength: 300 });
    const fal = textField({ label: "Альбом (необязательно)", value: d.album ?? (u.album || ""), maxlength: 300 });
    const keep = () => drafts.set(u.id, { artist: fa.input.value, title: ft.input.value, album: fal.input.value });
    for (const f of [fa, ft, fal]) f.input.addEventListener("input", keep);
    const err = el("p", { class: "dlg-error", role: "alert" });
    const act = async (path, body, btn) => {
      btn.disabled = true;
      try {
        await api(`/uploads/${u.id}/${path}`, { method: "POST", body: body || {} });
        haptic("success");
        announce(path === "cancel" ? `Загрузка «${u.name}» отменена` : `«${u.name}»: сохраняю`);
        openForm = null;
        drafts.delete(u.id);
        refresh();
      } catch (e) { btn.disabled = false; err.textContent = e.message; haptic("error"); }
    };
    const save = button({ label: "Сохранить", icon: "check", variant: "filled", onClick: () => {
      const a = fa.input.value.trim(), t = ft.input.value.trim();
      if (!a || !t) { err.textContent = !a ? "Укажите исполнителя" : "Укажите название"; (!a ? fa : ft).input.focus(); return; }
      act("info", { artist: a, title: t, album: fal.input.value.trim() }, save);
    } });
    const skip = button({ label: "Оставить как есть", variant: "text", onClick: () => act("skip", null, skip) });
    const cancel = button({ label: "Отмена", variant: "text", cls: "btn-danger-text", onClick: async () => {
      const ok = await dialog({ icon: "block", headline: "Отменить загрузку?", body: `«${u.name}» не попадёт в медиатеку, файл будет удалён.`, actions: [{ label: "Нет", value: false }, { label: "Отменить загрузку", value: true, variant: "danger" }] });
      if (ok) act("cancel", null, cancel);
    } });
    const card = el("li", { class: "up-need", "data-up": String(u.id) },
      el("div", { class: "up-need-head" }, el("span", { class: "up-ic is-need_info" }, icon("help", { fill: true })),
        el("div", { class: "li-text" }, el("span", { class: "li-headline", text: u.name }),
          el("span", { class: "li-supporting", text: `Не смог опознать${u.duration ? " · " + fmtDur(u.duration) : ""} — подскажите, что это` }))),
      el("div", { class: "up-form" }, fa, ft, fal, err),
      el("div", { class: "up-actions" }, cancel, skip, save));
    card.addEventListener("focusin", () => { openForm = u.id; });
    return card;
  }

  const off = onUploads(() => { if (root.isConnected || !started) render(); });
  let started = false;
  return {
    el: root,
    start() { started = true; watchers++; refresh(); },
    stop() { watchers = Math.max(0, watchers - 1); },
    destroy: off,
  };
}
