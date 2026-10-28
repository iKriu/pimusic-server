// Service worker PiMusic (только вне Telegram): кэш оболочки приложения — запуск без сети показывает экран
// «Сервер недоступен» вместо пустой страницы. /api, поток и обложки НЕ кэшируются.
// Стратегия: сначала сеть (cache: "no-cache" — проверка у сервера), без сети — из кэша.
// VERSION повышать при каждом выпуске: старый кэш удаляется при активации.
const VERSION = "2026-10-27.1";
const CACHE = "pimusic-shell-" + VERSION;
const JS = ["api", "app", "audiograph", "bulk", "color", "device", "dom", "eq", "eqview", "fullplayer", "header", "libsort", "libtrack", "login", "lyrics", "motion", "native", "nowplaying", "offline", "picker", "player", "playerctl", "playlists", "qr", "reorder", "select", "settings", "shapes", "shell", "store", "tg", "theme", "track", "ui", "updates", "uploads", "volume"]
  .map((n) => `static/js/${n}.js`)
  .concat(["album", "artist", "downloads", "library", "offline", "playlist", "search", "settings", "wave"].map((n) => `static/js/screens/${n}.js`));
const CSS = ["base", "components", "layout", "phase2", "queue", "screens", "tokens"].map((n) => `static/css/${n}.css`);
const SHELL = ["./", "static/manifest.webmanifest", ...CSS, ...JS, "static/icons/icon-192.png", "static/icons/icon-512.png", "static/icons/favicon-64.png", "static/fonts/MaterialSymbolsRounded.woff2"];
const COMPLETE = "__complete__"; // отметка «оболочка закэширована целиком» — без неё index офлайн не отдаём

// SW лежит в /app/ (scope /app/) или, на старом сервере, в /app/static/ (scope /app/static/)
const scope = new URL(self.registration ? self.registration.scope : "./", self.location.href);
const appBase = scope.pathname.endsWith("/static/") ? new URL("../", scope) : scope;
const abs = (p) => new URL(p, appBase).href;

self.addEventListener("install", (ev) => {
  ev.waitUntil((async () => {
    const c = await caches.open(CACHE);
    const res = await Promise.all(SHELL.map((p) => fetch(abs(p), { cache: "no-cache" }).then((r) => (r.ok ? c.put(abs(p), r).then(() => true) : false)).catch(() => false)));
    if (res.every(Boolean)) await c.put(abs(COMPLETE), new Response("1"));
    await self.skipWaiting();
  })());
});
self.addEventListener("activate", (ev) => {
  ev.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith("pimusic-shell-") && k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (ev) => {
  const req = ev.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return; // шрифты, telegram.org — браузер сам
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/stream/") || req.headers.has("range")) return;
  const nav = req.mode === "navigate";
  if (!nav && !/\/static\/.+\.(js|css|png|webmanifest|svg|woff2)$/.test(url.pathname)) return;
  ev.respondWith((async () => {
    try {
      const res = await fetch(req, { cache: "no-cache" });
      if (res.ok && res.type === "basic") { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(nav ? abs("./") : req, copy)); }
      return res;
    } catch {
      const c = await caches.open(CACHE);
      if (nav) {
        // index без модулей офлайн бесполезен (белый экран) — отдаём, только если оболочка целиком в кэше
        const done = await c.match(abs(COMPLETE));
        const hit = done && (await c.match(abs("./")));
        return hit || Response.error();
      }
      // для .js/.css — только точное совпадение; никакого HTML вместо скрипта
      return (await c.match(req, { ignoreSearch: true })) || Response.error();
    }
  })());
});
