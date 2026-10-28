// Эквалайзер: модель, пресеты, синхронизация по устройствам.
// Ключи настроек (settings.js):
//   eq.presets          — общая библиотека своих пресетов [{id, name, preamp, bands}]
//   eq.shared           — общий активный профиль {active, preamp, bands}
//   eq.device.<id>      — устройство {name, platform, mode: "sync"|"local", active, preamp, bands, enabled, limiter, spectrum,
//                          output: "default"} (output — задел под профили по выходу звука, сейчас всегда default)
// Режим «sync» — устройство следует eq.shared; «local» — свой активный пресет и свои полосы.
import * as settings from "./settings.js";
import * as graph from "./audiograph.js";
import { deviceId, deviceIdPersistent, platform, defaultName } from "./device.js";

export const FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
export const LIMITS = { f: [20, 20000], g: [-12, 12], q: [0.3, 8], preamp: [-12, 6] };
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

export const band = (f, g = 0, q = 1.0, type = "peaking") => ({ type, f, g, q });
export const flatBands = () => FREQS.map((f) => band(f, 0, 1.0));

// встроенные пресеты: усиления полос 31…16k
const B = (gains) => FREQS.map((f, i) => band(f, gains[i], 1.0));
const P = (id, name, gains) => {
  const max = Math.max(0, ...gains);
  return { id, name, builtin: true, preamp: -Math.round(max * 0.5 * 2) / 2, bands: B(gains) };
};
export const BUILTIN = [
  P("flat", "Ровно", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
  P("bass", "Бас+", [6, 5, 4, 2, 0.5, 0, 0, 0, 0, 0]),
  P("treble", "Высокие+", [0, 0, 0, 0, 0, 0.5, 2, 4, 5, 6]),
  P("vocal", "Вокал", [-2, -2, -1, 0, 2, 3, 3, 2, 0, -1]),
  P("rock", "Рок", [4, 3, 2, 0, -1, -1, 1, 2.5, 3.5, 4]),
  P("electro", "Электроника", [5, 4, 2, 0, -2, 0, 1, 2, 4, 5]),
  P("classic", "Классика", [3, 2, 1, 0, 0, 0, -1, -1, 1, 2]),
  P("podcast", "Подкаст", [-6, -4, -2, 0, 2, 3, 3, 2, 0, -2]),
  P("warm", "Наушники · тёплые", [3, 3, 2, 1, 0, 0, -1, -2, -2, -3]),
  P("night", "Громкость ночью", [-4, -3, -1, 0, 1, 2, 2, 1, 0, -2]),
];

const DEV = () => "eq.device." + deviceId();
const defaultsDev = () => ({
  name: defaultName(), platform: platform(), mode: "sync", active: "flat", preamp: 0, bands: flatBands(),
  enabled: !graph.iOS, limiter: true, spectrum: true, output: "default",
});

function sanitizeBands(bands) {
  if (!Array.isArray(bands) || bands.length !== FREQS.length) return flatBands();
  return bands.map((b, i) => ({
    type: ["peaking", "lowshelf", "highshelf"].includes(b && b.type) && (i === 0 || i === FREQS.length - 1 || b.type === "peaking") ? b.type : "peaking",
    f: clamp(Number(b && b.f) || FREQS[i], ...LIMITS.f),
    g: clamp(Math.round((Number(b && b.g) || 0) * 10) / 10, ...LIMITS.g), // шаг 0,1 дБ (старые 0,5 — без изменений)
    q: clamp(Number(b && b.q) || 1, ...LIMITS.q),
  }));
}

// ---------------------------------------------------------------- чтение
export function device(id = deviceId()) {
  const key = "eq.device." + id;
  const v = settings.get(key, null);
  if (!v && id === deviceId()) return defaultsDev();
  return v ? { ...defaultsDev(), ...v, bands: sanitizeBands(v.bands) } : null;
}
export const shared = () => {
  const v = settings.get("eq.shared", null);
  return v ? { active: v.active || "custom", preamp: clamp(Number(v.preamp) || 0, ...LIMITS.preamp), bands: sanitizeBands(v.bands) } : { active: "flat", preamp: 0, bands: flatBands() };
};
export const customPresets = () => (settings.get("eq.presets", []) || []).filter((p) => p && p.id && p.name).map((p) => ({ ...p, bands: sanitizeBands(p.bands) }));
export const allPresets = () => [...BUILTIN, ...customPresets()];
export const presetName = (id) => (allPresets().find((p) => p.id === id) || { name: id === "custom" ? "Свой" : "—" }).name;

// Действующий профиль устройства: полосы/преамп/пресет из общего (sync) или своего (local)
export function effective(id = deviceId()) {
  const d = device(id) || defaultsDev();
  const prof = d.mode === "sync" ? shared() : d;
  return { ...d, active: prof.active, preamp: prof.preamp, bands: prof.bands };
}

// Все устройства из настроек (для «Настройки → Устройства»)
export function devices() {
  return settings.keys("eq.device.").map((k) => ({ id: k.slice(10), ...device(k.slice(10)) })).filter((d) => d.name);
}

// ---------------------------------------------------------------- запись
function writeDevice(id, patch) {
  const d = device(id) || defaultsDev();
  settings.set("eq.device." + id, { ...d, ...patch });
}
// Полосы/преамп/пресет — в общий профиль (sync) или в своё устройство (local)
export function editProfile(patch, id = deviceId()) {
  const d = device(id) || defaultsDev();
  if (d.mode === "sync") settings.set("eq.shared", { ...shared(), ...patch });
  else writeDevice(id, patch);
}
export function setBand(i, patch, id) {
  const p = effective(id);
  const bands = p.bands.map((b, k) => (k === i ? { ...b, ...patch } : b));
  editProfile({ bands: sanitizeBands(bands), active: "custom" }, id);
}
export const setPreamp = (v, id) => editProfile({ preamp: clamp(Math.round(v * 10) / 10, ...LIMITS.preamp), active: "custom" }, id);
export function applyPreset(pid, id) {
  const p = allPresets().find((x) => x.id === pid);
  if (p) editProfile({ active: p.id, preamp: p.preamp, bands: sanitizeBands(p.bands) }, id);
}
export const reset = (id) => applyPreset("flat", id);
export const setEnabled = (on, id = deviceId()) => writeDevice(id, { enabled: !!on });
export const setOption = (k, v, id = deviceId()) => writeDevice(id, { [k]: v });
export const rename = (name, id = deviceId()) => writeDevice(id, { name: String(name).trim().slice(0, 60) || defaultName() });
// Смена режима: в «local» переносим текущий общий профиль, чтобы звук не прыгнул
export function setMode(mode, id = deviceId()) {
  const d = device(id) || defaultsDev();
  if (mode === d.mode) return;
  if (mode === "local") { const s = shared(); writeDevice(id, { mode, active: s.active, preamp: s.preamp, bands: s.bands }); }
  else writeDevice(id, { mode });
}

// Свои пресеты (общая библиотека)
const pid = () => "u" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
export function savePreset(name, id) {
  const p = effective(id);
  const item = { id: pid(), name: String(name).trim().slice(0, 40) || "Мой пресет", preamp: p.preamp, bands: p.bands };
  settings.set("eq.presets", [...customPresets(), item]);
  editProfile({ active: item.id }, id);
  return item;
}
export function renamePreset(id, name) { settings.set("eq.presets", customPresets().map((p) => (p.id === id ? { ...p, name: String(name).trim().slice(0, 40) || p.name } : p))); }
export function deletePreset(id) { settings.set("eq.presets", customPresets().filter((p) => p.id !== id)); }

// ---------------------------------------------------------------- звук этого устройства
let bypass = false; // A/B: временный обход, не сохраняется
export const isBypassed = () => bypass;
export function setBypass(on) { bypass = !!on; applyLocal(); }

export function applyLocal() {
  const e = effective();
  graph.allowOnIOS(e.enabled);
  graph.applyEq({ enabled: e.enabled && !bypass, preamp: e.preamp, bands: e.bands, limiter: e.limiter !== false });
}

// первая запись своего устройства (имя/платформа видны на других устройствах)
export function initEq() {
  // id не сохранится (приватный режим) — не засоряем список устройств одноразовыми записями
  if (!deviceIdPersistent()) { /* работаем по умолчаниям */ }
  else if (!settings.get(DEV(), null)) settings.set(DEV(), defaultsDev());
  else if (device().platform !== platform()) writeDevice(deviceId(), { platform: platform() });
  applyLocal();
  settings.onSetting((key) => { if (key.startsWith("eq.")) applyLocal(); });
}
