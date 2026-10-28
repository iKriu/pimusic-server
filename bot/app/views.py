"""Отображение хода загрузок в Telegram: реакции, живой отчёт по плейлисту, карточка трека.

Классы здесь не знают про Service — это «наблюдатели» (Job.view), которых воркер дёргает методами
started / progress / finished / completed. Всё общение с Telegram — через объект Bot (в тестах — мок).
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from .config import CFG

log = logging.getLogger(__name__)

TG_LIMIT = 4096
CAPTION_LIMIT = 1024

# Только эмодзи из стандартного набора реакций Telegram
R_ACCEPTED = "👀"
R_WORKING = "⚡"
R_OK = "👍"
R_PARTIAL = "🤔"
R_FAIL = "👎"

Buttons = Sequence[Sequence[tuple[str, str]]]


# ------------------------------------------------------------------ helpers
def esc(s: Any, limit: int = 0) -> str:
    s = "" if s is None else str(s)
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return html.escape(s, quote=False)


def is_not_modified(e: Exception) -> bool:
    return "not modified" in str(e).lower()


def keyboard(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    """[[(текст, callback_data)], …] -> InlineKeyboardMarkup. Значение-ссылка (http/https) — URL-кнопка."""
    if not buttons:
        return None
    rows = []
    for row in buttons:
        out = []
        for text, data in row:
            if data.startswith(("http://", "https://")):
                out.append(InlineKeyboardButton(text, url=data))
            else:
                out.append(InlineKeyboardButton(text, callback_data=data))
        if out:
            rows.append(out)
    return InlineKeyboardMarkup(rows) if rows else None


def progress_bar(done: int, total: int, width: int = 10) -> str:
    pct = int(done * 100 / total) if total else 100
    filled = min(width, pct * width // 100)
    return "▰" * filled + "▱" * (width - filled) + f" {pct}%"


def final_reaction(ok: int, total: int) -> str:
    if total and ok >= total:
        return R_OK
    return R_PARTIAL if ok else R_FAIL


def jellyfin_base() -> str:
    return CFG.jellyfin_public_base


def jellyfin_search_url(title: str) -> str:
    return f"{jellyfin_base()}/web/#/search.html?query={quote(title or '')}"


def fit_lines(lines: Sequence[str], priority: Iterable[int], budget: int) -> tuple[list[str], int]:
    """Выбрать строки по приоритету так, чтобы уложиться в budget символов; вернуть их в исходном порядке
    и число не поместившихся."""
    if sum(len(x) + 1 for x in lines) <= budget:
        return list(lines), 0
    budget -= 24  # место под «…и ещё K»
    keep: set[int] = set()
    used = 0
    for i in priority:
        if i in keep:
            continue
        cost = len(lines[i]) + 1
        if used + cost > budget:
            continue
        keep.add(i)
        used += cost
    return [lines[i] for i in sorted(keep)], len(lines) - len(keep)


def blockquote(lines: Sequence[str], more: int = 0) -> str:
    body = "\n".join(lines) + (f"\n…и ещё {more}" if more else "")
    return f"<blockquote expandable>{body}</blockquote>"


# ------------------------------------------------------------------ Telegram primitives
class Reactor:
    """Реакция на сообщение пользователя. Ошибки Telegram (реакции выключены в чате) глотаем."""

    def __init__(self, bot: Any, chat_id: int | None, message_id: int | None):
        self.bot, self.chat_id, self.message_id = bot, chat_id, message_id
        self.current: str | None = None

    async def set(self, emoji: str) -> None:
        if self.bot is None or self.chat_id is None or self.message_id is None or emoji == self.current:
            return
        self.current = emoji
        try:
            await self.bot.set_message_reaction(self.chat_id, self.message_id, reaction=emoji)
        except TelegramError as e:
            log.debug("reaction %s failed: %s", emoji, e)


class StatusMessage:
    """Одно служебное сообщение бота: первый show() отправляет его, следующие — редактируют."""

    def __init__(self, bot: Any, chat_id: int, message_id: int | None = None, reply_to: int | None = None):
        self.bot, self.chat_id, self.message_id, self.reply_to = bot, chat_id, message_id, reply_to

    def _reply(self) -> ReplyParameters | None:
        return ReplyParameters(self.reply_to, allow_sending_without_reply=True) if self.reply_to else None

    async def show(self, text: str, *, html_mode: bool = False, markup: InlineKeyboardMarkup | None = None) -> None:
        text = text[:TG_LIMIT]
        mode = ParseMode.HTML if html_mode else None
        try:
            if self.message_id is not None:
                try:
                    await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id,
                                                     parse_mode=mode, reply_markup=markup)
                    return
                except BadRequest as e:
                    if is_not_modified(e):
                        return
                    if "not found" not in str(e).lower() and "can't be edited" not in str(e).lower():
                        raise
                    self.message_id = None  # сообщение удалили — отправим новое
            m = await self.bot.send_message(self.chat_id, text, parse_mode=mode, reply_markup=markup,
                                            reply_parameters=self._reply())
            self.message_id = getattr(m, "message_id", None)
        except TelegramError as e:
            log.warning("status message failed: %s", e)

    async def delete(self) -> None:
        if self.message_id is None:
            return
        try:
            await self.bot.delete_message(self.chat_id, self.message_id)
        except TelegramError as e:
            log.debug("delete status failed: %s", e)
        self.message_id = None


# ------------------------------------------------------------------ live report for multi-track jobs
QUEUED, WORK, OK, CACHED, FAIL = "queued", "work", "ok", "cached", "fail"
ICONS = {QUEUED: "⏳", WORK: "⏳", OK: "✅", CACHED: "♻️", FAIL: "❌"}


@dataclass
class _Item:
    label: str
    state: str = QUEUED
    note: str = ""


class LiveReport:
    """Одно сообщение с ходом всего задания, редактируется не чаще min_interval секунд (+ финальная правка)."""

    MAX_ACTIVE = 5

    def __init__(self, title: str, reqs: Sequence[Any], edit: Callable[[str], Awaitable[Any]], *,
                 subtitle: str = "", reactor: Reactor | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, min_interval: float = 3.0,
                 limit: int = TG_LIMIT):
        self.title, self.subtitle = title, subtitle
        self.items = [_Item(label=getattr(r, "label", str(r))) for r in reqs]
        self._reqs = list(reqs)
        self._index = {id(r): i for i, r in enumerate(reqs)}
        self._edit, self.reactor = edit, reactor
        self._clock, self._sleep = clock, sleep
        self.min_interval, self.limit = min_interval, limit
        self._finish_order: list[int] = []
        self._last_edit: float | None = None
        self._last_text = ""
        self._pending: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.final = False
        self.edits = 0

    # -------------------------------------------------------- state
    def _idx(self, req: Any) -> int | None:
        i = self._index.get(id(req))
        if i is not None:
            return i
        for j, r in enumerate(self._reqs):
            if r == req:
                return j
        return None

    def _set(self, req: Any, state: str, note: str = "") -> bool:
        i = self._idx(req)
        if i is None:
            return False
        it = self.items[i]
        if it.state in (OK, CACHED, FAIL) and state in (QUEUED, WORK):
            return False  # опоздавший прогресс уже готового трека
        it.state, it.note = state, note
        if state in (OK, CACHED, FAIL):
            if i in self._finish_order:
                self._finish_order.remove(i)
            self._finish_order.append(i)
        return True

    def counts(self) -> dict[str, int]:
        c = {QUEUED: 0, WORK: 0, OK: 0, CACHED: 0, FAIL: 0}
        for it in self.items:
            c[it.state] += 1
        return c

    # -------------------------------------------------------- events (Job.view)
    async def start(self) -> None:
        """Показать исходное состояние (все треки в очереди)."""
        await self._flush(force=True)

    async def started(self, req: Any) -> None:
        if self._set(req, WORK, "ищу в источниках…"):
            await self._changed()

    async def progress(self, req: Any, text: str) -> None:
        if self.reactor:
            await self.reactor.set(R_WORKING)
        if self._set(req, WORK, text):
            await self._changed()

    async def finished(self, req: Any, outcome: Any) -> None:
        state = (CACHED if getattr(outcome, "reused", False) else OK) if outcome.ok else FAIL
        if self._set(req, state):
            await self._changed()

    async def completed(self, results: Sequence[Any]) -> None:
        for r in results:  # results надёжнее событий: последний finished может прийти позже completed
            self._set(r.req, (CACHED if r.reused else OK) if r.ok else FAIL)
        self.final = True
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._pending = None
        await self._flush(force=True)
        if self.reactor:
            c = self.counts()
            await self.reactor.set(final_reaction(c[OK] + c[CACHED], len(self.items)))

    # -------------------------------------------------------- throttling
    async def _changed(self) -> None:
        if self.final:
            return
        now = self._clock()
        if self._last_edit is None or now - self._last_edit >= self.min_interval:
            await self._flush()
        elif self._pending is None or self._pending.done():
            delay = self.min_interval - (now - self._last_edit)
            self._pending = asyncio.ensure_future(self._trailing(delay))

    async def _trailing(self, delay: float) -> None:
        try:
            await self._sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return
        self._pending = None
        if not self.final:
            await self._flush()

    async def _flush(self, force: bool = False) -> None:
        async with self._lock:
            if self.final and not force:
                return
            text = self.render()
            self._last_edit = self._clock()
            if text == self._last_text:
                return
            self._last_text = text
            self.edits += 1
            try:
                await self._edit(text)
            except Exception:  # noqa: BLE001 — отчёт не должен ронять загрузку
                log.exception("live report edit failed")

    # -------------------------------------------------------- rendering
    def _line(self, i: int) -> str:
        it = self.items[i]
        return f"{ICONS[it.state]} {i + 1}. {esc(it.label, 90)}"

    def render(self) -> str:
        c = self.counts()
        total = len(self.items)
        processed = c[OK] + c[CACHED] + c[FAIL]
        got = c[OK] + c[CACHED]
        parts: list[str] = []
        if self.final:
            head = "✅ Готово" if got else "❌ Ничего не нашлось"
            parts.append(f"<b>{head}: {esc(self.title, 200)}</b>")
            if self.subtitle:
                parts.append(f"<i>{esc(self.subtitle, 300)}</i>")
            summary = f"Добавлено <b>{got}</b> из {total}"
            if c[CACHED]:
                summary += f" (♻️ {c[CACHED]} уже были в общем кэше — место на диске не тратилось)"
            parts.append(summary)
        else:
            parts.append(f"📥 <b>{esc(self.title, 200)}</b>")
            if self.subtitle:
                parts.append(f"<i>{esc(self.subtitle, 300)}</i>")
            parts.append(f"{progress_bar(processed, total)} · {processed} из {total}")
        parts.append(f"✅ {c[OK]}  ♻️ {c[CACHED]}  ❌ {c[FAIL]}  ⏳ {c[QUEUED] + c[WORK]}")
        active = [i for i, it in enumerate(self.items) if it.state == WORK]
        if active and not self.final:
            lines = [f"⏳ {esc(self.items[i].label, 90)}" + (f" — <i>{esc(self.items[i].note, 80)}</i>"
                                                            if self.items[i].note else "")
                     for i in active[: self.MAX_ACTIVE]]
            if len(active) > self.MAX_ACTIVE:
                lines.append(f"…и ещё {len(active) - self.MAX_ACTIVE} в работе")
            parts.append("\n<b>Сейчас качается:</b>\n" + "\n".join(lines))
        head = "\n".join(parts)
        budget = self.limit - len(head) - 120  # заголовки секций и теги
        tail: list[str] = []
        if self.final:
            failed = [i for i, it in enumerate(self.items) if it.state == FAIL]
            if failed:
                flines = [f"• {esc(self.items[i].label, 90)}" for i in failed]
                fl, more = fit_lines(flines, range(len(flines)), budget)
                block = f"\n❌ <b>Не нашлось ({len(failed)}):</b>\n" + blockquote(fl, more)
                tail.append(block)
                budget -= len(block)
        if budget > 150:
            lines = [self._line(i) for i in range(total)]
            # приоритет: в работе, последние завершённые, затем очередь по порядку
            prio = active + list(reversed(self._finish_order)) + list(range(total))
            shown, more = fit_lines(lines, prio, budget)
            tail.append("\n<b>Треки:</b>\n" + blockquote(shown, more))
        text = head + "\n" + "\n".join(tail) if tail else head
        return text[: self.limit]


# ------------------------------------------------------------------ single track: status + card
def card_markup(track_id: int, title: str, fav: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("💔 Убрать из избранного" if fav else "❤️ В избранное",
                                  callback_data=f"fav:{track_id}"),
             InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{track_id}")]]
    url = jellyfin_search_url(title)
    if url.startswith("https://"):  # http/IP-ссылку Telegram может отвергнуть (BadRequest) — и карточки не будет
        rows.append([InlineKeyboardButton("▶️ Открыть в Jellyfin", url=url)])
    return InlineKeyboardMarkup(rows)


def card_caption(outcome: Any) -> str:
    t = outcome.track
    icon = "♻️" if outcome.reused else "✅"
    from_cache = outcome.source == "cache" or (outcome.reused and not outcome.source)
    raw = {"artist": t.artist or "", "title": t.title or "", "album": t.album or "",
           "source": "" if from_cache else (outcome.source or ""),
           "flags": " · ".join(outcome.flags) if outcome.flags else ""}
    limits = {"artist": 150, "title": 200, "album": 150, "source": 150, "flags": 200}

    def build() -> str:
        lines = [f"{icon} <b>{esc(raw['artist'], limits['artist'])} — {esc(raw['title'], limits['title'])}</b>"]
        if raw["album"]:
            lines.append(f"💿 {esc(raw['album'], limits['album'])}")
        if from_cache:
            lines.append("♻️ из общего кэша — место на диске не тратилось")
        elif raw["source"]:
            lines.append(f"📡 {esc(raw['source'], limits['source'])} → MP3 320")
        if raw["flags"]:
            lines.append(esc(raw["flags"], limits["flags"]))
        return "\n".join(lines)

    # готовый HTML не режем (порвутся теги/сущности) — укорачиваем самое длинное сырое поле до экранирования
    text = build()
    while len(text) > CAPTION_LIMIT:
        eff = {k: min(len(v), limits[k]) for k, v in raw.items()}
        key = max(eff, key=lambda k: eff[k])
        if eff[key] <= 1:
            break
        limits[key] = max(1, eff[key] - max(1, len(text) - CAPTION_LIMIT))
        text = build()
    return text


def cover_path(track: Any) -> Path | None:
    try:
        p = Path(track.path).parent / "cover.jpg"
        return p if p.is_file() else None
    except (OSError, TypeError, ValueError):
        return None


class SingleTrackView:
    """Один трек: «⏳ …» в статусном сообщении, затем карточка с обложкой и кнопками (или ❌)."""

    def __init__(self, status: StatusMessage, reactor: Reactor | None = None, *,
                 clock: Callable[[], float] = time.monotonic, min_interval: float = 2.0,
                 is_favorite: Callable[[Any], Awaitable[bool | None]] | None = None):
        self.status, self.reactor = status, reactor
        self.is_favorite = is_favorite  # для уже бывшего в медиатеке трека: стоит ли ❤️ в Jellyfin
        self._clock, self.min_interval = clock, min_interval
        self._last: float | None = None
        self.card_message_id: int | None = None

    async def started(self, req: Any) -> None:
        self._last = self._clock()
        await self.status.show(f"⏳ {req.label}: ищу в источниках…")

    async def progress(self, req: Any, text: str) -> None:
        if self.reactor:
            await self.reactor.set(R_WORKING)
        now = self._clock()
        if self._last is not None and now - self._last < self.min_interval:
            return
        self._last = now
        await self.status.show(f"⏳ {req.label}: {text}")

    async def finished(self, req: Any, outcome: Any) -> None:
        if outcome.ok and outcome.track is not None:
            await self._card(outcome)
        else:
            await self.status.show(outcome.text)
        if self.reactor:
            await self.reactor.set(R_OK if outcome.ok else R_FAIL)

    async def _initial_fav(self, outcome: Any) -> bool:
        if not outcome.reused or self.is_favorite is None:
            return False
        try:  # best effort: Jellyfin медленный/недоступен — просто пустое сердечко
            return bool(await asyncio.wait_for(self.is_favorite(outcome.track), 3))
        except Exception as e:  # noqa: BLE001
            log.debug("favorite lookup failed: %s", e)
            return False

    async def _card(self, outcome: Any) -> None:
        t = outcome.track
        caption, kb = card_caption(outcome), card_markup(t.id, t.title, fav=await self._initial_fav(outcome))
        cover = cover_path(t)
        if cover is not None:
            try:
                data = await asyncio.to_thread(cover.read_bytes)
                st = self.status
                try:
                    m = await st.bot.send_photo(st.chat_id, photo=data, caption=caption, parse_mode=ParseMode.HTML,
                                                reply_markup=kb, reply_parameters=st._reply())
                except BadRequest as e:  # возможно, Telegram отверг кнопки — ещё раз без них
                    log.warning("track card photo rejected, retrying without buttons: %s", e)
                    m = await st.bot.send_photo(st.chat_id, photo=data, caption=caption, parse_mode=ParseMode.HTML,
                                                reply_parameters=st._reply())
                self.card_message_id = getattr(m, "message_id", None)
                await st.delete()
                return
            except (TelegramError, OSError) as e:
                log.warning("track card photo failed, falling back to text: %s", e)
        await self.status.show(caption, html_mode=True, markup=kb)
        self.card_message_id = self.status.message_id
