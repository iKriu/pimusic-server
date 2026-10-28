"""Каскад источников: Soulseek (slskd API → sockseek) → spotDL → yt-dlp. Все результаты — MP3 CBR 320."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from urllib.parse import quote

from .config import CFG
from .sources import LONG_SET, TrackRequest
from .util import (AUDIO_EXTS, contains_all, find_audio_files, is_public_url_resolved, norm, primary_artist, run,
                   similarity, strip_feat, unwanted_variant)

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


@dataclass
class Downloaded:
    path: Path
    source: str        # soulseek / sockseek / spotdl / yt-dlp
    detail: str = ""   # пир, исходный формат и т.п.
    lossless_src: bool = False


async def _noop(_: str) -> None:
    return None


# ====================================================================== slskd (основной Soulseek-клиент)
class Slskd:
    """Работа через REST API slskd: всё, что качает бот, видно в веб-панели slskd."""

    def __init__(self) -> None:
        self.base = CFG.slskd_url + "/api/v0"
        self.headers = {"X-API-Key": CFG.slskd_api_key}
        self._search_lock = asyncio.Lock()  # slskd обрабатывает один POST /searches за раз (иначе 429)

    async def _req(self, method: str, path: str, *, timeout: float = 30, **kw):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=self.headers) as s:
            for attempt in range(5):
                async with s.request(method, self.base + path, **kw) as r:
                    if r.status == 429:
                        await asyncio.sleep(1 + attempt)
                        continue
                    text = await r.text()
                    if r.status >= 400 and r.status != 207:
                        raise RuntimeError(f"slskd {method} {path}: {r.status} {text[:200]}")
                    if not text:
                        return None
                    try:
                        return await r.json(content_type=None)
                    except ValueError:
                        return text
        raise RuntimeError("slskd: слишком много запросов")

    async def online(self) -> bool:
        try:
            st = await self._req("GET", "/server")
            return bool(st and (st.get("isLoggedIn") or "LoggedIn" in str(st.get("state", ""))))
        except Exception:  # noqa: BLE001
            return False

    async def search(self, text: str, timeout_s: int) -> list[dict]:
        sid = str(uuid.uuid4())
        try:
            async with self._search_lock:
                await self._req("POST", "/searches", timeout=timeout_s + 40, json={
                    "id": sid, "searchText": text, "searchTimeout": timeout_s * 1000,
                    "responseLimit": 120, "fileLimit": 5000, "filterResponses": True,
                    "minimumResponseFileCount": 1, "maximumPeerQueueLength": 500,
                })
            for _ in range(timeout_s * 2 + 20):
                await asyncio.sleep(1)
                st = await self._req("GET", f"/searches/{sid}")
                if st and st.get("isComplete"):
                    break
                # Остановить поиск досрочно (PUT) нельзя: в slskd 0.26 ответы остановленного поиска теряются
                # («Failed to finalize search»). Скорость даём штатно: короткий таймаут и лимит ответов.
            await asyncio.sleep(0.3)  # slskd отдаёт ответы только после завершения — даём дописать их в БД
            return await self._req("GET", f"/searches/{sid}/responses") or []
        finally:
            try:
                await self._req("DELETE", f"/searches/{sid}")
            except Exception:  # noqa: BLE001
                pass

    async def enqueue(self, username: str, filename: str, size: int, dest: str) -> bool:
        body = {"username": username, "files": [{"filename": filename, "size": size}],
                "options": {"destination": dest, "externalId": dest}}
        try:
            for attempt in range(3):
                try:
                    await self._req("POST", "/transfers/downloads/batches", json=body, timeout=60)
                    return True
                except RuntimeError as e:
                    # под нагрузкой slskd отвечает 500 «The wait timed out» — это не отказ пира, повторяем
                    if "timed out" not in str(e).lower() or attempt == 2:
                        raise
                    await asyncio.sleep(3 + attempt * 3)
            return False
        except RuntimeError as e:
            if " 404" in str(e) or "405" in str(e):
                # slskd < 0.26 без батчей — старый способ (файл упадёт в downloads/<папка пира>)
                try:
                    await self._req("POST", f"/transfers/downloads/{quote(username, safe='')}",
                                    json=[{"filename": filename, "size": size}])
                    return True
                except RuntimeError as e2:
                    log.info("slskd enqueue failed: %s", e2)
                    return False
            log.info("slskd enqueue failed: %s", e)
            return False

    async def transfer_state(self, username: str, filename: str) -> dict | None:
        try:
            data = await self._req("GET", f"/transfers/downloads/{quote(username, safe='')}")
        except RuntimeError:
            return None
        matches = [f for d in (data or {}).get("directories") or [] for f in d.get("files") or []
                   if f.get("filename") == filename]
        if not matches:
            return None
        # от прошлых попыток могут остаться завершённые записи — берём самую свежую
        return max(matches, key=lambda f: (f.get("requestedAt") or f.get("enqueuedAt") or ""))

    async def cancel(self, username: str, tid: str) -> None:
        try:
            await self._req("DELETE", f"/transfers/downloads/{quote(username, safe='')}/{tid}",
                            params={"remove": "true"})
        except RuntimeError:
            pass


SLSKD = Slskd()


def _basename(remote: str) -> str:
    return re.split(r"[\\/]", remote)[-1]


def _score_file(f: dict, resp: dict, req: TrackRequest) -> float | None:
    """Оценка кандидата. None — не подходит."""
    name = f.get("filename", "")
    ext = (f.get("extension") or Path(_basename(name)).suffix.lstrip(".")).lower()
    br = f.get("bitRate") or 0
    vbr = bool(f.get("isVariableBitRate"))
    if ext == "mp3":
        if br and br < 320:
            return None
        if vbr:
            return None
        if not br:  # неизвестный битрейт — оценим по размеру и длительности ниже
            length = f.get("length") or req.duration
            if length and f.get("size"):
                est = f["size"] * 8 / length / 1000
                if est < 300:
                    return None
            else:
                return None
        q = 100.0
    elif ext == "flac" and CFG.allow_flac_transcode:
        q = 85.0
    else:
        return None
    path_text = name.replace("\\", " ").replace("_", " ")
    fname = Path(_basename(name)).stem
    if req.title and not contains_all(path_text, strip_feat(req.title)):
        return None
    if req.artist and not contains_all(path_text, primary_artist(req.artist)):
        return None
    if not req.artist and req.query:
        # запрос без артиста: большинство слов запроса должно встретиться в пути (папки артиста/альбома + имя)
        q_words = set(norm(req.query).split())
        p_words = set(norm(path_text).split())
        if not q_words or len(q_words & p_words) / len(q_words) < 0.7:
            return None
    # отсекаем ремиксы/лайвы/каверы, если их не просили
    bad = ("remix", "live", "cover", "karaoke", "instrumental", "acapella", "8d", "nightcore", "sped up", "slowed",
           "минус", "remake", "edit", "extended", "acoustic", "demo", "rehearsal", "bootleg", "mashup")
    # norm() вырезает скобки с "edit"/"version" — для стоп-слов нужна простая нормализация
    def _plain(x: str) -> str:
        return " " + re.sub(r"[\W_]+", " ", x.lower()).strip() + " "
    want = _plain(req.title + " " + req.query)
    name_n = _plain(fname)
    for b in bad:  # по целым словам: «live» не должно срабатывать на «Oliver», «cover» — на «Discovery»
        if f" {b} " in name_n and f" {b} " not in want:
            return None
    if unwanted_variant(fname, want):  # sped up / speed up / slowed + reverb / TikTok version …
        return None
    length = f.get("length")
    if req.duration and length:
        diff = abs(length - req.duration)
        if diff > (10 if req.duration_from_mb else 6):
            return None
        q += 10 - diff
    q += similarity(req.title or req.query, fname) * 15
    if resp.get("hasFreeUploadSlot"):
        q += 20
    q -= min(resp.get("queueLength") or 0, 50) * 0.8
    q += min((resp.get("uploadSpeed") or 0) / 1_000_000, 5) * 4
    return q


async def soulseek_slskd(req: TrackRequest, progress: Progress) -> Downloaded | None:
    if not CFG.slskd_api_key or req.duration > LONG_SET:
        return None
    query = f"{primary_artist(req.artist)} {strip_feat(req.title)}" if req.artist else (req.query or req.title)
    query = re.sub(r"[^\w\s']", " ", query).strip()
    if not query:
        return None
    try:
        # импорт: 12 с — популярное находится за секунды; ручной запрос — сколько задано (по умолчанию 25 с)
        timeout_s = min(CFG.soulseek_search_timeout, 12) if req.origin == "import" else CFG.soulseek_search_timeout
        responses = await SLSKD.search(query, timeout_s)
    except Exception as e:  # noqa: BLE001
        log.warning("slskd search failed: %s", e)
        return None
    cands = []
    for resp in responses:
        for f in resp.get("files") or []:
            sc = _score_file(f, resp, req)
            if sc is not None:
                cands.append((sc, resp["username"], f))
    cands.sort(key=lambda x: -x[0])
    if not cands:
        return None
    await progress(f"Soulseek: найдено {len(cands)} вариантов, качаю лучший…")
    # лучший файл от каждого пира, максимум 6 пиров
    per_user: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for _, user, f in cands:
        if user not in seen:
            seen.add(user)
            per_user.append((user, f))
        if len(per_user) >= 6:
            break
    # Гонка: качаем у двух пиров сразу и берём того, кто отдаст файл первым (второго отменяем).
    # Многие пиры держат очередь, и последовательное ожидание каждого съедало минуты на трек.
    for i in range(0, len(per_user), SOULSEEK_RACE):
        batch = per_user[i:i + SOULSEEK_RACE]
        tasks = [asyncio.create_task(_attempt_slskd(user, f, progress)) for user, f in batch]
        winner: Downloaded | None = None
        try:
            for fut in asyncio.as_completed(tasks):
                res = await fut
                if res:
                    winner = res
                    break
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # проигравший мог успеть докачать (или докачал в момент отмены) — убираем его файл
            for r in results:
                if isinstance(r, Downloaded) and r is not winner:
                    cleanup_after(r.path)
        if winner:
            return winner
    return None


SOULSEEK_RACE = 2           # сколько пиров качают один трек одновременно
SOULSEEK_QUEUE_WAIT = 45    # с — сколько ждать, пока пир поставит в очередь и начнёт отдавать
SOULSEEK_STALL = 60         # с — загрузка без прогресса считается зависшей


async def _attempt_slskd(user: str, f: dict, progress: Progress) -> Downloaded | None:
    dest = f"bot/{uuid.uuid4().hex[:12]}"
    try:
        try:
            ok = await SLSKD.enqueue(user, f["filename"], f["size"], dest)
        except asyncio.CancelledError:
            # отменили посреди enqueue: slskd мог уже принять батч и качать — находим и отменяем загрузку
            try:
                st = await asyncio.shield(SLSKD.transfer_state(user, f["filename"]))
                if st and "Completed" not in st.get("state", ""):
                    await asyncio.shield(SLSKD.cancel(user, st.get("id", "")))
            except Exception as e:  # noqa: BLE001
                log.warning("slskd cancel after enqueue failed: %s", e)
            raise
        if not ok:
            return None
        path = await _wait_slskd(user, f, dest, progress)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — сбой одного пира не должен ронять всю гонку
        log.warning("slskd attempt %s failed: %s", user, e)
        return None
    if not path:
        return None
    ext = path.suffix.lower()
    return Downloaded(path, "soulseek", f"{user} · {ext[1:].upper()} {f.get('bitRate') or ''}".strip(),
                      lossless_src=ext == ".flac")


async def _wait_slskd(user: str, f: dict, dest: str, progress: Progress) -> Path | None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    last_bytes, last_change = 0, start
    last_pct = -1
    tid = ""
    try:
        return await _wait_loop(user, f, dest, progress, loop, start, last_bytes, last_change, last_pct)
    except asyncio.CancelledError:
        # проиграл гонку или задачу отменили — отменяем загрузку в slskd, чтобы не качать зря
        st = await asyncio.shield(SLSKD.transfer_state(user, f["filename"]))
        if st and "Completed" not in st.get("state", ""):
            await asyncio.shield(SLSKD.cancel(user, st.get("id", tid)))
        raise


async def _wait_loop(user: str, f: dict, dest: str, progress: Progress, loop, start: float, last_bytes: int,
                     last_change: float, last_pct: int) -> Path | None:
    while loop.time() - start < CFG.soulseek_download_timeout:
        await asyncio.sleep(3)
        st = await SLSKD.transfer_state(user, f["filename"])
        if not st:
            continue
        state = st.get("state", "")
        if "Succeeded" in state:
            return _locate_slskd_file(f["filename"], dest)
        if "Completed" in state:  # Errored / Rejected / Cancelled / TimedOut
            return None
        b = st.get("bytesTransferred") or 0
        if b != last_bytes:
            last_bytes, last_change = b, loop.time()
        pct = int(st.get("percentComplete") or 0)
        if pct // 25 != last_pct // 25 and pct:
            last_pct = pct
            await progress(f"Soulseek: {pct}% от {user}")
        # не начал отдавать за SOULSEEK_QUEUE_WAIT или зависло на SOULSEEK_STALL — пробуем других пиров
        if ((not b and loop.time() - start > SOULSEEK_QUEUE_WAIT)
                or (b and loop.time() - last_change > SOULSEEK_STALL)):
            await SLSKD.cancel(user, st.get("id", ""))
            return None
    st = await SLSKD.transfer_state(user, f["filename"])
    if st:
        await SLSKD.cancel(user, st.get("id", ""))
    return None


def _locate_slskd_file(remote: str, dest: str) -> Path | None:
    base = _basename(remote)
    stem = Path(base).stem
    root = CFG.slskd_downloads_dir
    for d in (root / dest, root):
        if not d.exists():
            continue
        files = [p for p in d.rglob("*") if p.is_file()]  # без glob-шаблонов: в именах бывают [ ] * ?
        exact = [p for p in files if p.name == base]
        if exact:
            return max(exact, key=lambda p: p.stat().st_mtime)
        fuzzy = [p for p in files if p.name.startswith(stem) and p.suffix.lower() in AUDIO_EXTS]
        if fuzzy:
            return max(fuzzy, key=lambda p: p.stat().st_mtime)
    return None


# ====================================================================== sockseek (второй Soulseek-движок)
def _sockseek_bin() -> str | None:
    return shutil.which("sockseek") or shutil.which("sldl")


_SOCKSEEK_LOCK = asyncio.Lock()  # один экземпляр — один логин в сети Soulseek


def _sockseek_config() -> Path:
    """Логин/пароль Soulseek для sockseek — в файле 0600 (а не в argv: его видно в списке процессов)."""
    path = CFG.state_dir / "sockseek.conf"
    text = f"username = {CFG.sldl_username}\npassword = {CFG.sldl_password}\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


async def soulseek_sockseek(req: TrackRequest, progress: Progress) -> Downloaded | None:
    exe = _sockseek_bin()
    if not exe or not CFG.sldl_username or not CFG.sldl_password or req.duration > LONG_SET:
        return None
    if req.artist and req.title and "," not in req.artist + req.title and "=" not in req.artist + req.title:
        inp = f"artist={req.artist}, title={req.title}"
        if req.duration:
            inp += f", length={int(req.duration)}"
    elif req.artist and req.title:  # запятая сломала бы формат "ключ=значение, ..." — ищем свободным текстом
        inp = f"{req.artist} - {req.title}".replace(",", " ")
    else:
        inp = req.query or req.title
    out = CFG.incoming_dir / "sockseek" / uuid.uuid4().hex[:12]
    out.mkdir(parents=True, exist_ok=True)
    fmts = "mp3,flac" if CFG.allow_flac_transcode else "mp3"
    cmd = [exe, "--song", "--input-type", "string", "--config", str(_sockseek_config()),
           "-o", str(out), "--name-format", "{artist( - )title|filename}",
           "--format", fmts, "--pref-format", "mp3", "--min-bitrate", "320", "--pref-min-bitrate", "320",
           "--length-tol", "5", "--strict-title", "--listen-port", str(CFG.sldl_listen_port),
           "--no-progress", "--no-write-index", "--search-timeout", str(CFG.soulseek_search_timeout * 1000),
           "--max-stale-time", "60000", "--fast-search", "--input", inp]
    await progress("Soulseek (sockseek): ищу у пиров…")
    async with _SOCKSEEK_LOCK:
        r = await run(cmd, timeout=CFG.soulseek_download_timeout + 120)
    files = [p for p in find_audio_files(out)]
    if r.rc != 0 and not files:
        log.info("sockseek rc=%s: %s", r.rc, (r.err or r.out)[-400:])
        shutil.rmtree(out, ignore_errors=True)
        return None
    if not files:
        shutil.rmtree(out, ignore_errors=True)
        return None
    f = max(files, key=lambda p: p.stat().st_size)
    if _variant_rejected(f.stem, req, "sockseek"):
        shutil.rmtree(out, ignore_errors=True)
        return None
    return Downloaded(f, "sockseek", f.suffix[1:].upper(), lossless_src=f.suffix.lower() == ".flac")


def _variant_rejected(found: str, req: TrackRequest, source: str) -> bool:
    """Нашлась ускоренная/замедленная версия, а её не просили — не берём."""
    v = unwanted_variant(found, f"{req.title} {req.query}")
    if v:
        log.info("%s: отклонён вариант «%s» (%s) для %s", source, found, v, req.label)
    return bool(v)


# ====================================================================== spotDL
_SPOTDL_SEM = asyncio.Semaphore(2)


async def spotdl(req: TrackRequest, progress: Progress) -> Downloaded | None:
    if not shutil.which("spotdl"):
        return None
    target = req.spotify_url or (f"{req.artist} - {req.title}" if req.artist else req.query or req.title)
    if not target or req.duration > LONG_SET:
        return None
    out = CFG.incoming_dir / "spotdl" / uuid.uuid4().hex[:12]
    out.mkdir(parents=True, exist_ok=True)
    if target.startswith("-"):  # spotdl не поддерживает "--", поэтому флаги в запросе просто не пропускаем
        target = target.lstrip("- ")
    if not target:
        return None
    cmd = ["spotdl", "download", target, "--output", str(out / "{artists} - {title}.{output-ext}"),
           "--format", "mp3", "--bitrate", "320k", "--print-errors", "--max-retries", "2",
           "--overwrite", "force", "--threads", "1", "--log-level", "ERROR",
           "--lyrics"]  # пустой список провайдеров: тексты ищет сам бот, а Genius на 429 вешал spotdl на 15 мин
    if CFG.spotify_client_id and CFG.spotify_client_secret:
        cmd += ["--use-official-api", "--client-id", CFG.spotify_client_id,
                "--client-secret", CFG.spotify_client_secret]
    cookies = CFG.state_dir / "youtube_cookies.txt"
    if cookies.exists():
        cmd += ["--cookie-file", str(cookies)]
    await progress("spotDL: ищу на YouTube Music…")
    async with _SPOTDL_SEM:  # YouTube Music режет частые запросы с одного адреса — не больше двух одновременно
        r = await run(cmd, timeout=300, cwd=out)
    files = find_audio_files(out)
    if not files:
        log.info("spotdl failed rc=%s: %s", r.rc, (r.err or r.out)[-400:])
        shutil.rmtree(out, ignore_errors=True)
        return None
    f = files[0]
    if _variant_rejected(f.stem, req, "spotdl"):
        shutil.rmtree(out, ignore_errors=True)
        return None
    if req.title and not req.spotify_url and similarity(req.title, f.stem) < 0.2:
        log.info("spotdl: подозрительный результат %s для %s", f.name, req.label)
    return Downloaded(f, "spotdl", "YouTube Music")


# ====================================================================== yt-dlp
async def ytdlp(req: TrackRequest, progress: Progress) -> Downloaded | None:
    if not shutil.which("yt-dlp"):
        return None
    targets: list[str] = []
    if req.direct_url:
        if not await is_public_url_resolved(req.direct_url):  # ссылки из плейлиста — проверяем перед загрузкой
            log.warning("yt-dlp: адрес не публичный, пропускаю: %s", req.direct_url[:200])
            return None
        targets.append(req.direct_url)
    else:
        q = req.search_text
        if not q:
            return None
        targets += [f"ytsearch1:{q} audio", f"scsearch1:{q}"]
    out = CFG.incoming_dir / "ytdlp" / uuid.uuid4().hex[:12]
    out.mkdir(parents=True, exist_ok=True)
    cookies = CFG.state_dir / "youtube_cookies.txt"
    for t in targets:
        await progress("yt-dlp: качаю " + ("по ссылке" if t.startswith("http") else t.split(":", 1)[0]) + "…")
        cmd = ["yt-dlp", "-f", "bestaudio/best", "--max-filesize", "400M", "-x", "--audio-format", "mp3", "--audio-quality", "320K",
               "--no-playlist", "--no-warnings", "--no-progress", "--restrict-filenames",
               "--match-filter", "duration < 14400", "--write-info-json",
               "-o", str(out / "%(id)s.%(ext)s"), "--", t]
        if cookies.exists():
            cmd[1:1] = ["--cookies", str(cookies)]
        r = await run(cmd, timeout=3600)
        files = find_audio_files(out)
        if files and not t.startswith("http") and _variant_rejected(_ytdlp_title(out), req, "yt-dlp"):
            for f in out.iterdir():  # поиск выдал ускоренную версию — пробуем следующий источник
                f.unlink(missing_ok=True)
            continue
        if files:
            return Downloaded(files[0], "yt-dlp", "SoundCloud" if t.startswith("scsearch") or "soundcloud" in t
                              else "YouTube")
        log.info("yt-dlp failed for %s: %s", t, r.err[-300:])
    shutil.rmtree(out, ignore_errors=True)
    return None


def _ytdlp_title(out: Path) -> str:
    """Название ролика из .info.json (имя файла — id)."""
    import json
    for j in out.glob("*.info.json"):
        try:
            return str(json.loads(j.read_text(encoding="utf-8")).get("title") or "")
        except (OSError, ValueError):
            continue
    return ""


# ====================================================================== каскад
# Downloaded.source -> имя шага каскада (чтобы после битого файла пропустить этот источник)
SOURCE_STEPS = {"soulseek": "soulseek_slskd", "sockseek": "soulseek_sockseek", "spotdl": "spotdl",
                "yt-dlp": "ytdlp"}


async def fetch(req: TrackRequest, progress: Progress = _noop,
                exclude: set[str] | frozenset = frozenset()) -> Downloaded | None:
    """exclude — имена шагов (SOURCE_STEPS), которые уже дали битый файл."""
    chain: list[Callable[[TrackRequest, Progress], Awaitable[Downloaded | None]]]
    if req.duration > LONG_SET or req.origin in ("soundcloud", "bandcamp") and req.direct_url and not req.artist:
        chain = [ytdlp]  # DJ-сеты и миксы есть только в оригинале
    elif req.origin in ("youtube", "soundcloud", "bandcamp", "url"):
        chain = [soulseek_slskd, soulseek_sockseek, ytdlp]  # сначала пробуем найти студийную версию
        if req.artist and req.title:
            chain.insert(2, spotdl)
    else:
        chain = [soulseek_slskd, soulseek_sockseek, spotdl, ytdlp]
    for step in chain:
        if step.__name__ in exclude:
            continue
        try:
            res = await step(req, progress)
        except Exception as e:  # noqa: BLE001 — один источник не должен ронять каскад
            log.exception("%s failed: %s", step.__name__, e)
            res = None
        if res:
            log.info("%s: %s via %s", req.label, res.path.name, res.source)
            return res
    return None


def cleanup_after(path: Path) -> None:
    """Удаляет временную папку именно этой загрузки (пустые папки соседних воркеров не трогаем)."""
    job_roots = [CFG.incoming_dir / x for x in ("sockseek", "spotdl", "ytdlp")] + [CFG.slskd_downloads_dir / "bot"]
    for parent in path.parents:
        if parent.parent in job_roots:
            shutil.rmtree(parent, ignore_errors=True)
            return
        if parent in (CFG.slskd_downloads_dir, CFG.incoming_dir):
            return
        if parent.parent == CFG.slskd_downloads_dir:  # старый slskd без батчей: downloads/<папка пира>/
            try:
                parent.rmdir()
            except OSError:
                pass
            return
