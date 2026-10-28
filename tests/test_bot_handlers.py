"""Telegram command handlers of PiMusicBot, without any network."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, NetworkError

import app.bot as botmod
from app.bot import Notifier, PiMusicBot
from app.db import User

ADMIN_TG = 999


def make_msg(text=""):
    msg = MagicMock()
    msg.text = text
    for n in ("reply_text", "reply_html", "delete"):
        setattr(msg, n, AsyncMock())
    status = MagicMock()
    status.edit_text = AsyncMock()
    msg.reply_text.return_value = status
    msg.status = status
    return msg


def make_update(tg_id=1, text=""):
    msg = make_msg(text)
    chat = MagicMock()
    wait = MagicMock()
    wait.edit_text = AsyncMock()
    chat.send_message = AsyncMock(return_value=wait)
    chat.wait = wait
    return SimpleNamespace(effective_user=SimpleNamespace(id=tg_id), effective_message=msg, effective_chat=chat)


def ctx(*args):
    return SimpleNamespace(args=list(args))


def html_texts(msg):
    return [c.args[0] for c in msg.reply_html.call_args_list]


@pytest.fixture
def bot(db, cfg, monkeypatch):
    b = PiMusicBot.__new__(PiMusicBot)
    b.db = db
    b.jf = AsyncMock()
    b.svc = MagicMock()
    b.svc.register = AsyncMock()
    b.svc.submit = AsyncMock()
    b.svc.delete_track = AsyncMock(return_value="deleted")
    b.svc.active = {}
    b.svc.queue = SimpleNamespace(qsize=lambda: 0)
    b.radio = MagicMock()
    b.radio.now_playing.return_value = None
    b.app = MagicMock()
    b.app.create_task = MagicMock(side_effect=lambda coro: coro.close())
    b.app.bot = AsyncMock()  # Telegram Bot API: реакции, статусы, карточки — без сети
    b._bg = []
    monkeypatch.setattr(botmod.radio, "sync_tuner", AsyncMock())
    return b


def add_user(db, login="alice", tg_id=1, admin=False):
    u = User(login=login, tg_id=tg_id, jf_user_id="jf-" + login, library_id=None,
             stream_token="tok" + login, is_admin=admin)
    db.upsert_user(u)
    return u


# ---------------------------------------------------------------- /register
async def test_register_wrong_arg_count_deletes_message_and_shows_usage(bot):
    upd = make_update(text="/register a b")
    await bot.cmd_register(upd, ctx("a", "b"))
    upd.effective_message.delete.assert_awaited_once()
    upd.effective_chat.send_message.assert_awaited_once()
    assert "/register" in upd.effective_chat.send_message.call_args.args[0]
    bot.svc.register.assert_not_awaited()


async def test_register_no_args_deletes_message(bot):
    upd = make_update()
    await bot.cmd_register(upd, SimpleNamespace(args=None))
    upd.effective_message.delete.assert_awaited_once()
    bot.svc.register.assert_not_awaited()


async def test_register_survives_delete_failure(bot):
    upd = make_update()
    upd.effective_message.delete.side_effect = BadRequest("cannot delete")
    await bot.cmd_register(upd, ctx("a", "b"))
    upd.effective_chat.send_message.assert_awaited_once()


async def test_register_success(bot, db):
    add_user(db, "newbie", tg_id=5)
    bot.svc.register.return_value = (True, "newbie")
    upd = make_update(tg_id=5, text="/register inv newbie pw")
    await bot.cmd_register(upd, ctx("inv", "newbie", "pw"))
    upd.effective_message.delete.assert_awaited_once()
    bot.svc.register.assert_awaited_once_with(5, "inv", "newbie", "pw")
    wait = upd.effective_chat.wait
    wait.edit_text.assert_awaited_once()
    text = wait.edit_text.call_args.args[0]
    assert "newbie" in text
    assert f"http://{botmod.CFG.public_host}:8096" in text
    assert wait.edit_text.call_args.kwargs["parse_mode"] == "HTML"


async def test_register_failure(bot):
    bot.svc.register.return_value = (False, "Неверный инвайт")
    upd = make_update()
    await bot.cmd_register(upd, ctx("x", "y", "z"))
    upd.effective_chat.wait.edit_text.assert_awaited_once_with("❌ Неверный инвайт")


async def test_register_exception_reported_as_failure(bot):
    bot.svc.register.side_effect = RuntimeError("boom")
    upd = make_update()
    await bot.cmd_register(upd, ctx("x", "y", "z"))
    out = upd.effective_chat.wait.edit_text.call_args.args[0]
    assert out.startswith("❌") and "boom" in out


async def test_register_html_escapes_login(bot):
    bot.svc.register.return_value = (True, "<script>")
    upd = make_update()
    await bot.cmd_register(upd, ctx("x", "<script>", "z"))
    out = upd.effective_chat.wait.edit_text.call_args.args[0]
    assert "<script>" not in out and "&lt;script&gt;" in out


async def test_register_failure_message_with_html_chars(bot):
    # failure text goes out without parse_mode (defaults are not HTML), so no escaping is needed
    bot.svc.register.return_value = (False, "<bad>")
    upd = make_update()
    await bot.cmd_register(upd, ctx("x", "y", "z"))
    assert "parse_mode" not in upd.effective_chat.wait.edit_text.call_args.kwargs


# ---------------------------------------------------------------- unregistered
@pytest.mark.parametrize("handler,args", [("on_text", ()), ("cmd_me", ()), ("cmd_wave", ()),
                                          ("cmd_mywave", ()), ("cmd_find", ("x",)), ("cmd_delete", ("1",)),
                                          ("cmd_radio_add", ())])
async def test_unregistered_gets_register_hint(bot, handler, args):
    upd = make_update(tg_id=42, text="Radiohead - Creep")
    await getattr(bot, handler)(upd, ctx(*args))
    hints = html_texts(upd.effective_message)
    assert len(hints) == 1 and "/register" in hints[0]
    bot.app.create_task.assert_not_called()
    bot.svc.submit.assert_not_awaited()
    bot.svc.delete_track.assert_not_awaited()


# ---------------------------------------------------------------- on_text
async def test_on_text_schedules_submit_and_sends_status(bot, db):
    u = add_user(db)
    seen = {}

    async def fake_submit(user, text, notify):
        seen["args"] = (user, text, notify)
    bot._submit = fake_submit
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    # ссылки качаются сразу, без выбора
    upd = make_update(text="  https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC \n")
    await bot.on_text(upd, ctx())
    upd.effective_message.reply_text.assert_awaited_once()
    assert upd.effective_message.reply_text.call_args.args[0].startswith("⏳")
    bot.app.create_task.assert_called_once()
    await seen["coro"]
    user, text, notify = seen["args"]
    assert user == u and text.startswith("https://open.spotify.com/track/") and isinstance(notify, Notifier)


async def test_on_text_multiline_list_runs_job_with_live_report(bot, db):
    from app.views import LiveReport
    u = add_user(db)
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    bot.svc.run_job = AsyncMock()
    upd = make_update(text="Radiohead - Creep\nMuse - Uprising")
    await bot.on_text(upd, ctx())
    await seen["coro"]
    job = bot.svc.run_job.await_args.args[0]
    assert job.user == u and [t.title for t in job.tracks] == ["Creep", "Uprising"]
    assert isinstance(job.view, LiveReport)
    assert bot.svc.run_job.await_args.kwargs["announce"] is False
    # 👀 на запрос пользователя
    assert bot.app.bot.set_message_reaction.await_args_list[0].kwargs["reaction"] == "👀"
    # живой отчёт сразу показан в статусном сообщении (редактирование, HTML)
    edit = bot.app.bot.edit_message_text.await_args
    assert "<blockquote expandable>" in edit.args[0] and edit.kwargs["parse_mode"] == "HTML"


async def test_submit_single_track_gets_single_view(bot, db, monkeypatch):
    from app.sources import Resolved
    from app.views import SingleTrackView
    u = add_user(db)
    monkeypatch.setattr(botmod.sources, "resolve", AsyncMock(return_value=Resolved(
        "track", "A — B", [TrackRequest(artist="A", title="B")])))
    bot.svc.run_job = AsyncMock()
    await bot._submit(u, "https://youtu.be/x", Notifier(make_msg()))
    job = bot.svc.run_job.await_args.args[0]
    assert isinstance(job.view, SingleTrackView) and job.title == "A — B"


async def test_submit_resolve_error_reacts_thumbs_down(bot, db, monkeypatch):
    from app.sources import Resolved
    u = add_user(db)
    monkeypatch.setattr(botmod.sources, "resolve", AsyncMock(return_value=Resolved("track", "", error="нельзя")))
    bot.svc.run_job = AsyncMock()
    msg = make_msg()
    await bot._submit(u, "https://x.y/z", Notifier(msg))
    bot.svc.run_job.assert_not_awaited()
    assert msg.reply_text.call_args.args[0] == "⚠️ нельзя"
    assert bot.app.bot.set_message_reaction.await_args.kwargs["reaction"] == "👎"


# ---------------------------------------------------------------- выбор из найденного
from app import search as searchmod  # noqa: E402
from app.sources import TrackRequest  # noqa: E402


def fake_result(owner=1, artist="Panic! At the Disco"):
    res = searchmod.Result(query="panic at the disco", owner=owner, artist=artist)
    res.tracks = [TrackRequest(artist="Panic! At the Disco", title="High Hopes", duration=190),
                  TrackRequest(artist="Panic! At the Disco", title="Into the Unknown", duration=190)]
    res.albums = [searchmod.Album(id="42", title="Death of a Bachelor", artist="Panic! At the Disco", year="2016")]
    return res


async def test_single_line_text_offers_choices_instead_of_downloading(bot, db, monkeypatch):
    add_user(db)
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    monkeypatch.setattr(botmod.search, "search", AsyncMock(return_value=fake_result()))
    upd = make_update(text="Panic! At the disco")
    await bot.on_text(upd, ctx())
    await seen["coro"]
    bot.svc.submit.assert_not_awaited()
    status = upd.effective_message.status
    status.edit_text.assert_awaited_once()
    text = status.edit_text.call_args.args[0]
    kb = status.edit_text.call_args.kwargs["reply_markup"]
    assert "High Hopes" in text and "Into the Unknown" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(d.startswith("t:") and d.endswith(":0") for d in datas)
    assert any(d.startswith("T:") for d in datas) and any(d.startswith("a:") for d in datas)


async def test_no_catalog_results_falls_back_to_direct_search(bot, db, monkeypatch):
    add_user(db)
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    monkeypatch.setattr(botmod.search, "search", AsyncMock(return_value=searchmod.Result(query="zz", owner=1)))
    bot._submit = AsyncMock()
    upd = make_update(text="очень редкий бутлег 1997")
    await bot.on_text(upd, ctx())
    await seen["coro"]
    bot._submit.assert_awaited_once()


def make_query(data, tg_id=1):
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = make_msg()
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=tg_id),
                           effective_message=q.message, effective_chat=MagicMock())


async def test_choice_track_enqueues_exactly_that_track(bot, db):
    add_user(db)
    token = searchmod.store(fake_result())
    bot.svc.run_job = AsyncMock()
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    upd = make_query(f"t:{token}:1")
    await bot.on_choice(upd, ctx())
    await seen["coro"]
    job = bot.svc.run_job.await_args.args[0]
    assert [t.title for t in job.tracks] == ["Into the Unknown"]
    assert job.tracks[0].artist == "Panic! At the Disco"


async def test_choice_download_all(bot, db):
    add_user(db)
    token = searchmod.store(fake_result())
    bot.svc.run_job = AsyncMock()
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    await bot.on_choice(make_query(f"T:{token}"), ctx())
    await seen["coro"]
    assert len(bot.svc.run_job.await_args.args[0].tracks) == 2


async def test_choice_album_open_and_download(bot, db, monkeypatch):
    add_user(db)
    res = fake_result()
    token = searchmod.store(res)
    album = [TrackRequest(artist="Panic! At the Disco", title=f"Song {i}", album="Death of a Bachelor", track_no=i)
             for i in range(1, 4)]
    monkeypatch.setattr(botmod.search, "album_tracks", AsyncMock(return_value=album))
    upd = make_query(f"a:{token}:0")
    await bot.on_choice(upd, ctx())
    text = upd.callback_query.edit_message_text.call_args.args[0]
    assert "Death of a Bachelor" in text and "Song 3" in text
    bot.svc.run_job = AsyncMock()
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    await bot.on_choice(make_query(f"A:{token}:0"), ctx())
    await seen["coro"]
    assert [t.track_no for t in bot.svc.run_job.await_args.args[0].tracks] == [1, 2, 3]


async def test_choice_by_other_user_rejected(bot, db):
    add_user(db, "alice", tg_id=1)
    add_user(db, "bob", tg_id=2)
    token = searchmod.store(fake_result(owner=1))
    bot.svc.run_job = AsyncMock()
    upd = make_query(f"t:{token}:0", tg_id=2)
    await bot.on_choice(upd, ctx())
    bot.app.create_task.assert_not_called()
    assert upd.callback_query.answer.call_args.kwargs.get("show_alert") is True


async def test_choice_expired_token(bot, db):
    add_user(db)
    upd = make_query("t:nope:0")
    await bot.on_choice(upd, ctx())
    assert "устарели" in upd.callback_query.answer.call_args.args[0]
    bot.app.create_task.assert_not_called()


async def test_choice_bad_index(bot, db):
    add_user(db)
    token = searchmod.store(fake_result())
    upd = make_query(f"t:{token}:99")
    await bot.on_choice(upd, ctx())
    bot.app.create_task.assert_not_called()


def test_render_escapes_html(bot):
    res = fake_result(artist="<b>x</b>")
    res.tracks[0].title = "<script>"
    text, _ = bot._render_main(res, "tok")
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_callback_data_fits_telegram_limit(bot):
    res = fake_result()
    res.albums = [searchmod.Album(id=str(i), title="A" * 200, artist="x") for i in range(12)]
    token = searchmod.store(res)
    _, kb = bot._render_main(res, token)
    _, kb2 = bot._render_album(res, token, 11, [TrackRequest(title=f"t{i}") for i in range(40)])
    for markup in (kb, kb2):
        for row in markup.inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64 and len(b.text) <= 64


async def test_on_text_too_short_ignored(bot, db):
    add_user(db)
    upd = make_update(text=" a ")
    await bot.on_text(upd, ctx())
    upd.effective_message.reply_text.assert_not_awaited()
    bot.app.create_task.assert_not_called()


async def test_on_text_none_text_ignored(bot, db):
    add_user(db)
    upd = make_update(text=None)
    await bot.on_text(upd, ctx())
    bot.app.create_task.assert_not_called()


async def test_submit_error_is_reported(bot, db, monkeypatch):
    u = add_user(db)
    monkeypatch.setattr(botmod.sources, "resolve", AsyncMock(side_effect=RuntimeError("kaboom")))
    notify = AsyncMock()
    await bot._submit(u, "x y", notify)
    assert notify.call_args.args[0].startswith("❌") and "kaboom" in notify.call_args.args[0]


# ---------------------------------------------------------------- Notifier
async def test_notifier_progress_creates_once_then_edits():
    msg = make_msg()
    n = Notifier(msg)
    await n("⏳ one")
    await n("⏳ two")
    await n("⏳ three")
    assert msg.reply_text.await_count == 1
    assert msg.status.edit_text.await_count == 2
    msg.status.edit_text.assert_awaited_with("⏳ three")


async def test_notifier_final_edits_status_not_new_reply():
    msg = make_msg()
    n = Notifier(msg)
    await n("⏳ working")
    await n("✅ done")
    assert msg.reply_text.await_count == 1
    msg.status.edit_text.assert_awaited_once_with("✅ done")
    assert n.status is None


async def test_notifier_final_without_status_replies():
    msg = make_msg()
    await Notifier(msg)("✅ done")
    msg.reply_text.assert_awaited_once_with("✅ done")


async def test_notifier_final_badrequest_falls_back_to_reply():
    msg = make_msg()
    n = Notifier(msg)
    await n("⏳ working")
    msg.status.edit_text.side_effect = BadRequest("message is not modified")
    await n("✅ done")
    assert msg.reply_text.await_count == 2
    assert msg.reply_text.call_args.args[0] == "✅ done"


async def test_notifier_progress_badrequest_suppressed():
    msg = make_msg()
    n = Notifier(msg)
    await n("⏳ a")
    msg.status.edit_text.side_effect = BadRequest("same")
    await n("⏳ b")
    assert msg.reply_text.await_count == 1


async def test_notifier_truncates_to_4000():
    msg = make_msg()
    await Notifier(msg)("x" * 10000)
    assert len(msg.reply_text.call_args.args[0]) == 4000
    msg2 = make_msg()
    await Notifier(msg2)("⏳" + "y" * 10000)
    assert len(msg2.reply_text.call_args.args[0]) == 4000


async def test_notifier_swallows_telegram_errors():
    msg = make_msg()
    msg.reply_text.side_effect = NetworkError("down")
    await Notifier(msg)("hi")  # must not raise


# ---------------------------------------------------------------- /radio_add
async def test_radio_add_missing_pipe(bot, db):
    add_user(db)
    upd = make_update(text="/radio_add Name http://example.com/s")
    await bot.cmd_radio_add(upd, ctx())
    assert "Формат" in html_texts(upd.effective_message)[0]
    assert db.list_radio(None) == []


async def test_radio_add_no_args(bot, db):
    add_user(db)
    upd = make_update(text="/radio_add")
    await bot.cmd_radio_add(upd, ctx())
    assert "Формат" in html_texts(upd.effective_message)[0]


@pytest.mark.parametrize("text", [
    "/radio_add Evil\nName | http://example.com/s",
    "/radio_add Name | http://example.com/s\n#EXTINF:-1,Hack",
    "/radio_add Name | http://jellyfin:8096/x",
    "/radio_add Name | http://192.168.1.5/s",
    "/radio_add Name | http://localhost/s",
    "/radio_add Name | http://127.0.0.1/s",
    "/radio_add Name | ftp://example.com/s",
    "/radio_add  | http://example.com/s",
])
async def test_radio_add_rejects_bad_input(bot, db, text):
    add_user(db)
    upd = make_update(text=text)
    await bot.cmd_radio_add(upd, ctx())
    upd.effective_message.reply_text.assert_awaited_once()
    assert "Нужны название" in upd.effective_message.reply_text.call_args.args[0]
    assert db.list_radio(None) == []
    botmod.radio.sync_tuner.assert_not_awaited()


async def test_radio_add_accepts_public_url(bot, db, cfg):
    add_user(db)
    upd = make_update(text="/radio_add Cool FM | https://stream.example.com/cool.mp3")
    await bot.cmd_radio_add(upd, ctx())
    rows = db.list_radio(None)
    assert len(rows) == 1 and rows[0]["name"] == "Cool FM"
    assert rows[0]["url"] == "https://stream.example.com/cool.mp3"
    p = Path(rows[0]["path"])
    assert p.exists() and cfg.radio_dir in p.parents
    assert p.read_text(encoding="utf-8").strip() == "https://stream.example.com/cool.mp3"
    botmod.radio.sync_tuner.assert_awaited_once()
    assert "добавлена" in upd.effective_message.reply_text.call_args.args[0]


async def test_radio_add_sync_failure_still_added(bot, db):
    add_user(db)
    botmod.radio.sync_tuner.side_effect = RuntimeError("jf down")
    upd = make_update(text="/radio_add Cool FM | https://stream.example.com/cool.mp3")
    await bot.cmd_radio_add(upd, ctx())
    assert len(db.list_radio(None)) == 1
    assert "jf down" in upd.effective_message.reply_text.call_args.args[0]


# ---------------------------------------------------------------- admin gating
ADMIN_ONLY = [("cmd_radio_del", ("1",)), ("cmd_status", ()), ("cmd_users", ()), ("cmd_cookies", ())]


@pytest.mark.parametrize("handler,args", ADMIN_ONLY)
async def test_admin_only_rejects_regular_user(bot, db, handler, args):
    add_user(db, tg_id=1)
    upd = make_update(tg_id=1)
    await getattr(bot, handler)(upd, ctx(*args))
    upd.effective_message.reply_text.assert_awaited_once_with("Только для администратора.")


@pytest.mark.parametrize("handler,args", ADMIN_ONLY)
async def test_admin_only_rejects_unregistered(bot, handler, args):
    upd = make_update(tg_id=12345)
    await getattr(bot, handler)(upd, ctx(*args))
    upd.effective_message.reply_text.assert_awaited_once_with("Только для администратора.")


async def test_users_passes_for_cfg_admin_and_escapes(bot, db):
    add_user(db, "<script>", tg_id=7)
    upd = make_update(tg_id=ADMIN_TG)
    await bot.cmd_users(upd, ctx())
    out = html_texts(upd.effective_message)[0]
    assert "&lt;script&gt;" in out and "<script>" not in out


async def test_users_passes_for_db_admin(bot, db):
    add_user(db, "boss", tg_id=3, admin=True)
    upd = make_update(tg_id=3)
    await bot.cmd_users(upd, ctx())
    assert "boss" in html_texts(upd.effective_message)[0]


async def test_users_empty(bot):
    upd = make_update(tg_id=ADMIN_TG)
    await bot.cmd_users(upd, ctx())
    assert html_texts(upd.effective_message) == ["Пока никого."]


async def test_status_admin(bot, db, monkeypatch):
    monkeypatch.setattr(botmod.library, "disk_usage", lambda: (1000, 400, 600))
    monkeypatch.setattr(botmod.downloaders, "SLSKD", SimpleNamespace(online=AsyncMock(return_value=True)))
    upd = make_update(tg_id=ADMIN_TG)
    await bot.cmd_status(upd, ctx())
    out = html_texts(upd.effective_message)[0]
    assert "онлайн" in out and "Пользователей" in out


async def test_cookies_admin_downloads_to_state_dir(bot, cfg):
    upd = make_update(tg_id=ADMIN_TG)
    f = MagicMock()
    f.download_to_drive = AsyncMock()
    upd.effective_message.document = MagicMock()
    upd.effective_message.document.get_file = AsyncMock(return_value=f)
    await bot.cmd_cookies(upd, ctx())
    f.download_to_drive.assert_awaited_once_with(str(cfg.state_dir / "youtube_cookies.txt"))
    assert "cookies" in upd.effective_message.reply_text.call_args.args[0]


async def test_radio_del_admin_validation_and_delete(bot, db, tmp_path):
    upd = make_update(tg_id=ADMIN_TG)
    await bot.cmd_radio_del(upd, ctx())
    assert "Формат" in upd.effective_message.reply_text.call_args.args[0]
    await bot.cmd_radio_del(upd, ctx("abc"))
    assert "Формат" in upd.effective_message.reply_text.call_args.args[0]
    await bot.cmd_radio_del(upd, ctx("12345"))
    assert upd.effective_message.reply_text.call_args.args[0] == "Нет такой станции."
    strm = tmp_path / "s.strm"
    strm.write_text("http://x.com/\n")
    rid = db.add_radio(None, "Stn", "http://x.com/", str(strm))
    await bot.cmd_radio_del(upd, ctx(str(rid)))
    assert db.get_radio(rid) is None and not strm.exists()
    assert "Stn" in upd.effective_message.reply_text.call_args.args[0]
    botmod.radio.sync_tuner.assert_awaited()


# ---------------------------------------------------------------- /find /delete
def add_track_for(db, login, artist, title, album=None, n=1):
    tid = db.add_track(match_key=f"{artist}-{title}".lower(), mbid=None, isrc=None, artist=artist,
                       title=title, album=album, duration=100, path=f"/m/{n}.mp3", source="t")
    db.link_track(login, tid, f"/u/{login}/{n}.mp3")
    return tid


async def test_find_without_results(bot, db):
    add_user(db)
    upd = make_update()
    await bot.cmd_find(upd, ctx("nothing"))
    upd.effective_message.reply_text.assert_awaited_once_with("Ничего не найдено.")


async def test_find_escapes_html(bot, db):
    add_user(db)
    tid = add_track_for(db, "alice", "Art<b>", "<script>alert(1)</script>", album="<i>Alb</i>")
    upd = make_update()
    await bot.cmd_find(upd, ctx("script"))
    out = html_texts(upd.effective_message)[0]
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "Art&lt;b&gt;" in out and "&lt;i&gt;Alb" in out
    kb = upd.effective_message.reply_html.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [f"del:{tid}"]


def make_cb(data, tg_id=1):
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = make_msg()
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=tg_id),
                           effective_message=q.message, effective_chat=MagicMock())


async def test_delete_button_asks_confirmation_then_deletes(bot, db):
    add_user(db)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    upd = make_cb(f"del:{tid}")
    await bot.on_delete_choice(upd, ctx())
    confirm = upd.callback_query.message.reply_html.call_args
    assert "Uprising" in confirm.args[0]
    datas = [b.callback_data for row in confirm.kwargs["reply_markup"].inline_keyboard for b in row]
    assert f"delok:{tid}" in datas
    bot.svc.delete_track.assert_not_awaited()
    upd2 = make_cb(f"delok:{tid}")
    await bot.on_delete_choice(upd2, ctx())
    bot.svc.delete_track.assert_awaited_once()
    assert bot.svc.delete_track.await_args.args[1] == tid


async def test_delete_button_foreign_track_rejected(bot, db):
    add_user(db, "alice", tg_id=1)
    add_user(db, "bob", tg_id=2)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    upd = make_cb(f"delok:{tid}", tg_id=2)
    await bot.on_delete_choice(upd, ctx())
    bot.svc.delete_track.assert_not_awaited()
    assert upd.callback_query.answer.call_args.kwargs.get("show_alert") is True


async def test_find_no_args_lists_everything(bot, db):
    add_user(db)
    add_track_for(db, "alice", "A", "T")
    upd = make_update()
    await bot.cmd_find(upd, SimpleNamespace(args=None))
    assert "A — T" in html_texts(upd.effective_message)[0]


@pytest.mark.parametrize("args", [(), ("abc",), ("-1",), ("1.5",)])
async def test_delete_invalid_args(bot, db, args):
    add_user(db)
    upd = make_update()
    await bot.cmd_delete(upd, ctx(*args))
    assert "Формат" in upd.effective_message.reply_text.call_args.args[0]
    bot.svc.delete_track.assert_not_awaited()


async def test_delete_calls_service(bot, db):
    u = add_user(db)
    upd = make_update()
    await bot.cmd_delete(upd, ctx("42"))
    bot.svc.delete_track.assert_awaited_once_with(u, 42)
    upd.effective_message.reply_text.assert_awaited_once_with("deleted")


# ---------------------------------------------------------------- /queue /me /help
async def test_queue_escapes_active_labels(bot, db):
    add_user(db, "alice", tg_id=1)
    bot.svc.active = {1: "alice: <b>evil</b>", 2: "alice: A & B", 3: "bob: секрет Боба"}
    upd = make_update()
    await bot.cmd_queue(upd, ctx())
    out = html_texts(upd.effective_message)[0]
    assert "&lt;b&gt;evil&lt;/b&gt;" in out and "A &amp; B" in out
    assert "<b>evil</b>" not in out and "Боба" not in out  # чужие загрузки не видны
    upd = make_update(tg_id=ADMIN_TG)  # админ видит все
    await bot.cmd_queue(upd, ctx())
    assert "Боба" in html_texts(upd.effective_message)[0]


async def test_queue_empty(bot):
    upd = make_update()
    await bot.cmd_queue(upd, ctx())
    assert "—" in html_texts(upd.effective_message)[0]


async def test_me_escapes_login_and_now_playing(bot, db):
    add_user(db, "<script>x</script>", tg_id=1)
    bot.radio.now_playing.return_value = "<script>song</script>"
    upd = make_update()
    await bot.cmd_me(upd, ctx())
    out = html_texts(upd.effective_message)[0]
    assert "&lt;script&gt;x" in out and "&lt;script&gt;song" in out
    assert "<b>&lt;script&gt;x&lt;/script&gt;</b>" in out


async def test_me_wave_url_is_escaped(bot, db):
    add_user(db, "<script>x</script>", tg_id=1)
    upd = make_update()
    await bot.cmd_me(upd, ctx())
    assert "<script>" not in html_texts(upd.effective_message)[0]


async def test_me_shows_admin_flag_and_url(bot, db):
    add_user(db, "root", tg_id=1, admin=True)
    upd = make_update()
    await bot.cmd_me(upd, ctx())
    out = html_texts(upd.effective_message)[0]
    assert "(админ)" in out and f"http://{botmod.CFG.public_host}:8096" in out


async def test_help_admin_section_only_for_admin(bot, db):
    add_user(db, "bob", tg_id=1)
    u1 = make_update(tg_id=1)
    await bot.cmd_help(u1, ctx())
    assert "Администратору" not in html_texts(u1.effective_message)[0]
    u2 = make_update(tg_id=ADMIN_TG)
    await bot.cmd_help(u2, ctx())
    assert "Администратору" in html_texts(u2.effective_message)[0]


async def test_help_escapes_login(bot, db):
    add_user(db, "<script>", tg_id=1)
    upd = make_update(tg_id=1)
    await bot.cmd_help(upd, ctx())
    assert "<script>" not in html_texts(upd.effective_message)[0]


# ---------------------------------------------------------------- wave
async def test_wave_empty_library(bot, db, monkeypatch):
    add_user(db)
    monkeypatch.setattr(botmod.wave, "build", AsyncMock(return_value=([], [])))
    upd = make_update()
    await bot.cmd_wave(upd, ctx())
    upd.effective_message.status.edit_text.assert_awaited_once()
    assert "пока пуста" in upd.effective_message.status.edit_text.call_args.args[0]
    bot.app.create_task.assert_not_called()


async def test_wave_size_is_clamped(bot, db, monkeypatch):
    add_user(db)
    build = AsyncMock(return_value=([], []))
    monkeypatch.setattr(botmod.wave, "build", build)
    await bot.cmd_wave(make_update(), ctx("500"))
    assert build.call_args.args[3] == 50
    await bot.cmd_wave(make_update(), ctx("0"))
    assert build.call_args.args[3] == 1


# ---------------------------------------------------------------- реакции и view при выборе кнопкой
async def test_choice_single_track_reacts_on_original_query_and_uses_single_view(bot, db):
    from app.views import SingleTrackView
    add_user(db)
    token = searchmod.store(fake_result())
    bot.svc.run_job = AsyncMock()
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    upd = make_query(f"t:{token}:0")
    upd.callback_query.message.reply_to_message = SimpleNamespace(chat_id=10, message_id=77)
    await bot.on_choice(upd, ctx())
    await seen["coro"]
    job = bot.svc.run_job.await_args.args[0]
    assert isinstance(job.view, SingleTrackView)
    r = bot.app.bot.set_message_reaction.await_args
    assert r.args == (10, 77) and r.kwargs["reaction"] == "👀"
    assert "в очереди" in bot.app.bot.send_message.await_args.args[1]


async def test_choice_download_all_uses_live_report(bot, db):
    from app.views import LiveReport
    add_user(db)
    token = searchmod.store(fake_result())
    bot.svc.run_job = AsyncMock()
    seen = {}
    bot.app.create_task = MagicMock(side_effect=lambda c: seen.setdefault("coro", c))
    await bot.on_choice(make_query(f"T:{token}"), ctx())
    await seen["coro"]
    job = bot.svc.run_job.await_args.args[0]
    assert isinstance(job.view, LiveReport)
    sent = bot.app.bot.send_message.await_args
    assert "High Hopes" in sent.args[1] and sent.kwargs["parse_mode"] == "HTML"


async def test_wave_with_tracks_starts_live_report(bot, db, monkeypatch):
    add_user(db)
    reqs = [TrackRequest(artist="A", title=f"S{i}") for i in range(3)]
    monkeypatch.setattr(botmod.wave, "build", AsyncMock(return_value=(reqs, ["Seed <1>"])))
    bot.svc.run_job = AsyncMock()
    upd = make_update()
    await bot.cmd_wave(upd, ctx())
    text = bot.app.bot.edit_message_text.await_args.args[0]
    assert "Моя Волна" in text and "Seed &lt;1&gt;" in text and "S2" in text
    bot.app.create_task.assert_called_once()


# ---------------------------------------------------------------- ❤️ избранное из карточки
def jf_items(path, fav=False, item_id="it1"):
    return {"Items": [{"Id": "other", "Path": "/x/y.mp3", "UserData": {"IsFavorite": False}},
                      {"Id": item_id, "Path": path, "UserData": {"IsFavorite": fav}}]}


def jf_request(items):
    """jf.request: GET /Items отдаёт результаты поиска, остальное (POST/DELETE избранного) — None."""
    async def request(method, path, **kw):
        return items if (method, path) == ("GET", "/Items") else None
    return AsyncMock(side_effect=request)


@pytest.mark.parametrize("was_fav,method,label", [(False, "POST", "💔 Убрать из избранного"),
                                                  (True, "DELETE", "❤️ В избранное")])
async def test_fav_toggle_indexed(bot, db, was_fav, method, label):
    add_user(db)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    bot.jf.audio_items = AsyncMock()
    bot.jf.request = jf_request(jf_items("/u/alice/1.mp3", fav=was_fav))
    upd = make_cb(f"fav:{tid}")
    upd.callback_query.edit_message_reply_markup = AsyncMock()
    await bot.on_fav(upd, ctx())
    bot.jf.audio_items.assert_not_awaited()  # всю медиатеку на каждый клик не выгружаем
    calls = bot.jf.request.await_args_list
    assert calls[0].args == ("GET", "/Items")
    params = calls[0].kwargs["params"]
    assert params["userId"] == "jf-alice" and params["searchTerm"] == "Uprising" and params["Limit"] == 50
    assert params["IncludeItemTypes"] == "Audio" and params["Recursive"] is True and params["Fields"] == "Path"
    assert calls[1].args == (method, "/UserFavoriteItems/it1") and calls[1].kwargs == {"params": {"userId": "jf-alice"}}
    kb = upd.callback_query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].text == label and kb.inline_keyboard[0][0].callback_data == f"fav:{tid}"
    upd.callback_query.answer.assert_awaited_once()


async def test_fav_matches_path_relative_to_media(bot, db):
    """У бота и Jellyfin разные точки монтирования — сверяем часть пути после /media/."""
    add_user(db)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    db._x("UPDATE user_tracks SET link_path=? WHERE track_id=?", ("/srv/pimusic/media/users/alice/1.mp3", tid))
    bot.jf.request = jf_request(jf_items("/media/users/alice/1.mp3"))
    upd = make_cb(f"fav:{tid}")
    upd.callback_query.edit_message_reply_markup = AsyncMock()
    await bot.on_fav(upd, ctx())
    assert bot.jf.request.await_args_list[1].args == ("POST", "/UserFavoriteItems/it1")


async def test_fav_not_indexed_yet(bot, db):
    add_user(db)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    bot.jf.request = jf_request(jf_items("/somewhere/else.mp3"))
    upd = make_cb(f"fav:{tid}")
    await bot.on_fav(upd, ctx())
    assert [c.args[0] for c in bot.jf.request.await_args_list] == ["GET"]
    assert "индексирует" in upd.callback_query.answer.await_args.args[0]


async def test_single_view_gets_favorite_lookup(bot, db):
    """Карточка уже бывшего трека узнаёт ❤️ через тот же поиск в Jellyfin."""
    from app.service import Job
    u = add_user(db)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    bot.jf.request = jf_request(jf_items("/u/alice/1.mp3", fav=True))
    job = Job(user=u, text="x", notify=AsyncMock(), tracks=[TrackRequest(artist="Muse", title="Uprising")])
    view = bot._make_view(job, MagicMock(), MagicMock())
    assert await view.is_favorite(db.get_track(tid)) is True


async def test_fav_foreign_track_rejected(bot, db):
    add_user(db, "alice", tg_id=1)
    add_user(db, "bob", tg_id=2)
    tid = add_track_for(db, "alice", "Muse", "Uprising")
    bot.jf.audio_items = AsyncMock()
    upd = make_cb(f"fav:{tid}", tg_id=2)
    await bot.on_fav(upd, ctx())
    bot.jf.audio_items.assert_not_awaited()
    assert upd.callback_query.answer.await_args.kwargs.get("show_alert") is True


# ---------------------------------------------------------------- кнопки прогресса импорта
@pytest.fixture
def imp(bot, db):
    add_user(db, "alice", tg_id=1)
    add_user(db, "bob", tg_id=2)
    iid = db.create_import("alice", 10, "likes.csv", [{"artist": "A", "title": "B"}])
    bot.svc.pause_import = MagicMock(return_value=True)
    bot.svc.resume_import = MagicMock(return_value=True)
    bot.svc.stop_import = MagicMock(return_value=True)
    bot.svc.failed_csv = MagicMock(return_value=b"Track name,Artist name\r\nB,A\r\n")
    bot.svc.refresh_import_message = AsyncMock()
    return iid


async def press(bot, data, tg_id=1):
    upd = make_cb(data, tg_id=tg_id)
    upd.callback_query.message.reply_document = AsyncMock()
    await bot.on_import_control(upd, ctx())
    return upd.callback_query


@pytest.mark.parametrize("data", ["impp", "impr", "impx", "impx!", "impf"])
async def test_import_buttons_owner_only(bot, imp, data):
    q = await press(bot, f"{data}:{imp}", tg_id=2)
    assert q.answer.await_args.kwargs.get("show_alert") is True
    for m in ("pause_import", "resume_import", "stop_import", "failed_csv"):
        getattr(bot.svc, m).assert_not_called()
    bot.svc.refresh_import_message.assert_not_awaited()


async def test_import_unknown_id_rejected(bot, imp):
    q = await press(bot, "impp:99999")
    assert q.answer.await_args.kwargs.get("show_alert") is True
    bot.svc.pause_import.assert_not_called()


async def test_import_pause_and_resume(bot, imp):
    q = await press(bot, f"impp:{imp}")
    bot.svc.pause_import.assert_called_once_with(imp, "alice")
    assert "пауз" in q.answer.await_args.args[0]
    q = await press(bot, f"impr:{imp}")
    bot.svc.resume_import.assert_called_once_with(imp, "alice")
    assert "Продолжаю" in q.answer.await_args.args[0]
    assert bot.svc.refresh_import_message.await_count == 2
    bot.svc.refresh_import_message.assert_awaited_with(imp)


async def test_import_pause_accepts_async_service(bot, imp):
    bot.svc.pause_import = AsyncMock(return_value=False)
    q = await press(bot, f"impp:{imp}")
    assert "не идёт" in q.answer.await_args.args[0]


async def test_import_stop_needs_confirmation(bot, imp):
    q = await press(bot, f"impx:{imp}")
    bot.svc.stop_import.assert_not_called()
    kb = q.message.reply_text.await_args.kwargs["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == [f"impx!:{imp}", f"impx-:{imp}"]
    assert "likes.csv" in q.message.reply_text.await_args.args[0]
    q = await press(bot, f"impx-:{imp}")
    bot.svc.stop_import.assert_not_called()
    q.message.delete.assert_awaited_once()
    q = await press(bot, f"impx!:{imp}")
    bot.svc.stop_import.assert_called_once_with(imp, "alice")
    assert "остановлен" in q.edit_message_text.await_args.args[0]
    bot.svc.refresh_import_message.assert_awaited_once_with(imp)


async def test_import_failed_list_sends_csv(bot, imp):
    q = await press(bot, f"impf:{imp}")
    bot.svc.failed_csv.assert_called_once_with(imp, "alice")
    kw = q.message.reply_document.await_args.kwargs
    assert kw["filename"] == "not_found.csv" and kw["document"].startswith(b"Track name")


async def test_import_failed_list_empty(bot, imp):
    bot.svc.failed_csv = MagicMock(return_value=None)
    q = await press(bot, f"impf:{imp}")
    q.message.reply_document.assert_not_awaited()
    assert "всё нашлось" in q.answer.await_args.args[0]


# ---------------------------------------------------------------- списки в раскрывающихся цитатах
async def test_find_uses_expandable_blockquote(bot, db):
    add_user(db)
    add_track_for(db, "alice", "A", "T")
    upd = make_update()
    await bot.cmd_find(upd, ctx("T"))
    assert "<blockquote expandable>" in html_texts(upd.effective_message)[0]


async def test_users_and_queue_use_expandable_blockquote(bot, db):
    add_user(db, "bob", tg_id=7)
    upd = make_update(tg_id=ADMIN_TG)
    await bot.cmd_users(upd, ctx())
    assert "<blockquote expandable>• bob" in html_texts(upd.effective_message)[0]
    bot.svc.active = {1: "alice: A — B"}
    upd = make_update(tg_id=ADMIN_TG)
    await bot.cmd_queue(upd, ctx())
    assert "<blockquote expandable>• alice: A — B</blockquote>" in html_texts(upd.effective_message)[0]


async def test_on_text_reacts_eyes(bot, db):
    add_user(db)
    upd = make_update(text="https://youtu.be/abc")
    upd.effective_message.chat_id, upd.effective_message.message_id = 10, 55
    await bot.on_text(upd, ctx())
    r = bot.app.bot.set_message_reaction.await_args
    assert r.args == (10, 55) and r.kwargs["reaction"] == "👀"
