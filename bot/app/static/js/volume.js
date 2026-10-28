// Громкость пользователя — только для мыши/десктопа (на телефоне — системная громкость): кнопка «Без звука»
// (значок по уровню: volume_off / volume_down / volume_up) и M3E-ползунок 0–100 %. Колесо мыши над блоком — ±5 %.
// Показ решает CSS (.vol скрыт без (hover: hover) и (pointer: fine)); значение — общее для всех копий (onVolume).
import { el, svg } from "./dom.js";
import { iconButton, slider } from "./ui.js";
import { getVolume, setVolume, toggleMute, onVolume } from "./player.js";

const pct = (v) => `${Math.round(v * 100)} %`;
// Значки громкости — SVG (Material Icons, Apache 2.0): volume_down/volume_off нет в подмножестве шрифта
// static/fonts (без сети оно не пересобирается); после пересборки можно вернуться к icon("volume_off").
const PATH = {
  up: "M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z",
  down: "M18.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM5 9v6h4l5 5V4L9 9H5z",
  off: "M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z",
};

export function volumeControl({ cls = "" } = {}) {
  const mute = iconButton("volume_up", "Без звука", { toggle: true, cls: "vol-mute", onClick: () => toggleMute() });
  const path = svg("path", { d: PATH.up });
  mute.replaceChildren(svg("svg", { class: "vol-ic", viewBox: "0 0 24 24", width: 24, height: 24, "aria-hidden": "true", focusable: "false" }, path));
  const s = slider({ label: "Громкость", min: 0, max: 1, step: 0.01, value: getVolume().level, format: pct, cls: "vol-slider",
    onInput: (v) => setVolume(v), onChange: (v) => setVolume(v) });
  const box = el("div", { class: "vol " + cls, role: "group", "aria-label": "Громкость" }, mute, s);
  const render = (st) => {
    const off = st.muted;
    path.setAttribute("d", off ? PATH.off : st.level < 0.5 ? PATH.down : PATH.up);
    // состояние передаёт aria-pressed при постоянной подписи; подсказка мыши — что сделает нажатие
    mute.setAttribute("aria-pressed", off ? "true" : "false");
    mute.title = off ? "Включить звук" : "Без звука";
    // «без звука» — ползунок показывает ноль, но уровень помнит (снимаем — вернётся прежний)
    s.set(off ? 0 : st.level);
    box.classList.toggle("is-muted", off);
  };
  // колесо — только над дорожкой; на границах (0 и 100 %) прокрутка страницы не перехватывается
  s.track.addEventListener("wheel", (ev) => {
    if (!ev.deltaY) return;
    const st = getVolume(), cur = st.muted ? 0 : st.level, up = ev.deltaY < 0;
    if ((up && cur >= 1) || (!up && cur <= 0)) return;
    ev.preventDefault();
    setVolume(cur + (up ? 0.05 : -0.05));
  }, { passive: false });
  const off = onVolume(render);
  render(getVolume());
  box.destroy = off;
  return box;
}
