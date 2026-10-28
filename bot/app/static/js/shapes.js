// Выразительные формы M3E (cookie, soft burst, sunny, pentagon, pill…) как полярные функции r(θ).
// Одинаковая сетка углов у всех форм → морфинг = линейная интерполяция радиусов (loading indicator).

const N = 120;
const TAU = Math.PI * 2;

const roundedPolygon = (n, round) => (t) => {
  const seg = TAU / n;
  const x = ((t % seg) + seg) % seg - seg / 2;
  const poly = Math.cos(Math.PI / n) / Math.cos(x);
  return poly * (1 - round) + round * 0.93;
};

export const SHAPES = {
  circle: () => 1,
  cookie9: (t) => 1 + 0.075 * Math.cos(9 * t),
  cookie4: (t) => 1 + 0.12 * Math.cos(4 * t),
  softBurst: (t) => 1 + 0.11 * Math.sign(Math.cos(10 * t)) * Math.abs(Math.cos(10 * t)) ** 0.8,
  sunny: (t) => 1 + 0.06 * Math.cos(8 * t),
  scallop: (t) => 0.93 + 0.07 * Math.abs(Math.cos(6 * t)),
  pentagon: roundedPolygon(5, 0.35),
  clover: (t) => 0.86 + 0.16 * Math.abs(Math.cos(2 * t)),
  oval: (t) => 1 / Math.sqrt((Math.cos(t + 0.6) / 1.05) ** 2 + (Math.sin(t + 0.6) / 0.78) ** 2),
  pill: (t) => (Math.abs(Math.cos(t + 0.78)) ** 3 + Math.abs(Math.sin(t + 0.78) / 0.62) ** 3) ** (-1 / 3),
};

export function radii(name) {
  const f = SHAPES[name] || SHAPES.circle;
  const r = new Float32Array(N);
  let max = 0;
  for (let i = 0; i < N; i++) { r[i] = f((i / N) * TAU); max = Math.max(max, r[i]); }
  for (let i = 0; i < N; i++) r[i] /= max; // вписываем в круг радиуса 1
  return r;
}

export function pathFromRadii(r, cx, cy, R, rot = 0) {
  let d = "";
  for (let i = 0; i < N; i++) {
    const t = (i / N) * TAU + rot;
    const x = cx + Math.cos(t) * r[i] * R, y = cy + Math.sin(t) * r[i] * R;
    d += (i ? "L" : "M") + x.toFixed(2) + " " + y.toFixed(2);
  }
  return d + "Z";
}

// data:-URL маски (для mask-image), рисуется в viewBox 100×100
export function shapeMaskUrl(name) {
  const d = pathFromRadii(radii(name), 50, 50, 50, -Math.PI / 2);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="${d}" fill="#000"/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

// Регистрируем маски как CSS-переменные: --shape-cookie9 и т.д.
export function installShapes() {
  const st = document.documentElement.style;
  for (const name of ["cookie9", "cookie4", "softBurst", "sunny", "scallop", "pentagon", "clover", "oval"]) {
    st.setProperty(`--shape-${name}`, shapeMaskUrl(name));
  }
  // плитка волны для wavy linear progress (24×12, синус, толщина 4)
  let d = "";
  for (let i = 0; i <= 48; i++) { const x = i / 2; d += (i ? "L" : "M") + x.toFixed(2) + " " + (6 - 3 * Math.sin((x / 24) * TAU)).toFixed(2); }
  const wave = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 12"><path d="${d}" fill="none" stroke="#000" stroke-width="4" stroke-linecap="round"/></svg>`;
  st.setProperty("--wave-mask", `url("data:image/svg+xml,${encodeURIComponent(wave)}")`);
}
