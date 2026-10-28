// «Скачанное» (оболочка Windows 2.0, features.cache): треки на устройстве — слушать без сети, размер, удаление;
// идущие загрузки — с волнистым круговым прогрессом и отменой. Без моста страница не открывается.
import { el, mount, nTracks } from "../dom.js";
import { iconButton, list, listItem, emptyState, dialog, snackbar, wavyCircular, announce } from "../ui.js";
import { topBar, trackScroll } from "../header.js";
import { open, back } from "../shell.js";
import { haptic } from "../tg.js";
import { libraryRow, playButtons, currentFab } from "../libtrack.js";
import { cacheOn, onOffline, offlineItems, offlinePending, deleteOffline, reload, fmtBytes } from "../offline.js";

const SOURCE = { kind: "offline", id: "all", title: "Скачанное" };

export function openOffline({ from = "detail" } = {}) {
  if (!cacheOn()) return;
  open((page) => build(page), { kind: "offline", from });
}

function build(page) {
  // «Удалить всё» — icon button в trailing шапки
  const delAll = iconButton("delete_sweep", "Удалить всё скачанное", { onClick: () => removeAll() });
  const bar = topBar({ title: "Скачанное", leading: iconButton("arrow_back", "Назад", { cls: "tbar-back", onClick: () => back() }), trailing: delAll });
  async function removeAll() {
    const items = offlineItems();
    if (!items.length) return;
    const total = items.reduce((s, it) => s + (it.size || 0), 0);
    haptic("warning");
    const ok = await dialog({ icon: "delete_sweep", headline: "Удалить всё скачанное?", body: `${nTracks(items.length)} (${fmtBytes(total)}) пропадут с этого устройства. В медиатеке они останутся.`,
      actions: [{ label: "Отмена", value: false }, { label: "Удалить", value: true, variant: "danger" }] });
    if (!ok) return;
    try { await deleteOffline("all"); haptic("success"); snackbar("Скачанное удалено"); } catch (e) { snackbar(e.message || "Не удалось удалить", { error: true }); }
  }
  const head = el("div", { class: "off-head" });
  const body = el("div", { class: "page-body" });
  page.scroll.append(bar, head, body);
  trackScroll(page.el, page.scroll);
  const fab = currentFab(page.el, page.scroll, body);
  page.onDestroy(() => fab.destroy());

  // трек из метаданных скачанного (сервер не нужен: поток — с диска через cacheUrl)
  const asTrack = (it) => ({ ...(it.meta || {}), id: it.meta && it.meta.id !== undefined ? it.meta.id : it.trackId, owned: true, _size: it.size });

  function render() {
    const items = offlineItems();
    const tracks = items.map(asTrack);
    const total = items.reduce((s, it) => s + (it.size || 0), 0);
    bar.setSub(items.length ? `${nTracks(items.length)} · ${fmtBytes(total)}` : "");
    delAll.hidden = !items.length;
    mount(head, items.length ? el("div", { class: "pl-actions off-actions" }, playButtons(tracks, SOURCE, { label: "Скачанное" })) : null);
    // идущие загрузки
    const progressRows = offlinePending().map((p) => listItem({
      leading: wavyCircular({ value: p.total ? p.downloaded / p.total : null, size: 40, label: "Скачивается" }),
      headline: p.title || "Трек", supporting: p.total ? `скачивается · ${fmtBytes(p.downloaded)} из ${fmtBytes(p.total)}` : "скачивается…", lines: 2, cls: "off-going",
      trailing: iconButton("close", `Отменить: ${p.title || "трек"}`, { onClick: () => deleteOffline(p.trackId).catch(() => {}) }),
    }));
    if (!items.length && !progressRows.length) {
      mount(body, emptyState({ shape: "cookie4", icon: "download_for_offline", title: "Ничего не скачано",
        text: "В меню трека, альбома или плейлиста — «Скачать на устройство»: такие треки играют без сети." }));
      return;
    }
    const rows = tracks.map((t, i) => libraryRow(t, { thumb: true, tracks, index: i, source: SOURCE, deletable: false, sub: [t.artist, fmtBytes(t._size)].filter(Boolean).join(" · ") }));
    mount(body, [progressRows.length ? list(progressRows, "off-progress") : null, rows.length ? list(rows, "off-list") : null]);
  }
  const off = onOffline((what, p) => {
    if (!page.el.isConnected) { off(); return; }
    if (what === "error" && p && p.message && !/отмен/i.test(p.message)) snackbar(`Не скачалось: ${p.message}`, { error: true });
    if (what === "done") announce("Трек скачан на устройство");
    render();
  });
  page.onDestroy(off);
  render();
  reload();
}

