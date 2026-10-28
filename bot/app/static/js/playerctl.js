// Настройки воспроизведения: нормализация громкости, плавный переход, скорость (ключ «player» в settings.js,
// синхронизируется между устройствами) и таймер сна (только здесь и сейчас). Листы управления — для «Сейчас играет»
// и экрана «Настройки».
import { el, icon, mount, setText } from "./dom.js";
import { sheet, list, listItem, slider, chip, switchRow, snackbar, announce } from "./ui.js";
import { haptic } from "./tg.js";
import * as settings from "./settings.js";
import { setPlayerOptions, playerOptions, setSleep, sleepState, onPlayer, player } from "./player.js";

const DEF = { normalize: true, crossfade: 0, speed: 1 };
export const opts = () => ({ ...DEF, ...(settings.get("player", {}) || {}) });
export function setOpt(k, v) { settings.set("player", { ...opts(), [k]: v }); }
export function initPlayerOptions() {
  setPlayerOptions(opts());
  settings.onSetting((key) => { if (key === "player") setPlayerOptions(opts()); });
}

export const fmtSpeed = (v) => `${String(Math.round(v * 100) / 100).replace(".", ",")}×`;
export const fmtXf = (v) => (v > 0 ? `${String(v).replace(".", ",")} с` : "Без паузы");
export function fmtSleep(s = sleepState()) {
  if (!s) return "Выкл.";
  if (s.mode === "track") return "После этого трека";
  const m = Math.floor(s.left / 60000), sec = Math.floor((s.left % 60000) / 1000);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function toggleNormalize() {
  const v = !opts().normalize;
  setOpt("normalize", v);
  haptic("select");
  snackbar(v ? "Нормализация громкости включена" : "Нормализация громкости выключена");
}

// ---------------------------------------------------------------- листы
export function sleepSheet(scope) {
  sheet({ title: "Таймер сна", label: "Таймер сна", scope, cls: "sheet-ctl", build(body, close) {
    const items = [[null, "Выключен"], [15, "15 минут"], [30, "30 минут"], [45, "45 минут"], [60, "1 час"], ["track", "До конца трека"]];
    const cur = sleepState();
    const curKey = !cur ? null : cur.mode === "track" ? "track" : "time";
    const status = el("p", { class: "ctl-status", "aria-live": "polite" });
    const upd = () => setText(status, cur || sleepState() ? `Сейчас: ${fmtSleep()}` : "Музыка затихнет плавно за несколько секунд до конца.");
    upd();
    body.append(status, list(items.map(([v, label]) => {
      const on = v === null ? !curKey : v === "track" ? curKey === "track" : false;
      return listItem({ leading: icon(v === "track" ? "music_note" : v === null ? "timer_off" : "bedtime"), headline: label, lines: 1, cls: on ? "is-on" : "",
        trailing: on ? icon("check", { class: "ctl-check" }) : null,
        onClick: () => { setSleep(v); haptic("success"); close(); announce(v === null ? "Таймер сна выключен" : `Таймер сна: ${label}`); } });
    }), "sheet-list"));
    const off = onPlayer((w) => { if (!body.isConnected) { off(); return; } if (w === "sleep") upd(); });
  } });
}

export function speedSheet(scope) {
  sheet({ title: "Скорость", label: "Скорость воспроизведения", scope, cls: "sheet-ctl", build(body) {
    const s = slider({ label: "Скорость", min: 0.5, max: 2, step: 0.05, value: opts().speed, format: fmtSpeed,
      onInput: (v) => setPlayerOptions({ speed: v }), onChange: (v) => setOpt("speed", v) });
    const chips = el("div", { class: "ctl-chips" }, [0.75, 1, 1.25, 1.5, 2].map((v) => chip({ label: fmtSpeed(v), variant: "filter", selected: opts().speed === v,
      onClick: () => { haptic("select"); setOpt("speed", v); s.set(v); for (const c of chips.children) c.setAttribute("aria-pressed", c.textContent === fmtSpeed(v) ? "true" : "false"); } })));
    body.append(el("div", { class: "sheet-pad" }, s, chips, el("p", { class: "ctl-status", text: "Высота голоса сохраняется. Для «Моей Волны» скорость всегда обычная." })));
  } });
}

export function crossfadeSheet(scope) {
  sheet({ title: "Плавный переход", label: "Плавный переход между треками", scope, cls: "sheet-ctl", build(body) {
    const s = slider({ label: "Длительность", min: 0, max: 12, step: 1, value: opts().crossfade, format: fmtXf, onChange: (v) => setOpt("crossfade", v) });
    body.append(el("div", { class: "sheet-pad" }, s, el("p", { class: "ctl-status", text: "0 — без паузы между треками (следующий загружается заранее). Больше нуля — треки плавно перетекают друг в друга." })));
  } });
}

// переключатели для экрана «Настройки»
export function normalizeRow() {
  return switchRow({ label: "Нормализация громкости", supporting: "Треки звучат одинаково громко (по анализу сервера, цель −14 LUFS)",
    checked: opts().normalize, onChange: (v) => setOpt("normalize", v) });
}

export { player, playerOptions, mount };
