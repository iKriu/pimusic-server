"""Интернет-радио (.strm) и персональная живая станция «Моя Волна» (HTTP MP3-поток через ffmpeg)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import re
import time
from collections import Counter, deque
from pathlib import Path

from aiohttp import web

from .config import CFG
from .db import DB, User
from .jellyfin import Jellyfin
from .util import is_public_url, safe_name

log = logging.getLogger(__name__)

CATALOG_TTL = 600
IDLE_STOP = 25  # секунд без слушателей — останавливаем станцию
LISTENER_QUEUE = 256  # кусков MP3 (~15–25 с звука) в очереди слушателя; переполнилась — слушатель отключается
NO_DATA_TIMEOUT = 30  # секунд без данных от станции — отдаём клиенту конец потока (он переподключится)
PCM = ["-f", "s16le", "-ar", "44100", "-ac", "2"]

DEFAULT_STATIONS = [
    ("Radio Paradise (Main Mix)", "http://stream.radioparadise.com/mp3-320"),
    ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-256-mp3"),
    ("SomaFM Indie Pop Rocks", "https://ice1.somafm.com/indiepop-128-mp3"),
    ("Radio Swiss Jazz", "http://stream.srg-ssr.ch/m/rsj/mp3_128"),
]


# ------------------------------------------------------------------ станции
# В Jellyfin 10.11 файлы .strm внутри музыкальных библиотек НЕ воспроизводятся (jellyfin/jellyfin#8201),
# поэтому интернет-радио подключается официальным способом — M3U-тюнером Live TV. Файлы .strm мы всё равно
# пишем в /media/radio/strm (их удобно открыть в VLC/Symfonium), но в библиотеки Jellyfin их не добавляем.
M3U_PATH_NAME = "radio.m3u"


def m3u_path() -> Path:
    return CFG.radio_dir / M3U_PATH_NAME


_PRIVATE_HOSTS = ("localhost", "jellyfin", "slskd", "bot", "pimusic-jellyfin", "pimusic-slskd", "pimusic-bot")


def valid_station(name: str, url: str) -> bool:
    """Защита общего M3U: без переводов строк/управляющих символов, только публичные http(s)-потоки."""
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name + url) or len(url) > 2000:
        return False
    return is_public_url(url)


def _m3u_name(name: str) -> str:
    return re.sub(r'[\r\n",]', " ", name).strip()


def write_strm(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url.strip() + "\n", encoding="utf-8")


def station_path(owner: str | None, name: str) -> Path:
    return CFG.radio_dir / "strm" / (owner or "_shared") / f"{safe_name(name)}.strm"


def wave_url(user: User) -> str:
    return f"{CFG.stream_public_base}/stream/{user.login}/{user.stream_token}.mp3"


def ensure_user_wave(user: User) -> Path:
    p = station_path(user.login, "Моя Волна")
    write_strm(p, wave_url(user))
    return p


def write_m3u(db: DB) -> Path:
    """Общий плейлист станций для M3U-тюнера Jellyfin (#EXTINF обязателен, иначе канал не появится)."""
    lines = ["#EXTM3U"]
    for i, r in enumerate(db.list_radio(None), start=1):
        if r["owner"] is not None:
            continue
        if not valid_station(r["name"], r["url"]):
            continue
        lines.append(f'#EXTINF:-1 tvg-id="pimusic-{r["id"]}" tvg-chno="{i}" group-title="Радио",{_m3u_name(r["name"])}')
        lines.append(r["url"])
    if CFG.wave_in_livetv:
        for u in db.all_users():
            lines.append(f'#EXTINF:-1 tvg-id="wave-{u.login}" group-title="Моя Волна",Моя Волна · {u.login}')
            lines.append(wave_url(u))
    p = m3u_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def ensure_default_stations(db: DB) -> None:
    CFG.radio_dir.mkdir(parents=True, exist_ok=True)
    if not any(r["owner"] is None for r in db.list_radio(None)):
        for name, url in DEFAULT_STATIONS:
            p = station_path(None, name)
            write_strm(p, url)
            db.add_radio(None, name, url, str(p))
    write_m3u(db)


async def sync_tuner(db: DB, jf: Jellyfin) -> None:
    write_m3u(db)
    await jf.upsert_m3u_tuner(str(m3u_path()), "PiMusic Radio")
    with contextlib.suppress(Exception):
        await jf.refresh_guide()


async def refresh_wave_playlist(jf: Jellyfin, user: User, size: int = 100, db: DB | None = None) -> int:
    """Личный приватный плейлист «Моя Волна» — работает во всех клиентах (Finamp, Symfonium, веб)."""
    picker = Picker(jf, user)
    ids: list[str] = []
    for _ in range(size * 2):
        it = await picker.next()
        if it is None:
            break
        if it["Id"] not in ids:
            ids.append(it["Id"])
        if len(ids) >= size or len(ids) >= len(picker.items):
            break
    if ids:
        key = f"wave_playlist:{user.login}"
        pid = await jf.replace_playlist(user.jf_user_id, "Моя Волна", ids, old_id=db.kv_get(key) if db else None)
        if db:
            db.kv_set(key, pid)  # по этому id бот отличает свой плейлист от лишних при очистке
    return len(ids)


# ------------------------------------------------------------------ подбор треков
class Picker:
    """Взвешенный случайный выбор: лайки, прослушивания, любимые жанры и артисты, без повторов."""

    def __init__(self, jf: Jellyfin, user: User, db: DB | None = None):
        self.jf, self.user, self.db = jf, user, db
        self.items: list[dict] = []
        self.weights: list[float] = []
        self.loaded_at = 0.0
        self.recent: deque[str] = deque(maxlen=40)
        self.last_artist = ""
        self._loading: asyncio.Task | None = None

    def _ensure_background_load(self) -> None:
        """Каталог Jellyfin на слабой машине грузится долго (~8 с на 500 треков) — грузим в фоне, станция не ждёт."""
        if self._loading is None or self._loading.done():
            async def run() -> None:
                try:
                    await self._load()
                except Exception as e:  # noqa: BLE001
                    log.warning("wave[%s]: не удалось загрузить каталог: %s", self.user.login, e)
            self._loading = asyncio.create_task(run(), name=f"wave-catalog-{self.user.login}")

    async def _quick_pick(self) -> dict | None:
        """Пока каталог Jellyfin грузится — случайный трек медиатеки из базы бота (мгновенно)."""
        if self.db is None:
            return None
        rows = self.db.find_user_tracks(self.user.login, "", 3000)
        random.shuffle(rows)
        recent = set(self.recent)
        for t, link in rows[:50]:
            key = f"local-{t.id}"
            if key in recent and len(rows) > 1:
                continue
            if await asyncio.to_thread(Path(link).exists):
                self.recent.append(key)
                self.last_artist = (t.artist or "").lower()
                return {"Id": key, "Path": link, "Name": t.title, "Artists": [t.artist], "Album": t.album or "",
                        "AlbumArtist": t.artist}
        return None

    async def _load(self) -> None:
        items = await self.jf.audio_items(self.user.jf_user_id)
        # Существование файла проверяется при выборе трека (next) — массовая проверка тысячи файлов на SD-карте
        # давала 10–15 с тишины при первом подключении к станции.
        items = [i for i in items if (i.get("Path") or "").lower().endswith((".mp3", ".flac", ".m4a", ".ogg"))]
        genre_aff: Counter[str] = Counter()
        artist_aff: Counter[str] = Counter()
        for it in items:
            ud = it.get("UserData") or {}
            score = (3.0 if ud.get("IsFavorite") else 0.0) + math.log1p(ud.get("PlayCount") or 0)
            for g in it.get("Genres") or []:
                genre_aff[g.lower()] += score
            for a in it.get("Artists") or [it.get("AlbumArtist") or ""]:
                artist_aff[a.lower()] += score
        gmax = max(genre_aff.values(), default=0) or 1
        amax = max(artist_aff.values(), default=0) or 1
        weights = []
        for it in items:
            ud = it.get("UserData") or {}
            w = 1.0
            w += 4.0 if ud.get("IsFavorite") else 0.0
            w += 1.2 * math.log1p(ud.get("PlayCount") or 0)
            w += 2.0 * max((genre_aff[g.lower()] / gmax for g in it.get("Genres") or []), default=0)
            w += 2.0 * max((artist_aff[a.lower()] / amax for a in it.get("Artists") or []), default=0)
            if ud.get("Likes") is False:  # «дизлайк» в некоторых клиентах
                w *= 0.1
            weights.append(w)
        self.items, self.weights, self.loaded_at = items, weights, time.monotonic()
        log.info("wave[%s]: каталог %d треков", self.user.login, len(items))

    async def next(self) -> dict | None:
        stale = not self.items or time.monotonic() - self.loaded_at > CATALOG_TTL
        if stale and self.db is not None:
            # живая станция: каталог обновляется в фоне, пока играет текущий (или быстрый) выбор
            self._ensure_background_load()
            if not self.items:
                quick = await self._quick_pick()
                if quick is not None:
                    return quick
                await self._loading  # медиатека пуста в базе бота — ждём Jellyfin
        elif stale:
            try:
                await self._load()
            except Exception as e:  # noqa: BLE001
                log.warning("wave[%s]: не удалось загрузить каталог: %s", self.user.login, e)
        if not self.items:
            return None
        # окно без повторов — до 2/3 каталога: нет повтора подряд даже в маленькой медиатеке, но остаётся случайность
        size = min(len(self.recent), max(1, len(self.items) * 2 // 3))
        window = list(self.recent)[-size:] if self.recent and len(self.items) > 1 else []
        recent = set(window)
        for _ in range(30):
            weights = []
            for it, w in zip(self.items, self.weights):
                if it["Id"] in recent and len(self.items) > 1:
                    weights.append(0.0)
                    continue
                artist = ((it.get("Artists") or [""])[0]).lower()
                weights.append(w * (0.25 if artist and artist == self.last_artist else 1.0))
            if not any(weights):
                self.recent.clear()
                recent = set()
                continue
            it = random.choices(self.items, weights=weights, k=1)[0]
            if await asyncio.to_thread(Path(it["Path"]).exists):
                self.recent.append(it["Id"])
                self.last_artist = ((it.get("Artists") or [""])[0]).lower()
                return it
            # файла больше нет (удалён из медиатеки) — убираем из каталога, чтобы не выбирать снова
            idx = self.items.index(it)
            del self.items[idx]
            del self.weights[idx]
            if not self.items:
                return None
            size = min(len(self.recent), max(1, len(self.items) * 2 // 3))
            recent = set(list(self.recent)[-size:]) if self.recent and len(self.items) > 1 else set()
        return None


# ------------------------------------------------------------------ живая станция
class Station:
    """Одна станция на пользователя: один энкодер, раздача всем слушателям (как настоящее радио)."""

    def __init__(self, jf: Jellyfin, user: User, db: DB | None = None):
        self.user = user
        self.picker = Picker(jf, user, db)
        self.listeners: set[asyncio.Queue[bytes | None]] = set()
        self.burst: deque[bytes] = deque(maxlen=48)  # ~200 КБ стартового буфера для мгновенного старта
        self.task: asyncio.Task | None = None
        self.now_playing = ""
        self.current: dict | None = None  # текущий Jellyfin-элемент (Name, Artists, Album, Path, RunTimeTicks…)
        self.started_at = 0.0  # time.time() начала текущего трека
        self._idle_since = time.monotonic()
        self._stopping = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done() and not self._stopping

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=LISTENER_QUEUE)
        if not self.running:
            # станция стояла: старый стартовый буфер — от прошлого трека, новому слушателю он не нужен
            self._stopping = False
            self.burst.clear()
            self.task = asyncio.create_task(self._run(), name=f"wave-{self.user.login}")
        for chunk in self.burst:
            q.put_nowait(chunk)
        self.listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.listeners.discard(q)
        if not self.listeners:
            self._idle_since = time.monotonic()

    @staticmethod
    def _close(q: asyncio.Queue) -> None:
        while not q.empty():
            q.get_nowait()
        q.put_nowait(None)

    def _fanout(self, chunk: bytes) -> None:
        self.burst.append(chunk)
        for q in list(self.listeners):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:  # медленный слушатель — отключаем
                self.listeners.discard(q)
                self._close(q)
                log.warning("wave[%s]: слушатель не успевает забирать поток — отключён", self.user.login)

    async def _run(self) -> None:
        enc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *PCM, "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", CFG.stream_bitrate, "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        assert enc.stdin and enc.stdout

        async def pump_out() -> None:
            while True:
                chunk = await enc.stdout.read(4096)
                if not chunk:
                    return
                self._fanout(chunk)

        out_task = asyncio.create_task(pump_out())
        log.info("wave[%s]: станция запущена", self.user.login)
        try:
            while True:
                if not self.listeners and time.monotonic() - self._idle_since > IDLE_STOP:
                    break
                item = await self.picker.next()
                if item is None:
                    self.current, self.now_playing = None, ""
                    await self._silence(enc.stdin, 5)
                    continue
                self.current, self.started_at = item, time.time()
                self.now_playing = f"{(item.get('Artists') or [''])[0]} — {item.get('Name')}"
                await self._play(item["Path"], enc.stdin)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._stopping = True
            self.current, self.now_playing = None, ""
            listeners, self.listeners = self.listeners, set()
            with contextlib.suppress(Exception):
                enc.stdin.close()
            with contextlib.suppress(Exception):
                enc.kill()
            out_task.cancel()
            for q in listeners:
                self._close(q)
            log.info("wave[%s]: станция остановлена", self.user.login)

    async def _play(self, path: str, sink: asyncio.StreamWriter) -> None:
        dec = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-re", "-i", path,
            "-vn", *PCM, "-af", "afade=t=in:d=0.4", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
        )
        assert dec.stdout
        try:
            while True:
                if not self.listeners and time.monotonic() - self._idle_since > IDLE_STOP:
                    break
                data = await dec.stdout.read(16384)
                if not data:
                    break
                sink.write(data)
                await sink.drain()
        finally:
            if dec.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    dec.kill()
            await dec.wait()

    async def _silence(self, sink: asyncio.StreamWriter, seconds: int) -> None:
        frame = b"\x00" * 44100 * 4 // 10
        for _ in range(seconds * 10):
            sink.write(frame)
            await sink.drain()
            await asyncio.sleep(0.1)


class RadioServer:
    def __init__(self, db: DB, jf: Jellyfin):
        self.db, self.jf = db, jf
        self.stations: dict[str, Station] = {}
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/stream/{login}/{token}.mp3", self.stream)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", CFG.stream_port).start()
        log.info("радио-сервер слушает :%d", CFG.stream_port)

    async def stop(self) -> None:
        for st in self.stations.values():
            if st.task:
                st.task.cancel()
        if self._runner:
            await self._runner.cleanup()

    async def health(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True})  # кто что слушает — не публичная информация

    def now_playing(self, login: str) -> str:
        st = self.stations.get(login)
        return st.now_playing if st and st.running else ""

    def station(self, login: str) -> Station | None:
        """Работающая станция пользователя (или None)."""
        st = self.stations.get(login)
        return st if st and st.running else None

    async def stream(self, req: web.Request) -> web.StreamResponse:
        user = self.db.user_by_token(req.match_info["login"], req.match_info["token"])
        if not user:
            raise web.HTTPNotFound()
        st = self.stations.get(user.login)
        if st is None or st.user.jf_user_id != user.jf_user_id:
            st = self.stations[user.login] = Station(self.jf, user, self.db)
        resp = web.StreamResponse(headers={
            "Content-Type": "audio/mpeg", "Cache-Control": "no-cache, no-store", "Connection": "close",
            "icy-name": f"Моя Волна — {user.login}", "icy-br": CFG.stream_bitrate.rstrip("k"),
        })
        await resp.prepare(req)
        if req.method == "HEAD":
            return resp
        q = st.subscribe()
        origin = (req.headers.get("Origin") or "-")[:100]
        log.info("wave[%s]: слушатель подключился (origin %s)", user.login, origin)
        began, sent, why = time.monotonic(), 0, "клиент отключился"
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=NO_DATA_TIMEOUT)
                except asyncio.TimeoutError:
                    why = f"нет данных {NO_DATA_TIMEOUT} с"
                    break
                if chunk is None:
                    why = "станция закрыла поток"
                    break
                if req.transport is None or req.transport.is_closing():
                    break
                await resp.write(chunk)
                sent += len(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            st.unsubscribe(q)
            log.info("wave[%s]: слушатель отключился через %.0f с, отдано %d КБ (%s)",
                     user.login, time.monotonic() - began, sent // 1024, why)
        return resp
