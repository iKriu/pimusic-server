// Обновление приложения Windows (мост PiMusicNative, features.updates): M3 basic dialog «Доступна версия X»
// с заметками «Что нового», «Позже» (dismissUpdate) / «Обновить» (installUpdate), линейный прогресс загрузки,
// ошибка — текст и «Повторить». Без моста (браузер, Telegram, Android) — ничего не делает.
// Контракт — docs/design/APPS_API.md, раздел «Обновления».
import { el } from "./dom.js";
import { dialog, wavyLinear, snackbar, announce } from "./ui.js";
import { haptic } from "./tg.js";
import { bridge } from "./native.js";

export const updatesSupported = () => { const n = bridge(); return !!(n && n.features && n.features.updates && typeof n.on === "function"); };

let inited = false, open = false;
export function initUpdates() {
  if (inited || !updatesSupported()) return;
  inited = true;
  // подписка сообщает оболочке, что диалог рисуем сами (иначе — системный)
  try { bridge().on("update", (u) => { if (u && u.available !== false && !u.installing) showUpdate(u); return true; }); } catch (e) { console.warn("PiMusicNative:", e && e.message); }
}

// Ручная проверка («Настройки → Приложение»). busy(on) — показать состояние «проверяю» у пункта
export async function checkForUpdates(busy = () => {}) {
  const n = bridge();
  if (!updatesSupported()) return;
  busy(true);
  let r;
  try { r = await n.checkUpdate(); } catch (e) {
    haptic("error");
    snackbar(`Не удалось проверить обновления: ${(e && e.message) || "нет связи"}`, { error: true });
    return;
  } finally { busy(false); }
  if (r && r.installing) { snackbar("Обновление уже устанавливается"); return; }
  if (r && r.available) { showUpdate(r); return; }
  haptic("success");
  snackbar(`Установлена последняя версия${r && r.currentVersion ? ` — ${r.currentVersion}` : ""}`);
}

export function showUpdate(u) {
  const n = bridge();
  if (open || !n) return;
  open = true;
  const version = u.version || "";
  const current = u.currentVersion || u.current || "";
  const notes = String(u.notes || "").trim();
  const progress = wavyLinear({ value: null, label: "Загрузка обновления" });
  const status = el("p", { class: "upd-status", "aria-live": "polite" });
  // место под прогресс зарезервировано (visibility), чтобы диалог не «прыгал» при старте загрузки
  const progBox = el("div", { class: "upd-progress is-idle", "aria-hidden": "true" }, progress, status);
  let installing = false;
  const showProg = (on) => { progBox.classList.toggle("is-idle", !on); if (on) progBox.removeAttribute("aria-hidden"); else progBox.setAttribute("aria-hidden", "true"); };
  const content = el("div", { class: "upd" },
    current ? el("p", { class: "upd-current", text: `Установлена ${current}` }) : null,
    notes ? el("h3", { class: "upd-h", text: "Что нового" }) : null,
    notes ? el("div", { class: "upd-notes" }, el("p", { text: notes })) : null,
    el("p", { class: "upd-note", text: "PiMusic закроется на время установки и запустится снова." }),
    progBox);
  let offs = [];
  const cleanup = () => { for (const f of offs) { try { f(); } catch { /* */ } } offs = []; };
  const okBtn = () => content.closest("dialog") && content.closest("dialog").querySelector(".dlg-actions .btn:last-child");
  const laterBtn = () => content.closest("dialog") && content.closest("dialog").querySelector(".dlg-actions .btn:first-child");
  const setInstalling = (on) => { installing = on; const b = laterBtn(); if (b) b.setAttribute("aria-disabled", on ? "true" : "false"); };
  const setOkLabel = (t) => { const b = okBtn(); const l = b && b.querySelector(".btn-label"); if (l) l.textContent = t; };
  // «Обновить» / «Повторить»: ждём ошибку (успех — оболочка закроет приложение сама)
  const install = () => new Promise((resolve) => {
    showProg(true);
    setInstalling(true);
    progress.set(null);
    status.textContent = "Загрузка…";
    let done = false;
    const fin = (msg) => { if (done) return; done = true; cleanup(); showProg(false); setInstalling(false); setOkLabel("Повторить"); haptic("error"); resolve(msg || "Не удалось обновить"); };
    offs.push(n.on("updateProgress", (p) => {
      const pct = p && Number.isFinite(p.percent) ? p.percent : p && p.total ? (p.downloaded / p.total) * 100 : null;
      progress.set(pct === null ? null : pct / 100);
      status.textContent = pct === null ? "Загрузка…" : pct >= 100 ? "Устанавливается…" : `Загрузка… ${Math.round(pct)} %`;
    }));
    offs.push(n.on("updateError", (e) => fin(`Ошибка обновления: ${(e && e.message) || "неизвестно"}`)));
    Promise.resolve().then(() => n.installUpdate()).catch((e) => fin(`Ошибка обновления: ${(e && e.message) || "неизвестно"}`));
  });
  announce(`Доступна версия ${version}`);
  dialog({
    icon: "system_update", headline: `Доступна версия ${version}`, content,
    actions: [{ label: "Позже", value: false }, { label: "Обновить", value: true }],
    onConfirm: install,
    spinner: false, // один индикатор — линейный прогресс в содержимом
    locked: () => installing, // во время установки «Позже», Esc и тап по фону не закрывают
  }).then((ok) => {
    open = false;
    cleanup();
    if (!ok) Promise.resolve().then(() => n.dismissUpdate()).catch(() => {}); // «Позже»: до перезапуска не спрашивать
  });
}
