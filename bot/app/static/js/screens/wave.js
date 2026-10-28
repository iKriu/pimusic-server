// Волна: hero-карточка (shape extra-extra-large) с коллажем обложек любимых альбомов в контент-цвете,
// cookie-кнопка «Слушать» — переключатель встроенного плеера (морфинг cookie ↔ скруглённый квадрат),
// карточка «Сейчас в эфире» с аудиореактивной волной, tonal «Обновить плейлист» / «Найти новое».
import { el, icon, mount, pic, nTracks, setText } from "../dom.js";
import { api, isAbort } from "../api.js";
import { button, iconButton, list, listItem, snackbar, emptyState, errorState, loadingIndicator, setBusy, sectionTitle, openMenu } from "../ui.js";
import { topBar, trackScroll } from "../header.js";
import { contentColor } from "../theme.js";
import { haptic, openExternal } from "../tg.js";
import * as store from "../store.js";
import { go } from "../shell.js";
import { player, onPlayer, isActive, retry } from "../player.js";
import { playToggle, waveViz, statusText, titleText, coverColor } from "../nowplaying.js";

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fallback */ }
  const ta = el("textarea", { style: { position: "fixed", opacity: "0" } });
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

export function createWave() {
  const scroller = el("div", { class: "scroll" });
  const view = el("section", { class: "view root root-wave", "aria-label": "Волна" }, scroller);
  const bar = topBar({ title: "Волна", subtitle: "Персональное радио по вашим лайкам" });
  const box = el("div", { class: "wave-content" });
  scroller.append(bar, box);
  trackScroll(view, scroller);
  let built = false;

  function build() {
    const me = store.state.me;
    if (!me) { mount(box, el("div", { class: "state" }, loadingIndicator())); return; }
    built = true;
    const collage = el("div", { class: "wave-collage", "aria-hidden": "true" });
    const result = el("div", { class: "wave-result", "aria-live": "polite" });
    const emptyLib = () => mount(result, emptyState({ shape: "clover", icon: "library_music", title: "Волне нечего играть",
      text: "В медиатеке пока нет треков — эфир собирается из вашей музыки. Найдите и скачайте пару любимых песен.", compact: true,
      action: button({ label: "Найти музыку", icon: "search", variant: "tonal", onClick: () => go("search", { focus: true }) }) }));
    const listen = playToggle({ onEmpty: emptyLib });
    const copyLink = async () => {
      const ok = await copyText(me.stream_url);
      haptic(ok ? "success" : "error");
      snackbar(ok ? "Ссылка на поток скопирована" : "Не удалось скопировать", { error: !ok });
    };
    const copy = iconButton("content_copy", "Скопировать ссылку на поток", { variant: "tonal", onClick: copyLink });
    const more = iconButton("more_vert", "Ещё о потоке", { variant: "tonal", onClick: () => {
      haptic("light");
      more.setAttribute("aria-expanded", "true");
      openMenu(more, [
        { icon: "open_in_new", label: "Открыть поток в плеере", supporting: "VLC, браузер или другое приложение", onClick: () => openExternal(me.stream_url) },
        { icon: "content_copy", label: "Скопировать ссылку", onClick: copyLink },
      ], () => more.setAttribute("aria-expanded", "false"));
    } });
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    const hero = el("section", { class: "wave-hero", "aria-label": "Моя Волна" },
      collage,
      el("div", { class: "wave-hero-text" },
        el("p", { class: "wave-over", text: "Бесконечный поток" }),
        el("h2", { class: "wave-title", text: "Моя Волна" }),
        el("p", { class: "wave-desc", text: "Радио из вашей медиатеки: любимое вперемешку с тем, что давно не играло." })),
      el("div", { class: "wave-hero-actions" }, listen, el("div", { class: "wave-hero-more" }, copy, more)));

    const np = nowPlayingCard();
    const refresh = button({ label: "Обновить плейлист", icon: "refresh", variant: "tonal", onClick: async () => {
      haptic("medium");
      setBusy(refresh, true, "Собираю плейлист…");
      try {
        const r = await api("/wave/refresh", { method: "POST", body: {} });
        haptic("success");
        mount(result, r.tracks
          ? el("div", { class: "card card-outlined wave-note" }, icon("check_circle", { fill: true, class: "ok" }), el("p", { text: `Плейлист «Моя Волна» обновлён: ${nTracks(r.tracks)}. Откройте его в Jellyfin или Finamp.` }))
          : emptyState({ icon: "library_music", title: "Медиатека пока пуста", text: "Добавьте пару треков — и плейлист соберётся.", compact: true }));
      } catch (e) { if (!isAbort(e)) { haptic("error"); mount(result, errorState(e)); } }
      finally { setBusy(refresh, false); }
    } });
    const discover = button({ label: "Найти новое", icon: "auto_awesome", variant: "tonal", onClick: async () => {
      haptic("medium");
      setBusy(discover, true, "Слушаю ваши лайки…");
      mount(result, el("div", { class: "state state-compact" }, loadingIndicator({ size: 56, contained: true, label: "Подбираю треки" }), el("p", { class: "state-text", text: "Подбираю похожее на любимое…" })));
      try {
        const r = await api("/wave/discover", { method: "POST", body: {} });
        if (!r.seeds.length) {
          mount(result, emptyState({ shape: "clover", icon: "auto_awesome", title: "Не на что опереться", text: "Добавьте пару треков и поставьте им ❤ в Jellyfin.", compact: true }));
          return;
        }
        haptic(r.queued.length ? "success" : "light");
        mount(result, el("div", { class: "card card-filled wave-found" },
          el("p", { class: "note", text: "На основе: " + r.seeds.slice(0, 5).join("; ") + (r.seeds.length > 5 ? "…" : "") }),
          r.queued.length
            ? [sectionTitle(`Качаю · ${r.queued.length}`), list(r.queued.map((q) => listItem({ leading: el("span", { class: "li-icon-box" }, icon("auto_awesome")), headline: q, lines: 1 }))),
              button({ label: "Смотреть загрузки", icon: "download", variant: "text", onClick: () => go("downloads") })]
            : el("p", { text: "Новых похожих треков не нашлось — всё похожее уже у вас." })));
        store.kick();
      } catch (e) {
        if (e.code === "busy") { haptic("warning"); snackbar("Волна уже подбирается — подождите немного"); mount(result, null); return; }
        haptic("error");
        mount(result, errorState(e));
      } finally { setBusy(discover, false); }
    } });

    const jf = el("section", { class: "card card-outlined jf-card", "aria-label": "Jellyfin" },
      el("span", { class: "jf-shape", "aria-hidden": "true" }, icon("library_music")),
      el("div", { class: "jf-text" }, el("h3", { text: "Jellyfin" }), el("p", { text: `${me.login} · ${nTracks(me.tracks)}` })),
      button({ label: "Открыть", icon: "open_in_new", variant: "text", onClick: () => openExternal(me.jellyfin_url) }));

    const url = el("div", { class: "card card-outlined url-card" }, el("p", { class: "note", text: "Ссылка на поток для VLC, плеера или умной колонки:" }), el("code", { class: "url", text: me.stream_url }));
    mount(box, [hero, np, el("div", { class: "wave-buttons" }, refresh, discover), result, jf, url]);

    // коллаж из обложек своих альбомов + контент-цвет первой
    api("/library/albums").then((r) => {
      const covers = r.items.filter((a) => a.cover).slice(0, 4);
      if (!covers.length) { collage.classList.add("is-empty"); mount(collage, el("span", { class: "collage-ph" }, icon("waves"))); return; }
      collage.dataset.n = String(covers.length);
      mount(collage, covers.map((a) => pic(a.cover, { cls: "collage-pic", ph: "album" })));
      contentColor(hero, covers[0].cover);
    }).catch(() => { collage.classList.add("is-empty"); mount(collage, el("span", { class: "collage-ph" }, icon("waves"))); });
  }

  return { el: view, scroller, show() { if (!built) build(); } };
}

// «Сейчас в эфире»: обложка текущего трека (контент-цвет), название/исполнитель, статус и волна
function nowPlayingCard() {
  const cover = el("span", { class: "np-cover" });
  const over = el("p", { class: "np-over" }, el("span", { class: "live-dot", "aria-hidden": "true" }), el("span", { class: "np-over-text" }));
  const title = el("h2", { class: "np-title" });
  const sub = el("p", { class: "np-sub" });
  const status = el("p", { class: "np-status", role: "status" });
  const actions = el("div", { class: "np-actions" });
  const card = el("section", { class: "np-card", "aria-label": "Сейчас в эфире", hidden: true },
    el("div", { class: "np-head" }, cover, el("div", { class: "np-text" }, over, title, sub, status)),
    waveViz(), actions);
  const memo = { url: undefined };
  let coverUrl;
  const render = (what) => {
    if (what === "time") return;
    card.hidden = !isActive() || player.mode !== "wave"; // очередь показывает «Сейчас играет», а не карточку эфира
    if (card.hidden) return;
    const n = player.now || {};
    card.dataset.state = player.state;
    setText(over.querySelector(".np-over-text"), player.state === "playing" ? "В эфире" + (n.listeners > 1 ? ` · слушателей: ${n.listeners}` : "") : player.state === "paused" ? "На паузе" : "Моя Волна");
    setText(title, titleText());
    const meta = [n.title ? n.artist : "", n.title ? n.album : ""].filter(Boolean).join(" · ");
    setText(sub, meta);
    sub.hidden = !meta;
    const st = player.state === "playing" ? (n.title ? "" : statusText()) : statusText();
    setText(status, st);
    status.hidden = !st;
    if (player.state === "error") {
      if (!actions.firstChild) {
        const tips = el("p", { class: "np-tip", text: "Проверьте связь. Если медиатека пуста, скачайте пару треков — эфир соберётся из них." });
        mount(actions, [tips, el("div", { class: "np-btns" },
          button({ label: "Повторить", icon: "refresh", variant: "filled", onClick: () => { haptic("medium"); retry(); } }),
          button({ label: "Найти музыку", icon: "search", variant: "text", onClick: () => go("search", { focus: true }) }))]);
      }
    } else if (actions.firstChild) actions.replaceChildren();
    const c = n.cover || null;
    if (c !== coverUrl) { coverUrl = c; cover.replaceChildren(pic(c, { cls: "np-pic", ph: "waves", eager: true })); }
    coverColor(card, c, memo);
  };
  onPlayer(render);
  render();
  return card;
}
