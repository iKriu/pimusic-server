"""Общая aiohttp-сессия и вежливые лимитеры запросов."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .config import CFG

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


def session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40, connect=15),
            headers={"User-Agent": CFG.user_agent},
            trust_env=True,  # HTTPS_PROXY/NO_PROXY из окружения (если сервер выходит в интернет через прокси)
        )
    return _session


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last = time.monotonic()


MB_LIMIT = RateLimiter(1.1)       # MusicBrainz: не чаще 1 запроса/с
ITUNES_LIMIT = RateLimiter(3.1)   # iTunes Search: ~20 запросов/мин
LASTFM_LIMIT = RateLimiter(0.25)
LRCLIB_LIMIT = RateLimiter(0.3)


class FetchError(RuntimeError):
    """Сервис не ответил (сеть, таймаут, 429/5xx после всех попыток) — в отличие от «ничего не найдено»."""


async def get_json(url: str, *, params: dict[str, Any] | None = None, limiter: RateLimiter | None = None,
                   headers: dict[str, str] | None = None, retries: int = 5, strict: bool = False) -> Any | None:
    """JSON или None. strict=True: сбой (сеть/таймаут/429/5xx после всех попыток) — FetchError, а None
    остаётся только для настоящего «нет данных» (404 и прочие 4xx) — фоновым проходам важна разница."""
    error = ""
    for attempt in range(retries):
        if limiter:
            await limiter.wait()
        try:
            async with session().get(url, params=params, headers=headers) as r:
                if r.status == 404:
                    return None
                if r.status in (429, 503):
                    error = f"HTTP {r.status}"
                    await asyncio.sleep(min(2 ** attempt, 20))  # MusicBrainz отвечает 503 при >1 запроса/с
                    continue
                if r.status >= 500 and strict:
                    error = f"HTTP {r.status}"
                    await asyncio.sleep(1 + attempt)
                    continue
                if r.status >= 400:
                    log.debug("GET %s -> %s", url, r.status)
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.debug("GET %s failed: %s", url, e)
            error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1 + attempt)
    if strict:
        raise FetchError(error or "нет ответа")
    return None


MAX_REDIRECTS = 5


async def get_bytes(url: str, *, limit: int = 15_000_000) -> bytes | None:
    """Скачать по адресу из внешних данных (обложки): каждый адрес, включая переходы по редиректам, должен
    резолвиться только в публичные IP — иначе отказ (SSRF во внутреннюю сеть)."""
    from urllib.parse import urljoin

    from .util import is_public_url_resolved
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not await is_public_url_resolved(url):
                log.info("get_bytes: адрес не публичный, пропускаю: %s", url[:200])
                return None
            async with session().get(url, allow_redirects=False) as r:
                if r.status in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if not loc:
                        return None
                    url = urljoin(url, loc)
                    continue
                if r.status >= 400:
                    return None
                # content.read(n) возвращает только то, что уже в буфере — читаем до конца потока кусками
                buf = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > limit:
                        return None
                return bytes(buf)
        return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
