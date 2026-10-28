"""Плеер по своим трекам и плейлисты: подписанный поток, доступ, API плейлистов, зеркало в Jellyfin, /start pl_."""
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import library, webapp
from app.db import User
from app.service import Service

from test_webapp import TOKEN, FakeSvc, hdr
from test_bot_handlers import bot, ctx, make_update  # noqa: F401 — фикстура bot

ALICE, BOB, CAROL = 4242, 555, 777


# ------------------------------------------------------------------ фикстуры
class PlSvc(FakeSvc):
    def __init__(self):
        super().__init__()
        self.changed: list[tuple] = []
        self.lib_changed: list[str] = []

    def playlist_changed(self, pid, delay=None, urgent=False):
        self.changed.append((pid, delay))
        self.urgent = getattr(self, "urgent", []) + ([pid] if urgent else [])

    def library_changed(self, user, delay=15):
        self.lib_changed.append(user.login)


def add_user(db, login, tg_id):
    u = User(login=login, tg_id=tg_id, jf_user_id="jf-" + login, library_id="lib-" + login,
             stream_token="st" + login, is_admin=False)
    db.upsert_user(u)
    return u


def add_lib(db, cfg, login, artist, album, title, *, cover=True, data=b"ID3" + bytes(range(256)) * 4,
            duration=100):
    """Трек в общем кэше + запись о ссылке пользователя (сам файл ссылки не создаётся)."""
    album_dir = cfg.library_dir / artist / album
    album_dir.mkdir(parents=True, exist_ok=True)
    path = album_dir / f"{title}.mp3"
    path.write_bytes(data)
    if cover:
        (album_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff-" + album.encode())
    tid = db.add_track(match_key=f"{artist}|{title}".lower(), mbid=None, isrc=None, artist=artist, title=title,
                       album=album, duration=duration, path=str(path), source="test")
    if login:
        db.link_track(login, tid, str(cfg.users_dir / login / artist / album / path.name))
    return tid


@pytest.fixture
def users(db):
    return {"alice": add_user(db, "alice", ALICE), "bob": add_user(db, "bob", BOB),
            "carol": add_user(db, "carol", CAROL)}


@pytest.fixture
def lib(db, cfg, users):
    return {
        "a1": add_lib(db, cfg, "alice", "Кино", "Группа крови", "01 Группа крови", duration=286),
        "a2": add_lib(db, cfg, "alice", "Кино", "Группа крови", "02 Закрой за мной дверь", duration=254),
        "a3": add_lib(db, cfg, "alice", "Кино", "Звезда", "01 Звезда", cover=False),
        "a4": add_lib(db, cfg, "alice", "AC_DC", "Back in Black", "01 Hells Bells"),
        "b1": add_lib(db, cfg, "bob", "Земфира", "Вендетта", "01 Дышать"),
        "b2": add_lib(db, cfg, "bob", "Земфира", "Вендетта", "02 Мы разбиваемся"),
        "c1": add_lib(db, cfg, "carol", "Сплин", "Гранатовый альбом", "01 Орбит без сахара"),
    }


@pytest.fixture
async def client(db, users):
    svc = PlSvc()
    app = web.Application()
    w = webapp.setup_webapp(app, db=db, svc=svc, jf=MagicMock(), bot_token=TOKEN)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.svc, c.w, c.db = svc, w, db
    yield c
    await c.close()


async def jget(client, path, tg=ALICE, status=200, **params):
    r = await client.get(path, params=params or None, headers=hdr(tg))
    assert r.status == status, (path, r.status, await r.text())
    return await r.json()


async def jpost(client, path, body=None, tg=ALICE, status=200):
    r = await client.post(path, json=body if body is not None else {}, headers=hdr(tg))
    assert r.status == status, (path, r.status, await r.text())
    return await r.json()


async def make_pl(client, name="Дорога", tg=ALICE, **extra):
    return await jpost(client, "/api/playlists", {"name": name, **extra}, tg=tg)


async def shared_pl(client, lib, members=(BOB,)):
    """Общий плейлист alice с её a1 и вступившими участниками."""
    pl = await make_pl(client, shared=True, track_ids=[lib["a1"]])
    for tg in members:
        await jpost(client, "/api/playlists/join", {"token": pl["invite"]["token"]}, tg=tg)
    return pl


# ------------------------------------------------------------------ подпись потока
def test_stream_expiry_rounds_up_to_hour():
    assert webapp.stream_expiry(now=10 * 3600) == 22 * 3600
    assert webapp.stream_expiry(now=10 * 3600 + 1) == 23 * 3600
    assert webapp.stream_expiry(now=10 * 3600 + 3599) == 23 * 3600
    # в течение часа ссылка стабильна — кэш браузера работает
    assert webapp.stream_url(TOKEN, "alice", 5, now=36001) == webapp.stream_url(TOKEN, "alice", 5, now=39599)


def test_stream_signature_separate_key():
    s = webapp.stream_signature(TOKEN, "alice", 5, 3600)
    assert len(s) == 32
    assert s != webapp.cover_signature(TOKEN, "alice", 5)[:32]
    assert s != webapp.stream_signature(TOKEN, "alice", 5, 7200)
    assert s != webapp.stream_signature(TOKEN, "alice", 6, 3600)
    assert s != webapp.stream_signature("1:other", "alice", 5, 3600)


async def test_library_items_have_stream_seconds_owned(client, lib):
    items = (await jget(client, "/api/library"))["items"]
    it = next(i for i in items if i["id"] == lib["a1"])
    assert it["seconds"] == 286 and it["duration"] == "4:46" and it["owned"] is True
    assert it["stream"].startswith(f"/api/stream/{lib['a1']}?u=alice&e=")
    art = await jget(client, "/api/library/artist", a="Кино")
    t = art["albums"][0]["tracks"][0]
    assert {"seconds", "stream", "owned"} <= set(t) and t["owned"] is True
    al = await jget(client, "/api/library/album", a="Кино", b="Группа крови")
    assert al["tracks"][0]["stream"]


async def test_stream_serves_file_with_range(client, lib, db, cfg):
    url = webapp.stream_url(TOKEN, "alice", lib["a1"])
    data = (cfg.library_dir / "Кино" / "Группа крови" / "01 Группа крови.mp3").read_bytes()
    r = await client.get(url)  # <audio> без initData
    assert r.status == 200
    assert r.headers["Content-Type"] == "audio/mpeg"
    assert r.headers["Cache-Control"] == "private, max-age=3600"
    assert await r.read() == data
    r = await client.get(url, headers={"Range": "bytes=10-19"})
    assert r.status == 206
    assert r.headers["Content-Range"] == f"bytes 10-19/{len(data)}"
    assert await r.read() == data[10:20]


async def test_stream_prefers_user_link(client, lib, db, cfg):
    link = cfg.users_dir / "alice" / "Кино" / "Группа крови" / "01 Группа крови.mp3"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_bytes(b"LINKED")
    r = await client.get(webapp.stream_url(TOKEN, "alice", lib["a1"]))
    assert r.status == 200 and await r.read() == b"LINKED"


async def test_stream_rejects_bad_signature_expiry_and_foreign(client, lib):
    tid = lib["a1"]
    url = webapp.stream_url(TOKEN, "alice", tid)
    q = parse_qs(urlsplit(url).query)
    exp, sig = int(q["e"][0]), q["s"][0]
    base = f"/api/stream/{tid}"
    assert (await client.get(f"{base}?u=alice&e={exp}&s=deadbeef")).status == 404
    assert (await client.get(f"{base}?u=alice&e={exp + 3600}&s={sig}")).status == 404  # подменили срок
    assert (await client.get(f"{base}?u=bob&e={exp}&s={sig}")).status == 404
    assert (await client.get(f"{base}?u=alice&s={sig}")).status == 404
    assert (await client.get(f"{base}?u=alice&e=x&s={sig}")).status == 404
    assert (await client.get(f"{base}?u=alice&e={exp}&s=%D0%AF")).status == 404  # не-ASCII подпись
    old = int(time.time()) - 10
    assert (await client.get(f"{base}?u=alice&e={old}&s={webapp.stream_signature(TOKEN, 'alice', tid, old)}")
            ).status == 404
    # чужой трек с честной подписью: доступа нет
    assert (await client.get(webapp.stream_url(TOKEN, "alice", lib["b1"]))).status == 404
    assert (await client.get(webapp.stream_url(TOKEN, "ghost", tid))).status == 404
    assert (await client.get(webapp.stream_url(TOKEN, "alice", 99999))).status == 404
    assert (await client.get(webapp.stream_url("1:other", "alice", tid))).status == 404


async def test_stream_and_cover_via_shared_playlist(client, lib, db):
    pl = await make_pl(client, "Общий", tg=BOB, shared=True, track_ids=[lib["b1"]])
    stream = webapp.stream_url(TOKEN, "alice", lib["b1"])
    cover = webapp.cover_url(TOKEN, "alice", lib["b1"])
    assert (await client.get(stream)).status == 404
    assert (await client.get(cover)).status == 404
    await jpost(client, "/api/playlists/join", {"token": pl["invite"]["token"]})
    r = await client.get(stream)
    assert r.status == 200 and (await r.read()).startswith(b"ID3")  # файл общего кэша
    r = await client.get(cover)
    assert r.status == 200 and await r.read() == b"\xff\xd8\xff-" + "Вендетта".encode()
    # трек вне плейлиста по-прежнему недоступен
    assert (await client.get(webapp.stream_url(TOKEN, "alice", lib["b2"]))).status == 404
    await jpost(client, f"/api/playlists/{pl['id']}/leave")
    assert (await client.get(stream)).status == 404


# ------------------------------------------------------------------ /api/tracks и /api/library/play
async def test_tracks_refresh_links(client, lib):
    ids = f"{lib['a2']},{lib['b1']},{lib['a1']},999999,{lib['a2']}"
    items = (await jget(client, "/api/tracks", ids=ids))["items"]
    assert [i["id"] for i in items] == [lib["a2"], lib["a1"]]
    assert items[0]["stream"] and items[0]["owned"] is True
    assert (await jget(client, "/api/tracks", ids=""))["items"] == []
    await jget(client, "/api/tracks", status=400, ids="1,x")
    await jget(client, "/api/tracks", status=400, ids=",".join(str(i) for i in range(501)))
    assert len((await jget(client, "/api/tracks", ids=",".join([str(lib["a1"])] * 500)))["items"]) == 1


async def test_tracks_include_playlist_tracks_not_owned(client, lib):
    await shared_pl(client, lib, members=(BOB,))
    items = (await jget(client, "/api/tracks", tg=BOB, ids=f"{lib['a1']},{lib['a2']},{lib['b1']}"))["items"]
    assert [(i["id"], i["owned"]) for i in items] == [(lib["a1"], False), (lib["b1"], True)]
    assert items[0]["stream"].startswith(f"/api/stream/{lib['a1']}?u=bob&")


async def test_library_play_orders(client, lib, db):
    data = await jget(client, "/api/library/play", order="artist")
    assert data["total"] == 4 and len(data["items"]) == 4
    assert [i["id"] for i in data["items"]] == [lib["a4"], lib["a1"], lib["a2"], lib["a3"]]  # по папкам
    db._x("UPDATE user_tracks SET added_at=added_at+100 WHERE track_id=?", (lib["a3"],))
    data = await jget(client, "/api/library/play", order="recent", limit="2")
    assert data["total"] == 4 and [i["id"] for i in data["items"]][0] == lib["a3"] and len(data["items"]) == 2
    data = await jget(client, "/api/library/play", order="shuffle", limit="99999")
    assert sorted(i["id"] for i in data["items"]) == sorted([lib["a1"], lib["a2"], lib["a3"], lib["a4"]])
    assert all(i["stream"] and i["owned"] for i in data["items"])
    await jget(client, "/api/library/play", status=400, order="random")


# ------------------------------------------------------------------ плейлисты: создание и чтение
async def test_create_list_get(client, lib, db):
    pl = await make_pl(client, "  Дорога  ", description=" в путь ", track_ids=[lib["a1"], lib["a2"], lib["b1"]])
    assert pl["name"] == "Дорога" and pl["description"] == "в путь"
    assert pl["count"] == 2 and pl["seconds"] == 286 + 254  # чужой b1 не добавился
    assert pl["role"] == "owner" and pl["owner"] == "alice" and pl["shared"] is False and pl["members"] == []
    assert "invite" not in pl
    assert pl["covers"] == [webapp.cover_url(TOKEN, "alice", lib["a1"])]  # одна обложка на альбом
    client.svc.changed.clear()
    pl2 = await make_pl(client, "Второй")
    assert pl2["count"] == 0 and pl2["covers"] == []
    db._x("UPDATE playlists SET updated_at=updated_at-100 WHERE id=?", (pl["id"],))
    items = (await jget(client, "/api/playlists"))["items"]
    assert [i["id"] for i in items] == [pl2["id"], pl["id"]]
    assert (await jget(client, "/api/playlists", tg=BOB))["items"] == []

    full = await jget(client, f"/api/playlists/{pl['id']}")
    assert [t["id"] for t in full["tracks"]] == [lib["a1"], lib["a2"]]
    t = full["tracks"][0]
    assert t["added_by"] == "alice" and isinstance(t["item_id"], int) and t["owned"] is True
    assert t["stream"] and t["cover"] and t["seconds"] == 286


async def test_create_validation_and_limits(client, lib, monkeypatch):
    await jpost(client, "/api/playlists", {}, status=400)
    await jpost(client, "/api/playlists", {"name": "   "}, status=400)
    await jpost(client, "/api/playlists", {"name": "x" * 101}, status=400)
    await jpost(client, "/api/playlists", {"name": 5}, status=400)
    await jpost(client, "/api/playlists", {"name": "ok", "description": "y" * 301}, status=400)
    await jpost(client, "/api/playlists", {"name": "ok", "track_ids": "1"}, status=400)
    await jpost(client, "/api/playlists", {"name": "ok", "track_ids": [True]}, status=400)
    assert (await make_pl(client, "x" * 100))["name"] == "x" * 100
    monkeypatch.setattr(webapp, "PL_MAX_PER_USER", 2)
    await make_pl(client, "два")
    r = await jpost(client, "/api/playlists", {"name": "три"}, status=409)
    assert r["error"] == "limit" and r["message"]
    await make_pl(client, "у Боба", tg=BOB)  # лимит — на пользователя


async def test_create_shared_has_invite(client, lib):
    pl = await make_pl(client, shared=True)
    inv = pl["invite"]
    assert pl["shared"] is True and len(inv["token"]) == 12
    assert inv["link"] is None  # имя бота неизвестно
    assert inv["app_link"].endswith(f"/app/#join={inv['token']}")
    client.w.bot_username = "pimusic_bot"
    full = await jget(client, f"/api/playlists/{pl['id']}")
    assert full["invite"]["link"] == f"https://t.me/pimusic_bot?start=pl_{inv['token']}"


async def test_bad_ids_and_unknown_routes(client, lib):
    await jget(client, "/api/playlists/999", status=404)
    r = await client.get("/api/playlists/abc", headers=hdr())
    assert r.status == 404
    assert (await client.get("/api/playlists")).status == 401  # без initData


# ------------------------------------------------------------------ права: владелец / участник / посторонний
async def test_permission_matrix(client, lib):
    pl = await shared_pl(client, lib, members=(BOB,))
    pid = pl["id"]
    item = (await jget(client, f"/api/playlists/{pid}"))["tracks"][0]["item_id"]

    # посторонний (carol) — 404 на всё, существование не раскрываем
    for path, body in [("update", {"name": "x"}), ("share", {"enabled": False}), ("add", {"track_ids": [lib["c1"]]}),
                       ("remove", {"item_ids": [item]}), ("move", {"item_id": item, "to": 0}), ("delete", {}),
                       ("leave", {}), ("kick", {"login": "bob"}), ("save", {})]:
        r = await jpost(client, f"/api/playlists/{pid}/{path}", body, tg=CAROL, status=404)
        assert r["error"] == "not_found", path
    await jget(client, f"/api/playlists/{pid}", tg=CAROL, status=404)

    # участник — редактор, но не владелец
    full = await jget(client, f"/api/playlists/{pid}", tg=BOB)
    assert full["role"] == "member" and "invite" not in full and full["members"] == [{"login": "bob"}]
    assert (await jpost(client, f"/api/playlists/{pid}/update", {"description": "от Боба"}, tg=BOB)
            )["description"] == "от Боба"
    r = await jpost(client, f"/api/playlists/{pid}/add", {"track_ids": [lib["b1"], lib["a2"]]}, tg=BOB)
    assert r == {"added": 1, "skipped": 1, "count": 2}  # a2 alice бобу недоступен
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": item, "to": 1}, tg=BOB)
    for path, body in [("share", {"enabled": False}), ("delete", {}), ("kick", {"login": "bob"})]:
        r = await jpost(client, f"/api/playlists/{pid}/{path}", body, tg=BOB, status=403)
        assert r["error"] == "forbidden" and r["message"]
    full = await jget(client, f"/api/playlists/{pid}")
    assert [(t["id"], t["added_by"]) for t in full["tracks"]] == [(lib["b1"], "bob"), (lib["a1"], "alice")]
    assert [(t["id"], t["owned"]) for t in full["tracks"]] == [(lib["b1"], False), (lib["a1"], True)]

    # владелец не может «выйти»
    r = await jpost(client, f"/api/playlists/{pid}/leave", status=409)
    assert r["error"] == "owner_cannot_leave"
    # участник может удалить трек
    bob_item = full["tracks"][0]["item_id"]
    assert await jpost(client, f"/api/playlists/{pid}/remove", {"item_ids": [bob_item, 424242]}, tg=BOB) == {
        "removed": 1, "count": 1}
    # участник видит плейлист в своём списке
    mine = (await jget(client, "/api/playlists", tg=BOB))["items"]
    assert [(p["id"], p["role"], p["owner"]) for p in mine] == [(pid, "member", "alice")]


async def test_update_validation(client, lib):
    pl = await make_pl(client)
    await jpost(client, f"/api/playlists/{pl['id']}/update", {"name": ""}, status=400)
    await jpost(client, f"/api/playlists/{pl['id']}/update", {"description": "z" * 301}, status=400)
    r = await jpost(client, f"/api/playlists/{pl['id']}/update", {"name": " Новое "})
    assert r["name"] == "Новое" and r["description"] == ""


# ------------------------------------------------------------------ общий доступ
async def test_share_enable_regenerate_disable(client, lib):
    pl = await make_pl(client)
    pid = pl["id"]
    await jpost(client, f"/api/playlists/{pid}/share", {}, status=400)
    r = await jpost(client, f"/api/playlists/{pid}/share", {"enabled": True})
    assert r["shared"] is True and r["invite"]["token"]
    t1 = r["invite"]["token"]
    r = await jpost(client, f"/api/playlists/{pid}/share", {"enabled": True})
    assert r["invite"]["token"] == t1  # без regenerate токен прежний
    await jpost(client, "/api/playlists/join", {"token": t1}, tg=BOB)

    r = await jpost(client, f"/api/playlists/{pid}/share", {"enabled": True, "regenerate": True})
    t2 = r["invite"]["token"]
    assert t2 != t1
    await jget(client, "/api/playlists/join", tg=CAROL, status=404, token=t1)
    await jpost(client, "/api/playlists/join", {"token": t1}, tg=CAROL, status=404)
    assert (await jget(client, "/api/playlists/join", tg=CAROL, token=t2))["already"] is False

    r = await jpost(client, f"/api/playlists/{pid}/share", {"enabled": False})
    assert r == {"shared": False, "invite": None}
    await jget(client, "/api/playlists/join", tg=CAROL, status=404, token=t2)
    # участники остаются
    assert (await jget(client, f"/api/playlists/{pid}", tg=BOB))["role"] == "member"
    full = await jget(client, f"/api/playlists/{pid}")
    assert full["invite_enabled"] is False and "invite" not in full
    assert full["shared"] is True  # участники есть — плейлист по-прежнему совместный


async def test_join_preview_and_join(client, lib, monkeypatch):
    pl = await make_pl(client, "Вечеринка", shared=True, track_ids=[lib["a1"], lib["a3"]])
    token = pl["invite"]["token"]
    prev = await jget(client, "/api/playlists/join", tg=BOB, token=token)
    assert prev == {"id": pl["id"], "name": "Вечеринка", "owner": "alice", "count": 2,
                    "covers": [webapp.cover_url(TOKEN, "alice", lib["a1"])], "already": False}
    r = await client.get(prev["covers"][0])  # обложки превью подписаны владельцем — открываются
    assert r.status == 200
    assert (await jget(client, "/api/playlists/join", token=token))["already"] is True  # владелец
    await jget(client, "/api/playlists/join", tg=BOB, status=404, token="nope")
    await jget(client, "/api/playlists/join", tg=BOB, status=404)
    await jpost(client, "/api/playlists/join", {"token": None}, tg=BOB, status=404)

    j = await jpost(client, "/api/playlists/join", {"token": token}, tg=BOB)
    assert j["role"] == "member" and j["members"] == [{"login": "bob"}] and "invite" not in j
    assert pl["id"] in [c[0] for c in client.svc.changed]
    again = await jpost(client, "/api/playlists/join", {"token": token}, tg=BOB)  # повтор — без дублей
    assert again["members"] == [{"login": "bob"}]
    assert (await jget(client, "/api/playlists/join", tg=BOB, token=token))["already"] is True
    own = await jpost(client, "/api/playlists/join", {"token": token})
    assert own["role"] == "owner" and own["invite"]["token"] == token

    monkeypatch.setattr(webapp, "PL_MAX_MEMBERS", 1)
    r = await jpost(client, "/api/playlists/join", {"token": token}, tg=CAROL, status=409)
    assert r["error"] == "members_limit"


async def test_leave_and_kick(client, lib):
    pl = await shared_pl(client, lib, members=(BOB, CAROL))
    pid = pl["id"]
    assert [m["login"] for m in (await jget(client, f"/api/playlists/{pid}"))["members"]] == ["bob", "carol"]
    client.svc.changed.clear()
    assert await jpost(client, f"/api/playlists/{pid}/leave", tg=CAROL) == {"ok": True}
    assert client.svc.changed == [(pid, 1)]
    await jget(client, f"/api/playlists/{pid}", tg=CAROL, status=404)
    await jpost(client, f"/api/playlists/{pid}/kick", {"login": "carol"}, status=404)
    await jpost(client, f"/api/playlists/{pid}/kick", {}, status=404)
    r = await jpost(client, f"/api/playlists/{pid}/kick", {"login": "bob"})
    assert r["members"] == [] and r["role"] == "owner"
    await jget(client, f"/api/playlists/{pid}", tg=BOB, status=404)
    assert (await jget(client, "/api/playlists", tg=BOB))["items"] == []


async def test_delete_playlist(client, lib, db, cfg):
    pl = await shared_pl(client, lib, members=(BOB,))
    assert await jpost(client, f"/api/playlists/{pl['id']}/delete") == {"ok": True}
    assert db.get_playlist(pl["id"]) is None
    assert db._q("SELECT count(*) c FROM playlist_members")[0]["c"] == 0
    assert db._q("SELECT count(*) c FROM playlist_items")[0]["c"] == 0
    await jget(client, f"/api/playlists/{pl['id']}", tg=BOB, status=404)
    assert db.get_track(lib["a1"]) is not None  # трек в медиатеке alice — не трогаем
    assert (cfg.library_dir / "Кино" / "Группа крови" / "01 Группа крови.mp3").exists()


# ------------------------------------------------------------------ добавление, удаление, порядок
async def test_add_album_artist_and_limit(client, lib, monkeypatch):
    pl = await make_pl(client)
    pid = pl["id"]
    r = await jpost(client, f"/api/playlists/{pid}/add", {"album": {"a": "Кино", "b": "Группа крови"}})
    assert r == {"added": 2, "skipped": 0, "count": 2}
    r = await jpost(client, f"/api/playlists/{pid}/add", {"artist": "Кино"})
    assert r == {"added": 1, "skipped": 2, "count": 3}
    tracks = (await jget(client, f"/api/playlists/{pid}"))["tracks"]
    assert [t["id"] for t in tracks] == [lib["a1"], lib["a2"], lib["a3"]]
    # чужой исполнитель/альбом — из своей медиатеки ничего
    assert (await jpost(client, f"/api/playlists/{pid}/add", {"artist": "Земфира"}))["added"] == 0
    await jpost(client, f"/api/playlists/{pid}/add", {"album": {"a": "Кино"}}, status=400)
    await jpost(client, f"/api/playlists/{pid}/add", {"artist": ""}, status=400)
    await jpost(client, f"/api/playlists/{pid}/add", {}, status=400)
    await jpost(client, f"/api/playlists/{pid}/add", {"track_ids": ["x"]}, status=400)
    monkeypatch.setattr(webapp, "PL_MAX_TRACKS", 4)
    r = await jpost(client, f"/api/playlists/{pid}/add", {"track_ids": [lib["a4"], lib["a4"]]})
    assert r["added"] == 1 and r["count"] == 4 and r["full"] is True
    pl2 = await make_pl(client, "Второй", track_ids=[lib["a1"]])
    r = await jpost(client, f"/api/playlists/{pl2['id']}/add", {"artist": "Кино"})
    assert r["added"] == 2 and r["count"] == 3


async def test_move_ordering_and_renormalize(client, lib, db):
    pl = await make_pl(client, track_ids=[lib["a1"], lib["a2"], lib["a3"], lib["a4"]])
    pid = pl["id"]

    async def order():
        return [t["id"] for t in (await jget(client, f"/api/playlists/{pid}"))["tracks"]]

    items = {t["id"]: t["item_id"] for t in (await jget(client, f"/api/playlists/{pid}"))["tracks"]}
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": items[lib["a4"]], "to": 0})
    assert await order() == [lib["a4"], lib["a1"], lib["a2"], lib["a3"]]
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": items[lib["a4"]], "to": 99})
    assert await order() == [lib["a1"], lib["a2"], lib["a3"], lib["a4"]]
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": items[lib["a1"]], "to": 2})
    assert await order() == [lib["a2"], lib["a3"], lib["a1"], lib["a4"]]
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": items[lib["a1"]], "to": -5})
    assert await order() == [lib["a1"], lib["a2"], lib["a3"], lib["a4"]]
    # многократная вставка между одними и теми же соседями: зазор схлопывается -> перенумерация
    expected = [lib["a1"], lib["a2"], lib["a3"], lib["a4"]]
    for i in range(80):
        moving = expected[-1] if i % 2 == 0 else expected[-2]
        await jpost(client, f"/api/playlists/{pid}/move", {"item_id": items[moving], "to": 1})
        expected.remove(moving)
        expected.insert(1, moving)
    assert await order() == expected
    pos = [r["position"] for r in db._q("SELECT position FROM playlist_items WHERE playlist_id=? ORDER BY position",
                                        (pid,))]
    assert len(set(pos)) == 4 and min(b - a for a, b in zip(pos, pos[1:])) > 1e-6
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": 424242, "to": 0}, status=404)
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": items[lib["a1"]]}, status=400)
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": True, "to": 0}, status=400)
    # чужая позиция (из другого плейлиста) не двигается
    other = await make_pl(client, "Другой", track_ids=[lib["a1"]])
    oid = (await jget(client, f"/api/playlists/{other['id']}"))["tracks"][0]["item_id"]
    await jpost(client, f"/api/playlists/{pid}/move", {"item_id": oid, "to": 0}, status=404)


async def test_for_track(client, lib):
    p1 = await make_pl(client, "Один", track_ids=[lib["a1"]])
    p2 = await make_pl(client, "Два")
    shared = await make_pl(client, "Боба", tg=BOB, shared=True)
    await jpost(client, "/api/playlists/join", {"token": shared["invite"]["token"]})
    items = (await jget(client, "/api/playlists/for-track", track_id=str(lib["a1"])))["items"]
    assert {(i["id"], i["name"], i["has"]) for i in items} == {
        (p1["id"], "Один", True), (p2["id"], "Два", False), (shared["id"], "Боба", False)}
    await jget(client, "/api/playlists/for-track", status=400, track_id="x")
    assert (await jget(client, "/api/playlists/for-track", tg=CAROL, track_id=str(lib["a1"])))["items"] == []


# ------------------------------------------------------------------ сохранить в медиатеку и общий кэш
async def test_save_to_library_links_missing(client, lib, db, cfg):
    pl = await make_pl(client, "Боба", tg=BOB, shared=True, track_ids=[lib["b1"], lib["b2"]])
    await jpost(client, "/api/playlists/join", {"token": pl["invite"]["token"]})
    await jpost(client, f"/api/playlists/{pl['id']}/add", {"track_ids": [lib["a1"]]})
    r = await jpost(client, f"/api/playlists/{pl['id']}/save")
    assert r == {"linked": 2, "missing": 0, "failed": 0}
    link = cfg.users_dir / "alice" / "Земфира" / "Вендетта" / "01 Дышать.mp3"
    assert link.is_file() and (link.parent / "cover.jpg").is_file()
    assert db.user_has_track("alice", lib["b1"]) == str(link)
    assert db.user_has_track("alice", lib["b2"])
    assert client.svc.lib_changed == ["alice"]
    assert (await jpost(client, f"/api/playlists/{pl['id']}/save")) == {"linked": 0, "missing": 0, "failed": 0}
    full = await jget(client, f"/api/playlists/{pl['id']}")
    assert all(t["owned"] for t in full["tracks"])
    # файл общего кэша пропал — пропускаем, не падаем
    pl2 = await make_pl(client, "c", tg=CAROL, shared=True, track_ids=[lib["c1"]])
    await jpost(client, "/api/playlists/join", {"token": pl2["invite"]["token"]})
    db._x("UPDATE tracks SET path=? WHERE id=?", (str(cfg.library_dir / "nope.mp3"), lib["c1"]))
    assert (await jpost(client, f"/api/playlists/{pl2['id']}/save")) == {"linked": 0, "missing": 1, "failed": 0}


def test_unlink_purges_cache_file_even_in_playlist(db, cfg, users, lib):
    # плейлист файл не держит: трека больше нет ни в одной медиатеке — файл с диска, строка из плейлиста
    track = db.get_track(lib["b1"])
    pid = db.create_playlist("carol", "Чужой")
    db.add_playlist_tracks(pid, [track.id], "bob", 5000)
    library.unlink_from_user(db, track, "bob")
    assert db.user_has_track("bob", track.id) is None
    assert db.get_track(track.id) is None and not Path(track.path).exists()
    assert db.playlist_track_ids(pid) == []


def test_unlink_keeps_cache_file_while_in_other_library(db, cfg, users, lib):
    track = db.get_track(lib["b1"])
    library.attach_to_user(db, track, "carol")
    library.unlink_from_user(db, track, "bob")
    assert db.get_track(track.id) is not None and Path(track.path).exists()  # нужен медиатеке carol
    library.unlink_from_user(db, track, "carol")
    assert db.get_track(track.id) is None and not Path(track.path).exists()


async def test_service_delete_track_purges_playlist_track(db, cfg, users, lib):
    svc = Service(db, MagicMock())
    svc._schedule_refresh = MagicMock()
    svc.playlist_changed = MagicMock()
    pid = db.create_playlist("alice", "Мой")
    db.add_playlist_tracks(pid, [lib["a1"]], "alice", 5000)
    path = db.get_track(lib["a1"]).path
    text = await svc.delete_track(users["alice"], lib["a1"])
    assert "Удалено" in text
    assert db.user_has_track("alice", lib["a1"]) is None
    assert db.playlist_track_ids(pid) == [] and not Path(path).exists()
    svc.playlist_changed.assert_called_once_with(pid)  # зеркало плейлиста пересобирается (id собран до удаления)


async def test_remove_from_playlist_purges_orphan(client, lib, db, cfg):
    # трек, которого нет ни в одной медиатеке (например, медиатеку вычистил reconcile), уходит с последним плейлистом
    pl = await make_pl(client, track_ids=[lib["a1"]])
    track = db.get_track(lib["a1"])
    db.unlink_track("alice", track.id)
    items = (await jget(client, f"/api/playlists/{pl['id']}"))["tracks"]
    assert items[0]["owned"] is False and items[0]["stream"]
    await jpost(client, f"/api/playlists/{pl['id']}/remove", {"item_ids": [items[0]["item_id"]]})
    assert db.get_track(lib["a1"]) is None
    assert not Path(track.path).exists()


async def test_delete_playlist_purges_orphans(client, lib, db):
    pl = await make_pl(client, track_ids=[lib["a1"], lib["a2"]])
    t1 = db.get_track(lib["a1"])
    library.unlink_from_user(db, t1, "alice")
    await jpost(client, f"/api/playlists/{pl['id']}/delete")
    assert db.get_track(lib["a1"]) is None and db.get_track(lib["a2"]) is not None


# ------------------------------------------------------------------ БД: kv и каскады
def test_kv_items_exact_prefix(db):
    db.kv_set("pl_jf:1:alice", "A")
    db.kv_set("pl_jf:10:alice", "B")
    db.kv_set("plXjf:1:alice", "C")
    assert db.kv_items("pl_jf:1:") == {"pl_jf:1:alice": "A"}
    assert set(db.kv_items("pl_jf:").values()) == {"A", "B"}
    db.kv_delete("pl_jf:1:alice")
    assert db.kv_items("pl_jf:1:") == {}


def test_delete_user_cascades_playlists(db, users, lib):
    pid = db.create_playlist("bob", "Боба")
    db.add_playlist_member(pid, "alice")
    other = db.create_playlist("alice", "alice")
    db.add_playlist_member(other, "bob")
    db.delete_user("bob")
    assert db.get_playlist(pid) is None
    assert db.playlist_members(other) == []


# ------------------------------------------------------------------ зеркало в Jellyfin
class FakeJF:
    def __init__(self, items_by_user):
        self.items_by_user = items_by_user
        self.created: list[tuple] = []
        self.deleted: list[str] = []
        self.fail = False
        self.fail_delete = False
        self.n = 0
        self.reads = 0
        self.active = self.max_active = 0

    async def audio_items(self, user_id, **_):
        self.reads += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.fail:
                raise RuntimeError("jellyfin down")
            return self.items_by_user.get(user_id, [])
        finally:
            self.active -= 1

    async def create_playlist(self, user_id, name, ids):
        self.n += 1
        self.created.append((user_id, name, list(ids)))
        return f"jf-pl-{self.n}"

    async def create_playlist_full(self, user_id, name, ids):
        return await self.create_playlist(user_id, name, ids)

    async def request(self, method, path, **_):
        assert method == "DELETE"
        if self.fail_delete:
            from app.jellyfin import JellyfinError
            raise JellyfinError(500, "nope")
        self.deleted.append(path.rsplit("/", 1)[1])


@pytest.fixture
def mirror(db, cfg, users, lib):
    link = lambda login, tid: db.user_has_track(login, tid)  # noqa: E731
    jf = FakeJF({
        "jf-alice": [{"Id": "ja1", "Path": link("alice", lib["a1"])}, {"Id": "ja2", "Path": link("alice", lib["a2"])}],
        "jf-bob": [{"Id": "jb1", "Path": link("bob", lib["b1"])}],
        "jf-carol": [],
    })
    svc = Service(db, jf)
    pid = db.create_playlist("alice", "Моя Волна лета", share_token="tok")
    db.add_playlist_tracks(pid, [lib["a2"], lib["b1"], lib["a1"]], "alice", 5000)
    db.add_playlist_member(pid, "bob")
    db.add_playlist_member(pid, "carol")
    return svc, jf, pid


async def test_jellyfin_mirror_sync(db, mirror):
    svc, jf, pid = mirror
    assert await svc.sync_playlist_mirror(pid) is True
    # у каждого — только его треки, в порядке плейлиста; у carol своих нет — зеркала нет
    assert sorted(jf.created) == [("jf-alice", "Моя Волна лета", ["ja2", "ja1"]),
                                  ("jf-bob", "Моя Волна лета", ["jb1"])]
    stored = db.kv_items(f"pl_jf:{pid}:")
    assert set(stored) == {f"pl_jf:{pid}:alice", f"pl_jf:{pid}:bob"}
    assert set(stored.values()) <= svc.own_playlist_ids()  # зеркала — «свои» для очистки «Моя Волна…»

    # ничего не менялось — каталог Jellyfin не читается, зеркала не пересоздаются
    jf.created.clear()
    reads = jf.reads
    assert await svc.sync_playlist_mirror(pid) is True
    assert jf.created == [] and jf.reads == reads

    # переименование — пересоздание, старое удаляется
    db.update_playlist(pid, name="Лето")
    await svc.sync_playlist_mirror(pid)
    assert {c[1] for c in jf.created} == {"Лето"} and set(stored.values()) <= set(jf.deleted)

    bob_id = db.kv_get(f"pl_jf:{pid}:bob")
    db.remove_playlist_member(pid, "bob")
    await svc.sync_playlist_mirror(pid)
    assert bob_id in jf.deleted and db.kv_get(f"pl_jf:{pid}:bob") is None
    assert db.kv_get(f"pl_hash:{pid}:bob") is None

    alice_id = db.kv_get(f"pl_jf:{pid}:alice")
    db.delete_playlist(pid)
    await svc.sync_playlist_mirror(pid)
    assert alice_id in jf.deleted and db.kv_items(f"pl_jf:{pid}:") == {} and db.kv_items(f"pl_hash:{pid}:") == {}


async def test_mirror_kept_while_jellyfin_scans(db, mirror, caplog):
    svc, jf, pid = mirror
    await svc.sync_playlist_mirror(pid)
    alice_id = db.kv_get(f"pl_jf:{pid}:alice")
    jf.items_by_user["jf-alice"] = []  # Jellyfin пересканирует и пока не видит файлов
    svc._jf_paths_cache.clear()
    db.update_playlist(pid, name="Новое")
    svc.playlist_changed(pid, 999)  # отметка «грязный»
    assert await svc.sync_playlist_mirror(pid) is False
    assert db.kv_get(f"pl_jf:{pid}:alice") == alice_id and alice_id not in jf.deleted
    assert db.kv_get(f"pl_dirty:{pid}") == "1"  # досинхронизируем позже
    assert "оставляю как есть" in caplog.text


async def test_mirror_partial_resolution_not_marked_done(db, mirror, lib):
    svc, jf, pid = mirror
    jf.items_by_user["jf-alice"] = jf.items_by_user["jf-alice"][:1]  # ja1 есть, ja2 ещё нет
    assert await svc.sync_playlist_mirror(pid) is False
    assert ("jf-alice", "Моя Волна лета", ["ja1"]) in jf.created
    assert db.kv_get(f"pl_hash:{pid}:alice") is None


async def test_mirror_trash_retry(db, mirror):
    svc, jf, pid = mirror
    await svc.sync_playlist_mirror(pid)
    old = db.kv_get(f"pl_jf:{pid}:alice")
    jf.fail_delete = True
    db.update_playlist(pid, name="Другое")
    await svc.sync_playlist_mirror(pid)
    assert db.kv_get(f"pl_jf_trash:{old}") == "1" and old not in jf.deleted
    jf.fail_delete = False
    await svc.sync_playlist_mirror(pid)  # следующая синхронизация разбирает корзину
    assert old in jf.deleted and db.kv_items("pl_jf_trash:") == {}


async def test_mirror_syncs_serialised_and_paths_cached(db, mirror, lib):
    svc, jf, pid = mirror
    pid2 = db.create_playlist("alice", "Второй")
    db.add_playlist_tracks(pid2, [lib["a1"]], "alice", 5000)
    await asyncio.gather(svc.sync_playlist_mirror(pid), svc.sync_playlist_mirror(pid2))
    assert jf.max_active == 1  # по одной
    assert jf.reads == 2  # alice и bob; второй плейлист alice — из кэша путей


async def test_mirror_failure_only_logs(db, cfg, users, lib, caplog):
    jf = FakeJF({})
    jf.fail = True
    svc = Service(db, jf)
    pid = db.create_playlist("alice", "x")
    db.add_playlist_tracks(pid, [lib["a1"]], "alice", 5000)
    assert await svc.sync_playlist_mirror(pid) is False
    rec = next(r for r in caplog.records if "зеркало плейлиста" in r.getMessage())
    assert rec.exc_info  # не сетевая ошибка — с трассировкой
    assert db.kv_items(f"pl_jf:{pid}:") == {}


async def test_playlist_changed_debounced(db, cfg, users):
    svc = Service(db, FakeJF({}))
    calls = []

    async def fake_sync(pid):
        calls.append(pid)
        return True
    svc.sync_playlist_mirror = fake_sync
    for _ in range(5):
        svc.playlist_changed(7, 0.05)
    svc.playlist_changed(8, 0.05)
    await asyncio.sleep(0.2)
    assert sorted(calls) == [7, 8]
    svc.playlist_changed(7, 0.01)
    await asyncio.sleep(0.1)
    assert calls.count(7) == 2
    # обычная правка срок не сокращает, urgent — сокращает
    svc.playlist_changed(9, 30)
    svc.playlist_changed(9, 0.01)
    assert svc._pl_due[9] - time.monotonic() > 20
    svc.playlist_changed(9, 0.01, urgent=True)
    await asyncio.sleep(0.1)
    assert 9 in calls


async def test_dirty_playlists_persist_and_resume(db, cfg, users):
    svc = Service(db, FakeJF({}))
    svc.playlist_changed(5, 999)
    svc.playlist_changed(6, 999)
    assert set(db.kv_items("pl_dirty:")) == {"pl_dirty:5", "pl_dirty:6"}
    for t in list(svc._pl_tasks.values()):
        t.cancel()
    # «перезапуск»: новый сервис подхватывает отметки
    svc2 = Service(db, FakeJF({}))
    db.kv_set("pl_dirty:junk", "1")
    assert svc2._resume_dirty_playlists(999) == 2
    assert set(svc2._pl_due) == {5, 6} and db.kv_get("pl_dirty:junk") is None
    for t in list(svc2._pl_tasks.values()):
        t.cancel()
    # успешная синхронизация снимает отметку (плейлиста 5 нет — зеркал нет, всё чисто)
    assert await svc2.sync_playlist_mirror(5) is True
    assert db.kv_get("pl_dirty:5") is None


async def test_delete_track_reschedules_playlists_with_it(db, cfg, users, lib):
    svc = Service(db, MagicMock())
    svc._schedule_refresh = MagicMock()
    svc.playlist_changed = MagicMock()
    pid = db.create_playlist("bob", "Боба")
    db.add_playlist_tracks(pid, [lib["a1"]], "alice", 5000)
    await svc.delete_track(users["alice"], lib["a1"])
    svc.playlist_changed.assert_called_once_with(pid)


def _jf_for_delete(playlists):
    jf = MagicMock()
    jf.deleted = []

    async def none(*a, **k):
        return []

    async def pls(_uid):
        return playlists

    async def req(method, path, **_):
        jf.deleted.append(path.rsplit("/", 1)[1])
    jf.delete_library = none
    jf.user_playlists = pls
    jf.delete_user = none
    jf.request = req
    return jf


async def test_delete_account_cleans_owned_playlists(db, cfg, users, lib):
    svc = Service(db, _jf_for_delete([{"Id": "x1"}]))
    svc.playlist_changed = MagicMock()
    pid = db.create_playlist("bob", "Боба")
    db.add_playlist_tracks(pid, [lib["b1"]], "bob", 5000)
    db.add_playlist_member(pid, "alice")
    path = db.get_track(lib["b1"]).path
    await svc.delete_account(users["bob"])
    assert db.get_playlist(pid) is None
    assert db.get_track(lib["b1"]) is None and not Path(path).exists()
    svc.playlist_changed.assert_any_call(pid, 1, urgent=True)
    assert svc.jf.deleted == ["x1"]  # обычный пользователь: все его плейлисты Jellyfin удаляются


async def test_delete_account_admin_keeps_manual_jellyfin_playlists(db, cfg, users, lib):
    admin = User("boss", 9001, "jf-boss", "lib-boss", "stb", True)
    db.upsert_user(admin)
    db.kv_set("pl_jf:3:boss", "aa-11")
    db.kv_set("pl_hash:3:boss", "h")
    db.kv_set("wave_playlist:boss", "bb22")
    db.kv_set("pl_jf:3:alice", "cc33")
    svc = Service(db, _jf_for_delete([{"Id": "aa11"}, {"Id": "bb-22"}, {"Id": "manual"}]))
    await svc.delete_account(admin)
    assert sorted(svc.jf.deleted) == ["aa11", "bb-22"]  # ручной плейлист админа остался
    assert db.kv_get("pl_jf:3:boss") is None and db.kv_get("pl_hash:3:boss") is None
    assert db.kv_get("wave_playlist:boss") is None and db.kv_get("pl_jf:3:alice") == "cc33"


async def test_safe_logs_traceback_for_bugs_only(db, caplog):
    from app.jellyfin import JellyfinError
    svc = Service(db, None)

    async def bug():
        raise KeyError("x")

    async def net():
        raise JellyfinError(502, "down")
    await svc._safe(bug())
    await svc._safe(net())
    recs = [r for r in caplog.records if "background task failed" in r.getMessage()]
    assert [bool(r.exc_info) for r in recs] == [True, False]


# ------------------------------------------------------------------ транзакции и гонки
def test_tx_rolls_back(db, users):
    pid = db.create_playlist("alice", "x")
    with pytest.raises(RuntimeError):
        with db._tx() as c:
            c.execute("UPDATE playlists SET name='y' WHERE id=?", (pid,))
            with db._tx():  # вложенная — часть внешней
                c.execute("UPDATE playlists SET description='z' WHERE id=?", (pid,))
            raise RuntimeError
    pl = db.get_playlist(pid)
    assert pl["name"] == "x" and pl["description"] == ""
    assert not db._conn.in_transaction


def test_limits_hold_under_concurrency(db, users):
    import threading
    from app.db import PlaylistLimit
    made, refused = [], []

    def worker(i):
        try:
            made.append(db.create_playlist("alice", f"p{i}", max_per_user=3))
        except PlaylistLimit:
            refused.append(i)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(made) == 3 and len(refused) == 9
    pid = made[0]
    joined = []
    for login in ("bob", "carol"):
        try:
            joined.append(db.add_playlist_member(pid, login, max_members=1))
        except PlaylistLimit:
            joined.append("limit")
    assert joined == [True, "limit"]


def test_add_tracks_skips_vanished_track(db, users, lib):
    pid = db.create_playlist("alice", "x")
    assert db.add_playlist_tracks(pid, [lib["a1"], 987654, lib["a2"]], "alice", 5000) == (2, 1)
    assert db.playlist_track_ids(pid) == [lib["a1"], lib["a2"]]
    assert db.add_playlist_tracks(424242, [lib["a1"]], "alice", 5000) == (0, 1)  # плейлист удалили
    assert db.create_playlist("alice", "y", track_ids=[lib["a3"], 987654]) > 0


def test_purge_only_when_row_deleted_and_keeps_foreign_files(db, cfg, users, lib):
    t = db.get_track(lib["a3"])  # «Звезда», один трек в папке альбома
    album = Path(t.path).parent
    (album / "notes.txt").write_text("чужой файл")
    Path(t.path).with_suffix(".lrc").write_text("[00:00]")
    pid = db.create_playlist("bob", "x")
    db.add_playlist_tracks(pid, [t.id], "bob", 5000)
    db.link_track("bob", t.id, str(cfg.users_dir / "bob" / "x.mp3"))
    db.unlink_track("alice", t.id)
    assert library.purge_if_unused(db, t) is False and Path(t.path).exists()  # нужен медиатеке bob
    db.unlink_track("bob", t.id)
    assert library.purge_if_unused(db, t) is True and db.playlist_track_ids(pid) == []  # плейлист не держит
    assert not Path(t.path).exists() and not Path(t.path).with_suffix(".lrc").exists()
    assert (album / "notes.txt").exists()  # rmtree больше нет: посторонние файлы целы
    assert library.purge_if_unused(db, t) is False  # строку уже удалили — файлы не трогаем повторно


def test_upsert_user_keeps_playlists(db, users):
    pid = db.create_playlist("alice", "Мой")
    db.add_playlist_member(pid, "bob")
    db.upsert_user(User("alice", ALICE, "jf-alice2", "lib2", "new-token", False))
    db.upsert_user(User("bob", BOB, "jf-bob", "lib-bob", "stbob", True))
    assert db.get_playlist(pid)["owner"] == "alice" and db.playlist_members(pid) == ["bob"]


# ------------------------------------------------------------------ event loop, обложки, ввод
async def test_heavy_handlers_off_event_loop_and_cover_memo(client, lib, monkeypatch):
    import threading
    pl = await make_pl(client, track_ids=[lib["a1"], lib["a2"], lib["a3"]])
    calls = []
    real = webapp._cover_path

    def spy(*a):
        calls.append((threading.get_ident(), a))
        return real(*a)
    monkeypatch.setattr(webapp, "_cover_path", spy)
    for path in ("/api/playlists", f"/api/playlists/{pl['id']}", f"/api/tracks?ids={lib['a1']},{lib['a2']}",
                 "/api/library/play?order=artist", "/api/library"):
        calls.clear()
        r = await client.get(path, headers=hdr())
        assert r.status == 200, path
        assert calls and threading.get_ident() not in {c[0] for c in calls}, path
        # a1 и a2 из одной папки альбома — обложка ищется один раз
        dirs = [str(Path(c[1][0]).parent) for c in calls]
        assert len(dirs) == len(set(dirs)), path


async def test_huge_and_bad_ids_are_4xx(client, lib):
    huge = str(2 ** 70)
    await jget(client, "/api/tracks", status=400, ids=huge)
    await jget(client, "/api/tracks", status=400, ids="0")
    await jget(client, "/api/playlists/for-track", status=400, track_id=huge)
    await jget(client, "/api/playlists/for-track", status=400, track_id="-1")
    await jget(client, f"/api/playlists/{huge}", status=404)
    pl = await make_pl(client, track_ids=[lib["a1"], lib["a2"]])
    await jpost(client, f"/api/playlists/{pl['id']}/add", {"track_ids": [2 ** 70]}, status=400)
    await jpost(client, f"/api/playlists/{pl['id']}/add", {"track_ids": [1.5]}, status=400)
    await jpost(client, f"/api/playlists/{pl['id']}/remove", {"item_ids": [-3]}, status=400)
    await jpost(client, "/api/playlists", {"name": "x", "track_ids": [2 ** 64]}, status=400)
    items = (await jget(client, f"/api/playlists/{pl['id']}"))["tracks"]
    await jpost(client, f"/api/playlists/{pl['id']}/move", {"item_id": items[1]["item_id"], "to": -(2 ** 80)})
    assert [t["id"] for t in (await jget(client, f"/api/playlists/{pl['id']}"))["tracks"]] == [lib["a2"], lib["a1"]]
    await jpost(client, f"/api/playlists/{pl['id']}/move", {"item_id": items[1]["item_id"], "to": 1e400}, status=400)
    await jpost(client, f"/api/playlists/{pl['id']}/move", {"item_id": 2 ** 70, "to": 0}, status=400)
    assert (await client.get(f"/api/stream/{huge}?u=alice&e=9999999999&s=x")).status == 404
    assert (await client.get(f"/api/cover/{huge}?u=alice&s=x")).status == 404


async def test_kick_rotates_invite(client, lib):
    pl = await shared_pl(client, lib, members=(BOB,))
    old = pl["invite"]["token"]
    r = await jpost(client, f"/api/playlists/{pl['id']}/kick", {"login": "bob"})
    assert r["invite"]["token"] != old and r["invite_enabled"] is True
    await jpost(client, "/api/playlists/join", {"token": old}, tg=BOB, status=404)  # по старой не вернуться
    await jget(client, "/api/playlists/join", tg=BOB, status=404, token=old)
    # личный плейлист: ссылки не было и не появляется
    priv = await make_pl(client, "Личный")
    client.db.add_playlist_member(priv["id"], "carol")
    r = await jpost(client, f"/api/playlists/{priv['id']}/kick", {"login": "carol"})
    assert "invite" not in r and r["invite_enabled"] is False and r["shared"] is False


async def test_save_counts_failed_and_still_refreshes(client, lib, monkeypatch):
    pl = await make_pl(client, "Боба", tg=BOB, shared=True, track_ids=[lib["b1"], lib["b2"]])
    await jpost(client, "/api/playlists/join", {"token": pl["invite"]["token"]})
    real = library.give_cached
    n = []

    def flaky(db, t, login):
        n.append(t.id)
        if len(n) == 2:
            raise OSError("диск")
        return real(db, t, login)
    monkeypatch.setattr(library, "give_cached", flaky)
    assert await jpost(client, f"/api/playlists/{pl['id']}/save") == {"linked": 1, "missing": 0, "failed": 1}
    assert client.svc.lib_changed == ["alice"]


async def test_stream_content_type_by_extension(client, db, cfg, users):
    d = cfg.library_dir / "X" / "Y"
    d.mkdir(parents=True)
    (d / "t.flac").write_bytes(b"fLaC" + b"\0" * 64)
    tid = db.add_track(match_key="x|t", mbid=None, isrc=None, artist="X", title="t", album="Y", duration=1,
                       path=str(d / "t.flac"), source="test")
    db.link_track("alice", tid, str(cfg.users_dir / "alice" / "X" / "Y" / "t.flac"))
    r = await client.get(webapp.stream_url(TOKEN, "alice", tid))
    assert r.status == 200 and r.headers["Content-Type"] != "audio/mpeg"


# ------------------------------------------------------------------ бот: /start pl_<token>, /playlists
@pytest.fixture
def https(monkeypatch):
    monkeypatch.setenv("STREAM_PUBLIC_URL", "https://music.test")


def _button(msg):
    kb = msg.reply_html.call_args.kwargs.get("reply_markup")
    return kb.inline_keyboard[0][0] if kb else None


async def test_start_playlist_invite(bot, db, https):  # noqa: F811
    add_user(db, "alice", 1)
    add_user(db, "bob", 2)
    pid = db.create_playlist("alice", "Дорога <домой>", "в путь", share_token="TOKEN123")
    upd = make_update(tg_id=2)
    await bot.cmd_start(upd, ctx("pl_TOKEN123"))
    text = upd.effective_message.reply_html.call_args.args[0]
    assert "Дорога &lt;домой&gt;" in text and "alice" in text and "Присоединиться" in text
    btn = _button(upd.effective_message)
    assert btn.text == "Открыть плейлист"
    assert btn.web_app.url == "https://music.test/app/#join=TOKEN123"
    assert db.playlist_role(pid, "bob") is None  # молча не вступаем

    db.add_playlist_member(pid, "bob")
    upd = make_update(tg_id=2)
    await bot.cmd_start(upd, ctx("pl_TOKEN123"))
    assert "уже участник" in upd.effective_message.reply_html.call_args.args[0]


async def test_start_playlist_invite_bad_token_and_unregistered(bot, db, https):  # noqa: F811
    add_user(db, "alice", 1)
    db.create_playlist("alice", "Дорога", share_token="TOK")
    upd = make_update(tg_id=1)
    await bot.cmd_start(upd, ctx("pl_WRONG"))
    assert "недействительно" in upd.effective_message.reply_text.call_args.args[0]
    upd = make_update(tg_id=50)
    await bot.cmd_start(upd, ctx("pl_TOK"))
    text = upd.effective_message.reply_html.call_args.args[0]
    assert "Дорога" in text and "/register" in text
    assert _button(upd.effective_message) is None


async def test_start_without_args_shows_help(bot, db):  # noqa: F811
    upd = make_update(tg_id=1)
    await bot.cmd_start(upd, ctx())
    text = upd.effective_message.reply_html.call_args.args[0]
    assert "PiMusic" in text and "/playlists" in text


async def test_start_invite_without_https_gives_text_link(bot, db, monkeypatch):  # noqa: F811
    monkeypatch.delenv("STREAM_PUBLIC_URL", raising=False)
    add_user(db, "alice", 1)
    db.create_playlist("alice", "Дорога", share_token="TOK")
    upd = make_update(tg_id=1)
    await bot.cmd_start(upd, ctx("pl_TOK"))
    assert "/app/#join=TOK" in upd.effective_message.reply_html.call_args.args[0]
    assert _button(upd.effective_message) is None


async def test_playlists_command(bot, db, cfg, https):  # noqa: F811
    add_user(db, "alice", 1)
    add_user(db, "bob", 2)
    p1 = db.create_playlist("alice", "Мой", share_token="t")
    tid = add_lib(db, cfg, "alice", "A", "B", "C")
    db.add_playlist_tracks(p1, [tid], "alice", 5000)
    p2 = db.create_playlist("bob", "Боба")
    db.add_playlist_member(p2, "alice")
    upd = make_update(tg_id=1)
    await bot.cmd_playlists(upd, ctx())
    text = upd.effective_message.reply_html.call_args.args[0]
    assert "Мой" in text and "1 тр." in text and "общий" in text and "владелец bob" in text
    assert _button(upd.effective_message).web_app.url == "https://music.test/app/#playlists"

    upd = make_update(tg_id=2)
    db.delete_playlist(p2)
    await bot.cmd_playlists(upd, ctx())
    assert "пока нет" in upd.effective_message.reply_html.call_args.args[0]
