/*
 * PiMusic native bridge (Windows/Tauri и Android/Capacitor).
 *
 * Внедряется оболочкой в КАЖДУЮ страницу до её скриптов (document-start).
 * Перед этим файлом оболочка определяет window.__PIMUSIC_NATIVE_CONFIG__ =
 *   { platform: "windows"|"android", shell: "tauri"|"capacitor", version, serverUrl, features: {...}, insets? }
 *
 * Веб-приложение НЕ обязано знать о мосте: он сам
 *   - перехватывает внешние ссылки (<a> на чужой origin, window.open) и открывает их в системе
 *     (t.me → приложение Telegram);
 *   - подслушивает navigator.mediaSession (metadata, playbackState, setActionHandler, setPositionState)
 *     и передаёт состояние оболочке (трей, уведомление Android, SMTC), а команды оболочки (медиаклавиши,
 *     кнопки трея/уведомления, потеря аудиофокуса) вызывает через те же обработчики mediaSession;
 *   - на Android выставляет CSS-переменные отступов (--pimusic-inset-*, --tg-inset-*).
 * API для веб-приложения описан в apps/README.md.
 */
(function () {
  "use strict";
  if (window.PiMusicNative && window.PiMusicNative.__bridge) return;
  var CFG = window.__PIMUSIC_NATIVE_CONFIG__ || {};
  var PLATFORM = CFG.platform || "unknown";
  var DEDUPE_MS = 600;

  // ------------------------------------------------------------------ транспорт
  function call(method, args) {
    args = args || {};
    try {
      var A = window.PiMusicAndroid; // Android: WebViewCompat.addWebMessageListener (только сервер и https://localhost)
      if (A && typeof A.postMessage === "function") return androidCall(A, method, args);
      var T = window.__TAURI_INTERNALS__;
      if (T && typeof T.invoke === "function") return T.invoke("pimusic_native", { method: method, args: args });
    } catch (e) { return Promise.reject(e); }
    return Promise.reject(new Error("PiMusicNative: нет нативного транспорта"));
  }
  // Android: запрос {id, method, args} → ответ {id, ok, result|error} через onmessage того же объекта
  var pending = {}, seq = 0, androidHooked = false;
  function androidCall(A, method, args) {
    if (!androidHooked) {
      androidHooked = true;
      A.onmessage = function (ev) {
        var r; try { r = JSON.parse(ev.data); } catch (e) { return; }
        var p = pending[r.id]; if (!p) return;
        delete pending[r.id];
        if (r.ok) p.resolve(r.result); else p.reject(new Error(r.error || "native error"));
      };
    }
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      pending[id] = { resolve: resolve, reject: reject };
      A.postMessage(JSON.stringify({ id: id, method: String(method), args: args }));
      setTimeout(function () { if (pending[id]) { delete pending[id]; reject(new Error("PiMusicNative: нет ответа")); } }, 15000);
    });
  }
  // относительный адрес API (/api/stream/…) → абсолютный на сервере (Windows 2.0: страница не на сервере)
  function absApi(u) {
    try { return new URL(u, (CFG.apiBase || location.origin) + "/").href; } catch (e) { return u; }
  }
  function fire(method, args) { call(method, args).catch(function () { /* оболочка могла не успеть */ }); }

  // ------------------------------------------------------------------ события
  var listeners = {};
  function on(name, fn) {
    (listeners[name] = listeners[name] || []).push(fn);
    // подписка на "update" = веб-приложение само рисует диалог обновления (Windows), системный не нужен
    if (name === "update" && listeners[name].length === 1) fire("setUpdateUi", { enabled: true });
    return function () { off(name, fn); };
  }
  function off(name, fn) {
    var a = listeners[name]; if (!a) return;
    var i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
    if (name === "update" && i >= 0 && a.length === 0) fire("setUpdateUi", { enabled: false });
  }
  // true — событие обработано веб-приложением (слушатель вернул true или вызвал preventDefault у DOM-события)
  function dispatch(name, detail) {
    var handled = false;
    var a = (listeners[name] || []).slice();
    for (var i = 0; i < a.length; i++) {
      try { if (a[i](detail) === true) handled = true; } catch (e) { console.error(e); }
    }
    try {
      var ev = new CustomEvent("pimusic-native:" + name, { detail: detail, cancelable: true });
      if (!window.dispatchEvent(ev)) handled = true;
    } catch (e) { /* */ }
    return handled;
  }

  // ------------------------------------------------------------------ внешние ссылки
  function isExternal(href) {
    try {
      var u = new URL(href, location.href);
      if (u.protocol === "javascript:" || u.protocol === "blob:" || u.protocol === "data:" || u.protocol === "about:") return false;
      if (u.protocol !== "http:" && u.protocol !== "https:") return true; // tg:, mailto:, intent: …
      return u.origin !== location.origin;
    } catch (e) { return false; }
  }
  function openExternal(url) {
    var abs = url;
    try { abs = new URL(url, location.href).href; } catch (e) { /* */ }
    return call("openExternal", { url: abs });
  }
  document.addEventListener("click", function (ev) {
    if (ev.defaultPrevented || ev.button > 1) return;
    var t = ev.target, a = null;
    while (t && t !== document) { if (t.tagName === "A" && t.hasAttribute("href")) { a = t; break; } t = t.parentNode; }
    if (!a) return;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#" || a.hasAttribute("download")) return;
    if (!isExternal(href)) return;
    ev.preventDefault();
    openExternal(a.href || href);
  }, true);
  var origOpen = window.open;
  window.open = function (url) {
    if (url && isExternal(String(url))) { openExternal(String(url)); return null; }
    return origOpen ? origOpen.apply(window, arguments) : null;
  };

  // ------------------------------------------------------------------ Media Session
  var handlers = {};          // action -> обработчик веб-приложения
  var lastBrowser = {};       // action -> время вызова браузером (SMTC/клавиши WebView2)
  var lastNative = {};        // action -> время команды от оболочки
  var media = { title: "", artist: "", album: "", artwork: "", state: "none", duration: 0, position: 0, rate: 1 };
  var pushT = 0;

  function now() { return Date.now(); }
  function family(a) { return a === "play" || a === "pause" || a === "playpause" ? "playpause" : a; }
  function pushMedia(immediate) {
    clearTimeout(pushT);
    var send = function () {
      var acts = [];
      for (var k in handlers) if (handlers[k]) acts.push(k);
      fire("mediaUpdate", {
        title: media.title, artist: media.artist, album: media.album, artwork: media.artwork,
        state: media.state, duration: media.duration, position: media.position, rate: media.rate, actions: acts
      });
    };
    if (immediate) send(); else pushT = setTimeout(send, 150);
  }
  function bestArtwork(list) {
    if (!list || !list.length) return "";
    var best = list[0], bestSize = 0;
    for (var i = 0; i < list.length; i++) {
      var s = String(list[i].sizes || "").split(/\s+/)[0].split("x"); var n = parseInt(s[0], 10) || 0;
      if (n >= bestSize) { bestSize = n; best = list[i]; }
    }
    try { return new URL(best.src, location.href).href; } catch (e) { return best.src || ""; }
  }

  var ms = navigator.mediaSession;
  if (!ms) {
    // старый WebView без Media Session: заглушка, чтобы веб-код работал так же
    ms = { metadata: null, playbackState: "none", setActionHandler: function () {}, setPositionState: function () {} };
    try { Object.defineProperty(navigator, "mediaSession", { value: ms, configurable: true }); } catch (e) { /* */ }
    if (typeof window.MediaMetadata === "undefined") {
      window.MediaMetadata = function (init) {
        init = init || {};
        this.title = init.title || ""; this.artist = init.artist || ""; this.album = init.album || "";
        this.artwork = init.artwork || [];
      };
    }
  }
  try {
    var proto = Object.getPrototypeOf(ms) || {};
    var mdD = Object.getOwnPropertyDescriptor(proto, "metadata") || Object.getOwnPropertyDescriptor(ms, "metadata");
    var psD = Object.getOwnPropertyDescriptor(proto, "playbackState") || Object.getOwnPropertyDescriptor(ms, "playbackState");
    var mdVal = null, psVal = "none";
    var getMd = function () { return mdD && mdD.get ? mdD.get.call(ms) : mdVal; };
    var getPs = function () { return psD && psD.get ? psD.get.call(ms) : psVal; };
    Object.defineProperty(ms, "metadata", {
      configurable: true, enumerable: true, get: getMd,
      set: function (v) {
        if (mdD && mdD.set) mdD.set.call(ms, v); else mdVal = v;
        var prevKey = media.title + "|" + media.artist;
        media.title = (v && v.title) || ""; media.artist = (v && v.artist) || ""; media.album = (v && v.album) || "";
        media.artwork = v ? bestArtwork(v.artwork) : "";
        if (!v) { media.duration = 0; media.position = 0; }
        pushMedia(prevKey !== media.title + "|" + media.artist);
      }
    });
    Object.defineProperty(ms, "playbackState", {
      configurable: true, enumerable: true, get: getPs,
      set: function (v) {
        if (psD && psD.set) psD.set.call(ms, v); else psVal = v;
        var changed = media.state !== v; media.state = v;
        if (changed) pushMedia(true);
      }
    });
    var origSetAction = ms.setActionHandler ? ms.setActionHandler.bind(ms) : function () {};
    ms.setActionHandler = function (action, fn) {
      handlers[action] = fn || null;
      pushMedia(false);
      var wrapped = fn ? function (d) {
        var f = family(action);
        if (now() - (lastNative[f] || 0) < DEDUPE_MS) return; // эта же команда уже пришла от оболочки
        lastBrowser[f] = now();
        return fn(d);
      } : null;
      return origSetAction(action, wrapped); // может бросить для неподдерживаемого действия — как и без моста
    };
    var origSetPos = ms.setPositionState ? ms.setPositionState.bind(ms) : function () {};
    ms.setPositionState = function (st) {
      if (st && typeof st.duration === "number") {
        media.duration = st.duration || 0; media.position = st.position || 0; media.rate = st.playbackRate || 1;
      } else { media.duration = 0; media.position = 0; }
      pushMedia(false);
      return origSetPos.apply(null, arguments);
    };
  } catch (e) { console.warn("PiMusicNative: mediaSession hook", e); }

  // Команда от оболочки: media {action: play|pause|playpause|nexttrack|previoustrack|stop|seekto|seekforward|seekbackward, seekTime?}
  function nativeMedia(d) {
    var action = d && d.action; if (!action) return false;
    var f = family(action);
    if (now() - (lastBrowser[f] || 0) < DEDUPE_MS) return true; // браузер (SMTC) уже обработал это нажатие
    lastNative[f] = now();
    if (dispatch("media", d)) return true;
    if (action === "playpause") action = media.state === "playing" ? "pause" : "play";
    var h = handlers[action];
    if (!h && action === "stop") h = handlers.pause;
    if (!h) return false;
    try { h({ action: action, seekTime: d.seekTime, seekOffset: d.seekOffset, fastSeek: false }); } catch (e) { console.error(e); }
    return true;
  }

  // ------------------------------------------------------------------ отступы (Android edge-to-edge)
  var insets = CFG.insets || null;
  function applyInsets() {
    var root = document.documentElement; if (!root || !insets) return;
    ["top", "right", "bottom", "left"].forEach(function (s) {
      var v = (insets[s] || 0) + "px";
      root.style.setProperty("--pimusic-inset-" + s, v);
      root.style.setProperty("--tg-inset-" + s, v); // токены веб-приложения берут max(env(), --tg-inset-*)
    });
  }
  if (insets) { applyInsets(); document.addEventListener("DOMContentLoaded", applyInsets); }

  // ------------------------------------------------------------------ цвет темы → системные панели/заголовок окна
  var lastTheme = "";
  function syncTheme() {
    var m = document.querySelector('meta[name="theme-color"]');
    var c = m && m.getAttribute("content"); if (!c || c === lastTheme) return;
    lastTheme = c; fire("setThemeColor", { color: c });
  }
  function watchTheme() {
    syncTheme();
    try {
      new MutationObserver(syncTheme).observe(document.head || document.documentElement,
        { subtree: true, childList: true, attributes: true, attributeFilter: ["content"] });
    } catch (e) { /* */ }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", watchTheme); else watchTheme();

  // ------------------------------------------------------------------ публичный объект
  var api = {
    __bridge: 1,
    platform: PLATFORM,                    // "windows" | "android"
    shell: CFG.shell || "",               // "tauri" | "capacitor"
    version: CFG.version || "",           // версия оболочки (не веб-приложения)
    serverUrl: CFG.serverUrl || "",
    features: CFG.features || {},
    call: call,                            // низкоуровневый вызов: call(method, args) → Promise
    on: on, off: off,
    openExternal: openExternal,
    openServerSettings: function () { return call("openServerSettings"); },
    getSettings: function () { return call("getSettings"); },
    setSettings: function (patch) { return call("setSettings", patch || {}); },
    setServerUrl: function (url) { return call("setServerUrl", { url: url }); },
    getInfo: function () { return call("getInfo"); },
    notify: function (title, body) { return call("notify", { title: title, body: body || "" }); },
    // Windows 2.0: интерфейс встроен, API — на apiBase (то же, что window.PIMUSIC_API_BASE)
    apiBase: CFG.apiBase || "",
    localUi: !!CFG.localUi,
    // кэш потока и офлайн (Windows; features.cache / features.offline) — docs/design/APPS_API.md
    // cacheUrl(trackId, streamUrl) → адрес для <audio>: с диска, если трек есть целиком, иначе с докачкой в кэш
    cacheUrl: function (trackId, streamUrl) {
      if (!CFG.cacheBase) return streamUrl;
      var abs = streamUrl;
      try { abs = new URL(streamUrl, (CFG.apiBase || location.origin) + "/").href; } catch (e) { /* как есть */ }
      return CFG.cacheBase + "/stream/" + encodeURIComponent(String(trackId)) + "?src=" + encodeURIComponent(abs);
    },
    cachePrefetch: function (trackId, url) { return call("cachePrefetch", { trackId: trackId, url: absApi(url) }); },
    cacheStatus: function (trackId) { return call("cacheStatus", { trackId: trackId }); },
    cacheInfo: function () { return call("cacheInfo"); },
    cacheClear: function () { return call("cacheClear"); },
    setCacheLimit: function (bytes) { return call("setCacheLimit", { bytes: bytes }); },
    offlineDownload: function (trackId, url, meta) { return call("offlineDownload", { trackId: trackId, url: absApi(url), meta: meta || null }); },
    offlineList: function () { return call("offlineList"); },
    offlineDelete: function (trackId) { return call("offlineDelete", trackId === "all" ? { all: true } : { trackId: trackId }); },
    // обновления оболочки (Windows; features.updates): события "update", "updateProgress", "updateError"
    checkUpdate: function () { return call("checkUpdate"); },
    installUpdate: function () { return call("installUpdate"); },
    dismissUpdate: function () { return call("dismissUpdate"); },
    get media() { return JSON.parse(JSON.stringify(media)); },
    // вызывается оболочкой
    _emit: function (name, detail) {
      if (name === "media") return nativeMedia(detail);
      if (name === "insets") { insets = detail; applyInsets(); }
      return dispatch(name, detail);
    }
  };
  try { Object.defineProperty(window, "PiMusicNative", { value: api, configurable: false, writable: false }); }
  catch (e) { window.PiMusicNative = api; }
  var ready = function () { dispatch("ready", { platform: PLATFORM, version: api.version }); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else setTimeout(ready, 0);
})();
