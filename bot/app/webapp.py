"""Telegram Mini App (WebApp): SPA в /app/ и JSON API в /api/ поверх того же aiohttp-сервера, что и радио.

Авторизация: каждый запрос к /api/ несёт заголовок X-Telegram-Init-Data (Telegram.WebApp.initData),
подпись проверяется по официальному алгоритму, Telegram-пользователь сопоставляется с логином бота.
Обложки отдаются по подписанным ссылкам: <img> не умеет слать заголовки.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import functools
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, quote, unquote

from aiohttp import web, web_fileresponse

from . import auth, downloaders, importer, libview, library, radio, search, sources, util, wave
from . import lyrics as lyrics_mod
from . import lyrics_fix
from . import uploads as uploads_mod
from .config import CFG
from .db import DB, DeviceLimit, PlaylistLimit, User
from .service import Job, Service
from .sources import TrackRequest
from .util import match_key

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
OPENAPI_PATH = Path(__file__).resolve().parent / "openapi.json"  # описание API для сторонних клиентов
API_VERSION = 1  # растёт только при несовместимых изменениях; новые поля и эндпоинты его не меняют
INIT_HEADER = "X-Telegram-Init-Data"
ACTIVITY_LEN = 50
MAX_QUERY = 200
STREAM_TTL = 12 * 3600
MAX_TRACK_IDS = 500     # /api/tracks
MAX_PLAY = 3000         # /api/library/play
PL_MAX_NAME = 100
PL_MAX_DESC = 300
PL_MAX_PER_USER = 200
PL_MAX_TRACKS = 5000
PL_MAX_MEMBERS = 20
MAX_ID = 2 ** 62        # id из запроса: 0 < id < MAX_ID (больше SQLite не примет)
PL_COVER_SCAN = 60      # сколько первых треков смотреть в поисках обложек мозаики

_STATE_KEY = web.AppKey("pimusic_webapp", object)
# request[...] — id устройства, если вошли по Bearer-токену
_DEVICE_KEY: Any = (web.RequestKey("pimusic_device_id", int) if hasattr(web, "RequestKey")
                    else "pimusic_device_id")
SETTING_KEY_RE = re.compile(r"^[a-z0-9._-]{1,80}$")
SETTING_MAX_VALUE = 32 * 1024
SETTING_MAX_KEYS = 100
TOO_MANY_DEVICES = (f"Уже {auth.MAX_DEVICES} устройств — выйдите на одном из старых (/devices в боте или "
                    "«Настройки → Устройства»).")
UPLOAD_GLOBAL_CONCURRENCY = 4   # одновременных запросов /api/upload на сервер
UPLOAD_USER_CONCURRENCY = 2     # и на пользователя
UPLOAD_READ_TIMEOUT = 60        # сек без данных от клиента — обрыв
UPLOAD_SPACE_STEP = 64 * 1024 * 1024  # свободное место перепроверяется каждые 64 МБ
# шрифты Mini App (static/fonts/*.woff2): в slim-образе и на Windows mimetypes их не знает — иначе
# application/octet-stream, а с nosniff браузер такой шрифт может не принять
# (aiohttp ≥ 3.10 держит для FileResponse свой экземпляр MimeTypes — регистрируем и там)
for _types in (mimetypes, getattr(web_fileresponse, "CONTENT_TYPES", None)):
    if _types is not None:
        _types.add_type("font/woff2", ".woff2")
        _types.add_type("font/woff", ".woff")
REDOWNLOAD_PER_HOUR = 10
REDOWNLOAD_LOCK_TTL = 3600  # сек: страховка, если задание перекачки не завершилось штатно
LYRICS_REFETCH_PER_HOUR = 20   # «Найти текст заново»: запросов на пользователя в час
LYRICS_REFETCH_TIMEOUT = 100   # сек на поиск: LRCLIB + syncedlyrics (у прокси /api/ таймаут 120 с)
PLAYLIST_SORTS = ("manual",) + libview.TRACK_SORTS
IMPORT_EXTS = (".txt", ".csv", ".tsv")  # списки треков для импорта (POST /api/import/file)
IMPORT_MAX_BYTES = 2 * 1024 * 1024
USE_IMPORT_MSG = "Это список треков, а не аудио — загрузите его как импорт списка (TXT/CSV)."
CORS_ALLOW_HEADERS = "Authorization, Content-Type, Range, X-Telegram-Init-Data"
CORS_EXPOSE_HEADERS = "Content-Range, Accept-Ranges, Content-Length, Content-Type"


# ------------------------------------------------------------------ подписи
def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400,
                       now: float | None = None) -> dict | None:
    """Проверка Telegram.WebApp.initData. Возвращает поля (user уже разобран из JSON) или None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received = data.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    # compare_digest на str падает TypeError для не-ASCII → сравниваем байты
    if not hmac.compare_digest(calc.encode(), received.lower().encode("utf-8", "replace")):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    now = time.time() if now is None else now
    if auth_date <= 0 or now - auth_date > max_age or auth_date - now > 300:
        return None
    out: dict[str, Any] = dict(data)
    if "user" in data:
        try:
            out["user"] = json.loads(data["user"])
        except ValueError:
            return None
        if not isinstance(out["user"], dict):
            return None
    return out


def _cover_key(bot_token: str) -> bytes:
    return hmac.new(b"PiMusicCover", bot_token.encode(), hashlib.sha256).digest()


COVER_TTL = 24 * 3600


def cover_expiry(now: float | None = None) -> int:
    """Сейчас + 24 ч, вверх до часа: в течение часа ссылка стабильна (кэш браузера)."""
    t = int(time.time() if now is None else now) + COVER_TTL
    return -(-t // 3600) * 3600


def cover_signature(bot_token: str, login: str, track_id: int, exp: int | None = None) -> str:
    exp = cover_expiry() if exp is None else exp
    msg = f"{login}:{track_id}:{exp}".encode("utf-8", "replace")
    return hmac.new(_cover_key(bot_token), msg, hashlib.sha256).hexdigest()[:32]


def cover_url(bot_token: str, login: str, track_id: int, now: float | None = None) -> str:
    exp = cover_expiry(now)
    return f"/api/cover/{track_id}?u={quote(login)}&s={cover_signature(bot_token, login, track_id, exp)}&e={exp}"


def _stream_key(bot_token: str) -> bytes:
    return hmac.new(b"PiMusicStream", bot_token.encode(), hashlib.sha256).digest()


def stream_signature(bot_token: str, login: str, track_id: int, exp: int) -> str:
    msg = f"{login}:{track_id}:{exp}".encode("utf-8", "replace")
    return hmac.new(_stream_key(bot_token), msg, hashlib.sha256).hexdigest()[:32]


def stream_expiry(now: float | None = None) -> int:
    """Сейчас + 12 ч, вверх до целого часа: в течение часа ссылка не меняется — работает кэш браузера."""
    t = int(time.time() if now is None else now) + STREAM_TTL
    return -(-t // 3600) * 3600


def stream_url(bot_token: str, login: str, track_id: int, now: float | None = None) -> str:
    exp = stream_expiry(now)
    return (f"/api/stream/{track_id}?u={quote(login)}&e={exp}"
            f"&s={stream_signature(bot_token, login, track_id, exp)}")


def _cover_path(track_path: str, link_path: str | None = None) -> Path | None:
    for base in (track_path, link_path):
        if base:
            p = Path(base).parent / "cover.jpg"
            if p.is_file():
                return p
    return None


def _cover_finder() -> Callable[[str, str | None], Path | None]:
    """_cover_path с памятью по папкам: у треков одного альбома одна обложка — один is_file() на запрос."""
    memo: dict[tuple[str, str], Path | None] = {}

    def find(track_path: str, link_path: str | None = None) -> Path | None:
        key = (str(Path(track_path).parent) if track_path else "", str(Path(link_path).parent) if link_path else "")
        if key not in memo:
            memo[key] = _cover_path(track_path, link_path)
        return memo[key]
    return find


def _valid_id(v: Any) -> int | None:
    """Положительный id из JSON/строки запроса (0 < id < 2**62); bool, дробные и мусор — None."""
    if isinstance(v, (bool, float)):
        return None
    try:
        i = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return i if 0 < i < MAX_ID else None


def artist_signature(bot_token: str, login: str, folder: str) -> str:
    msg = f"artist:{login}:{folder}".encode("utf-8", "replace")
    return hmac.new(_cover_key(bot_token), msg, hashlib.sha256).hexdigest()[:32]


def artist_image_url(bot_token: str, login: str, folder: str) -> str:
    return (f"/api/artist-image?u={quote(login)}&a={quote(folder, safe='')}"
            f"&s={artist_signature(bot_token, login, folder)}")


def _artist_image_path(rows: list[tuple[Any, str]]) -> Path | None:
    """folder.jpg папки исполнителя: в медиатеке пользователя или в общем кэше."""
    for t, link in rows[:5]:
        for base in (link, t.path):
            if base:
                p = Path(base).parent.parent / "folder.jpg"
                if p.is_file():
                    return p
    return None


# ------------------------------------------------------------------ сериализация
def _track_json(i: int, t: TrackRequest) -> dict:
    # key/isrc — чтобы клиент сверял ряд с ключами своей медиатеки (/api/library/keys) и показывал ✓
    return {"i": i, "artist": t.artist, "title": t.title, "album": t.album,
            "duration": search.fmt_duration(t.duration),
            "key": match_key(t.artist, t.title) if t.artist and t.title else None, "isrc": t.isrc or None,
            "cover": t.cover_url or None, "preview": search.valid_preview(t.preview) or None}


def _ctrack_json(i: int, t: TrackRequest) -> dict:
    return {**_track_json(i, t), "year": t.year, "no": t.track_no}


def _album_json(i: int, a: search.Album) -> dict:
    return {"i": i, "id": a.id, "title": a.title, "year": a.year, "kind": a.kind, "artist": a.artist,
            "artist_id": a.artist_id, "cover": a.cover or None}


def _artist_json(a: search.Artist) -> dict:
    return {"id": a.id, "name": a.name, "picture": a.picture or a.picture_xl or None, "fans": a.fans}


MAX_FOLDER = 255
MAX_PICK = 500  # сколько треков можно выбрать одним запросом kind=tracks


def _err(status: int, error: str, **extra: Any) -> web.Response:
    return web.json_response({"error": error, **extra}, status=status)


Handler = Callable[[web.Request, User, int], Awaitable[web.StreamResponse]]


class WebApp:
    def __init__(self, *, db: DB, svc: Service, jf: Any, bot_token: str, radio_server: Any = None):
        self.db, self.svc, self.jf, self.bot_token = db, svc, jf, bot_token
        self.radio = radio_server  # radio.RadioServer: «сейчас в эфире» для встроенного плеера
        self.logs: dict[str, collections.deque] = {}
        self.jobs: dict[str, list[dict]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._wave_busy: set[str] = set()  # логины, у которых сейчас подбирается волна
        self.bot_username: str | None = None  # getMe при старте бота: ссылки-приглашения t.me/<бот>
        self._start_limit = auth.RateLimiter(10, 600)   # /api/auth/start и виджет: 10 за 10 минут на IP (/64)
        self._start_global = auth.RateLimiter(300, 600)  # и на весь сервер
        self._poll_limit = auth.RateLimiter(400, 600)   # опрос раз в 2 с — с запасом
        self._widget_replay = auth.ReplayGuard(auth.WIDGET_MAX_AGE + 60)
        self._trusted = auth.parse_networks(CFG.trusted_proxies)
        self._upload_global = asyncio.Semaphore(UPLOAD_GLOBAL_CONCURRENCY)
        self._upload_users: dict[str, int] = {}
        self._redownloading: set[int] = set()              # треки, которые сейчас перекачиваются
        self._redownload_log: dict[str, list[float]] = {}  # логин -> когда просил перекачать (лимит в час)
        self._lyrics_busy: set[int] = set()                # треки, текст которых сейчас ищется заново
        self._lyrics_log: dict[str, list[float]] = {}      # логин -> когда просил найти текст заново

    # ---------------------------------------------------------------- инфраструктура
    def log(self, login: str, text: str) -> None:
        dq = self.logs.setdefault(login, collections.deque(maxlen=ACTIVITY_LEN))
        dq.append({"ts": int(time.time()), "text": text})

    def _notifier(self, login: str) -> Callable[[str], Awaitable[None]]:
        async def notify(text: str) -> None:
            self.log(login, text)
        return notify

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _admission(self, user: User, n: int) -> web.Response | None:
        """Квота очереди и свободное место — до постановки загрузок (иначе API ответило бы «в очереди» впустую)."""
        fn = getattr(self.svc, "admission", None)
        refused = fn(user, n) if fn is not None else None
        if not refused:
            return None
        return _err(507 if refused[0] == "disk_full" else 429, refused[0], message=refused[1])

    def start_job(self, user: User, reqs: list[TrackRequest], title: str, *, origin: str = "webapp",
                  after: Callable[[], None] | None = None, resolved: Any = None,
                  playlist: tuple[int, str, float] | None = None) -> Job:
        reqs = [dataclasses.replace(r) for r in reqs]  # копии: воркер дополняет длительность и т.п.
        job = Job(user=user, text=title, notify=self._notifier(user.login), tracks=reqs, title=title, origin=origin)
        if playlist is not None:  # готовые треки — в этот плейлист по порядку (позиция = база + индекс)
            job.playlist_id, job.playlist_name, job.playlist_base = playlist
        link_playlist = getattr(self.svc, "link_playlist", None)
        if resolved is not None and link_playlist is not None:
            link_playlist(job, resolved)  # ссылка на плейлист — плейлист бота в порядке источника
        entry = {"title": title, "total": len(reqs), "started": int(time.time())}
        self.jobs.setdefault(user.login, []).append(entry)
        if len(reqs) > 1:
            self.log(user.login, f"📥 В очереди: {title} — {len(reqs)} треков")
        else:
            self.log(user.login, f"⏳ {reqs[0].label}: ищу в источниках…")

        async def run() -> None:
            try:
                await self.svc.run_job(job, announce=False)
                if after:
                    after()
            except Exception as e:  # noqa: BLE001
                log.exception("webapp job failed")
                self.log(user.login, f"❌ {title}: ошибка {e}")
            finally:
                lst = self.jobs.get(user.login, [])
                if entry in lst:
                    lst.remove(entry)

        self._spawn(run())
        return job

    def auth(self, request: web.Request) -> tuple[User | None, int, web.Response | None]:
        """Telegram initData (Mini App) или Authorization: Bearer <токен устройства> (PWA, приложения)."""
        bearer = request.headers.get("Authorization", "")
        if not request.headers.get(INIT_HEADER) and bearer[:7].lower() == "bearer ":
            return self._auth_bearer(request, bearer[7:].strip())
        data = validate_init_data(request.headers.get(INIT_HEADER, ""), self.bot_token)
        if not data or not isinstance(data.get("user"), dict) or "id" not in data["user"]:
            return None, 0, _err(401, "unauthorized")
        try:
            tg_id = int(data["user"]["id"])
        except (TypeError, ValueError):
            return None, 0, _err(401, "unauthorized")
        user = self.db.user_by_tg(tg_id)
        if not user:
            return None, tg_id, _err(403, "not_registered")
        return user, tg_id, None

    def _auth_bearer(self, request: web.Request, token: str) -> tuple[User | None, int, web.Response | None]:
        if not auth.TOKEN_RE.match(token):
            return None, 0, _err(401, "unauthorized")
        dev = self.db.device_by_token_hash(auth.sha256(token))  # в базе только хэш; поиск по хэшу
        user = self.db.user_by_login(dev["login"]) if dev else None
        if not dev or not user:  # отозван (выход, /devices) или не существовал
            return None, 0, _err(401, "revoked", message="Вход на этом устройстве отменён — войдите заново.")
        now = int(time.time())
        if now - dev["last_seen"] > auth.DEVICE_IDLE_TTL:  # 90 дней без входа — токен больше не действует
            self.db.revoke_device(user.login, dev["id"])
            return None, 0, _err(401, "token_expired", message="Вход устарел — войдите заново.")
        if now - dev["last_seen"] >= auth.LAST_SEEN_STEP:
            self.db.touch_device(dev["id"], now)
        request[_DEVICE_KEY] = dev["id"]
        # результаты поиска привязаны к владельцу по tg_id; без Telegram — устойчивый псевдо-id от логина
        return user, user.tg_id or -(int(auth.sha256("login:" + user.login)[:12], 16) + 1), None

    @staticmethod
    def source(request: web.Request) -> str:
        """Откуда действие: 'app' — вход по токену устройства (приложение, браузер): в Telegram ничего не пишем,
        всё видно через API; 'tg' — Mini App в Telegram (initData): сообщения в чат с ботом, как раньше."""
        return "app" if request.get(_DEVICE_KEY) is not None else "tg"

    @staticmethod
    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "bad_json"}), content_type="application/json")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "bad_json"}), content_type="application/json")
        return data

    def api(self, handler: Handler) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        @functools.wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            user, tg_id, error = self.auth(request)
            if error is not None:
                return error
            try:
                resp = await handler(request, user, tg_id)  # type: ignore[arg-type]
            except web.HTTPException:
                raise
            except Exception:  # noqa: BLE001
                log.exception("webapp API %s failed", request.path)
                return _err(500, "internal")
            resp.headers.setdefault("Cache-Control", "no-store")
            return resp
        return wrapped

    def _result(self, token: str, tg_id: int) -> tuple[search.Result | None, web.Response | None]:
        res = search.get(str(token or ""))
        if not res:
            return None, _err(404, "expired", message="Результаты поиска устарели — повторите поиск.")
        if res.owner != tg_id:
            return None, _err(403, "forbidden")
        return res, None

    # ---------------------------------------------------------------- статика
    async def index(self, _: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def service_worker(self, _: web.Request) -> web.StreamResponse:
        """SW из /app/: scope не может быть шире пути файла (static/sw.js покрыл бы только /app/static/)."""
        path = STATIC_DIR / "sw.js"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "text/javascript; charset=utf-8",
                                               "Cache-Control": "no-cache", "Service-Worker-Allowed": "/app/"})

    async def manifest(self, _: web.Request) -> web.StreamResponse:
        path = STATIC_DIR / "manifest.webmanifest"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "application/manifest+json; charset=utf-8",
                                               "Cache-Control": "no-cache"})

    async def redirect(self, _: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/app/")

    # ---------------------------------------------------------------- API
    async def me(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        jf_url = CFG.jellyfin_public_base
        return web.json_response({
            "login": user.login, "tracks": self.db.user_track_count(user.login), "stream_url": radio.wave_url(user),
            "jellyfin_url": jf_url, "is_admin": bool(user.is_admin or tg_id in CFG.admin_telegram_ids),
        })

    async def do_search(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        q = request.query.get("q", "").strip()[:MAX_QUERY]
        if not q:
            return _err(400, "empty_query")
        res = await search.catalog_search(q, tg_id)
        token = search.store(res)
        return web.json_response({
            "token": token, "artist": res.artist, "artist_id": res.artist_id or None,
            "picture": res.picture or None, "fans": res.fans,
            "tracks": [_track_json(i, t) for i, t in enumerate(res.tracks)],
            "albums": [_album_json(i, a) for i, a in enumerate(res.albums)],
            "artists": [_artist_json(a) for a in res.artists],
        })

    # ---------------------------------------------------------------- каталог (Deezer)
    @staticmethod
    def _catalog_id(request: web.Request) -> str | None:
        return search.valid_id(request.query.get("id", "")[:32])

    async def _catalog(self, fn: Callable[[], Awaitable[Any]]) -> tuple[Any, web.Response | None]:
        try:
            return await fn(), None
        except LookupError:
            return None, _err(404, "not_found", message="Не нашлось в каталоге.")
        except Exception as e:  # noqa: BLE001
            log.warning("catalog request failed: %s", e)
            return None, _err(502, "catalog_failed", message="Каталог недоступен, попробуйте позже.")

    async def catalog_album(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        aid = self._catalog_id(request)
        if not aid:
            return _err(400, "bad_id")
        res, error = await self._catalog(lambda: search.catalog_album(aid, tg_id))
        if error:
            return error
        al = res.albums[0]
        return web.json_response({
            "token": search.store(res), "id": al.id, "title": al.title, "artist": al.artist,
            "artist_id": al.artist_id or None, "year": al.year, "kind": al.kind,
            "cover": al.cover_xl or al.cover or None,
            "tracks": [_ctrack_json(i, t) for i, t in enumerate(res.tracks)],
        })

    async def catalog_artist(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        aid = self._catalog_id(request)
        if not aid:
            return _err(400, "bad_id")
        res, error = await self._catalog(lambda: search.catalog_artist(aid, tg_id))
        if error:
            return error
        return web.json_response({
            "token": search.store(res), "id": res.artist_id, "name": res.artist,
            "picture": res.picture or None, "fans": res.fans,
            "tracks": [_track_json(i, t) for i, t in enumerate(res.tracks)],
            "albums": [_album_json(i, a) for i, a in enumerate(res.albums)],
        })

    async def catalog_artist_tracks(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        aid = self._catalog_id(request)
        if not aid:
            return _err(400, "bad_id")
        data, error = await self._catalog(lambda: search.artist_all_tracks(aid))
        if error:
            return error
        artist, tracks, meta = data
        res = search.Result(query=artist.name, owner=tg_id, artist=artist.name, artist_id=artist.id,
                            title=f"{artist.name} — все треки", tracks=list(tracks))
        return web.json_response({
            "token": search.store(res), "id": artist.id, "name": artist.name,
            "albums": meta.get("albums", 0), "capped": bool(meta.get("capped")),
            "tracks": [_ctrack_json(i, t) for i, t in enumerate(res.tracks)],
        })

    async def album(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        res, error = self._result(request.query.get("token", ""), tg_id)
        if error:
            return error
        try:
            i = int(request.query.get("i", ""))
            if i < 0:
                raise IndexError
            al = res.albums[i]  # type: ignore[union-attr]
        except (ValueError, IndexError):
            return _err(400, "bad_index")
        tracks = await search.album_tracks(res, i)  # type: ignore[arg-type]
        return web.json_response({"i": i, "title": al.title, "artist": al.artist, "year": al.year,
                                  "tracks": [_track_json(j, t) for j, t in enumerate(tracks)]})

    async def download(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Скачать трек(и) из результатов поиска/каталога; playlist_id — и добавить в этот плейлист по порядку."""
        data = await self.body(request)
        target = None
        if data.get("playlist_id") is not None:
            target = self._target_playlist(user, data.get("playlist_id"))
            if target is None:
                return _err(404, "playlist_not_found", message="Плейлист не найден.")
        parsed = await self._download_reqs(data, tg_id)
        if isinstance(parsed, web.Response):
            return parsed
        reqs, title = parsed
        refused = self._admission(user, len(reqs))
        if refused:
            return refused
        out: dict[str, Any] = {"queued": len(reqs), "title": title}
        if target is not None:
            out["playlist"] = await self._queue_into_playlist(user, reqs, title, target)
        else:
            self.start_job(user, reqs, title)
        return web.json_response(out)

    def _target_playlist(self, user: User, pid: Any) -> Any:
        pid = _valid_id(pid)
        return self.db.get_playlist(pid) if pid and self.db.playlist_role(pid, user.login) else None

    async def _queue_into_playlist(self, user: User, reqs: list[TrackRequest], title: str, pl: Any) -> dict:
        """Треки, которые уже есть в медиатеке, — в плейлист сразу; остальные — по мере загрузки, в том же порядке."""
        def now() -> tuple[float, int]:
            base = self.db.playlist_end_position(pl["id"])
            added = 0
            for idx, r in enumerate(reqs[:PL_MAX_TRACKS]):
                if not (r.artist and r.title):
                    continue
                t = library.lookup_cached(self.db, r.artist, r.title, isrc=r.isrc)
                if t and self.db.user_has_track(user.login, t.id) \
                        and self.db.put_playlist_track(pl["id"], t.id, base + idx, user.login, PL_MAX_TRACKS):
                    added += 1
            return base, added
        base, added = await asyncio.to_thread(now)
        if added:
            self._pl_changed(pl["id"])
        self.start_job(user, reqs, title, playlist=(pl["id"], pl["name"], base))
        return {"id": pl["id"], "name": pl["name"], "added_now": added}

    async def _download_reqs(self, data: dict, tg_id: int) -> tuple[list[TrackRequest], str] | web.Response:
        """token + kind (track / all / album / tracks / album_track) + i, j, idx -> (запросы, заголовок)."""
        res, error = self._result(data.get("token", ""), tg_id)
        if error:
            return error
        assert res is not None
        kind = data.get("kind")
        try:
            i = int(data.get("i", 0))
            j = int(data.get("j", 0))
            if i < 0 or j < 0:
                raise IndexError
            if kind == "track":
                reqs = [res.tracks[i]]
                title = reqs[0].label
            elif kind == "all":
                reqs = list(res.tracks)
                title = res.title or res.artist or res.query
            elif kind == "album":
                al = res.albums[i]
                reqs = list(await search.album_tracks(res, i))
                title = f"{al.artist} — {al.title}"
            elif kind == "tracks":  # выбранные треки результата (например, «только недостающие»)
                idx = data.get("idx")
                if not isinstance(idx, list) or not idx or len(idx) > MAX_PICK:
                    raise ValueError
                picked = sorted({int(x) for x in idx})
                if picked[0] < 0:
                    raise IndexError
                reqs = [res.tracks[k] for k in picked]
                base = res.title or res.artist or res.query
                title = reqs[0].label if len(reqs) == 1 else f"{base} — {len(reqs)} тр."
            elif kind == "album_track":
                al = res.albums[i]
                reqs = [(await search.album_tracks(res, i))[j]]
                title = reqs[0].label
            else:
                return _err(400, "bad_kind")
        except (TypeError, ValueError, IndexError):
            return _err(400, "bad_index")
        if not reqs:
            return _err(404, "nothing_to_download")
        return reqs, title

    async def download_link(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Ссылка Spotify / YouTube / SoundCloud / Bandcamp / Яндекс… — как если бы её прислали боту."""
        url = (await self.body(request)).get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > 2000 or sources.classify(url) == "text":
            return _err(400, "bad_url", message="Нужна ссылка на трек, альбом или плейлист.")
        try:
            resolved = await sources.resolve(url.strip())
        except Exception as e:  # noqa: BLE001
            log.warning("link resolve failed: %s", e)
            return _err(502, "resolve_failed", message="Не удалось открыть ссылку, попробуйте позже.")
        if resolved.error:
            return _err(422, "resolve_error", message=resolved.error)
        if not resolved.tracks:
            return _err(404, "nothing_to_download", message="Ничего не нашёл по этой ссылке.")
        refused = self._admission(user, len(resolved.tracks))
        if refused:
            return refused
        title = resolved.title or resolved.tracks[0].label
        job = self.start_job(user, resolved.tracks, title, resolved=resolved)
        if resolved.note:
            self.log(user.login, resolved.note)
        pl = {"id": job.playlist_id, "name": job.playlist_name} if isinstance(job.playlist_id, int) else None
        return web.json_response({"queued": len(resolved.tracks), "title": title, "kind": resolved.kind,
                                  "note": resolved.note or None, "playlist": pl})

    async def activity(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        prefix = f"{user.login}:"
        active = [v[len(prefix):].strip() for v in list(self.svc.active.values()) if v.startswith(prefix)]
        items = []
        for info in list((getattr(self.svc, "active_info", None) or {}).values()):
            req = info.get("req")
            if info.get("login") != user.login or req is None:
                continue
            items.append({"text": req.label, "artist": req.artist or None, "title": req.title or req.query or None,
                          "track_id": req.redownload or None, "progress": info.get("progress") or None})
        return web.json_response({
            "active": active,
            "active_items": items,
            "jobs": list(self.jobs.get(user.login, [])),
            "log": list(reversed(self.logs.get(user.login, ()))),
            "queue": self.svc.queue.qsize(),
        })

    @staticmethod
    def _view_params(qp: Any, sorts: tuple[str, ...], default: str) -> tuple[dict, web.Response | None]:
        """sort / order / genre / q / seed из строки запроса (общие для медиатеки, «Слушать», исполнителей)."""
        sort = qp.get("sort", default)
        order = qp.get("order", "")
        if sort not in sorts:
            return {}, _err(400, "bad_sort", message="sort: " + ", ".join(sorts))
        if order not in ("", "asc", "desc"):
            return {}, _err(400, "bad_order", message="order: asc или desc")
        try:
            seed = int(qp["seed"]) if qp.get("seed") else None
        except ValueError:
            seed = None
        return {"sort": sort, "desc": None if not order else order == "desc", "seed": seed,
                "genre": qp.get("genre", "").strip()[:100], "q": qp.get("q", "").strip()[:MAX_QUERY]}, None

    def _view_rows(self, user: User, v: dict) -> tuple[list, int]:
        """Строки медиатеки после фильтра и сортировки; второе — размер всей медиатеки. Вызывать в потоке."""
        rows = self.db.user_library_view(user.login)
        total = len(rows)
        rows = libview.filter_rows(rows, q=v["q"], genre=v["genre"])
        return libview.sort_rows(rows, v["sort"], v["desc"], v["seed"]), total

    async def library(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Медиатека: q (поиск), genre (фильтр), sort, order, offset/limit (≤ 500). total — после фильтра."""
        v, error = self._view_params(request.query, libview.TRACK_SORTS, "added")
        if error:
            return error
        try:
            limit = max(1, min(500, int(request.query.get("limit", "50"))))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            limit, offset = 50, 0

        def build() -> dict:
            rows, lib_total = self._view_rows(user, v)
            return {"total": len(rows), "library_total": lib_total, "offset": offset, "limit": limit,
                    "sort": v["sort"], "items": self._lib_items_sync(user, rows[offset:offset + limit])}
        return web.json_response(await asyncio.to_thread(build))

    async def library_genres(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        rows = await asyncio.to_thread(self.db.user_library_view, user.login)
        return web.json_response({"items": libview.genre_counts(rows)})

    async def _lib_items(self, user: User, rows: list[tuple[Any, str | None]]) -> list[dict]:
        # is_file() по каждой строке — блокирующий I/O, уводим из event loop
        return await asyncio.to_thread(self._lib_items_sync, user, rows)

    def _lib_items_sync(self, user: User, rows: list) -> list[dict]:
        """rows: (трек, ссылка) или (трек, ссылка, когда добавлен)."""
        find = _cover_finder()
        return [self._lib_item(user, r[0], bool(find(r[0].path, r[1])), owned=r[1] is not None,
                               added_at=r[2] if len(r) > 2 else None) for r in rows]

    def _lib_item(self, user: User, t: Any, has_cover: bool, owned: bool = True, added_at: int | None = None) -> dict:
        return {"id": t.id, "artist": t.artist, "title": t.title, "album": t.album or "",
                "duration": search.fmt_duration(t.duration or 0), "seconds": int(t.duration or 0),
                "cover": cover_url(self.bot_token, user.login, t.id) if has_cover else None,
                "stream": stream_url(self.bot_token, user.login, t.id), "owned": owned,
                # ReplayGain (цель −14 LUFS): None — громкость ещё не посчитана
                "gain": round(t.gain_db, 2) if getattr(t, "gain_db", None) is not None else None,
                "peak": round(t.peak, 4) if getattr(t, "peak", None) is not None else None,
                "genre": getattr(t, "genre", None), "year": getattr(t, "year", None), "added_at": added_at,
                "broken": bool(getattr(t, "broken", False)),  # файл помечен проблемным, идёт/не удалась перекачка
                **_track_keys_size(t.path)}

    @staticmethod
    def _folder_param(request: web.Request, name: str) -> str | None:
        v = request.query.get(name, "")
        return v if v and len(v) <= MAX_FOLDER and "\x00" not in v else None

    def _first_cover(self, user: User, rows: list[tuple[Any, str]]) -> str | None:
        """Ссылка на обложку альбома — по первому треку, у которого есть cover.jpg (вызывать в потоке)."""
        for t, link in rows[:5]:
            if _cover_path(t.path, link):
                return cover_url(self.bot_token, user.login, t.id)
        return None

    def _groups(self, request: web.Request, user: User, by: str) -> tuple[list[dict] | None, web.Response | None]:
        """Исполнители/альбомы медиатеки с фильтром genre и sort=name|count|added|year. Вызывать в потоке."""
        v, error = self._view_params(request.query, libview.GROUP_SORTS, "name")
        if error:
            return None, error
        rows = libview.filter_rows(self.db.user_library_view(user.login), genre=v["genre"])
        return libview.sort_groups(libview.group_rows(rows, by), v["sort"], v["desc"], by), None

    async def library_artists(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        def build() -> tuple[list[dict] | None, web.Response | None]:
            groups, error = self._groups(request, user, "artist")
            if error:
                return None, error
            out = []
            for g in groups:  # type: ignore[union-attr]
                img = _artist_image_path([(t, link) for t, link, _ in g["rows"]])
                out.append({"key": g["artist_key"], "name": g["artist"], "count": g["count"], "year": g["year"],
                            "added_at": g["added_at"],
                            "image": artist_image_url(self.bot_token, user.login, g["artist_key"]) if img else None})
            return out, None
        items, error = await asyncio.to_thread(build)
        return error or web.json_response({"items": items})

    async def library_albums(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        def build() -> tuple[list[dict] | None, web.Response | None]:
            groups, error = self._groups(request, user, "album")
            if error:
                return None, error
            return [{"artist_key": g["artist_key"], "key": g["key"], "title": g["title"], "artist": g["artist"],
                     "count": g["count"], "year": g["year"], "added_at": g["added_at"],
                     "cover": self._first_cover(user, [(t, link) for t, link, _ in g["rows"]])}
                    for g in groups], None  # type: ignore[union-attr]
        items, error = await asyncio.to_thread(build)
        return error or web.json_response({"items": items})

    def _album_groups(self, user: User, rows: list[tuple[Any, str]]) -> list[dict]:
        """Треки, сгруппированные по папке альбома (вызывать в потоке: проверяет файлы обложек)."""
        groups: dict[str, list[tuple[Any, str]]] = {}
        for t, link in rows:
            groups.setdefault(self.db.library_folders(link)[1], []).append((t, link))
        out = []
        find = _cover_finder()
        for key, items in groups.items():
            covers = [bool(find(t.path, link)) for t, link in items]
            title = self.db.display_name(key, [t.album or "" for t, _ in items])
            cover = next((cover_url(self.bot_token, user.login, t.id)
                          for (t, _), c in zip(items, covers) if c), None)
            out.append({"key": key, "title": title, "cover": cover,
                        "tracks": [self._lib_item(user, t, c) for (t, _), c in zip(items, covers)]})
        return sorted(out, key=lambda g: g["title"].casefold())

    async def library_artist(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        key = self._folder_param(request, "a")
        if key is None:
            return _err(400, "bad_artist")

        def build() -> dict | None:
            rows = self.db.user_tracks_by_artist(user.login, key)
            if not rows:
                return None
            img = _artist_image_path(rows)
            return {"key": key, "name": self.db.display_name(key, [t.artist for t, _ in rows]), "count": len(rows),
                    "image": artist_image_url(self.bot_token, user.login, key) if img else None,
                    "albums": self._album_groups(user, rows)}
        data = await asyncio.to_thread(build)
        if not data:
            return _err(404, "not_found", message="В медиатеке нет треков этого исполнителя.")
        return web.json_response(data)

    async def library_album(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        a, b = self._folder_param(request, "a"), self._folder_param(request, "b")
        if a is None or b is None:
            return _err(400, "bad_album")

        def build() -> dict | None:
            rows = [(t, link) for t, link in self.db.user_tracks_by_artist(user.login, a)
                    if self.db.library_folders(link)[1] == b]
            if not rows:
                return None
            group = self._album_groups(user, rows)[0]
            return {**group, "artist_key": a, "artist": self.db.display_name(a, [t.artist for t, _ in rows])}
        data = await asyncio.to_thread(build)
        if not data:
            return _err(404, "not_found", message="Альбом не найден в медиатеке.")
        return web.json_response(data)

    async def artist_image(self, request: web.Request) -> web.StreamResponse:
        login, folder, sig = request.query.get("u", ""), request.query.get("a", ""), request.query.get("s", "")
        if not login or not folder or not sig or len(folder) > MAX_FOLDER or not self.bot_token \
                or not hmac.compare_digest(artist_signature(self.bot_token, login, folder).encode(),
                                           sig.encode("utf-8", "replace")):
            raise web.HTTPNotFound()
        user = self.db.user_by_login(login)
        if not user or user.login != login:
            raise web.HTTPNotFound()
        # владение: у пользователя есть треки в этой папке исполнителя; путь берём только из БД
        path = await asyncio.to_thread(lambda: _artist_image_path(self.db.user_tracks_by_artist(login, folder)))
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "image/jpeg", "Cache-Control": "max-age=86400"})

    async def delete(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        data = await self.body(request)
        try:
            track_id = int(data.get("track_id"))
        except (TypeError, ValueError):
            return _err(400, "bad_track_id")
        if not self.db.user_has_track(user.login, track_id):
            return _err(404, "not_found", message="Трек не найден в вашей медиатеке.")
        text = await self.svc.delete_track(user, track_id)
        self.log(user.login, text)
        # gone — трек больше недоступен совсем (ни у кого в медиатеке нет — файл удалён, из плейлистов он ушёл):
        # клиенту убрать его из очереди; иначе он остаётся в плейлистах с owned=false
        gone = await asyncio.to_thread(self.db.track_access, user.login, track_id) is None
        return web.json_response({"ok": True, "text": text, "track_id": track_id, "gone": gone})

    async def library_delete(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Удалить из медиатеки несколько треков (до 500). Трек, которого больше нет ни в одной медиатеке, удаляется
        с диска и уходит из плейлистов; есть у кого-то ещё — остаётся в плейлистах."""
        ids = self._int_list((await self.body(request)).get("track_ids"), MAX_TRACK_IDS)
        if not ids:
            return _err(400, "bad_track_ids", message=f"Нужен список track_ids (до {MAX_TRACK_IDS}).")
        uniq = list(dict.fromkeys(ids))
        owned = await asyncio.to_thread(lambda: [i for i in uniq if self.db.user_has_track(user.login, i)])
        for tid in owned:
            await self.svc.delete_track(user, tid)
        if owned:
            self.log(user.login, f"🗑 Удалено из медиатеки: {len(owned)}")
        # deleted_ids — убрать из списков медиатеки; gone_ids — недоступны совсем (нет и в плейлистах): из очереди
        left = await asyncio.to_thread(self.db.accessible_tracks, user.login, owned)
        return web.json_response({"deleted": len(owned), "skipped": len(uniq) - len(owned),
                                  "deleted_ids": owned, "gone_ids": [i for i in owned if i not in left]})

    async def cover(self, request: web.Request) -> web.StreamResponse:
        login, sig = request.query.get("u", ""), request.query.get("s", "")
        track_id = _valid_id(request.match_info["track_id"])
        try:
            exp = int(request.query.get("e", ""))
        except ValueError:
            raise web.HTTPNotFound()
        if track_id is None or exp < time.time():
            raise web.HTTPNotFound()
        if not login or not sig or not self.bot_token or not hmac.compare_digest(
                cover_signature(self.bot_token, login, track_id, exp).encode(), sig.encode("utf-8", "replace")):
            raise web.HTTPNotFound()
        user = self.db.user_by_login(login)
        if not user or user.login != login:
            raise web.HTTPNotFound()
        def find() -> Path | None:  # своя медиатека или доступный плейлист
            hit = self.db.track_access(login, track_id)
            return _cover_path(hit[0].path, hit[1]) if hit else None
        path = await asyncio.to_thread(find)
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "image/jpeg", "Cache-Control": "max-age=86400"})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Файл трека для <audio> (Range/206 — FileResponse). Любая ошибка — 404 без подробностей."""
        q = request.query
        login, sig = q.get("u", ""), q.get("s", "")
        track_id = _valid_id(request.match_info["track_id"])
        try:
            exp = int(q.get("e", ""))
        except ValueError:
            raise web.HTTPNotFound()
        if track_id is None or not login or not sig or not self.bot_token or exp < time.time() \
                or not hmac.compare_digest(stream_signature(self.bot_token, login, track_id, exp).encode(),
                                           sig.encode("utf-8", "replace")):
            raise web.HTTPNotFound()
        user = self.db.user_by_login(login)
        if not user or user.login != login:
            raise web.HTTPNotFound()

        def pick() -> Path | None:  # ссылка пользователя, иначе файл общего кэша (трек из чужого плейлиста)
            hit = self.db.track_access(login, track_id)
            for p in (hit[1], hit[0].path) if hit else ():
                if p and Path(p).is_file():
                    return Path(p)
            return None
        path = await asyncio.to_thread(pick)
        if not path:
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "private, max-age=3600"}
        if path.suffix.lower() == ".mp3":
            headers["Content-Type"] = "audio/mpeg"  # иначе тип угадает FileResponse
        return web.FileResponse(path, headers=headers)

    # ---------------------------------------------------------------- очередь плеера
    async def track_redownload(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """POST /api/tracks/{id}/redownload — «Перекачать трек»: файл помечается проблемным (из кэша его больше
        не выдают), трек ищется и качается заново; новый файл после проверки заменяет старый у всех держателей.
        Не нашлось — старый файл остаётся. Только для трека из своей медиатеки; не чаще REDOWNLOAD_PER_HOUR в час."""
        tid = _valid_id(request.match_info["tid"])
        track = await asyncio.to_thread(self.db.get_track, tid) if tid else None
        if not track or not await asyncio.to_thread(self.db.user_has_track, user.login, track.id):
            return _err(404, "not_found", message="Трек не найден в вашей медиатеке.")
        if track.id in self._redownloading:
            return _err(409, "busy", message="Этот трек уже перекачивается.")
        now = time.time()
        recent = [x for x in self._redownload_log.get(user.login, []) if now - x < 3600]
        if len(recent) >= REDOWNLOAD_PER_HOUR:
            return _err(429, "rate_limited", message=f"Перекачивать можно не больше {REDOWNLOAD_PER_HOUR} треков "
                                                     "в час — попробуйте позже.")
        refused = self._admission(user, 1)
        if refused:
            return refused
        self._redownload_log[user.login] = recent + [now]
        await asyncio.to_thread(self.db.set_track_broken, track.id, True)
        req = TrackRequest(artist=track.artist, title=track.title, album=track.album or "", isrc=track.isrc or "",
                           redownload=track.id)
        title = f"Перекачать: {track.artist} — {track.title}"
        self._redownloading.add(track.id)
        self.start_job(user, [req], title, origin="redownload",
                       after=lambda: self._redownloading.discard(track.id))
        # если задание упало, after не вызовется — снимем блокировку по таймеру
        asyncio.get_running_loop().call_later(REDOWNLOAD_LOCK_TTL, self._redownloading.discard, track.id)
        text = f"🔁 Перекачиваю: {track.artist} — {track.title}. Старый файл останется, пока не найдётся подходящий."
        self.log(user.login, text)
        return web.json_response({"ok": True, "text": text, "job": {"title": title, "track_id": track.id}})

    async def tracks(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Свежие подписанные ссылки: доступные треки в порядке запроса, недоступные пропущены."""
        raw = [x for x in request.query.get("ids", "").split(",") if x.strip()]
        if len(raw) > MAX_TRACK_IDS:
            return _err(400, "too_many", message=f"Не больше {MAX_TRACK_IDS} треков за запрос.")
        ids = [_valid_id(x) for x in raw]
        if any(i is None for i in ids):
            return _err(400, "bad_ids")

        def build() -> list[dict]:
            hits = self.db.accessible_tracks(user.login, ids)  # type: ignore[arg-type]
            rows = [hits[i] for i in dict.fromkeys(ids) if i in hits]
            return self._lib_items_sync(user, rows)
        return web.json_response({"items": await asyncio.to_thread(build)})

    LEGACY_PLAY = {"shuffle": ("random", None), "artist": ("artist", False), "recent": ("added", True)}

    async def library_play(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Вся медиатека (или текущий вид: genre, q, sort, order) одной очередью — «Слушать» / «Перемешать».
        Старые значения order=shuffle|artist|recent по-прежнему понимаются."""
        legacy = self.LEGACY_PLAY.get(request.query.get("order", ""))
        if legacy and "sort" not in request.query:
            qp = {k: v for k, v in request.query.items() if k != "order"}
            v, error = self._view_params(qp, libview.TRACK_SORTS, legacy[0])
            if not error:
                v["desc"] = legacy[1]
        else:
            v, error = self._view_params(request.query, libview.TRACK_SORTS, "artist")
        if error:
            return error
        try:
            limit = max(1, min(MAX_PLAY, int(request.query.get("limit", "1000"))))
        except ValueError:
            limit = 1000

        def build() -> dict:
            rows, _ = self._view_rows(user, v)
            return {"total": len(rows), "sort": v["sort"], "items": self._lib_items_sync(user, rows[:limit])}
        return web.json_response(await asyncio.to_thread(build))

    # ---------------------------------------------------------------- плейлисты
    # Тела обработчиков — в потоке: SQLite и проверки файлов обложек не должны держать event loop.
    def _invite(self, token: str | None) -> dict | None:
        if not token:
            return None
        name = self.bot_username
        return {"token": token, "link": f"https://t.me/{name}?start=pl_{token}" if name else None,
                "app_link": f"{CFG.stream_public_base}/app/#join={token}"}

    def _pl_covers(self, sign_login: str, pid: int, find: Callable | None = None) -> list[str]:
        """До 4 разных обложек (разные cover.jpg) для мозаики. Вызывать в потоке."""
        find = find or _cover_finder()
        out: list[str] = []
        seen: set[Path] = set()
        for _, t, link in self.db.playlist_items(pid, sign_login, limit=PL_COVER_SCAN):
            p = find(t.path, link)
            if p and p not in seen:
                seen.add(p)
                out.append(cover_url(self.bot_token, sign_login, t.id))
                if len(out) >= 4:
                    break
        return out

    def _brief(self, user: User, pl: Any, role: str, find: Callable | None = None) -> dict:
        """Краткое описание плейлиста. Вызывать в потоке (обложки — файловый I/O)."""
        pid = pl["id"]
        count, seconds = self.db.playlist_stats(pid)
        members = self.db.playlist_members(pid)
        return {"id": pid, "name": pl["name"], "description": pl["description"], "count": count,
                "seconds": int(seconds), "covers": self._pl_covers(user.login, pid, find),
                # shared — совместный (есть ссылка или участники); invite_enabled — ссылка сейчас работает
                "shared": pl["share_token"] is not None or bool(members),
                "invite_enabled": pl["share_token"] is not None, "role": role, "owner": pl["owner"],
                "members": [{"login": m} for m in members], "updated_at": pl["updated_at"]}

    def _brief_sync(self, user: User, pid: int, *, invite: bool = False) -> dict:
        pl = self.db.get_playlist(pid)
        role = self.db.playlist_role(pid, user.login) or "member"
        out = self._brief(user, pl, role)
        if invite and role == "owner" and pl["share_token"]:
            out["invite"] = self._invite(pl["share_token"])
        return out

    async def _brief_now(self, user: User, pid: int, *, invite: bool = False) -> dict:
        return await asyncio.to_thread(functools.partial(self._brief_sync, user, pid, invite=invite))

    def _playlist(self, request: web.Request, user: User,
                  owner_only: bool = False) -> tuple[Any, str, web.Response | None]:
        """Плейлист из пути и роль пользователя. Чужой/несуществующий — 404 (существование не раскрываем)."""
        pid = _valid_id(request.match_info.get("pid", ""))
        role = self.db.playlist_role(pid, user.login) if pid else None
        if not role:
            return None, "", _err(404, "not_found", message="Плейлист не найден.")
        if owner_only and role != "owner":
            return None, role, _err(403, "forbidden", message="Это может только владелец плейлиста.")
        return self.db.get_playlist(pid), role, None

    def _pl_changed(self, pid: int, delay: float | None = None, *, urgent: bool = False) -> None:
        fn = getattr(self.svc, "playlist_changed", None)  # зеркало в Jellyfin (best effort)
        if fn is None:
            return
        try:
            kw: dict[str, Any] = {"urgent": True} if urgent else {}
            fn(pid, **kw) if delay is None else fn(pid, delay, **kw)
        except Exception:  # noqa: BLE001
            log.exception("playlist mirror scheduling failed")

    def _purge_sync(self, track_ids: list[int]) -> None:
        """Треки, выпавшие из плейлистов и медиатек, — из общего кэша (вызывать в потоке)."""
        for tid in track_ids:
            t = self.db.get_track(tid)
            if t:
                try:
                    library.purge_if_unused(self.db, t)
                except OSError as e:
                    log.warning("не удалось удалить файл трека %s: %s", tid, e)

    @staticmethod
    def _pl_text(data: dict) -> tuple[str | None, str | None, web.Response | None]:
        name = data.get("name")
        desc = data.get("description")
        if name is not None:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > PL_MAX_NAME:
                return None, None, _err(400, "bad_name", message=f"Название — от 1 до {PL_MAX_NAME} символов.")
            name = name.strip()
        if desc is not None:
            if not isinstance(desc, str) or len(desc.strip()) > PL_MAX_DESC:
                return None, None, _err(400, "bad_description",
                                        message=f"Описание — не длиннее {PL_MAX_DESC} символов.")
            desc = desc.strip()
        return name, desc, None

    @staticmethod
    def _int_list(v: Any, limit: int) -> list[int] | None:
        if not isinstance(v, list) or len(v) > limit:
            return None
        out = [_valid_id(x) for x in v]
        return None if any(i is None for i in out) else out  # type: ignore[return-value]

    def _accessible(self, user: User, ids: list[int]) -> list[int]:
        """Только доступные добавляющему треки (медиатека или его плейлисты), без повторов, в порядке запроса."""
        hits = self.db.accessible_tracks(user.login, ids)
        return [i for i in dict.fromkeys(ids) if i in hits]

    async def playlists(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        def build() -> list[dict]:
            find = _cover_finder()
            return [self._brief(user, pl, "owner" if pl["owner"] == user.login else "member", find)
                    for pl in self.db.user_playlists(user.login)]
        return web.json_response({"items": await asyncio.to_thread(build)})

    async def playlist_create(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        data = await self.body(request)
        if data.get("name") is None:
            return _err(400, "bad_name", message=f"Название — от 1 до {PL_MAX_NAME} символов.")
        name, desc, error = self._pl_text(data)
        if error:
            return error
        ids: list[int] = []
        if data.get("track_ids") is not None:
            ids = self._int_list(data.get("track_ids"), PL_MAX_TRACKS)  # type: ignore[assignment]
            if ids is None:
                return _err(400, "bad_track_ids")
        token = secrets.token_urlsafe(9) if data.get("shared") is True else None
        catalog = None
        if data.get("catalog") is not None:  # треки из поиска/каталога: плейлист наполняется по мере загрузки
            if not isinstance(data["catalog"], dict):
                return _err(400, "bad_catalog")
            catalog = await self._download_reqs(data["catalog"], tg_id)
            if isinstance(catalog, web.Response):
                return catalog
            refused = self._admission(user, len(catalog[0]))
            if refused:
                return refused

        def run() -> dict | None:
            try:
                pid = self.db.create_playlist(user.login, name or "", desc or "", token,
                                              track_ids=self._accessible(user, ids) if ids else (),
                                              max_per_user=PL_MAX_PER_USER, max_tracks=PL_MAX_TRACKS)
            except PlaylistLimit:
                return None
            return self._brief_sync(user, pid, invite=True)
        out = await asyncio.to_thread(run)
        if out is None:
            return _err(409, "limit", message=f"Можно создать не больше {PL_MAX_PER_USER} плейлистов.")
        self._pl_changed(out["id"])
        if catalog is not None:
            reqs, title = catalog
            q = await self._queue_into_playlist(user, reqs, title, self.db.get_playlist(out["id"]))
            out.update(await self._brief_now(user, out["id"], invite=True))
            out["queued"], out["added_now"] = len(reqs), q["added_now"]
        return web.json_response(out)

    async def playlist_get(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Плейлист с треками. sort: manual (порядок плейлиста, по умолчанию) или как у /api/library
        (added — когда добавлен в плейлист, title, artist, album, genre, year, duration, random); order: asc/desc."""
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        v, error = self._view_params(request.query, PLAYLIST_SORTS, "manual")
        if error:
            return error

        def build() -> dict:
            find = _cover_finder()
            out = self._brief(user, pl, role, find)
            items = self.db.playlist_items(pl["id"], user.login)
            if v["sort"] != "manual":
                by_item = {item["item_id"]: (item, t, link) for item, t, link in items}
                rows = [(t, link, item["item_added_at"], item["item_id"]) for item, t, link in items]
                items = [by_item[r[3]] for r in libview.sort_rows(rows, v["sort"], v["desc"], v["seed"])]
            out["sort"] = v["sort"]
            out["tracks"] = [{**self._lib_item(user, t, bool(find(t.path, link)), owned=link is not None),
                              "item_id": item["item_id"], "added_by": item["added_by"]}
                             for item, t, link in items]
            return out
        out = await asyncio.to_thread(build)
        if role == "owner" and pl["share_token"]:
            out["invite"] = self._invite(pl["share_token"])
        return web.json_response(out)

    async def playlist_update(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        name, desc, error = self._pl_text(await self.body(request))
        if error:
            return error
        await asyncio.to_thread(functools.partial(self.db.update_playlist, pl["id"], name=name, description=desc))
        if name is not None and name != pl["name"]:
            self._pl_changed(pl["id"])
        return web.json_response(await self._brief_now(user, pl["id"]))

    async def playlist_share(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user, owner_only=True)
        if error:
            return error
        data = await self.body(request)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return _err(400, "bad_enabled")
        token = pl["share_token"]
        if not enabled:
            token = None  # участники остаются, новые по старой ссылке не вступят
        elif not token or data.get("regenerate") is True:
            token = secrets.token_urlsafe(9)
        if token != pl["share_token"]:
            await asyncio.to_thread(self.db.set_share_token, pl["id"], token)
        return web.json_response({"shared": token is not None, "invite": self._invite(token)})

    async def playlist_add(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        data = await self.body(request)
        ids: list[int] | None = None
        album = artist = None
        if data.get("track_ids") is not None:
            ids = self._int_list(data.get("track_ids"), PL_MAX_TRACKS)
            if ids is None:
                return _err(400, "bad_track_ids")
        elif isinstance(data.get("album"), dict):
            album = (data["album"].get("a"), data["album"].get("b"))
            if not all(isinstance(x, str) and x and len(x) <= MAX_FOLDER for x in album):
                return _err(400, "bad_album")
        elif data.get("artist") is not None:
            artist = data.get("artist")
            if not isinstance(artist, str) or not artist or len(artist) > MAX_FOLDER:
                return _err(400, "bad_artist")
        else:
            return _err(400, "nothing_to_add")

        def run() -> dict:
            if album is not None:
                want = [t.id for t, link in self.db.user_tracks_by_artist(user.login, album[0])
                        if self.db.library_folders(link)[1] == album[1]]
            elif artist is not None:
                want = [t.id for t, _ in self.db.user_tracks_by_artist(user.login, artist)]
            else:
                want = ids or []
            ok = self._accessible(user, want)
            added, skipped = self.db.add_playlist_tracks(pl["id"], ok, user.login, PL_MAX_TRACKS)
            count, _ = self.db.playlist_stats(pl["id"])
            out: dict[str, Any] = {"added": added, "skipped": skipped + len(dict.fromkeys(want)) - len(ok),
                                   "count": count}
            if count >= PL_MAX_TRACKS:
                out["full"] = True
            return out
        out = await asyncio.to_thread(run)
        if out["added"]:
            self._pl_changed(pl["id"])
        return web.json_response(out)

    async def playlist_remove(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        ids = self._int_list((await self.body(request)).get("item_ids"), PL_MAX_TRACKS)
        if ids is None:
            return _err(400, "bad_item_ids")

        def run() -> dict:
            removed = self.db.remove_playlist_items(pl["id"], ids)
            self._purge_sync(removed)
            return {"removed": len(removed), "count": self.db.playlist_stats(pl["id"])[0]}
        out = await asyncio.to_thread(run)
        if out["removed"]:
            self._pl_changed(pl["id"])
        return web.json_response(out)

    async def playlist_move(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        data = await self.body(request)
        item_id, to = _valid_id(data.get("item_id")), data.get("to")
        if item_id is None or isinstance(to, bool) or not isinstance(to, (int, str)):
            return _err(400, "bad_move")
        try:
            to = max(-MAX_ID, min(MAX_ID, int(to)))
        except (TypeError, ValueError, OverflowError):
            return _err(400, "bad_move")
        if not await asyncio.to_thread(self.db.move_playlist_item, pl["id"], item_id, to):
            return _err(404, "not_found", message="Трека уже нет в плейлисте.")
        self._pl_changed(pl["id"])
        return web.json_response({"ok": True})

    async def playlist_move_many(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Выделение позиций — блоком на индекс to (в итоговом списке остальных), порядок внутри сохраняется."""
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        data = await self.body(request)
        ids, to = self._int_list(data.get("item_ids"), PL_MAX_TRACKS), data.get("to")
        if not ids or isinstance(to, bool) or not isinstance(to, (int, str)):
            return _err(400, "bad_move")
        try:
            to = max(-MAX_ID, min(MAX_ID, int(to)))
        except (TypeError, ValueError, OverflowError):
            return _err(400, "bad_move")
        moved = await asyncio.to_thread(self.db.move_playlist_items, pl["id"], ids, to)
        if not moved:
            return _err(404, "not_found", message="Этих треков уже нет в плейлисте.")
        self._pl_changed(pl["id"])
        return web.json_response({"ok": True, "moved": moved})

    async def playlist_delete(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user, owner_only=True)
        if error:
            return error

        def run() -> None:
            self._purge_sync(self.db.delete_playlist(pl["id"]))
        await asyncio.to_thread(run)
        self._pl_changed(pl["id"], 1, urgent=True)  # зеркала у всех участников удаляются
        return web.json_response({"ok": True})

    async def playlist_leave(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        if role == "owner":
            return _err(409, "owner_cannot_leave", message="Владелец не может выйти — плейлист можно удалить.")
        await asyncio.to_thread(self.db.remove_playlist_member, pl["id"], user.login)
        self._pl_changed(pl["id"], 1, urgent=True)
        return web.json_response({"ok": True})

    async def playlist_kick(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Убрать участника. Ссылка-приглашение заодно меняется: по старой исключённый не вернётся."""
        pl, role, error = self._playlist(request, user, owner_only=True)
        if error:
            return error
        login = (await self.body(request)).get("login")
        rotate = pl["share_token"] is not None
        new_token = secrets.token_urlsafe(9) if rotate else None
        if not isinstance(login, str) or not await asyncio.to_thread(functools.partial(
                self.db.remove_playlist_member, pl["id"], login, rotate=rotate, new_token=new_token)):
            return _err(404, "not_member", message="Такого участника нет.")
        self._pl_changed(pl["id"], 1, urgent=True)
        return web.json_response(await self._brief_now(user, pl["id"], invite=True))

    async def playlist_save(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Недостающие треки плейлиста — в медиатеку пользователя жёсткими ссылками из общего кэша."""
        pl, role, error = self._playlist(request, user)
        if error:
            return error
        res = {"linked": 0, "missing": 0, "failed": 0}

        def run() -> None:
            for _, t, link in self.db.playlist_items(pl["id"], user.login):
                if link is not None:
                    continue
                if not Path(t.path).is_file():
                    res["missing"] += 1
                    continue
                try:
                    library.give_cached(self.db, t, user.login)
                    res["linked"] += 1
                except (OSError, ValueError, LookupError, sqlite3.Error) as e:
                    res["failed"] += 1
                    log.warning("не удалось связать %s с медиатекой %s: %s", t.path, user.login, e)
        try:
            await asyncio.to_thread(run)
        finally:
            if res["linked"]:  # даже если упали на середине — уже связанное должно попасть в Jellyfin
                changed = getattr(self.svc, "library_changed", None)  # скан Jellyfin, как после загрузки
                if changed is not None:
                    changed(user)
                self._pl_changed(pl["id"], 90)  # после сканирования Jellyfin — новые треки попадут в зеркало
        if res["linked"]:
            self.log(user.login, f"💾 Из плейлиста «{pl['name']}» в медиатеку: {res['linked']}")
        return web.json_response(res)

    async def playlist_join_preview(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        pl = self.db.playlist_by_token(request.query.get("token", "")[:64])
        if not pl:
            return _err(404, "not_found", message="Приглашение недействительно или доступ выключен.")

        def build() -> dict:
            # обложки подписаны владельцем: у приглашённого ещё нет доступа к трекам
            return {"id": pl["id"], "name": pl["name"], "owner": pl["owner"],
                    "count": self.db.playlist_stats(pl["id"])[0], "covers": self._pl_covers(pl["owner"], pl["id"]),
                    "already": self.db.playlist_role(pl["id"], user.login) is not None}
        return web.json_response(await asyncio.to_thread(build))

    async def playlist_join(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        token = (await self.body(request)).get("token")
        pl = self.db.playlist_by_token(token[:64] if isinstance(token, str) else "")
        if not pl:
            return _err(404, "not_found", message="Приглашение недействительно или доступ выключен.")
        if not self.db.playlist_role(pl["id"], user.login):
            try:
                joined = await asyncio.to_thread(self.db.add_playlist_member, pl["id"], user.login, PL_MAX_MEMBERS)
            except PlaylistLimit:
                return _err(409, "members_limit", message=f"В плейлисте уже {PL_MAX_MEMBERS} участников.")
            if joined:
                self._pl_changed(pl["id"])
        return web.json_response(await self._brief_now(user, pl["id"], invite=True))

    async def playlists_for_track(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        tid = _valid_id(request.query.get("track_id", ""))
        if tid is None:
            return _err(400, "bad_track_id")
        rows = await asyncio.to_thread(self.db.playlists_for_track, user.login, tid)
        return web.json_response({"items": [{"id": p["id"], "name": p["name"], "has": has} for p, has in rows]})

    # ---------------------------------------------------------------- текст песни
    async def lyrics(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Синхронный/обычный текст: .lrc/.txt рядом с файлом или USLT-тег. Доступ — как у потока."""
        tid = _valid_id(request.match_info["track_id"])
        if tid is None:
            return _err(404, "not_found", message="Трек не найден.")

        def build() -> dict | None:
            hit = self.db.track_access(user.login, tid)
            return lyrics_mod.read_local(tid, [hit[1], hit[0].path]) if hit else None
        out = await asyncio.to_thread(build)
        if out is None:
            return _err(404, "not_found", message="Трек не найден.")
        return web.json_response(out)

    async def lyrics_refetch(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """POST /api/tracks/{id}/lyrics/refetch — найти текст заново (неверный/сбитый текст). Доступ — как у потока.
        Тело (необязательно): {"clear": true} — если нового текста нет, удалить текущий (только для трека из своей
        медиатеки: файлы текста общие у всех держателей трека). Отвечает новым текстом в формате GET /api/lyrics/{id}
        плюс status: updated | same | cleared | not_found."""
        tid = _valid_id(request.match_info["tid"])
        hit = await asyncio.to_thread(self.db.track_access, user.login, tid) if tid else None
        if not hit:
            return _err(404, "not_found", message="Трек не найден.")
        clear = False
        if request.can_read_body:
            clear = (await self.body(request)).get("clear") is True
        track, link = hit
        clear = clear and link is not None  # по доступу через чужой плейлист удалить текст у всех нельзя
        if track.id in self._lyrics_busy:
            return _err(409, "busy", message="Текст этого трека уже ищется.")
        now = time.time()
        recent = [x for x in self._lyrics_log.get(user.login, []) if now - x < 3600]
        if len(recent) >= LYRICS_REFETCH_PER_HOUR:
            return _err(429, "rate_limited", message=f"Искать текст заново можно не больше {LYRICS_REFETCH_PER_HOUR} "
                                                     "раз в час — попробуйте позже.")
        self._lyrics_log[user.login] = recent + [now]
        self._lyrics_busy.add(track.id)
        try:
            res = await lyrics_fix.refetch(self.db, track, clear=clear, timeout=LYRICS_REFETCH_TIMEOUT)
        finally:
            self._lyrics_busy.discard(track.id)
        if res["status"] == "error":
            return _err(504, "timeout", message="Сервисы текстов сейчас недоступны — попробуйте позже.")
        if res["status"] in ("updated", "cleared"):
            refresh = getattr(self.svc, "_schedule_refresh", None)
            if refresh:
                refresh(user, 5)  # Jellyfin перечитает .lrc при сканировании
        out = await asyncio.to_thread(lyrics_mod.read_local, track.id, [link, track.path])
        return web.json_response({**out, "status": res["status"]})

    # ---------------------------------------------------------------- настройки (по ключам)
    async def settings_get(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        items = {}
        for key, (value, ts) in (await asyncio.to_thread(self.db.settings, user.login)).items():
            try:
                items[key] = {"value": json.loads(value), "updated_at": ts}
            except ValueError:
                continue
        return web.json_response({"items": items})

    async def settings_put(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        key = request.match_info["key"]
        if not SETTING_KEY_RE.match(key):
            return _err(400, "bad_key", message="Ключ: до 80 символов a-z, 0-9, «.», «_», «-».")
        cap = SETTING_MAX_VALUE + 4096
        if request.content_length is not None and request.content_length > cap:
            return _err(413, "too_large", message="Значение больше 32 КБ.")
        try:
            raw = await request.read()  # без Content-Length aiohttp сам ограничит client_max_size
        except web.HTTPRequestEntityTooLarge:
            return _err(413, "too_large", message="Значение больше 32 КБ.")
        if len(raw) > cap:
            return _err(413, "too_large", message="Значение больше 32 КБ.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _err(400, "bad_json")
        if not isinstance(data, dict) or "value" not in data:
            return _err(400, "bad_value")
        base = data.get("base")
        if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
            return _err(400, "bad_base")
        value = json.dumps(data["value"], ensure_ascii=False, separators=(",", ":"))
        if len(value.encode("utf-8")) > SETTING_MAX_VALUE:
            return _err(413, "too_large", message="Значение больше 32 КБ.")
        status, cur = await asyncio.to_thread(self.db.put_setting, user.login, key, value, base, SETTING_MAX_KEYS)
        if status == "conflict":
            return _err(409, "conflict", message="Настройку уже изменили на другом устройстве.",
                        current={"value": json.loads(cur[0]), "updated_at": cur[1]} if cur else None)
        if status == "limit":
            return _err(409, "limit", message=f"Не больше {SETTING_MAX_KEYS} настроек.")
        return web.json_response({"key": key, "value": data["value"], "updated_at": cur[1]})  # type: ignore[index]

    async def settings_delete(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        key = request.match_info["key"]
        if not SETTING_KEY_RE.match(key):
            return _err(400, "bad_key")
        deleted = await asyncio.to_thread(self.db.delete_setting, user.login, key)
        return web.json_response({"ok": True, "deleted": deleted})

    # ---------------------------------------------------------------- вход вне Telegram и устройства
    @staticmethod
    def _plain(payload: dict, status: int = 200) -> web.Response:
        resp = web.json_response(payload, status=status)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _ip(self, request: web.Request) -> str:
        return auth.client_ip(request.remote, request.headers.get("X-Forwarded-For"), self._trusted)

    def _start_allowed(self, request: web.Request) -> bool:
        return self._start_limit.allow(auth.rate_key(self._ip(request))) and self._start_global.allow("*")

    async def _json_or_empty(self, request: web.Request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            raw = await request.content.read(8193)
            data = json.loads(raw.decode("utf-8")) if raw and len(raw) <= 8192 else {}
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _issue_token(self, login: str, name: str, platform: str) -> str:
        token, token_hash = auth.new_token()
        self.db.add_device(login, name, platform, token_hash, auth.MAX_DEVICES, idle_ttl=auth.DEVICE_IDLE_TTL)
        return token

    async def auth_config(self, _: web.Request) -> web.Response:
        return self._plain({"bot": self.bot_username,
                            "login_widget": bool(CFG.login_widget and self.bot_username)})

    async def auth_start(self, request: web.Request) -> web.Response:
        """Вход по ссылке в бота: nonce на 5 минут; «Разрешить» в Telegram — и опрос /api/auth/poll выдаст токен."""
        if not self._start_allowed(request):
            return _err(429, "rate_limited", message="Слишком много попыток входа, подождите несколько минут.")
        data = await self._json_or_empty(request)
        name, platform = auth.clean_device(data.get("device"), data.get("platform"))
        nonce, nonce_hash = auth.new_nonce()
        code = auth.new_code()  # показывается на экране устройства; в боте нужно выбрать именно его
        if not await asyncio.to_thread(functools.partial(
                self.db.create_nonce, nonce_hash, name, platform, auth.NONCE_TTL, auth.MAX_PENDING_NONCES,
                client_ip=self._ip(request), code=code)):
            return _err(503, "busy", message="Сервер занят, попробуйте позже.")
        bot = self.bot_username
        return self._plain({"nonce": nonce, "code": code,
                            "link": f"https://t.me/{bot}?start=login_{nonce}" if bot else None,
                            "expires_in": auth.NONCE_TTL})

    async def auth_poll(self, request: web.Request) -> web.Response:
        if not self._poll_limit.allow(self._ip(request)):
            return _err(429, "rate_limited")
        nonce = (await self._json_or_empty(request)).get("nonce")
        if not isinstance(nonce, str) or not auth.NONCE_RE.match(nonce):
            return _err(400, "bad_nonce")

        def run() -> dict:
            row = self.db.nonce_by_hash(auth.sha256(nonce))
            if row is None or row["expires_at"] < time.time() or row["status"] == "used":
                return {"status": "expired"}
            if row["status"] in ("pending", "denied"):
                return {"status": row["status"]}
            user = self.db.user_by_login(row["login"] or "")
            if not user:
                return {"status": "expired"}
            if self.db.device_count(user.login) >= auth.MAX_DEVICES:  # nonce не тратим: выйдите на старом
                return {"status": "too_many_devices", "message": TOO_MANY_DEVICES}
            if not self.db.use_nonce(row["id"]):  # токен — только первому опросу
                return {"status": "expired"}
            try:
                token = self._issue_token(user.login, row["device"], row["platform"])
            except DeviceLimit:
                return {"status": "too_many_devices", "message": TOO_MANY_DEVICES}
            return {"status": "ok", "token": token, "login": user.login}
        return self._plain(await asyncio.to_thread(run))

    async def auth_telegram(self, request: web.Request) -> web.Response:
        """Telegram Login Widget (нужен /setdomain у @BotFather и LOGIN_WIDGET=true)."""
        if not CFG.login_widget:
            return _err(404, "disabled", message="Вход через виджет Telegram выключен.")
        if not self._start_allowed(request):
            return _err(429, "rate_limited", message="Слишком много попыток входа, подождите несколько минут.")
        data = await self._json_or_empty(request)
        name, platform = auth.clean_device(data.pop("device", None), data.pop("platform", None))
        fields = auth.check_widget(data, self.bot_token)
        if fields is None:
            return _err(401, "unauthorized", message="Подпись Telegram не подошла или устарела.")
        if not self._widget_replay.first(f"{fields['id']}:{fields['auth_date']}:{data.get('hash')}"):
            return _err(401, "unauthorized", message="Эти данные входа уже использованы — войдите заново.")
        user = self.db.user_by_tg(int(fields["id"]))
        if not user:
            return _err(403, "not_registered", message="Сначала зарегистрируйтесь в боте.")
        try:
            token = await asyncio.to_thread(self._issue_token, user.login, name or "Вход через Telegram", platform)
        except DeviceLimit:
            return _err(409, "too_many_devices", message=TOO_MANY_DEVICES)
        return self._plain({"status": "ok", "token": token, "login": user.login})

    async def auth_logout(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """Выход на этом устройстве; {"all": true} — на всех устройствах (токены отзываются, и этот тоже)."""
        if (await self._json_or_empty(request)).get("all") is True:
            n = await asyncio.to_thread(self.db.revoke_all_devices, user.login)
            return web.json_response({"ok": True, "revoked": n})
        dev = request.get(_DEVICE_KEY)
        if dev is not None:
            await asyncio.to_thread(self.db.revoke_device, user.login, dev)
        return web.json_response({"ok": True, "revoked": dev is not None})

    async def devices(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        cur = request.get(_DEVICE_KEY)
        rows = await asyncio.to_thread(self.db.user_devices, user.login)
        return web.json_response({"items": [
            {"id": r["id"], "name": r["name"], "platform": r["platform"], "created_at": r["created_at"],
             "last_seen": r["last_seen"], "current": r["id"] == cur} for r in rows]})

    async def devices_revoke_all(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """«Выйти на всех устройствах» (в том числе на этом, если вошли по токену)."""
        n = await asyncio.to_thread(self.db.revoke_all_devices, user.login)
        return web.json_response({"ok": True, "revoked": n})

    async def device_revoke(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        did = _valid_id(request.match_info["did"])
        if did is None or not await asyncio.to_thread(self.db.revoke_device, user.login, did):
            return _err(404, "not_found", message="Устройство не найдено.")
        return web.json_response({"ok": True})

    # ---------------------------------------------------------------- загрузка своих файлов
    def _uploads(self) -> Any:
        return getattr(self.svc, "uploads", None)

    def _upload_json(self, user: User, r: Any, find: Callable) -> dict:
        track = None
        if r["track_id"] and r["state"] in ("done", "duplicate"):
            hit = self.db.track_access(user.login, r["track_id"])
            if hit:
                track = self._lib_item(user, hit[0], bool(find(hit[0].path, hit[1])), owned=hit[1] is not None)
        return {"id": r["id"], "name": r["orig_name"], "state": r["state"],
                "state_text": uploads_mod.STATE_TEXT.get(r["state"], r["state"]),
                "artist": r["guess_artist"] or "", "title": r["guess_title"] or "", "album": r["guess_album"] or "",
                "duration": round(r["duration"], 1) if r["duration"] else None, "size": r["size"],
                "note": r["note"] or None, "created_at": r["created_at"], "updated_at": r["updated_at"],
                "track": track}

    async def upload(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """multipart/form-data, поток на диск по частям; до 50 файлов за запрос, до UPLOAD_MAX_MB каждый.
        Одновременно: UPLOAD_USER_CONCURRENCY запросов на пользователя и UPLOAD_GLOBAL_CONCURRENCY на сервер."""
        up = self._uploads()
        if up is None:
            return _err(503, "unavailable", message="Загрузка файлов недоступна.")
        if self._upload_users.get(user.login, 0) >= UPLOAD_USER_CONCURRENCY or self._upload_global.locked():
            resp = _err(429, "busy", message="Уже идёт загрузка — дождитесь её окончания.")
            resp.force_close()
            return resp
        self._upload_users[user.login] = self._upload_users.get(user.login, 0) + 1
        try:
            async with self._upload_global:
                return await self._upload_files(request, user, up)
        finally:
            n = self._upload_users.get(user.login, 1) - 1
            if n > 0:
                self._upload_users[user.login] = n
            else:
                self._upload_users.pop(user.login, None)

    async def _upload_files(self, request: web.Request, user: User, up: Any) -> web.Response:
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError, KeyError):
            return _err(400, "bad_multipart", message="Ожидается multipart/form-data с файлами.")
        batch = f"app:{secrets.token_hex(8)}"
        limit = CFG.upload_max_mb * 1024 * 1024
        quota = CFG.upload_user_quota_gb * 1024 ** 3
        accepted: list[dict] = []
        rejected: list[dict] = []
        count = 0

        def stop(status: int, error: str, message: str, name: str) -> web.Response:
            """Дальше не читаем: ответ сразу, соединение закрывается (остаток тела не принимаем)."""
            rejected.append({"name": name, "error": error, "message": message})
            resp = web.json_response({"uploads": accepted, "rejected": rejected, "error": error, "message": message},
                                     status=status)
            resp.force_close()
            return resp

        while True:
            try:
                part = await asyncio.wait_for(reader.next(), UPLOAD_READ_TIMEOUT)
            except asyncio.TimeoutError:
                return stop(408, "timeout", "Загрузка прервалась: нет данных от устройства.", "")
            if part is None:
                break
            if not getattr(part, "filename", None):
                await part.release()
                continue
            count += 1
            raw_name = part.filename
            if re.search(r"%[0-9A-Fa-f]{2}", raw_name):  # некоторые клиенты (aiohttp, curl) кодируют имя в %XX
                raw_name = unquote(raw_name, errors="replace")
            name = uploads_mod.clean_name(raw_name)
            if Path(name).suffix.lower() in IMPORT_EXTS:  # список треков, а не аудио — это импорт
                rejected.append({"name": name, "error": "use_import", "message": USE_IMPORT_MSG})
                await part.release()
                continue
            if count > uploads_mod.MAX_FILES_PER_REQUEST:
                return stop(413, "too_many", f"За раз — не больше {uploads_mod.MAX_FILES_PER_REQUEST} файлов.", name)
            try:
                up.check_accept(user)
                # из приложения — без чата: «не опознан» и итоги видны в /api/uploads (форма need_info)
                uid, path = up.begin(user, name, origin="app", batch=batch,
                                     chat_id=user.tg_id if self.source(request) == "tg" else None)
            except uploads_mod.UploadError as e:
                if e.args[0] in ("disk_full", "too_many"):
                    return stop(507 if e.args[0] == "disk_full" else 429, e.args[0], e.args[1], name)
                rejected.append({"name": name, "error": e.args[0], "message": e.args[1]})
                await part.release()
                continue
            queued = await asyncio.to_thread(self.db.queued_upload_bytes, user.login)
            size, problem, next_check = 0, None, UPLOAD_SPACE_STEP
            try:
                f = await asyncio.to_thread(open, path, "wb")
                try:
                    while True:
                        chunk = await asyncio.wait_for(part.read_chunk(256 * 1024), UPLOAD_READ_TIMEOUT)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            problem = ("too_large", f"Файл больше {CFG.upload_max_mb} МБ.", 413)
                        elif queued + size > quota:
                            problem = ("quota", f"В очереди уже больше {CFG.upload_user_quota_gb} ГБ ваших файлов —"
                                                " дождитесь обработки.", 413)
                        elif size >= next_check:
                            next_check += UPLOAD_SPACE_STEP
                            if not await asyncio.to_thread(uploads_mod.free_space_ok):
                                problem = ("disk_full", "На сервере мало места — загрузка остановлена.", 507)
                        if problem:
                            break
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)
            except asyncio.TimeoutError:
                up.abort(uid, "загрузка оборвалась")
                return stop(408, "timeout", "Загрузка прервалась: нет данных от устройства.", name)
            except BaseException:
                up.abort(uid, "загрузка оборвалась")
                raise
            if problem:
                up.abort(uid, problem[1])
                return stop(problem[2], problem[0], problem[1], name)
            if size == 0:
                up.abort(uid, "Пустой файл.")
                rejected.append({"name": name, "error": "empty", "message": "Пустой файл."})
                continue
            up.received(uid, size)
            accepted.append({"id": uid, "name": name, "state": "queued"})
        if not accepted and not rejected:
            return _err(400, "no_files", message="Не выбрано ни одного файла.")
        if not accepted and rejected and all(r["error"] == "use_import" for r in rejected):
            return web.json_response({"uploads": [], "rejected": rejected, "error": "use_import",
                                      "message": USE_IMPORT_MSG}, status=415)
        if accepted:
            self.log(user.login, f"📤 Загружено файлов: {len(accepted)}")
        return web.json_response({"uploads": accepted, "rejected": rejected})

    async def uploads_list(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        def build() -> list[dict]:
            find = _cover_finder()
            return [self._upload_json(user, r, find) for r in self.db.user_uploads(user.login, 100)]
        return web.json_response({"items": await asyncio.to_thread(build)})

    async def _upload_action(self, request: web.Request, user: User, action: str) -> web.Response:
        up = self._uploads()
        uid = _valid_id(request.match_info["uid"])
        row = self.db.get_upload(uid) if uid and up is not None else None
        if not row or row["login"] != user.login:
            return _err(404, "not_found", message="Загрузка не найдена.")
        if action == "info":
            data = await self.body(request)
            artist, title, album = data.get("artist"), data.get("title"), data.get("album") or ""
            if not all(isinstance(x, str) and x.strip() for x in (artist, title)) or not isinstance(album, str) \
                    or len(artist) > 200 or len(title) > 300 or len(album) > 300:
                return _err(400, "bad_info", message="Нужны исполнитель и название.")
            ok = up.provide_info(user.login, uid, artist.strip(), title.strip(), album.strip())
        elif action == "skip":
            ok = up.skip(user.login, uid)
        else:
            ok = up.cancel(user.login, uid)
        if not ok:
            return _err(409, "bad_state", message="С этой загрузкой так уже нельзя.")
        find = _cover_finder()
        return web.json_response(await asyncio.to_thread(self._upload_json, user, self.db.get_upload(uid), find))

    async def upload_info(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        return await self._upload_action(request, user, "info")

    async def upload_skip(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        return await self._upload_action(request, user, "skip")

    async def upload_cancel(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        return await self._upload_action(request, user, "cancel")

    async def imports(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        out = []
        for imp in self.db.running_imports(user.login):
            iid = imp["id"]
            out.append({"id": iid, "filename": imp["filename"], "total": imp["total"], "state": imp["state"],
                        "counts": self.db.import_counts(iid), "text": self.svc.import_status_text(iid)})
        return web.json_response({"imports": out})

    async def import_file(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """POST /api/import/file (multipart, поле file): список TXT/CSV/TSV → импорт с плейлистом, как файл боту."""
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError, KeyError):
            return _err(400, "bad_multipart", message="Ожидается multipart/form-data с полем file.")
        data, name = None, ""
        while True:
            try:
                part = await asyncio.wait_for(reader.next(), UPLOAD_READ_TIMEOUT)
            except asyncio.TimeoutError:
                return _err(408, "timeout", message="Загрузка прервалась: нет данных от устройства.")
            if part is None:
                break
            if getattr(part, "name", None) != "file" or not getattr(part, "filename", None) or data is not None:
                await part.release()
                continue
            raw = part.filename
            if re.search(r"%[0-9A-Fa-f]{2}", raw):
                raw = unquote(raw, errors="replace")
            name = uploads_mod.clean_name(raw)
            if Path(name).suffix.lower() not in IMPORT_EXTS:
                resp = _err(415, "unsupported", message="Подходят списки .txt, .csv или .tsv.")
                resp.force_close()
                return resp
            buf = bytearray()
            while True:
                try:
                    chunk = await asyncio.wait_for(part.read_chunk(64 * 1024), UPLOAD_READ_TIMEOUT)
                except asyncio.TimeoutError:
                    return _err(408, "timeout", message="Загрузка прервалась: нет данных от устройства.")
                if not chunk:
                    break
                buf += chunk
                if len(buf) > IMPORT_MAX_BYTES:
                    resp = _err(413, "too_large", message=f"Файл больше {IMPORT_MAX_BYTES // 1024 ** 2} МБ — "
                                                          "разбейте его на части.")
                    resp.force_close()
                    return resp
            data = bytes(buf)
        if data is None:
            return _err(400, "no_file", message="Не выбран файл со списком треков.")
        if not data.strip():
            return _err(400, "empty", message="Файл пустой.")
        try:
            items, fmt = await importer.parse_file(data, name)
        except ValueError as e:
            return _err(400, "bad_format", message=f"Не понял формат файла: {e}. Подходят CSV из Tune My Music / "
                                                   "Exportify (колонки «Track name», «Artist name») или TXT со "
                                                   "строками «Артист - Песня».")
        except Exception:  # noqa: BLE001
            log.exception("import parse failed (app)")
            return _err(400, "bad_file", message="Не удалось прочитать файл.")
        if not items:
            return _err(400, "no_tracks", message="В файле не нашлось ни одного трека.")
        # как у бота: черновик (вставка позиций — в потоке), затем запуск в event loop (плейлист и очередь импорта)
        iid = await asyncio.to_thread(functools.partial(self.svc.draft_import, user, user.tg_id, name, items,
                                                        source=self.source(request)))
        self.svc.confirm_import(iid)
        imp = await asyncio.to_thread(self.db.get_import, iid)
        pl = await asyncio.to_thread(self.db.get_playlist, imp["playlist_id"]) if imp and imp["playlist_id"] else None
        self.log(user.login, f"📄 Импорт «{name}»: {len(items)} треков")
        return web.json_response({"import_id": iid, "format": fmt, "total": len(items), "name": Path(name).stem or name,
                                  "playlist": {"id": pl["id"], "name": pl["name"]} if pl else None})

    async def import_cancel(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        data = await self.body(request) if request.can_read_body else {}
        if data.get("id") is not None:  # один импорт; без id — все (как раньше)
            iid, error = self._import_id(data)
            if error:
                return error
            n = 1 if self.svc.stop_import(iid, user.login) else 0
            if not n:
                return _err(404, "not_found", message="Импорт не найден или уже завершён.")
        else:
            n = self.svc.cancel_imports(user.login)
        if n:
            self.log(user.login, f"⏹ Импорт остановлен ({n})")
        return web.json_response({"cancelled": n})

    @staticmethod
    def _import_id(data: dict) -> tuple[int, web.Response | None]:
        try:
            iid = int(data.get("id"))
        except (TypeError, ValueError):
            return 0, _err(400, "bad_id")
        return iid, None

    async def import_pause(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        iid, error = self._import_id(await self.body(request))
        if error:
            return error
        if not self.svc.pause_import(iid, user.login):
            return _err(409, "bad_state", message="Импорт нельзя поставить на паузу.")
        self.log(user.login, "⏸ Импорт на паузе")
        return web.json_response({"ok": True, "state": "paused"})

    async def import_resume(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        iid, error = self._import_id(await self.body(request))
        if error:
            return error
        if not self.svc.resume_import(iid, user.login):
            return _err(409, "bad_state", message="Импорт нельзя продолжить.")
        self.log(user.login, "▶️ Импорт продолжен")
        return web.json_response({"ok": True, "state": "running"})

    async def import_failed(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        iid, error = self._import_id(dict(request.query))
        if error:
            return error
        imp = self.db.get_import(iid)
        if not imp or imp["login"] != user.login:
            return _err(404, "not_found")
        rows = self.db.failed_import_items(iid)[:MAX_PICK]
        return web.json_response({"items": [{"artist": r["artist"], "title": r["title"], "album": r["album"] or "",
                                             "note": r["note"] or ""} for r in rows]})

    async def library_keys(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        """Ключи медиатеки для пометки ✓ в поиске: match_key («артист|название») и ISRC."""
        def build() -> dict:
            keys = self.db.user_track_keys(user.login)
            rows = self.db._q(  # noqa: SLF001 — узкий read-only запрос, без новой функции в db.py
                "SELECT DISTINCT t.isrc FROM user_tracks u JOIN tracks t ON t.id=u.track_id "
                "WHERE u.login=? AND t.isrc IS NOT NULL AND t.isrc<>''", (user.login,))
            return {"keys": sorted(keys), "isrc": sorted(r["isrc"] for r in rows)}
        return web.json_response(await asyncio.to_thread(build))

    async def wave_refresh(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        try:
            n = await radio.refresh_wave_playlist(self.jf, user, CFG.wave_playlist_size, self.db)
        except Exception as e:  # noqa: BLE001
            log.warning("wave playlist refresh failed: %s", e)
            return _err(502, "jellyfin_failed", message="Jellyfin недоступен, попробуйте позже.")
        return web.json_response({"tracks": n})

    async def wave_discover(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        data = await self.body(request) if request.can_read_body else {}
        size = data.get("size")
        try:
            size = max(1, min(50, int(size))) if size else CFG.wave_size
        except (TypeError, ValueError):
            size = CFG.wave_size
        if user.login in self._wave_busy:
            return _err(409, "busy", message="Волна уже подбирается, подождите.")
        self._wave_busy.add(user.login)
        try:
            reqs, seeds = await wave.build(self.db, self.jf, user, size)
        except Exception as e:  # noqa: BLE001
            log.warning("wave build failed: %s", e)
            return _err(502, "wave_failed", message="Не удалось подобрать треки, попробуйте позже.")
        finally:
            self._wave_busy.discard(user.login)
        if reqs:
            def remember() -> None:
                from .util import match_key
                owned = self.db.user_track_keys(user.login)
                for r in reqs:
                    k = match_key(r.artist, r.title)
                    self.db.mark_wave(user.login, k, k in owned)
            self.start_job(user, reqs, "Моя Волна", origin="wave", after=remember)
        return web.json_response({"seeds": seeds, "queued": [r.label for r in reqs]})

    async def wave_now(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        """«Сейчас в эфире» станции «Моя Волна» этого пользователя (для встроенного плеера и Media Session)."""
        st = self.radio.station(user.login) if self.radio is not None else None
        item = st.current if st is not None else None
        out: dict[str, Any] = {"live": st is not None, "listeners": len(st.listeners) if st else 0,
                               "artist": None, "title": None, "album": None, "duration": None,
                               "started_at": None, "cover": None, "track_id": None}
        if not item:
            return web.json_response(out)
        ticks = item.get("RunTimeTicks")
        out.update({
            "artist": (item.get("Artists") or [item.get("AlbumArtist") or ""])[0] or None,
            "title": item.get("Name") or None, "album": item.get("Album") or None,
            "duration": int(ticks // 10_000_000) if isinstance(ticks, (int, float)) and ticks > 0 else None,
            "started_at": int(st.started_at) if st and st.started_at else None,
        })
        path = item.get("Path") or ""

        def find_cover() -> tuple[str | None, int | None]:
            hit = self.db.user_track_by_path(user.login, path) if path else None
            if hit and _cover_path(hit[0].path, hit[1]):
                return cover_url(self.bot_token, user.login, hit[0].id), hit[0].id
            return None, hit[0].id if hit else None
        out["cover"], out["track_id"] = await asyncio.to_thread(find_cover)
        return web.json_response(out)

    # ---------------------------------------------------------------- сведения для сторонних клиентов
    async def info(self, _: web.Request) -> web.Response:
        """GET /api/info (без авторизации): версия API, способы входа и лимиты — чтобы клиент подстроился сам."""
        return self._plain({
            "name": "PiMusic", "api_version": API_VERSION, "openapi": "/api/openapi.json",
            "auth": {"bot": self.bot_username, "login_widget": bool(CFG.login_widget and self.bot_username),
                     "methods": ["bearer", "telegram_init_data"], "platforms": list(auth.PLATFORMS),
                     "max_devices": auth.MAX_DEVICES, "login_ttl": auth.NONCE_TTL},
            "limits": {"tracks_per_request": MAX_TRACK_IDS, "play_queue": MAX_PLAY, "library_page": 500,
                       "pick_tracks": MAX_PICK, "playlists_per_user": PL_MAX_PER_USER,
                       "playlist_tracks": PL_MAX_TRACKS, "playlist_members": PL_MAX_MEMBERS,
                       "playlist_name": PL_MAX_NAME, "playlist_description": PL_MAX_DESC,
                       "setting_value_bytes": SETTING_MAX_VALUE, "settings_keys": SETTING_MAX_KEYS,
                       "upload_max_mb": CFG.upload_max_mb, "upload_files_per_request": uploads_mod.MAX_FILES_PER_REQUEST,
                       "import_max_bytes": IMPORT_MAX_BYTES, "stream_ttl": STREAM_TTL, "cover_ttl": COVER_TTL},
            "features": ["search", "catalog", "download", "download_link", "library", "playlists", "shared_playlists",
                         "stream", "lyrics", "lyrics_refetch", "redownload", "settings", "uploads", "import",
                         "wave", "radio", "devices", "updates", "admin"],
        })

    async def openapi(self, _: web.Request) -> web.StreamResponse:
        """GET /api/openapi.json — машиночитаемое описание API (OpenAPI 3.1), из него можно сгенерировать клиент."""
        if not OPENAPI_PATH.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(OPENAPI_PATH, headers={"Content-Type": "application/json; charset=utf-8",
                                                       "Cache-Control": "no-cache"})

    @staticmethod
    def _is_admin(user: User, tg_id: int) -> bool:
        return bool(user.is_admin or tg_id in CFG.admin_telegram_ids)

    # ---------------------------------------------------------------- интернет-радио (как /radio в боте)
    @staticmethod
    def _radio_json(r: Any) -> dict:
        return {"id": r["id"], "name": r["name"], "url": r["url"]}

    async def radio_list(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        """Общие станции интернет-радио (в Jellyfin: Live TV → Каналы); url — поток, его можно играть напрямую."""
        rows = await asyncio.to_thread(self.db.list_radio, None)
        return web.json_response({"items": [self._radio_json(r) for r in rows if r["owner"] is None]})

    async def radio_add(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """POST /api/radio {"name", "url"} — добавить общую станцию (как /radio_add в боте)."""
        data = await self.body(request)
        name, url = data.get("name"), data.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            return _err(400, "bad_station", message="Нужны название и ссылка на поток.")
        name, url = name.strip(), url.strip()
        # текст + DNS: имя, указывающее на адрес локальной сети или шлюза, не принимаем (SSRF через ffmpeg)
        if not radio.valid_station(name, url) or not await util.is_public_url_resolved(url):
            return _err(400, "bad_station", message="Нужны название (до 100 символов) и публичная http(s)-ссылка "
                                                    "на поток.")

        def run() -> int:
            p = radio.station_path(None, name)
            radio.write_strm(p, url)
            return self.db.add_radio(None, name[:100], url, str(p))
        rid = await asyncio.to_thread(run)
        synced = await self._radio_sync()
        row = await asyncio.to_thread(self.db.get_radio, rid)
        return web.json_response({**self._radio_json(row), "jellyfin": synced})

    async def radio_delete(self, request: web.Request, user: User, tg_id: int) -> web.Response:
        """POST /api/radio/{id}/delete — только администратор (как /radio_del)."""
        if not self._is_admin(user, tg_id):
            return _err(403, "forbidden", message="Только для администратора.")
        rid = _valid_id(request.match_info["rid"])
        row = await asyncio.to_thread(self.db.get_radio, rid) if rid else None
        if not row or row["owner"] is not None:
            return _err(404, "not_found", message="Нет такой станции.")

        def run() -> None:
            Path(row["path"]).unlink(missing_ok=True)
            self.db.delete_radio(row["id"])
        await asyncio.to_thread(run)
        return web.json_response({"ok": True, "jellyfin": await self._radio_sync()})

    async def _radio_sync(self) -> bool:
        """Список станций → M3U-тюнер Jellyfin (best effort): False — Jellyfin не принял, станция всё равно сохранена."""
        try:
            await radio.sync_tuner(self.db, self.jf)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("список радиостанций в Jellyfin не обновлён: %s", e)
            return False

    # ---------------------------------------------------------------- администратор (как /status и /users)
    async def admin_status(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        if not self._is_admin(user, tg_id):
            return _err(403, "forbidden", message="Только для администратора.")
        total, used, free = await asyncio.to_thread(library.disk_usage)
        st = await asyncio.to_thread(self.db.stats)
        try:
            slsk: bool | None = bool(await asyncio.wait_for(downloaders.SLSKD.online(), 10))
        except Exception:  # noqa: BLE001
            slsk = None
        return web.json_response({"disk": {"total": total, "used": used, "free": free}, "users": st["users"],
                                  "tracks": st["tracks"], "links": st["links"], "soulseek_online": slsk,
                                  "queue": self.svc.queue.qsize(), "jellyfin_url": CFG.jellyfin_public_base})

    async def admin_users(self, _: web.Request, user: User, tg_id: int) -> web.Response:
        if not self._is_admin(user, tg_id):
            return _err(403, "forbidden", message="Только для администратора.")

        def build() -> list[dict]:
            return [{"login": u.login, "tracks": self.db.user_track_count(u.login), "is_admin": bool(u.is_admin),
                     "telegram": u.tg_id is not None} for u in self.db.all_users()]
        return web.json_response({"items": await asyncio.to_thread(build)})


def _track_keys_size(path: str) -> dict:
    """artist_key / album_key — папки трека, как их ждут /api/library/artist?a= и /api/library/album?a=&b=
    (структура личной медиатеки повторяет общий кэш); size — размер файла в байтах (None — файла нет).
    Вызывать в потоке: stat файла."""
    try:
        size: int | None = os.stat(path).st_size
    except (OSError, ValueError):
        size = None
    a, b = DB.library_folders(path) if path else ("", "")
    return {"artist_key": a or None, "album_key": b or None, "size": size}


def _cors_add(headers: Any, origin: str) -> None:
    headers["Access-Control-Allow-Origin"] = origin
    headers["Access-Control-Allow-Headers"] = CORS_ALLOW_HEADERS
    headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, PUT, DELETE, OPTIONS"
    headers["Access-Control-Expose-Headers"] = CORS_EXPOSE_HEADERS
    headers["Access-Control-Max-Age"] = "600"
    vary = headers.get("Vary")
    headers["Vary"] = f"{vary}, Origin" if vary and "origin" not in vary.lower() else (vary or "Origin")


# Пути с CORS для нативных оболочек. /stream/ — живой поток «Моей Волны»: интерфейс Windows 2.0 открыт с
# http://tauri.localhost и ставит <audio crossorigin="anonymous"> (для эквалайзера Web Audio), так что без
# Access-Control-Allow-Origin WebView2 рвал поток сразу после ответа — станция стартовала и через 5 попыток
# клиента останавливалась («Поток „Моей Волны“ сейчас недоступен»).
CORS_PATHS = ("/api/", "/stream/")


def _cors_allowed(request: web.Request, origins: set[str]) -> str:
    origin = request.headers.get("Origin", "")
    return origin if origin and origin in origins and request.path.startswith(CORS_PATHS) else ""


def cors_middleware(origins: set[str]) -> Callable:
    """CORS для /api/* только для Origin нативных оболочек (Tauri, Capacitor, localhost) и CORS_ORIGINS.
    Middleware отвечает только на preflight (OPTIONS); заголовки остальным ответам ставит cors_prepare —
    в on_response_prepare, чтобы их получал КАЖДЫЙ ответ: ошибки 401/404/429/500, FileResponse потока и обложек
    (206, HEAD), multipart-загрузки, ответы, подготовленные до выхода из обработчика."""
    @web.middleware
    async def mw(request: web.Request, handler: Callable) -> web.StreamResponse:
        origin = _cors_allowed(request, origins)
        if origin and request.method == "OPTIONS":
            return web.Response(status=204)  # заголовки добавит cors_prepare
        return await handler(request)
    return mw


def cors_prepare(origins: set[str]) -> Callable:
    async def on_prepare(request: web.Request, response: web.StreamResponse) -> None:
        origin = _cors_allowed(request, origins)
        if origin:
            _cors_add(response.headers, origin)
    return on_prepare


async def _static_no_cache(request: web.Request, response: web.StreamResponse) -> None:
    """Статика Mini App — всегда с перепроверкой (ETag/Last-Modified → 304): иначе после выкладки браузер по
    эвристике держит старые ES-модули вперемешку с новыми. Остальные ответы не трогаем."""
    if request.path.startswith("/app/static/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"


# CSP Mini App. Внешние источники: telegram-web-app.js и виджет входа (telegram.org; виджет — iframe/запрос
# oauth.telegram.org и eval для data-onauth → 'unsafe-eval'), шрифты Google, обложки с CDN (Deezer, iTunes и т.п. —
# любой https:), data:/blob: для картинок, масок и тишины в <audio>. Встроенных скриптов в static нет; <style> вставляет
# telegram-web-app.js → 'unsafe-inline' только для стилей. ipc:/ipc.localhost — мост Tauri (Windows-оболочка).
# frame-ancestors: Telegram Web (web/webk/webz.telegram.org); Tauri/Capacitor грузят страницу верхним окном.
CSP_APP = "; ".join((
    "default-src 'self'",
    "script-src 'self' https://telegram.org 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    "media-src 'self' blob: data: https://*.dzcdn.net",  # 30-секундные фрагменты Deezer (search.valid_preview)
    "connect-src 'self' https://oauth.telegram.org ipc: http://ipc.localhost",
    "frame-src https://oauth.telegram.org tg:",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self' https://web.telegram.org https://webk.telegram.org https://webz.telegram.org",
))
CSP_API = "default-src 'none'; frame-ancestors 'none'"  # JSON, поток и обложки — не страницы
REFERRER_POLICY = "strict-origin-when-cross-origin"  # наружу — только origin (виджету Telegram он нужен)


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    """nosniff и Referrer-Policy — на всё; CSP — на /app/ (страница, SW, статика) и /api/. Не перетирает
    заголовки, которые обработчик уже выставил сам."""
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", REFERRER_POLICY)
    path = request.path
    if path == "/app" or path.startswith("/app/"):
        h.setdefault("Content-Security-Policy", CSP_APP)
    elif path.startswith("/api/"):
        h.setdefault("Content-Security-Policy", CSP_API)


# ------------------------------------------------------------------ автообновление приложений
# Репозиторий приватный — обновления раздаёт сам сервис из UPDATES_DIR. Без авторизации: приложение проверяет
# обновления ещё до входа. Только плоские имена из белого списка и известные типы; ни листинга, ни подкаталогов.
UPDATE_NAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
UPDATE_TYPES = {".json": "application/json", ".exe": "application/octet-stream",
                ".apk": "application/vnd.android.package-archive", ".sig": "text/plain; charset=utf-8"}
UPDATE_BINARY_CACHE = "public, max-age=604800"  # бинарники с версией в имени — неделя; манифесты — no-cache


def _update_file(name: str) -> Path | None:
    """Файл обновления или None: имя по белому списку, без «..», внутри UPDATES_DIR (симлинк наружу — нет)."""
    if not UPDATE_NAME_RE.fullmatch(name) or ".." in name:
        return None
    if Path(name).suffix.lower() not in UPDATE_TYPES:
        return None
    try:
        root = CFG.updates_dir.resolve(strict=True)
        path = (root / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if path.parent != root or not path.is_file():
        return None
    return path


async def updates(request: web.Request) -> web.StreamResponse:
    """GET /updates/{name}: манифесты (*.json, no-cache) и бинарники (Range через FileResponse, долгий кэш)."""
    name = request.match_info.get("name", "")
    path = await asyncio.to_thread(_update_file, name)
    if path is None:
        raise web.HTTPNotFound()
    ext = path.suffix.lower()
    # манифест и его подпись (windows-native.json.sig) меняются на месте — без кэша, иначе подпись разойдётся с файлом
    cache = "no-cache" if ext == ".json" or name.endswith(".json.sig") else UPDATE_BINARY_CACHE
    return web.FileResponse(path, headers={"Content-Type": UPDATE_TYPES[ext], "Cache-Control": cache})


def setup_webapp(app: web.Application, *, db: DB, svc: Service, jf: Any, bot_token: str,
                 radio: Any = None) -> WebApp:
    """Регистрирует /app/ (SPA) и /api/* в aiohttp-приложении. Вызывать до запуска AppRunner.

    radio — RadioServer (необязательно): без него /api/wave/now всегда отвечает live=false.
    """
    w = WebApp(db=db, svc=svc, jf=jf, bot_token=bot_token, radio_server=radio)
    app[_STATE_KEY] = w
    origins = set(auth.DEFAULT_CORS_ORIGINS) | set(CFG.cors_origins)
    app.middlewares.append(cors_middleware(origins))
    app.on_response_prepare.append(cors_prepare(origins))
    app.on_response_prepare.append(_static_no_cache)
    app.on_response_prepare.append(_security_headers)
    r = app.router
    r.add_get("/app", w.redirect)
    r.add_get("/app/", w.index)
    r.add_get("/app/sw.js", w.service_worker)
    r.add_get("/app/manifest.webmanifest", w.manifest)
    r.add_static("/app/static/", STATIC_DIR, append_version=False)
    r.add_get("/updates/{name}", updates)
    r.add_get("/api/me", w.api(w.me))
    r.add_get("/api/search", w.api(w.do_search))
    r.add_get("/api/album", w.api(w.album))
    r.add_post("/api/download", w.api(w.download))
    r.add_post("/api/download/link", w.api(w.download_link))
    r.add_get("/api/activity", w.api(w.activity))
    r.add_get("/api/catalog/album", w.api(w.catalog_album))
    r.add_get("/api/catalog/artist", w.api(w.catalog_artist))
    r.add_get("/api/catalog/artist/tracks", w.api(w.catalog_artist_tracks))
    r.add_get("/api/library", w.api(w.library))
    r.add_get("/api/library/artists", w.api(w.library_artists))
    r.add_get("/api/library/albums", w.api(w.library_albums))
    r.add_get("/api/library/genres", w.api(w.library_genres))
    r.add_get("/api/library/artist", w.api(w.library_artist))
    r.add_get("/api/library/album", w.api(w.library_album))
    r.add_get("/api/artist-image", w.artist_image)
    r.add_post("/api/delete", w.api(w.delete))
    r.add_get("/api/cover/{track_id}", w.cover)
    r.add_get("/api/stream/{track_id}", w.stream)
    r.add_get("/api/tracks", w.api(w.tracks))
    r.add_post(r"/api/tracks/{tid:\d+}/redownload", w.api(w.track_redownload))
    r.add_post(r"/api/tracks/{tid:\d+}/lyrics/refetch", w.api(w.lyrics_refetch))
    r.add_get("/api/library/play", w.api(w.library_play))
    r.add_get("/api/playlists", w.api(w.playlists))
    r.add_post("/api/playlists", w.api(w.playlist_create))
    r.add_get("/api/playlists/join", w.api(w.playlist_join_preview))
    r.add_post("/api/playlists/join", w.api(w.playlist_join))
    r.add_get("/api/playlists/for-track", w.api(w.playlists_for_track))
    r.add_get(r"/api/playlists/{pid:\d+}", w.api(w.playlist_get))
    r.add_post("/api/library/delete", w.api(w.library_delete))
    for action in ("update", "share", "add", "remove", "move", "move_many", "delete", "leave", "kick", "save"):
        path = action.replace("_", "-")  # move_many -> /move-many
        r.add_post(rf"/api/playlists/{{pid:\d+}}/{path}", w.api(getattr(w, f"playlist_{action}")))
    r.add_get("/api/import", w.api(w.imports))
    r.add_post("/api/import/file", w.api(w.import_file))
    r.add_post("/api/import/cancel", w.api(w.import_cancel))
    r.add_post("/api/import/pause", w.api(w.import_pause))
    r.add_post("/api/import/resume", w.api(w.import_resume))
    r.add_get("/api/import/failed", w.api(w.import_failed))
    r.add_get("/api/library/keys", w.api(w.library_keys))
    r.add_post("/api/wave/refresh", w.api(w.wave_refresh))
    r.add_post("/api/wave/discover", w.api(w.wave_discover))
    r.add_get("/api/wave/now", w.api(w.wave_now))
    r.add_get("/api/lyrics/{track_id}", w.api(w.lyrics))
    r.add_get("/api/settings", w.api(w.settings_get))
    r.add_put("/api/settings/{key}", w.api(w.settings_put))
    r.add_delete("/api/settings/{key}", w.api(w.settings_delete))
    r.add_get("/api/auth/config", w.auth_config)
    r.add_post("/api/auth/start", w.auth_start)
    r.add_post("/api/auth/poll", w.auth_poll)
    r.add_post("/api/auth/telegram", w.auth_telegram)
    r.add_post("/api/auth/logout", w.api(w.auth_logout))
    r.add_get("/api/devices", w.api(w.devices))
    r.add_post("/api/upload", w.api(w.upload))
    r.add_get("/api/uploads", w.api(w.uploads_list))
    for action in ("info", "skip", "cancel"):
        r.add_post(rf"/api/uploads/{{uid:\d+}}/{action}", w.api(getattr(w, f"upload_{action}")))
    r.add_post("/api/devices/revoke-all", w.api(w.devices_revoke_all))
    r.add_post(r"/api/devices/{did:\d+}/revoke", w.api(w.device_revoke))
    r.add_get("/api/info", w.info)
    r.add_get("/api/openapi.json", w.openapi)
    r.add_get("/api/radio", w.api(w.radio_list))
    r.add_post("/api/radio", w.api(w.radio_add))
    r.add_post(r"/api/radio/{rid:\d+}/delete", w.api(w.radio_delete))
    r.add_get("/api/admin/status", w.api(w.admin_status))
    r.add_get("/api/admin/users", w.api(w.admin_users))
    return w
