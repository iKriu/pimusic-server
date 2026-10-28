// Вход вне Telegram (браузер, PWA, Windows, Android): «Войти через Telegram» → бот спрашивает подтверждение →
// опрос /api/auth/poll каждые 2 с → токен устройства. QR-код той же ссылки — для входа с телефона, где есть Telegram.
// Если сервер разрешил (login_widget) — ещё и Telegram Login Widget.
import { el, icon, mount, setText } from "./dom.js";
import { api, setToken } from "./api.js";
import { button, loadingIndicator, announce } from "./ui.js";
import { qrSvg } from "./qr.js";
import { authPlatform, defaultName } from "./device.js";
import { inShell } from "./native.js";

// сервер ждёт строки: device — имя (≤ 64), platform — web|pwa|windows|android|ios|linux|macos
const deviceBody = () => ({ device: defaultName().slice(0, 64), platform: authPlatform() });

export function showLogin(host, { reason = "" } = {}) {
  let alive = true, pollT = 0, expT = 0, nonce = "", link = "";
  const status = el("p", { class: "login-status", role: "status", "aria-live": "polite" });
  const qrBox = el("div", { class: "login-qr" });
  const tgBtn = el("a", { class: "btn btn-filled btn-m has-icon login-tg", href: "#", target: "_blank", rel: "noopener" }, icon("send", { class: "btn-icon" }), el("span", { class: "btn-label", text: "Войти через Telegram" }));
  const retry = button({ label: "Попробовать снова", icon: "refresh", variant: "tonal", onClick: () => start() });
  // код подтверждения: бот покажет три кнопки, выбрать нужно ту, что здесь
  const codeBox = el("div", { class: "login-code", hidden: true, role: "group", "aria-labelledby": "login-code-l" },
    el("span", { class: "login-code-l", id: "login-code-l", text: "Код подтверждения" }),
    el("span", { class: "login-code-v" }),
    el("span", { class: "login-code-hint", text: "Выберите этот код в Telegram" }));
  retry.hidden = true;
  const widget = el("div", { class: "login-widget" });
  const card = el("section", { class: "login-card", "aria-labelledby": "login-h" },
    el("span", { class: "state-art login-art", style: { "--state-shape": "var(--shape-cookie9)" } }, icon("graphic_eq", { fill: true })),
    el("h1", { class: "login-title", id: "login-h", text: "Вход в PiMusic" }),
    el("p", { class: "login-text", text: reason || "Войдите через Telegram: бот спросит, разрешить ли вход на этом устройстве." }),
    codeBox, tgBtn, status, retry,
    el("div", { class: "login-or" }, el("span", { text: "или отсканируйте с телефона, где есть Telegram" })),
    qrBox, widget);
  mount(host, el("div", { class: "login" }, card));
  host.hidden = false;

  tgBtn.addEventListener("click", (ev) => {
    if (!link) { ev.preventDefault(); return; }
    setStatus("wait", "Ждём подтверждения в Telegram…");
  });

  function setStatus(kind, text) {
    status.dataset.kind = kind;
    mount(status, [kind === "wait" ? loadingIndicator({ size: 24, label: "Ожидание" }) : kind === "error" ? icon("error") : null, el("span", { text })]);
  }

  // Каждый start() — свой «прогон»: опросы прежнего nonce останавливаются (AbortController + номер прогона)
  let run = 0, ctrl = null;
  const stopRun = () => { run++; clearTimeout(pollT); clearTimeout(expT); if (ctrl) ctrl.abort(); ctrl = null; nonce = ""; };
  const safeLink = (u) => typeof u === "string" && (u.startsWith("https://t.me/") || u.startsWith("tg://")) ? u : "";

  async function start() {
    stopRun();
    const my = run;
    ctrl = new AbortController();
    retry.hidden = true;
    tgBtn.classList.add("is-busy");
    setStatus("", "Готовлю ссылку…");
    codeBox.hidden = true;
    let r;
    try { r = await api("/auth/start", { method: "POST", body: deviceBody(), auth: false, signal: ctrl.signal }); } catch (e) {
      if (my !== run || !alive) return;
      tgBtn.classList.remove("is-busy");
      setStatus("error", e.status === 404 ? "Сервер пока не поддерживает вход вне Telegram." : e.status === 429 ? "Слишком много попыток — подождите минуту." : e.message);
      retry.hidden = false;
      return;
    }
    if (!alive || my !== run) return;
    nonce = r.nonce; link = safeLink(r.link);
    if (r.code) {
      setText(codeBox.querySelector(".login-code-v"), String(r.code));
      codeBox.hidden = false;
      codeBox.setAttribute("aria-label", `Код подтверждения ${r.code}. Выберите этот код в Telegram`);
    }
    tgBtn.href = link || "#";
    tgBtn.classList.remove("is-busy");
    let qr = null;
    try { qr = link ? qrSvg(link, { label: "QR-код для входа через Telegram" }) : null; } catch (e) { console.warn("QR:", e && e.message); }
    mount(qrBox, qr);
    setStatus(link ? "" : "error", link ? "Ссылка действует 5 минут." : "Сервер не знает имя бота — вход по ссылке пока недоступен.");
    expT = setTimeout(() => { if (my !== run) return; stopRun(); setStatus("error", "Ссылка устарела."); codeBox.hidden = true; retry.hidden = false; }, (r.expires_in || 300) * 1000);
    poll(my);
  }

  async function poll(my) {
    if (!alive || !nonce || my !== run) return;
    clearTimeout(pollT);
    let r;
    try { r = await api("/auth/poll", { method: "POST", body: { nonce }, auth: false, signal: ctrl && ctrl.signal }); } catch (e) {
      if (my !== run || !alive || (e && e.name === "AbortError")) return;
      pollT = setTimeout(() => poll(my), e.status === 0 ? 5000 : e.status === 429 ? 6000 : 3000);
      return;
    }
    if (!alive || my !== run) return;
    if (r.status === "ok" && r.token) {
      stopRun();
      setToken(r.token);
      setStatus("ok", `Готово: вы вошли как ${r.login}`);
      announce(`Вход выполнен: ${r.login}`);
      setTimeout(() => location.reload(), 400);
      return;
    }
    if (r.status === "denied") {
      stopRun();
      codeBox.hidden = true;
      setStatus("error", "Вход отклонён: в Telegram выбран другой код или нажато «Отклонить». Начните заново — код будет новым.");
      retry.hidden = false;
      retry.focus();
      return;
    }
    if (r.status === "expired") { stopRun(); codeBox.hidden = true; setStatus("error", "Ссылка устарела."); retry.hidden = false; return; }
    pollT = setTimeout(() => poll(my), 2000);
  }
  // вернулись из Telegram — проверяем сразу, не ждём таймер
  const onVis = () => { if (document.visibilityState === "visible" && alive && nonce) poll(run); };
  document.addEventListener("visibilitychange", onVis);

  // Telegram Login Widget — только если сервер разрешил (нужен /setdomain у BotFather)
  api("/auth/config", { auth: false }).then((c) => {
    if (!alive || !c || !c.login_widget || !c.bot || inShell()) return;
    window.pimusicTgAuth = async (user) => {
      try {
        const r = await api("/auth/telegram", { method: "POST", body: { ...user, ...deviceBody() }, auth: false });
        setToken(r.token);
        location.reload();
      } catch (e) { setStatus("error", e.message); }
    };
    const s = document.createElement("script");
    s.async = true;
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.dataset.telegramLogin = c.bot;
    s.dataset.size = "large";
    s.dataset.radius = "20";
    s.dataset.onauth = "pimusicTgAuth(user)";
    s.dataset.requestAccess = "write";
    widget.appendChild(s);
  }).catch(() => {});

  start();
  return { close() { alive = false; stopRun(); document.removeEventListener("visibilitychange", onVis); host.hidden = true; } };
}

export { setText };
