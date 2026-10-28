"""Перезапрос текстов песен у уже скачанных треков: по кнопке пользователя (POST /api/tracks/{id}/lyrics/refetch)
и разовой проверкой всего кэша (`python -m app lyrics-check`, по умолчанию сухой прогон с отчётом)."""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from . import http, lyrics, tagger
from .db import DB, Track

log = logging.getLogger(__name__)


@dataclass
class Local:
    """Текст, который сейчас лежит у трека: .lrc/.txt рядом с файлом кэша или USLT."""
    text: str = ""
    source: str = ""  # lrc | txt | tags | ""

    @property
    def synced(self) -> bool:
        return lyrics.is_synced(self.text)


def read_current(path: Path) -> Local:
    """Блокирующий I/O — вызывать в потоке."""
    for ext in (".lrc", ".txt"):
        side = path.with_suffix(ext)
        try:
            text = side.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if text.strip():
            return Local(text, ext[1:])
    return Local(lyrics._uslt(path) or "", "tags") if path.suffix.lower() == ".mp3" else Local()


def track_duration(track: Track) -> float:
    """Длительность из базы, иначе из самого файла (блокирующий I/O)."""
    if track.duration:
        return float(track.duration)
    try:
        from mutagen import File
        f = File(track.path)
        return float(f.info.length) if f and f.info else 0.0
    except Exception:  # noqa: BLE001 — нет файла / не аудио
        return 0.0


def _replace_side(path: Path, text: str) -> None:
    if text:
        path.write_text(text.strip() + "\n", encoding="utf-8")  # существующий файл — тот же inode у всех ссылок
    else:
        path.unlink(missing_ok=True)


def write_lyrics(path: Path, links: list[Path], synced: str, plain: str) -> None:
    """Заменяет текст трека везде: теги MP3, .lrc/.txt в общем кэше и в медиатеках пользователей (жёсткие ссылки).
    Пустые synced и plain — текст удаляется. Блокирующий I/O — вызывать в потоке.
    Запись не атомарна (truncate+write и правка тегов в том же inode): атомарная замена через os.replace разорвала бы
    жёсткие ссылки. Читатель в эти миллисекунды может увидеть пустой .lrc — плеер просто перечитает текст."""
    from .library import _link

    tagger.write_lyrics_tags(path, synced, plain)
    _replace_side(path.with_suffix(".lrc"), synced)
    _replace_side(path.with_suffix(".txt"), "" if synced else plain)
    for link in links:
        if link == path:
            continue
        for ext in (".lrc", ".txt"):
            src, dst = path.with_suffix(ext), link.with_suffix(ext)
            if src.exists():
                if dst.parent.exists():
                    _link(src, dst)
            else:
                dst.unlink(missing_ok=True)


def _links(db: DB, track: Track) -> list[Path]:
    return [Path(r["link_path"]) for r in db.links_for_track(track.id) if r["link_path"]]


FETCH_TIMEOUT = 100  # сек на поиск текста (LRCLIB ~3 запроса + syncedlyrics до 45 с)
_write_lock = asyncio.Lock()  # запись текста — по одной: файлы общие у всех держателей трека


async def refetch(db: DB, track: Track, *, clear: bool = False, dry_run: bool = False,
                  timeout: float = FETCH_TIMEOUT) -> dict:
    """Ищет текст заново по новым правилам и (кроме dry_run) записывает его.

    status: updated — найден и записан новый текст; same — найден тот же; cleared — нового нет, старый удалён
    (clear=True или старый синхронный текст точно чужой/сбит — sync_problem(strict=False)); not_found — лучшего
    текста нет, старый оставлен (в т.ч. когда нашёлся только обычный текст, а старый синхронный в порядке);
    error — сервисы текстов недоступны или не ответили за timeout: ничего не меняется."""
    path = Path(track.path)
    old, duration = await asyncio.gather(asyncio.to_thread(read_current, path),
                                         asyncio.to_thread(track_duration, track))
    problem = lyrics.sync_problem(old.text, duration) if old.synced else ""
    sure = bool(problem) and bool(lyrics.sync_problem(old.text, duration, strict=False))
    out = {"status": "not_found", "synced": False, "plain": False, "problem": problem or None}
    try:
        # таймаут — только на поиск: запись ниже не прерывается на полпути
        synced, plain = await asyncio.wait_for(
            lyrics.fetch(track.artist, track.title, track.album or "", duration, raise_errors=True), timeout)
    except (asyncio.TimeoutError, http.FetchError, OSError) as e:
        log.warning("текст %s — %s: поиск не удался (%s), старый текст оставлен", track.artist, track.title, e)
        out["status"] = "error"
        return out
    out.update(synced=bool(synced), plain=bool(synced or plain))
    if synced or plain:
        new_text = (synced or plain).strip()
        if new_text == old.text.strip():
            out["status"] = "same"
        elif old.synced and not problem and not synced:
            out["status"] = "not_found"  # исправный синхронный текст на обычный не меняем
        else:
            out["status"] = "updated"
    elif old.text and (clear or sure):  # «кончается до середины» без замены не удаляем — может быть своим
        out["status"] = "cleared"
    if dry_run or out["status"] in ("same", "not_found"):
        return out
    links = await asyncio.to_thread(_links, db, track)
    async with _write_lock:
        # shield: отмена запроса (клиент ушёл) не обрывает запись, иначе .lrc и теги разойдутся
        await asyncio.shield(asyncio.to_thread(write_lyrics, path, links, synced, plain))
    log.info("текст %s — %s: %s", track.artist, track.title, out["status"])
    return out


# ------------------------------------------------------------------ разовая проверка всего кэша
async def audit(db: DB, *, apply: bool = False, refetch_all: bool = False, limit: int = 0,
                out=print) -> dict:
    """Проверяет синхронные тексты всех треков по новым правилам (lyrics.sync_problem): метки против
    длительности и сбитая синхронизация. Сухой прогон (по умолчанию) только печатает отчёт; apply — перезапрашивает
    подозрительные тексты и записывает найденное (не нашлось — удаляется только точно чужой/сбитый текст, при сбое
    сети ничего не трогается).
    refetch_all — перезапросить вообще все треки (долго: LRCLIB ~3 запроса в секунду)."""
    stats = {"tracks": 0, "synced": 0, "suspicious": 0, "updated": 0, "cleared": 0, "not_found": 0, "same": 0,
             "error": 0}
    last = 0
    while True:
        batch = await asyncio.to_thread(db.tracks_after, last, 200)
        if not batch:
            break
        for t in batch:
            last = t.id
            if limit and stats["tracks"] >= limit:
                return _summary(stats, apply, out)
            stats["tracks"] += 1
            path = Path(t.path)
            if not await asyncio.to_thread(path.exists):
                continue
            cur, duration = await asyncio.gather(asyncio.to_thread(read_current, path),
                                                 asyncio.to_thread(track_duration, t))
            problem = lyrics.sync_problem(cur.text, duration) if cur.synced else ""
            stats["synced"] += cur.synced
            if problem:
                stats["suspicious"] += 1
            if not problem and not refetch_all:
                continue
            label = f"#{t.id} {t.artist} — {t.title} ({int(duration) // 60}:{int(duration) % 60:02d})"
            if problem and not apply and not refetch_all:
                out(f"⚠ {label}: {problem} [{cur.source}]")
                continue
            res = await refetch(db, t, dry_run=not apply)  # удаляет только точно чужой/сбитый текст
            stats[res["status"]] += 1
            if problem or res["status"] != "same":
                verb = {"updated": "новый текст" + (" (синхр.)" if res["synced"] else ""),
                        "cleared": "текст удалён", "not_found": "лучшего нет, оставлен", "same": "без изменений",
                        "error": "сервисы текстов недоступны, оставлен"}
                pre = "" if apply else "(будет) "
                out(f"{'⚠' if problem else '•'} {label}: {problem or 'перепроверка'} → {pre}{verb[res['status']]}")
    return _summary(stats, apply, out)


def _summary(stats: dict, apply: bool, out) -> dict:
    out(f"Треков: {stats['tracks']}, с синхронным текстом: {stats['synced']}, подозрительных: {stats['suspicious']}"
        + (f"; заменено: {stats['updated']}, удалено: {stats['cleared']}, не найдено: {stats['not_found']}, "
           f"ошибок: {stats['error']}"
           if apply else "; сухой прогон — ничего не изменено (запустите с --apply)"))
    return stats


def main(argv: list[str]) -> int:
    """python -m app lyrics-check [--apply] [--all] [--limit N]"""
    ap = argparse.ArgumentParser(prog="python -m app lyrics-check",
                                 description="Проверка текстов песен по новым правилам (по умолчанию сухой прогон).")
    ap.add_argument("--apply", action="store_true", help="перезапросить подозрительные тексты и записать результат")
    ap.add_argument("--all", action="store_true", help="перезапросить тексты всех треков, а не только подозрительных")
    ap.add_argument("--limit", type=int, default=0, help="проверить не больше N треков")
    args = ap.parse_args(argv)
    from . import http
    from .config import CFG

    async def run() -> None:
        db = DB(CFG.db_path)
        try:
            await audit(db, apply=args.apply, refetch_all=args.all, limit=args.limit)
        finally:
            await http.close()
    asyncio.run(run())
    if args.apply:
        print("Jellyfin увидит новые тексты после ближайшего сканирования медиатеки.")
    return 0
