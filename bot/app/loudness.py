"""Громкость треков для нормализации в плеере (ReplayGain): теги REPLAYGAIN_* или анализ ffmpeg EBU R128.

Цель −14 LUFS: gain = −14 − I (интегральная громкость), peak — true peak (линейный). Анализ идёт в фоне
по одному треку с низким приоритетом (nice). Если в ffmpeg нет фильтра ebur128 — пробуем loudnorm,
нет и его (или ffmpeg) — анализ выключается до перезапуска, плеер просто не нормализует.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from .util import FFMPEG_LIMITS, run

log = logging.getLogger(__name__)

TARGET_LUFS = -14.0
RG_REFERENCE_LUFS = -18.0  # опорный уровень ReplayGain 2.0 в тегах
GAIN_RANGE = (-30.0, 20.0)
ANALYZE_TIMEOUT = 600

_GAIN_RE = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*(?:db)?\s*$", re.I)
_I_RE = re.compile(r"Integrated loudness:.*?\bI:\s*([+-]?\d+(?:\.\d+)?|-?inf)\s*LUFS", re.S)
_TP_RE = re.compile(r"True peak:.*?\bPeak:\s*([+-]?\d+(?:\.\d+)?|-?inf)\s*dBFS", re.S)

class Transient(RuntimeError):
    """ffmpeg не дошёл до конца (таймаут, убит по памяти/сигналом) — стоит повторить позже."""


def _check(r: Any) -> None:
    if r.rc == -9 or r.rc < 0 or r.rc in (137, 139, 143):
        raise Transient(f"ffmpeg rc={r.rc}: {(r.err or '')[-120:]}")


_filters: str | None = None   # 'ebur128' / 'loudnorm' / '' (нечем) — определяется один раз


def _clamp_gain(g: float) -> float:
    return round(max(GAIN_RANGE[0], min(GAIN_RANGE[1], g)), 2)


def _clamp_peak(p: float) -> float | None:
    return round(min(p, 4.0), 4) if p > 0 else None


def from_tags(path: Path) -> tuple[float, float | None] | None:
    """REPLAYGAIN_TRACK_GAIN / _PEAK из тегов (ID3 TXXX, Vorbis/FLAC, MP4 freeform) через mutagen."""
    tags = None
    try:
        import mutagen
        f = mutagen.File(str(path))
        tags = f.tags if f is not None else None
    except Exception:  # noqa: BLE001 — битый/нераспознанный файл: попробуем голый ID3, иначе ffmpeg
        pass
    if not tags:
        try:
            from mutagen.id3 import ID3
            tags = ID3(str(path))
        except Exception:  # noqa: BLE001
            return None
    vals: dict[str, str] = {}
    try:
        items = tags.items()
    except Exception:  # noqa: BLE001
        return None
    for k, v in items:
        key = str(k).lower()
        if key.startswith("txxx:"):
            key = key[5:]
        key = key.split(":")[-1] if key.startswith("----") else key
        if key in ("replaygain_track_gain", "replaygain_track_peak"):
            text = getattr(v, "text", v)
            if isinstance(text, (list, tuple)):
                text = text[0] if text else ""
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            vals[key] = str(text)
    m = _GAIN_RE.match(vals.get("replaygain_track_gain", ""))
    if not m:
        return None
    # ReplayGain 2.0 в тегах считается от −18 LUFS, наша цель −14: сдвигаем на разницу опорных уровней
    gain = _clamp_gain(float(m.group(1).replace(",", ".")) + (TARGET_LUFS - RG_REFERENCE_LUFS))
    peak = None
    try:
        peak = _clamp_peak(float(vals.get("replaygain_track_peak", "").replace(",", ".")))
    except ValueError:
        pass
    return gain, peak


def _cmd(args: list[str]) -> list[str]:
    nice = shutil.which("nice")
    base = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-protocol_whitelist", "file", *args]
    return [nice, "-n", "19", *base] if nice else base


async def _detect() -> str:
    global _filters
    if _filters is None:
        r = await run(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
        if r.rc != 0:
            log.warning("ffmpeg недоступен — громкость треков не считается: %s", (r.err or r.out)[-200:])
            _filters = ""
        elif re.search(r"\bebur128\b", r.out):
            _filters = "ebur128"
        elif re.search(r"\bloudnorm\b", r.out):
            _filters = "loudnorm"
        else:
            log.warning("в ffmpeg нет фильтров ebur128/loudnorm — громкость треков не считается")
            _filters = ""
    return _filters


def parse_ebur128(text: str) -> tuple[float, float | None] | None:
    """Итог ffmpeg ebur128=peak=true (stderr): «I: -9.1 LUFS» и «True peak: … Peak: -0.3 dBFS»."""
    tail = text[text.rfind("Summary:"):] if "Summary:" in text else text
    m = _I_RE.search(tail)
    if not m or "inf" in m.group(1):
        return None
    i = float(m.group(1))
    peak = None
    tp = _TP_RE.search(tail)
    if tp and "inf" not in tp.group(1):
        peak = _clamp_peak(10 ** (float(tp.group(1)) / 20))
    return _clamp_gain(TARGET_LUFS - i), peak


def parse_loudnorm(text: str) -> tuple[float, float | None] | None:
    """JSON-блок loudnorm print_format=json: input_i, input_tp."""
    start, end = text.rfind("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        i = float(data["input_i"])
    except (ValueError, KeyError, TypeError):
        return None
    if i == float("-inf") or i != i:
        return None
    peak = None
    try:
        peak = _clamp_peak(10 ** (float(data.get("input_tp")) / 20))
    except (TypeError, ValueError, OverflowError):
        pass
    return _clamp_gain(TARGET_LUFS - i), peak


async def analyze(path: Path) -> tuple[float, float | None] | None:
    """(gain дБ, peak) или None (файла нет / анализ не удался / нечем считать)."""
    if not await asyncio.to_thread(path.is_file):
        return None
    tagged = await asyncio.to_thread(from_tags, path)
    if tagged:
        return tagged
    mode = await _detect()
    if mode == "ebur128":
        r = await run(_cmd(["-i", str(path), "-map", "a:0", "-filter:a", "ebur128=peak=true:framelog=verbose",
                            "-f", "null", "-"]), timeout=ANALYZE_TIMEOUT, limits=FFMPEG_LIMITS)
        _check(r)
        return parse_ebur128(r.err) if r.rc == 0 else None
    if mode == "loudnorm":
        r = await run(_cmd(["-i", str(path), "-map", "a:0", "-filter:a", "loudnorm=print_format=json",
                            "-f", "null", "-"]), timeout=ANALYZE_TIMEOUT, limits=FFMPEG_LIMITS)
        _check(r)
        return parse_loudnorm(r.err) if r.rc == 0 else None
    return None


def available() -> bool | None:
    """None — ещё не проверяли; False — считать нечем."""
    return None if _filters is None else bool(_filters)
