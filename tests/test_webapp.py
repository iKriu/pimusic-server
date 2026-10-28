"""Telegram Mini App: подпись initData, API и статика."""
import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import radio, search, webapp
from app.db import User
from app.search import Album, Result
from app.sources import TrackRequest

TOKEN = "123456:TEST-bot-token"
TG_ID = 4242


def make_init_data(user: dict | None = None, *, token: str = TOKEN, auth_date: int | None = None,
                   extra: dict | None = None) -> str:
    fields = {"auth_date": str(int(time.time()) if auth_date is None else auth_date),
              "query_id": "AAH-test"}
    if user is not None:
        fields["user"] = json.dumps(user, separators=(",", ":"), ensure_ascii=False)
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def hdr(tg_id: int = TG_ID) -> dict:
    return {"X-Telegram-Init-Data": make_init_data({"id": tg_id, "first_name": "Тест", "username": "t"})}


# ------------------------------------------------------------------ validate_init_data
def test_validate_init_data_valid():
    data = webapp.validate_init_data(make_init_data({"id": 7, "first_name": "Аня"}), TOKEN)
    assert data is not None
    assert data["user"] == {"id": 7, "first_name": "Аня"}
    assert data["query_id"] == "AAH-test"


def test_validate_init_data_with_signature_field():
    # новые клиенты добавляют поле signature — оно участвует в data_check_string
    init = make_init_data({"id": 7}, extra={"signature": "abc"})
    assert webapp.validate_init_data(init, TOKEN) is not None


def test_validate_init_data_tampered():
    init = make_init_data({"id": 7})
    assert "%3A7" in init
    assert webapp.validate_init_data(init.replace("%3A7", "%3A8"), TOKEN) is None
    assert webapp.validate_init_data(init, "999:other-token") is None
    forged = make_init_data({"id": 7}, token="999:other-token")
    assert webapp.validate_init_data(forged, TOKEN) is None


def test_validate_init_data_expired_and_missing():
    old = make_init_data({"id": 7}, auth_date=int(time.time()) - 86400 - 60)
    assert webapp.validate_init_data(old, TOKEN) is None
    assert webapp.validate_init_data(old, TOKEN, max_age=10 * 86400) is not None
    assert webapp.validate_init_data("", TOKEN) is None
    assert webapp.validate_init_data("auth_date=1&user=%7B%7D", TOKEN) is None  # нет hash
    assert webapp.validate_init_data(make_init_data({"id": 7}), "") is None
    assert webapp.validate_init_data("%%%garbage", TOKEN) is None


# ------------------------------------------------------------------ фикстуры сервера
class FakeSvc:
    def __init__(self):
        self.jobs = []
        self.queue = asyncio.Queue()
        self.active = {}
        self.delete_track = AsyncMock(return_value="🗑 Удалено из вашей медиатеки: A — B")
        self.cancel_imports = MagicMock(return_value=1)
        self.done = asyncio.Event()

    async def run_job(self, job, announce=True):
        self.jobs.append((job, announce))
        await job.notify(f"✅ {job.title}")
        self.done.set()

    def import_status_text(self, iid):
        return f"📥 Импорт #{iid}"


@pytest.fixture
def alice(db):
    u = User(login="alice", tg_id=TG_ID, jf_user_id="jf-alice", library_id="lib", stream_token="stok", is_admin=False)
    db.upsert_user(u)
    return u


@pytest.fixture
async def client(db, alice):
    svc = FakeSvc()
    jf = MagicMock()
    app = web.Application()
    w = webapp.setup_webapp(app, db=db, svc=svc, jf=jf, bot_token=TOKEN)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.svc, c.jf, c.w, c.db = svc, jf, w, db
    yield c
    await c.close()


def fake_result(owner=TG_ID):
    return Result(query="кино", owner=owner, artist="Кино",
                  tracks=[TrackRequest(artist="Кино", title="Группа крови", album="Группа крови", duration=286),
                          TrackRequest(artist="Кино", title="Кукушка", duration=400)],
                  albums=[Album(id="1", title="Звезда по имени Солнце", artist="Кино", year="1989")])


ALBUM = [TrackRequest(artist="Кино", title="Звезда по имени Солнце", album="Звезда по имени Солнце", track_no=1),
         TrackRequest(artist="Кино", title="Пачка сигарет", album="Звезда по имени Солнце", track_no=2)]


@pytest.fixture
def fake_search(monkeypatch):
    calls = []

    async def fake(q, owner):
        calls.append((q, owner))
        return fake_result(owner)

    async def fake_album(res, idx):
        res.album_tracks[idx] = ALBUM
        return ALBUM

    monkeypatch.setattr(search, "search", fake)
    monkeypatch.setattr(search, "catalog_search", fake)
    monkeypatch.setattr(search, "album_tracks", fake_album)
    return calls


# ------------------------------------------------------------------ авторизация / статика
async def test_api_requires_valid_init_data(client):
    r = await client.get("/api/me")
    assert r.status == 401
    r = await client.get("/api/me", headers={"X-Telegram-Init-Data": "auth_date=1&hash=00"})
    assert r.status == 401
    old = make_init_data({"id": TG_ID}, auth_date=int(time.time()) - 2 * 86400)
    r = await client.get("/api/me", headers={"X-Telegram-Init-Data": old})
    assert r.status == 401


async def test_unregistered_user_403(client):
    r = await client.get("/api/me", headers=hdr(777))
    assert r.status == 403
    assert (await r.json()) == {"error": "not_registered"}


async def test_static_index_served(client):
    r = await client.get("/app/")
    assert r.status == 200
    body = await r.text()
    assert "telegram-web-app.js" in body and "PiMusic" in body
    r = await client.get("/app", allow_redirects=False)
    assert r.status in (301, 302) and r.headers["Location"] == "/app/"
    assert 'type="module"' in body and "static/js/app.js" in body
    # фронтенд разбит на ES-модули и CSS-слои в подпапках static/
    r = await client.get("/app/static/js/api.js")
    assert r.status == 200 and "X-Telegram-Init-Data" in await r.text()
    for path in ("js/app.js", "css/tokens.css", "css/components.css"):
        r = await client.get(f"/app/static/{path}")
        assert r.status == 200, path
    assert (await client.get("/app/static/../webapp.py")).status in (403, 404)


async def test_me(client, cfg):
    r = await client.get("/api/me", headers=hdr())
    assert r.status == 200
    data = await r.json()
    assert data["login"] == "alice" and data["tracks"] == 0 and data["is_admin"] is False
    assert data["stream_url"].endswith("/stream/alice/stok.mp3")
    assert data["jellyfin_url"] == f"http://{cfg.public_host}:8096"


# ------------------------------------------------------------------ поиск и загрузка
async def test_search_and_download_track(client, fake_search):
    r = await client.get("/api/search", params={"q": "кино"}, headers=hdr())
    assert r.status == 200
    data = await r.json()
    assert fake_search == [("кино", TG_ID)]
    assert data["artist"] == "Кино"
    assert data["tracks"][0] == {"i": 0, "artist": "Кино", "title": "Группа крови", "album": "Группа крови",
                                 "duration": "4:46", "key": "кино|группа крови", "isrc": None,
                                 "cover": None, "preview": None}
    assert data["albums"][0]["title"] == "Звезда по имени Солнце" and data["albums"][0]["year"] == "1989"
    token = data["token"]

    r = await client.post("/api/download", json={"token": token, "kind": "track", "i": 1}, headers=hdr())
    assert r.status == 200
    assert await r.json() == {"queued": 1, "title": "Кино — Кукушка"}
    await asyncio.wait_for(client.svc.done.wait(), 2)
    job, announce = client.svc.jobs[0]
    assert announce is False and job.origin == "webapp" and job.user.login == "alice"
    assert [t.title for t in job.tracks] == ["Кукушка"]
    original = search.get(token).tracks[1]
    assert job.tracks[0] == original and job.tracks[0] is not original  # копия

    for _ in range(20):
        r = await client.get("/api/activity", headers=hdr())
        act = await r.json()
        if act["log"] and act["log"][0]["text"].startswith("✅"):
            break
        await asyncio.sleep(0.02)
    assert act["log"][0]["text"] == "✅ Кино — Кукушка"
    assert act["log"][1]["text"].startswith("⏳")
    assert act["queue"] == 0 and act["active"] == []


async def test_download_all_and_album(client, fake_search):
    token = (await (await client.get("/api/search", params={"q": "кино"}, headers=hdr())).json())["token"]
    r = await client.post("/api/download", json={"token": token, "kind": "all"}, headers=hdr())
    assert (await r.json())["queued"] == 2

    r = await client.get("/api/album", params={"token": token, "i": "0"}, headers=hdr())
    al = await r.json()
    assert [t["title"] for t in al["tracks"]] == ["Звезда по имени Солнце", "Пачка сигарет"]

    r = await client.post("/api/download", json={"token": token, "kind": "album", "i": 0}, headers=hdr())
    assert await r.json() == {"queued": 2, "title": "Кино — Звезда по имени Солнце"}
    r = await client.post("/api/download", json={"token": token, "kind": "album_track", "i": 0, "j": 1}, headers=hdr())
    assert (await r.json())["title"] == "Кино — Пачка сигарет"
    for _ in range(50):
        if len(client.svc.jobs) >= 3:
            break
        await asyncio.sleep(0.01)
    titles = [[t.title for t in j.tracks] for j, _ in client.svc.jobs]
    assert ["Группа крови", "Кукушка"] in titles
    assert ["Звезда по имени Солнце", "Пачка сигарет"] in titles
    assert ["Пачка сигарет"] in titles
    assert all(j.origin == "webapp" for j, _ in client.svc.jobs)


async def test_download_errors(client, fake_search, db):
    r = await client.post("/api/download", json={"token": "nope", "kind": "track", "i": 0}, headers=hdr())
    assert r.status == 404 and (await r.json())["error"] == "expired"
    token = (await (await client.get("/api/search", params={"q": "кино"}, headers=hdr())).json())["token"]
    r = await client.post("/api/download", json={"token": token, "kind": "track", "i": 9}, headers=hdr())
    assert r.status == 400
    r = await client.post("/api/download", json={"token": token, "kind": "track", "i": -1}, headers=hdr())
    assert r.status == 400
    r = await client.post("/api/download", json={"token": token, "kind": "weird"}, headers=hdr())
    assert r.status == 400
    r = await client.post("/api/download", data="not json", headers=hdr())
    assert r.status == 400
    # чужой результат поиска
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    r = await client.post("/api/download", json={"token": token, "kind": "track", "i": 0}, headers=hdr(555))
    assert r.status == 403
    r = await client.get("/api/search", params={"q": " "}, headers=hdr())
    assert r.status == 400
    assert client.svc.jobs == []


# ------------------------------------------------------------------ медиатека и обложки
def add_track(db, cfg, login, artist, title, *, cover=True):
    album_dir = cfg.library_dir / artist / "Album"
    album_dir.mkdir(parents=True, exist_ok=True)
    path = album_dir / f"{title}.mp3"
    path.write_bytes(b"ID3")
    if cover:
        (album_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff-jpeg")
    tid = db.add_track(match_key=f"{artist}|{title}".lower(), mbid=None, isrc=None, artist=artist, title=title,
                       album="Album", duration=200, path=str(path), source="test")
    db.link_track(login, tid, str(cfg.users_dir / login / artist / "Album" / path.name))
    return tid


async def test_library_and_signed_cover(client, db, cfg):
    t1 = add_track(db, cfg, "alice", "Artist1", "Song1")
    t2 = add_track(db, cfg, "alice", "Artist2", "Song2", cover=False)
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    t3 = add_track(db, cfg, "bob", "Artist3", "Song3")

    r = await client.get("/api/library", headers=hdr())
    data = await r.json()
    assert data["total"] == 2
    items = {it["id"]: it for it in data["items"]}
    assert set(items) == {t1, t2}
    assert items[t1]["artist"] == "Artist1" and items[t1]["title"] == "Song1" and items[t1]["album"] == "Album"
    assert items[t2]["cover"] is None
    url = items[t1]["cover"]
    assert url.startswith(f"/api/cover/{t1}?u=alice&s=")

    r = await client.get(url)  # без заголовка initData — <img> его не шлёт
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/jpeg"
    assert "max-age=86400" in r.headers["Cache-Control"]
    assert await r.read() == b"\xff\xd8\xff-jpeg"

    r = await client.get("/api/library", params={"q": "song2"}, headers=hdr())
    assert [it["id"] for it in (await r.json())["items"]] == [t2]

    # подделанная подпись, чужой трек, подпись другого пользователя
    assert (await client.get(f"/api/cover/{t1}?u=alice&s=deadbeef")).status == 404
    assert (await client.get(f"/api/cover/{t1}?u=alice")).status == 404
    sig_t3 = webapp.cover_signature(TOKEN, "alice", t3)
    assert (await client.get(f"/api/cover/{t3}?u=alice&s={sig_t3}")).status == 404  # alice не владеет t3
    bob_url = webapp.cover_url(TOKEN, "bob", t3)
    assert (await client.get(bob_url)).status == 200
    sig_bob = bob_url.split("s=")[1]
    assert (await client.get(f"/api/cover/{t1}?u=alice&s={sig_bob}")).status == 404
    assert (await client.get(f"/api/cover/{t1}?u=bob&s={sig_bob}")).status == 404
    forged = webapp.cover_signature("999:other", "alice", t1)
    assert (await client.get(f"/api/cover/{t1}?u=alice&s={forged}")).status == 404


async def test_delete(client, db, cfg):
    tid = add_track(db, cfg, "alice", "A", "B")
    r = await client.post("/api/delete", json={"track_id": tid}, headers=hdr())
    assert r.status == 200
    assert (await r.json())["ok"] is True
    client.svc.delete_track.assert_awaited_once()
    user, track_id = client.svc.delete_track.await_args.args
    assert user.login == "alice" and track_id == tid
    r = await client.post("/api/delete", json={"track_id": 99999}, headers=hdr())
    assert r.status == 404
    r = await client.post("/api/delete", json={"track_id": "x"}, headers=hdr())
    assert r.status == 400
    assert client.svc.delete_track.await_count == 1


# ------------------------------------------------------------------ импорт
async def test_import_endpoints(client, db):
    iid = db.create_import("alice", 1, "library.csv", [{"artist": "A", "title": f"T{i}"} for i in range(3)])
    db.create_import("bob", 2, "other.csv", [{"artist": "A", "title": "X"}])
    items = db._q("SELECT id FROM import_items WHERE import_id=? ORDER BY idx", (iid,))
    db.set_import_item(items[0]["id"], "done")
    r = await client.get("/api/import", headers=hdr())
    data = await r.json()
    assert len(data["imports"]) == 1
    imp = data["imports"][0]
    assert imp["id"] == iid and imp["filename"] == "library.csv" and imp["total"] == 3
    assert imp["counts"]["done"] == 1 and imp["counts"]["pending"] == 2
    assert imp["text"] == f"📥 Импорт #{iid}"

    r = await client.post("/api/import/cancel", headers=hdr())
    assert await r.json() == {"cancelled": 1}
    client.svc.cancel_imports.assert_called_once_with("alice")


# ------------------------------------------------------------------ волна
async def test_wave_refresh(client, monkeypatch, cfg):
    calls = []

    async def fake_refresh(jf, user, size, db):
        calls.append((jf, user.login, size, db))
        return 42

    monkeypatch.setattr(webapp.radio, "refresh_wave_playlist", fake_refresh)
    r = await client.post("/api/wave/refresh", headers=hdr())
    assert await r.json() == {"tracks": 42}
    assert calls == [(client.jf, "alice", cfg.wave_playlist_size, client.db)]

    async def boom(*a, **k):
        raise RuntimeError("jellyfin down")

    monkeypatch.setattr(webapp.radio, "refresh_wave_playlist", boom)
    r = await client.post("/api/wave/refresh", headers=hdr())
    assert r.status == 502


async def test_wave_discover(client, monkeypatch, cfg):
    calls = []
    reqs = [TrackRequest(artist="X", title="Y", origin="wave"), TrackRequest(artist="Z", title="W", origin="wave")]

    async def fake_build(db, jf, user, size):
        calls.append(size)
        return reqs, ["Кино — Кукушка"]

    monkeypatch.setattr(webapp.wave, "build", fake_build)
    r = await client.post("/api/wave/discover", json={"size": 5}, headers=hdr())
    assert await r.json() == {"seeds": ["Кино — Кукушка"], "queued": ["X — Y", "Z — W"]}
    assert calls == [5]
    await asyncio.wait_for(client.svc.done.wait(), 2)
    job, announce = client.svc.jobs[0]
    assert [t.label for t in job.tracks] == ["X — Y", "Z — W"] and announce is False
    for _ in range(50):
        if client.db.wave_seen("alice"):
            break
        await asyncio.sleep(0.01)
    assert len(client.db.wave_seen("alice")) == 2

    r = await client.post("/api/wave/discover", headers=hdr())  # без тела — размер по умолчанию
    assert r.status == 200
    assert calls[-1] == cfg.wave_size


async def test_wave_discover_empty(client, monkeypatch):
    async def fake_build(db, jf, user, size):
        return [], []

    monkeypatch.setattr(webapp.wave, "build", fake_build)
    r = await client.post("/api/wave/discover", json={}, headers=hdr())
    assert await r.json() == {"seeds": [], "queued": []}
    assert client.svc.jobs == []


# ------------------------------------------------------------------ регрессии ревью
async def test_non_ascii_init_hash_is_401_not_500(client):
    init = make_init_data({"id": TG_ID})
    bad = init.split("&hash=")[0] + "&hash=%D1%8F%D1%8F"  # «яя» — compare_digest на str падал TypeError
    assert webapp.validate_init_data(init.split("&hash=")[0] + "&hash=яя", TOKEN) is None
    r = await client.get("/api/me", headers={"X-Telegram-Init-Data": bad})
    assert r.status == 401


async def test_non_ascii_cover_signature_is_404(client, db, cfg):
    t1 = add_track(db, cfg, "alice", "Artist1", "Song1")
    r = await client.get(f"/api/cover/{t1}?u=alice&s=%D1%8F")
    assert r.status == 404


async def test_library_cover_lookup_off_event_loop(client, db, cfg, monkeypatch):
    import threading
    add_track(db, cfg, "alice", "Artist1", "Song1")
    threads = []
    real = webapp._cover_path

    def spy(*a):
        threads.append(threading.get_ident())
        return real(*a)

    monkeypatch.setattr(webapp, "_cover_path", spy)
    r = await client.get("/api/library", headers=hdr())
    assert (await r.json())["items"][0]["cover"]
    assert threads and threading.get_ident() not in threads, "is_file() не должен выполняться в event loop"


async def test_wave_discover_busy_while_in_flight(client, monkeypatch):
    gate = asyncio.Event()

    async def slow_build(db, jf, user, size):
        await gate.wait()
        return [], []

    monkeypatch.setattr(webapp.wave, "build", slow_build)
    first = asyncio.ensure_future(client.post("/api/wave/discover", json={}, headers=hdr()))
    for _ in range(100):
        if "alice" in client.w._wave_busy:
            break
        await asyncio.sleep(0.01)
    r = await client.post("/api/wave/discover", json={}, headers=hdr())
    assert r.status == 409 and (await r.json())["error"] == "busy"
    gate.set()
    assert (await first).status == 200
    r = await client.post("/api/wave/discover", json={}, headers=hdr())
    assert r.status == 200, "после завершения первого запроса можно снова"


# ------------------------------------------------------------------ каталог: три раздела, альбом, артист
NEW_ENDPOINTS = ["/api/catalog/album?id=1", "/api/catalog/artist?id=1", "/api/catalog/artist/tracks?id=1",
                 "/api/library/artists", "/api/library/albums", "/api/library/artist?a=X",
                 "/api/library/album?a=X&b=Y"]


async def test_new_endpoints_require_auth(client):
    for path in NEW_ENDPOINTS:
        r = await client.get(path)
        assert r.status == 401, path
        r = await client.get(path, headers=hdr(777))
        assert r.status == 403, path


async def test_search_three_sections(client, monkeypatch):
    calls = []

    async def fake(q, owner):
        calls.append((q, owner))
        res = fake_result(owner)
        res.artist_id, res.picture, res.fans = "1", "https://img/kino.jpg", 5
        res.albums[0].cover, res.albums[0].artist_id = "https://img/a.jpg", "1"
        res.artists = [search.Artist(id="1", name="Кино", picture="https://img/p.jpg", fans=5),
                       search.Artist(id="2", name="Кино-2")]
        return res

    monkeypatch.setattr(search, "catalog_search", fake)
    r = await client.get("/api/search", params={"q": "к" * 500}, headers=hdr())
    data = await r.json()
    assert calls == [("к" * webapp.MAX_QUERY, TG_ID)]  # длина запроса ограничена
    assert data["artist"] == "Кино" and data["artist_id"] == "1" and data["picture"] == "https://img/kino.jpg"
    assert len(data["tracks"]) == 2
    assert data["albums"][0] == {"i": 0, "id": "1", "title": "Звезда по имени Солнце", "year": "1989",
                                 "kind": "album", "artist": "Кино", "artist_id": "1", "cover": "https://img/a.jpg"}
    assert data["artists"] == [{"id": "1", "name": "Кино", "picture": "https://img/p.jpg", "fans": 5},
                               {"id": "2", "name": "Кино-2", "picture": None, "fans": 0}]


@pytest.fixture
def dz(monkeypatch):
    from test_search_catalog import ALBUM_302, artist_routes, deezer
    search._ALBUM_CACHE.clear()
    search._ARTIST_TRACKS.clear()
    routes = artist_routes(extra={"/album/302": ALBUM_302})
    calls = deezer(monkeypatch, routes)
    yield calls
    search._ALBUM_CACHE.clear()
    search._ARTIST_TRACKS.clear()


async def wait_jobs(client, n):
    for _ in range(100):
        if len(client.svc.jobs) >= n:
            break
        await asyncio.sleep(0.01)
    return client.svc.jobs


async def test_catalog_album_page_and_download(client, dz, db):
    r = await client.get("/api/catalog/album", params={"id": "302"}, headers=hdr())
    assert r.status == 200
    al = await r.json()
    assert al["title"] == "Звезда по имени Солнце" and al["artist"] == "Кино" and al["artist_id"] == "1"
    assert al["year"] == "1989" and al["cover"] == "https://img/xl.jpg"
    assert [(t["i"], t["no"], t["title"]) for t in al["tracks"]] == [(0, 1, "Звезда по имени Солнце"),
                                                                     (1, 2, "Пачка сигарет")]
    token = al["token"]
    r = await client.post("/api/download", json={"token": token, "kind": "all"}, headers=hdr())
    assert await r.json() == {"queued": 2, "title": "Кино — Звезда по имени Солнце"}
    r = await client.post("/api/download", json={"token": token, "kind": "track", "i": 1}, headers=hdr())
    assert await r.json() == {"queued": 1, "title": "Кино — Пачка сигарет"}
    jobs = await wait_jobs(client, 2)
    titles = sorted([t.title for t in j.tracks] for j, _ in jobs)
    assert titles == [["Звезда по имени Солнце", "Пачка сигарет"], ["Пачка сигарет"]]
    assert all(j.origin == "webapp" for j, _ in jobs)
    assert jobs[0][0].tracks[0].cover_url == "https://img/xl.jpg" and jobs[0][0].tracks[0].year == "1989"
    # чужой токен
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    r = await client.post("/api/download", json={"token": token, "kind": "all"}, headers=hdr(555))
    assert r.status == 403


async def test_catalog_bad_ids_and_errors(client, dz, monkeypatch):
    for bad in ("", "abc", "1/../2", "../../etc", "1" * 20):
        for path in ("/api/catalog/album", "/api/catalog/artist", "/api/catalog/artist/tracks"):
            r = await client.get(path, params={"id": bad}, headers=hdr())
            assert r.status == 400, (path, bad)
    assert not [c for c in dz if "etc" in c[0] or "abc" in c[0]]
    r = await client.get("/api/catalog/album", params={"id": "404"}, headers=hdr())
    assert r.status == 404 and (await r.json())["error"] == "not_found"
    r = await client.get("/api/catalog/artist", params={"id": "404"}, headers=hdr())
    assert r.status == 404

    async def boom(*a, **k):
        raise RuntimeError("deezer down")

    monkeypatch.setattr(webapp.search.http, "get_json", boom)
    r = await client.get("/api/catalog/album", params={"id": "777"}, headers=hdr())
    assert r.status == 502 and (await r.json())["error"] == "catalog_failed"


async def test_catalog_artist_page_and_all_tracks(client, dz):
    r = await client.get("/api/catalog/artist", params={"id": "1"}, headers=hdr())
    art = await r.json()
    assert art["name"] == "Кино" and art["fans"] == 123456 and art["picture"] == "https://img/kino-xl.jpg"
    assert [t["title"] for t in art["tracks"]] == ["Кукушка", "Группа крови"]
    assert [a["id"] for a in art["albums"]] == ["14", "15", "13", "11"]  # новые сверху
    # альбом из дискографии качается по тому же токену
    r = await client.post("/api/download", json={"token": art["token"], "kind": "album", "i": 2}, headers=hdr())
    assert await r.json() == {"queued": 3, "title": "Кино — Звезда по имени Солнце"}
    r = await client.post("/api/download", json={"token": art["token"], "kind": "all"}, headers=hdr())
    assert await r.json() == {"queued": 2, "title": "Кино — популярное"}

    r = await client.get("/api/catalog/artist/tracks", params={"id": "1"}, headers=hdr())
    allt = await r.json()
    assert allt["name"] == "Кино" and allt["albums"] == 3 and allt["capped"] is False
    assert [t["title"] for t in allt["tracks"]] == ["Звезда по имени Солнце", "Кукушка", "Группа крови",
                                                    "Закрой за мной дверь"]
    assert allt["tracks"][1]["album"] == "Звезда по имени Солнце" and allt["tracks"][1]["year"] == "1989"
    n = len(dz)
    r = await client.get("/api/catalog/artist/tracks", params={"id": "1"}, headers=hdr())
    again = await r.json()
    assert len(dz) == n, "повторный запрос — из кэша"
    assert again["token"] != allt["token"]
    r = await client.post("/api/download", json={"token": allt["token"], "kind": "all"}, headers=hdr())
    assert await r.json() == {"queued": 4, "title": "Кино — все треки"}
    r = await client.post("/api/download", json={"token": allt["token"], "kind": "track", "i": 3}, headers=hdr())
    assert (await r.json())["title"] == "Кино — Закрой за мной дверь"
    jobs = await wait_jobs(client, 4)
    assert any(len(j.tracks) == 4 for j, _ in jobs)


async def test_catalog_artist_tracks_cap(client, dz, monkeypatch):
    monkeypatch.setattr(search, "ARTIST_MAX_TRACKS", 2)
    r = await client.get("/api/catalog/artist/tracks", params={"id": "1"}, headers=hdr())
    data = await r.json()
    assert len(data["tracks"]) == 2 and data["capped"] is True


# ------------------------------------------------------------------ своя медиатека: исполнители и альбомы
def add_lib(db, cfg, login, artist, album, title, *, cover=True, track_artist=None):
    album_dir = cfg.library_dir / artist / album
    album_dir.mkdir(parents=True, exist_ok=True)
    path = album_dir / f"{title}.mp3"
    path.write_bytes(b"ID3")
    if cover:
        (album_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff-" + album.encode())
    tid = db.add_track(match_key=f"{artist}|{title}".lower(), mbid=None, isrc=None, artist=track_artist or artist,
                       title=title, album=album, duration=100, path=str(path), source="test")
    db.link_track(login, tid, str(cfg.users_dir / login / artist / album / path.name))
    return tid


@pytest.fixture
def lib(db, cfg, alice):
    ids = {
        "k1": add_lib(db, cfg, "alice", "Кино", "Группа крови", "01 Группа крови"),
        "k2": add_lib(db, cfg, "alice", "Кино", "Группа крови", "02 Закрой за мной дверь"),
        "k3": add_lib(db, cfg, "alice", "Кино", "Звезда", "01 Звезда", cover=False,
                      track_artist="Кино feat. Гость"),
        "a1": add_lib(db, cfg, "alice", "AC_DC", "Back in Black", "01 Hells Bells", track_artist="AC/DC"),
    }
    (cfg.library_dir / "Кино" / "folder.jpg").write_bytes(b"\xff\xd8\xff-kino")
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    ids["b1"] = add_lib(db, cfg, "bob", "Земфира", "Вендетта", "01 Дышать")
    (cfg.library_dir / "Земфира" / "folder.jpg").write_bytes(b"\xff\xd8\xff-zem")
    return ids


def test_db_library_helpers(db, cfg, lib):
    assert db.user_artists("alice") == [("AC_DC", "AC/DC", 1), ("Кино", "Кино", 3)]
    albums = db.user_albums("alice")
    assert [(a, b, t, ar, n) for a, b, t, ar, n, _ in albums] == [
        ("AC_DC", "Back in Black", "Back in Black", "AC/DC", 1),
        ("Кино", "Группа крови", "Группа крови", "Кино", 2),
        ("Кино", "Звезда", "Звезда", "Кино", 1)]
    rows = db.user_tracks_by_artist("alice", "Кино")
    assert [t.title for t, _ in rows] == ["01 Группа крови", "02 Закрой за мной дверь", "01 Звезда"]
    assert db.user_tracks_by_artist("alice", "Земфира") == []
    assert db.user_artists("nobody") == []


async def test_library_artists_albums_and_artist_page(client, lib):
    r = await client.get("/api/library/artists", headers=hdr())
    items = (await r.json())["items"]
    assert [(i["key"], i["name"], i["count"]) for i in items] == [("AC_DC", "AC/DC", 1), ("Кино", "Кино", 3)]
    assert items[0]["image"] is None
    img = items[1]["image"]
    assert img.startswith("/api/artist-image?u=alice&a=") and "&s=" in img
    r = await client.get(img)  # <img> без initData
    assert r.status == 200 and await r.read() == b"\xff\xd8\xff-kino"
    assert r.headers["Content-Type"] == "image/jpeg"

    r = await client.get("/api/library/albums", headers=hdr())
    albums = (await r.json())["items"]
    assert [(a["artist"], a["title"], a["count"]) for a in albums] == [
        ("AC/DC", "Back in Black", 1), ("Кино", "Группа крови", 2), ("Кино", "Звезда", 1)]
    assert albums[1]["cover"] == webapp.cover_url(TOKEN, "alice", lib["k1"])
    assert albums[2]["cover"] is None
    assert albums[1]["artist_key"] == "Кино" and albums[1]["key"] == "Группа крови"

    r = await client.get("/api/library/artist", params={"a": "Кино"}, headers=hdr())
    art = await r.json()
    assert art["name"] == "Кино" and art["count"] == 3 and art["image"] == img
    assert [(g["title"], [t["id"] for t in g["tracks"]]) for g in art["albums"]] == [
        ("Группа крови", [lib["k1"], lib["k2"]]), ("Звезда", [lib["k3"]])]
    assert art["albums"][0]["cover"] and art["albums"][1]["cover"] is None
    assert art["albums"][1]["tracks"][0]["artist"] == "Кино feat. Гость"

    r = await client.get("/api/library/album", params={"a": "Кино", "b": "Группа крови"}, headers=hdr())
    al = await r.json()
    assert al["title"] == "Группа крови" and al["artist"] == "Кино" and al["artist_key"] == "Кино"
    assert [t["title"] for t in al["tracks"]] == ["01 Группа крови", "02 Закрой за мной дверь"]
    assert al["cover"] == webapp.cover_url(TOKEN, "alice", lib["k1"])

    # чужая медиатека и мусорные параметры
    r = await client.get("/api/library/artist", params={"a": "Земфира"}, headers=hdr())
    assert r.status == 404
    r = await client.get("/api/library/album", params={"a": "Земфира", "b": "Вендетта"}, headers=hdr())
    assert r.status == 404
    r = await client.get("/api/library/artist", headers=hdr())
    assert r.status == 400
    r = await client.get("/api/library/artist", params={"a": "x" * 300}, headers=hdr())
    assert r.status == 400
    r = await client.get("/api/library/album", params={"a": "Кино"}, headers=hdr())
    assert r.status == 400
    r = await client.get("/api/library/artist", params={"a": "../Кино"}, headers=hdr())
    assert r.status == 404


async def test_signed_artist_image_ownership(client, lib):
    good = webapp.artist_image_url(TOKEN, "alice", "Кино")
    assert (await client.get(good)).status == 200
    sig = webapp.artist_signature(TOKEN, "alice", "Кино")
    # подпись не та / нет / не-ASCII / чужим ключом
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "Кино", "s": "deadbeef"})).status == 404
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "Кино"})).status == 404
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "Кино", "s": "я"})).status == 404
    forged = webapp.artist_signature("999:other", "alice", "Кино")
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "Кино", "s": forged})).status == 404
    # подпись alice к другой папке / другому пользователю не подходит
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "Земфира", "s": sig})).status == 404
    assert (await client.get("/api/artist-image", params={"u": "bob", "a": "Кино", "s": sig})).status == 404
    # корректная подпись, но у alice нет треков Земфиры — владение проверяется
    zsig = webapp.artist_signature(TOKEN, "alice", "Земфира")
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "Земфира", "s": zsig})).status == 404
    # у bob есть — ему отдаём
    assert (await client.get(webapp.artist_image_url(TOKEN, "bob", "Земфира"))).status == 200
    # обход пути подписью не спасает: путь берётся только из БД
    tsig = webapp.artist_signature(TOKEN, "alice", "..")
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "..", "s": tsig})).status == 404
    # исполнитель без folder.jpg
    asig = webapp.artist_signature(TOKEN, "alice", "AC_DC")
    assert (await client.get("/api/artist-image", params={"u": "alice", "a": "AC_DC", "s": asig})).status == 404


async def test_library_groups_off_event_loop(client, lib, monkeypatch):
    import threading
    threads = []
    real = webapp._cover_path

    def spy(*a):
        threads.append(threading.get_ident())
        return real(*a)

    monkeypatch.setattr(webapp, "_cover_path", spy)
    for path in ("/api/library/albums", "/api/library/artist?a=%D0%9A%D0%B8%D0%BD%D0%BE"):
        threads.clear()
        r = await client.get(path, headers=hdr())
        assert r.status == 200
        assert threads and threading.get_ident() not in threads, path


# ------------------------------------------------------------------ дополнения для M3-фронтенда
async def test_library_keys(client, db, cfg):
    add_track(db, cfg, "alice", "Artist1", "Song1")
    tid = add_track(db, cfg, "alice", "Artist2", "Song2")
    db._x("UPDATE tracks SET isrc='USX1' WHERE id=?", (tid,))
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    add_track(db, cfg, "bob", "Artist3", "Song3")
    r = await client.get("/api/library/keys", headers=hdr())
    assert r.status == 200
    assert await r.json() == {"keys": ["artist1|song1", "artist2|song2"], "isrc": ["USX1"]}
    assert (await client.get("/api/library/keys")).status == 401


async def test_download_selected_tracks(client, fake_search):
    token = (await (await client.get("/api/search", params={"q": "кино"}, headers=hdr())).json())["token"]
    r = await client.post("/api/download", json={"token": token, "kind": "tracks", "idx": [1, 0, 1]}, headers=hdr())
    assert r.status == 200
    assert await r.json() == {"queued": 2, "title": "Кино — 2 тр."}
    r = await client.post("/api/download", json={"token": token, "kind": "tracks", "idx": [1]}, headers=hdr())
    assert await r.json() == {"queued": 1, "title": "Кино — Кукушка"}
    for bad in ([], [5], [-1], "1", ["x"], list(range(webapp.MAX_PICK + 1))):
        r = await client.post("/api/download", json={"token": token, "kind": "tracks", "idx": bad}, headers=hdr())
        assert r.status == 400, bad
    for _ in range(50):
        if len(client.svc.jobs) >= 2:
            break
        await asyncio.sleep(0.01)
    titles = [[t.title for t in j.tracks] for j, _ in client.svc.jobs]
    assert ["Группа крови", "Кукушка"] in titles and ["Кукушка"] in titles


async def test_import_pause_resume_stop_failed(client, db):
    iid = db.create_import("alice", 1, "library.csv", [{"artist": "A", "title": f"T{i}"} for i in range(3)])
    other = db.create_import("bob", 2, "other.csv", [{"artist": "A", "title": "X"}])
    items = db._q("SELECT id FROM import_items WHERE import_id=? ORDER BY idx", (iid,))
    db.set_import_item(items[0]["id"], "failed", "не нашлось")
    svc = client.svc
    svc.pause_import = MagicMock(return_value=True)
    svc.resume_import = MagicMock(return_value=True)
    svc.stop_import = MagicMock(return_value=True)

    data = await (await client.get("/api/import", headers=hdr())).json()
    assert data["imports"][0]["state"] == "running"

    r = await client.post("/api/import/pause", json={"id": iid}, headers=hdr())
    assert await r.json() == {"ok": True, "state": "paused"}
    svc.pause_import.assert_called_once_with(iid, "alice")
    r = await client.post("/api/import/resume", json={"id": iid}, headers=hdr())
    assert await r.json() == {"ok": True, "state": "running"}
    svc.resume_import.assert_called_once_with(iid, "alice")
    svc.pause_import.return_value = False
    r = await client.post("/api/import/pause", json={"id": iid}, headers=hdr())
    assert r.status == 409
    r = await client.post("/api/import/pause", json={"id": "x"}, headers=hdr())
    assert r.status == 400

    r = await client.post("/api/import/cancel", json={"id": iid}, headers=hdr())
    assert await r.json() == {"cancelled": 1}
    svc.stop_import.assert_called_once_with(iid, "alice")
    svc.cancel_imports.assert_not_called()
    svc.stop_import.return_value = False
    r = await client.post("/api/import/cancel", json={"id": other}, headers=hdr())
    assert r.status == 404

    r = await client.get("/api/import/failed", params={"id": str(iid)}, headers=hdr())
    assert await r.json() == {"items": [{"artist": "A", "title": "T0", "album": "", "note": "не нашлось"}]}
    r = await client.get("/api/import/failed", params={"id": str(other)}, headers=hdr())
    assert r.status == 404  # чужой импорт
    r = await client.get("/api/import/failed", params={"id": "zz"}, headers=hdr())
    assert r.status == 400
    for path in ("/api/import/pause", "/api/import/resume"):
        assert (await client.post(path, json={"id": iid})).status == 401


# ------------------------------------------------------------------ /api/wave/now (встроенный плеер)
class FakeStation:
    def __init__(self, item=None, listeners=1, started_at=1_700_000_000.5):
        self.current, self.listeners, self.started_at = item, set(range(listeners)), started_at


class FakeRadio:
    def __init__(self, stations=None):
        self.stations = stations or {}
        self.asked = []

    def station(self, login):
        self.asked.append(login)
        return self.stations.get(login)


@pytest.fixture
async def radio_client(db, alice):
    fr = FakeRadio()
    app = web.Application()
    webapp.setup_webapp(app, db=db, svc=FakeSvc(), jf=MagicMock(), bot_token=TOKEN, radio=fr)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.radio, c.db = fr, db
    yield c
    await c.close()


async def test_wave_now_without_radio_is_offline(client):
    r = await client.get("/api/wave/now", headers=hdr())
    assert r.status == 200
    data = await r.json()
    assert data == {"live": False, "listeners": 0, "artist": None, "title": None, "album": None,
                    "duration": None, "started_at": None, "cover": None, "track_id": None}
    assert r.headers["Cache-Control"] == "no-store"


async def test_wave_now_requires_auth(radio_client):
    assert (await radio_client.get("/api/wave/now")).status == 401
    assert (await radio_client.get("/api/wave/now", headers=hdr(tg_id=777))).status == 403
    assert radio_client.radio.asked == []


async def test_wave_now_idle_station(radio_client):
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["live"] is False and data["title"] is None
    assert radio_client.radio.asked == ["alice"]


async def test_wave_now_live_with_cover(radio_client, db, cfg):
    tid = add_track(db, cfg, "alice", "Кино", "Кукушка")
    link = db.user_has_track("alice", tid)
    radio_client.radio.stations["alice"] = FakeStation(
        {"Name": "Кукушка", "Artists": ["Кино"], "Album": "Чёрный альбом", "Path": link,
         "RunTimeTicks": 4_000_000_000}, listeners=2)
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["live"] is True and data["listeners"] == 2
    assert (data["artist"], data["title"], data["album"]) == ("Кино", "Кукушка", "Чёрный альбом")
    assert data["duration"] == 400 and data["started_at"] == 1_700_000_000
    assert data["cover"] == webapp.cover_url(TOKEN, "alice", tid)
    assert (await radio_client.get(data["cover"])).status == 200


async def test_wave_now_cover_by_library_path_and_foreign_track(radio_client, db, cfg):
    tid = add_track(db, cfg, "alice", "A", "Song")
    lib_path = db.get_track(tid).path
    radio_client.radio.stations["alice"] = FakeStation({"Name": "Song", "AlbumArtist": "A", "Path": lib_path})
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["artist"] == "A" and data["cover"] == webapp.cover_url(TOKEN, "alice", tid)
    assert data["duration"] is None
    # трек есть только у другого пользователя / без обложки → cover=null, но метаданные отдаются
    db.upsert_user(User(login="bob", tg_id=TG_ID + 1, jf_user_id="jf-bob", library_id="lib", stream_token="b",
                        is_admin=False))
    other = add_track(db, cfg, "bob", "B", "Other")
    radio_client.radio.stations["alice"] = FakeStation({"Name": "Other", "Artists": ["B"],
                                                        "Path": db.get_track(other).path})
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["title"] == "Other" and data["cover"] is None
    nocover = add_track(db, cfg, "alice", "C", "Bare", cover=False)
    radio_client.radio.stations["alice"] = FakeStation({"Name": "Bare", "Artists": ["C"],
                                                        "Path": db.user_has_track("alice", nocover)})
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["cover"] is None


async def test_wave_now_live_between_tracks(radio_client):
    radio_client.radio.stations["alice"] = FakeStation(None, listeners=1)
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["live"] is True and data["listeners"] == 1 and data["title"] is None


def test_radio_server_station_only_running(db, cfg):
    rs = radio.RadioServer(db, MagicMock())
    assert rs.station("alice") is None
    st = radio.Station(MagicMock(), User(login="alice", tg_id=1, jf_user_id="j", library_id=None,
                                         stream_token="t", is_admin=False))
    rs.stations["alice"] = st
    assert st.current is None and st.started_at == 0.0
    assert rs.station("alice") is None  # не запущена


# ------------------------------------------------------------------ 30-секундные фрагменты в JSON треков
PREVIEW = "https://cdns-preview-d.dzcdn.net/stream/c-deadbeef-1.mp3"


def test_track_json_preview_field():
    t = TrackRequest(artist="Кино", title="Кукушка", preview=PREVIEW)
    assert webapp._track_json(0, t)["preview"] == PREVIEW
    assert webapp._ctrack_json(0, t)["preview"] == PREVIEW
    assert webapp._track_json(0, TrackRequest(artist="A", title="B"))["preview"] is None
    # даже если в TrackRequest попало что-то чужое — наружу не отдаём
    assert webapp._track_json(0, TrackRequest(artist="A", title="B", preview="https://evil.com/x.mp3"))["preview"] is None


async def test_search_returns_preview(client, monkeypatch):
    async def fake(q, owner):
        res = fake_result(owner)
        res.tracks[0] = TrackRequest(artist="Кино", title="Группа крови", preview=PREVIEW)
        return res

    monkeypatch.setattr(search, "catalog_search", fake)
    data = await (await client.get("/api/search", params={"q": "кино"}, headers=hdr())).json()
    assert [t["preview"] for t in data["tracks"]] == [PREVIEW, None]


@pytest.fixture
def dz_preview(monkeypatch):
    from test_search_catalog import ALBUM_302, artist_routes, deezer, dz_track
    search._ALBUM_CACHE.clear()
    search._ARTIST_TRACKS.clear()
    routes = artist_routes(extra={"/album/302": ALBUM_302})
    routes["/album/302/tracks"] = {"data": [dz_track("Звезда по имени Солнце", preview=PREVIEW),
                                            dz_track("Пачка сигарет", preview="https://evil.example.com/p.mp3"),
                                            dz_track("Спокойная ночь")]}
    routes["/artist/1/top"] = {"data": [dz_track("Кукушка", preview=PREVIEW), dz_track("Группа крови")]}
    routes["/album/13"]["tracks"]["data"][0] = dz_track("Звезда по имени Солнце", preview=PREVIEW)
    yield deezer(monkeypatch, routes)
    search._ALBUM_CACHE.clear()
    search._ARTIST_TRACKS.clear()


async def test_catalog_pages_return_preview(client, dz_preview):
    al = await (await client.get("/api/catalog/album", params={"id": "302"}, headers=hdr())).json()
    assert [t["preview"] for t in al["tracks"]] == [PREVIEW, None, None]
    art = await (await client.get("/api/catalog/artist", params={"id": "1"}, headers=hdr())).json()
    assert [(t["title"], t["preview"]) for t in art["tracks"]] == [("Кукушка", PREVIEW), ("Группа крови", None)]
    allt = await (await client.get("/api/catalog/artist/tracks", params={"id": "1"}, headers=hdr())).json()
    assert allt["tracks"][0]["title"] == "Звезда по имени Солнце" and allt["tracks"][0]["preview"] == PREVIEW
    assert all("preview" in t for t in allt["tracks"])
    # фрагмент не влияет на скачивание: тот же токен качает трек как раньше
    r = await client.post("/api/download", json={"token": al["token"], "kind": "track", "i": 0}, headers=hdr())
    assert (await r.json())["queued"] == 1


# ------------------------------------------------------------------ Android 2.0: аддитивные поля
async def test_track_keys_and_size(client, db, cfg):
    tid = add_track(db, cfg, "alice", "Кино", "Кукушка")
    items = (await (await client.get("/api/library", headers=hdr())).json())["items"]
    it = next(i for i in items if i["id"] == tid)
    assert (it["artist_key"], it["album_key"], it["size"]) == ("Кино", "Album", 3)
    # ключи подходят для перехода «К исполнителю / К альбому»
    r = await client.get("/api/library/album", params={"a": it["artist_key"], "b": it["album_key"]}, headers=hdr())
    assert r.status == 200
    r = await client.get("/api/library/artist", params={"a": it["artist_key"]}, headers=hdr())
    assert r.status == 200
    got = (await (await client.get(f"/api/tracks?ids={tid}", headers=hdr())).json())["items"][0]
    assert got["artist_key"] == "Кино" and got["size"] == 3
    pid = db.create_playlist("alice", "P", track_ids=[tid])
    pt = (await (await client.get(f"/api/playlists/{pid}", headers=hdr())).json())["tracks"][0]
    assert (pt["artist_key"], pt["album_key"]) == ("Кино", "Album")
    Path(db.get_track(tid).path).unlink()
    pt = (await (await client.get(f"/api/playlists/{pid}", headers=hdr())).json())["tracks"][0]
    assert pt["size"] is None  # файла нет — размер неизвестен


async def test_wave_now_track_id(radio_client, db, cfg):
    tid = add_track(db, cfg, "alice", "Кино", "Кукушка")
    radio_client.radio.stations["alice"] = FakeStation(
        {"Name": "Кукушка", "Artists": ["Кино"], "Path": db.user_has_track("alice", tid)})
    data = await (await radio_client.get("/api/wave/now", headers=hdr())).json()
    assert data["track_id"] == tid and data["cover"] and data["title"] == "Кукушка"


async def test_activity_active_items(client):
    req = TrackRequest(artist="Кино", title="Звезда", redownload=7)
    client.svc.active = {1: "alice: Кино — Звезда", 2: "bob: X — Y"}
    client.svc.active_info = {1: {"login": "alice", "req": req, "progress": "Soulseek: качаю 40 %"},
                              2: {"login": "bob", "req": TrackRequest(artist="X", title="Y"), "progress": ""}}
    data = await (await client.get("/api/activity", headers=hdr())).json()
    assert data["active"] == ["Кино — Звезда"]  # старое строковое поле — как было
    assert data["active_items"] == [{"text": "Кино — Звезда", "artist": "Кино", "title": "Звезда", "track_id": 7,
                                     "progress": "Soulseek: качаю 40 %"}]


async def test_worker_progress_fills_active_info(db, cfg, monkeypatch):
    from app import service as service_mod
    from app.service import Job, Service
    svc = Service(db, None)
    seen = []

    async def fake_process(job, req):
        svc.active_info[0]["progress"] = "x"
        seen.append(dict(svc.active_info[0]))
        return service_mod.TrackOutcome(req, True, "ok")
    monkeypatch.setattr(svc, "_process", fake_process)
    q = asyncio.Queue()
    fut = asyncio.get_running_loop().create_future()

    async def notify(text):
        pass
    req = TrackRequest(artist="A", title="B")
    await q.put((Job(user=User("alice", 1, "jf", None, "t", False), text="x", notify=notify, tracks=[req]), req, fut))
    await svc._worker_step(0, q)
    assert seen[0]["login"] == "alice" and seen[0]["req"] is req and svc.active_info == {}


async def test_static_woff2_font_type_and_cache(client):
    fonts = sorted((webapp.STATIC_DIR / "fonts").glob("*.woff2"))
    assert fonts, "в static/fonts нет шрифтов"
    r = await client.get(f"/app/static/fonts/{fonts[0].name}")
    assert r.status == 200 and r.headers["Content-Type"] == "font/woff2"
    assert r.headers["Cache-Control"] == "no-cache"  # как у остальной статики
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    css = await client.get("/app/static/css/tokens.css")
    assert css.headers["Cache-Control"] == r.headers["Cache-Control"]
