// Плейлисты: клиент API, шина изменений, мозаика обложек, диалог «Новый плейлист», лист «Добавить в плейлист»,
// «Поделиться» (Telegram share + копирование ссылки).
import { el, icon, pic, mount, nTracks, plural, fmtTotal, initial } from "./dom.js";
import { api } from "./api.js";
import { button, dialog, sheet, list, listItem, snackbar, textField, switchRow, skeletonList, errorState, setBusy } from "./ui.js";
import { haptic, openTelegramLink } from "./tg.js";
import { shell } from "./shell.js";
import { copyText } from "./screens/wave.js";

export const MAX_NAME = 100, MAX_DESC = 300;

// ---------------------------------------------------------------- API
const post = (path, body) => api(path, { method: "POST", body: body || {} });
export const pl = {
  list: (signal) => api("/playlists", { signal }),
  get: (id, signal) => api(`/playlists/${id}`, { signal }),
  create: (body) => post("/playlists", body),
  update: (id, body) => post(`/playlists/${id}/update`, body),
  share: (id, enabled, regenerate = false) => post(`/playlists/${id}/share`, { enabled, regenerate }),
  add: (id, body) => post(`/playlists/${id}/add`, body),
  remove: (id, itemIds) => post(`/playlists/${id}/remove`, { item_ids: itemIds }),
  move: (id, itemId, to) => post(`/playlists/${id}/move`, { item_id: itemId, to }),
  moveMany: (id, itemIds, to) => post(`/playlists/${id}/move-many`, { item_ids: itemIds, to }),
  del: (id) => post(`/playlists/${id}/delete`),
  leave: (id) => post(`/playlists/${id}/leave`),
  kick: (id, login) => post(`/playlists/${id}/kick`, { login }),
  save: (id) => post(`/playlists/${id}/save`),
  joinPreview: (token, signal) => api("/playlists/join?token=" + encodeURIComponent(token), { signal }),
  join: (token) => post("/playlists/join", { token }),
  forTrack: (trackId) => api("/playlists/for-track?track_id=" + encodeURIComponent(trackId)),
};

// шина: список/страницы перечитывают данные после изменений
const subs = new Set();
export const onPlaylists = (fn) => { subs.add(fn); return () => subs.delete(fn); };
export function changed(id, what = "update") { for (const fn of subs) { try { fn(id, what); } catch (e) { console.error(e); } } }

export const metaLine = (p) => [nTracks(p.count || 0), fmtTotal(p.seconds || 0)].filter(Boolean).join(" · ");
export const nMembers = (n) => `${n} ${plural(n, "участник", "участника", "участников")}`;

// ---------------------------------------------------------------- мозаика 2×2 (1–3 обложки — одна во весь квадрат)
export function mosaic(covers, { cls = "" } = {}) {
  const list4 = (covers || []).filter(Boolean);
  const box = el("span", { class: "mosaic " + cls, "data-n": list4.length >= 4 ? "4" : list4.length ? "1" : "0", "aria-hidden": "true" });
  if (!list4.length) box.appendChild(el("span", { class: "mosaic-ph" }, icon("queue_music", { fill: true })));
  else if (list4.length < 4) box.appendChild(pic(list4[0], { cls: "mosaic-pic", ph: "queue_music" }));
  else for (const c of list4.slice(0, 4)) box.appendChild(pic(c, { cls: "mosaic-pic", ph: "music_note" }));
  return box;
}

// аватары участников: инициалы в тональных кругах (фото Telegram сервер не отдаёт)
export function avatars(logins, { max = 4 } = {}) {
  const box = el("span", { class: "avatars", "aria-hidden": "true" });
  logins.slice(0, max).forEach((l, i) => box.appendChild(el("span", { class: `avatar av-${hash(l) % 4}`, style: { zIndex: String(max - i) }, text: initial(l) })));
  if (logins.length > max) box.appendChild(el("span", { class: "avatar av-more", text: `+${logins.length - max}` }));
  return box;
}
const hash = (s) => [...String(s)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);

export const sharedBadge = () => el("span", { class: "pl-badge" }, icon("group", { fill: true }), el("span", { text: "Общий" }));

// ---------------------------------------------------------------- «Новый плейлист»
// body: { track_ids?, album?, artist? } — что сразу добавить. Возвращает созданный плейлист или null.
export async function createPlaylistDialog({ add = null, name = "" } = {}) {
  const nameF = textField({ label: "Название", value: name, maxlength: MAX_NAME });
  const shared = switchRow({ label: "Общий плейлист", supporting: "Друзья по ссылке смогут добавлять и убирать треки" });
  // запрос — внутри диалога: при ошибке он остаётся открытым с сообщением, введённое не теряется
  let p = null, added = 0;
  const ok = await dialog({
    icon: "playlist_add", headline: "Новый плейлист",
    content: el("div", { class: "dlg-form" }, nameF, shared),
    validate: () => (nameF.input.value.trim() ? "" : "Введите название плейлиста"),
    onConfirm: async () => {
      const body = { name: nameF.input.value.trim().slice(0, MAX_NAME), shared: shared.checked };
      if (add && add.track_ids) body.track_ids = add.track_ids;
      try { p = await pl.create(body); } catch (e) { haptic("error"); return e.message || "Не удалось создать плейлист"; }
      added = add && add.track_ids ? p.count || 0 : 0;
      if (add && (add.album || add.artist)) { try { added = (await pl.add(p.id, add)).added || 0; } catch { added = -1; } }
      return null;
    },
    actions: [{ label: "Отмена", value: false }, { label: "Создать", value: true }],
  });
  if (!ok || !p) return null;
  haptic("success");
  snackbar(!add ? `Создан плейлист «${p.name}»`
    : added > 0 ? `Создан плейлист «${p.name}» · добавлено: ${nTracks(added)}`
      : `Создан плейлист «${p.name}», но треки добавить не удалось`, { error: add && added <= 0 });
  changed(p.id, "create");
  return p;
}

// Переименование и описание (одна форма)
export async function editPlaylistDialog(p) {
  const nameF = textField({ label: "Название", value: p.name, maxlength: MAX_NAME });
  const descF = textField({ label: "Описание", value: p.description || "", maxlength: MAX_DESC, multiline: true });
  let r = null;
  const ok = await dialog({
    icon: "edit_note", headline: "Изменить плейлист",
    content: el("div", { class: "dlg-form" }, nameF, descF),
    validate: () => (nameF.input.value.trim() ? "" : "Введите название плейлиста"),
    onConfirm: async () => {
      try { r = await pl.update(p.id, { name: nameF.input.value.trim().slice(0, MAX_NAME), description: descF.input.value.trim().slice(0, MAX_DESC) }); } catch (e) { haptic("error"); return e.message || "Не удалось сохранить"; }
      return null;
    },
    actions: [{ label: "Отмена", value: false }, { label: "Сохранить", value: true }],
  });
  if (!ok || !r) return null;
  haptic("success");
  changed(p.id);
  return r;
}

// ---------------------------------------------------------------- «Добавить в плейлист»
// track — один трек (лист с отметками «уже есть»); add — {track_ids}|{album}|{artist} для набора треков
export function addToPlaylist({ track = null, add = null, title = "", scope = null } = {}) {
  const what = track ? { track_ids: [track.id] } : add;
  sheet({
    title: "Добавить в плейлист", label: "Добавить в плейлист",
    cls: "sheet-addto" + (shell.twoPane ? " sheet-dialog" : ""), scope,
    build(body, close) {
      if (track || title) {
        body.appendChild(el("div", { class: "sheet-track" }, track ? pic(track.cover, { cls: "li-thumb", ph: "music_note" }) : el("span", { class: "li-icon-box" }, icon("queue_music")),
          el("div", { class: "li-text" }, el("span", { class: "li-headline", text: track ? track.title : title }),
            track ? el("span", { class: "li-supporting", text: track.artist }) : null)));
      }
      const box = el("div", { class: "addto-list" });
      const newRow = listItem({ leading: el("span", { class: "li-icon-box li-icon-new" }, icon("add")), headline: "Новый плейлист", lines: 1, cls: "addto-new",
        onClick: async () => {
          close();
          await createPlaylistDialog({ add: what, name: "" });
        } });
      body.append(list([newRow], "sheet-list"), box);
      const load = async () => {
        mount(box, skeletonList(3, { thumb: false }));
        let items;
        try {
          const all = (await pl.list()).items || [];
          const covers = new Map(all.map((p) => [p.id, p.covers]));
          items = track ? (await pl.forTrack(track.id)).items.map((p) => ({ ...p, covers: covers.get(p.id) || [] }))
            : all.map((p) => ({ id: p.id, name: p.name, has: false, count: p.count, shared: p.shared, covers: p.covers }));
        } catch (e) {
          mount(box, errorState(e.status === 404 ? { message: "Плейлисты пока недоступны на сервере" } : e, load));
          return;
        }
        if (!items.length) { mount(box, el("p", { class: "note", text: "Плейлистов пока нет — создайте первый." })); return; }
        mount(box, list(items.map((p) => addRow(p)), "sheet-list addto-rows"));
      };
      const addRow = (p) => {
        let has = !!p.has;
        const check = el("span", { class: "addto-check", "aria-hidden": "true" }, icon(has ? "check_circle" : "add_circle", { fill: has }));
        const row = listItem({ leading: mosaic(p.covers || [], { cls: "addto-mosaic" }), headline: p.name, lines: 1, trailing: check,
          onClick: async () => {
            if (busy) return;
            busy = true;
            row.classList.add("is-busy");
            try {
              if (!has) {
                const r = await pl.add(p.id, what);
                has = !!track;
                haptic("success");
                snackbar(track ? `Добавлено в «${p.name}»` : `В «${p.name}» добавлено: ${nTracks(r.added || 0)}${r.skipped ? ` · уже были: ${r.skipped}` : ""}`);
                changed(p.id);
                if (!track) close();
              } else {
                // убрать: нужен item_id — берём из плейлиста
                const full = await pl.get(p.id);
                const it = (full.tracks || []).find((x) => x.id === track.id);
                if (it) await pl.remove(p.id, [it.item_id]);
                has = false;
                haptic("light");
                snackbar(`Убрано из «${p.name}»`);
                changed(p.id);
              }
              mount(check, icon(has ? "check_circle" : "add_circle", { fill: has }));
              main.setAttribute("aria-pressed", has ? "true" : "false");
            } catch (e) { haptic("error"); snackbar(e.message, { error: true }); }
            finally { busy = false; row.classList.remove("is-busy"); }
          } });
        const main = row.querySelector(".li-main");
        if (track) main.setAttribute("aria-pressed", has ? "true" : "false");
        row.classList.toggle("is-on", has);
        return row;
      };
      let busy = false;
      load();
    },
  });
}

// ---------------------------------------------------------------- «Поделиться»
export async function sharePlaylist(p) {
  let invite = p.invite;
  const on = "invite_enabled" in p ? p.invite_enabled : p.shared;
  if (!on || !invite) {
    if (p.role !== "owner") { snackbar("Поделиться может только владелец плейлиста"); return null; }
    const ok = await dialog({ icon: "group", headline: p.shared ? "Включить ссылку-приглашение?" : "Сделать плейлист общим?", body: "По ссылке друзья из бота смогут присоединиться и редактировать плейлист. Доступ можно выключить в любой момент.",
      actions: [{ label: "Отмена", value: false }, { label: "Сделать общим", value: true }] });
    if (!ok) return null;
    try { const r = await pl.share(p.id, true); invite = r.invite; p.shared = true; p.invite_enabled = true; p.invite = invite; changed(p.id); } catch (e) { snackbar(e.message, { error: true }); return null; }
  }
  if (!invite) return null;
  const url = invite.link || invite.app_link;
  const text = `Плейлист «${p.name}» в PiMusic — присоединяйтесь`;
  haptic("light");
  if (invite.link && openTelegramLink("https://t.me/share/url?url=" + encodeURIComponent(url) + "&text=" + encodeURIComponent(text))) return invite;
  const ok = await copyText(url);
  snackbar(ok ? "Ссылка-приглашение скопирована" : "Не удалось скопировать ссылку", { error: !ok });
  return invite;
}

export async function copyInvite(invite) {
  const url = invite && (invite.link || invite.app_link);
  if (!url) return;
  const ok = await copyText(url);
  haptic(ok ? "success" : "error");
  snackbar(ok ? "Ссылка скопирована" : "Не удалось скопировать", { error: !ok });
}

// Кнопка с индикатором на время запроса
export async function busyCall(btn, fn) {
  setBusy(btn, true);
  try { return await fn(); } finally { setBusy(btn, false); }
}

export { button };

// ---------------------------------------------------------------- выбор плейлиста (для каталога: «Скачать в плейлист»)
// Возвращает {id, name} или null. «Новый плейлист» — создаётся пустым и сразу выбирается.
export function pickPlaylist({ title = "Скачать в плейлист", note = "", scope = null } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const sh = sheet({
      title, label: title, scope, cls: "sheet-addto" + (shell.twoPane ? " sheet-dialog" : ""),
      build(body, close) {
        if (note) body.appendChild(el("p", { class: "note", text: note }));
        const box = el("div", { class: "addto-list" });
        const newRow = listItem({ leading: el("span", { class: "li-icon-box li-icon-new" }, icon("add")), headline: "Новый плейлист", lines: 1, cls: "addto-new",
          onClick: async () => { close(); const p = await createPlaylistDialog(); finish(p ? { id: p.id, name: p.name } : null); } });
        body.append(list([newRow], "sheet-list"), box);
        mount(box, skeletonList(3, { thumb: false }));
        pl.list().then((r) => {
          const items = (r.items || []);
          if (!items.length) { mount(box, el("p", { class: "note", text: "Плейлистов пока нет — создайте первый." })); return; }
          mount(box, list(items.map((p) => listItem({ leading: mosaic(p.covers || [], { cls: "addto-mosaic" }), headline: p.name, supporting: nTracks(p.count || 0), lines: 2,
            onClick: () => { close(); finish({ id: p.id, name: p.name }); } })), "sheet-list addto-rows"));
        }, (e) => mount(box, errorState(e)));
      },
    });
    // закрыли лист без выбора
    const d = sh.el;
    new MutationObserver((_, mo) => { if (!d.isConnected) { mo.disconnect(); setTimeout(() => finish(null), 0); } }).observe(document.body, { childList: true });
  });
}
