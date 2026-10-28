// Подготовка встроенного интерфейса Windows 2.0 (frontendDist Tauri = apps/windows/dist-ui):
//   dist-ui/index.html  — static/index.html без telegram-web-app.js (в оболочке Telegram не нужен, внешний скрипт
//                          запрещён CSP); пути static/… остаются относительными, как на сервере в /app/
//   dist-ui/static/     — копия bot/app/static (Mini App)
//   dist-ui/setup/      — страница выбора сервера (apps/shared/setup)
//   dist-ui/webui.json  — версия интерфейса: sha256 дерева static (и коммит, если известен)
// Источник static: $WEBUI_SRC, иначе apps/webui (так его заливают на сервер сборки), иначе ../bot/app/static.
// node build/prepare-webui.mjs [выходной каталог]   → печатает sha256 дерева
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const APPS = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = resolve(process.argv[2] || join(APPS, "windows", "dist-ui"));
const candidates = [process.env.WEBUI_SRC, join(APPS, "webui"), join(APPS, "..", "bot", "app", "static")].filter(Boolean);
const src = candidates.find((d) => existsSync(join(d, "index.html")) && existsSync(join(d, "js", "app.js")));
if (!src) {
  console.error("!! не найден static Mini App (index.html + js/app.js): " + candidates.join(", "));
  process.exit(1);
}

// файлы, которые оболочке не нужны: service worker (в оболочке не регистрируется) и служебный мусор
const SKIP = (rel) => rel === "sw.js" || /(^|\/)(\.|Thumbs\.db$|desktop\.ini$)/i.test(rel);

function walk(dir) {
  const res = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) res.push(...walk(p));
    else res.push(p);
  }
  return res;
}

rmSync(out, { recursive: true, force: true });
mkdirSync(join(out, "static"), { recursive: true });

const files = walk(src)
  .map((p) => relative(src, p).split(sep).join("/"))
  .filter((rel) => !SKIP(rel))
  .sort();
const tree = createHash("sha256");
for (const rel of files) {
  const data = readFileSync(join(src, rel));
  tree.update(rel + "\0" + createHash("sha256").update(data).digest("hex") + "\n");
  mkdirSync(dirname(join(out, "static", rel)), { recursive: true });
  writeFileSync(join(out, "static", rel), data);
}
const sha = tree.digest("hex");

// index.html — в корень (страница /index.html грузит static/css, static/js — как /app/ на сервере)
let html = readFileSync(join(src, "index.html"), "utf8");
const before = html;
html = html.replace(/[ \t]*<script[^>]*src="https:\/\/telegram\.org\/[^"]*"[^>]*><\/script>\r?\n?/g, "");
if (html === before) console.error("!! в index.html не нашёлся telegram-web-app.js — проверьте разметку");
html = html.replace(/<meta charset="utf-8">/i, (m) => `${m}\n<meta name="pimusic-webui" content="${sha.slice(0, 16)}">`);
writeFileSync(join(out, "index.html"), html);

cpSync(join(APPS, "shared", "setup"), join(out, "setup"), { recursive: true });

const info = {
  sha256: sha,
  files: files.length,
  source: process.env.WEBUI_COMMIT || null,
};
writeFileSync(join(out, "webui.json"), JSON.stringify(info, null, 2) + "\n");
console.log(sha);
