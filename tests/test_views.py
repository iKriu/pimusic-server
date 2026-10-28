"""Telegram views (живой отчёт, карточка трека, реакции, Messenger) — без сети: Bot замокан."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from app import views
from app.bot import Messenger
from app.sources import TrackRequest
from app.views import LiveReport, Reactor, SingleTrackView, StatusMessage


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def outcome(req, ok=True, reused=False, track=None, text="", source="", flags=None):
    return SimpleNamespace(req=req, ok=ok, reused=reused, track=track, text=text or ("✅" if ok else f"❌ {req.label}"),
                           source=source, flags=flags or [])


def reqs(n, prefix="Song"):
    return [TrackRequest(artist="Artist", title=f"{prefix} {i}") for i in range(n)]


def make_report(rs, **kw):
    edits = []

    async def edit(text):
        edits.append(text)

    kw.setdefault("min_interval", 0)
    rep = LiveReport("My playlist", rs, edit, **kw)
    return rep, edits


def fake_bot():
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=100)
    bot.send_photo.return_value = SimpleNamespace(message_id=101)
    return bot


def reactions(bot):
    return [c.kwargs["reaction"] for c in bot.set_message_reaction.await_args_list]


# ---------------------------------------------------------------- LiveReport
async def test_live_report_icons_and_counts():
    rs = reqs(5)
    rep, edits = make_report(rs)
    await rep.start()
    assert "⏳ 5" in edits[-1] and "<blockquote expandable>" in edits[-1]
    await rep.started(rs[0])
    await rep.progress(rs[0], "Soulseek: 50% от peer")
    assert "Сейчас качается" in edits[-1] and "Soulseek: 50% от peer" in edits[-1]
    await rep.finished(rs[0], outcome(rs[0]))
    await rep.finished(rs[1], outcome(rs[1], reused=True))
    await rep.finished(rs[2], outcome(rs[2], ok=False))
    text = edits[-1]
    assert "✅ 1. Artist — Song 0" in text
    assert "♻️ 2. Artist — Song 1" in text
    assert "❌ 3. Artist — Song 2" in text
    assert "⏳ 4. Artist — Song 3" in text
    assert "✅ 1  ♻️ 1  ❌ 1  ⏳ 2" in text
    assert "3 из 5" in text and "60%" in text


async def test_live_report_final_state():
    rs = reqs(3)
    rep, edits = make_report(rs)
    await rep.start()
    res = [outcome(rs[0]), outcome(rs[1], reused=True), outcome(rs[2], ok=False)]
    await rep.completed(res)
    text = edits[-1]
    assert text.startswith("<b>✅ Готово: My playlist</b>")
    assert "Добавлено <b>2</b> из 3" in text and "общем кэше" in text
    assert "❌ <b>Не нашлось (1):</b>\n<blockquote expandable>• Artist — Song 2</blockquote>" in text
    assert "Сейчас качается" not in text


async def test_live_report_nothing_found_title():
    rs = reqs(2)
    rep, edits = make_report(rs)
    await rep.completed([outcome(r, ok=False) for r in rs])
    assert edits[-1].startswith("<b>❌ Ничего не нашлось")


async def test_live_report_completed_wins_over_late_finished():
    rs = reqs(2)
    rep, edits = make_report(rs)
    await rep.completed([outcome(rs[0]), outcome(rs[1])])
    n = len(edits)
    await rep.finished(rs[1], outcome(rs[1]))  # опоздавшее событие воркера
    await rep.progress(rs[1], "late")
    assert len(edits) == n and "Готово" in edits[-1]


async def test_live_report_throttling_with_fake_clock():
    rs = reqs(4)
    clock = Clock()
    gate = asyncio.Event()
    sleeps = []

    async def sleep(d):
        sleeps.append(d)
        await gate.wait()

    rep, edits = make_report(rs, clock=clock, sleep=sleep, min_interval=3.0)
    await rep.start()                       # t=0 — первая правка
    assert len(edits) == 1
    clock.t = 1.0
    await rep.started(rs[0])                # слишком рано — отложенная правка
    clock.t = 1.5
    await rep.progress(rs[0], "Soulseek: 10%")
    await rep.finished(rs[1], outcome(rs[1]))
    assert len(edits) == 1
    await asyncio.sleep(0)
    assert sleeps == [2.0]                  # одна отложенная правка, а не по одной на событие
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(edits) == 2 and "Soulseek: 10%" in edits[-1] and "✅ 2." in edits[-1]
    clock.t = 3.0                           # 3.0 − 1.5 < 3 → снова ждём
    gate.clear()
    await rep.finished(rs[2], outcome(rs[2]))
    assert len(edits) == 2
    clock.t = 3.2
    await rep.completed([outcome(r) for r in rs])   # финальная правка — всегда, сразу
    assert len(edits) == 3 and "Готово" in edits[-1]
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(edits) == 3                  # отменённая отложенная правка не перетёрла финал


async def test_live_report_skips_identical_edits():
    rs = reqs(2)
    rep, edits = make_report(rs)
    await rep.start()
    await rep.start()
    assert len(edits) == 1


async def test_live_report_500_tracks_fit_telegram_limit():
    rs = [TrackRequest(artist="Очень длинное имя исполнителя & Co", title=f"Песня номер {i} (Remastered Deluxe)")
          for i in range(500)]
    rep, edits = make_report(rs)
    await rep.start()
    assert len(edits[-1]) <= 4096 and "…и ещё" in edits[-1]
    for r in rs[:300]:
        await rep.finished(r, outcome(r))
    for r in rs[300:400]:
        await rep.finished(r, outcome(r, ok=False))
    await rep.started(rs[400])
    text = edits[-1]
    assert len(text) <= 4096 and text.count("<blockquote expandable>") == 1 and text.endswith("</blockquote>")
    assert "…и ещё" in text
    assert "400. Очень длинное" in text     # последний завершённый виден
    assert "⏳ Очень длинное имя исполнителя &amp; Co — Песня номер 400" in text   # в работе
    await rep.completed([outcome(r) for r in rs[:300]] + [outcome(r, ok=False) for r in rs[300:]])
    final = edits[-1]
    assert len(final) <= 4096 and "Не нашлось (200)" in final and final.endswith("</blockquote>")


async def test_live_report_escapes_html():
    rs = [TrackRequest(artist="<b>Evil</b>", title="Tom & Jerry <script>")]
    rep, edits = make_report(rs + reqs(1), subtitle="<i>seed</i>")
    await rep.start()
    await rep.progress(rs[0], "<img src=x>")
    text = edits[-1]
    assert "<script>" not in text and "<img" not in text and "<b>Evil" not in text
    assert "&lt;b&gt;Evil&lt;/b&gt; — Tom &amp; Jerry &lt;script&gt;" in text
    assert "&lt;i&gt;seed&lt;/i&gt;" in text


async def test_live_report_reactions():
    bot = fake_bot()
    rs = reqs(2)
    rep, _ = make_report(rs, reactor=Reactor(bot, 5, 3))
    await rep.progress(rs[0], "x")
    await rep.progress(rs[1], "y")
    await rep.completed([outcome(rs[0]), outcome(rs[1], ok=False)])
    assert reactions(bot) == ["⚡", "🤔"]


async def test_live_report_edit_failure_does_not_raise():
    async def edit(_):
        raise RuntimeError("tg down")
    rep = LiveReport("t", reqs(2), edit, min_interval=0)
    await rep.start()


def test_final_reaction():
    assert views.final_reaction(3, 3) == "👍"
    assert views.final_reaction(1, 3) == "🤔"
    assert views.final_reaction(0, 3) == "👎"


# ---------------------------------------------------------------- Reactor / StatusMessage
async def test_reactor_swallows_telegram_error_and_dedupes():
    bot = fake_bot()
    bot.set_message_reaction.side_effect = Forbidden("reactions disabled")
    r = Reactor(bot, 1, 2)
    await r.set("👀")
    await r.set("👀")
    assert bot.set_message_reaction.await_count == 1
    await Reactor(None, None, None).set("👀")  # без сообщения — ничего


async def test_status_message_sends_then_edits_and_ignores_not_modified():
    bot = fake_bot()
    st = StatusMessage(bot, 5, reply_to=3)
    await st.show("one")
    assert st.message_id == 100
    assert bot.send_message.await_args.kwargs["reply_parameters"].message_id == 3
    bot.edit_message_text.side_effect = BadRequest("Message is not modified: specified new message content")
    await st.show("one")
    assert bot.send_message.await_count == 1


async def test_status_message_resends_when_deleted():
    bot = fake_bot()
    st = StatusMessage(bot, 5, message_id=7)
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await st.show("x")
    assert bot.send_message.await_count == 1 and st.message_id == 100


# ---------------------------------------------------------------- SingleTrackView
def make_track(tmp_path, cover=True, **kw):
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    p = d / "01.mp3"
    p.write_bytes(b"x")
    if cover:
        (d / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    data = dict(id=42, artist="Muse <live>", title="Uprising & Co", album="The Resistance", path=str(p))
    data.update(kw)
    return SimpleNamespace(**data)


def make_single(bot, clock=None):
    reactor = Reactor(bot, 5, 3)
    st = StatusMessage(bot, 5, message_id=7, reply_to=3)
    kw = {"clock": clock} if clock else {}
    return SingleTrackView(st, reactor, **kw), reactor


async def test_single_view_card_with_cover_and_reactions(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAM_PUBLIC_URL", "https://music.example.com")
    bot = fake_bot()
    view, reactor = make_single(bot)
    req = TrackRequest(artist="Muse", title="Uprising")
    await reactor.set("👀")
    await view.started(req)
    assert "⏳ Muse — Uprising" in bot.edit_message_text.await_args.args[0]
    await view.progress(req, "Soulseek: 50% от peer")
    track = make_track(tmp_path)
    await view.finished(req, outcome(req, track=track, source="Soulseek (peer, 320)",
                                     flags=["🖼 обложка", "🎤 синхр. текст"]))
    assert reactions(bot) == ["👀", "⚡", "👍"]
    bot.send_photo.assert_awaited_once()
    kw = bot.send_photo.await_args.kwargs
    assert kw["photo"] == b"\xff\xd8jpeg" and kw["parse_mode"] == "HTML"
    cap = kw["caption"]
    assert "Muse &lt;live&gt; — Uprising &amp; Co" in cap and "💿 The Resistance" in cap
    assert "Soulseek (peer, 320)" in cap and "🎤 синхр. текст" in cap
    kb: InlineKeyboardMarkup = kw["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert btns[0].text == "❤️ В избранное" and btns[0].callback_data == "fav:42"
    assert btns[1].callback_data == "del:42"
    assert btns[2].url == "https://music.example.com/web/#/search.html?query=Uprising%20%26%20Co"
    bot.delete_message.assert_awaited_once_with(5, 7)   # «⏳ …» заменён карточкой


def test_card_markup_skips_non_https_url(monkeypatch):
    """http/IP-ссылку Telegram может отвергнуть — кнопку «Открыть в Jellyfin» тогда не добавляем."""
    monkeypatch.delenv("STREAM_PUBLIC_URL", raising=False)
    kb = views.card_markup(42, "Uprising")
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["fav:42", "del:42"]


async def test_single_view_photo_bad_request_retries_without_buttons(tmp_path):
    bot = fake_bot()
    bot.send_photo.side_effect = [BadRequest("Wrong http url"), SimpleNamespace(message_id=101)]
    view, _ = make_single(bot)
    req = TrackRequest(artist="A", title="B")
    await view.finished(req, outcome(req, track=make_track(tmp_path)))
    assert bot.send_photo.await_count == 2
    assert "reply_markup" in bot.send_photo.await_args_list[0].kwargs
    assert "reply_markup" not in bot.send_photo.await_args_list[1].kwargs
    assert view.card_message_id == 101
    bot.delete_message.assert_awaited_once_with(5, 7)


@pytest.mark.parametrize("field", ["artist", "title", "album"])
def test_card_caption_long_escaped_fields_fit_without_broken_html(tmp_path, field):
    import re
    long = "&<>" * 400  # каждое «&» превращается в «&amp;» — экранированный текст в разы длиннее сырого
    track = SimpleNamespace(id=1, artist="A", title="B", album="C", path="")
    setattr(track, field, long)
    cap = views.card_caption(outcome(TrackRequest(artist="A", title="B"), track=track,
                                     source="&" * 500, flags=["<x>" * 100]))
    assert len(cap) <= views.CAPTION_LIMIT
    assert cap.count("<b>") == cap.count("</b>") == 1
    # ни одной разорванной сущности: каждое «&» начинает &amp;/&lt;/&gt;
    assert not re.search(r"&(?!amp;|lt;|gt;)", cap)
    assert "<" not in re.sub(r"</?b>", "", cap)


async def test_single_view_reused_track_shows_favorite_heart(tmp_path):
    bot = fake_bot()
    reactor = Reactor(bot, 5, 3)
    st = StatusMessage(bot, 5, message_id=7, reply_to=3)
    looked = []

    async def is_favorite(track):
        looked.append(track.id)
        return True
    view = SingleTrackView(st, reactor, is_favorite=is_favorite)
    req = TrackRequest(artist="A", title="B")
    await view.finished(req, outcome(req, reused=True, track=make_track(tmp_path), source="cache"))
    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].text == "💔 Убрать из избранного" and looked == [42]


async def test_single_view_favorite_lookup_failure_or_new_track_defaults_empty(tmp_path):
    bot = fake_bot()
    st = StatusMessage(bot, 5, message_id=7, reply_to=3)
    calls = []

    async def broken(track):
        calls.append(track.id)
        raise RuntimeError("jellyfin down")
    view = SingleTrackView(st, None, is_favorite=broken)
    req = TrackRequest(artist="A", title="B")
    await view.finished(req, outcome(req, reused=True, track=make_track(tmp_path / "a")))
    assert bot.send_photo.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "❤️ В избранное"
    await view.finished(req, outcome(req, reused=False, track=make_track(tmp_path / "b")))
    assert calls == [42], "для только что скачанного трека Jellyfin не спрашиваем"


async def test_single_view_card_text_fallback_without_cover(tmp_path):
    bot = fake_bot()
    view, _ = make_single(bot)
    req = TrackRequest(artist="A", title="B")
    await view.finished(req, outcome(req, reused=True, track=make_track(tmp_path, cover=False), source="cache"))
    bot.send_photo.assert_not_awaited()
    args = bot.edit_message_text.await_args
    assert args.kwargs["parse_mode"] == "HTML" and args.kwargs["message_id"] == 7
    assert "♻️" in args.args[0] and "общего кэша" in args.args[0]
    assert args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "fav:42"


async def test_single_view_photo_failure_falls_back_to_text(tmp_path):
    bot = fake_bot()
    bot.send_photo.side_effect = BadRequest("IMAGE_PROCESS_FAILED")
    view, _ = make_single(bot)
    req = TrackRequest(artist="A", title="B")
    await view.finished(req, outcome(req, track=make_track(tmp_path)))
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is not None
    bot.delete_message.assert_not_awaited()


async def test_single_view_failure(tmp_path):
    bot = fake_bot()
    view, reactor = make_single(bot)
    req = TrackRequest(artist="A", title="B")
    await reactor.set("👀")
    await view.started(req)
    await view.finished(req, outcome(req, ok=False, text="❌ Не нашёл ни в одном источнике: A — B"))
    assert bot.edit_message_text.await_args.args[0] == "❌ Не нашёл ни в одном источнике: A — B"
    assert "parse_mode" not in bot.edit_message_text.await_args.kwargs or \
        bot.edit_message_text.await_args.kwargs["parse_mode"] is None
    assert reactions(bot) == ["👀", "👎"]


async def test_single_view_progress_is_throttled():
    bot = fake_bot()
    clock = Clock()
    view, _ = make_single(bot, clock)
    req = TrackRequest(artist="A", title="B")
    await view.started(req)
    clock.t = 0.5
    await view.progress(req, "1%")
    clock.t = 2.5
    await view.progress(req, "50%")
    texts = [c.args[0] for c in bot.edit_message_text.await_args_list]
    assert texts == ["⏳ A — B: ищу в источниках…", "⏳ A — B: 50%"]


def test_jellyfin_base_prefers_https_public_url(monkeypatch):
    monkeypatch.setenv("STREAM_PUBLIC_URL", "https://music.example.com/")
    assert views.jellyfin_base() == "https://music.example.com"
    monkeypatch.setenv("STREAM_PUBLIC_URL", "http://music.example.com")
    assert views.jellyfin_base().endswith(":8096")


def test_fit_lines_and_blockquote():
    lines = [f"line {i}" for i in range(100)]
    shown, more = views.fit_lines(lines, range(100), 200)
    assert more > 0 and len(shown) + more == 100 and shown == lines[: len(shown)]
    assert views.blockquote(["a"], 3) == "<blockquote expandable>a\n…и ещё 3</blockquote>"
    assert views.fit_lines(["a"], [0], 100) == (["a"], 0)


# ---------------------------------------------------------------- Messenger
def make_messenger():
    owner = SimpleNamespace(app=SimpleNamespace(bot=fake_bot()))
    return Messenger(owner), owner.app.bot


async def test_messenger_send_converts_buttons():
    m, bot = make_messenger()
    mid = await m.send(1, "text", [[("⏸ Пауза", "impp:5"), ("⏹ Стоп", "impx:5")], [("Site", "https://x.org")]])
    assert mid == 100
    kw = bot.send_message.await_args.kwargs
    kb = kw["reply_markup"]
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [["impp:5", "impx:5"], [None]]
    assert kb.inline_keyboard[1][0].url == "https://x.org"
    assert kw["parse_mode"] is None


async def test_messenger_send_plain_and_html():
    m, bot = make_messenger()
    await m.send(1, "x")
    assert bot.send_message.await_args.kwargs["reply_markup"] is None
    await m.send(1, "<b>x</b>", html=True)
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


async def test_messenger_edit_ignores_not_modified_only():
    m, bot = make_messenger()
    await m.edit(1, 2, "t", [[("▶️", "impr:1")]], html=True)
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "impr:1" and kw["parse_mode"] == "HTML"
    bot.edit_message_text.side_effect = BadRequest("Message is not modified")
    await m.edit(1, 2, "t")
    bot.edit_message_text.side_effect = BadRequest("Chat not found")
    with pytest.raises(BadRequest):
        await m.edit(1, 2, "t")


async def test_messenger_send_document():
    m, bot = make_messenger()
    await m.send_document(1, b"a,b", "not_found.csv", "cap")
    kw = bot.send_document.await_args.kwargs
    assert kw["document"] == b"a,b" and kw["filename"] == "not_found.csv"


def test_keyboard_empty():
    assert views.keyboard(None) is None and views.keyboard([]) is None
    assert isinstance(views.keyboard([[("a", "b")]]), InlineKeyboardMarkup)

