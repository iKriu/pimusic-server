"""Каталог Mini App поверх Deezer: три раздела поиска, страницы альбома и артиста, «все треки» артиста."""
import asyncio

import pytest

from app import http, search


def deezer(monkeypatch, routes: dict):
    """Подмена http.get_json: routes — путь Deezer -> ответ (или функция params -> ответ)."""
    calls = []

    async def fake(url, *, params=None, limiter=None, **kw):
        assert url.startswith(search.DEEZER), url
        path = url[len(search.DEEZER):]
        calls.append((path, dict(params or {}), limiter))
        r = routes.get(path)
        return r(params or {}) if callable(r) else r

    monkeypatch.setattr(http, "get_json", fake)
    return calls


@pytest.fixture(autouse=True)
def _clean_caches():
    search._ALBUM_CACHE.clear()
    search._ARTIST_TRACKS.clear()
    search._ARTIST_LOCKS.clear()
    yield
    search._ALBUM_CACHE.clear()
    search._ARTIST_TRACKS.clear()
    search._ARTIST_LOCKS.clear()


def dz_track(title, artist="Кино", album="Группа крови", duration=200, **kw):
    return {"title": title, "duration": duration, "artist": {"name": artist},
            "album": {"title": album, "cover_xl": "https://img/xl.jpg"}, **kw}


def dz_album(aid, title, artist="Кино", artist_id=1, **kw):
    return {"id": aid, "title": title, "artist": {"id": artist_id, "name": artist},
            "cover_medium": f"https://img/{aid}.jpg", **kw}


# ------------------------------------------------------------------ поиск
async def test_catalog_search_three_sections_dedupe_and_limits(monkeypatch):
    tracks = [dz_track(f"Песня {i}") for i in range(20)]
    tracks.insert(1, dz_track("Песня 0 (Remastered)"))  # повтор по нормализованному названию
    albums = [dz_album(100 + i, f"Альбом {i}", record_type="album") for i in range(15)]
    albums.insert(2, dz_album(999, "Альбом 0 [Deluxe]"))  # то же название того же артиста
    artists = [{"id": 10 + i, "name": f"Кино {i}", "nb_fan": 10} for i in range(9)]
    artists.insert(3, {"id": 1, "name": "Кино", "nb_fan": 500000, "picture_xl": "https://img/kino.jpg"})
    artists.append({"id": 10, "name": "Кино 0"})  # тот же id ещё раз
    calls = deezer(monkeypatch, {"/search": {"data": tracks}, "/search/album": {"data": albums},
                                 "/search/artist": {"data": artists}})

    res = await search.catalog_search("кино", 77)
    assert res.owner == 77 and res.query == "кино"
    assert len(res.tracks) == search.SEARCH_TRACKS == 15
    assert [t.title for t in res.tracks[:2]] == ["Песня 0", "Песня 1"]
    assert len(res.albums) == search.SEARCH_ALBUMS == 12
    assert "Альбом 0 [Deluxe]" not in [a.title for a in res.albums]
    assert res.albums[0].id == "100" and res.albums[0].cover == "https://img/100.jpg"
    assert res.albums[0].artist == "Кино" and res.albums[0].artist_id == "1"
    # известный артист — карточка и первым в разделе «Исполнители»
    assert res.artist == "Кино" and res.artist_id == "1" and res.picture == "https://img/kino.jpg"
    assert res.fans == 500000
    assert [a.id for a in res.artists][:2] == ["1", "10"]
    assert len(res.artists) == search.SEARCH_ARTISTS == 8
    assert len({a.id for a in res.artists}) == 8
    # три запроса к Deezer, все через лимитер
    assert sorted(c[0] for c in calls) == ["/search", "/search/album", "/search/artist"]
    assert all(c[2] is search.DEEZER_LIMIT for c in calls)


async def test_catalog_search_for_song_title_has_no_artist_card(monkeypatch):
    deezer(monkeypatch, {"/search": {"data": [dz_track("Кукушка")]},
                         "/search/album": {"data": [dz_album(5, "Кукушка", artist="Земфира", artist_id=9)]},
                         "/search/artist": {"data": [{"id": 9, "name": "Земфира", "nb_fan": 900000}]}})
    res = await search.catalog_search("кукушка", 1)
    assert res.artist == "" and res.artist_id == ""
    assert [a.name for a in res.artists] == ["Земфира"]
    assert [a.title for a in res.albums] == ["Кукушка"] and [t.title for t in res.tracks] == ["Кукушка"]


async def test_catalog_search_falls_back_to_itunes(monkeypatch):
    deezer(monkeypatch, {})
    from app.sources import TrackRequest

    async def itunes(q):
        return [TrackRequest(artist="A", title="B")]

    monkeypatch.setattr(search, "_itunes_tracks", itunes)
    res = await search.catalog_search("zzz", 1)
    assert [t.label for t in res.tracks] == ["A — B"] and res.albums == [] and res.artists == []


async def test_catalog_search_survives_deezer_errors(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(http, "get_json", boom)

    async def itunes(q):
        return []

    monkeypatch.setattr(search, "_itunes_tracks", itunes)
    res = await search.catalog_search("кино", 1)
    assert res.tracks == [] and res.albums == [] and res.artists == []


def test_valid_id():
    assert search.valid_id("302127") == "302127"
    assert search.valid_id(" 42 ") == "42"
    for bad in ("", "abc", "1/../2", "-1", "1e5", "1" * 16, None, "12?x=1"):
        assert search.valid_id(bad) is None


async def test_legacy_search_still_works(monkeypatch):
    deezer(monkeypatch, {
        "/search/artist": {"data": [{"id": 1, "name": "Кино", "nb_fan": 500000}]},
        "/search": {"data": [dz_track("Кукушка")]},
        "/artist/1/top": {"data": [dz_track("Группа крови")]},
        "/artist/1/albums": {"data": [{"id": 7, "title": "Звезда", "record_type": "album", "release_date": "1989-01-01"}]},
    })
    res = await search.search("кино", 5)
    assert res.artist == "Кино" and [t.title for t in res.tracks] == ["Группа крови"]
    assert [(a.id, a.year) for a in res.albums] == [("7", "1989")]
    assert res.artists == [] and res.title == ""


# ------------------------------------------------------------------ альбом
ALBUM_302 = {"id": 302, "title": "Звезда по имени Солнце", "record_type": "album", "release_date": "1989-06-01",
             "cover_medium": "https://img/m.jpg", "cover_xl": "https://img/xl.jpg", "nb_tracks": 3,
             "artist": {"id": 1, "name": "Кино"},
             "tracks": {"data": [dz_track("Звезда по имени Солнце"), dz_track("Пачка сигарет")]}}


async def test_catalog_album_full_tracklist_and_cache(monkeypatch):
    calls = deezer(monkeypatch, {
        "/album/302": ALBUM_302,
        "/album/302/tracks": {"data": [dz_track("Звезда по имени Солнце"), dz_track("Пачка сигарет"),
                                       dz_track("Спокойная ночь", artist="")]},
    })
    res = await search.catalog_album("302", 9)
    assert res.owner == 9 and res.title == "Кино — Звезда по имени Солнце"
    assert res.artist == "Кино" and res.artist_id == "1"
    al = res.albums[0]
    assert (al.id, al.title, al.year, al.cover_xl) == ("302", "Звезда по имени Солнце", "1989", "https://img/xl.jpg")
    assert [t.title for t in res.tracks] == ["Звезда по имени Солнце", "Пачка сигарет", "Спокойная ночь"]
    assert [t.track_no for t in res.tracks] == [1, 2, 3]
    assert res.tracks[2].artist == "Кино"  # пустой артист трека — артист альбома
    assert all(t.album == "Звезда по имени Солнце" and t.year == "1989" for t in res.tracks)
    assert res.album_tracks[0] == res.tracks  # скачивание kind=album не ходит в сеть повторно
    n = len(calls)
    await search.catalog_album("302", 10)
    assert len(calls) == n, "треклист берётся из кэша"


async def test_catalog_album_not_found(monkeypatch):
    deezer(monkeypatch, {"/album/1": {"error": {"type": "DataException", "code": 800}}})
    with pytest.raises(LookupError):
        await search.catalog_album("1", 1)
    with pytest.raises(LookupError):
        await search.catalog_album("2", 1)  # 404 -> None


# ------------------------------------------------------------------ артист
def artist_routes(n_albums=4, extra=None):
    raw = [
        {"id": 11, "title": "Группа крови", "record_type": "album", "release_date": "1988-01-01"},
        {"id": 12, "title": "Группа крови (Remastered)", "record_type": "album", "release_date": "2019-01-01"},
        {"id": 13, "title": "Звезда по имени Солнце", "record_type": "album", "release_date": "1989-01-01"},
        {"id": 14, "title": "Кукушка", "record_type": "single", "release_date": "2021-01-01"},
        {"id": 15, "title": "Сборник", "record_type": "compile", "release_date": "2000-01-01"},
    ][:n_albums + 1]
    routes = {
        "/artist/1": {"id": 1, "name": "Кино", "nb_fan": 123456, "picture_xl": "https://img/kino-xl.jpg"},
        "/artist/1/top": {"data": [dz_track("Кукушка"), dz_track("Кукушка (Live)"), dz_track("Группа крови")]},
        "/artist/1/albums": {"data": raw},
        "/album/11": {"id": 11, "title": "Группа крови", "release_date": "1988-01-01",
                      "tracks": {"data": [dz_track("Группа крови"), dz_track("Закрой за мной дверь")]}},
        "/album/13": {"id": 13, "title": "Звезда по имени Солнце", "release_date": "1989-01-01",
                      "tracks": {"data": [dz_track("Звезда по имени Солнце"), dz_track("Группа крови (Live)"),
                                          dz_track("Кукушка")]}},
        "/album/14": {"id": 14, "title": "Кукушка", "release_date": "2021-01-01",
                      "tracks": {"data": [dz_track("Кукушка (Remastered)")]}},
        "/album/15": {"id": 15, "title": "Сборник", "tracks": {"data": [dz_track("Сборная песня")]}},
    }
    routes.update(extra or {})
    return routes


async def test_catalog_artist_page(monkeypatch):
    deezer(monkeypatch, artist_routes())
    res = await search.catalog_artist("1", 3)
    assert res.artist == "Кино" and res.artist_id == "1" and res.fans == 123456
    assert res.picture == "https://img/kino-xl.jpg" and res.title == "Кино — популярное"
    assert [t.title for t in res.tracks] == ["Кукушка", "Группа крови"]
    # новые сверху, переиздание с тем же названием — один раз (оригинал 1988)
    assert [(a.id, a.year, a.kind) for a in res.albums] == [
        ("14", "2021", "single"), ("15", "2000", "compile"), ("13", "1989", "album"), ("11", "1988", "album")]
    assert all(a.artist == "Кино" and a.artist_id == "1" for a in res.albums)


async def test_catalog_artist_not_found(monkeypatch):
    deezer(monkeypatch, {})
    with pytest.raises(LookupError):
        await search.catalog_artist("1", 3)
    with pytest.raises(LookupError):
        await search.artist_all_tracks("1")


async def test_artist_all_tracks_dedupe_order_and_cache(monkeypatch):
    calls = deezer(monkeypatch, artist_routes())
    artist, tracks, meta = await search.artist_all_tracks("1")
    assert artist.name == "Кино"
    titles = [t.title for t in tracks]
    # «Группа крови (Live)» и «Кукушка (Remastered)» — повторы; сборники не берём
    assert sorted(titles) == sorted(["Группа крови", "Закрой за мной дверь", "Звезда по имени Солнце", "Кукушка"])
    kuku = next(t for t in tracks if t.title == "Кукушка")
    assert kuku.album == "Звезда по имени Солнце" and kuku.year == "1989", "альбомная версия важнее сингла"
    # порядок: новые альбомы сверху, внутри альбома — по номеру
    assert titles == ["Звезда по имени Солнце", "Кукушка", "Группа крови", "Закрой за мной дверь"]
    assert meta == {"albums": 3, "capped": False}
    assert "/album/15" not in [c[0] for c in calls] and "/album/12" not in [c[0] for c in calls]
    assert all(c[2] is search.DEEZER_LIMIT for c in calls)
    n = len(calls)
    again = await search.artist_all_tracks("1")
    assert len(calls) == n and [t.title for t in again[1]] == titles


async def test_artist_all_tracks_prefers_studio_over_live(monkeypatch):
    routes = artist_routes()
    routes["/artist/1/albums"]["data"].append(
        {"id": 16, "title": "Концерт в Москве", "record_type": "album", "release_date": "1980-01-01"})
    routes["/album/16"] = {"id": 16, "title": "Концерт в Москве", "release_date": "1980-01-01",
                           "tracks": {"data": [dz_track("Кукушка"), dz_track("Редкая концертная")]}}
    deezer(monkeypatch, routes)
    _, tracks, meta = await search.artist_all_tracks("1")
    by_title = {t.title: t.album for t in tracks}
    assert by_title["Кукушка"] == "Звезда по имени Солнце"
    assert by_title["Редкая концертная"] == "Концерт в Москве"
    assert meta["albums"] == 4


async def test_artist_all_tracks_caps(monkeypatch):
    monkeypatch.setattr(search, "ARTIST_MAX_ALBUMS", 1)
    calls = deezer(monkeypatch, artist_routes())
    _, tracks, meta = await search.artist_all_tracks("1")
    assert [c[0] for c in calls if c[0].startswith("/album/")] == ["/album/11"]
    assert [t.title for t in tracks] == ["Группа крови", "Закрой за мной дверь"]
    assert meta["capped"] is True

    search._ARTIST_TRACKS.clear()
    monkeypatch.setattr(search, "ARTIST_MAX_ALBUMS", 30)
    monkeypatch.setattr(search, "ARTIST_MAX_TRACKS", 3)
    _, tracks, meta = await search.artist_all_tracks("1")
    assert len(tracks) == 3 and meta["capped"] is True


async def test_artist_all_tracks_concurrent_requests_fetch_once(monkeypatch):
    calls = deezer(monkeypatch, artist_routes())
    a, b = await asyncio.gather(search.artist_all_tracks("1"), search.artist_all_tracks("1"))
    assert [t.title for t in a[1]] == [t.title for t in b[1]]
    assert sum(1 for c in calls if c[0] == "/artist/1") == 1


# ------------------------------------------------------------------ 30-секундные фрагменты (preview)
PV = "https://cdns-preview-d.dzcdn.net/stream/c-deadbeef-1.mp3"


@pytest.mark.parametrize("url,ok", [
    (PV, True),
    ("http://cdnt-preview.dzcdn.net/api/1/1/a/b/c/abc.mp3?hdnea=exp=1~acl=*~hmac=ff", True),
    ("https://dzcdn.net/x.mp3", True),
    ("", False), (None, False), (123, False),
    ("https://evil.example.com/x.mp3", False),
    ("https://dzcdn.net.evil.com/x.mp3", False),
    ("https://evil-dzcdn.net/x.mp3", False),
    ("javascript:alert(1)//dzcdn.net", False),
    ("ftp://cdns-preview-d.dzcdn.net/x.mp3", False),
    ("https://user:pw@cdns-preview-d.dzcdn.net/x.mp3", False),
    ("https://cdns-preview-d.dzcdn.net/x y.mp3", False),
    ("https://cdns-preview-d.dzcdn.net/" + "a" * 1000, False),
    ("https://[::1/x", False),
])
def test_valid_preview(url, ok):
    assert search.valid_preview(url) == (url if ok else "")


def test_req_from_deezer_carries_preview():
    assert search._req_from_deezer(dz_track("Кукушка", preview=PV)).preview == PV
    assert search._req_from_deezer(dz_track("Кукушка", preview="https://evil.com/a.mp3")).preview == ""
    assert search._req_from_deezer(dz_track("Кукушка")).preview == ""


async def test_catalog_search_and_album_keep_preview(monkeypatch):
    deezer(monkeypatch, {"/search": {"data": [dz_track("Песня", preview=PV), dz_track("Без фрагмента")]},
                         "/search/album": {"data": []}, "/search/artist": {"data": []}})
    res = await search.catalog_search("кино", 1)
    assert [t.preview for t in res.tracks] == [PV, ""]
