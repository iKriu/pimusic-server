// Генерирует исходники иконок PiMusic (M3E «печенька» 10 волн + волна-эквалайзер, янтарь #F5A524).
// node gen-icons.mjs <outDir>  → icon.svg, icon-maskable.svg, tray.svg, android/*.xml
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const out = process.argv[2] || "out";
const AMBER = "#F5A524", DARK = "#17130E";

function cookie(cx, cy, R, A, N = 10, steps = 400) {
  let d = "";
  for (let i = 0; i <= steps; i++) {
    const t = (i / steps) * Math.PI * 2, r = R + A * Math.cos(N * t);
    d += (i ? "L" : "M") + (cx + r * Math.sin(t)).toFixed(2) + " " + (cy - r * Math.cos(t)).toFixed(2);
  }
  return d + "Z";
}
// столбики: относительные высоты, ширина и зазор — доли радиуса
function bars(cx, cy, R) {
  const hs = [0.22, 0.44, 0.64, 0.36, 0.56, 0.26], w = R * 0.13, gap = R * 0.09;
  const x0 = cx - (hs.length * w + (hs.length - 1) * gap) / 2;
  return hs.map((h, k) => {
    const H = h * R * 2, x = x0 + k * (w + gap), y = cy - H / 2, r = w / 2;
    // скруглённый прямоугольник как path (годится и для Android vector)
    return `M${(x + r).toFixed(2)} ${y.toFixed(2)}A${r.toFixed(2)} ${r.toFixed(2)} 0 0 1 ${(x + w).toFixed(2)} ${(y + r).toFixed(2)}`
      + `L${(x + w).toFixed(2)} ${(y + H - r).toFixed(2)}A${r.toFixed(2)} ${r.toFixed(2)} 0 0 1 ${x.toFixed(2)} ${(y + H - r).toFixed(2)}`
      + `L${x.toFixed(2)} ${(y + r).toFixed(2)}A${r.toFixed(2)} ${r.toFixed(2)} 0 0 1 ${(x + r).toFixed(2)} ${y.toFixed(2)}Z`;
  }).join("");
}

mkdirSync(join(out, "android"), { recursive: true });
const svg = (size, body) => `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${body}</svg>\n`;

// 1) основная иконка (прозрачный фон) — Windows .ico/.png
{
  const S = 1024, c = S / 2, R = 440, A = 36;
  writeFileSync(join(out, "icon.svg"), svg(S, `<path fill="${AMBER}" d="${cookie(c, c, R, A)}"/><path fill="${DARK}" d="${bars(c, c, R)}"/>`));
}
// 2) maskable (тёмный фон, печенька в безопасной зоне)
{
  const S = 1024, c = S / 2, R = 300, A = 26;
  writeFileSync(join(out, "icon-maskable.svg"), svg(S, `<rect width="${S}" height="${S}" fill="${DARK}"/><path fill="${AMBER}" d="${cookie(c, c, R, A)}"/><path fill="${DARK}" d="${bars(c, c, R)}"/>`));
}
// 3) трей: крупнее и без мелких деталей (16–32 px)
{
  const S = 256, c = S / 2, R = 116, A = 10;
  writeFileSync(join(out, "tray.svg"), svg(S, `<path fill="${AMBER}" d="${cookie(c, c, R, A)}"/><path fill="${DARK}" d="${bars(c, c, R)}"/>`));
}
// 4) Android adaptive icon (108dp, безопасная зона — круг 66dp)
{
  const V = 108, c = 54, R = 29, A = 2.4;
  const vec = (paths) => `<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp" android:height="108dp" android:viewportWidth="${V}" android:viewportHeight="${V}">
${paths}
</vector>
`;
  writeFileSync(join(out, "android", "ic_launcher_foreground.xml"), vec(
    `    <path android:fillColor="${AMBER}" android:pathData="${cookie(c, c, R, A, 10, 240)}"/>\n    <path android:fillColor="${DARK}" android:pathData="${bars(c, c, R)}"/>`));
  // монохромная (Android 13 themed icons): печенька с вырезанными столбиками
  writeFileSync(join(out, "android", "ic_launcher_monochrome.xml"), vec(
    `    <path android:fillColor="#FFFFFFFF" android:fillType="evenOdd" android:pathData="${cookie(c, c, R, A, 10, 240)}${bars(c, c, R)}"/>`));
  // уведомление: только силуэт, белый
  const n = 24, nc = 12, nR = 10.6, nA = 0.9;
  writeFileSync(join(out, "android", "ic_stat_pimusic.xml"), `<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="24dp" android:height="24dp" android:viewportWidth="${n}" android:viewportHeight="${n}">
    <path android:fillColor="#FFFFFFFF" android:fillType="evenOdd" android:pathData="${cookie(nc, nc, nR, nA, 10, 160)}${bars(nc, nc, nR)}"/>
</vector>
`);
  // заставка Android 12+: иконка 240dp с кругом 160dp — печенька в центре
  const sV = 240, sc = 120, sR = 74, sA = 6;
  writeFileSync(join(out, "android", "splash_icon.xml"), `<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="240dp" android:height="240dp" android:viewportWidth="${sV}" android:viewportHeight="${sV}">
    <path android:fillColor="${AMBER}" android:pathData="${cookie(sc, sc, sR, sA, 10, 240)}"/>
    <path android:fillColor="${DARK}" android:pathData="${bars(sc, sc, sR)}"/>
</vector>
`);
}
console.log("icons ->", out);
