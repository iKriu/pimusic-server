"""Общий кэш треков и личные медиатеки через жёсткие ссылки."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import metadata, tagger
from .config import CFG
from .db import DB, Track
from .metadata import Hint, TrackMeta
from mutagen.id3 import ID3

from .util import (
    normalize_genres, year_from,
    clean_title, match_key, mp3_is_cbr320, probe, safe_name, split_artist_title, strip_feat, transcode_mp3_320,
)

log = logging.getLogger(__name__)

# одновременно размечаем не больше двух файлов — MusicBrainz всё равно ограничен 1 запросом/с
_TAG_SEM = asyncio.Semaphore(2)
_PATH_LOCK = asyncio.Lock()
_PLACE_LOCK = threading.Lock()  # та же секция в потоке: страховка, если корутину отменили, а поток ещё работает


class BadAudio(RuntimeError):
    """Файл не читается/не перекодируется — стоит попробовать другой источник."""


@dataclass
class IngestResult:
    track: Track
    reused: bool
    meta: TrackMeta | None
    info: dict[str, bool]
    link_path: Path
    replaced: bool = False  # файл трека в кэше заменён новым (перекачка проблемного / своего файла)


def ensure_dirs() -> None:
    for d in (CFG.library_dir, CFG.users_dir, CFG.radio_dir, CFG.incoming_dir, CFG.slskd_downloads_dir,
              CFG.incoming_dir / "work", CFG.share_dir):
        d.mkdir(parents=True, exist_ok=True)


def user_root(login: str) -> Path:
    return CFG.users_dir / login


def work_dir() -> Path:
    d = CFG.incoming_dir / "work" / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _album_folder(meta: TrackMeta) -> str:
    # Без года: треки одного альбома нередко размечаются с разным годом (год первого выпуска по MusicBrainz,
    # год издания по iTunes, синглы раньше альбома), и папки «Альбом (2010)» / «Альбом (2011)» Jellyfin
    # показывал как разные альбомы. Год хранится в тегах.
    return safe_name(meta.album)


_YEAR_SUFFIX = re.compile(r"\s\(\d{4}\)$")


def merge_artist_variants(db: DB) -> int:
    """Папки одного исполнителя, отличающиеся регистром/знаками («Panic! At The Disco» / «Panic! At the Disco»)
    и «X feat. Y» -> одна папка (та, где больше треков). Альбомы переносятся целиком."""
    if not CFG.library_dir.exists():
        return 0
    moved = 0
    dirs = [d for d in CFG.library_dir.iterdir() if d.is_dir()]
    groups: dict[str, list[Path]] = {}
    for d in dirs:
        key = _artist_key(strip_feat(d.name) or d.name)
        groups.setdefault(key or f"\0{d.name}", []).append(d)  # пустой ключ («!!!») — только сама папка
    for ds in groups.values():
        main = max(ds, key=lambda d: (len(list(d.rglob("*.mp3"))), " feat" not in d.name.lower()))
        clean = safe_name(strip_feat(main.name) or main.name)
        if clean != main.name:
            # «Raving George feat. Oscar and the Wolf» -> «Raving George» (папка исполнителя без «feat.»)
            target = CFG.library_dir / clean
            target.mkdir(exist_ok=True)
            ds = ds + [target] if target not in ds else ds
            main = target
        if len(ds) < 2:
            continue
        for d in ds:
            if d == main:
                continue
            for album in [a for a in d.iterdir() if a.is_dir()]:
                target = _existing_album_dir(main, album.name)
                target.mkdir(parents=True, exist_ok=True)
                ref = next(iter(sorted(target.glob("*.mp3"))), None)
                for f in sorted(album.glob("*.mp3")):
                    moved += _move_track_file(db, f, target, ref)
                cover = album / "cover.jpg"
                if cover.exists() and not (target / "cover.jpg").exists():
                    shutil.move(str(cover), target / "cover.jpg")
                shutil.rmtree(album, ignore_errors=True)
            if (d / "folder.jpg").exists() and not (main / "folder.jpg").exists():
                shutil.move(str(d / "folder.jpg"), main / "folder.jpg")
            shutil.rmtree(d, ignore_errors=True)
    return moved


def merge_compilation_splits(db: DB) -> int:
    """Один альбом в «Various Artists» и у отдельных исполнителей (Sing 2 OST: 6 треков у VA + по одному у
    Tori Kelly / Taron Egerton …) -> собрать в папку, где треков больше; теги альбома выровнять по ней."""
    va = _existing_artist_dir(VARIOUS)
    if not va.is_dir():
        return 0
    moved = 0
    for va_album in [a for a in va.iterdir() if a.is_dir()]:
        key = _album_key(va_album.name)
        if not key:  # «???» — сравнивать не с чем
            continue
        group = [va_album] + [a for d in CFG.library_dir.iterdir() if d.is_dir() and d != va
                              for a in d.iterdir() if a.is_dir() and _album_key(a.name) == key
                              and _same_album_tags(a, va_album)]
        if len(group) < 2:
            continue
        main = max(group, key=lambda a: (len(list(a.glob("*.mp3"))), a == va_album))
        ref = next(iter(sorted(main.glob("*.mp3"))), None)
        for a in group:
            if a == main:
                continue
            for f in sorted(a.glob("*.mp3")):
                moved += _move_track_file(db, f, main, ref)
            cover = a / "cover.jpg"
            if cover.exists() and not (main / "cover.jpg").exists():
                shutil.move(str(cover), main / "cover.jpg")
            shutil.rmtree(a, ignore_errors=True)
            try:
                a.parent.rmdir()  # исполнитель без других альбомов
            except OSError:
                pass
    return moved


def _same_album_tags(a: Path, b: Path) -> bool:
    """Защита от совпадения названий у разных альбомов: совпадает MB Album Id/Release Group, а если тегов нет —
    название альбома из тегов идентично."""
    def info(d: Path) -> tuple[str, str, str]:
        for f in sorted(d.glob("*.mp3")):
            try:
                t = ID3(str(f))
            except Exception:  # noqa: BLE001
                continue
            g = lambda k: str(t.get(k).text[0]) if t.get(k) else ""  # noqa: E731
            return g("TXXX:MusicBrainz Album Id"), g("TXXX:MusicBrainz Release Group Id"), g("TALB")
        return "", "", ""
    ia, ib = info(a), info(b)
    if ia[0] and ia[0] == ib[0]:
        return True
    if ia[1] and ia[1] == ib[1]:
        return True
    if (ia[0] and ib[0]) or (ia[1] and ib[1]):
        return False  # у обоих есть ID MusicBrainz, и они разные — это разные альбомы («30» Adele и «30» V $)
    return bool(ia[2]) and ia[2].strip().lower() == ib[2].strip().lower()


def merge_split_albums(db: DB) -> int:
    """Однократная миграция: «Альбом (2010)», «Альбом (2011)», «Альбом» одного исполнителя -> одна папка «Альбом».
    Переносит файлы общего кэша, пересоздаёт жёсткие ссылки в медиатеках, обновляет пути в БД и выравнивает
    теги альбома по папке, где треков больше всего. Возвращает число перенесённых треков."""
    moved = 0
    if not CFG.library_dir.exists():
        return 0
    for artist_dir in sorted(p for p in CFG.library_dir.iterdir() if p.is_dir()):
        groups: dict[str, list[Path]] = {}
        for d in sorted(p for p in artist_dir.iterdir() if p.is_dir()):
            groups.setdefault(_album_key(d.name) or f"\0{d.name}", []).append(d)
        for dirs in groups.values():
            # итоговое имя — самой «наполненной» папки без года
            main = max(dirs, key=lambda d: (len(list(d.glob("*.mp3"))), not _YEAR_SUFFIX.search(d.name)))
            target = artist_dir / _YEAR_SUFFIX.sub("", main.name)
            if dirs == [target]:
                continue
            ref_dir = max(dirs, key=lambda d: len(list(d.glob("*.mp3"))))
            ref = next(iter(sorted(ref_dir.glob("*.mp3"))), None)
            target.mkdir(exist_ok=True)
            for d in dirs:
                for f in sorted(d.glob("*.mp3")):
                    moved += _move_track_file(db, f, target, ref)
                cover = d / "cover.jpg"
                if cover.exists() and not (target / "cover.jpg").exists():
                    shutil.move(str(cover), target / "cover.jpg")
                if d != target:
                    shutil.rmtree(d, ignore_errors=True)
            # обложку из общего кэша — во все медиатеки, где есть этот альбом
            if (target / "cover.jpg").exists():
                for f in target.glob("*.mp3"):
                    t = db.track_by_path(str(f))
                    for r in db.links_for_track(t.id) if t else []:
                        dst = Path(r["link_path"]).parent / "cover.jpg"
                        if not dst.exists():
                            _link(target / "cover.jpg", dst)
    if moved:
        log.info("объединены разделённые альбомы: перенесено треков %d", moved)
    return moved


def _move_track_file(db: DB, f: Path, target: Path, ref: Path | None) -> int:
    track = db.track_by_path(str(f))
    if ref is not None and ref != f:
        try:
            tagger.harmonize_album_tags(f, ref)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось выровнять теги %s: %s", f, e)
    if f.parent == target:
        return 0
    dst = _unique(target / f.name)
    shutil.move(str(f), dst)
    for ext in (".lrc", ".txt"):
        side = f.with_suffix(ext)
        if side.exists():
            shutil.move(str(side), dst.with_suffix(ext))
    if not track:
        return 1
    db.set_track_path(track.id, str(dst))
    rel = dst.relative_to(CFG.library_dir)
    for r in db.links_for_track(track.id):
        old = Path(r["link_path"])
        new = user_root(r["login"]) / rel
        _link(dst, new)
        for ext in (".lrc", ".txt"):
            if dst.with_suffix(ext).exists():
                _link(dst.with_suffix(ext), new.with_suffix(ext))
        if old != new:
            for x in (old, old.with_suffix(".lrc"), old.with_suffix(".txt")):
                x.unlink(missing_ok=True)
            _remove_empty_dirs(old.parent, user_root(r["login"]))
        db.link_track(r["login"], track.id, str(new))
    return 1


def _remove_empty_dirs(d: Path, stop: Path) -> None:
    while d != stop and d.is_dir():
        leftovers = [x for x in d.iterdir() if x.name != "cover.jpg"]
        if leftovers:
            return
        shutil.rmtree(d, ignore_errors=True)
        d = d.parent


VARIOUS = "Various Artists"


def _artist_key(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.lower())


def _existing_artist_dir(name: str) -> Path:
    """«Panic! At The Disco» и «Panic! At the Disco» — одна папка (на Linux регистр различается)."""
    target = CFG.library_dir / name
    if target.exists() or not CFG.library_dir.is_dir():
        return target
    key = _artist_key(name)
    if not key:
        return target
    for d in CFG.library_dir.iterdir():
        if d.is_dir() and _artist_key(d.name) == key:
            return d
    return target


def cache_path_for(meta: TrackMeta) -> Path:
    # исполнитель альбома без «feat. …»: «Raving George feat. Oscar and the Wolf» -> «Raving George»
    album_artist = strip_feat(meta.album_artist or meta.artist) or meta.album_artist or meta.artist
    artist_dir = _existing_artist_dir(safe_name(VARIOUS if meta.compilation else album_artist))
    if meta.track_no:
        prefix = f"{meta.disc_no}-{meta.track_no:02d} " if (meta.disc_total or 1) > 1 else f"{meta.track_no:02d} "
    else:
        prefix = ""
    fname = prefix + (safe_name(f"{meta.artist} - {meta.title}") if meta.compilation else safe_name(meta.title))
    folder = _album_folder(meta)
    album_dir = _existing_album_dir(artist_dir, folder)
    if not album_dir.exists():
        # Один и тот же альбом не должен расходиться между «Various Artists» и исполнителем
        # (саундтрек, где у части треков источник не отметил «сборник», или наоборот)
        other = _existing_artist_dir(safe_name(album_artist if meta.compilation else VARIOUS))
        cand = _existing_album_dir(other, folder)
        if cand.exists():
            album_dir = cand
    return album_dir / f"{fname}.mp3"


def _album_key(name: str) -> str:
    """«American Beauty / American Psycho» == «American Beauty/American Psycho» == «american beauty_american psycho».
    Скобки с Deluxe/Remaster не убираем: это другие издания."""
    return re.sub(r"[\W_]+", "", _YEAR_SUFFIX.sub("", name).lower())


def _existing_album_dir(artist_dir: Path, folder: str) -> Path:
    """Если у исполнителя уже есть папка этого альбома с чуть иным написанием — кладём трек туда."""
    target = artist_dir / folder
    if target.exists() or not artist_dir.is_dir():
        return target
    key = _album_key(folder)
    if not key:
        return target
    for d in artist_dir.iterdir():
        if d.is_dir() and _album_key(d.name) == key:
            return d
    return target


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 100):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem} {uuid.uuid4().hex[:6]}{path.suffix}")


def _link(src: Path, dst: Path) -> None:
    if dst.exists():
        try:
            if os.path.samefile(src, dst):
                return
        except OSError:
            pass
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as e:  # другая ФС — запасной вариант, но этого не должно быть при правильной установке
        log.warning("hardlink failed (%s), copying %s", e, src)
        shutil.copy2(src, dst)


class TrackGone(LookupError):
    """Трек удалили из общего кэша, пока его выдавали пользователю."""


def user_link_path(track: Track, login: str) -> Path:
    return user_root(login) / Path(track.path).relative_to(CFG.library_dir)


def attach_to_user(db: DB, track: Track, login: str) -> Path:
    """Выдать трек из кэша пользователю без гонки с очисткой кэша: сначала строка в БД (в транзакции, если трек
    ещё есть), потом жёсткие ссылки; ошибка файловой системы — строка откатывается."""
    dst = user_link_path(track, login)
    ok, existed = db.link_track_if_exists(login, track.id, str(dst))
    if not ok:
        raise TrackGone(track.id)
    try:
        return link_to_user(track, login)
    except OSError:
        if not existed:
            db.unlink_track(login, track.id)
        raise


def link_to_user(track: Track, login: str) -> Path:
    """Пробрасывает трек (и cover.jpg, .lrc) из общего кэша в папку пользователя жёсткими ссылками."""
    src = Path(track.path)
    rel = src.relative_to(CFG.library_dir)
    dst = user_root(login) / rel
    _link(src, dst)
    for side in (src.parent / "cover.jpg", src.with_suffix(".lrc"), src.with_suffix(".txt")):
        if side.exists():
            _link(side, dst.parent / side.name if side.name == "cover.jpg" else dst.with_suffix(side.suffix))
    return dst


def unlink_from_user(db: DB, track: Track, login: str) -> None:
    link = db.user_has_track(login, track.id)
    if link:
        p = Path(link)
        for f in (p, p.with_suffix(".lrc"), p.with_suffix(".txt")):
            f.unlink(missing_ok=True)
        d = p.parent
        if not any(x.suffix == ".mp3" for x in d.glob("*.mp3")):
            (d / "cover.jpg").unlink(missing_ok=True)
            for parent in (d, d.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break
    db.unlink_track(login, track.id)
    purge_if_unused(db, track)


# ------------------------------------------------------------------ раздача Soulseek (/media/share)
# slskd раздаёт не весь кэш (там и личные загрузки пользователей), а отдельную папку с жёсткими ссылками на треки
# кэша, у которых source != 'upload'. Синхронизируется при загрузке/удалении/замене и полностью — при старте.
def share_path(track_path: str | Path) -> Path | None:
    try:
        return CFG.share_dir / Path(track_path).relative_to(CFG.library_dir)
    except ValueError:
        return None


def share_add(track: Track) -> None:
    """Трек — в раздачу (личную загрузку — наоборот, убрать). Ошибки только в лог: раздача не главное."""
    if track.source == "upload":
        share_remove(track.path)
        return
    dst = share_path(track.path)
    if dst is None:
        return
    try:
        _link(Path(track.path), dst)
    except OSError as e:
        log.warning("раздача Soulseek: не удалось связать %s: %s", track.path, e)


def share_remove(track_path: str | Path) -> None:
    dst = share_path(track_path)
    if dst is None:
        return
    dst.unlink(missing_ok=True)
    d = dst.parent
    while d != CFG.share_dir and CFG.share_dir in d.parents:
        try:
            d.rmdir()  # только пустые папки
        except OSError:
            break
        d = d.parent


def rebuild_share(db: DB) -> tuple[int, int]:
    """Полная сверка раздачи с базой (при старте): добавить недостающее, убрать лишнее. (добавлено, убрано)."""
    want: dict[Path, Path] = {}
    for t in db.tracks_after(0, 10 ** 9):
        dst = share_path(t.path)
        if dst is not None and t.source != "upload" and Path(t.path).is_file():
            want[dst] = Path(t.path)
    added = removed = 0
    for dst, src in want.items():
        try:
            if not dst.exists() or not os.path.samefile(src, dst):
                _link(src, dst)
                added += 1
        except OSError as e:
            log.warning("раздача Soulseek: %s: %s", dst, e)
    root = CFG.share_dir
    if root.is_dir():
        for f in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
            try:
                if f.is_file() and f not in want:
                    f.unlink()
                    removed += 1
                elif f.is_dir() and not any(f.iterdir()):
                    f.rmdir()
            except OSError:
                continue
    return added, removed


def purge_if_unused(db: DB, track: Track) -> bool:
    """Файл общего кэша удаляется, когда трека нет ни в одной медиатеке; из плейлистов он уходит вместе с файлом."""
    # проверка и удаление строки — одна транзакция; файлы трогаем, только если строку удалили мы
    if not db.delete_track_if_unused(track.id):
        return False
    share_remove(track.path)
    src = Path(track.path)
    for f in (src, src.with_suffix(".lrc"), src.with_suffix(".txt")):
        f.unlink(missing_ok=True)
    d = src.parent
    try:
        if d.is_dir() and not any(d.glob("*.mp3")):
            (d / "cover.jpg").unlink(missing_ok=True)  # обложка альбома без треков; чужие файлы не трогаем
            d.rmdir()        # только пустые папки
            d.parent.rmdir()
    except OSError:
        pass
    return True


def _different_recording(t: Track, mbid: str | None, isrc: str | None) -> bool:
    """Совпало только «артист|название», но ISRC или MBID другой — это другая версия («Levitating» и
    «Levitating (feat. DaBaby)», альбомная и сингловая редакция), а не дубликат."""
    return bool((isrc and t.isrc and t.isrc != isrc) or (mbid and t.mbid and t.mbid != mbid))


def find_same_recording(db: DB, *, artist: str, title: str, mbid: str | None = None, isrc: str | None = None,
                        alias_key: str | None = None) -> Track | None:
    for t in (db.find_track(mbid=mbid) if mbid else None, db.find_track(isrc=isrc) if isrc else None):
        if t and Path(t.path).exists():
            return t
    key = match_key(artist, title) if artist else ""
    for k in (alias_key, key):
        if not usable_key(k):  # «!!!|???» после нормализации — «|»: совпадёт с чем угодно
            continue
        t = db.find_track(alias_key=k) or db.find_track(match_key=k)
        if t and Path(t.path).exists() and not _different_recording(t, mbid, isrc):
            return t
    return None


def usable_key(k: str | None) -> bool:
    """Ключ «артист|название», у которого обе части не пустые после нормализации."""
    if not k:
        return False
    a, _, t = k.partition("|")
    return bool(a.strip()) and bool(t.strip())


def lookup_cached(db: DB, artist: str, title: str, *, isrc: str = "") -> Track | None:
    return find_same_recording(db, artist=artist, title=title, isrc=isrc or None)


def _hint_from_file(hint: Hint, tags: dict[str, str], src: Path) -> Hint:
    h = Hint(**hint.__dict__)
    if not h.artist:
        h.artist = tags.get("artist") or tags.get("album_artist") or ""
    if not h.title:
        h.title = tags.get("title") or ""
    if not h.album:
        h.album = tags.get("album") or ""
    if not h.isrc:
        h.isrc = tags.get("isrc") or tags.get("tsrc") or ""
    if not h.title:
        stem = re.sub(r"^\s*\d{1,3}\s*[.\-_)]?\s+", "", src.stem)  # «011. Artist - Title» -> «Artist - Title»
        a, t = split_artist_title(clean_title(stem))
        h.artist = h.artist or a
        h.title = t
    h.title = clean_title(h.title)
    return h


def _take_mp3(src: Path, mp3: Path, keep_mp3: bool) -> None:
    """Готовый MP3 переезжает во временную папку (в потоке: на другой ФС это копирование)."""
    shutil.move(str(src), mp3)
    if keep_mp3:  # свой файл пользователя: APEv2-теги (с картинками любого размера) — прочь, ID3 перепишет tagger
        try:
            from mutagen.apev2 import delete as ape_delete
            ape_delete(str(mp3))
        except Exception:  # noqa: BLE001
            pass


def _prepare_target(meta: TrackMeta, work: Path) -> tuple[Path, Path]:
    """Путь в кэше (обход папок исполнителей) и копия обложки альбома в рабочую папку. (target, cover.jpg)."""
    target = cache_path_for(meta)
    cached_cover = target.parent / "cover.jpg"
    if cached_cover.exists():
        shutil.copy2(cached_cover, work / "cover.jpg")
    return target, cached_cover


def _place(db: DB, *, mp3: Path, work: Path, target: Path, cached_cover: Path, meta: TrackMeta, info, hint: Hint,
           source: str, login: str, alias: str, replace_id: int | None = None) -> tuple[Track, bool, Path | None, bool]:
    """Критическая секция ingest (в потоке, под _PATH_LOCK): проверка дубликата и вставка атомарно, иначе два
    воркера положат один трек дважды. (трек, уже был в кэше, ссылка пользователя — для повтора; None у нового,
    файл в кэше заменён)."""
    with _PLACE_LOCK:
        return _place_locked(db, mp3=mp3, work=work, target=target, cached_cover=cached_cover, meta=meta, info=info,
                             hint=hint, source=source, login=login, alias=alias, replace_id=replace_id)


def _place_locked(db: DB, *, mp3: Path, work: Path, target: Path, cached_cover: Path, meta: TrackMeta, info,
                  hint: Hint, source: str, login: str, alias: str,
                  replace_id: int | None = None) -> tuple[Track, bool, Path | None, bool]:
    existing = db.get_track(replace_id) if replace_id else None  # «Перекачать трек»: заменить именно этот файл
    if existing is None or not Path(existing.path).exists():
        existing = find_same_recording(db, artist=meta.artist, title=meta.title,
                                       mbid=meta.mb_recording_id or None, isrc=meta.isrc or hint.isrc or None)
    if existing and Path(existing.path).exists():
        replaced = False
        if source == "upload" or existing.broken or existing.id == replace_id:
            # файл, загруженный вручную, главнее любого скачанного; скачанный заменяет только проблемный файл или
            # тот, что просили перекачать, — файл подменяется у всех держателей
            replace_track_file(db, existing, mp3, source, duration=int(info.duration or 0) or None)
            existing = db.get_track(existing.id) or existing
            replaced = True
        try:
            link = attach_to_user(db, existing, login)
        except TrackGone:  # кэш очистили между поиском и выдачей — кладём файл как новый трек
            pass
        else:
            db.add_alias(alias, existing.id)
            return existing, True, link, replaced
    dst = _unique(target)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # трек ложится в уже существующий альбом — берём год/ID релиза/исполнителя альбома у соседей,
    # иначе Jellyfin может показать треки одного альбома как разные альбомы
    ref = next(iter(sorted(dst.parent.glob("*.mp3"))), None)
    if ref is not None:
        try:
            tagger.harmonize_album_tags(mp3, ref)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось выровнять теги альбома: %s", e)
    try:
        for ext in (".lrc", ".txt"):
            side = mp3.with_suffix(ext)
            if side.exists():
                shutil.move(str(side), dst.with_suffix(ext))
        if (work / "cover.jpg").exists() and not cached_cover.exists():
            shutil.move(str(work / "cover.jpg"), cached_cover)
        shutil.move(str(mp3), dst)
        tid = db.add_track(match_key=match_key(meta.artist, meta.title), mbid=meta.mb_recording_id or None,
                           isrc=meta.isrc or None, artist=meta.artist, title=meta.title, album=meta.album,
                           # длительность — настоящая длина файла, а не каталога (они могут расходиться)
                           duration=int(info.duration or meta.duration or 0), path=str(dst), source=source,
                           genres=normalize_genres(meta.genres) or normalize_genres(info.tags.get("genre")),
                           year=year_from(meta.date) or year_from(info.tags.get("date")))
    except BaseException:
        for f in (dst, dst.with_suffix(".lrc"), dst.with_suffix(".txt")):
            f.unlink(missing_ok=True)
        raise
    db.add_alias(alias, tid)
    track = db.track_by_path(str(dst))
    assert track is not None
    return track, False, None, False


def _finish_new(db: DB, track: Track, login: str) -> Path:
    """Новый трек в кэше: строка пользователя и ссылка (attach_to_user: сначала БД, потом ссылка), раздача."""
    link = attach_to_user(db, track, login)
    share_add(track)
    return link


def duration_mismatch(expected: float | None, actual: float | None) -> bool:
    """Файл заметно другой длины, чем трек в каталоге: другая версия (клип с вставками, укороченная редакция,
    чужая песня). Допуск — 10 с или 7 % длины."""
    if not expected or not actual:
        return False
    return abs(actual - expected) > max(10.0, 0.07 * expected)


async def ingest(db: DB, src: Path, hint: Hint, *, source: str, login: str, keep_mp3: bool = False,
                 replace_id: int | None = None) -> IngestResult:
    """Файл из любого источника -> MP3 CBR 320 -> теги -> общий кэш -> жёсткая ссылка пользователю.
    keep_mp3 — свой файл пользователя: MP3 любого битрейта не перекодируется (апскейл качества не добавит).
    replace_id — «Перекачать трек»: новый файл заменяет файл этого трека в кэше у всех держателей.
    Скачанный файл другой длины, чем ожидалось (запрос или найденный каталог), — BadAudio: следующий источник.
    Файловые операции и запросы к БД — в потоках (asyncio.to_thread), event loop не блокируется."""
    info = await probe(src)
    if info is None:
        raise BadAudio("файл не читается ffprobe")
    if source != "upload" and duration_mismatch(hint.duration, info.duration):
        raise BadAudio(f"длительность {info.duration:.0f} с, а у трека {hint.duration:.0f} с — другая версия")
    hint = _hint_from_file(hint, info.tags, src)
    if not hint.duration:
        hint.duration = info.duration

    # свой файл пользователя: его обложка и текст — запасной вариант, если в сети ничего не найдётся
    own = await asyncio.to_thread(tagger.read_own_art, src) if source == "upload" else None
    work = await asyncio.to_thread(work_dir)
    try:
        mp3 = work / "track.mp3"
        if info.codec == "mp3" and src.suffix.lower() == ".mp3" and (keep_mp3 or await mp3_is_cbr320(src)):
            await asyncio.to_thread(_take_mp3, src, mp3, keep_mp3)
        else:
            if not await transcode_mp3_320(src, mp3):
                raise BadAudio(f"не удалось перекодировать {info.codec} в MP3 320")
            await asyncio.to_thread(src.unlink, missing_ok=True)

        alias = match_key(hint.artist, hint.title) if hint.artist else ""
        async with _TAG_SEM:
            meta = await metadata.resolve(hint)
            if source != "upload" and meta.matched != "none" and duration_mismatch(meta.duration, info.duration):
                raise BadAudio(f"длительность {info.duration:.0f} с, а в каталоге {meta.duration:.0f} с "
                               "— другая версия")
            # Теги, обложка и тексты готовятся во временной папке; в кэш попадает только готовый файл
            target, cached_cover = await asyncio.to_thread(_prepare_target, meta, work)
            result_info = await tagger.tag_file(mp3, meta, work, own=own)

        async with _PATH_LOCK:  # asyncio-замок держится, пока поток выполняет критическую секцию
            track, reused, link, replaced = await asyncio.to_thread(
                _place, db, mp3=mp3, work=work, target=target, cached_cover=cached_cover, meta=meta, info=info,
                hint=hint, source=source, login=login, alias=alias, replace_id=replace_id)
        if reused:
            if replaced:
                result_info["matched"] = meta.matched != "none"  # type: ignore[assignment]
            return IngestResult(track, True, meta, result_info if replaced else {}, link, replaced)
        link = await asyncio.to_thread(_finish_new, db, track, login)
        result_info["matched"] = meta.matched != "none"  # type: ignore[assignment]
        return IngestResult(track, False, meta, result_info, link)
    finally:
        await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)


def replace_upload_copy(db: DB, track: Track, new_file: Path, source: str) -> None:
    """Файл трека, загруженный пользователем, заменяется скачанным."""
    replace_track_file(db, track, new_file, source)


def replace_track_file(db: DB, track: Track, new_file: Path, source: str, *, duration: int | None = None) -> None:
    """Файл трека в кэше заменяется новым атомарно (os.replace на том же пути); жёсткие ссылки держателей
    пересоздаются (у них старое содержимое); громкость — пересчитать, пометка «проблемный» снимается."""
    dst = Path(track.path)
    tmp = dst.with_name(dst.name + ".new")
    shutil.move(str(new_file), tmp)
    os.replace(tmp, dst)
    for ext in (".lrc", ".txt"):
        side = new_file.with_suffix(ext)
        if side.exists():
            shutil.move(str(side), dst.with_suffix(ext))
    for r in db.links_for_track(track.id):
        _link(dst, Path(r["link_path"]))
        for ext in (".lrc", ".txt"):
            if dst.with_suffix(ext).exists():
                _link(dst.with_suffix(ext), Path(r["link_path"]).with_suffix(ext))
    db.track_file_replaced(track.id, source, duration)
    updated = db.get_track(track.id)
    if updated is not None:
        share_add(updated)  # не личная загрузка — можно раздавать (раздача тоже получает новый файл)
    log.info("файл трека заменён (%s, было %s): %s — %s", source, track.source, track.artist, track.title)


def read_genre_year(path: str | Path) -> tuple[list[str], int | None]:
    """Жанры (TCON, в т.ч. ID3v1-номера «(17)») и год (TDRC/TDOR/TYER) из тегов файла."""
    try:
        tags = ID3(str(path))
    except Exception:  # noqa: BLE001 — нет тегов / не mp3 / файла нет
        return [], None
    genres: list[str] = []
    for frame in tags.getall("TCON"):
        try:
            genres += [str(g) for g in frame.genres]
        except Exception:  # noqa: BLE001
            genres += [str(t) for t in frame.text]
    year = None
    for key in ("TDRC", "TDOR", "TYER"):
        frame = tags.get(key)
        if frame is not None and frame.text:
            year = year_from(str(frame.text[0]))
            if year:
                break
    return normalize_genres(genres), year


def backfill_genres_from_tags(db: DB, batch: int = 200, cursor_key: str = "genre_tags_cursor") -> tuple[int, bool]:
    """Один шаг однократного прохода по кэшу: жанр/год из тегов файлов. (обновлено, проход закончен)."""
    last = int(db.kv_get(cursor_key) or 0)
    tracks = db.tracks_after(last, batch)
    n = 0
    for t in tracks:
        if t.genre is None or t.year is None:
            genres, year = read_genre_year(t.path)
            if genres or year:
                db.set_genre_year(t.id, genres or None, year)
                n += 1
        last = t.id
    db.kv_set(cursor_key, str(last))
    return n, len(tracks) < batch


def give_cached(db: DB, track: Track, login: str) -> IngestResult:
    """TrackGone — трек успели удалить из кэша (тогда его нужно скачать заново)."""
    link = attach_to_user(db, track, login)
    return IngestResult(track, True, None, {}, link)


def disk_usage() -> tuple[int, int, int]:
    u = shutil.disk_usage(CFG.media_root)
    return u.total, u.used, u.free
