// M3-цвет без зависимостей: тональные палитры и схема ролей из seed-цвета или обложки.
//
// Выбор модели (компактная HCT-подобная аппроксимация):
//  * ТОН = CIE L* — ровно как в HCT. Для каждого тона решаем яркость Y(L*) и подбираем OKLab-светлоту так,
//    чтобы относительная яркость цвета точно совпала с Y. Поэтому контраст между ролями (он зависит только
//    от Y) получается тем же, что и в Material Color Utilities: пары с разницей тонов ≥ 40 дают ≥ 3:1,
//    ≥ 50 — ≥ 4.5:1 (AA).
//  * ОТТЕНОК и ХРОМА — в OKLCH (перцептивно ровный оттенок, без «синего сдвига» CIELAB).
//    Хрома HCT пересчитана в OKLCH коэффициентом ≈ 0.0035 (48 HCT ≈ 0.17 OKLCH).
//  * Выход за sRGB: бинарный поиск максимальной хромы при фиксированных тоне и оттенке (как в HCT-solver).
//  * Варианты (как в design/canvas/src/m3.js): tonalSpot для seed — primary 0.105, secondary 0.042,
//    tertiary +60° 0.068, neutral 0.012/0.022; content для обложек — хрома primary следует источнику
//    (0.075…0.17), tertiary — гармонизированный зелёно-бирюзовый. Роль success («в медиатеке») — hue 165,
//    притянутый к источнику ≤ 15° (Blend.harmonize), одинаково читается на любой контент-схеме.
import { absUrl, remote } from "./api.js";

const lin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const gam = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

export function hexToRgb(hex) {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((x) => x + x).join("") : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
const toHex = (rgb) => "#" + rgb.map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");

export function rgbToOklch([R, G, B]) {
  const r = lin(R / 255), g = lin(G / 255), b = lin(B / 255);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
  const a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
  const bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  const C = Math.hypot(a, bb);
  let h = (Math.atan2(bb, a) * 180) / Math.PI;
  if (h < 0) h += 360;
  return { L, C, h };
}

function oklabToLinear(L, a, b) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ];
}

const yFromTone = (t) => (t > 8 ? ((t + 16) / 116) ** 3 : t / 903.2962962963);
const luminance = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b;

// Цвет заданного тона (L*) с оттенком h и хромой C (OKLCH), с уменьшением хромы до попадания в sRGB
function solve(h, C, tone) {
  if (tone <= 0) return [0, 0, 0];
  if (tone >= 100) return [255, 255, 255];
  const Yt = yFromTone(tone);
  const rad = (h * Math.PI) / 180;
  const attempt = (c) => {
    const a = c * Math.cos(rad), b = c * Math.sin(rad);
    let lo = 0, hi = 1, rgb = null;
    for (let i = 0; i < 22; i++) {
      const L = (lo + hi) / 2;
      rgb = oklabToLinear(L, a, b);
      if (luminance(rgb) < Yt) lo = L; else hi = L;
    }
    const ok = rgb.every((v) => v >= -1e-4 && v <= 1 + 1e-4);
    return { ok, rgb };
  };
  let r = attempt(C);
  if (!r.ok) {
    let lo = 0, hi = C, best = attempt(0);
    for (let i = 0; i < 14; i++) {
      const mid = (lo + hi) / 2, t = attempt(mid);
      if (t.ok) { best = t; lo = mid; } else hi = mid;
    }
    r = best;
  }
  return r.rgb.map((v) => gam(Math.min(1, Math.max(0, v))) * 255);
}

export function tonalPalette(h, C) {
  const cache = new Map();
  return (tone) => {
    if (!cache.has(tone)) cache.set(tone, toHex(solve(h, C, tone)));
    return cache.get(tone);
  };
}

// Тоны ролей M3 (dark / light)
// светлая схема: primary-container 88 и secondary-container 92 — у холодных seed (синий, сирень) на тоне 90 хрома
// обоих упирается в границу sRGB и контейнеры сливаются; разница тонов их разводит (контраст текста — с запасом)
const ROLES = {
  primary: ["p", 80, 40], "on-primary": ["p", 20, 100], "primary-container": ["p", 30, 88], "on-primary-container": ["p", 90, 10],
  "inverse-primary": ["p", 40, 80], "surface-tint": ["p", 80, 40],
  "primary-fixed": ["p", 90, 90], "primary-fixed-dim": ["p", 80, 80], "on-primary-fixed": ["p", 10, 10],
  secondary: ["s", 80, 40], "on-secondary": ["s", 20, 100], "secondary-container": ["s", 30, 92], "on-secondary-container": ["s", 90, 10],
  tertiary: ["t", 80, 40], "on-tertiary": ["t", 20, 100], "tertiary-container": ["t", 30, 90], "on-tertiary-container": ["t", 90, 10],
  error: ["e", 80, 40], "on-error": ["e", 20, 100], "error-container": ["e", 30, 90], "on-error-container": ["e", 90, 10],
  background: ["n", 6, 98], "on-background": ["n", 90, 10],
  surface: ["n", 6, 98], "surface-dim": ["n", 6, 87], "surface-bright": ["n", 24, 98],
  "surface-container-lowest": ["n", 4, 100], "surface-container-low": ["n", 10, 96], "surface-container": ["n", 12, 94],
  "surface-container-high": ["n", 17, 92], "surface-container-highest": ["n", 22, 90],
  "on-surface": ["n", 90, 10], "surface-variant": ["nv", 30, 90], "on-surface-variant": ["nv", 80, 30],
  outline: ["nv", 60, 50], "outline-variant": ["nv", 30, 80],
  "inverse-surface": ["n", 90, 20], "inverse-on-surface": ["n", 20, 95],
  scrim: ["n", 0, 0], shadow: ["n", 0, 0],
  success: ["ok", 80, 40], "on-success": ["ok", 20, 100], "success-container": ["ok", 30, 90], "on-success-container": ["ok", 90, 10],
};

// «в медиатеке» — отдельная success-роль: зелёно-бирюзовый (hue 165), притянутый к источнику ≤ 15° (harmonize)
export function harmonize(target, src) {
  const d = ((src - target + 540) % 360) - 180;
  return (target + Math.sign(d) * Math.min(Math.abs(d) * 0.5, 15) + 360) % 360;
}

// variant: "tonalSpot" — базовая схема из seed; "content" — из обложки (хрома следует источнику)
export function schemeFrom({ h, C }, dark, variant = "tonalSpot") {
  let spec;
  if (variant === "content") {
    const pc = Math.min(Math.max(C, 0.075), 0.17);
    spec = { p: [h, pc], s: [h, Math.min(Math.max(pc - 0.09, pc * 0.42), 0.07)], t: [harmonize(165, h), Math.max(Math.min(pc * 0.62, 0.11), 0.06)],
      n: [h, Math.min(pc / 9, 0.018)], nv: [h, Math.min(pc / 9 + 0.012, 0.03)] };
  } else {
    spec = { p: [h, 0.105], s: [h, 0.042], t: [(h + 60) % 360, 0.068], n: [h, 0.012], nv: [h, 0.022] };
  }
  spec.e = [29, 0.21];
  spec.ok = [harmonize(165, h), 0.1];
  const pal = {};
  for (const [k, [hh, cc]] of Object.entries(spec)) pal[k] = tonalPalette(hh, cc);
  const out = {};
  for (const [role, [p, d, l]] of Object.entries(ROLES)) out[role] = pal[p](dark ? d : l);
  return out;
}

export const SEED = "#F5A524";
export const seedSource = () => rgbToOklch(hexToRgb(SEED));

export function applyScheme(target, scheme) {
  const st = target.style;
  for (const [role, hex] of Object.entries(scheme)) st.setProperty(`--md-sys-color-${role}`, hex);
}
export function clearScheme(target) {
  for (const role of Object.keys(ROLES)) target.style.removeProperty(`--md-sys-color-${role}`);
}

export function contrast(hexA, hexB) {
  const Y = (hex) => luminance(hexToRgb(hex).map((v) => lin(v / 255)));
  const a = Y(hexA) + 0.05, b = Y(hexB) + 0.05;
  return a > b ? a / b : b / a;
}

// ---------------------------------------------------------------- цвет из обложки
// Уменьшаем до 32×32, строим гистограмму оттенков (36 корзин), вес — население × хрома (как Score в MCU),
// отбрасываем почти чёрное/белое. Если холст «испорчен» (CORS) или обложка серая — null (берём seed).
const coverCache = new Map();

export function sourceFromImage(img) {
  try {
    const n = 32;
    const c = document.createElement("canvas");
    c.width = n; c.height = n;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, n, n);
    const data = ctx.getImageData(0, 0, n, n).data; // SecurityError при tainted canvas
    const bins = Array.from({ length: 36 }, () => ({ w: 0, c: 0, h: 0, cnt: 0 }));
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 128) continue;
      const { L, C, h } = rgbToOklch([data[i], data[i + 1], data[i + 2]]);
      if (L < 0.2 || L > 0.96 || C < 0.03) continue;
      const b = bins[Math.floor(h / 10) % 36];
      b.w += C; b.c += C; b.cnt++;
      b.h += h;
    }
    // сглаживание соседних корзин, чтобы один оттенок не делился на две
    let best = null, bestScore = 0;
    bins.forEach((b, i) => {
      if (!b.cnt) return;
      const score = b.w + 0.5 * (bins[(i + 35) % 36].w + bins[(i + 1) % 36].w);
      if (score > bestScore) { bestScore = score; best = b; }
    });
    if (!best || best.cnt < 12) return null;
    return { h: best.h / best.cnt, C: best.c / best.cnt };
  } catch {
    return null;
  }
}

export function sourceFromUrl(url) {
  if (!url) return Promise.resolve(null);
  if (coverCache.has(url)) return coverCache.get(url);
  const p = new Promise((resolve) => {
    const img = new Image();
    // отдельный объект только для выборки: у видимой <img> crossOrigin не ставим, чтобы не сломать показ
    // свои обложки (/api/…) с того же сервера — без CORS; интерфейс локальный (оболочка) — тоже через CORS
    const own = url.startsWith("/") && !remote;
    if (!own) img.crossOrigin = "anonymous";
    img.decoding = "async";
    const t = setTimeout(() => resolve(null), 6000);
    img.onload = () => { clearTimeout(t); resolve(sourceFromImage(img)); };
    img.onerror = () => { clearTimeout(t); resolve(null); };
    img.src = url.startsWith("/") ? absUrl(url) : url;
  });
  coverCache.set(url, p);
  return p;
}
