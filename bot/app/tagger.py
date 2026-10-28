"""Очистка и запись тегов ID3v2.4 через mutagen + обложка (APIC, cover.jpg) + тексты (USLT, SYLT, .lrc)."""
from __future__ import annotations

import asyncio
import copy
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import (
    APIC, ID3, SYLT, TALB, TCMP, TCON, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TSOP, TSO2, TSRC, TXXX,
    UFID, USLT, ID3NoHeaderError, Encoding,
)

from . import http, lyrics
from .metadata import TrackMeta

log = logging.getLogger(__name__)

COVER_SIZE = 1000


def _normalize_cover(data: bytes) -> bytes | None:
    """Любая картинка -> квадратный JPEG до 1000x1000."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        w, h = im.size
        if min(w, h) < 200:
            return None
        if abs(w - h) > min(w, h) * 0.05:  # обрезаем до квадрата по центру
            side = min(w, h)
            left, top = (w - side) // 2, (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
        if im.size[0] > COVER_SIZE:
            im = im.resize((COVER_SIZE, COVER_SIZE), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=92, optimize=True, progressive=True)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        log.debug("cover normalize failed: %s", e)
        return None


@dataclass
class OwnArt:
    """Обложка и текст, которые уже были в своём файле пользователя: запасной вариант, если в сети ничего нет."""
    cover: bytes | None = None
    synced: str = ""
    plain: str = ""


def _lrc_from_sylt(frames) -> str:
    for f in frames:
        if f.format == 2 and f.text:  # метки в миллисекундах
            return "\n".join(f"[{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000 // 10:02d}]{line}"
                             for line, ms in f.text)
    return ""


def read_own_art(path: Path) -> OwnArt:
    """Обложка (передняя, иначе любая) и текст из тегов файла: ID3 (MP3), FLAC/Vorbis, MP4. Ошибки — пусто."""
    own = OwnArt()
    texts: list[str] = []
    try:
        import mutagen
        try:  # ID3 — напрямую: mutagen.File не узнаёт MP3 с битыми/нестандартными кадрами, а теги в нём читаются
            f, tags = None, ID3(str(path))
        except Exception:  # noqa: BLE001
            f = mutagen.File(str(path))
            tags = getattr(f, "tags", None)
        if tags is None and not getattr(f, "pictures", None):
            return own
        if isinstance(tags, ID3):
            pics = tags.getall("APIC")
            pic = next((p for p in pics if p.type == 3), pics[0] if pics else None)
            own.cover = pic.data if pic else None
            own.synced = _lrc_from_sylt(tags.getall("SYLT"))
            texts = [str(x.text) for x in tags.getall("USLT")]
        elif hasattr(f, "pictures"):  # FLAC
            pics = f.pictures
            pic = next((p for p in pics if p.type == 3), pics[0] if pics else None)
            own.cover = pic.data if pic else None
        if tags is not None and not isinstance(tags, ID3):
            if "covr" in tags and tags["covr"] and not own.cover:  # MP4
                own.cover = bytes(tags["covr"][0])
            for key in ("\xa9lyr", "lyrics", "unsyncedlyrics", "LYRICS", "UNSYNCEDLYRICS"):
                if key in tags:
                    v = tags[key]
                    texts += [str(x) for x in (v if isinstance(v, list) else [v])]
    except Exception as e:  # noqa: BLE001 — чужой файл: любые теги, любые ошибки
        log.debug("own art of %s: %s", path, e)
    for t in (x.strip() for x in texts):
        if not t:
            continue
        if not own.synced and lyrics.parse_lrc(t):
            own.synced = t
        elif not own.plain:
            own.plain = t
    return own


def _own_lyrics(own: OwnArt, duration: float) -> tuple[str, str]:
    """Свой текст файла в виде сервера: синхронный — только если метки сходятся с треком, иначе — обычный текст."""
    synced = lyrics.clean_lrc(own.synced)
    if synced and lyrics.sync_problem(synced, duration, strict=False):
        plain = own.plain or "\n".join(x["text"] for x in lyrics.parse_lrc_lines(synced) if x["text"])
        return "", plain.strip()
    return synced, own.plain.strip() if not synced else ""


def _read_local_cover(p: Path) -> bytes | None:
    try:
        return p.read_bytes() if p.exists() and p.stat().st_size > 5000 else None
    except OSError:
        return None


async def fetch_cover(meta: TrackMeta, existing_dir: Path | None = None) -> bytes | None:
    if existing_dir is not None:
        local = await asyncio.to_thread(_read_local_cover, existing_dir / "cover.jpg")
        if local:
            return local
    for url in meta.cover_urls:
        data = await http.get_bytes(url)
        if data:
            img = await asyncio.to_thread(_normalize_cover, data)
            if img:
                return img
    return None


def _sort_name(name: str) -> str:
    low = name.lower()
    for art in ("the ", "a "):
        if low.startswith(art) and len(name) > len(art) + 1:
            return f"{name[len(art):]}, {name[:len(art) - 1]}"
    return name


def write_tags(path: Path, meta: TrackMeta, cover: bytes | None, synced: str, plain: str) -> None:
    try:
        old = ID3(str(path))
        old.delete(str(path), delete_v1=True, delete_v2=True)
    except ID3NoHeaderError:
        pass
    tags = ID3()
    enc = Encoding.UTF8
    tags.add(TIT2(encoding=enc, text=meta.title))
    # TPE1: несколько исполнителей как мульти-значение ID3v2.4 (Jellyfin корректно делит их на отдельных артистов),
    # отображаемая строка "A feat. B" идёт в TXXX:ARTIST_CREDIT
    artists = [a for a in meta.artists if a] or [meta.artist]
    tags.add(TPE1(encoding=enc, text=artists))
    tags.add(TXXX(encoding=enc, desc="ARTISTS", text=artists))
    if meta.artist and meta.artist != "; ".join(artists):
        tags.add(TXXX(encoding=enc, desc="ARTIST_CREDIT", text=meta.artist))
    tags.add(TPE2(encoding=enc, text=meta.album_artist))
    tags.add(TSOP(encoding=enc, text=_sort_name(artists[0])))
    tags.add(TSO2(encoding=enc, text=_sort_name(meta.album_artist)))
    tags.add(TALB(encoding=enc, text=meta.album))
    if meta.track_no:
        tags.add(TRCK(encoding=enc, text=f"{meta.track_no}/{meta.track_total}" if meta.track_total else str(meta.track_no)))
    tags.add(TPOS(encoding=enc, text=f"{meta.disc_no or 1}/{meta.disc_total or 1}"))
    if meta.date:
        tags.add(TDRC(encoding=enc, text=meta.date))
        tags.add(TDOR(encoding=enc, text=meta.date))
    if meta.genres:
        tags.add(TCON(encoding=enc, text=meta.genres))
    if meta.isrc:
        tags.add(TSRC(encoding=enc, text=meta.isrc))
    if meta.label:
        tags.add(TPUB(encoding=enc, text=meta.label))
    if meta.compilation:
        tags.add(TCMP(encoding=enc, text="1"))
    if meta.release_type:
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Album Type", text=meta.release_type))
    # Идентификаторы MusicBrainz (имена полей как у Picard — их понимает Jellyfin)
    if meta.mb_recording_id:
        tags.add(UFID(owner="http://musicbrainz.org", data=meta.mb_recording_id.encode()))
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Track Id", text=meta.mb_recording_id))
    if meta.mb_track_id:
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Release Track Id", text=meta.mb_track_id))
    if meta.mb_release_id:
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Album Id", text=meta.mb_release_id))
    if meta.mb_release_group_id:
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Release Group Id", text=meta.mb_release_group_id))
    if meta.mb_artist_ids:
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Artist Id", text=meta.mb_artist_ids))
    if meta.mb_album_artist_ids:
        tags.add(TXXX(encoding=enc, desc="MusicBrainz Album Artist Id", text=meta.mb_album_artist_ids))
    if cover:
        tags.add(APIC(encoding=enc, mime="image/jpeg", type=3, desc="Cover", data=cover))
    _add_lyrics(tags, synced, plain)
    tags.save(str(path), v2_version=4, v1=0)


def _add_lyrics(tags: ID3, synced: str, plain: str) -> None:
    enc = Encoding.UTF8
    lyric_text = synced or plain
    if lyric_text:
        # В USLT кладём LRC с таймкодами — Jellyfin 10.9+ и Symfonium распознают его как синхронный текст
        tags.add(USLT(encoding=enc, lang="XXX", desc="", text=lyric_text))
    if synced:
        sylt = [(line, ms) for ms, line in lyrics.parse_lrc(synced)]
        if sylt:
            tags.add(SYLT(encoding=enc, lang="XXX", format=2, type=1, desc="", text=sylt))


def write_lyrics_tags(path: Path, synced: str, plain: str) -> bool:
    """Заменяет только тексты (USLT/SYLT) в MP3, остальные теги не трогает. mutagen пишет в тот же файл (inode),
    так что жёсткие ссылки в медиатеках пользователей видят новый текст. Возвращает, был ли файл изменён."""
    if path.suffix.lower() != ".mp3":
        return False
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        if not (synced or plain):
            return False
        tags = ID3()
    old = [repr(f) for f in tags.getall("USLT") + tags.getall("SYLT")]
    tags.delall("USLT")
    tags.delall("SYLT")
    _add_lyrics(tags, synced, plain)
    new = [repr(f) for f in tags.getall("USLT") + tags.getall("SYLT")]
    if old == new:
        return False
    tags.save(str(path), v2_version=4, v1=0)
    return True


_ALBUM_FRAMES = ("TALB", "TPE2", "TSO2", "TDRC", "TDOR", "TCMP", "TPUB",
                 "TXXX:MusicBrainz Album Id", "TXXX:MusicBrainz Release Group Id",
                 "TXXX:MusicBrainz Album Artist Id", "TXXX:MusicBrainz Album Type")


def harmonize_album_tags(path: Path, ref: Path) -> None:
    """Альбомные теги (название, исполнитель альбома, год, ID релиза) — как у эталонного трека той же папки.
    Номер трека/диска, исполнитель трека и текст не трогаем."""
    try:
        src = ID3(str(ref))
        dst = ID3(str(path))
    except ID3NoHeaderError:
        return
    changed = False
    for key in _ALBUM_FRAMES:
        frame = src.get(key)
        cur = dst.get(key)
        if frame is None:
            if cur is not None and key == "TCMP":  # эталон не сборник — снимаем флаг сборника
                dst.delall(key)
                changed = True
            continue
        if cur is None or list(cur.text) != list(frame.text):
            dst.delall(key)
            dst.add(copy.deepcopy(frame))
            changed = True
    if changed:
        dst.save(str(path), v2_version=4, v1=0)


def _write_all(path: Path, meta: TrackMeta, album_dir: Path, cover: bytes | None, synced: str, plain: str) -> None:
    """Теги, cover.jpg и .lrc/.txt рядом с файлом — блокирующий I/O, вызывается в потоке."""
    write_tags(path, meta, cover, synced, plain)
    if cover and not (album_dir / "cover.jpg").exists():
        (album_dir / "cover.jpg").write_bytes(cover)
    lrc_path = path.with_suffix(".lrc")
    if synced:
        lrc_path.write_text(synced.strip() + "\n", encoding="utf-8")
    elif plain and not lrc_path.exists():
        lrc_path.with_suffix(".txt").write_text(plain.strip() + "\n", encoding="utf-8")


async def tag_file(path: Path, meta: TrackMeta, album_dir: Path, own: OwnArt | None = None) -> dict[str, bool]:
    """Полная разметка файла. Возвращает, что удалось сделать. own — обложка и текст своего файла пользователя:
    в сети ничего не нашлось — берутся они, приведённые к виду сервера (JPEG до 1000×1000, USLT/SYLT, .lrc/.txt)."""
    cover = await fetch_cover(meta, album_dir)
    synced, plain = await lyrics.fetch(meta.artist, meta.title, meta.album, meta.duration)
    if own is not None:
        if not cover and own.cover:
            cover = await asyncio.to_thread(_normalize_cover, own.cover)
        if not (synced or plain):
            synced, plain = _own_lyrics(own, meta.duration)
    await asyncio.to_thread(_write_all, path, meta, album_dir, cover, synced, plain)
    return {"cover": bool(cover), "synced_lyrics": bool(synced), "lyrics": bool(synced or plain)}
