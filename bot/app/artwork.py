"""Догрузка картинок: обложки альбомов (cover.jpg + APIC) и фото исполнителей (folder.jpg) для общего кэша,
жёсткие ссылки в личные медиатеки и прямая загрузка в Jellyfin тем альбомам/артистам, у которых нет Primary.

Трогает только cover.jpg / folder.jpg / кадр APIC — безопасно запускать параллельно с загрузками."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from mutagen.id3 import APIC, ID3, ID3NoHeaderError, Encoding

from . import http, library, metadata, tagger
from .config import CFG
from .db import DB
from .util import norm, primary_artist, safe_name, similarity

log = logging.getLogger(__name__)

DEEZER = "https://api.deezer.com"
ITUNES = "https://itunes.apple.com/search"
DEEZER_LIMIT = http.RateLimiter(0.25)    # Deezer: до 50 запросов за 5 с — держимся с большим запасом
VARIOUS = {"various artists", "various", "сборник"}
MISS_TTL = 5 * 3600                      # не нашли картинку — повторяем не раньше чем через 5 ч (цикл раз в 6 ч)

_misses: dict[str, float] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------- сравнение названий
_TRANSLIT = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d", "ł": "l", "þ": "th", "ð": "d"})


def _key(s: str | None) -> str:
    return norm((s or "").lower().translate(_TRANSLIT))


def _match(a: str | None, b: str | None) -> float:
    """0..1: насколько совпадают два названия (японские/корейские названия — одно «слово», поэтому
    кроме Jaccard по словам сравниваем строки без пробелов и вхождение одной в другую)."""
    na, nb = _key(a), _key(b)
    if not na or not nb:
        return 0.0
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if ca == cb:
        return 1.0
    short, long_ = sorted((ca, cb), key=len)
    if short in long_ and len(short) >= len(long_) * 0.5:
        return 0.8
    return similarity(na, nb)


def _is_various(name: str) -> bool:
    return _key(name) in VARIOUS


def _deezer_placeholder(url: str, md5: Any = "not-given") -> bool:
    """У Deezer без картинки отдаётся заглушка: пустой md5 и URL вида …/images/artist//1000x1000-…"""
    return not url or "/images/artist//" in url or "/images/cover//" in url or md5 == "" or md5 is None


# ---------------------------------------------------------------- теги папки
def _frame_text(tags: ID3, key: str) -> str:
    f = tags.get(key)
    if f is None or not getattr(f, "text", None):
        return ""
    return str(f.text[0]).strip()


def _mp3s(album_dir: Path) -> list[Path]:
    return sorted(p for p in album_dir.glob("*.mp3") if p.is_file())


def _album_info(album_dir: Path) -> dict[str, str]:
    """Альбом, исполнитель альбома, ID MusicBrainz и первый трек — из тегов (запасной вариант — имена папок)."""
    info = {"album": album_dir.name, "artist": album_dir.parent.name, "release": "", "group": "",
            "track_artist": "", "title": ""}
    for f in _mp3s(album_dir):
        try:
            t = ID3(str(f))
        except (ID3NoHeaderError, Exception):  # noqa: BLE001
            continue
        info["album"] = _frame_text(t, "TALB") or info["album"]
        info["artist"] = _frame_text(t, "TPE2") or _frame_text(t, "TPE1") or info["artist"]
        info["release"] = _frame_text(t, "TXXX:MusicBrainz Album Id")
        info["group"] = _frame_text(t, "TXXX:MusicBrainz Release Group Id")
        info["track_artist"] = _frame_text(t, "TPE1")
        info["title"] = _frame_text(t, "TIT2")
        break
    return info


def _embedded_cover(album_dir: Path) -> bytes | None:
    for f in _mp3s(album_dir):
        try:
            frames = ID3(str(f)).getall("APIC")
        except (ID3NoHeaderError, Exception):  # noqa: BLE001
            continue
        if frames:
            front = next((x for x in frames if x.type == 3), frames[0])
            if front.data:
                return bytes(front.data)
    return None


def _embed_apic(album_dir: Path, cover: bytes) -> int:
    """Вшивает обложку в треки папки, где её нет. Остальные кадры не трогаем. Возвращает число файлов."""
    n = 0
    for f in _mp3s(album_dir):
        try:
            try:
                tags = ID3(str(f))
            except ID3NoHeaderError:
                tags = ID3()
            if tags.getall("APIC"):
                continue
            tags.add(APIC(encoding=Encoding.UTF8, mime="image/jpeg", type=3, desc="Cover", data=cover))
            tags.save(str(f), v2_version=4, v1=0)  # запись на месте — жёсткие ссылки пользователей те же
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось вшить обложку в %s: %s", f, e)
    return n


def _write_atomic(path: Path, data: bytes) -> bytes:
    """Пишем через временный файл; если кто-то успел положить файл раньше — оставляем его."""
    if path.exists():
        return path.read_bytes()
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    if path.exists():
        tmp.unlink(missing_ok=True)
        return path.read_bytes()
    os.replace(tmp, path)
    return data


def _user_links(db: DB | None, album_dir: Path) -> list[Path]:
    """Пути пользовательских копий треков этой папки (по базе)."""
    if db is None:
        return []
    out: list[Path] = []
    for f in _mp3s(album_dir):
        t = db.track_by_path(str(f))
        if t:
            out.extend(Path(r["link_path"]) for r in db.links_for_track(t.id))
    return out


def _link_cover_to_users(db: DB | None, album_dir: Path) -> None:
    src = album_dir / "cover.jpg"
    for d in {p.parent for p in _user_links(db, album_dir)}:
        if d.is_dir():
            try:
                library._link(src, d / "cover.jpg")
            except OSError as e:
                log.warning("не удалось связать обложку в %s: %s", d, e)


# ---------------------------------------------------------------- источники обложек
async def _normalize(data: bytes | None) -> bytes | None:
    return await asyncio.to_thread(tagger._normalize_cover, data) if data else None


async def _fetch_image(url: str) -> bytes | None:
    if not url:
        return None
    return await _normalize(await http.get_bytes(url))


async def _from_caa(info: dict[str, str]) -> bytes | None:
    for kind, mbid in (("release", info["release"]), ("release-group", info["group"])):
        if mbid:
            img = await _fetch_image(f"{metadata.CAA}/{kind}/{mbid}/front-1200")
            if img:
                return img
    return None


async def _from_itunes(info: dict[str, str]) -> bytes | None:
    term = f"{info['artist']} {info['album']}".strip()
    for country in metadata.ITUNES_COUNTRIES:
        data = await http.get_json(ITUNES, params={"term": term, "entity": "album", "limit": "15",
                                                   "country": country}, limiter=http.ITUNES_LIMIT)
        best, best_sc = None, 0.0
        for r in (data or {}).get("results") or []:
            ts = _match(info["album"], metadata._clean_itunes_album(r.get("collectionName", "")))
            as_ = _match(info["artist"], r.get("artistName", ""))
            if ts >= 0.7 and as_ >= 0.6 and ts + as_ > best_sc:
                best, best_sc = r, ts + as_
        if best and best.get("artworkUrl100"):
            img = await _fetch_image(metadata._itunes_art(best["artworkUrl100"]))
            if img:
                return img
    return None


async def _deezer(path: str, q: str) -> list[dict]:
    data = await http.get_json(f"{DEEZER}{path}", params={"q": q, "limit": "25"}, limiter=DEEZER_LIMIT)
    if not isinstance(data, dict) or data.get("error"):
        return []
    return [x for x in data.get("data") or [] if isinstance(x, dict)]


async def _from_deezer_album(info: dict[str, str]) -> bytes | None:
    artist, album = info["artist"], info["album"]
    for q in (f'artist:"{artist}" album:"{album}"', f"{artist} {album}"):
        best, best_sc = None, 0.0
        for a in await _deezer("/search/album", q):
            ts = _match(album, a.get("title"))
            as_ = _match(artist, (a.get("artist") or {}).get("name"))
            url = a.get("cover_xl") or a.get("cover_big") or ""
            if ts >= 0.7 and as_ >= 0.6 and not _deezer_placeholder(url, a.get("md5_image", "not-given")) \
                    and ts + as_ > best_sc:
                best, best_sc = url, ts + as_
        if best:
            img = await _fetch_image(best)
            if img:
                return img
    return None


async def _from_deezer_album_title(info: dict[str, str]) -> bytes | None:
    """Последний шанс: поиск только по названию альбома. Нужен, когда исполнитель в тегах записан иначе,
    чем в каталоге (MusicBrainz отдаёт «Moya Mishel», Deezer знает «Моя Мишель»). Берём только точное
    совпадение названия и только если исполнитель хоть немного похож или такой альбом единственный."""
    album, artist = info["album"], info["artist"]
    if not album or len(_key(album).replace(" ", "")) < 3:
        return None
    want = _key(album).replace(" ", "")
    exact = [a for a in await _deezer("/search/album", album)
             if _key(a.get("title")).replace(" ", "") == want
             and not _deezer_placeholder(a.get("cover_xl") or "", a.get("md5_image", "not-given"))]
    if not exact:
        return None
    if len(exact) > 1:
        exact = [a for a in exact if _match(artist, (a.get("artist") or {}).get("name")) >= 0.3] or []
    if len(exact) != 1:
        return None
    return await _fetch_image(exact[0].get("cover_xl") or exact[0].get("cover_big") or "")


async def _from_deezer_track(info: dict[str, str]) -> bytes | None:
    artist, title = info["track_artist"] or info["artist"], info["title"]
    if not title:
        return None
    for t in await _deezer("/search", f'artist:"{artist}" track:"{title}"'):
        alb = t.get("album") or {}
        url = alb.get("cover_xl") or alb.get("cover_big") or ""
        if _match(title, t.get("title")) >= 0.7 and _match(artist, (t.get("artist") or {}).get("name")) >= 0.6 \
                and not _deezer_placeholder(url, alb.get("md5_image", "not-given")):
            img = await _fetch_image(url)
            if img:
                return img
    return None


async def ensure_album_cover(album_dir: Path, db: DB | None = None) -> bytes | None:
    """cover.jpg папки альбома общего кэша: уже есть — вернуть; иначе найти (APIC треков → Cover Art Archive →
    iTunes → Deezer альбом → Deezer трек), записать, связать в медиатеки пользователей и вшить в треки без APIC."""
    album_dir = Path(album_dir)
    cover_path = album_dir / "cover.jpg"
    if cover_path.exists():
        return cover_path.read_bytes()
    if not album_dir.is_dir() or not _mp3s(album_dir):
        return None
    info = await asyncio.to_thread(_album_info, album_dir)
    img = await _normalize(await asyncio.to_thread(_embedded_cover, album_dir))
    source = "APIC"
    for name, fn in (("CAA", _from_caa), ("iTunes", _from_itunes), ("Deezer", _from_deezer_album),
                     ("Deezer-трек", _from_deezer_track), ("Deezer-название", _from_deezer_album_title)):
        if img:
            break
        source = name
        try:
            img = await fn(info)
        except Exception as e:  # noqa: BLE001 — сбой одного источника не мешает остальным
            log.warning("обложка %s / %s: источник %s упал: %s", info["artist"], info["album"], name, e)
    if not img:
        log.info("обложка не найдена: %s / %s", info["artist"], info["album"])
        return None
    img = await asyncio.to_thread(_write_atomic, cover_path, img)
    await asyncio.to_thread(_link_cover_to_users, db, album_dir)
    n = await asyncio.to_thread(_embed_apic, album_dir, img)
    log.info("обложка (%s): %s / %s, вшита в %d треков", source, info["artist"], info["album"], n)
    return img


# ---------------------------------------------------------------- фото исполнителей
async def fetch_artist_image(name: str) -> bytes | None:
    """Фото исполнителя с Deezer: имя совпадает (с точностью до регистра/диакритики) или очень похоже;
    из нескольких кандидатов — самый популярный. Заглушку Deezer отбрасываем."""
    if not name or _is_various(name):
        return None
    want = _key(name)
    cands = []
    for a in await _deezer("/search/artist", name):
        got = _key(a.get("name"))
        exact = bool(got) and got.replace(" ", "") == want.replace(" ", "")
        if not exact and similarity(want, got) < 0.85:
            continue
        url = a.get("picture_xl") or a.get("picture_big") or ""
        if _deezer_placeholder(url, a.get("md5_image", "not-given")):
            continue
        cands.append((exact, int(a.get("nb_fan") or 0), url))
    for _exact, _fans, url in sorted(cands, reverse=True):
        img = await _fetch_image(url)
        if img:
            return img
    # «Filatov & Karas, Burito», «A feat. B» — фото первого исполнителя лучше, чем ничего
    first = primary_artist(name)
    if first and _key(first) != want:
        return await fetch_artist_image(first)
    return None


def _artist_name(artist_dir: Path) -> str:
    for f in sorted(artist_dir.rglob("*.mp3")):
        try:
            t = ID3(str(f))
        except (ID3NoHeaderError, Exception):  # noqa: BLE001
            continue
        name = _frame_text(t, "TPE2")
        if name:
            return name
    return artist_dir.name


def _link_artist_to_users(db: DB | None, artist_dir: Path) -> None:
    if db is None:
        return
    src = artist_dir / "folder.jpg"
    dirs: set[Path] = set()
    for album in (d for d in artist_dir.iterdir() if d.is_dir()):
        for p in _user_links(db, album):
            if p.parent.parent.name == artist_dir.name:
                dirs.add(p.parent.parent)
    for d in dirs:
        if d.is_dir():
            try:
                library._link(src, d / "folder.jpg")
            except OSError as e:
                log.warning("не удалось связать фото исполнителя в %s: %s", d, e)


async def ensure_artist_image(artist_dir: Path, db: DB | None = None) -> bytes | None:
    """folder.jpg папки исполнителя общего кэша (+ жёсткие ссылки в медиатеки, где есть его треки)."""
    artist_dir = Path(artist_dir)
    path = artist_dir / "folder.jpg"
    if path.exists():
        return path.read_bytes()
    if not artist_dir.is_dir() or _is_various(artist_dir.name):
        return None
    name = await asyncio.to_thread(_artist_name, artist_dir)
    if _is_various(name):
        return None
    img = await fetch_artist_image(name)
    if not img:
        log.info("фото исполнителя не найдено: %s", name)
        return None
    img = await asyncio.to_thread(_write_atomic, path, img)
    await asyncio.to_thread(_link_artist_to_users, db, artist_dir)
    log.info("фото исполнителя: %s", name)
    return img


# ---------------------------------------------------------------- Jellyfin
def _recently_missed(key: str) -> bool:
    t = _misses.get(key)
    return t is not None and time.monotonic() - t < MISS_TTL


async def _cached_try(key: str, coro_fn, ready: Path | None = None) -> bytes | None:
    """Повторно не ищем то, что недавно не нашлось (иначе каждый прогон — десятки внешних запросов).
    ready — файл картинки: если он уже появился, промах не помеха."""
    if _recently_missed(key) and not (ready is not None and ready.exists()):
        return None
    try:
        img = await coro_fn()
    except Exception as e:  # noqa: BLE001
        log.warning("поиск картинки %s упал: %s", key, e)
        img = None
    if img:
        _misses.pop(key, None)
    else:
        _misses[key] = time.monotonic()
    return img


def cache_dir_for_jf_path(jf_path: str) -> Path | None:
    """/media/users/<login>/<Artist>/<Album> -> CFG.library_dir/<Artist>/<Album>."""
    if not jf_path:
        return None
    p = PurePosixPath(jf_path.replace("\\", "/"))
    users = PurePosixPath(CFG.users_dir.as_posix())
    try:
        rel = p.relative_to(users)
    except ValueError:
        parts = p.parts
        if "users" not in parts:
            return None
        rel = PurePosixPath(*parts[parts.index("users") + 1:])
    if len(rel.parts) < 2:
        return None
    return CFG.library_dir.joinpath(*rel.parts[1:])


def _artist_dir_by_name(name: str) -> Path | None:
    d = CFG.library_dir / safe_name(name)
    if d.is_dir():
        return d
    want = _key(name)
    if not want or not CFG.library_dir.is_dir():
        return None
    for x in CFG.library_dir.iterdir():
        if x.is_dir() and _key(x.name) == want:
            return x
    return None


def _missing_primary(item: dict) -> bool:
    return not (item.get("ImageTags") or {}).get("Primary")


async def _jf_artists(jf, user_id: str) -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        data = await jf.request("GET", "/Artists", params={"userId": user_id, "StartIndex": start, "Limit": 500,
                                                           "Fields": "Path", "EnableImages": True})
        data = data if isinstance(data, dict) else {}
        items = data.get("Items") or []
        out.extend(items)
        start += len(items)
        if not items or start >= data.get("TotalRecordCount", start):
            return out


async def sync_jellyfin_images(db: DB, jf) -> dict[str, int]:
    """Альбомам и исполнителям Jellyfin без Primary — загрузить картинку напрямую (POST /Items/{id}/Images)."""
    counts = {"albums": 0, "artists": 0, "albums_missing": 0, "artists_missing": 0, "errors": 0}
    done: set[str] = set()
    for u in db.all_users():
        if not u.jf_user_id:
            continue
        try:
            albums = await jf.items(u.jf_user_id, IncludeItemTypes="MusicAlbum", Recursive=True, Fields="Path")
        except Exception as e:  # noqa: BLE001
            log.warning("Jellyfin: не удалось получить альбомы %s: %s", u.login, e)
            counts["errors"] += 1
            albums = []
        for it in albums:
            iid = it.get("Id")
            if not iid or iid in done or not _missing_primary(it):
                continue
            done.add(iid)
            folder = cache_dir_for_jf_path(it.get("Path") or "")
            if folder is None or not folder.is_dir():
                log.debug("Jellyfin-альбом %s: папка %s не найдена в кэше", it.get("Name"), it.get("Path"))
                counts["albums_missing"] += 1
                continue
            img = await _cached_try(f"album:{folder}", lambda f=folder: ensure_album_cover(f, db),
                                    folder / "cover.jpg")
            if not img:
                counts["albums_missing"] += 1
                continue
            try:
                await jf.upload_image(iid, img)
                counts["albums"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Jellyfin: не удалось загрузить обложку %s: %s", it.get("Name"), e)
                counts["errors"] += 1
        try:
            artists = await _jf_artists(jf, u.jf_user_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Jellyfin: не удалось получить исполнителей %s: %s", u.login, e)
            counts["errors"] += 1
            artists = []
        for it in artists:
            iid, name = it.get("Id"), it.get("Name") or ""
            if not iid or iid in done or not _missing_primary(it) or not name or _is_various(name):
                continue
            done.add(iid)
            d = _artist_dir_by_name(name)
            if d is not None:
                img = await _cached_try(f"artist:{d}", lambda d=d: ensure_artist_image(d, db),
                                         d / "folder.jpg")
            else:  # исполнитель только из TPE1 (feat.) — папки нет, картинку загружаем без файла
                img = await _cached_try(f"artist-name:{_key(name)}", lambda n=name: fetch_artist_image(n))
            if not img:
                counts["artists_missing"] += 1
                continue
            try:
                await jf.upload_image(iid, img)
                counts["artists"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Jellyfin: не удалось загрузить фото %s: %s", name, e)
                counts["errors"] += 1
    if counts["albums"] or counts["artists"]:
        log.info("Jellyfin: загружено обложек %d, фото исполнителей %d", counts["albums"], counts["artists"])
    return counts


# ---------------------------------------------------------------- полный проход
def _scan_missing() -> tuple[list[Path], list[Path]]:
    albums: list[Path] = []
    artists: list[Path] = []
    if not CFG.library_dir.is_dir():
        return albums, artists
    for a in sorted(p for p in CFG.library_dir.iterdir() if p.is_dir()):
        if not (a / "folder.jpg").exists() and not _is_various(a.name):
            artists.append(a)
        for al in sorted(p for p in a.iterdir() if p.is_dir()):
            if not (al / "cover.jpg").exists() and any(al.glob("*.mp3")):
                albums.append(al)
    return albums, artists


async def backfill(db: DB, jf=None) -> dict[str, int]:
    """Обложки всем папкам кэша без cover.jpg, фото всем исполнителям без folder.jpg, затем — Jellyfin."""
    async with _lock:  # отложенный запуск после загрузки и цикл раз в 6 ч не должны идти одновременно
        counts = {"covers": 0, "artist_images": 0}
        try:
            albums, artists = await asyncio.to_thread(_scan_missing)
        except Exception as e:  # noqa: BLE001
            log.warning("artwork: не удалось просмотреть кэш: %s", e)
            albums, artists = [], []
        for al in albums:
            if await _cached_try(f"album:{al}", lambda al=al: ensure_album_cover(al, db)):
                counts["covers"] += 1
        for a in artists:
            if await _cached_try(f"artist:{a}", lambda a=a: ensure_artist_image(a, db)):
                counts["artist_images"] += 1
        if jf is not None:
            try:
                counts.update(await sync_jellyfin_images(db, jf))
            except Exception as e:  # noqa: BLE001
                log.warning("artwork: синхронизация с Jellyfin не удалась: %s", e)
        if counts["covers"] or counts["artist_images"]:
            log.info("artwork: новых обложек %d, фото исполнителей %d", counts["covers"], counts["artist_images"])
        return counts


def needs_artwork(track_path: str | Path) -> bool:
    """После загрузки: у альбома нет cover.jpg или у исполнителя нет folder.jpg."""
    p = Path(track_path)
    album, artist = p.parent, p.parent.parent
    if not (album / "cover.jpg").exists():
        return True
    return not _is_various(artist.name) and not (artist / "folder.jpg").exists()
