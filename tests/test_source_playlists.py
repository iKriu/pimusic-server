"""Плейлисты из импортов и ссылок, предупреждение Spotify «только 100», фильтр ускоренных/замедленных версий."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import downloaders, metadata, service, sources, webapp
from app.db import DB, User
from app.metadata import Hint
from app.service import Service, TrackOutcome
from app.sources import Resolved, TrackRequest
from app.util import unwanted_variant

from test_webapp import TOKEN, FakeSvc, hdr
from test_bot_handlers import bot, make_update  # noqa: F401 — фикстура bot


# ------------------------------------------------------------------ общие помощники
def add_user(db, login, tg_id):
    u = User(login, tg_id, "jf-" + login, "lib-" + login, "st" + login, False)
    db.upsert_user(u)
    return u


def add_track(db, cfg, login, artist, title, *, isrc=None):
    d = cfg.library_dir / artist / "Album"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{title}.mp3"
    p.write_bytes(b"ID3")
    from app.util import match_key
    tid = db.add_track(match_key=match_key(artist, title), mbid=None, isrc=isrc, artist=artist, title=title,
                       album="Album", duration=100, path=str(p), source="test")
    if login:
        db.link_track(login, tid, str(cfg.users_dir / login / artist / "Album" / p.name))
    return tid


@pytest.fixture
def svc(db, cfg):
    s = Service(db, None)
    s._schedule_refresh = lambda *a, **k: None
    return s


def order(db, pid):
    return db.playlist_track_ids(pid)


# ------------------------------------------------------------------ фильтр вариантов
@pytest.mark.parametrize("cand,want,bad", [
    ("Song (Speed Up)", "Song", "speed up"),
    ("Song - Sped Up", "Song", "sped up"),
    ("Song (speedup version)", "Song", "speedup"),
    ("Song spedup", "Song", "spedup"),
    ("Песня (ускоренная версия)", "Песня", "ускоренная"),
    ("Song (Slowed + Reverb)", "Song", "slowed"),
    ("Song (Super Slowed)", "Song", "slowed"),
    ("Song (TikTok Version)", "Song", "tiktok version"),
    ("Song (Speed Up)", "Song (Speed Up)", None),   # просили именно её
    ("Reverberation", "Song", None),                # по целым словам
    ("Speedway", "Song", None),
])
def test_unwanted_variant(cand, want, bad):
    assert unwanted_variant(cand, want) == bad


def _f(name, length=200):
    return {"filename": f"@@u\\Music\\Artist\\{name}.mp3", "extension": "mp3", "bitRate": 320, "length": length,
            "size": 8_000_000}


def test_score_file_rejects_speed_variants():
    req = TrackRequest(artist="Artist", title="Song", duration=200)
    assert downloaders._score_file(_f("Artist - Song"), {}, req) is not None
    for v in ("Song (Speed Up)", "Song speedup", "Song (Slowed + Reverb)", "Song (TikTok Version)",
              "Song reverb"):
        assert downloaders._score_file(_f(f"Artist - {v}"), {}, req) is None, v
    asked = TrackRequest(artist="Artist", title="Song (Speed Up)", duration=200)
    assert downloaders._score_file(_f("Artist - Song (Speed Up)"), {}, asked) is not None


async def test_ytdlp_skips_sped_up_search_result(cfg, monkeypatch):
    calls = []

    async def fake_run(cmd, timeout=None, cwd=None):
        out = Path(cmd[cmd.index("-o") + 1]).parent
        target = cmd[-1]
        calls.append(target)
        title = "Song (Sped Up)" if target.startswith("ytsearch") else "Song"
        (out / "vid.mp3").write_bytes(b"ID3")
        (out / "vid.info.json").write_text(json.dumps({"title": title}), encoding="utf-8")
        return SimpleNamespace(rc=0, out="", err="")
    monkeypatch.setattr(downloaders.shutil, "which", lambda _x: "/usr/bin/yt-dlp")
    monkeypatch.setattr(downloaders, "run", fake_run)
    res = await downloaders.ytdlp(TrackRequest(artist="Artist", title="Song"), downloaders._noop)
    assert [c.split(":")[0] for c in calls] == ["ytsearch1", "scsearch1"]
    assert res is not None and res.detail == "SoundCloud"
    # по прямой ссылке пользователя не фильтруем
    calls.clear()
    res = await downloaders.ytdlp(TrackRequest(direct_url="https://youtu.be/x"), downloaders._noop)
    assert res is not None and len(calls) == 1


async def test_spotdl_rejects_variant(cfg, monkeypatch):
    async def fake_run(cmd, timeout=None, cwd=None):
        (Path(cwd) / "Artist - Song (Speed Up).mp3").write_bytes(b"ID3")
        return SimpleNamespace(rc=0, out="", err="")
    monkeypatch.setattr(downloaders.shutil, "which", lambda _x: "/usr/bin/spotdl")
    monkeypatch.setattr(downloaders, "run", fake_run)
    assert await downloaders.spotdl(TrackRequest(artist="Artist", title="Song"), downloaders._noop) is None


def test_metadata_prefers_non_variant_recording():
    hint = Hint(artist="Artist", title="Song", duration=200)
    rec = {"title": "Song", "artist-credit": [{"name": "Artist", "artist": {"id": "a", "name": "Artist"}}],
           "length": 200000, "score": 100, "releases": [{"status": "Official"}]}
    fast = dict(rec, title="Song (Sped Up)")
    assert metadata._score_recording(rec, hint) - metadata._score_recording(fast, hint) >= 40
    assert metadata._plausible(rec, hint) and not metadata._plausible(fast, hint)
    asked = Hint(artist="Artist", title="Song (Sped Up)")
    assert metadata._plausible(fast, asked)


async def test_itunes_skips_variant_releases(monkeypatch):
    async def fake_get_json(url, params=None, limiter=None, **kw):
        return {"results": [
            {"trackName": "Song", "collectionName": "Song (Sped Up) - Single", "artistName": "Artist",
             "trackTimeMillis": 150000},
            {"trackName": "Song", "collectionName": "Album", "artistName": "Artist", "trackTimeMillis": 200000},
        ]}
    monkeypatch.setattr(metadata.http, "get_json", fake_get_json)
    it = await metadata.lookup_itunes(Hint(artist="Artist", title="Song", duration=200))
    assert it["collectionName"] == "Album"


# ------------------------------------------------------------------ Spotify: только 100 треков
async def test_spotify_playlist_cap_note_and_spotdl_backoff(cfg, monkeypatch):
    runs = []

    async def fake_run(cmd, timeout=None, cwd=None):
        runs.append(timeout)
        return SimpleNamespace(rc=1, out="", err="HTTP Error 401")

    async def fake_embed(kind, sid):
        return "Мой плейлист", [TrackRequest(artist="A", title=f"T{i}", origin="spotify") for i in range(100)]
    monkeypatch.setattr(sources, "run", fake_run)
    monkeypatch.setattr(sources, "_spotify_embed", fake_embed)
    monkeypatch.setattr(sources, "_spotdl_playlists_blocked_until", 0.0)
    url = "https://open.spotify.com/playlist/" + "a" * 22
    r = await sources.resolve(url)
    assert r.kind == "playlist" and r.from_link and len(r.tracks) == 100 and "только первые 100" in r.note
    assert runs == [sources.SPOTDL_PLAYLIST_TIMEOUT]  # не 5 минут
    r2 = await sources.resolve(url)
    assert runs == [sources.SPOTDL_PLAYLIST_TIMEOUT] and r2.note  # spotdl для плейлистов пропущен

    async def embed99(kind, sid):
        return "Короткий", [TrackRequest(artist="A", title="T")] * 99
    monkeypatch.setattr(sources, "_spotify_embed", embed99)
    assert (await sources.resolve(url)).note == ""


async def test_text_list_is_not_a_link(cfg):
    r = await sources.resolve("A - B\nC - D")
    assert r.kind == "playlist" and r.from_link is False


# ------------------------------------------------------------------ БД: миграция и позиции
def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE imports (id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT NOT NULL,
        chat_id INTEGER NOT NULL, filename TEXT NOT NULL, total INTEGER NOT NULL, status_msg INTEGER,
        state TEXT NOT NULL DEFAULT 'running', created_at INTEGER NOT NULL);
        CREATE TABLE import_items (id INTEGER PRIMARY KEY AUTOINCREMENT, import_id INTEGER NOT NULL,
        idx INTEGER NOT NULL, artist TEXT NOT NULL, title TEXT NOT NULL, album TEXT, isrc TEXT, duration REAL,
        spotify_url TEXT, state TEXT NOT NULL DEFAULT 'pending', note TEXT);
        INSERT INTO imports(login,chat_id,filename,total,created_at) VALUES('x',1,'a.csv',1,0);""")
    c.commit()
    c.close()
    d = DB(path)
    assert "track_id" in {r[1] for r in d._conn.execute("PRAGMA table_info(import_items)")}
    assert d.get_import(1)["playlist_id"] is None
    DB(path)._conn.close()  # повторно — без ошибок
    d._conn.close()


def test_free_name_and_positioned_insert(db, cfg):
    add_user(db, "alice", 1)
    assert db.free_playlist_name("alice", "Лето") == "Лето"
    db.create_playlist("alice", "Лето")
    db.create_playlist("alice", "Лето (2)")
    assert db.free_playlist_name("alice", "Лето") == "Лето (3)"
    assert len(db.free_playlist_name("alice", "x" * 300)) == 100
    pid = db.create_playlist("alice", "p")
    t = [add_track(db, cfg, "alice", "A", f"T{i}") for i in range(4)]
    for i in (3, 0, 2, 1):  # загрузки завершаются вразнобой
        assert db.put_playlist_track(pid, t[i], i, "alice")
    assert order(db, pid) == t
    assert not db.put_playlist_track(pid, t[0], 9, "alice")       # дубль
    assert not db.put_playlist_track(pid, 999999, 9, "alice")     # трека нет
    assert not db.put_playlist_track(424242, t[0], 0, "alice")    # плейлиста нет
    assert not db.put_playlist_track(db.create_playlist("alice", "q"), t[0], 0, "alice", limit=0)


# ------------------------------------------------------------------ плейлист импорта
def _items(n):
    return [{"artist": "A", "title": f"T{i}", "album": "", "isrc": "", "duration": 0, "spotify_url": ""}
            for i in range(n)]


def _done(svc, db, item, track_id, iid):
    async def go():
        fut = asyncio.get_running_loop().create_future()
        track = db.get_track(track_id) if track_id else None
        fut.set_result(TrackOutcome(TrackRequest(), track_id is not None, "✅", track=track))
        svc._import_item_done(item["id"], fut, {item["id"]: iid})
    asyncio.run(go())


def test_import_playlist_follows_file_order(db, cfg, svc):
    u = add_user(db, "alice", 1)
    db.create_playlist("alice", "My Spotify Library")
    iid = svc.draft_import(u, 1, "My Spotify Library.csv", _items(4))
    assert db.get_import(iid)["playlist_id"] is None  # черновик — ещё без плейлиста
    svc._ensure_import_feeder = lambda: None
    assert svc.confirm_import(iid)
    imp = db.get_import(iid)
    pl = db.get_playlist(imp["playlist_id"])
    assert pl["name"] == "My Spotify Library (2)" and pl["owner"] == "alice"
    items = db._q("SELECT * FROM import_items WHERE import_id=? ORDER BY idx", (iid,))
    t = [add_track(db, cfg, "alice", "A", f"T{i}") for i in range(4)]
    for i in (2, 0, 3):
        _done(svc, db, items[i], t[i], iid)
    _done(svc, db, items[1], None, iid)  # не нашлось
    assert order(db, pl["id"]) == [t[0], t[2], t[3]]
    assert db.get_import_item(items[2]["id"])["track_id"] == t[2]
    assert db.kv_get(f"pl_dirty:{pl['id']}") == "1"  # зеркало в Jellyfin запланировано
    text = svc.import_status_text(iid)
    assert "📃 Плейлист «My Spotify Library (2)» — в Mini App и Jellyfin" in text
    # пользователь удалил плейлист — дальше не добавляем и не пересоздаём
    db.delete_playlist(pl["id"])
    t4 = add_track(db, cfg, "alice", "A", "T9")
    svc.import_playlist(db.get_import(iid))
    _done(svc, db, items[1], t4, iid)
    assert db.user_playlists("alice")[0]["name"] == "My Spotify Library"
    assert "📃" not in svc.import_status_text(iid)


def test_import_playlist_limit_note_and_finish_message(db, cfg, svc, monkeypatch):
    u = add_user(db, "alice", 1)
    monkeypatch.setattr(service, "PL_MAX_TRACKS", 2)
    svc._ensure_import_feeder = lambda: None
    iid = svc.start_import(u, 1, "big.txt", _items(3))
    pid = db.get_import(iid)["playlist_id"]
    items = db._q("SELECT * FROM import_items WHERE import_id=? ORDER BY idx", (iid,))
    t = [add_track(db, cfg, "alice", "A", f"T{i}") for i in range(3)]
    for i in range(3):
        _done(svc, db, items[i], t[i], iid)
    assert order(db, pid) == t[:2]  # первые N треков файла
    assert "(первые 2 треков файла)" in svc.import_status_text(iid)

    class M:
        sent, edits = [], []

        async def edit(self, *a, **k):
            self.edits.append(a[2])

        async def send(self, chat, text, **k):
            self.sent.append(text)

        async def send_document(self, *a, **k):
            pass
    svc.messenger = M()
    db.set_import_status_msg(iid, 5)
    asyncio.run(svc._finish_import(iid))
    assert "📃 Плейлист «big»" in svc.messenger.sent[0]


def test_import_without_user_row_does_not_break(db, svc):
    svc._ensure_import_feeder = lambda: None
    ghost = User("ghost", 7, "jf", None, "t", False)  # нет в таблице users — плейлист не создать (FK)
    iid = svc.start_import(ghost, 1, "x.csv", _items(1))
    assert db.get_import(iid)["playlist_id"] is None


def test_backfill_existing_imports(db, cfg, svc):
    add_user(db, "iKriu", 1)
    add_user(db, "bob", 2)
    iid = db.create_import("iKriu", 1, "Liked Songs.csv", [
        {"artist": "Muse", "title": "Uprising", "isrc": "GBAHT0900320"},
        {"artist": "Adele", "title": "Hello", "isrc": ""},
        {"artist": "Nobody", "title": "Missing", "isrc": ""},
        {"artist": "Queen", "title": "Bohemian Rhapsody", "isrc": ""},
        {"artist": "Bob Only", "title": "Song", "isrc": ""},
        {"artist": "Pending", "title": "Later", "isrc": ""},
    ])
    rows = db._q("SELECT * FROM import_items WHERE import_id=? ORDER BY idx", (iid,))
    muse = add_track(db, cfg, "iKriu", "Muse", "Uprising (Album Version)", isrc="GBAHT0900320")  # по ISRC
    adele = add_track(db, cfg, "iKriu", "Adele", "Hello")                                    # по ключу
    queen = add_track(db, cfg, "iKriu", "Queen", "Bohemian Rhapsody")
    add_track(db, cfg, "bob", "Bob Only", "Song")                                             # чужой — нет
    for r, st in zip(rows, ("cached", "done", "failed", "done", "done", "pending")):
        db.set_import_item(r["id"], st)
    db.set_import_item(rows[3]["id"], "done", track_id=queen)
    draft = db.create_import("iKriu", 1, "draft.csv", _items(1), state="draft")
    assert svc.backfill_import_playlists() == 1
    imp = db.get_import(iid)
    pl = db.get_playlist(imp["playlist_id"])
    assert pl["name"] == "Liked Songs" and order(db, pl["id"]) == [muse, adele, queen]
    assert db.get_import_item(rows[0]["id"])["track_id"] == muse
    assert db.get_import(draft)["playlist_id"] is None
    assert svc.backfill_import_playlists() == 0  # однократно (флаг в kv)
    # импорт ещё идёт: следующие позиции попадают в тот же плейлист live
    later = add_track(db, cfg, "iKriu", "Pending", "Later")
    _done(svc, db, rows[5], later, iid)
    assert order(db, pl["id"]) == [muse, adele, queen, later]


# ------------------------------------------------------------------ плейлист из ссылки
def _resolved(kind="playlist", n=3, from_link=True, title="Road Trip"):
    return Resolved(kind, title, [TrackRequest(artist="A", title=f"T{i}") for i in range(n)], from_link=from_link)


def test_link_playlist_only_for_playlist_links(db, svc):
    u = add_user(db, "alice", 1)
    for res in (_resolved("album"), _resolved(from_link=False), _resolved(n=1)):
        job = service.Job(user=u, text="x", notify=AsyncMock(), tracks=res.tracks, title=res.title)
        svc.link_playlist(job, res)
        assert job.playlist_id is None
    res = _resolved()
    job = service.Job(user=u, text="x", notify=AsyncMock(), tracks=res.tracks, title=res.title)
    svc.link_playlist(job, res)
    assert db.get_playlist(job.playlist_id)["name"] == "Road Trip" and job.playlist_name == "Road Trip"


async def test_link_job_fills_playlist_in_source_order(db, cfg, svc, monkeypatch):
    u = add_user(db, "alice", 1)
    tracks = {f"T{i}": add_track(db, cfg, "alice", "A", f"T{i}") for i in range(3)}
    delays = {"T0": 0.06, "T1": 0.0, "T2": 0.03}

    async def fake_process(self, job, req):
        await asyncio.sleep(delays[req.title])
        if req.title == "T1":
            return TrackOutcome(req, False, "❌")
        return TrackOutcome(req, True, "✅", track=db.get_track(tracks[req.title]))
    monkeypatch.setattr(Service, "_process", fake_process)
    notes = []

    async def notify(text):
        notes.append(text)
    workers = [asyncio.create_task(svc._worker(i)) for i in range(3)]
    try:
        res = _resolved()
        res.tracks.append(TrackRequest(artist="A", title="T0"))  # дубль в источнике — один раз
        delays["T0"] = 0.06
        job = service.Job(user=u, text="url", notify=notify, tracks=res.tracks, title=res.title)
        svc.link_playlist(job, res)
        await svc.run_job(job, announce=False)
    finally:
        for w in workers:
            w.cancel()
    assert order(db, job.playlist_id) == [tracks["T0"], tracks["T2"]]
    assert "📃 Плейлист «Road Trip» — в Mini App и Jellyfin" in notes[-1]


async def test_service_submit_sends_note_and_links(db, svc, monkeypatch):
    u = add_user(db, "alice", 1)
    res = _resolved()
    res.note = sources.SPOTIFY_CAP_NOTE

    async def fake_resolve(text):
        return res
    monkeypatch.setattr(service.sources, "resolve", fake_resolve)
    svc.run_job = AsyncMock()
    notify = AsyncMock()
    job = await svc.submit(u, "https://open.spotify.com/playlist/x", notify)
    notify.assert_any_await(sources.SPOTIFY_CAP_NOTE)
    assert job.playlist_id and job.playlist_name == "Road Trip"


async def test_bot_submit_note_and_playlist_subtitle(bot, db, monkeypatch):  # noqa: F811
    u = add_user(db, "alice", 1)
    res = _resolved()
    res.note = sources.SPOTIFY_CAP_NOTE

    async def fake_resolve(text):
        return res
    monkeypatch.setattr(botmod_sources(), "resolve", fake_resolve)

    def link(job, resolved):
        job.playlist_id, job.playlist_name = 5, "Road Trip"
    bot.svc.link_playlist = link
    bot.svc.run_job = AsyncMock()
    bot._start_job = AsyncMock()
    notify = AsyncMock()
    notify.msg = make_update(tg_id=1).effective_message
    notify.msg.chat_id, notify.msg.message_id = 1, 2
    notify.status = None
    await bot._submit(u, "https://open.spotify.com/playlist/x", notify)
    notify.assert_any_await(sources.SPOTIFY_CAP_NOTE)
    assert bot._start_job.await_args.args[3] == "📃 Плейлист «Road Trip» — в Mini App и Jellyfin"
    job = bot.svc.run_job.await_args.args[0]
    assert job.playlist_id == 5


def botmod_sources():
    import app.bot as botmod
    return botmod.sources


# ------------------------------------------------------------------ Mini App: ссылка
class LinkSvc(FakeSvc):
    def __init__(self, db):
        super().__init__()
        self.real = Service(db, None)

    def link_playlist(self, job, resolved):
        self.real.link_playlist(job, resolved)


@pytest.fixture
async def client(db, cfg):
    add_user(db, "alice", 4242)
    svc = LinkSvc(db)
    app = web.Application()
    webapp.setup_webapp(app, db=db, svc=svc, jf=MagicMock(), bot_token=TOKEN)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.svc = svc
    yield c
    await c.close()


async def test_download_link_endpoint(client, db, monkeypatch):
    res = _resolved()
    res.note = sources.SPOTIFY_CAP_NOTE

    async def fake_resolve(text):
        return res
    monkeypatch.setattr(webapp.sources, "resolve", fake_resolve)
    r = await client.post("/api/download/link", json={"url": "https://open.spotify.com/playlist/x"}, headers=hdr())
    data = await r.json()
    assert r.status == 200 and data["queued"] == 3 and data["note"] == sources.SPOTIFY_CAP_NOTE
    assert data["playlist"]["name"] == "Road Trip" and db.get_playlist(data["playlist"]["id"])
    await asyncio.wait_for(client.svc.done.wait(), 2)
    job = client.svc.jobs[0][0]
    assert job.playlist_id == data["playlist"]["id"]
    r = await client.post("/api/download/link", json={"url": "просто текст"}, headers=hdr())
    assert r.status == 400
    res.error = "Не удалось прочитать ссылку Spotify (приватный плейлист?)."
    r = await client.post("/api/download/link", json={"url": "https://open.spotify.com/playlist/y"}, headers=hdr())
    assert r.status == 422 and (await r.json())["message"].startswith("Не удалось")
