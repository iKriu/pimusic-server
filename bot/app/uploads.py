"""Загрузка своих аудиофайлов (бот, Mini App) -> общий кэш и медиатека пользователя.

Конвейер (отдельные воркеры, очередь в БД переживает перезапуск):
ffprobe (это аудио?) -> опознание: теги файла -> AcoustID (если есть ключ) -> MusicBrainz/iTunes по имени файла
(с проверкой длительности) -> нет уверенности: need_info (бот спрашивает «Исполнитель — Название»).
Дальше — тот же library.ingest, что и для скачанного: MP3 320 (MP3 любого битрейта не перекодируется),
теги/обложка/текст, дедупликация по ISRC/MBID/записи, жёсткая ссылка в медиатеку.

Безопасность: имя файла пользователя только показывается, файл лежит в incoming/uploads/<uuid>.<расширение
из белого списка>; размер, число, свободное место — ограничены.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from . import acoustid, library, metadata
from .config import CFG
from .db import DB, User
from .metadata import Hint
from .util import AUDIO_EXTS, clean_title, probe, split_artist_title

log = logging.getLogger(__name__)

MAX_FILES_PER_REQUEST = 50
MAX_ACTIVE_PER_USER = 200
BOT_MAX_BYTES = 20 * 1024 * 1024        # Bot API отдаёт ботам файлы только до 20 МБ
MAX_DURATION = 4 * 3600
DURATION_TOL = 7                         # сек: расхождение с найденной записью MusicBrainz/iTunes
NEED_INFO_TTL = 14 * 86400               # «ждут названия» дольше — отменяются
ORPHAN_TTL = 86400
RECEIVING_TTL = 3600                     # «получаю» дольше часа — оборванная загрузка
TERMINAL = ("done", "duplicate", "failed", "cancelled")
# контейнеры, которые принимаем (format_name ffprobe; «mov,mp4,m4a,3gp,3g2,mj2» — один из них)
AUDIO_FORMATS = {"mp3", "flac", "ogg", "wav", "mov", "mp4", "m4a", "matroska", "webm", "aiff", "asf", "aac"}
FAIL_TEXT = "не удалось обработать файл"  # подробности — только в логе
STATE_TEXT = {"receiving": "получаю", "queued": "в очереди", "analyzing": "опознаю", "need_info": "ждёт названия",
              "processing": "сохраняю", "done": "сохранён", "duplicate": "уже был", "failed": "не удалось",
              "cancelled": "отменён"}


class UploadError(Exception):
    """Файл не принят: args[0] — код (too_large / bad_type / disk_full / too_many), args[1] — текст."""


def upload_dir() -> Path:
    return CFG.incoming_dir / "uploads"


def clean_name(name: Any) -> str:
    """Имя для показа (не путь!): только последняя часть, без управляющих символов, до 200 символов."""
    n = re.split(r"[\\/]", name if isinstance(name, str) else "")[-1]
    n = re.sub(r"[\x00-\x1f\x7f]+", " ", n).strip()
    return n[:200] or "file"


def allowed_ext(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    return ext if ext in AUDIO_EXTS else None


def free_space_ok(extra: int = 0) -> bool:
    try:
        root = CFG.media_root if CFG.media_root.exists() else CFG.incoming_dir.parent
        free = shutil.disk_usage(root).free
    except OSError:
        return True
    return free - extra >= CFG.upload_min_free_gb * 1024 ** 3


def new_path(ext: str) -> Path:
    d = upload_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}{ext}"


def guess_from_name(name: str) -> tuple[str, str]:
    """«03. Artist - Title (Official Audio).flac» -> ("Artist", "Title")."""
    stem = Path(name).stem.replace("_", " ")
    stem = re.sub(r"^\s*\d{1,3}\s*[.\-_)]?\s+", "", stem)
    return split_artist_title(clean_title(stem))


def fmt_dur(sec: float | None) -> str:
    s = int(sec or 0)
    return f"{s // 60}:{s % 60:02d}" if s else ""


class Uploads:
    """Очередь загрузок пользователя. Сообщения — через svc.messenger (бот), обновление Jellyfin — через svc."""

    def __init__(self, svc: Any, db: DB):
        self.svc, self.db = svc, db
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task] = []

    # -------------------------------------------------------------- приём
    def check_accept(self, user: User, size: int | None = None) -> None:
        if self.db.active_upload_count(user.login) >= MAX_ACTIVE_PER_USER:
            raise UploadError("too_many", f"В очереди уже {MAX_ACTIVE_PER_USER} файлов — дождитесь обработки.")
        if size and size > CFG.upload_max_mb * 1024 * 1024:
            raise UploadError("too_large", f"Файл больше {CFG.upload_max_mb} МБ.")
        if self.db.queued_upload_bytes(user.login) + (size or 0) > CFG.upload_user_quota_gb * 1024 ** 3:
            raise UploadError("quota", f"В очереди уже больше {CFG.upload_user_quota_gb} ГБ ваших файлов — "
                                       "дождитесь обработки.")
        if not free_space_ok(size or 0):
            raise UploadError("disk_full", "На сервере мало места — загрузка временно недоступна.")

    def begin(self, user: User, name: str, *, origin: str, chat_id: int | None, batch: str) -> tuple[int, Path]:
        """Строка receiving (пачка из Telegram не «закончится» раньше времени) и путь для записи."""
        name = clean_name(name)
        ext = allowed_ext(name)
        if not ext:
            raise UploadError("bad_type", "Это не аудиофайл (mp3, flac, wav, m4a, ogg, opus…).")
        path = new_path(ext)
        uid = self.db.create_upload(user.login, name, str(path), state="receiving", origin=origin,
                                    chat_id=chat_id, batch=batch)
        return uid, path

    def received(self, uid: int, size: int) -> None:
        self.db.set_upload(uid, expect=("receiving",), state="queued", size=size)
        self._wake.set()

    def abort(self, uid: int, note: str) -> None:
        row = self.db.get_upload(uid)
        if row:
            Path(row["path"]).unlink(missing_ok=True)
            self.db.set_upload(uid, state="failed", note=note[:300])

    # -------------------------------------------------------------- ответы пользователя
    def provide_info(self, login: str, uid: int, artist: str, title: str, album: str = "") -> bool:
        row = self.db.get_upload(uid)
        if not row or row["login"] != login:
            return False
        ok = self.db.set_upload(uid, expect=("need_info",), state="queued", manual=1, guess_artist=artist[:200],
                                guess_title=title[:300], guess_album=(album or row["guess_album"] or "")[:300])
        if ok:
            self._wake.set()
        return ok

    def skip(self, login: str, uid: int) -> bool:
        """«Оставить как есть»: сохранить с тем, что есть (теги/имя файла)."""
        row = self.db.get_upload(uid)
        if not row or row["login"] != login:
            return False
        title = row["guess_title"] or Path(row["orig_name"]).stem
        ok = self.db.set_upload(uid, expect=("need_info",), state="queued", manual=1, guess_title=title)
        if ok:
            self._wake.set()
        return ok

    def cancel(self, login: str, uid: int) -> bool:
        row = self.db.get_upload(uid)
        if not row or row["login"] != login:
            return False
        if not self.db.set_upload(uid, expect=("queued", "need_info"), state="cancelled"):
            return False
        Path(row["path"]).unlink(missing_ok=True)
        return True

    # -------------------------------------------------------------- воркеры
    def start(self) -> None:
        req, failed = self.db.reset_uploads()
        if req or failed:
            log.info("загрузки после перезапуска: в очередь %d, прервано %d", req, failed)
        for i in range(max(1, CFG.upload_workers)):
            self._workers.append(asyncio.create_task(self._worker(), name=f"upload-{i}"))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()

    async def _worker(self) -> None:
        while True:
            try:
                row = await asyncio.to_thread(self.db.claim_upload)
                if row is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), 60)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self.process(row)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("upload worker error")
                await asyncio.sleep(5)

    async def process(self, row: Any) -> str:
        """Одна загрузка (state=analyzing). Возвращает итоговое состояние."""
        uid, path = row["id"], Path(row["path"])
        user = self.db.user_by_login(row["login"])
        try:
            if not user:
                raise UploadError("failed", "пользователь удалён")
            info = await probe(path) if path.is_file() else None
            if info is None or not info.duration or info.duration <= 0:
                raise UploadError("failed", "это не аудио или файл повреждён")
            if not set(info.format_name.split(",")) & AUDIO_FORMATS:
                log.warning("upload %s: неподходящий контейнер %r", uid, info.format_name)
                raise UploadError("failed", "это не аудио или формат не поддерживается")
            if info.duration > MAX_DURATION:
                raise UploadError("failed", "длиннее 4 часов")
            self.db.set_upload(uid, duration=info.duration)
            if row["manual"]:
                hint = Hint(artist=row["guess_artist"] or "", title=row["guess_title"] or "",
                            album=row["guess_album"] or "", duration=info.duration)
            else:
                hint, guess = await self._identify(path, row["orig_name"], info)
                if hint is None:
                    self.db.set_upload(uid, expect=("analyzing",), state="need_info", guess_artist=guess[0],
                                       guess_title=guess[1], guess_album=guess[2])
                    await self._notify(uid)
                    return "need_info"
            self.db.set_upload(uid, state="processing")
            res = await library.ingest(self.db, path, hint, source="upload", login=user.login, keep_mp3=True)
            note = None
            if info.codec == "mp3" and info.bitrate and info.bitrate < 300_000:
                note = f"MP3 {info.bitrate // 1000} kbps — сохранён без перекодирования"
            if res.replaced:  # такой трек уже был — загруженный файл заменил его у всех (ручная загрузка главнее)
                note = "заменил прежний файл трека" + (f"; {note}" if note else "")
            state = "duplicate" if res.reused and not res.replaced else "done"
            self.db.set_upload(uid, state=state, track_id=res.track.id, note=note,
                               guess_artist=res.track.artist, guess_title=res.track.title,
                               guess_album=res.track.album or "")
            self._after_ingest(user)
        except UploadError as e:
            state = "failed"
            path.unlink(missing_ok=True)
            self.db.set_upload(uid, state=state, note=e.args[1] if len(e.args) > 1 else str(e))
        except Exception:  # noqa: BLE001
            log.exception("upload %s failed", uid)
            state = "failed"
            path.unlink(missing_ok=True)
            self.db.set_upload(uid, state=state, note=FAIL_TEXT)
        await self._notify(uid)
        return state

    def _after_ingest(self, user: User) -> None:
        for name, args in (("_schedule_refresh", (user,)), ("loudness_changed", ()), ("_schedule_artwork", ())):
            fn = getattr(self.svc, name, None)
            if fn is not None:
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    log.debug("%s failed", name, exc_info=True)

    async def _identify(self, path: Path, orig_name: str, info: Any) -> tuple[Hint | None, tuple[str, str, str]]:
        """(подсказка для ingest или None, догадка для формы need_info)."""
        tags = info.tags
        t_artist = tags.get("artist") or tags.get("album_artist") or ""
        t_title, t_album = tags.get("title") or "", tags.get("album") or ""
        isrc = tags.get("isrc") or tags.get("tsrc") or ""
        dur = info.duration
        if t_artist and t_title:  # теги пользователя — им и верим (ingest всё равно уточнит по MusicBrainz)
            return Hint(artist=t_artist, title=t_title, album=t_album, duration=dur, isrc=isrc), \
                (t_artist, t_title, t_album)
        if CFG.acoustid_api_key:
            try:
                m = await acoustid.lookup(path, CFG.acoustid_api_key)
            except Exception as e:  # noqa: BLE001
                log.warning("AcoustID lookup failed: %s", e)
                m = None
            if m:
                return Hint(artist=m.artist, title=m.title, album=m.album or t_album, duration=dur), \
                    (m.artist, m.title, m.album)
        f_artist, f_title = guess_from_name(orig_name)
        artist, title = t_artist or f_artist, t_title or f_title
        guess = (artist, title or Path(orig_name).stem, t_album)
        if not artist or not title:
            return None, guess
        probe_hint = Hint(artist=artist, title=title, album=t_album, duration=dur)
        try:
            mb = await metadata.lookup_musicbrainz(probe_hint)
        except Exception as e:  # noqa: BLE001
            log.debug("MB lookup failed: %s", e)
            mb = None
        if mb and (not mb.duration or abs(mb.duration - dur) <= DURATION_TOL):
            return Hint(artist=mb.artist, title=mb.title, album=mb.album, duration=dur, isrc=mb.isrc or ""), guess
        try:
            it = await metadata.lookup_itunes(probe_hint)
        except Exception as e:  # noqa: BLE001
            log.debug("iTunes lookup failed: %s", e)
            it = None
        it_dur = (it or {}).get("trackTimeMillis", 0) / 1000
        if it and (not it_dur or abs(it_dur - dur) <= DURATION_TOL):
            return Hint(artist=it.get("artistName") or artist, title=it.get("trackName") or title,
                        album=t_album, duration=dur), guess
        return None, guess

    # -------------------------------------------------------------- уведомления
    def item_line(self, row: Any) -> str:
        label = f"{row['guess_artist']} — {row['guess_title']}" if row["guess_artist"] and row["guess_title"] \
            else row["orig_name"]
        icon = {"done": "✅", "duplicate": "♻️", "failed": "❌", "need_info": "❓", "cancelled": "⏹"}.get(row["state"],
                                                                                                         "⏳")
        tail = f" — {row['note']}" if row["state"] == "failed" and row["note"] else ""
        return f"{icon} {label}{tail}"

    async def _notify(self, uid: int) -> None:
        """need_info — сразу вопрос (бот). Итог — одним сообщением на пачку, когда в ней всё обработано;
        позже решённые (после ответа) — отдельной строкой."""
        messenger = getattr(self.svc, "messenger", None)
        row = self.db.get_upload(uid)
        if not row or not row["chat_id"] or messenger is None:
            return
        try:
            if row["state"] == "need_info":
                if row["origin"] == "bot":
                    await self._ask(messenger, row)
                await self._maybe_summary(messenger, row)
                return
            if row["state"] not in TERMINAL:
                return
            key = f"upl_batch:{row['batch']}"
            if row["batch"] and self.db.kv_get(key):
                await messenger.send(row["chat_id"], self.item_line(row))  # дозаполнили после вопроса
                return
            await self._maybe_summary(messenger, row)
        except Exception as e:  # noqa: BLE001
            log.warning("upload notification failed: %s", e)

    async def _ask(self, messenger: Any, row: Any) -> None:
        dur = fmt_dur(row["duration"])
        text = (f"❓ Не смог опознать «{row['orig_name']}»{f' ({dur})' if dur else ''}.\n"
                "Ответьте на это сообщение: «Исполнитель — Название» (можно добавить « — Альбом»).")
        if row["guess_title"]:
            text += f"\nПохоже на: {row['guess_artist'] + ' — ' if row['guess_artist'] else ''}{row['guess_title']}"
        mid = await messenger.send(row["chat_id"], text, buttons=[[("Оставить как есть", f"upl:keep:{row['id']}"),
                                                                   ("Отмена", f"upl:cancel:{row['id']}")]])
        if mid:
            self.db.set_upload(row["id"], prompt_msg=mid)

    async def _maybe_summary(self, messenger: Any, row: Any) -> None:
        batch = self.db.batch_uploads(row["batch"]) if row["batch"] else [row]
        if any(r["state"] in self.db.UPLOAD_ACTIVE for r in batch):
            return
        key = f"upl_batch:{row['batch']}"
        if row["batch"]:
            if self.db.kv_get(key):
                return
            self.db.kv_set(key, "1")
        if len(batch) == 1:
            if batch[0]["state"] != "need_info":
                await messenger.send(row["chat_id"], self.item_line(batch[0]))
            return
        c: dict[str, int] = {}
        for r in batch:
            c[r["state"]] = c.get(r["state"], 0) + 1
        head = (f"📤 Загружено файлов: {len(batch)} — ✅ {c.get('done', 0)}, ♻️ {c.get('duplicate', 0)} уже были, "
                f"❓ {c.get('need_info', 0)} ждут названия, ❌ {c.get('failed', 0)}")
        lines = [self.item_line(r) for r in batch][:40]
        await messenger.send(row["chat_id"], head + "\n" + "\n".join(lines))

    # -------------------------------------------------------------- чистка
    def cleanup(self) -> int:
        """Брошенные «ждут названия» (2 недели) и файлы без записи в базе (сутки)."""
        n = 0
        for r in self.db.stale_receiving(RECEIVING_TTL):  # оборванные приёмы (упал бот/клиент посреди файла)
            if self.db.set_upload(r["id"], expect=("receiving",), state="failed", note="загрузка оборвалась"):
                Path(r["path"]).unlink(missing_ok=True)
                n += 1
        for r in self.db.stale_uploads(NEED_INFO_TTL):
            if self.db.set_upload(r["id"], expect=("need_info",), state="cancelled", note="не дождался названия"):
                Path(r["path"]).unlink(missing_ok=True)
                n += 1
        keep = self.db.upload_paths()
        d = upload_dir()
        if d.is_dir():
            cutoff = time.time() - ORPHAN_TTL
            for f in d.iterdir():
                try:
                    if f.is_file() and str(f) not in keep and f.stat().st_mtime < cutoff:
                        f.unlink()
                        n += 1
                except OSError:
                    continue
        return n
