"""Бизнес-логика: регистрация, очередь загрузок, обновление библиотек Jellyfin."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from . import artwork, downloaders, library, loudness, metadata, radio, sources, uploads
from .config import CFG
from .db import DB, PL_MAX_PER_USER, PL_MAX_TRACKS, PlaylistLimit, Track, User
from .jellyfin import Jellyfin, JellyfinError
from .metadata import Hint
from .util import normalize_genres, year_from
from .sources import TrackRequest

log = logging.getLogger(__name__)

LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
RESERVED = {"admin", "root", "library", "radio", "incoming", "users", "system", "jellyfin", "slskd", "bot"}

Notify = Callable[[str], Awaitable[None]]


def library_name(login: str) -> str:
    return f"Музыка · {login}"


PL_JF_PREFIX = "pl_jf:"  # kv: pl_jf:<playlist_id>:<login> -> id зеркала плейлиста в Jellyfin
PL_HASH_PREFIX = "pl_hash:"  # kv: pl_hash:<playlist_id>:<login> -> хэш имени и состава последнего зеркала
PL_DIRTY_PREFIX = "pl_dirty:"  # kv: pl_dirty:<playlist_id> — зеркало ещё не синхронизировано
PL_TRASH_PREFIX = "pl_jf_trash:"  # kv: pl_jf_trash:<jellyfin id> — старое зеркало, которое не удалось удалить
PL_SYNC_DELAY = 10.0
PL_RESUME_DELAY = 60.0
PL_PATHS_TTL = 60.0
MAX_QUEUED_PER_USER = 2000       # треков одного пользователя в очереди загрузок одновременно
LOUDNESS_MAX_RETRIES = 3
LOUDNESS_RETRY_DELAY = 3600.0  # потом ×2, ×4
PL_MIRROR_MAX = PL_MAX_TRACKS  # зеркало — целиком (плейлист бота сам не больше)
IMPORT_PL_BACKFILL_FLAG = "import_playlists_backfill_v1"


def playlist_line(name: str) -> str:
    return f"📃 Плейлист «{name}» — в Mini App и Jellyfin"


def _network_error(e: BaseException) -> bool:
    """Ожидаемые сбои сети/Jellyfin — без трассировки в логе."""
    return isinstance(e, (JellyfinError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError))


@dataclass
class TrackOutcome:
    req: TrackRequest
    ok: bool
    text: str
    reused: bool = False
    track: Track | None = None      # что легло в медиатеку (для карточки трека)
    source: str = ""                # soulseek / spotdl / yt-dlp / cache
    flags: list[str] = field(default_factory=list)


@dataclass
class Job:
    user: User
    text: str
    notify: Notify
    origin: str = "user"            # user / wave
    tracks: list[TrackRequest] = field(default_factory=list)
    title: str = ""
    done: int = 0
    failed: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    # Наблюдатель за ходом задания (живой отчёт, реакции, карточки в Telegram). Методы (все async, необязательны):
    #   started(req), progress(req, text), finished(req, outcome), completed(results)
    view: Any = None
    # плейлист бота, который наполняется по ходу (ссылка на плейлист): позиция = индекс в tracks
    playlist_id: int | None = None
    playlist_name: str = ""
    playlist_base: float = 0.0      # позиция первого трека (добавление в конец уже заполненного плейлиста)
    counted: bool = False           # треки учтены в квоте очереди пользователя


def _tg_chat(imp: Any) -> bool:
    """Писать ли о ходе импорта в Telegram: только импорты из бота / Mini App в Telegram (есть чат)."""
    keys = imp.keys() if hasattr(imp, "keys") else ()
    return bool(imp["chat_id"]) and ("source" not in keys or imp["source"] != "app")


async def _view_call(job: "Job", name: str, *args: Any) -> bool:
    """Вызвать метод наблюдателя, если он есть. True — наблюдатель обработал событие."""
    fn = getattr(job.view, name, None) if job.view is not None else None
    if fn is None:
        return False
    try:
        await fn(*args)
    except Exception:  # noqa: BLE001 — сбой отображения не должен ломать загрузку
        log.exception("view.%s failed", name)
    return True


class Service:
    def __init__(self, db: DB, jf: Jellyfin):
        self.db, self.jf = db, jf
        self.queue: asyncio.Queue[tuple[Job, TrackRequest, asyncio.Future]] = asyncio.Queue()
        # отдельная очередь и пул воркеров для импорта медиатеки — обычные запросы её не ждут
        self.import_queue: asyncio.Queue[tuple[Job, TrackRequest, asyncio.Future]] = asyncio.Queue()
        self._refresh_pending: dict[str, asyncio.Task] = {}
        self._workers: list[asyncio.Task] = []
        self.active: dict[int, str] = {}  # воркер -> что качается сейчас
        self.active_info: dict[int, dict] = {}  # воркер -> {login, req, progress} для /api/activity
        self._tasks: set[asyncio.Task] = set()
        self._rate_marks: dict[int, list[tuple[float, int]]] = {}
        self._last_progress_text: dict[int, str] = {}
        self._refresh_first = 0.0
        self._fails: dict[int, list[float]] = {}  # tg_id -> время неудачных попыток регистрации
        # зеркала плейлистов: срок синхронизации, задачи, общий замок, кэш «путь -> id» Jellyfin
        self._pl_due: dict[int, float] = {}
        self._pl_tasks: dict[int, asyncio.Task] = {}
        self._pl_lock = asyncio.Lock()
        self._jf_paths_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._loud_wake = asyncio.Event()  # новый трек скачан — посчитать громкость, не дожидаясь паузы
        self.uploads = uploads.Uploads(self, db)  # свои файлы пользователей (бот, Mini App)

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        library.ensure_dirs()
        # до старта воркеров: объединить альбомы, разнесённые по папкам «Альбом (год)» старой схемы
        try:
            moved = 0
            for fn in (library.merge_artist_variants, library.merge_compilation_splits, library.merge_split_albums):
                moved += await asyncio.to_thread(fn, self.db)
            if moved:
                self._schedule_refresh(None, 5)
        except Exception:  # noqa: BLE001
            log.exception("слияние альбомов не удалось")
        radio.ensure_default_stations(self.db)
        try:  # раздача Soulseek: после слияния альбомов пути могли смениться — полная сверка
            added, removed = await asyncio.to_thread(library.rebuild_share, self.db)
            if added or removed:
                log.info("раздача Soulseek: добавлено %d, убрано %d", added, removed)
                try:  # slskd читает кэш раздачи с диска — просим пересканировать
                    await downloaders.SLSKD._req("PUT", "/shares", json={})
                except Exception as e:  # noqa: BLE001
                    log.warning("не удалось запустить пересканирование раздачи slskd: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("сверка раздачи Soulseek не удалась")
        for i in range(max(1, CFG.download_workers)):
            self._workers.append(self._spawn_worker(i))
        try:
            await radio.sync_tuner(self.db, self.jf)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось настроить M3U-тюнер радио: %s", e)
        if self.db.running_imports():  # продолжить импорт, прерванный перезапуском
            self._ensure_import_feeder()
        self._tasks.add(asyncio.create_task(self._reconcile_loop(), name="reconcile"))
        self._tasks.add(asyncio.create_task(self._artwork_loop(), name="artwork"))
        if CFG.loudness_analysis:
            self._tasks.add(asyncio.create_task(self._loudness_loop(), name="loudness"))
        self._tasks.add(asyncio.create_task(self._genre_loop(), name="genres"))
        self.uploads.start()
        await self._safe(self.apply_deletion_policy())
        await self._safe(self.jf.delete_orphan_playlists(self.own_playlist_ids()))
        await self._safe(self._empty_trash())
        await self._safe(asyncio.to_thread(self.backfill_import_playlists))
        self._resume_dirty_playlists()

    def own_playlist_ids(self) -> set[str]:
        """Плейлисты Jellyfin, созданные ботом: «Моя Волна» и зеркала плейлистов бота (pl_jf:) — последние
        тоже «свои», даже если их назвали «Моя Волна …», и очистка лишних их не тронет."""
        return set(self.db.kv_values("wave_playlist:")) | set(self.db.kv_items(PL_JF_PREFIX).values())

    async def apply_deletion_policy(self) -> None:
        """Каждому разрешено удалять только в своей библиотеке (для уже зарегистрированных — при старте)."""
        for u in self.db.all_users():
            if not u.library_id:
                continue
            try:
                pol = ((await self.jf.get_user(u.jf_user_id)).get("Policy") or {})
                if pol.get("EnableContentDeletionFromFolders") != [u.library_id] or pol.get("EnableContentDeletion"):
                    await self.jf.set_policy(u.jf_user_id, EnableContentDeletion=False,
                                             EnableContentDeletionFromFolders=[u.library_id])
            except Exception as e:  # noqa: BLE001
                log.debug("deletion policy for %s: %s", u.login, e)

    RECONCILE_MAX_SHARE = 0.2   # пропало больше 20 % ссылок пользователя (и больше 10) — это сбой диска, не удаление
    RECONCILE_MAX_ABS = 10

    async def reconcile_deleted(self) -> int:
        """Трек удалили в Jellyfin/Finamp (файл в папке пользователя пропал) — чистим базу и общий кэш.
        Защита от потери медиатеки при недоступном диске: нет папки пользователей / логина — проход пропускается;
        пропало слишком много — стоп с ошибкой в логе; удаляется только то, чего нет два прохода подряд."""
        users_dir = CFG.users_dir
        present = await asyncio.to_thread(lambda: users_dir.is_dir() and any(users_dir.iterdir()))
        if not present:
            log.warning("reconcile: папка медиатек %s отсутствует или пуста — проход пропущен", users_dir)
            return 0
        rows = self.db.all_user_tracks()

        def scan() -> list:
            by_login: dict[str, list] = {}
            for r in rows:
                by_login.setdefault(r["login"], []).append(r)
            out = []
            for login, items in by_login.items():
                if not library.user_root(login).is_dir():
                    log.warning("reconcile: нет папки медиатеки %s — пропускаю", login)
                    continue
                gone = [r for r in items if not Path(r["link_path"]).exists()]
                if len(gone) > self.RECONCILE_MAX_ABS and len(gone) > self.RECONCILE_MAX_SHARE * len(items):
                    log.error("reconcile: у %s пропало %d из %d ссылок — похоже на сбой диска, ничего не удаляю",
                              login, len(gone), len(items))
                    continue
                out += gone
            return out
        candidates = await asyncio.to_thread(scan)
        prev: set = getattr(self, "_reconcile_seen", set())
        now = {(r["login"], r["track_id"]) for r in candidates}
        self._reconcile_seen = now  # подтверждение — на следующем проходе
        missing = [r for r in candidates if (r["login"], r["track_id"]) in prev]
        for r in missing:
            track = self.db.get_track(r["track_id"])
            if track:
                pids = self._playlists_of([track.id])
                await asyncio.to_thread(library.unlink_from_user, self.db, track, r["login"])
                log.info("удалён пользователем %s в плеере: %s — %s", r["login"], track.artist, track.title)
                self._playlists_touched(pids)
        if missing:
            self._schedule_refresh(None, 5)
        return len(missing)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            await self._safe(self.reconcile_deleted())
            await self._safe(asyncio.to_thread(cleanup_stale_incoming))
            await self._safe(asyncio.to_thread(self.uploads.cleanup))

    async def _loudness_loop(self, first_delay: float = 90, idle: float = 600) -> None:
        """ReplayGain в фоне: по одному треку (ffmpeg с nice), новые загрузки — первыми, затем старые."""
        await asyncio.sleep(first_delay)
        done = 0
        retry: dict[int, tuple[int, float]] = {}  # трек -> (неудач по таймауту/памяти, когда повторить)
        while True:
            try:
                now = time.monotonic()
                skip = {tid for tid, (_, at) in retry.items() if at > now}
                t = await asyncio.to_thread(self.db.next_loudness_track, skip)
                if t is None:
                    if done:
                        log.info("громкость посчитана для %d треков", done)
                        done = 0
                    self._loud_wake.clear()
                    try:
                        await asyncio.wait_for(self._loud_wake.wait(), idle)
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    res = await loudness.analyze(Path(t.path))
                except loudness.Transient as e:  # таймаут / убит по памяти — не помечаем, повторим позже
                    n = retry.get(t.id, (0, 0.0))[0] + 1
                    if n < LOUDNESS_MAX_RETRIES:
                        retry[t.id] = (n, time.monotonic() + LOUDNESS_RETRY_DELAY * 2 ** (n - 1))
                        log.warning("громкость %s — %s: %s, повторю позже (%d)", t.artist, t.title, e, n)
                        continue
                    log.warning("громкость %s — %s: %s, %d раз подряд — пропускаю", t.artist, t.title, e, n)
                    res = None
                retry.pop(t.id, None)
                if res is None and loudness.available() is False:
                    log.warning("громкость треков не считается: в ffmpeg нет ebur128/loudnorm")
                    return  # нечем считать — не отмечаем треки как неудачные, после обновления образа досчитаем
                gain, peak = res if res else (None, None)
                await asyncio.to_thread(self.db.set_loudness, t.id, gain, peak)
                done += 1
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("анализ громкости упал")
                await asyncio.sleep(60)

    GENRE_TAGS_DONE = "genre_tags_done_v1"
    GENRE_ONLINE_CURSOR = "genre_online_cursor"
    GENRE_ONLINE_PER_HOUR = 60
    GENRE_ONLINE_FAILS = "genre_online_fails"  # «id трека:неудач подряд» — сбои сети на текущем треке курсора
    GENRE_ONLINE_MAX_FAILS = 6                 # с паузами 1, 2, 4, 8, 16, 24 ч ≈ двое суток, потом трек пропускаем
    GENRE_ONLINE_MAX_PAUSE = 24 * 3600

    async def _genre_loop(self, first_delay: float = 180, pause: float = 1.0) -> None:
        """Жанр и год для уже скачанных треков: сначала один раз из тегов файлов (дёшево), затем — для оставшихся
        без жанра — iTunes по одному запросу, не больше GENRE_ONLINE_PER_HOUR в час. Новые треки получают жанр
        при скачивании, сюда не попадают."""
        await asyncio.sleep(first_delay)
        try:
            if not self.db.kv_get(self.GENRE_TAGS_DONE):
                total = 0
                while True:
                    n, done = await asyncio.to_thread(library.backfill_genres_from_tags, self.db)
                    total += n
                    if done:
                        break
                    await asyncio.sleep(pause)
                self.db.kv_set(self.GENRE_TAGS_DONE, "1")
                log.info("жанр/год из тегов файлов: обновлено треков %d", total)
            while True:
                n, done = await self.genres_online_step(self.GENRE_ONLINE_PER_HOUR)
                if done:
                    return
                await asyncio.sleep(self._genre_pause())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("фоновое заполнение жанров упало")

    def _genre_fails(self) -> tuple[int, int]:
        """(id трека, неудач подряд) для трека, на котором споткнулся онлайн-проход; (0, 0) — сбоев нет."""
        raw = self.db.kv_get(self.GENRE_ONLINE_FAILS) or ""
        try:
            tid, n = raw.split(":")
            return int(tid), int(n)
        except ValueError:
            return 0, 0

    def _genre_pause(self) -> float:
        """Пауза до следующей порции: час, а при сбоях сети подряд — 2, 4, 8… ч, не больше суток."""
        n = self._genre_fails()[1]
        return float(min(3600 * 2 ** max(n - 1, 0), self.GENRE_ONLINE_MAX_PAUSE))

    async def genres_online_step(self, limit: int) -> tuple[int, bool]:
        """До limit треков без жанра — жанр (и год) из iTunes. (найдено, проход закончен).
        Курсор двигается только после ответа сервиса (пустой ответ — тоже ответ). При сбое сети трек
        спросим в следующей порции; после GENRE_ONLINE_MAX_FAILS сбоев подряд на одном треке — пропускаем его."""
        last = int(self.db.kv_get(self.GENRE_ONLINE_CURSOR) or 0)
        tracks = await asyncio.to_thread(self.db.tracks_after, last, limit, without_genre=True)
        found = 0
        for t in tracks:
            try:
                it = await metadata.lookup_itunes(Hint(artist=t.artist, title=t.title, album=t.album or "",
                                                       duration=t.duration or 0), strict=True)
            except Exception as e:  # noqa: BLE001 — сетевой сбой: курсор не двигаем, трек спросим в следующий раз
                ftid, n = self._genre_fails()
                n = n + 1 if ftid == t.id else 1
                if n < self.GENRE_ONLINE_MAX_FAILS:
                    self.db.kv_set(self.GENRE_ONLINE_FAILS, f"{t.id}:{n}")
                    log.warning("iTunes: жанр для %s — %s не получен (%s), повторю позже (%d)",
                                t.artist, t.title, e, n)
                    return found, False
                log.warning("iTunes: жанр для %s — %s не получен %d раз подряд (%s) — пропускаю",
                            t.artist, t.title, n, e)
                self.db.kv_set(self.GENRE_ONLINE_FAILS, "")
                self.db.kv_set(self.GENRE_ONLINE_CURSOR, str(t.id))
                continue
            if self._genre_fails() != (0, 0):
                self.db.kv_set(self.GENRE_ONLINE_FAILS, "")
            genres = normalize_genres((it or {}).get("primaryGenreName"))
            genres = [g for g in genres if g.casefold() not in ("music", "музыка")]
            year = year_from((it or {}).get("releaseDate"))
            if genres or (year and t.year is None):
                self.db.set_genre_year(t.id, genres or None, year if t.year is None else None)
                found += genres != []
            self.db.kv_set(self.GENRE_ONLINE_CURSOR, str(t.id))
        return found, len(tracks) < limit

    def loudness_changed(self) -> None:
        ev = getattr(self, "_loud_wake", None)
        if ev is not None:
            ev.set()

    async def _artwork_loop(self) -> None:
        """Обложки и фото исполнителей: через минуту после старта, затем раз в 6 часов."""
        await asyncio.sleep(60)
        while True:
            await self._safe(artwork.backfill(self.db, self.jf))
            await asyncio.sleep(6 * 3600)

    def _schedule_artwork(self, delay: float = 120) -> None:
        """Отложенная догрузка картинок после загрузок: серия треков — один проход."""
        self._artwork_dirty = True
        t = getattr(self, "_artwork_pending", None)
        if t is not None and not t.done():
            return  # уже запланирован (или идёт): после прохода он повторится, если были новые загрузки
        try:
            t = asyncio.get_running_loop().create_task(self._artwork_later(delay))
        except RuntimeError:
            return
        self._artwork_pending = t
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    async def _artwork_later(self, delay: float) -> None:
        while True:
            await asyncio.sleep(delay)
            self._artwork_dirty = False
            await self._safe(artwork.backfill(self.db, self.jf))
            if not self._artwork_dirty:
                return


    async def stop(self) -> None:
        up = getattr(self, "uploads", None)
        if up is not None:
            await up.stop()
        for w in self._workers + list(getattr(self, "_import_workers", None) or []):
            w.cancel()

    # ------------------------------------------------------------------ импорт медиатеки из файла
    # Очередь импорта хранится в БД (переживает перезапуск) и идёт с низким приоритетом: новые позиции
    # подаются воркерам, только когда обычная очередь пуста, и в работе одновременно не больше
    # (воркеров − 1) позиций импорта — один воркер всегда свободен для запросов пользователей.
    # Сообщения в Telegram отправляет бот через self.messenger (send/edit/send_document).
    messenger = None

    def start_import(self, user: User, chat_id: int, filename: str, items: list[dict],
                     status_msg: int | None = None, source: str = "tg") -> int:
        # status_msg пишем сразу при создании: иначе очередь могла успеть отправить второе сообщение прогресса
        iid = self.db.create_import(user.login, chat_id, filename, items, status_msg=status_msg, source=source)
        self.ensure_import_playlist(iid)
        self._ensure_import_feeder()
        return iid

    def draft_import(self, user: User, chat_id: int, filename: str, items: list[dict],
                     status_msg: int | None = None, source: str = "tg") -> int:
        """Разобранный файл ждёт подтверждения в базе, а не в памяти: перезапуск бота кнопку не ломает.
        source='app' — из приложения/браузера: сообщений в Telegram о ходе импорта не будет."""
        return self.db.create_import(user.login, chat_id, filename, items, status_msg=status_msg, state="draft",
                                     source=source)

    def confirm_import(self, iid: int, status_msg: int | None = None) -> bool:
        if not self.db.activate_import_draft(iid):
            return False
        if status_msg:
            self.db.set_import_status_msg(iid, status_msg)
        self.ensure_import_playlist(iid)
        self._ensure_import_feeder()
        return True

    # ------------------------------------------------------------------ плейлисты из импортов и ссылок
    def create_source_playlist(self, login: str, base: str) -> tuple[int, str] | None:
        """Плейлист бота для импорта/ссылки (имя свободное: «X», «X (2)»…). Best effort: None — не вышло."""
        try:
            name = self.db.free_playlist_name(login, base)
            return self.db.create_playlist(login, name, max_per_user=PL_MAX_PER_USER), name
        except PlaylistLimit:
            log.info("у %s уже %d плейлистов — плейлист «%s» не создан", login, PL_MAX_PER_USER, base)
        except Exception:  # noqa: BLE001
            log.exception("не удалось создать плейлист «%s» для %s", base, login)
        return None

    def ensure_import_playlist(self, iid: int) -> int | None:
        """Плейлист импорта: имя файла без расширения. Создаётся один раз (повторное подтверждение — нет)."""
        imp = self.db.get_import(iid)
        if not imp:
            return None
        if imp["playlist_id"]:
            return imp["playlist_id"]
        made = self.create_source_playlist(imp["login"], Path(imp["filename"]).stem or "Импорт")
        if not made:
            return None
        self.db.set_import_playlist(iid, made[0])
        return made[0]

    def import_playlist(self, imp: Any) -> Any:
        """Живой плейлист импорта (None — не создавался или пользователь его удалил)."""
        pid = imp["playlist_id"] if imp is not None else None
        return self.db.get_playlist(pid) if pid else None

    def _put_in_playlist(self, pid: int, track_id: int, index: int, login: str, base: float = 0.0) -> bool:
        """Трек в плейлист источника на позицию источника (base + index); удалённый плейлист не пересоздаётся."""
        if index >= PL_MAX_TRACKS:
            return False
        if not self.db.playlist_role(pid, login):  # вышел или исключён, пока шла загрузка — не дописываем
            return False
        try:
            added = self.db.put_playlist_track(pid, track_id, base + index, login, PL_MAX_TRACKS)
        except Exception:  # noqa: BLE001 — плейлист не должен ломать загрузку
            log.exception("не удалось добавить трек %s в плейлист %s", track_id, pid)
            return False
        if added:
            self.playlist_changed(pid)
        return added

    def _job_track_done(self, job: Job, req: TrackRequest, outcome: TrackOutcome) -> None:
        if not job.playlist_id or not outcome.ok or outcome.track is None:
            return
        idx = next((i for i, r in enumerate(job.tracks) if r is req), None)
        if idx is not None:
            self._put_in_playlist(job.playlist_id, outcome.track.id, idx, job.user.login, job.playlist_base)

    def link_playlist(self, job: Job, resolved: Any) -> None:
        """Ссылка на плейлист (Spotify/YouTube/SoundCloud/Яндекс…) — плейлист бота с её названием.
        Альбомы — нет: они и так альбомы в медиатеке."""
        if not getattr(resolved, "from_link", False) or resolved.kind != "playlist" or len(job.tracks) < 2:
            return
        made = self.create_source_playlist(job.user.login, resolved.title or "Плейлист")
        if made:
            job.playlist_id, job.playlist_name = made

    def backfill_import_playlists(self) -> int:
        """Однократно: плейлисты для импортов, начатых до появления этой функции. Порядок — как в файле,
        треки — только из медиатеки пользователя (поиск по ISRC, затем общий кэш/псевдонимы, затем ключ)."""
        if self.db.kv_get(IMPORT_PL_BACKFILL_FLAG):
            return 0
        n = 0
        for imp in self.db.imports_without_playlist():
            if not self.db.user_by_login(imp["login"]):
                continue
            pid = self.ensure_import_playlist(imp["id"])
            if not pid:
                continue
            n += 1
            owned = self.db.user_library(imp["login"])
            ids = {t.id for t, _ in owned}
            by_isrc = {t.isrc.upper(): t.id for t, _ in owned if t.isrc}
            by_key = {t.match_key: t.id for t, _ in owned}
            for it in self.db.import_items_ok(imp["id"]):
                if it["idx"] >= PL_MAX_TRACKS:
                    break
                tid = it["track_id"] if it["track_id"] in ids else None
                if tid is None and it["isrc"]:
                    tid = by_isrc.get(it["isrc"].upper())
                if tid is None:
                    t = library.lookup_cached(self.db, it["artist"], it["title"], isrc=it["isrc"] or "")
                    tid = t.id if t and t.id in ids else None
                if tid is None:
                    from .util import match_key
                    tid = by_key.get(match_key(it["artist"], it["title"]))
                if tid is None:
                    continue
                self.db.set_import_item(it["id"], it["state"], it["note"], track_id=tid)
                self.db.put_playlist_track(pid, tid, it["idx"], imp["login"], PL_MAX_TRACKS)
            self.playlist_changed(pid, PL_RESUME_DELAY)
        self.db.kv_set(IMPORT_PL_BACKFILL_FLAG, "1")
        if n:
            log.info("созданы плейлисты для %d прежних импортов", n)
        return n

    def _wake_event(self) -> asyncio.Event:
        ev = getattr(self, "_import_wake", None)
        if ev is None:
            ev = self._import_wake = asyncio.Event()
        return ev

    def _ensure_import_feeder(self) -> None:
        t = getattr(self, "_import_task", None)
        if t is None or t.done():
            self._import_task = asyncio.create_task(self._import_feeder(), name="import-feeder")
        self._wake_event().set()  # простаивающая очередь сразу берёт новый/продолженный импорт

    async def _import_feeder(self) -> None:
        self.db.reset_active_import_items()
        self.db.purge_import_drafts()
        inflight: dict[int, int] = {}          # import_item.id -> import.id
        last_progress: dict[int, float] = {}
        limit = max(1, CFG.import_workers)
        if not getattr(self, "_import_workers", None):
            # номера 100+ — чтобы /queue различал воркеры импорта и обычные
            self._import_workers = [self._spawn_worker(100 + i, self.import_queue) for i in range(limit)]
        while True:
            try:
                running = self.db.running_imports()
                if not running and not inflight:
                    wake = self._wake_event()
                    try:
                        await asyncio.wait_for(wake.wait(), 30)
                    except asyncio.TimeoutError:
                        pass
                    wake.clear()
                    continue
                while len(inflight) < limit:
                    item = await asyncio.to_thread(self.db.next_import_item, set(inflight.values()))
                    if item is None:
                        break
                    imp = self.db.get_import(item["import_id"])
                    user = self.db.user_by_login(imp["login"]) if imp else None
                    if not user:
                        self.db.set_import_item(item["id"], "failed", "пользователь удалён")
                        continue
                    self.db.set_import_item(item["id"], "active")
                    inflight[item["id"]] = item["import_id"]
                    req = TrackRequest(artist=item["artist"], title=item["title"], album=item["album"] or "",
                                       isrc=item["isrc"] or "", duration=item["duration"] or 0.0,
                                       spotify_url=item["spotify_url"] or "", origin="import",
                                       query=f"{item['artist']} - {item['title']}")

                    async def quiet(_text: str) -> None:
                        return None

                    job = Job(user=user, text=req.label, notify=quiet, origin="import", tracks=[req], title=req.label)
                    fut: asyncio.Future = asyncio.get_running_loop().create_future()
                    fut.add_done_callback(lambda f, iid=item["id"]: self._import_item_done(iid, f, inflight))
                    await self.import_queue.put((job, req, fut))
                now = time.monotonic()
                for imp in running:
                    counts = self.db.import_counts(imp["id"])
                    if counts["pending"] == 0 and counts["active"] == 0:
                        await self._finish_import(imp["id"])
                    elif now - last_progress.get(imp["id"], 0) > 10:  # живой прогресс: раз в 10 с
                        last_progress[imp["id"]] = now
                        await self._import_progress(imp["id"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("import feeder error")
            await asyncio.sleep(3)

    def _import_item_done(self, item_id: int, fut: asyncio.Future, inflight: dict[int, int]) -> None:
        import_id = inflight.pop(item_id, None)
        if fut.cancelled():
            self.db.set_import_item(item_id, "pending")
            return
        outcome: TrackOutcome = fut.result()
        state = ("cached" if outcome.reused else "done") if outcome.ok else "failed"
        track_id = outcome.track.id if outcome.ok and outcome.track is not None else None
        self.db.set_import_item(item_id, state, None if outcome.ok else outcome.text[:300], track_id=track_id)
        if track_id is not None and import_id is not None:
            item = self.db.get_import_item(item_id)
            pl = self.import_playlist(self.db.get_import(import_id))
            if item is not None and pl is not None:
                self._put_in_playlist(pl["id"], track_id, item["idx"], pl["owner"])
        # пауза/стоп: очередь такой импорт больше не обновляет — докачался последний начатый трек,
        # обновляем сообщение сами, иначе в нём навсегда висит «осталось/сейчас качаю»
        imp = self.db.get_import(import_id) if import_id is not None else None
        if imp and imp["state"] != "running" and self.db.import_counts(import_id)["active"] == 0:
            try:
                t = asyncio.get_running_loop().create_task(self.refresh_import_message(import_id))
            except RuntimeError:
                return
            tasks = getattr(self, "_tasks", None)
            if tasks is not None:
                tasks.add(t)
                t.add_done_callback(tasks.discard)

    def import_status_text(self, iid: int, live: bool = False) -> str:
        imp = self.db.get_import(iid)
        c = self.db.import_counts(iid)
        processed = c["done"] + c["cached"] + c["failed"]
        total = imp["total"] if imp else processed
        pct = int(processed * 100 / total) if total else 100
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        state = imp["state"] if imp else ""
        head = {"paused": "⏸ Импорт на паузе", "done": "🎉 Импорт завершён", "cancelled": "⏹ Импорт остановлен"}.get(
            state, "📥 Импорт")
        text = (f"{head} «{imp['filename'] if imp else '?'}»\n{bar} {pct}%\n{processed} из {total}\n\n"
                f"✅ скачано: {c['done']}\n♻️ уже было: {c['cached']}\n❌ не нашлось: {c['failed']}\n"
                f"⏳ осталось: {c['pending'] + c['active']}")
        pl = self.import_playlist(imp)
        if pl is not None:
            text += "\n\n" + playlist_line(pl["name"])
            if total > PL_MAX_TRACKS:
                text += f" (первые {PL_MAX_TRACKS} треков файла)"
        if live and state == "running":
            rate = self._import_rate(iid, processed)
            if rate:
                left = (c["pending"] + c["active"]) / rate
                eta = f"{left / 60:.1f} ч" if left >= 60 else f"{max(1, round(left))} мин"
                text += f"\n🚀 скорость: {rate:.1f} трека/мин · осталось ≈ {eta}"
            login = imp["login"] if imp else ""
            now_playing = [v.split(": ", 1)[1] for k, v in sorted(self.active.items())
                           if k >= 100 and v.startswith(f"{login}: ")]
            if now_playing:
                text += "\n\nСейчас качаю:\n" + "\n".join(f"⏳ {x}" for x in now_playing)
        return text

    def _import_rate(self, iid: int, processed: int) -> float:
        """Скорость (треков в минуту) по фактическим замерам с момента, как очередь увидела этот импорт."""
        marks = self._rate_marks.setdefault(iid, [])
        now = time.monotonic()
        marks.append((now, processed))
        while len(marks) > 2 and now - marks[0][0] > 1800:  # окно 30 минут
            marks.pop(0)
        t0, p0 = marks[0]
        if now - t0 < 60 or processed <= p0:
            return 0.0
        return (processed - p0) / ((now - t0) / 60)

    def _import_buttons(self, iid: int) -> list[list[tuple[str, str]]]:
        imp = self.db.get_import(iid)
        state = imp["state"] if imp else ""
        if state not in ("running", "paused"):
            return [[("📄 Что не нашлось", f"impf:{iid}")]] if self.db.import_counts(iid)["failed"] else []
        toggle = ("▶️ Продолжить", f"impr:{iid}") if state == "paused" else ("⏸ Пауза", f"impp:{iid}")
        return [[toggle, ("⏹ Стоп", f"impx:{iid}")], [("📄 Что не нашлось", f"impf:{iid}")]]

    def _owned_import(self, iid: int, login: str):
        imp = self.db.get_import(iid)
        return imp if imp and imp["login"] == login else None

    def pause_import(self, iid: int, login: str) -> bool:
        imp = self._owned_import(iid, login)
        if not imp or imp["state"] != "running":
            return False
        self.db.set_import_state(iid, "paused")  # уже начатые треки докачаются, новые не берутся
        return True

    def resume_import(self, iid: int, login: str) -> bool:
        imp = self._owned_import(iid, login)
        if not imp or imp["state"] != "paused":
            return False
        self.db.set_import_state(iid, "running")
        self._rate_marks.pop(iid, None)
        self._ensure_import_feeder()
        return True

    def stop_import(self, iid: int, login: str) -> bool:
        imp = self._owned_import(iid, login)
        if not imp or imp["state"] not in ("running", "paused"):
            return False
        self.db.cancel_import(iid)
        return True

    def failed_csv(self, iid: int, login: str) -> bytes | None:
        if not self._owned_import(iid, login):
            return None
        failed = self.db.failed_import_items(iid)
        if not failed:
            return None
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Track name", "Artist name", "Album", "ISRC", "Spotify", "Reason"])
        for r in failed:
            w.writerow([r["title"], r["artist"], r["album"] or "", r["isrc"] or "", r["spotify_url"] or "",
                        (r["note"] or "").replace("\n", " ")])
        return buf.getvalue().encode("utf-8-sig")

    async def refresh_import_message(self, iid: int) -> None:
        await self._import_progress(iid)

    async def _import_progress(self, iid: int) -> None:
        imp = self.db.get_import(iid)
        if not imp or not self.messenger or not _tg_chat(imp):
            return  # импорт из приложения — прогресс только в API (GET /api/import)
        text = self.import_status_text(iid, live=True)
        buttons = self._import_buttons(iid)
        if self._last_progress_text.get(iid) == text:
            return  # Telegram ругается на правку без изменений
        self._last_progress_text[iid] = text
        try:
            if imp["status_msg"]:
                await self.messenger.edit(imp["chat_id"], imp["status_msg"], text, buttons=buttons)
            else:
                mid = await self.messenger.send(imp["chat_id"], text, buttons=buttons)
                if mid:
                    self.db.set_import_status_msg(iid, mid)
        except Exception as e:  # noqa: BLE001
            log.debug("import progress message failed: %s", e)

    async def _finish_import(self, iid: int) -> None:
        imp = self.db.get_import(iid)
        if not imp:
            return
        self.db.set_import_state(iid, "done")
        self._schedule_refresh(None, 5)
        if not self.messenger or not _tg_chat(imp):
            return
        text = self.import_status_text(iid)
        try:
            if imp["status_msg"]:
                await self.messenger.edit(imp["chat_id"], imp["status_msg"], text, buttons=self._import_buttons(iid))
                # полный итог уже в сообщении прогресса — отдельной строкой только уведомление (push)
                c = self.db.import_counts(iid)
                pl = self.import_playlist(imp)
                await self.messenger.send(imp["chat_id"], f"🎉 Импорт «{imp['filename']}» завершён: "
                                                          f"✅ {c['done']}, ♻️ {c['cached']}, ❌ {c['failed']}"
                                          + (f"\n{playlist_line(pl['name'])}" if pl is not None else ""))
            else:
                await self.messenger.send(imp["chat_id"], text)
            failed = self.db.failed_import_items(iid)
            if failed:
                import csv
                import io
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["Track name", "Artist name", "Album", "ISRC", "Spotify", "Reason"])
                for r in failed:
                    w.writerow([r["title"], r["artist"], r["album"] or "", r["isrc"] or "", r["spotify_url"] or "",
                                (r["note"] or "").replace("\n", " ")])
                await self.messenger.send_document(
                    imp["chat_id"], buf.getvalue().encode("utf-8-sig"), "not_found.csv",
                    f"Не нашлось {len(failed)} треков. Этот файл можно отправить боту ещё раз позже — "
                    "уже скачанное он пропустит мгновенно.")
        except Exception as e:  # noqa: BLE001
            log.warning("import finish message failed: %s", e)

    def cancel_imports(self, login: str) -> int:
        n = 0
        for imp in self.db.running_imports(login):
            self.db.cancel_import(imp["id"])
            n += 1
        return n

    # ------------------------------------------------------------------ регистрация
    # ------------------------------------------------------------------ лимиты регистрации (в базе: переживают
    # перезапуск). На логин — против подбора чужого логина, общий — против перебора с разных Telegram-аккаунтов.
    REG_WINDOW = 3600
    REG_PER_LOGIN = 5
    REG_GLOBAL = 30
    MIN_PASSWORD = 10

    def _reg_hits(self, key: str) -> list[float]:
        import json as _json
        try:
            hits = _json.loads(self.db.kv_get(f"regfail:{key}") or "[]")
        except ValueError:
            hits = []
        now = time.time()
        return [t for t in hits if isinstance(t, (int, float)) and now - t < self.REG_WINDOW]

    def _reg_fail(self, login: str | None) -> None:
        import json as _json
        for key in ("*", login.lower() if login else None):
            if key:
                self.db.kv_set(f"regfail:{key}", _json.dumps(self._reg_hits(key) + [time.time()]))

    def _reg_blocked(self, login: str) -> bool:
        return len(self._reg_hits("*")) >= self.REG_GLOBAL or len(self._reg_hits(login.lower())) >= self.REG_PER_LOGIN

    async def register(self, tg_id: int, invite: str, login: str, password: str) -> tuple[bool, str]:
        now = time.time()
        fails = [t for t in self._fails.get(tg_id, []) if now - t < 900]
        if len(fails) >= 5 or (LOGIN_RE.match(login or "") and self._reg_blocked(login)):
            return False, "Слишком много неудачных попыток. Попробуйте позже."
        if not CFG.invite_code or not secrets.compare_digest(invite.strip().encode(), CFG.invite_code.encode()):
            self._fails[tg_id] = fails + [now]
            self._reg_fail(None)
            return False, "Неверный инвайт-код."
        if not LOGIN_RE.match(login):
            return False, "Логин: 3–32 символа, латиница, цифры, точка, дефис, подчёркивание."
        current = self.db.user_by_tg(tg_id)
        if current and current.login.lower() != login.lower():
            return False, f"Этот Telegram уже привязан к логину {current.login}."
        is_admin_login = login.lower() == CFG.jellyfin_admin_user.lower()
        if login.lower() in RESERVED and not is_admin_login:
            return False, "Этот логин зарезервирован, выберите другой."

        existing_local = self.db.user_by_login(login)
        jf_user = await self.jf.user_by_name(login)
        if jf_user:
            # Чужой существующий аккаунт Jellyfin через /register не привязывается (иначе регистрация — оракул
            # паролей Jellyfin). Можно: повторная регистрация того же Telegram и администратор (свои аккаунты).
            own = existing_local is not None and existing_local.tg_id == tg_id
            if not own and tg_id not in CFG.admin_telegram_ids:
                self._fails.setdefault(tg_id, []).append(time.time())
                self._reg_fail(login)
                return False, ("Этот логин уже занят. Если это ваш аккаунт Jellyfin — попросите администратора "
                               "привязать его.")
            if not await self.jf.check_password(login, password):
                self._fails.setdefault(tg_id, []).append(time.time())
                self._reg_fail(login)
                return False, "Пароль от этого аккаунта не подходит."
            created = False
        else:
            if len(password) < self.MIN_PASSWORD:
                return False, f"Пароль слишком короткий (минимум {self.MIN_PASSWORD} символов)."
            jf_user = await self.jf.create_user(login, password)
            created = True
            # сразу закрываем доступ ко всем папкам — до того, как библиотека будет готова
            try:
                await self.jf.set_policy(jf_user["Id"], EnableAllFolders=False, EnabledFolders=[])
            except Exception:
                await self.jf.delete_user(jf_user["Id"])  # не оставляем аккаунт с доступом ко всему
                raise
        login = jf_user["Name"]
        uid = jf_user["Id"]
        try:
            return await self._finish_register(tg_id, login, uid, jf_user, existing_local)
        except Exception:
            if created:  # не оставляем «висящий» аккаунт без медиатеки
                try:
                    await self.jf.delete_user(uid)
                except JellyfinError:
                    pass
            raise

    async def _finish_register(self, tg_id: int, login: str, uid: str, jf_user: dict,
                               existing_local: User | None) -> tuple[bool, str]:

        root = library.user_root(login)
        root.mkdir(parents=True, exist_ok=True)
        lib = await self.jf.create_library(library_name(login), str(root))
        lib_id = lib["ItemId"]

        is_admin = bool((jf_user.get("Policy") or {}).get("IsAdministrator"))
        if not is_admin:
            await self.jf.set_policy(
                uid, IsAdministrator=False, IsHidden=True, IsDisabled=False,
                EnableAllFolders=False, EnabledFolders=[lib_id],
                # удалять можно только в своей библиотеке (из Jellyfin/Finamp/Symfonium)
                EnableContentDeletion=False, EnableContentDeletionFromFolders=[lib_id],
                EnableMediaPlayback=True, EnableAudioPlaybackTranscoding=True,
                EnableContentDownloading=True, EnableRemoteAccess=True, EnableLiveTvAccess=True,
                EnableLiveTvManagement=False, EnablePublicSharing=False, EnableLyricManagement=False,
                EnableCollectionManagement=False, EnableSubtitleManagement=False,
                EnablePlaybackRemuxing=True, EnableVideoPlaybackTranscoding=False,
                EnableSharedDeviceControl=False, EnableRemoteControlOfOtherUsers=False,
            )
        user = User(login=login, tg_id=tg_id, jf_user_id=uid, library_id=lib_id,
                    stream_token=existing_local.stream_token if existing_local else secrets.token_urlsafe(18),
                    is_admin=is_admin or tg_id in CFG.admin_telegram_ids)
        self.db.upsert_user(user)
        radio.ensure_user_wave(user)
        if CFG.wave_in_livetv:
            t = asyncio.create_task(self._safe(radio.sync_tuner(self.db, self.jf)))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        await self._isolate_others(lib_id)
        return True, login

    async def _isolate_others(self, new_lib_id: str) -> None:
        """Гарантия изоляции: ни у одного обычного пользователя нет доступа к чужим папкам."""
        try:
            libs = {u.login: u.library_id for u in self.db.all_users() if u.library_id}
            for jf_u in await self.jf.users():
                pol = jf_u.get("Policy") or {}
                if pol.get("IsAdministrator"):
                    continue
                own = libs.get(jf_u["Name"])
                if not own:  # аккаунт заведён вручную, не через бота — не трогаем
                    continue
                allowed = [own]
                if pol.get("EnableAllFolders") or sorted(pol.get("EnabledFolders") or []) != sorted(allowed):
                    await self.jf.set_policy(jf_u["Id"], EnableAllFolders=False, EnabledFolders=allowed)
        except JellyfinError as e:
            log.warning("isolation check failed: %s", e)

    async def _safe(self, coro: Awaitable) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            log.warning("background task failed: %s", e, exc_info=not _network_error(e))

    # ------------------------------------------------------------------ загрузки
    async def submit(self, user: User, text: str, notify: Notify) -> Job:
        job = Job(user=user, text=text, notify=notify)
        resolved = await sources.resolve(text)
        if resolved.error:
            await notify("⚠️ " + resolved.error)
            return job
        if not resolved.tracks:
            await notify("Ничего не нашёл по этой ссылке.")
            return job
        job.tracks, job.title = resolved.tracks, resolved.title
        if resolved.note:
            await notify(resolved.note)
        self.link_playlist(job, resolved)
        await self.run_job(job, announce=len(job.tracks) > 1)
        return job

    def admission(self, user: User, n: int) -> tuple[str, str] | None:
        """Можно ли поставить ещё n треков: (код, сообщение) — нельзя. Квота очереди на пользователя и место."""
        if not uploads.free_space_ok():
            return "disk_full", "На сервере мало места — загрузки временно остановлены."
        queued = self.__dict__.setdefault("_queued", {}).get(user.login, 0)
        if queued + n > MAX_QUEUED_PER_USER:
            return "quota", (f"В очереди уже {queued} ваших треков (максимум {MAX_QUEUED_PER_USER}) — "
                             "дождитесь, пока скачаются.")
        return None

    def _count_queued(self, login: str, delta: int) -> None:
        q = self.__dict__.setdefault("_queued", {})
        q[login] = max(0, q.get(login, 0) + delta)
        if not q[login]:
            q.pop(login, None)

    async def run_job(self, job: Job, *, announce: bool = True) -> None:
        refused = self.admission(job.user, len(job.tracks))
        if refused:
            await job.notify("⚠️ " + refused[1])
            await _view_call(job, "completed", [])
            return
        self._count_queued(job.user.login, len(job.tracks))
        job.counted = True
        if announce:
            await job.notify(f"📥 В очереди: {job.title} — {len(job.tracks)} треков. Буду сообщать о прогрессе.")
        futures = []
        loop = asyncio.get_running_loop()
        for req in job.tracks:
            fut: asyncio.Future = loop.create_future()
            futures.append(fut)
            await self.queue.put((job, req, fut))
        results: list[TrackOutcome] = await asyncio.gather(*futures)
        if await _view_call(job, "completed", results):
            return
        if len(results) > 1:
            ok = [r for r in results if r.ok]
            bad = [r for r in results if not r.ok]
            text = f"✅ Готово: {job.title}\nДобавлено {len(ok)} из {len(results)}."
            reused = sum(1 for r in ok if r.reused)
            if reused:
                text += f" ({reused} уже были в общем кэше — место на диске не тратилось)"
            if job.playlist_id and self.db.get_playlist(job.playlist_id):
                text += "\n" + playlist_line(job.playlist_name)
            if bad:
                text += "\n\nНе нашлось:\n" + "\n".join(f"• {r.req.label}" for r in bad[:25])
                if len(bad) > 25:
                    text += f"\n…и ещё {len(bad) - 25}"
            await job.notify(text)

    def _spawn_worker(self, n: int, queue: asyncio.Queue | None = None) -> asyncio.Task:
        """Воркер очереди; если он неожиданно завершился (не отмена) — запускается заново."""
        task = asyncio.create_task(self._worker(n, queue), name=f"{'import' if n >= 100 else 'dl'}-worker-{n}")

        def respawn(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            log.error("воркер %s завершился (%s) — перезапускаю", n, t.exception())
            new = self._spawn_worker(n, queue)
            for lst in (self._workers, getattr(self, "_import_workers", None) or []):
                if t in lst:
                    lst[lst.index(t)] = new
        task.add_done_callback(respawn)
        return task

    async def _worker(self, n: int, queue: asyncio.Queue | None = None) -> None:
        queue = queue if queue is not None else self.queue
        while True:
            try:
                await self._worker_step(n, queue)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — воркер не должен тихо умирать
                log.exception("воркер %s: непредвиденная ошибка", n)
                await asyncio.sleep(1)

    async def _worker_step(self, n: int, queue: asyncio.Queue) -> None:
        """Одно задание из очереди."""
        job, req, fut = await queue.get()
        outcome = TrackOutcome(req, False, f"❌ {req.label}: прервано")
        try:
            self.active[n] = f"{job.user.login}: {req.label}"
            self.active_info[n] = {"login": job.user.login, "req": req, "progress": ""}
            await _view_call(job, "started", req)
            outcome = await self._process(job, req)
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("job failed")
            outcome = TrackOutcome(req, False, f"❌ {req.label}: ошибка {e}")
        finally:
            self.active.pop(n, None)
            self.active_info.pop(n, None)
            queue.task_done()
            if job.counted:
                self._count_queued(job.user.login, -1)
            if not fut.done():
                fut.set_result(outcome)
        self._job_track_done(job, req, outcome)
        try:
            if await _view_call(job, "finished", req, outcome):
                pass
            elif len(job.tracks) == 1:
                await job.notify(outcome.text)
            elif job.origin == "user":
                job.done += 1
                if job.done % 10 == 0 and job.done < len(job.tracks):
                    await job.notify(f"… {job.title}: обработано {job.done}/{len(job.tracks)}")
        except Exception:  # noqa: BLE001
            log.exception("notify failed")

    async def _process(self, job: Job, req: TrackRequest) -> TrackOutcome:
        user = job.user
        # 1. общий кэш: если трек уже скачан кем-то — просто жёсткая ссылка
        # файл помечен проблемным — сначала качаем заново, а он — запасной; загруженный вручную выдаётся сразу
        upload_copy = None
        replace_id = req.redownload  # трек кэша, файл которого заменит скачанный
        if req.artist and req.title and not req.redownload:  # «Перекачать трек» кэш не смотрит
            cached = await asyncio.to_thread(library.lookup_cached, self.db, req.artist, req.title, isrc=req.isrc)
            has = bool(cached) and bool(await asyncio.to_thread(self.db.user_has_track, user.login, cached.id))
            if cached and cached.broken and not has:
                upload_copy, cached = cached, None
            if cached and has:
                if not _replaces_own(job):
                    return TrackOutcome(req, True, f"♻️ Уже в вашей медиатеке: {cached.artist} — {cached.title}", True,
                                        track=cached, source="cache")
                # свой трек скачивают ещё раз — значит, в медиатеке не та версия: качаем заново и заменяем файл
                replace_id, cached = cached.id, None
            if cached:
                try:
                    await asyncio.to_thread(library.give_cached, self.db, cached, user.login)
                except library.TrackGone:  # кэш очистили в эту секунду — качаем как новый
                    cached = None
                else:
                    self._schedule_refresh(user)
                    return TrackOutcome(req, True, f"♻️ {cached.artist} — {cached.title} (из общего кэша)", True,
                                        track=cached, source="cache")

        single = len(job.tracks) == 1
        # Каноническая длительность из MusicBrainz до поиска: по ней Soulseek отсеивает radio edit, live и т.п.
        if req.artist and req.title and not req.duration:
            try:
                mb = await metadata.lookup_musicbrainz(Hint(artist=req.artist, title=req.title, album=req.album,
                                                            isrc=req.isrc))
                if mb and mb.duration:
                    req.duration = mb.duration
                    req.duration_from_mb = True
            except Exception as e:  # noqa: BLE001
                log.debug("MB duration lookup failed: %s", e)
        if req.artist and req.title and not req.duration:
            # нового релиза ещё нет в MusicBrainz — длительность из Deezer (по ISRC или поиском): без неё
            # YouTube-версию другой длины (клип, укороченная редакция) не отличить от нужной
            try:
                d = await metadata.deezer_duration(req.artist, req.title, req.isrc)
                if d:
                    req.duration = d
                    req.duration_from_mb = True  # «примерная» длительность: допуск у Soulseek шире
            except Exception as e:  # noqa: BLE001
                log.debug("Deezer duration lookup failed: %s", e)

        async def progress(msg: str) -> None:
            for info in list(getattr(self, "active_info", {}).values()):  # для /api/activity → active_items[].progress
                if info.get("req") is req:
                    info["progress"] = msg
            if await _view_call(job, "progress", req, msg):
                return
            if single:
                await job.notify(f"⏳ {req.label}: {msg}")

        if req.artist and req.title:
            hint = Hint(artist=req.artist, title=req.title, album=req.album, duration=req.duration, isrc=req.isrc,
                        track_no=req.track_no, year=req.year, cover_url=req.cover_url)
        else:
            # Свободный запрос («Panic! At the Disco») — это не название песни. Что именно скачалось, знают только
            # теги и имя файла: иначе трек «High Hopes» получит теги случайной песни с похожим названием.
            hint = Hint(duration=0, cover_url="")
        dl, res, tried = None, None, set()
        for _ in range(3):  # битый файл от источника — следующий источник, а не провал задания
            dl = await (downloaders.fetch(req, progress, exclude=tried) if tried else downloaders.fetch(req, progress))
            if not dl:
                break
            try:
                extra = {"replace_id": replace_id} if replace_id else {}
                res = await library.ingest(self.db, dl.path, hint, source=dl.source, login=user.login, **extra)
            except library.BadAudio as e:
                log.warning("%s: файл от %s не подошёл (%s) — пробую другой источник", req.label, dl.source, e)
                tried.add(downloaders.SOURCE_STEPS.get(dl.source, dl.source))
                await progress(f"{dl.source}: файл повреждён, ищу в другом источнике…")
            finally:
                await asyncio.to_thread(_drop_download, dl.path)
            if res is not None:
                break
        if res is None:
            dl = None
        if not dl and replace_id:
            note = " (найденные файлы не подошли)" if tried else ""
            return TrackOutcome(req, False, f"❌ Не удалось перекачать: {req.label}{note} — старый файл оставлен")
        if not dl and upload_copy is not None:  # не скачалось — отдаём загруженный кем-то (или проблемный) файл
            try:
                await asyncio.to_thread(library.give_cached, self.db, upload_copy, user.login)
            except library.TrackGone:
                return TrackOutcome(req, False, f"❌ Не нашёл ни в одном источнике: {req.label}")
            self._schedule_refresh(user)
            return TrackOutcome(req, True, f"♻️ {upload_copy.artist} — {upload_copy.title} (из общего кэша)", True,
                                track=upload_copy, source="cache")
        if not dl or res is None:
            note = " (найденные файлы оказались повреждены или другой версии)" if tried else ""
            return TrackOutcome(req, False, f"❌ Не нашёл ни в одном источнике: {req.label}{note}")
        self._schedule_refresh(user)
        t = res.track
        if not res.reused:
            self.loudness_changed()
        try:
            if not res.reused and await asyncio.to_thread(artwork.needs_artwork, t.path):
                self._schedule_artwork()
        except Exception as e:  # noqa: BLE001
            log.debug("artwork check failed: %s", e)
        if res.replaced:
            self.loudness_changed()  # громкость нового файла — посчитать заново
            self._schedule_artwork()
        if res.reused and not res.replaced:
            return TrackOutcome(req, True, f"♻️ {t.artist} — {t.title} (уже был в общем кэше)", True,
                                track=t, source="cache")
        flags = []
        if res.info.get("cover"):
            flags.append("🖼 обложка")
        if res.info.get("synced_lyrics"):
            flags.append("🎤 синхр. текст")
        elif res.info.get("lyrics"):
            flags.append("📝 текст")
        if res.meta and res.meta.matched != "none":
            flags.append("🏷 " + res.meta.matched)
        src = {"soulseek": "Soulseek", "sockseek": "Soulseek", "spotdl": "spotDL", "yt-dlp": "yt-dlp"}.get(dl.source)
        album = f"\n💿 {t.album}" if t.album else ""
        head = "🔁 Файл заменён:" if res.replaced else "✅"
        return TrackOutcome(req, True, f"{head} {t.artist} — {t.title}{album}\n📡 {src} ({dl.detail}) → MP3 320\n"
                                       + " · ".join(flags), track=t, source=f"{src} ({dl.detail})", flags=flags)

    # ------------------------------------------------------------------ обновление библиотек
    def _schedule_refresh(self, user: User | None = None, delay: float = 15) -> None:
        """Одно отложенное сканирование на все изменения. Проверено на Jellyfin 10.11: новые файлы надёжно
        находит только POST /Library/Refresh (инкрементальный, секунды); inotify, /Items/{id}/Refresh и
        /Library/Media/Updated для добавленных файлов срабатывают не всегда."""
        t = self._refresh_pending.get("*")
        now = time.monotonic()
        if t and not t.done():
            if now - self._refresh_first > 90:  # непрерывный поток добавлений не должен откладывать скан вечно
                return
            t.cancel()
        else:
            self._refresh_first = now

        async def later() -> None:
            await asyncio.sleep(delay)
            try:
                await self.jf.refresh_all()
            except Exception as e:  # noqa: BLE001
                log.warning("refresh failed: %s", e)

        self._refresh_pending["*"] = asyncio.create_task(later())

    def library_changed(self, user: User | None = None, delay: float = 15) -> None:
        """Медиатека изменилась не через загрузку (например, «сохранить плейлист в медиатеку»)."""
        self._schedule_refresh(user, delay)

    # ------------------------------------------------------------------ зеркала плейлистов в Jellyfin
    # Best effort: у каждого участника (и владельца) — одноимённый плейлист Jellyfin только из треков его
    # медиатеки (библиотеки Jellyfin изолированы). Изменения копятся: синхронизация через PL_SYNC_DELAY
    # после последней правки; «грязные» плейлисты помнятся в kv и досинхронизируются после перезапуска.
    # Все синхронизации идут по одной (на слабой машине каталог Jellyfin читается секунды). Ошибки — только в лог.
    def playlist_changed(self, pid: int, delay: float = PL_SYNC_DELAY, *, urgent: bool = False) -> None:
        """urgent (удаление, выход, исключение) — срок не откладывается дальше уже назначенного."""
        try:
            self.db.kv_set(f"{PL_DIRTY_PREFIX}{pid}", "1")
        except Exception:  # noqa: BLE001
            log.exception("не удалось отметить плейлист %s для синхронизации", pid)
        new = time.monotonic() + delay
        old = self._pl_due.get(pid)
        self._pl_due[pid] = new if old is None else (min(old, new) if urgent else max(old, new))
        t = self._pl_tasks.get(pid)
        if t is not None and not t.done():
            return
        try:
            t = asyncio.get_running_loop().create_task(self._playlist_later(pid), name=f"pl-sync-{pid}")
        except RuntimeError:
            return
        self._pl_tasks[pid] = t
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _playlist_later(self, pid: int) -> None:
        try:
            while pid in self._pl_due:
                wait = self._pl_due[pid] - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                self._pl_due.pop(pid, None)
                try:
                    await self.sync_playlist_mirror(pid)
                except Exception:  # noqa: BLE001
                    log.exception("синхронизация зеркала плейлиста %s упала", pid)
        finally:
            if self._pl_tasks.get(pid) is asyncio.current_task():
                self._pl_tasks.pop(pid, None)

    def _resume_dirty_playlists(self, delay: float = PL_RESUME_DELAY) -> int:
        n = 0
        for key in self.db.kv_items(PL_DIRTY_PREFIX):
            try:
                pid = int(key[len(PL_DIRTY_PREFIX):])
            except ValueError:
                self.db.kv_delete(key)
                continue
            self.playlist_changed(pid, delay)
            n += 1
        return n

    async def sync_playlist_mirror(self, pid: int) -> bool:
        """True — все зеркала плейлиста актуальны (отметка «грязный» снимается)."""
        async with self._pl_lock:
            await self._empty_trash()
            ok = await self._sync_mirror(pid)
            if ok:
                self.db.kv_delete(f"{PL_DIRTY_PREFIX}{pid}")
            return ok

    async def _sync_mirror(self, pid: int) -> bool:
        prefix = f"{PL_JF_PREFIX}{pid}:"
        pl = self.db.get_playlist(pid)
        logins = [pl["owner"], *self.db.playlist_members(pid)] if pl else []
        # вышедшие/исключённые/удалённые участники и удалённый плейлист — убираем их зеркала
        for key, jf_id in self.db.kv_items(prefix).items():
            if key[len(prefix):] not in logins:
                await self._drop_mirror(pid, key[len(prefix):], jf_id)
        if not pl:
            for key in self.db.kv_items(f"{PL_HASH_PREFIX}{pid}:"):
                self.db.kv_delete(key)
            return True
        ok = True
        for login in logins:
            user = self.db.user_by_login(login)
            if not user:
                continue
            try:
                ok = await self._sync_one(pid, pl["name"], user) and ok
            except Exception as e:  # noqa: BLE001
                ok = False
                log.warning("зеркало плейлиста %s для %s не обновлено: %s", pid, login, e,
                            exc_info=not _network_error(e))
        return ok

    async def _sync_one(self, pid: int, name: str, user: User) -> bool:
        key, hkey = f"{PL_JF_PREFIX}{pid}:{user.login}", f"{PL_HASH_PREFIX}{pid}:{user.login}"
        wanted = [link for _, _, link in await asyncio.to_thread(self.db.playlist_items, pid, user.login) if link]
        old = self.db.kv_get(key)
        digest = hashlib.sha1("\n".join([name, *wanted]).encode("utf-8", "replace")).hexdigest()
        if self.db.kv_get(hkey) == digest and bool(old) == bool(wanted):
            return True  # состав и имя не менялись — каталог Jellyfin не читаем
        if not wanted:
            if old:
                await self._drop_mirror(pid, user.login, old)
            self.db.kv_set(hkey, digest)
            return True
        by_path = await self._jf_paths(user.jf_user_id)
        ids = [by_path[p] for p in wanted if p in by_path]
        missing = len(wanted) - len(ids)
        if not ids:
            # Jellyfin ещё не просканировал файлы — существующее зеркало не трогаем
            log.warning("зеркало плейлиста %s для %s: ни один из %d треков не найден в Jellyfin, оставляю как есть",
                        pid, user.login, len(wanted))
            return False
        if missing:
            log.info("зеркало плейлиста %s для %s: не найдено в Jellyfin %d из %d", pid, user.login, missing,
                     len(wanted))
        if len(ids) > PL_MIRROR_MAX:
            log.info("зеркало плейлиста %s для %s обрезано до %d треков из %d", pid, user.login, PL_MIRROR_MAX,
                     len(ids))
            ids = ids[:PL_MIRROR_MAX]
        # пересоздание (менять состав API-ключом Jellyfin 10.11 не даёт); по имени не ищем — ручной не тронем
        new = await self.jf.create_playlist_full(user.jf_user_id, name, ids)
        self.db.kv_set(key, new)
        if old and old != new:
            await self._delete_jf_playlist(old)
        if missing:
            self.db.kv_delete(hkey)  # досинхронизируем, когда Jellyfin увидит остальные
            return False
        self.db.kv_set(hkey, digest)
        return True

    async def _jf_paths(self, jf_user_id: str) -> dict[str, str]:
        """Путь файла -> id элемента Jellyfin; кэш на PL_PATHS_TTL (500 треков на слабой машине — секунды)."""
        hit = self._jf_paths_cache.get(jf_user_id)
        if hit and time.monotonic() - hit[0] < PL_PATHS_TTL:
            return hit[1]
        by_path = {it["Path"]: it["Id"] for it in await self.jf.audio_items(jf_user_id)
                   if it.get("Path") and it.get("Id")}
        self._jf_paths_cache[jf_user_id] = (time.monotonic(), by_path)
        return by_path

    async def _delete_jf_playlist(self, jf_id: str) -> bool:
        try:
            await self.jf.request("DELETE", f"/Items/{jf_id}", ok404=True)
            self.db.kv_delete(f"{PL_TRASH_PREFIX}{jf_id}")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось удалить плейлист Jellyfin %s (повторю позже): %s", jf_id, e,
                        exc_info=not _network_error(e))
            self.db.kv_set(f"{PL_TRASH_PREFIX}{jf_id}", "1")
            return False

    async def _drop_mirror(self, pid: int, login: str, jf_id: str) -> None:
        await self._delete_jf_playlist(jf_id)  # не удалось — id останется в корзине pl_jf_trash:
        self.db.kv_delete(f"{PL_JF_PREFIX}{pid}:{login}")
        self.db.kv_delete(f"{PL_HASH_PREFIX}{pid}:{login}")

    async def _empty_trash(self) -> None:
        for key in self.db.kv_items(PL_TRASH_PREFIX):
            await self._delete_jf_playlist(key[len(PL_TRASH_PREFIX):])

    def _playlists_of(self, track_ids: Any) -> set[int]:
        pids: set[int] = set()
        for tid in track_ids:
            pids.update(self.db.playlists_with_track(tid))
        return pids

    def _playlists_touched(self, pids: Any) -> None:
        """Трек ушёл из медиатеки (и, возможно, из плейлистов вместе с файлом) — зеркала этих плейлистов пересобрать.
        Плейлисты собирать до удаления: после очистки кэша строк трека в них уже нет."""
        for pid in pids:
            self.playlist_changed(pid)

    async def refresh_now(self) -> None:
        """Сканирование прямо сейчас с ожиданием окончания (нужно перед сборкой плейлиста «Моя Волна»)."""
        try:
            await self.jf.refresh_all()
        except Exception as e:  # noqa: BLE001
            log.warning("library refresh failed: %s", e)
            return
        for _ in range(60):
            await asyncio.sleep(3)
            try:
                tasks = await self.jf.request("GET", "/ScheduledTasks", params={"isHidden": False})
                scan = next((x for x in tasks or [] if x.get("Key") == "RefreshLibrary"), None)
                if not scan or scan.get("State") == "Idle":
                    return
            except Exception:  # noqa: BLE001
                return

    # ------------------------------------------------------------------ удаление
    async def delete_track(self, user: User, track_id: int) -> str:
        track = self.db.get_track(track_id)
        if not track or not self.db.user_has_track(user.login, track_id):
            return "Трек не найден в вашей медиатеке."
        pids = self._playlists_of([track_id])
        await asyncio.to_thread(library.unlink_from_user, self.db, track, user.login)
        self._schedule_refresh(user, 5)
        self._playlists_touched(pids)
        return f"🗑 Удалено из вашей медиатеки: {track.artist} — {track.title}"

    async def delete_account(self, user: User) -> None:
        login = user.login
        playlists = self.db.user_playlists(login)
        owned = [p["id"] for p in playlists if p["owner"] == login]
        # треки плейлистов, которые исчезнут вместе с владельцем: файлы кэша чистим после удаления
        orphan_ids = {tid for pid in owned for tid in self.db.playlist_track_ids(pid)}

        def unlink_all() -> set[int]:
            pids: set[int] = set()
            for t, _ in self.db.find_user_tracks(login, "", 1000000):
                pids |= self._playlists_of([t.id])
                library.unlink_from_user(self.db, t, login)
            return pids
        touched = await asyncio.to_thread(unlink_all)
        try:
            await self.jf.delete_library(library_name(login))
        except JellyfinError as e:
            log.warning("не удалось удалить библиотеку %s: %s", login, e)
        mirrors = {k: v for k, v in self.db.kv_items(PL_JF_PREFIX).items() if k.endswith(":" + login)}
        wave_key = f"wave_playlist:{login}"
        own = {v.replace("-", "") for v in [*mirrors.values(), self.db.kv_get(wave_key)] if v}
        try:  # плейлисты удалённого пользователя Jellyfin показал бы всем остальным
            for pl in await self.jf.user_playlists(user.jf_user_id):
                # у админа аккаунт Jellyfin остаётся — удаляем только созданные ботом (волна, зеркала)
                if user.is_admin and pl["Id"].replace("-", "") not in own:
                    continue
                await self.jf.request("DELETE", f"/Items/{pl['Id']}")
        except JellyfinError as e:
            log.warning("не удалось удалить плейлисты %s: %s", login, e)
        for k in mirrors:
            self.db.kv_delete(k)
            self.db.kv_delete(PL_HASH_PREFIX + k[len(PL_JF_PREFIX):])
        self.db.kv_delete(wave_key)
        if not user.is_admin:
            try:
                await self.jf.delete_user(user.jf_user_id)
            except JellyfinError as e:
                log.warning("не удалось удалить пользователя Jellyfin %s: %s", login, e)

        def cleanup() -> None:
            import shutil
            shutil.rmtree(library.user_root(login), ignore_errors=True)
            self.db.delete_user(login)  # плейлисты владельца и членство удаляются каскадом
            for tid in orphan_ids:
                t = self.db.get_track(tid)
                if t:
                    library.purge_if_unused(self.db, t)
        await asyncio.to_thread(cleanup)
        for p in playlists:  # зеркала у оставшихся участников — убрать/пересобрать
            self.playlist_changed(p["id"], 1, urgent=True)
        self._playlists_touched(touched - set(owned))  # плейлисты владельца удалены вместе с ним


def _replaces_own(job: Job) -> bool:
    """Повторная загрузка трека, который уже в медиатеке, заменяет его файл, если трек просили отдельно (бот,
    Mini App). Альбомы, импорт, волна и загрузка в плейлист свои треки по-прежнему пропускают."""
    return len(job.tracks) == 1 and job.origin in ("user", "webapp") and not job.playlist_id


def _drop_download(path: Path) -> None:
    """Скачанный исходник и временная папка загрузки — после ingest (в потоке)."""
    path.unlink(missing_ok=True)
    downloaders.cleanup_after(path)


def queue_position(svc: Service) -> int:
    return svc.queue.qsize()


__all__ = ["Service", "Job", "library_name", "queue_position", "Path"]


def cleanup_stale_incoming(max_age_h: float = 3) -> int:
    """Файлы во временных папках старше max_age_h часов — это загрузки от пиров, которых бот не дождался
    (перешёл к другому, а первый всё равно докачал), или остатки прерванных задач. Удаляем."""
    cutoff = time.time() - max_age_h * 3600
    removed = 0
    root = CFG.incoming_dir
    for sub in ("slskd", "sockseek", "spotdl", "ytdlp", "work"):
        d = root / sub
        if not d.exists():
            continue
        for p in sorted(d.rglob("*"), key=lambda x: -len(x.parts)):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
                elif p.is_dir() and p != d and not any(p.iterdir()) and p.stat().st_mtime < cutoff:
                    p.rmdir()
            except OSError:
                continue
    if removed:
        log.info("очищено временных файлов: %d", removed)
    return removed
