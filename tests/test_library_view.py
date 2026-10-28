"""Сортировка и жанры медиатеки: нормализация, миграция, фоновое заполнение, API медиатеки/исполнителей/альбомов."""
import asyncio
import json
import sqlite3
import time

from app import library, libview, service
from app.db import DB
from app.service import Service
from app.util import artist_sort_key, collate, normalize_genres, year_from

from test_webapp import alice, client, hdr  # noqa: F401 — фикстуры


def add(db, cfg, artist, album, title, *, genres=None, year=None, duration=100, added=None, login="alice"):
    d = cfg.library_dir / artist / album
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{title}.mp3"
    p.write_bytes(b"ID3")
    tid = db.add_track(match_key=f"{artist}|{title}".lower(), mbid=None, isrc=None, artist=artist, title=title,
                       album=album, duration=duration, path=str(p), source="test", genres=genres, year=year)
    db.link_track(login, tid, str(cfg.users_dir / login / artist / album / p.name))
    if added is not None:
        db._x("UPDATE user_tracks SET added_at=? WHERE track_id=? AND login=?", (added, tid, login))
    return tid


# ------------------------------------------------------------------ нормализация
def test_normalize_genres_and_year():
    assert normalize_genres("rock; Pop / hip hop") == ["Rock", "Pop", "Hip-Hop"]
    assert normalize_genres(["edm", "alternative rock", "R&B", "Rock"]) == ["EDM", "Alternative Rock", "R&B", "Rock"]
    assert normalize_genres("русский рок, ПОП") == ["Русский рок", "Поп"]
    assert normalize_genres("rock\x00rock\x00 ") == ["Rock"]
    assert normalize_genres(None) == [] and normalize_genres(["", "17"]) == []
    assert year_from("2019-05-03") == 2019 and year_from(1987) == 1987 and year_from("") is None
    assert year_from("1850") is None


def test_collation_russian_and_the():
    assert collate("Ёлка") == collate("елка") and collate("  A   b ") == "a b"
    assert artist_sort_key("The Beatles") == "beatles" and artist_sort_key("Theodor") == "theodor"


# ------------------------------------------------------------------ БД
def test_migration_adds_genre_year_columns(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, match_key TEXT NOT NULL,
        mbid TEXT, isrc TEXT, artist TEXT NOT NULL, title TEXT NOT NULL, album TEXT, duration INTEGER,
        path TEXT NOT NULL UNIQUE, source TEXT, created_at INTEGER NOT NULL);
        INSERT INTO tracks(match_key,artist,title,path,created_at) VALUES('a|b','A','B','/x.mp3',0);""")
    c.commit()
    c.close()
    d = DB(path)
    t = d.get_track(1)
    assert (t.genre, t.genres, t.year) == (None, None, None)
    d.set_genre_year(1, ["Rock", "Pop"], 1999)
    d.set_genre_year(1, None, None)  # пустое не затирает
    t = d.get_track(1)
    assert (t.genre, json.loads(t.genres), t.year) == ("Rock", ["Rock", "Pop"], 1999)
    d._conn.close()


def test_read_genre_year_and_backfill(db, cfg, alice):  # noqa: F811
    from mutagen.id3 import ID3, TCON, TDRC
    ids = []
    for i in range(5):
        tid = add(db, cfg, "A", "Al", f"T{i}")
        tags = ID3()
        tags.add(TCON(encoding=3, text=["(17)", "indie pop"] if i % 2 == 0 else ["Электроника"]))
        tags.add(TDRC(encoding=3, text="2004-02-01"))
        tags.save(db.get_track(tid).path)
        ids.append(tid)
    assert library.read_genre_year(db.get_track(ids[0]).path) == (["Rock", "Indie Pop"], 2004)
    assert library.read_genre_year("/nonexistent.mp3") == ([], None)
    n1, done1 = library.backfill_genres_from_tags(db, batch=3)
    n2, done2 = library.backfill_genres_from_tags(db, batch=3)
    assert (n1, done1, n2, done2) == (3, False, 2, True)
    assert db.get_track(ids[1]).genre == "Электроника" and db.get_track(ids[4]).year == 2004
    assert library.backfill_genres_from_tags(db, batch=3) == (0, True)  # курсор в конце


async def test_genres_online_step_and_loop(db, cfg, alice, monkeypatch):  # noqa: F811
    a = add(db, cfg, "Кино", "Звезда", "Кукушка")
    b = add(db, cfg, "X", "Y", "Z", genres=["Jazz"])
    c = add(db, cfg, "Нет", "Нет", "Ничего")
    asked = []

    async def fake_itunes(hint, **kw):
        asked.append(hint.title)
        if hint.title == "Кукушка":
            return {"primaryGenreName": "russian rock", "releaseDate": "1990-01-01T00:00:00Z"}
        return {"primaryGenreName": "Music"} if hint.title == "Ничего" else None
    monkeypatch.setattr(service.metadata, "lookup_itunes", fake_itunes)
    svc = Service(db, None)
    assert await svc.genres_online_step(10) == (1, True)
    assert asked == ["Кукушка", "Ничего"]  # с жанром — не спрашиваем
    assert (db.get_track(a).genre, db.get_track(a).year) == ("Russian Rock", 1990)
    assert db.get_track(c).genre is None and db.get_track(b).genre == "Jazz"  # «Music» — не жанр
    # цикл: проход по тегам один раз (флаг в kv), онлайн-проход закончен — выходит
    db.kv_set(svc.GENRE_ONLINE_CURSOR, "0")
    asked.clear()
    await asyncio.wait_for(svc._genre_loop(first_delay=0, pause=0), 5)
    assert db.kv_get(svc.GENRE_TAGS_DONE) == "1" and asked == ["Ничего"]


async def test_ingest_stores_genres_and_year(db, cfg, monkeypatch, tmp_path):
    from app.metadata import Hint, TrackMeta
    from app.util import AudioInfo
    src = tmp_path / "in.mp3"
    src.write_bytes(b"ID3")

    async def fake_probe(p):
        return AudioInfo("mp3", 320000, 200, 44100, {"genre": "pop"}, format_name="mp3")

    async def yes(p):
        return True

    async def fake_resolve(hint):
        return TrackMeta(title="T", artist="A", album_artist="A", album="Al", genres=["electronic", "House"],
                         date="2011-03-01")

    async def fake_tag(path, meta, work, **_):
        return {}
    monkeypatch.setattr(library, "probe", fake_probe)
    monkeypatch.setattr(library, "mp3_is_cbr320", yes)
    monkeypatch.setattr(library.metadata, "resolve", fake_resolve)
    monkeypatch.setattr(library.tagger, "tag_file", fake_tag)
    db.upsert_user(__import__("app.db", fromlist=["User"]).User("bob", 9, "jf", None, "t", False))
    res = await library.ingest(db, src, Hint(artist="A", title="T"), source="test", login="bob")
    t = db.get_track(res.track.id)
    assert (t.genre, json.loads(t.genres), t.year) == ("Electronic", ["Electronic", "House"], 2011)


# ------------------------------------------------------------------ сортировка (чистые функции)
def test_sort_rows_missing_values_last_and_seed(db, cfg, alice):  # noqa: F811
    add(db, cfg, "B", "Al", "x", year=2001)
    add(db, cfg, "A", "Al", "y")
    add(db, cfg, "C", "Al", "z", year=1999)
    rows = db.user_library_view("alice")
    for desc in (False, True):
        ys = [r[0].year for r in libview.sort_rows(rows, "year", desc)]
        assert ys[-1] is None and ys[:2] == ([1999, 2001] if not desc else [2001, 1999])
    r1 = [r[0].id for r in libview.sort_rows(rows, "random", seed=42)]
    assert r1 == [r[0].id for r in libview.sort_rows(rows, "random", seed=42)]  # стабильно для пагинации


# ------------------------------------------------------------------ API
async def _lib(client, **params):
    r = await client.get("/api/library", params=params, headers=hdr())
    assert r.status == 200, await r.text()
    return await r.json()


async def test_library_sort_filter_paging(client, db, cfg):
    now = int(time.time())
    t1 = add(db, cfg, "The Beatles", "Abbey Road", "Something", genres=["Rock"], year=1969, duration=182,
             added=now - 300)
    t2 = add(db, cfg, "Ёлка", "Прованс", "Прованс", genres=["Pop", "Русский поп"], year=2011, duration=215,
             added=now - 100)
    t3 = add(db, cfg, "Aerosmith", "Pump", "Elevator", genres=["Rock", "Hard Rock"], year=None, duration=320,
             added=now - 200)
    t4 = add(db, cfg, "Земфира", "Вендетта", "Дышать", genres=None, year=2005, duration=0, added=now)
    ids = lambda d: [i["id"] for i in d["items"]]  # noqa: E731
    d = await _lib(client)
    assert ids(d) == [t4, t2, t3, t1] and d["total"] == 4 and d["library_total"] == 4  # по умолчанию — новые
    item = next(i for i in d["items"] if i["id"] == t2)
    assert item["genre"] == "Pop" and item["year"] == 2011 and item["added_at"] == now - 100
    assert ids(await _lib(client, sort="artist")) == [t3, t1, t2, t4]  # «The» не учитывается, ё = е
    assert ids(await _lib(client, sort="artist", order="desc")) == [t4, t2, t1, t3]
    assert ids(await _lib(client, sort="title")) == [t3, t1, t4, t2]
    assert ids(await _lib(client, sort="year", order="asc")) == [t1, t4, t2, t3]  # без года — в конце
    assert ids(await _lib(client, sort="year")) == [t2, t4, t1, t3]  # по умолчанию — новые годы первыми
    assert ids(await _lib(client, sort="duration", order="desc")) == [t3, t2, t1, t4]
    assert ids(await _lib(client, sort="genre")) == [t2, t3, t1, t4]  # Rock: Aerosmith, Beatles; без жанра — в конце
    d = await _lib(client, genre="rock", sort="title")
    assert ids(d) == [t3, t1] and d["total"] == 2 and d["library_total"] == 4
    assert ids(await _lib(client, genre="Русский поп")) == [t2]
    assert ids(await _lib(client, q="елка")) == [t2]
    d = await _lib(client, sort="artist", offset=1, limit=2)
    assert ids(d) == [t1, t2] and d["total"] == 4 and d["offset"] == 1
    assert set(ids(await _lib(client, sort="random", seed=7))) == {t1, t2, t3, t4}
    for bad in ({"sort": "zzz"}, {"order": "up"}):
        r = await client.get("/api/library", params=bad, headers=hdr())
        assert r.status == 400


async def test_genres_artists_albums(client, db, cfg):
    now = int(time.time())
    add(db, cfg, "Кино", "Группа крови", "a", genres=["Rock"], year=1988, added=now - 50)
    add(db, cfg, "Кино", "Группа крови", "b", genres=["Rock", "Post-Punk"], year=1988, added=now - 40)
    add(db, cfg, "Кино", "Звезда", "c", genres=["Rock"], year=1989, added=now - 30)
    add(db, cfg, "Земфира", "Вендетта", "d", genres=["Pop"], year=2005, added=now)
    add(db, cfg, "Безжанровый", "Х", "e", added=now - 100)
    r = await client.get("/api/library/genres", headers=hdr())
    assert (await r.json())["items"] == [{"name": "Rock", "count": 3}, {"name": "Pop", "count": 1},
                                         {"name": "Post-Punk", "count": 1}]

    async def get(path, **params):
        r = await client.get(path, params=params, headers=hdr())
        assert r.status == 200, await r.text()
        return (await r.json())["items"]
    arts = await get("/api/library/artists")
    assert [a["name"] for a in arts] == ["Безжанровый", "Земфира", "Кино"]
    assert [a["name"] for a in await get("/api/library/artists", sort="count")] == ["Кино", "Безжанровый", "Земфира"]
    assert [a["name"] for a in await get("/api/library/artists", sort="added")][0] == "Земфира"
    assert [a["name"] for a in await get("/api/library/artists", sort="year")] == ["Земфира", "Кино", "Безжанровый"]
    assert [(a["name"], a["count"]) for a in await get("/api/library/artists", genre="rock")] == [("Кино", 3)]
    albums = await get("/api/library/albums", genre="Rock", sort="year", order="asc")
    assert [(a["title"], a["count"], a["year"]) for a in albums] == [("Группа крови", 2, 1988), ("Звезда", 1, 1989)]
    assert (await client.get("/api/library/albums", params={"sort": "title"}, headers=hdr())).status == 400


async def test_play_respects_view_and_legacy(client, db, cfg):
    now = int(time.time())
    a = add(db, cfg, "A", "Al", "a", genres=["Rock"], added=now - 10)
    b = add(db, cfg, "B", "Al", "b", genres=["Pop"], added=now)
    c = add(db, cfg, "C", "Al", "c", genres=["Rock"], added=now - 5)

    async def play(**params):
        r = await client.get("/api/library/play", params=params, headers=hdr())
        assert r.status == 200, await r.text()
        return [i["id"] for i in (await r.json())["items"]]
    assert await play(genre="rock", sort="added", order="desc") == [c, a]
    assert await play() == [a, b, c]  # по умолчанию — по исполнителю
    assert await play(order="recent") == [b, c, a]  # старые значения
    assert sorted(await play(order="shuffle")) == sorted([a, b, c])
    assert await play(order="artist", genre="pop") == [b]
