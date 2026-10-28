"""Поиск вариантов для выбора в боте: артист (топ-треки + альбомы) или список треков.
Источник — Deezer API (без ключа, отдаёт ISRC и длительность), запасной — iTunes Search."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import http
from .sources import TrackRequest
from .util import norm, similarity

log = logging.getLogger(__name__)

DEEZER = "https://api.deezer.com"
TTL = 3600  # сколько живут кнопки результата поиска
DEEZER_LIMIT = http.RateLimiter(0.2)  # каталог Mini App: вместе с artwork.DEEZER_LIMIT < 50 запросов / 5 с
CATALOG_TTL = 3600                    # кэш треклистов альбомов и «всех треков» артиста
SEARCH_TRACKS, SEARCH_ALBUMS, SEARCH_ARTISTS = 15, 12, 8
ARTIST_MAX_ALBUMS, ARTIST_MAX_TRACKS = 30, 400
_ID_RE = re.compile(r"^[0-9]{1,15}$")
_PARENS = re.compile(r"\(.*?\)|\[.*?\]")
_LIVE = re.compile(r"\b(live|concert|tour|unplugged|концерт|в севкабеле|акустика)\b", re.I)


@dataclass
class Album:
    id: str
    title: str
    artist: str
    year: str = ""
    kind: str = "album"      # album / ep / single / compile
    tracks: int = 0
    source: str = "deezer"
    cover: str = ""          # миниатюра (cover_medium)
    cover_xl: str = ""
    artist_id: str = ""


@dataclass
class Artist:
    id: str
    name: str
    picture: str = ""        # picture_medium
    picture_xl: str = ""
    fans: int = 0
    albums: int = 0


@dataclass
class Result:
    query: str
    owner: int                               # tg_id того, кто искал — чужие кнопки не работают
    artist: str = ""                         # если запрос — это имя артиста
    tracks: list[TrackRequest] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    album_tracks: dict[int, list[TrackRequest]] = field(default_factory=dict)  # кэш раскрытых альбомов
    artists: list[Artist] = field(default_factory=list)  # раздел «Исполнители» в Mini App
    artist_id: str = ""
    picture: str = ""
    fans: int = 0
    title: str = ""                          # заголовок задания «скачать все»


_RESULTS: dict[str, Result] = {}


def store(res: Result) -> str:
    now = time.time()
    for k in [k for k, v in _RESULTS.items() if now - v.created > TTL]:
        del _RESULTS[k]
    token = secrets.token_urlsafe(6)
    _RESULTS[token] = res
    return token


def get(token: str) -> Result | None:
    res = _RESULTS.get(token)
    if res and time.time() - res.created <= TTL:
        return res
    return None


def fmt_duration(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


_PREVIEW_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*dzcdn\.net$")


def valid_preview(url: object) -> str:
    """30-секундный фрагмент Deezer: только http(s)-ссылка на *.dzcdn.net, иначе пустая строка."""
    if not isinstance(url, str) or not url or len(url) > 1000 or any(ord(c) < 33 for c in url):
        return ""
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or u.username or u.password or not _PREVIEW_HOST.match(host):
        return ""
    return url


def _req_from_deezer(t: dict, album: str = "") -> TrackRequest:
    return TrackRequest(
        artist=(t.get("artist") or {}).get("name", ""),
        title=t.get("title") or t.get("title_short") or "",
        album=album or (t.get("album") or {}).get("title", ""),
        duration=float(t.get("duration") or 0),
        isrc=t.get("isrc") or "",
        track_no=int(t.get("track_position") or 0),
        cover_url=(t.get("album") or {}).get("cover_xl") or "",
        query=f"{(t.get('artist') or {}).get('name', '')} - {t.get('title', '')}",
        origin="text",
        preview=valid_preview(t.get("preview")),
    )


def _dedupe(tracks: list[TrackRequest], limit: int) -> list[TrackRequest]:
    seen: set[str] = set()
    out = []
    for t in tracks:
        k = norm(t.artist) + "|" + norm(re.sub(r"\(.*?\)|\[.*?\]", "", t.title))
        if k in seen or not t.title:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _match_artist(query: str, items: list[dict]) -> dict | None:
    q = norm(query)
    for a in items:
        name = norm(a.get("name", ""))
        if name == q or (len(q) > 3 and similarity(name, q) >= 0.85):
            return a
    return None


async def _deezer_artist(query: str) -> dict | None:
    data = await http.get_json(f"{DEEZER}/search/artist", params={"q": query, "limit": "5"})
    return _match_artist(query, (data or {}).get("data") or [])


async def _deezer_artist_page(a: dict) -> tuple[list[TrackRequest], list[Album]]:
    top = await http.get_json(f"{DEEZER}/artist/{a['id']}/top", params={"limit": "15"})
    tracks = _dedupe([_req_from_deezer(t) for t in (top or {}).get("data") or []], 10)
    albums_raw = await http.get_json(f"{DEEZER}/artist/{a['id']}/albums", params={"limit": "100"})
    albums: list[Album] = []
    seen: set[str] = set()
    for al in (albums_raw or {}).get("data") or []:
        kind = al.get("record_type") or "album"
        if kind not in ("album", "ep"):
            continue
        key = norm(re.sub(r"\(.*?\)|\[.*?\]", "", al.get("title", "")))
        if key in seen:  # deluxe / remaster одного и того же альбома
            continue
        seen.add(key)
        albums.append(Album(id=str(al["id"]), title=al.get("title", ""), artist=a.get("name", ""),
                            year=(al.get("release_date") or "")[:4], kind=kind, tracks=int(al.get("fans") or 0)))
    # сначала популярные студийные альбомы, концертные и сборники-переиздания — в конце
    albums.sort(key=lambda x: (bool(_LIVE.search(x.title)), x.kind != "album", -x.tracks))
    return tracks, albums[:12]


async def _deezer_tracks(query: str) -> list[TrackRequest]:
    data = await http.get_json(f"{DEEZER}/search", params={"q": query, "limit": "25"})
    return _dedupe([_req_from_deezer(t) for t in (data or {}).get("data") or []], 10)


async def _itunes_tracks(query: str) -> list[TrackRequest]:
    data = await http.get_json("https://itunes.apple.com/search",
                               params={"term": query, "entity": "song", "limit": "25"}, limiter=http.ITUNES_LIMIT)
    out = []
    for r in (data or {}).get("results") or []:
        out.append(TrackRequest(artist=r.get("artistName", ""), title=r.get("trackName", ""),
                                album=re.sub(r"\s+-\s+(Single|EP)$", "", r.get("collectionName", "")),
                                duration=(r.get("trackTimeMillis") or 0) / 1000,
                                track_no=int(r.get("trackNumber") or 0),
                                query=f"{r.get('artistName', '')} - {r.get('trackName', '')}", origin="text"))
    return _dedupe(out, 10)


async def search(query: str, owner: int) -> Result:
    res = Result(query=query, owner=owner)
    try:
        artist = await _deezer_artist(query)
        found = await _deezer_tracks(query)
        # режим артиста — только если это действительно известный артист, а не совпадение с названием песни
        if artist and ((artist.get("nb_fan") or 0) >= 20000
                       or (found and norm(found[0].artist) == norm(artist.get("name", "")))):
            res.artist = artist.get("name", query)
            res.tracks, res.albums = await _deezer_artist_page(artist)
        if not res.tracks:
            res.tracks = found
            res.artist = res.artist if res.albums else ""
    except Exception as e:  # noqa: BLE001
        log.warning("Deezer search failed: %s", e)
    if not res.tracks and not res.albums:
        try:
            res.tracks = await _itunes_tracks(query)
        except Exception as e:  # noqa: BLE001
            log.warning("iTunes search failed: %s", e)
    return res


async def album_tracks(res: Result, idx: int) -> list[TrackRequest]:
    if idx in res.album_tracks:
        return res.album_tracks[idx]
    al = res.albums[idx]
    data = await http.get_json(f"{DEEZER}/album/{al.id}")
    tracks = []
    for t in ((data or {}).get("tracks") or {}).get("data") or []:
        req = _req_from_deezer(t, album=al.title)
        req.cover_url = (data or {}).get("cover_xl") or ""
        req.year = al.year
        tracks.append(req)
    # номер трека в списке /album/{id} не отдаётся — порядок в списке и есть порядок на альбоме
    for i, t in enumerate(tracks, start=1):
        t.track_no = t.track_no or i
    res.album_tracks[idx] = tracks
    return tracks


# ================================================================== каталог для Mini App
def valid_id(value: object) -> str | None:
    """Deezer id — только цифры: в путь API подставляется лишь проверенное значение."""
    s = str(value or "").strip()
    return s if _ID_RE.match(s) else None


def _title_key(title: str) -> str:
    return norm(_PARENS.sub("", title or ""))


async def _get(path: str, params: dict | None = None) -> dict | None:
    data = await http.get_json(f"{DEEZER}{path}", params=params, limiter=DEEZER_LIMIT)
    if not isinstance(data, dict) or data.get("error"):
        return None
    return data


async def _safe(coro):  # noqa: ANN001, ANN202
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning("Deezer request failed: %s", e)
        return None


def _items(data: dict | None) -> list[dict]:
    return [x for x in ((data or {}).get("data") or []) if isinstance(x, dict)]


def _album_from(al: dict, artist: str = "", artist_id: str = "") -> Album:
    a = al.get("artist") or {}
    return Album(id=str(al.get("id") or ""), title=al.get("title") or "", artist=a.get("name") or artist,
                 year=(al.get("release_date") or "")[:4], kind=al.get("record_type") or "album",
                 tracks=int(al.get("nb_tracks") or 0), cover=al.get("cover_medium") or al.get("cover") or "",
                 cover_xl=al.get("cover_xl") or al.get("cover_big") or "",
                 artist_id=str(a.get("id") or artist_id or ""))


def _artist_from(a: dict) -> Artist:
    return Artist(id=str(a.get("id") or ""), name=a.get("name") or "",
                  picture=a.get("picture_medium") or a.get("picture") or "",
                  picture_xl=a.get("picture_xl") or a.get("picture_big") or "",
                  fans=int(a.get("nb_fan") or 0), albums=int(a.get("nb_album") or 0))


async def catalog_search(query: str, owner: int) -> Result:
    """Три раздела для любого запроса: треки, альбомы, исполнители (Deezer, запросы параллельно)."""
    res = Result(query=query, owner=owner)
    tr, al, ar = await asyncio.gather(
        _safe(_get("/search", {"q": query, "limit": "30"})),
        _safe(_get("/search/album", {"q": query, "limit": "25"})),
        _safe(_get("/search/artist", {"q": query, "limit": "10"})))
    res.tracks = _dedupe([_req_from_deezer(t) for t in _items(tr)], SEARCH_TRACKS)
    seen: set[str] = set()
    for raw in _items(al):
        a = _album_from(raw)
        key = norm(a.artist) + "|" + _title_key(a.title)
        if not a.id or not a.title or key in seen:
            continue
        seen.add(key)
        res.albums.append(a)
        if len(res.albums) >= SEARCH_ALBUMS:
            break
    artists_raw = _items(ar)
    ids: set[str] = set()
    for raw in artists_raw:
        a = _artist_from(raw)
        if a.id and a.name and a.id not in ids:
            ids.add(a.id)
            res.artists.append(a)
    # карточка артиста — только если запрос действительно его имя (те же правила, что в боте)
    known = _match_artist(query, artists_raw)
    if known and ((known.get("nb_fan") or 0) >= 20000
                  or (res.tracks and norm(res.tracks[0].artist) == norm(known.get("name", "")))):
        k = _artist_from(known)
        res.artist, res.artist_id, res.picture, res.fans = k.name, k.id, k.picture_xl or k.picture, k.fans
        res.artists = [k] + [a for a in res.artists if a.id != k.id]
    res.artists = res.artists[:SEARCH_ARTISTS]
    if not res.tracks and not res.albums and not res.artists:
        try:
            res.tracks = await _itunes_tracks(query)
        except Exception as e:  # noqa: BLE001
            log.warning("iTunes search failed: %s", e)
    return res


_ALBUM_CACHE: dict[str, tuple[float, dict]] = {}
_ARTIST_TRACKS: dict[str, tuple[float, Artist, list[TrackRequest], dict]] = {}
_ARTIST_LOCKS: dict[str, asyncio.Lock] = {}


def _prune(cache: dict, limit: int) -> None:
    now = time.time()
    for k in [k for k, v in cache.items() if now - v[0] > CATALOG_TTL]:
        del cache[k]
    while len(cache) >= limit:
        del cache[next(iter(cache))]


async def _album_data(album_id: str) -> dict | None:
    hit = _ALBUM_CACHE.get(album_id)
    if hit and time.time() - hit[0] <= CATALOG_TTL:
        return hit[1]
    data = await _get(f"/album/{album_id}")
    if not data or not data.get("id"):
        return None
    tracks = _items(data.get("tracks"))
    if int(data.get("nb_tracks") or 0) > len(tracks):  # длинные альбомы: полный треклист отдельным запросом
        more = await _safe(_get(f"/album/{album_id}/tracks", {"limit": "500"}))
        if more and len(_items(more)) > len(tracks):
            data = {**data, "tracks": {"data": _items(more)}}
    _prune(_ALBUM_CACHE, 500)
    _ALBUM_CACHE[album_id] = (time.time(), data)
    return data


def _album_reqs(data: dict, al: Album) -> list[TrackRequest]:
    out = []
    for i, t in enumerate(_items(data.get("tracks")), start=1):
        req = _req_from_deezer(t, album=al.title)
        if not req.artist:
            req.artist = al.artist
        req.cover_url = data.get("cover_xl") or al.cover_xl or ""
        req.year = al.year
        req.track_no = req.track_no or i  # в /album/{id} номер не отдаётся — порядок списка и есть порядок
        if req.title:
            out.append(req)
    return out


async def catalog_album(album_id: str, owner: int) -> Result:
    """Страница альбома по Deezer id. LookupError — такого альбома нет."""
    data = await _album_data(album_id)
    if not data:
        raise LookupError(album_id)
    al = _album_from(data)
    tracks = _album_reqs(data, al)
    res = Result(query=al.title, owner=owner, artist=al.artist, artist_id=al.artist_id,
                 title=f"{al.artist} — {al.title}" if al.artist else al.title, tracks=tracks, albums=[al])
    res.album_tracks[0] = tracks
    return res


def _discography(raw: list[dict], artist: Artist) -> list[Album]:
    """Альбомы/EP/синглы, новые сверху; переиздания с тем же названием и типом — один раз (самое раннее)."""
    albums = [a for a in (_album_from(x, artist.name, artist.id) for x in raw) if a.id and a.title]
    first: dict[str, Album] = {}
    for a in sorted(albums, key=lambda x: x.year or "9999"):
        first.setdefault(f"{a.kind}|{_title_key(a.title)}", a)
    return sorted(first.values(), key=lambda x: x.year or "0000", reverse=True)


async def _artist_base(artist_id: str) -> tuple[Artist, list[dict]]:
    info, albums = await asyncio.gather(_get(f"/artist/{artist_id}"),
                                        _safe(_get(f"/artist/{artist_id}/albums", {"limit": "100"})))
    if not info or not info.get("id"):
        raise LookupError(artist_id)
    return _artist_from(info), _items(albums)


async def catalog_artist(artist_id: str, owner: int) -> Result:
    """Страница артиста: фото, популярное, дискография. LookupError — такого артиста нет."""
    (artist, raw), top = await asyncio.gather(_artist_base(artist_id),
                                              _safe(_get(f"/artist/{artist_id}/top", {"limit": "25"})))
    res = Result(query=artist.name, owner=owner, artist=artist.name, artist_id=artist.id,
                 picture=artist.picture_xl or artist.picture, fans=artist.fans,
                 title=f"{artist.name} — популярное")
    res.tracks = _dedupe([_req_from_deezer(t) for t in _items(top)], SEARCH_TRACKS)
    res.albums = _discography(raw, artist)
    return res


_KIND_ORDER = {"album": 0, "ep": 1, "single": 2}


def _year_desc(t: TrackRequest) -> int:
    return -int(t.year) if (t.year or "").isdigit() else 0


async def artist_all_tracks(artist_id: str) -> tuple[Artist, list[TrackRequest], dict]:
    """Все треки артиста по дискографии без повторов (по нормализованному названию; приоритет — студийные
    альбомы, затем EP и синглы, ранние издания раньше). Не больше ARTIST_MAX_ALBUMS альбомов и
    ARTIST_MAX_TRACKS треков. Кэш в памяти на CATALOG_TTL."""
    hit = _ARTIST_TRACKS.get(artist_id)
    if hit and time.time() - hit[0] <= CATALOG_TTL:
        return hit[1], hit[2], hit[3]
    lock = _ARTIST_LOCKS.setdefault(artist_id, asyncio.Lock())
    try:
        async with lock:  # два одновременных запроса одного артиста — один проход по Deezer
            hit = _ARTIST_TRACKS.get(artist_id)
            if hit and time.time() - hit[0] <= CATALOG_TTL:
                return hit[1], hit[2], hit[3]
            artist, raw = await _artist_base(artist_id)
            albums = [a for a in _discography(raw, artist) if a.kind in _KIND_ORDER]
            # студийные альбомы раньше EP и синглов, концертники — после, ранние издания — раньше
            albums.sort(key=lambda a: (_KIND_ORDER[a.kind], bool(_LIVE.search(a.title)), a.year or "9999"))
            capped = len(albums) > ARTIST_MAX_ALBUMS
            albums = albums[:ARTIST_MAX_ALBUMS]
            datas = await asyncio.gather(*(_safe(_album_data(a.id)) for a in albums))
            seen: set[str] = set()
            out: list[TrackRequest] = []
            for al, data in zip(albums, datas):
                for t in _album_reqs(data, al) if data else []:
                    k = _title_key(t.title)
                    if k and k not in seen:
                        seen.add(k)
                        out.append(t)
                if len(out) >= ARTIST_MAX_TRACKS:
                    capped = True
                    out = out[:ARTIST_MAX_TRACKS]
                    break
            out.sort(key=lambda t: (_year_desc(t), norm(t.album), t.track_no))
            meta = {"albums": sum(1 for d in datas if d), "capped": capped}
            _prune(_ARTIST_TRACKS, 200)
            _ARTIST_TRACKS[artist_id] = (time.time(), artist, out, meta)
            return artist, out, meta
    finally:
        if not lock.locked():
            _ARTIST_LOCKS.pop(artist_id, None)
