"""«Перекачать трек», проверка длительности скачанного файла, сортировка плейлистов, источник действий (app/tg)."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import auth, downloaders, http, library, service, webapp
from app.db import User
from app.metadata import Hint, TrackMeta
from app.metadata import deezer_duration as real_deezer_duration  # до подмены автофикстурой conftest
from app.service import Job, Service
from app.sources import TrackRequest
from app.uploads import Uploads
from app.util import AudioInfo, match_key

from test_webapp import TG_ID, TOKEN, hdr


def add_track(db, cfg, login, artist, title, *, album="Album", data=b"ID3old", source="yt-dlp", duration=200):
    d = cfg.library_dir / artist / album
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{title}.mp3"
    p.write_bytes(data)
    tid = db.add_track(match_key=match_key(artist, title), mbid=None, isrc=None, artist=artist, title=title,
                       album=album, duration=duration, path=str(p), source=source)
    if login:
        library.attach_to_user(db, db.get_track(tid), login)
    return tid


def user(db, login="alice", tg_id=TG_ID):
    u = User(login, tg_id, "jf-" + login, "lib", "st" + login, False)
    db.upsert_user(u)
    return u


# ------------------------------------------------------------------ длительность
def test_duration_mismatch():
    assert library.duration_mismatch(238, 212.3)          # тот самый случай: −26 с
    assert not library.duration_mismatch(238, 230)        # в пределах 10 с
    assert not library.duration_mismatch(600, 640)        # 7 % от 10 минут — 42 с
    assert library.duration_mismatch(600, 650)
    assert not library.duration_mismatch(0, 100) and not library.duration_mismatch(200, None)


@pytest.fixture
def fake_pipeline(monkeypatch):
    """ingest без ffmpeg и сети: probe отдаёт state.duration, разметка — заглушки."""
    state = MagicMock(duration=212.0, meta_duration=238, matched="itunes")

    async def fake_probe(p):
        return AudioInfo("mp3", 320000, state.duration, 44100, {}, format_name="mp3") if Path(p).is_file() else None

    async def yes(p):
        return True

    async def fake_resolve(hint):
        return TrackMeta(title=hint.title, artist=hint.artist, album_artist=hint.artist, album="Album",
                         duration=state.meta_duration, matched=state.matched)

    async def fake_tag(path, meta, work, **_):
        return {}
    monkeypatch.setattr(library, "probe", fake_probe)
    monkeypatch.setattr(library, "mp3_is_cbr320", yes)
    monkeypatch.setattr(library.metadata, "resolve", fake_resolve)
    monkeypatch.setattr(library.tagger, "tag_file", fake_tag)
    return state


def src_file(tmp_path, name="in.mp3", data=b"ID3new"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


async def test_ingest_rejects_other_version_by_request_duration(db, cfg, fake_pipeline, tmp_path):
    user(db)
    with pytest.raises(library.BadAudio, match="другая версия"):
        await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T", duration=238), source="yt-dlp",
                             login="alice")


async def test_ingest_rejects_other_version_by_catalog_duration(db, cfg, fake_pipeline, tmp_path):
    """В запросе длительности нет (экспорт Spotify), но каталог нашёл трек на 238 с — файл на 212 с не берём."""
    user(db)
    with pytest.raises(library.BadAudio, match="в каталоге"):
        await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T"), source="yt-dlp", login="alice")
    fake_pipeline.matched = "none"  # каталог не опознал — сравнивать не с чем
    res = await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T"), source="yt-dlp", login="alice")
    assert res.track.duration == 212  # в базе — настоящая длина файла


async def test_ingest_upload_not_checked(db, cfg, fake_pipeline, tmp_path):
    user(db)
    res = await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T", duration=238), source="upload",
                               login="alice", keep_mp3=True)
    assert res.track.duration == 212


async def test_ingest_replaces_broken_file_for_all_holders(db, cfg, fake_pipeline, tmp_path):
    user(db)
    user(db, "bob", 2)
    tid = add_track(db, cfg, "alice", "A", "T", duration=238)
    library.attach_to_user(db, db.get_track(tid), "bob")
    db.set_loudness(tid, -5.0, 1.5)
    db.set_track_broken(tid)
    assert db.get_track(tid).broken
    fake_pipeline.duration = 237.5
    res = await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T", duration=238), source="soulseek",
                               login="bob")
    t = db.get_track(tid)
    assert res.replaced and res.track.id == tid and not t.broken and t.source == "soulseek"
    assert t.gain_db is None and t.duration == 237  # громкость — пересчитать, длительность — новая
    assert Path(t.path).read_bytes() == b"ID3new"
    for login in ("alice", "bob"):  # у всех держателей — новый файл (жёсткие ссылки пересозданы)
        assert Path(db.user_has_track(login, tid)).read_bytes() == b"ID3new"
    assert len(db._q("SELECT id FROM tracks")) == 1


async def test_ingest_replace_id_targets_track(db, cfg, fake_pipeline, tmp_path):
    user(db)
    tid = add_track(db, cfg, "alice", "Other", "Name", duration=238)  # ключ не совпадёт с метаданными
    fake_pipeline.duration = 238.0
    res = await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T", duration=238), source="spotdl",
                               login="alice", replace_id=tid)
    assert res.replaced and res.track.id == tid and Path(db.get_track(tid).path).read_bytes() == b"ID3new"


async def test_ingest_healthy_cached_not_replaced(db, cfg, fake_pipeline, tmp_path):
    user(db)
    tid = add_track(db, cfg, "alice", "A", "T", duration=212)
    fake_pipeline.meta_duration = 212
    res = await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T"), source="soulseek", login="alice")
    assert res.reused and not res.replaced and Path(db.get_track(tid).path).read_bytes() == b"ID3old"


async def test_ingest_upload_replaces_cached_file_for_all(db, cfg, fake_pipeline, tmp_path):
    """Ручная загрузка главнее: файл трека заменяется у всех держателей, и скачанный потом его не вытесняет."""
    user(db)
    user(db, "bob", 2)
    tid = add_track(db, cfg, "alice", "A", "T", duration=212)
    res = await library.ingest(db, src_file(tmp_path, data=b"ID3up"), Hint(artist="A", title="T"), source="upload",
                               login="bob", keep_mp3=True)
    t = db.get_track(tid)
    assert res.reused and res.replaced and res.track.id == tid and t.source == "upload"
    for login in ("alice", "bob"):
        assert Path(db.user_has_track(login, tid)).read_bytes() == b"ID3up"
    fake_pipeline.meta_duration = 212
    res = await library.ingest(db, src_file(tmp_path), Hint(artist="A", title="T"), source="soulseek", login="alice")
    assert res.reused and not res.replaced and Path(t.path).read_bytes() == b"ID3up"


# ------------------------------------------------------------------ _process: проблемный трек и Deezer
@pytest.fixture(autouse=True)
def _no_musicbrainz(monkeypatch):
    async def none(hint):
        return None
    monkeypatch.setattr(service.metadata, "lookup_musicbrainz", none)


def svc_for(db):
    svc = Service(db, None)
    svc._schedule_refresh = lambda *a, **k: None
    svc._schedule_artwork = lambda *a, **k: None
    return svc


async def _notify(text):
    pass


async def test_broken_cached_track_downloaded_again(db, cfg, monkeypatch):
    u = user(db, "bob", 2)
    user(db)
    tid = add_track(db, cfg, "alice", "A", "T")
    db.set_track_broken(tid)
    fetched = []

    async def fake_fetch(req, progress, exclude=frozenset()):
        fetched.append(req.label)
        return None  # скачать не удалось
    monkeypatch.setattr(service.downloaders, "fetch", fake_fetch)
    svc = svc_for(db)
    out = await svc._process(Job(user=u, text="x", notify=_notify), TrackRequest(artist="A", title="T", duration=200))
    assert fetched == ["A — T"]  # сначала пробуем скачать заново, а не берём из кэша
    assert out.ok and "из общего кэша" in out.text and db.user_has_track("bob", tid)  # не вышло — запасной файл


async def test_redownload_failure_keeps_old_file(db, cfg, monkeypatch):
    u = user(db)
    tid = add_track(db, cfg, "alice", "A", "T")

    async def nothing(req, progress, exclude=frozenset()):
        return None
    monkeypatch.setattr(service.downloaders, "fetch", nothing)
    out = await svc_for(db)._process(Job(user=u, text="x", notify=_notify),
                                     TrackRequest(artist="A", title="T", redownload=tid))
    assert not out.ok and "старый файл оставлен" in out.text
    assert Path(db.get_track(tid).path).read_bytes() == b"ID3old" and db.user_has_track("alice", tid)


async def test_redownload_passes_replace_id_and_skips_cache(db, cfg, monkeypatch):
    u = user(db)
    tid = add_track(db, cfg, "alice", "A", "T")
    seen = {}
    work = cfg.incoming_dir / "ytdlp" / "x"

    async def fake_fetch(req, progress, exclude=frozenset()):
        work.mkdir(parents=True, exist_ok=True)
        f = work / "f.mp3"
        f.write_bytes(b"x")
        return downloaders.Downloaded(f, "soulseek", "peer")

    async def fake_ingest(db_, path, hint, *, source, login, keep_mp3=False, replace_id=None):
        seen["replace_id"] = replace_id
        return library.IngestResult(db_.get_track(tid), True, None, {}, Path(path), replaced=True)
    monkeypatch.setattr(service.downloaders, "fetch", fake_fetch)
    monkeypatch.setattr(service.library, "ingest", fake_ingest)
    out = await svc_for(db)._process(Job(user=u, text="x", notify=_notify),
                                     TrackRequest(artist="A", title="T", redownload=tid))
    assert seen["replace_id"] == tid and out.ok and out.text.startswith("🔁 Файл заменён")


async def test_repeat_download_of_own_track_replaces_file(db, cfg, monkeypatch):
    """Свой трек скачивают ещё раз отдельно — это перекачка (заменить файл), а не «уже в медиатеке»."""
    u = user(db)
    tid = add_track(db, cfg, "alice", "A", "T")
    seen = []
    work = cfg.incoming_dir / "ytdlp" / "x"

    async def fake_fetch(req, progress, exclude=frozenset()):
        work.mkdir(parents=True, exist_ok=True)
        f = work / "f.mp3"
        f.write_bytes(b"x")
        return downloaders.Downloaded(f, "soulseek", "peer")

    async def fake_ingest(db_, path, hint, *, source, login, keep_mp3=False, replace_id=None):
        seen.append(replace_id)
        return library.IngestResult(db_.get_track(tid), True, None, {}, Path(path), replaced=True)
    monkeypatch.setattr(service.downloaders, "fetch", fake_fetch)
    monkeypatch.setattr(service.library, "ingest", fake_ingest)
    monkeypatch.setattr(service.metadata, "lookup_musicbrainz", lambda hint: _none())
    monkeypatch.setattr(service.metadata, "deezer_duration", lambda *a: _none())
    svc = svc_for(db)
    for origin in ("webapp", "user"):
        req = TrackRequest(artist="A", title="T")
        out = await svc._process(Job(user=u, text="x", notify=_notify, origin=origin, tracks=[req]), req)
        assert out.ok and out.text.startswith("🔁 Файл заменён") and seen[-1] == tid
        assert req.redownload == 0  # запрос из результатов поиска не меняется
    # альбом/импорт/волна свои треки по-прежнему пропускают — без скачивания
    seen.clear()
    reqs = [TrackRequest(artist="A", title="T"), TrackRequest(artist="B", title="U")]
    out = await svc._process(Job(user=u, text="x", notify=_notify, tracks=reqs), reqs[0])
    assert "Уже в вашей медиатеке" in out.text and seen == []
    req = TrackRequest(artist="A", title="T")
    out = await svc._process(Job(user=u, text="x", notify=_notify, origin="import", tracks=[req]), req)
    assert "Уже в вашей медиатеке" in out.text and seen == []


async def _none():
    return None


async def test_process_takes_duration_from_deezer(db, cfg, monkeypatch):
    u = user(db)
    got = {}

    async def no_mb(hint):
        return None

    async def dz(artist, title, isrc=""):
        return 238.0

    async def fake_fetch(req, progress, exclude=frozenset()):
        got["duration"], got["approx"] = req.duration, req.duration_from_mb
        return None
    monkeypatch.setattr(service.metadata, "lookup_musicbrainz", no_mb)
    monkeypatch.setattr(service.metadata, "deezer_duration", dz)
    monkeypatch.setattr(service.downloaders, "fetch", fake_fetch)
    monkeypatch.setattr(service.library, "lookup_cached", lambda *a, **k: None)
    await svc_for(db)._process(Job(user=u, text="x", notify=_notify),
                               TrackRequest(artist="T-Shirts & Sweats", title="I'm Not Made for This",
                                            isrc="SE5Q52503575"))
    assert got == {"duration": 238.0, "approx": True}


async def test_deezer_duration_by_isrc_and_search(monkeypatch):
    calls = []

    async def fake_get_json(url, params=None, **kw):
        calls.append(url)
        if "isrc:" in url:
            return {"error": {"type": "DataException"}} if url.endswith("NOPE") else {"duration": 238}
        return {"data": [{"title": "Song (Sped Up)", "duration": 100, "artist": {"name": "A"}},
                         {"title": "Other", "duration": 150, "artist": {"name": "A"}},
                         {"title": "Song", "duration": 201, "artist": {"name": "A"}}]}
    monkeypatch.setattr(http, "get_json", fake_get_json)
    assert await real_deezer_duration("A", "Song", "SE5Q52503575") == 238.0
    assert await real_deezer_duration("A", "Song", "NOPE") == 201.0  # по ISRC нет — поиск, ускоренную пропускаем
    assert await real_deezer_duration("", "", "") == 0.0


# ------------------------------------------------------------------ API: перекачка, сортировка, источник
@pytest.fixture
async def client(db, cfg, monkeypatch):
    user(db)
    monkeypatch.setattr(service.uploads, "free_space_ok", lambda extra=0: True)
    svc = Service(db, None)
    svc._ensure_import_feeder = lambda: None
    svc.uploads = Uploads(MagicMock(messenger=None), db)
    app = web.Application()
    w = webapp.setup_webapp(app, db=db, svc=svc, jf=MagicMock(), bot_token=TOKEN)
    started = []
    w.start_job = lambda u, reqs, title, **kw: started.append((u.login, reqs, title, kw)) or MagicMock()
    c = TestClient(TestServer(app))
    await c.start_server()
    c.db, c.svc, c.w, c.started = db, svc, w, started
    yield c
    await c.close()


def bearer(db, login="alice"):
    t, h = auth.new_token()
    db.add_device(login, "test", "web", h)
    return {"Authorization": f"Bearer {t}"}


async def test_redownload_endpoint(client, cfg):
    db = client.db
    tid = add_track(db, cfg, "alice", "A", "T")
    user(db, "bob", 2)
    other = add_track(db, cfg, "bob", "B", "X")
    r = await client.post(f"/api/tracks/{other}/redownload", headers=hdr())
    assert r.status == 404  # не в своей медиатеке
    assert (await client.post("/api/tracks/999999/redownload", headers=hdr())).status == 404
    r = await client.post(f"/api/tracks/{tid}/redownload", headers=hdr())
    data = await r.json()
    assert r.status == 200 and data["ok"] is True and "Перекачиваю" in data["text"]
    assert data["job"] == {"title": "Перекачать: A — T", "track_id": tid}
    assert db.get_track(tid).broken  # из кэша больше не выдаётся
    login, reqs, title, kw = client.started[0]
    assert login == "alice" and reqs[0].redownload == tid and (reqs[0].artist, reqs[0].title) == ("A", "T")
    r = await client.post(f"/api/tracks/{tid}/redownload", headers=hdr())
    assert r.status == 409 and (await r.json())["error"] == "busy"
    # в медиатеке видно, что файл проблемный
    items = (await (await client.get("/api/library", headers=hdr())).json())["items"]
    assert {i["id"]: i["broken"] for i in items}[tid] is True


async def test_redownload_rate_limit(client, cfg, monkeypatch):
    monkeypatch.setattr(webapp, "REDOWNLOAD_PER_HOUR", 2)
    ids = [add_track(client.db, cfg, "alice", "A", f"T{i}") for i in range(3)]
    for tid in ids[:2]:
        assert (await client.post(f"/api/tracks/{tid}/redownload", headers=hdr())).status == 200
    r = await client.post(f"/api/tracks/{ids[2]}/redownload", headers=hdr())
    assert r.status == 429 and (await r.json())["error"] == "rate_limited"


async def test_playlist_sort(client, cfg):
    db = client.db
    a = add_track(db, cfg, "alice", "Б", "Вторая", album="Z", duration=300)
    b = add_track(db, cfg, "alice", "А", "Первая", album="Y", duration=100)
    c = add_track(db, cfg, None, "В", "Третья", album="X", duration=200)
    pid = db.create_playlist("alice", "P", track_ids=[a, b, c])
    db._x("UPDATE playlist_items SET added_at=? WHERE track_id=?", (100, b))
    db._x("UPDATE playlist_items SET added_at=? WHERE track_id=?", (300, c))
    db._x("UPDATE playlist_items SET added_at=? WHERE track_id=?", (200, a))

    async def ids(qs=""):
        r = await client.get(f"/api/playlists/{pid}{qs}", headers=hdr())
        body = await r.json()
        return body["sort"], [t["id"] for t in body["tracks"]]
    assert await ids() == ("manual", [a, b, c])
    assert await ids("?sort=manual") == ("manual", [a, b, c])
    assert await ids("?sort=title") == ("title", [a, b, c])  # Вторая, Первая, Третья
    assert await ids("?sort=artist") == ("artist", [b, a, c])
    assert await ids("?sort=album") == ("album", [c, b, a])
    assert await ids("?sort=duration") == ("duration", [b, c, a])
    assert await ids("?sort=duration&order=desc") == ("duration", [a, c, b])
    assert await ids("?sort=added") == ("added", [c, a, b])  # когда добавлен в плейлист, новые первыми
    for bad in ("?sort=zzz", "?sort=title&order=up"):
        assert (await client.get(f"/api/playlists/{pid}{bad}", headers=hdr())).status == 400
    body = await (await client.get(f"/api/playlists/{pid}?sort=title", headers=hdr())).json()
    assert all("item_id" in t for t in body["tracks"])  # позиции плейлиста для действий — на месте


CSV = "Track name,Artist name\nГруппа крови,Кино\n"


def form(name, data):
    import aiohttp
    fd = aiohttp.FormData()
    fd.add_field("file", data, filename=name, content_type="text/csv")
    return fd


async def test_import_source_app_vs_tg(client):
    db = client.db
    r = await client.post("/api/import/file", data=form("a.csv", CSV.encode()), headers=bearer(db))
    app_imp = db.get_import((await r.json())["import_id"])
    assert app_imp["source"] == "app" and app_imp["chat_id"] == 0
    r = await client.post("/api/import/file", data=form("b.csv", CSV.encode()), headers=hdr())
    tg_imp = db.get_import((await r.json())["import_id"])
    assert tg_imp["source"] == "tg" and tg_imp["chat_id"] == TG_ID
    # прогресс и итог импорта из приложения в Telegram не уходят, из Telegram — как раньше
    m = MagicMock(send=AsyncMock(return_value=5), edit=AsyncMock(), send_document=AsyncMock())
    client.svc.messenger = m
    await client.svc._import_progress(app_imp["id"])
    await client.svc._finish_import(app_imp["id"])
    assert not m.send.await_count and not m.edit.await_count and not m.send_document.await_count
    await client.svc._import_progress(tg_imp["id"])
    assert m.send.await_args.args[0] == TG_ID


async def test_upload_source_app_has_no_chat(client):
    import aiohttp
    db = client.db
    for headers, chat in ((bearer(db), None), (hdr(), TG_ID)):
        fd = aiohttp.FormData()
        fd.add_field("files", b"ID3" * 100, filename="Кино - Звезда.mp3", content_type="audio/mpeg")
        r = await client.post("/api/upload", data=fd, headers=headers)
        uid = (await r.json())["uploads"][0]["id"]
        row = db.get_upload(uid)
        assert row["origin"] == "app" and row["chat_id"] == chat


def test_source_helper():
    req = {}
    assert webapp.WebApp.source(MagicMock(get=req.get)) == "tg"
    req[webapp._DEVICE_KEY] = 1
    assert webapp.WebApp.source(MagicMock(get=req.get)) == "app"


def test_lf_only():
    for f in ("bot/app/library.py", "bot/app/service.py", "bot/app/webapp.py", "bot/app/db.py"):
        assert b"\r" not in (Path(__file__).resolve().parents[1] / f).read_bytes(), f

