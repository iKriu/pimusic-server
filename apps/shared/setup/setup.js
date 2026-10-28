// Страница выбора сервера PiMusic (без inline-скриптов — строгая CSP).
(function () {
  // логотип: «печенька» M3E (10 волн) + волна-эквалайзер
  var d = "", N = 10, R = 200, A = 18;
  for (var i = 0; i <= 360; i++) {
    var t = i / 360 * Math.PI * 2, r = R + A * Math.cos(N * t);
    d += (i ? "L" : "M") + (256 + r * Math.sin(t)).toFixed(1) + " " + (256 - r * Math.cos(t)).toFixed(1);
  }
  document.getElementById("cookie").setAttribute("d", d + "Z");
  var hs = [44, 88, 128, 72, 112, 52], g = document.getElementById("bars"), w = 26, gap = 18;
  var x0 = 256 - (hs.length * w + (hs.length - 1) * gap) / 2;
  hs.forEach(function (h, k) {
    var r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", x0 + k * (w + gap)); r.setAttribute("y", 256 - h / 2); r.setAttribute("width", w);
    r.setAttribute("height", h); r.setAttribute("rx", w / 2); g.appendChild(r);
  });

  var DEFAULT = "";   // адреса по умолчанию нет — кнопка «По умолчанию» показывается, только если оболочка его задаёт
  var inp = document.getElementById("url"), err = document.getElementById("err"), go = document.getElementById("go");
  if (/[?&]error=1/.test(location.search)) document.getElementById("banner").classList.add("on");

  function normalize(v) {
    v = String(v || "").trim();
    if (!v) return "";
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) v = (/^(\d{1,3}\.){3}\d{1,3}|^localhost|^[^./]+\.local/i.test(v) ? "http://" : "https://") + v;
    var u;
    try { u = new URL(v); } catch (e) { return ""; }
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    if (u.pathname === "/" || u.pathname === "") u.pathname = "/app/";
    return u.href;
  }
  function native() { return window.PiMusicNative; }
  function showDefault() { document.getElementById("def").hidden = !DEFAULT; }
  function init() {
    var N = native();
    showDefault();
    if (!N) { inp.value = DEFAULT; return; }
    N.getSettings().then(function (s) {
      DEFAULT = (s && s.defaultServerUrl) || DEFAULT;
      inp.value = (s && s.serverUrl) || DEFAULT;
      showDefault();
    }).catch(function () { inp.value = DEFAULT; });
  }
  document.getElementById("def").onclick = function () { inp.value = DEFAULT; inp.focus(); };
  document.getElementById("f").onsubmit = function (ev) {
    ev.preventDefault();
    var url = normalize(inp.value);
    if (!url) { err.textContent = "Введите адрес вида https://сервер/app/"; inp.focus(); return; }
    err.textContent = ""; go.disabled = true; go.textContent = "Подключение…";
    var N = native();
    if (!N) { location.href = url; return; }
    N.setServerUrl(url).then(function (r) {
      // pending — оболочка спрашивает подтверждение (вызов не со встроенной страницы)
      if (r && r.pending) { go.disabled = false; go.textContent = "Подключиться"; }
    }).catch(function (e) {
      go.disabled = false; go.textContent = "Подключиться";
      err.textContent = "Не получилось: " + (e && e.message || e);
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  // мост может появиться чуть позже страницы (Android)
  setTimeout(function () { if (!inp.value) init(); }, 400);
})();
