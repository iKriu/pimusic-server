// DOM-помощники. Правило проекта: данные сервера попадают в DOM только через textContent/свойства,
// никогда через innerHTML.
import { absUrl, API_BASE } from "./api.js";

// Язык фрагмента (WCAG 3.1.2): японский текст (есть кана) помечаем lang="ja" — для озвучки и выбора шрифта
const KANA = /[぀-ヿㇰ-ㇿ]/;
export function setText(node, text) {
  const s = text == null ? "" : String(text);
  node.textContent = s;
  if (KANA.test(s)) node.setAttribute("lang", "ja"); else if (node.getAttribute("lang") === "ja") node.removeAttribute("lang");
  return node;
}

export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "text") setText(node, v);
      else if (k === "class") node.className = v;
      else if (k === "style" && typeof v === "object") {
        for (const [sk, sv] of Object.entries(v)) {
          if (sk.startsWith("--")) node.style.setProperty(sk, sv); else node.style[sk] = sv;
        }
      }
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "hidden") node.hidden = !!v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  append(node, children);
  return node;
}

export function append(node, child) {
  if (child === undefined || child === null || child === false) return node;
  if (Array.isArray(child)) { for (const c of child) append(node, c); return node; }
  node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  return node;
}

export function mount(container, content) {
  container.replaceChildren();
  append(container, content);
  return container;
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

// Material Symbols: лигатура + запасной символ, если шрифт не загрузился (класс .no-icon-font на <html>)
const FALLBACK = {
  // новые: тема, кэш/офлайн, «к текущему», обновления
  my_location: "◎", download_done: "✓", file_download_off: "⊘", system_update: "⇩", cached: "↻", data_usage: "◔",
  brightness_auto: "◐", light_mode: "☀", dark_mode: "☾", delete_sweep: "🗑", arrow_drop_down: "▾",
  search: "⌕", arrow_back: "←", close: "✕", download: "↓", check_circle: "✓", check: "✓", error: "!",
  more_vert: "⋮", delete: "🗑", album: "◉", person: "☺", library_music: "♫", waves: "≈",
  history: "↺", play_arrow: "▶", pause: "❚❚", stop: "■", refresh: "↻", auto_awesome: "✦", content_copy: "⧉",
  open_in_new: "↗", expand_more: "▾", filter_list: "≡", music_note: "♪", queue_music: "☰", settings: "⚙",
  info: "i", arrow_forward: "→", chevron_right: "›", cloud_off: "☁", lock: "🔒", person_add: "+", wifi_off: "⨯",
  library_add_check: "✓", playlist_add: "+", schedule: "◷", report: "!", tune: "≡", clear_all: "✕", graphic_eq: "≈",
  sensors: "◉", north_west: "↖", sentiment_dissatisfied: "☹", travel_explore: "⌕", radio: "◎", keyboard_arrow_down: "▾",
  skip_next: "⏭", skip_previous: "⏮", shuffle: "⤮", repeat: "⟳", repeat_one: "⟲", queue_play_next: "⇥", add_to_queue: "+",
  drag_indicator: "⠿", group: "☺", share: "↗", edit: "✎", link: "∞", logout: "⇥", arrow_upward: "↑", arrow_downward: "↓",
  remove_circle: "−", library_add: "+", add: "+", playlist_play: "▶", person_remove: "−", link_off: "⨯", done: "✓", add_circle: "+", group_add: "+", edit_note: "✎", reorder: "≡", lyrics: "¶", equalizer: "≡", bedtime: "☾", speed: "»", join: "⧓", volume_up: "♪",
  timer_off: "◷", sync: "⟳", devices: "▭", bookmark_add: "+", restart_alt: "↺", send: "➤", desktop_windows: "▭", phone_android: "▯",
  install_desktop: "▭", language: "◎", upload_file: "⇧", help: "?", block: "⦸", dns: "▤", checklist: "☑", swap_vert: "⇅", vertical_align_top: "⤒", vertical_align_bottom: "⤓", done_all: "✓", download_for_offline: "↓", sort: "⇅", radio_button_checked: "◉", radio_button_unchecked: "○",
};
export function icon(name, opts = {}) {
  const span = el("span", {
    class: "ms" + (opts.fill ? " ms-fill" : "") + (opts.class ? " " + opts.class : ""),
    "aria-hidden": "true", "data-fb": FALLBACK[name] || "•", translate: "no",
  });
  span.textContent = name;
  return span;
}

export function debounce(fn, ms) {
  let t;
  const d = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}

export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
export const nTracks = (n) => `${n} ${plural(n, "трек", "трека", "треков")}`;
export const nAlbums = (n) => `${n} ${plural(n, "альбом", "альбома", "альбомов")}`;

export function fmtFans(n) {
  if (!n) return "";
  const s = n >= 1e6 ? (Math.round(n / 1e5) / 10).toLocaleString("ru-RU") + " млн"
    : n >= 1e4 ? Math.round(n / 1e3) + " тыс." : n.toLocaleString("ru-RU");
  return `${s} ${plural(n, "слушатель", "слушателя", "слушателей")}`;
}

export function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? hm : d.toLocaleDateString("ru-RU") + " " + hm;
}

// «3:42» → секунды; сумма длительностей → «44 мин» / «1 ч 12 мин»
export function parseDur(s) {
  if (!s) return 0;
  const p = String(s).split(":").map(Number);
  return p.some(Number.isNaN) ? 0 : p.reduce((a, b) => a * 60 + b, 0);
}
export function fmtTotal(sec) {
  if (!sec) return "";
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${Math.max(1, m)} мин`;
}
export function fmtEta(minutes) {
  if (!isFinite(minutes) || minutes <= 0) return "";
  const m = Math.round(minutes);
  if (m < 1) return "меньше минуты";
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
}

export const initial = (name) => ((name || "").trim().charAt(0).toUpperCase() || "♪");

// URL картинок — только https:// или свои относительные /api/… (никаких javascript:/data:)
export function safeUrl(u) {
  if (typeof u !== "string" || !u) return null;
  if (u.startsWith("/api/")) return absUrl(u); // свои обложки — от адреса сервера (интерфейс может быть локальным)
  try {
    const url = new URL(u, API_BASE + "/");
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
}

// <img> с плейсхолдером: битая ссылка не оставляет пустой квадрат (состояние Partial)
export function pic(src, { cls = "", ph = "music_note", alt = "", eager = false } = {}) {
  const url = safeUrl(src);
  const holder = el("span", { class: "pic " + cls });
  const placeholder = () => holder.replaceChildren(el("span", { class: "pic-ph" }, typeof ph === "string" && ph.length > 1 ? icon(ph) : el("span", { class: "pic-initial", text: ph })));
  if (!url) { placeholder(); holder.classList.add("is-ph"); return holder; }
  const img = el("img", { src: url, alt, loading: eager ? "eager" : "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => { holder.classList.add("is-ph"); placeholder(); }, { once: true });
  img.addEventListener("load", () => holder.classList.add("is-loaded"), { once: true });
  holder.appendChild(img);
  return holder;
}

export const storage = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* приватный режим / квота */ }
  },
};

export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
