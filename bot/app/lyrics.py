"""Тексты песен: LRCLIB (точный поиск по длительности) -> syncedlyrics (Musixmatch, NetEase, ...)."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from . import http
from .util import primary_artist, similarity, strip_feat

log = logging.getLogger(__name__)

LRCLIB = "https://lrclib.net/api"
_TS = re.compile(r"^\s*\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def is_synced(text: str | None) -> bool:
    if not text:
        return False
    return sum(1 for line in text.splitlines() if _TS.match(line)) >= 3


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """LRC -> [(миллисекунды, строка)] для тега SYLT."""
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        stamps = []
        rest = line
        while True:
            m = _TS.match(rest)
            if not m:
                break
            frac = m.group(3) or "0"
            ms = int(frac.ljust(3, "0")[:3])
            stamps.append(int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + ms)
            rest = rest[m.end():]
        for s in stamps:
            out.append((s, rest.strip()))
    return sorted(out)


def lrc_to_plain(text: str) -> str:
    lines = []
    for line in text.splitlines():
        while _TS.match(line):
            line = _TS.sub("", line, count=1)
        if not re.match(r"^\s*\[[a-z]+:.*\]\s*$", line, re.I):
            lines.append(line.strip())
    return "\n".join(lines).strip()


# ------------------------------------------------------------------ подбор и проверка текста
# Уточнения версии в названии («(Eurovision 2018 - Israel)», «- 2011 Remaster», «(Live)», «[Ремикс]»): текст у них
# тот же, а поиск по полному названию находит чужие или криво синхронизированные записи
_VERSION_WORD = (r"(?:re-?mix\w*|rmx|mix|edit|version|ver\.?|live|remaster\w*|mono|stereo|acoustic|unplugged|demo|"
                 r"extended|bonus|deluxe|eurovision|esc|cover|session\w*|recorded|"
                 r"soundtrack|ost|prod\.?|explicit|sped|slowed|reverb|ремикс\w*|версия|верси\w+|"
                 r"евровидени\w*|концерт\w*|акустик\w*|живьём|живьем|сингл|саундтрек|из\s+(?:к/ф|фильма|сериала)|"
                 r"\d{4})")
_BRACKET_VER = re.compile(r"\s*[\(\[][^\(\)\[\]]*\b" + _VERSION_WORD + r"\b[^\(\)\[\]]*[\)\]]", re.I)
_DASH_VER = re.compile(r"\s+[-–—]\s+[^-–—]*\b" + _VERSION_WORD + r"\b.*$", re.I)
# служебные строки NetEase: «作词 : …», «作曲 : …» — это не текст песни
_CREDIT = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:作词|作曲|编曲|制作人|混音|母带|和声|吉他|贝斯|鼓|录音|监制|出品)\s*[:：]")

SYNC_TAIL = 15.0        # последняя метка может быть позже конца трека не больше чем на столько секунд
SYNC_MIN_SHARE = 0.5    # ...и не раньше этой доли длительности (иначе это текст другой, короткой песни)
SYNC_FAST_GAP = 0.8     # строки чаще этого (сек) подряд — признак сбитой синхронизации
SYNC_FAST_SHARE = 0.3   # ...если таких промежутков больше этой доли
SEARCH_DUR_TOL = 5.0    # LRCLIB search: допуск по длительности записи


def search_title(title: str) -> str:
    """Название для поиска текста: без feat. и уточнений версии в скобках и после дефиса. Пустое — исходное."""
    t = strip_feat(title or "")
    for _ in range(3):  # вложенные/повторные уточнения: «Song (Live) [2011 Remaster]»
        t2 = _DASH_VER.sub("", _BRACKET_VER.sub("", t)).strip(" -–—_|")
        if t2 == t:
            break
        t = t2
    return re.sub(r"\s{2,}", " ", t).strip() or strip_feat(title or "").strip()


def clean_lrc(text: str) -> str:
    """Убирает служебные строки (авторы у NetEase) — в плеере они выглядят как первая строка песни."""
    return "\n".join(ln for ln in (text or "").splitlines() if not _CREDIT.match(ln)).strip()


def length_problem(text: str, duration: float, *, strict: bool = True) -> str:
    """Метки не сходятся с длиной трека — это текст другой песни ("" — сходятся или длина неизвестна).
    strict=False — только явный случай «текст длиннее трека»: «кончается до середины» бывает и у своего текста
    (длинный инструментальный хвост в extended/live-версиях), поэтому такой текст не удаляем, а только не берём."""
    if not duration or duration < 30:
        return ""
    lines = parse_lrc_lines(clean_lrc(text))
    words = [x for x in lines if x["text"]]
    if not words:
        return ""
    last = lines[-1]["t"]
    if last > duration + SYNC_TAIL:
        return f"последняя строка на {last:.0f} с, а трек {duration:.0f} с — текст другой песни"
    if strict and words[-1]["t"] < duration * SYNC_MIN_SHARE:
        return f"текст кончается на {words[-1]['t']:.0f} с из {duration:.0f} — похоже на текст другой песни"
    return ""


def sync_problem(text: str, duration: float, *, strict: bool = True) -> str:
    """Почему синхронный текст подозрителен ("" — всё в порядке). duration — длительность трека в секундах (0 —
    неизвестна: проверяется только синхронизация). strict=False — только уверенные признаки (см. length_problem)."""
    words = [x for x in parse_lrc_lines(clean_lrc(text)) if x["text"]]
    if len(words) < 3:
        return ""  # судить не по чему (is_synced такой текст и так не считает синхронным)
    why = length_problem(text, duration, strict=strict)
    if why:
        return why
    gaps = [b["t"] - a["t"] for a, b in zip(words, words[1:])]
    # Хвост из строк «пачкой» в последние секунды — частый дефект LRCLIB (досинхронизировали не до конца):
    # основная часть при этом верна, такой текст не бракуем. Сбитой считаем синхронизацию с пачками по всей песне.
    while gaps and gaps[-1] < 2 * SYNC_FAST_GAP:
        gaps.pop()
    if len(gaps) >= 10:
        fast = sum(1 for g in gaps if g < SYNC_FAST_GAP)
        if fast / len(gaps) > SYNC_FAST_SHARE:
            return f"сбитая синхронизация: {fast} из {len(gaps)} строк идут чаще {SYNC_FAST_GAP} с"
    return ""


def _ok_synced(text: str, duration: float, where: str, who: str) -> str:
    """Синхронный текст после проверки или "" (с записью в журнал, почему отброшен)."""
    if not text or not is_synced(text):
        return ""
    text = clean_lrc(text)
    why = sync_problem(text, duration)
    if why:
        log.info("текст %s отброшен (%s): %s", who, where, why)
        return ""
    return text


_HDR = {"Lrclib-Client": "PiMusic/1.0"}


async def _lrclib_get(artist: str, title: str, album: str, duration: float, raise_errors: bool) -> dict | None:
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(int(round(duration)))
    data = await http.get_json(f"{LRCLIB}/get", params=params, limiter=http.LRCLIB_LIMIT, headers=_HDR,
                               strict=raise_errors)
    return data if isinstance(data, dict) else None


async def _lrclib_search(artist: str, title: str, duration: float, who: str,
                         raise_errors: bool) -> tuple[str, str] | None:
    """Нечёткий поиск: только записи того же исполнителя, с похожим названием и (если известна) той же длины;
    синхронный текст ещё и проверяется по меткам."""
    data = await http.get_json(f"{LRCLIB}/search", params={"track_name": title, "artist_name": artist},
                               limiter=http.LRCLIB_LIMIT, headers=_HDR, strict=raise_errors)
    best: tuple[float, str, str] | None = None
    for r in data if isinstance(data, list) else []:
        if r.get("instrumental"):
            continue
        st = similarity(title, search_title(r.get("trackName") or ""))
        sa = similarity(artist, primary_artist(r.get("artistName") or ""))
        if st < 0.6 or sa < 0.5:
            continue
        sc = st * 2 + sa
        if duration and r.get("duration"):
            d = abs(float(r["duration"]) - duration)
            if d > SEARCH_DUR_TOL:
                continue
            sc += 0.5 if d <= 2 else 0
        elif st < 1 or sa < 1:  # длительность не сверить — только точное совпадение названия и исполнителя
            continue
        synced = _ok_synced(r.get("syncedLyrics") or "", duration, "LRCLIB search", who)
        plain = (r.get("plainLyrics") or "").strip()
        if r.get("syncedLyrics") and not synced and not (duration and r.get("duration")):
            plain = ""  # синхронный текст забракован, а длину записи не сверить — обычному тексту тоже не верим
        if not synced and not plain:
            continue
        sc += 0.5 if synced else 0
        if sc >= 2.2 and (best is None or sc > best[0]):
            best = (sc, synced, plain)
    return (best[1], best[2]) if best else None


def _syncedlyrics(query: str) -> str | None:
    """Только синхронный текст: у обычного из syncedlyrics нет ни названия, ни меток — его не проверить."""
    try:
        import syncedlyrics
    except ImportError:
        return None
    try:
        return syncedlyrics.search(query, synced_only=True)
    except Exception as e:  # noqa: BLE001 — провайдеры syncedlyrics нестабильны
        log.debug("syncedlyrics failed: %s", e)
        return None


async def fetch(artist: str, title: str, album: str = "", duration: float = 0.0, *,
                raise_errors: bool = False) -> tuple[str, str]:
    """Возвращает (synced_lrc, plain). Любое из значений может быть пустым: лучше без текста, чем чужой.
    raise_errors=True — сбой сети/LRCLIB/таймаут не выдаётся за «текста нет», а пробрасывается (http.FetchError,
    TimeoutError…): перезапрос по нему не удаляет старый текст.

    Порядок: LRCLIB get по исходному названию (точная запись альбома) → LRCLIB get по очищенному названию →
    LRCLIB search → syncedlyrics (Musixmatch, NetEase…). Каждый синхронный текст сверяется с длительностью трека
    и проверяется на сбитую синхронизацию (sync_problem)."""
    a, t = primary_artist(artist), strip_feat(title)
    q = search_title(t)
    who = f"«{artist} — {title}»"
    plain = ""
    try:
        attempts = [(t, album)] + ([(q, "")] if q.lower() != t.lower() else [])
        for name, alb in attempts:
            data = await _lrclib_get(a, name, alb, duration, raise_errors)
            if not data or data.get("instrumental"):
                continue
            raw = data.get("syncedLyrics") or ""
            synced = _ok_synced(raw, duration, "LRCLIB get", who)
            if synced:
                return synced, (data.get("plainLyrics") or "").strip() or lrc_to_plain(synced)
            # запись нашлась точно (исполнитель, название, длина ±2 с): её обычный текст берём, даже если метки
            # сбиты; но если метки выходят за длину трека — в записи текст другой песни, не берём ничего
            if not plain and not (raw and is_synced(raw) and length_problem(raw, duration, strict=False)):
                plain = (data.get("plainLyrics") or "").strip()
        res = await _lrclib_search(a, q, duration, who, raise_errors)
        if res and res[0]:
            return res[0], res[1] or plain or lrc_to_plain(res[0])
        if res and not plain:
            plain = res[1]
        if duration:  # без длительности текст из syncedlyrics не проверить
            sl = await asyncio.wait_for(asyncio.to_thread(_syncedlyrics, f"{q} {a}"), timeout=45)
            synced = _ok_synced(sl or "", duration, "syncedlyrics", who)
            if synced:
                return synced, plain or lrc_to_plain(synced)
        return "", plain
    except asyncio.TimeoutError:
        if raise_errors:
            raise
        return "", plain
    except Exception as e:  # noqa: BLE001
        if raise_errors:
            raise
        log.warning("lyrics fetch failed for %s - %s: %s", artist, title, e)
        return "", plain


# ------------------------------------------------------------------ локальный текст для плеера (/api/lyrics)
_OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]\s*$", re.I)
_META = re.compile(r"^\s*\[[a-z]+:.*\]\s*$", re.I)
_LOCAL_CACHE: "dict[tuple, dict]" = {}
_LOCAL_CACHE_MAX = 512


def parse_lrc_lines(text: str) -> list[dict]:
    """LRC -> [{"t": секунды, "text": строка}] по времени. Несколько меток на строку, [offset:±мс]
    (положительный offset — текст раньше)."""
    offset = 0
    out: list[tuple[float, int, str]] = []
    for n, line in enumerate(text.splitlines()):
        m = _OFFSET.match(line)
        if m:
            offset = int(m.group(1))
            continue
        stamps, rest = [], line
        while True:
            mm = _TS.match(rest)
            if not mm:
                break
            frac = mm.group(3) or "0"
            stamps.append(int(mm.group(1)) * 60 + int(mm.group(2)) + int(frac.ljust(3, "0")[:3]) / 1000)
            rest = rest[mm.end():]
        for s in stamps:
            out.append((s, n, rest.strip()))
    out.sort()
    return [{"t": round(max(0.0, s - offset / 1000), 3), "text": t} for s, _, t in out]


def _uslt(path: Path) -> str | None:
    try:
        from mutagen.id3 import ID3
        tags = ID3(str(path))
    except Exception:  # noqa: BLE001 — нет тегов / не mp3
        return None
    for frame in tags.getall("USLT"):
        if frame.text and frame.text.strip():
            return frame.text
    return None


def _shape(text: str, source: str) -> dict:
    if is_synced(text):
        lines = parse_lrc_lines(text)
        return {"synced": lines or None, "plain": lrc_to_plain(text) or None, "source": source}
    plain = "\n".join(ln for ln in text.splitlines() if not _META.match(ln)).strip()
    return {"synced": None, "plain": plain or None, "source": source if plain else None}


def read_local(track_id: int, paths: "list[str | None]") -> dict:
    """Текст трека для плеера: .lrc / .txt рядом с файлом (ссылка пользователя, затем общий кэш), иначе USLT.
    Кэш в памяти по (track_id, пути, mtime). Вызывать в потоке (файловый I/O)."""
    files = [Path(p) for p in paths if p]
    cands: list[tuple[Path, str]] = []
    for f in files:
        cands += [(f.with_suffix(".lrc"), "lrc")]
    for f in files:
        cands += [(f.with_suffix(".txt"), "lrc")]
    cands += [(f, "tags") for f in files]
    stamp = []
    for p, _ in cands:
        try:
            stamp.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            stamp.append((str(p), None))
    key = (track_id, tuple(stamp))
    hit = _LOCAL_CACHE.get(key)
    if hit is not None:
        return hit
    res: dict = {"synced": None, "plain": None, "source": None}
    for (p, source), (_, mtime) in zip(cands, stamp):
        if mtime is None:
            continue
        try:
            text = _uslt(p) if source == "tags" else p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if text and text.strip():
            res = _shape(text, source)
            if res["synced"] or res["plain"]:
                break
    if len(_LOCAL_CACHE) >= _LOCAL_CACHE_MAX:
        _LOCAL_CACHE.clear()
    _LOCAL_CACHE[key] = res
    return res


__all__ = ["fetch", "parse_lrc", "lrc_to_plain", "is_synced", "parse_lrc_lines", "read_local", "search_title",
           "clean_lrc", "sync_problem", "length_problem"]
