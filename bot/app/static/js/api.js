// Клиент JSON API. Авторизация: в Telegram — заголовок X-Telegram-Init-Data (подпись проверяет сервер);
// вне Telegram (PWA, Windows, Android) — Authorization: Bearer <токен устройства>. 401 без Telegram — токен
// недействителен (отозван): стираем и показываем экран входа (событие pimusic:auth-lost).
import { tg } from "./tg.js";

export const INIT_HEADER = "X-Telegram-Init-Data";

// Базовый адрес сервера: интерфейс может лежать локально (Windows-оболочка), а API, потоки и обложки — на сервере.
// window.PIMUSIC_API_BASE задаёт оболочка до загрузки модулей; в Telegram и браузере — текущий origin.
export const API_BASE = (() => {
  const b = typeof window !== "undefined" && window.PIMUSIC_API_BASE;
  try { return b ? new URL(String(b)).origin + new URL(String(b)).pathname.replace(/\/+$/, "") : location.origin; } catch { return location.origin; }
})();
export const remote = API_BASE !== location.origin; // интерфейс не с того же сервера
// относительный адрес сервера (/api/…, /stream/…) → абсолютный от API_BASE
export function absUrl(u) {
  if (!u) return "";
  try { return new URL(u, API_BASE + "/").href; } catch { return ""; }
}

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const MESSAGES = {
  0: "Нет связи с сервером",
  401: "Сессия устарела — откройте приложение заново",
  403: "Нет доступа",
  404: "Не найдено",
  409: "Уже выполняется",
  500: "Ошибка сервера",
  502: "Внешний сервис недоступен",
};

export const isAbort = (e) => e && (e.name === "AbortError" || e.code === "aborted");

const TKEY = "pimusic.auth.token";
export function getToken() { try { return localStorage.getItem(TKEY) || ""; } catch { return ""; } }
export function setToken(t) { try { if (t) localStorage.setItem(TKEY, t); else localStorage.removeItem(TKEY); } catch { /* */ } }
export const inTelegram = () => !!(tg && tg.initData);
export const authMode = () => (inTelegram() ? "telegram" : getToken() ? "token" : "none");

export async function api(path, { method = "GET", body, signal, auth = true, keepalive = false } = {}) {
  const headers = {};
  if (auth && inTelegram()) headers[INIT_HEADER] = tg.initData;
  else if (auth && getToken()) headers.Authorization = "Bearer " + getToken();
  const init = { method, headers, signal, credentials: remote ? "omit" : "same-origin" }; // чужой origin — только Bearer
  if (keepalive) init.keepalive = true;
  if (body instanceof FormData) init.body = body; // multipart: границу ставит браузер
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(API_BASE + "/api" + path, init);
  } catch (e) {
    if (isAbort(e)) throw e;
    throw new ApiError(MESSAGES[0], 0, "network");
  }
  let data = {};
  try { data = await r.json(); } catch (e) { if (isAbort(e)) throw e; data = {}; }
  if (!r.ok) {
    const msg = (data && (data.message || data.text)) || MESSAGES[r.status] || `Ошибка ${r.status}`;
    if (r.status === 401 && auth && !inTelegram() && getToken()) {
      setToken("");
      const code = data && data.error;
      const reason = code === "revoked" ? "Вход на этом устройстве отозван (в настройках другого устройства или в боте). Войдите снова."
        : code === "token_expired" ? "Срок входа на этом устройстве истёк. Войдите снова." : "Вход на этом устройстве больше не действует. Войдите снова.";
      window.dispatchEvent(new CustomEvent("pimusic:auth-lost", { detail: { reason } }));
    }
    const err = new ApiError(msg, r.status, (data && data.error) || String(r.status));
    err.data = data;
    throw err;
  }
  return data;
}

// Запросы экрана: отменяются при уходе со страницы (AbortController на каждый экран)
export function scope() {
  const ctrl = new AbortController();
  return {
    signal: ctrl.signal,
    get alive() { return !ctrl.signal.aborted; },
    get: (path) => api(path, { signal: ctrl.signal }),
    post: (path, body) => api(path, { method: "POST", body: body || {}, signal: ctrl.signal }),
    abort: () => ctrl.abort(),
  };
}
