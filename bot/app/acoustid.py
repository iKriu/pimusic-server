"""Опознание файла по звуку: отпечаток Chromaprint (fpcalc или ffmpeg -f chromaprint) -> AcoustID -> MusicBrainz.

Нужен бесплатный ключ ACOUSTID_API_KEY (https://acoustid.org/new-application). Без ключа или без fpcalc/
chromaprint в ffmpeg шаг просто пропускается.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import http
from .util import FFMPEG_LIMITS, PROBE_LIMITS, run

log = logging.getLogger(__name__)

API = "https://api.acoustid.org/v2/lookup"
MIN_SCORE = 0.8
LIMIT = http.RateLimiter(0.34)  # правила AcoustID: не больше 3 запросов в секунду


@dataclass
class Match:
    artist: str
    title: str
    album: str
    score: float
    mbid: str = ""


async def fingerprint(path: Path) -> tuple[int, str] | None:
    """(длительность, сжатый отпечаток base64)."""
    if shutil.which("fpcalc"):
        r = await run(["fpcalc", "-json", "-length", "120", str(path)], timeout=120, limits=FFMPEG_LIMITS,
                      max_stdout=1024 * 1024)
        if r.rc == 0:
            try:
                data = json.loads(r.out)
                return int(float(data["duration"])), str(data["fingerprint"])
            except (ValueError, KeyError, TypeError):
                pass
    # запасной путь: мультиплексор chromaprint в ffmpeg (есть, если ffmpeg собран с --enable-chromaprint)
    r = await run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-protocol_whitelist", "file",
                   "-i", str(path), "-t", "120", "-f", "chromaprint", "-fp_format", "base64", "-"], timeout=120,
                  limits=FFMPEG_LIMITS, max_stdout=1024 * 1024)
    fp = r.out.strip() if r.rc == 0 else ""
    if not fp:
        return None
    probe = await run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries", "format=duration",
                       "-of", "csv=p=0", str(path)], timeout=60, limits=PROBE_LIMITS)
    try:
        return int(float(probe.out.strip())), fp
    except ValueError:
        return None


def best_match(data: dict | None) -> Match | None:
    """Лучшая запись ответа AcoustID с оценкой ≥ MIN_SCORE."""
    best: Match | None = None
    for res in (data or {}).get("results") or []:
        score = float(res.get("score") or 0)
        if score < MIN_SCORE:
            continue
        for rec in res.get("recordings") or []:
            title = rec.get("title") or ""
            artists = rec.get("artists") or []
            artist = "".join(a.get("name", "") + (a.get("joinphrase") or "") for a in artists).strip()
            if not title or not artist:
                continue
            groups = rec.get("releasegroups") or []
            album = next((g.get("title", "") for g in groups if g.get("type") == "Album"),
                         groups[0].get("title", "") if groups else "")
            if best is None or score > best.score:
                best = Match(artist, title, album, score, rec.get("id") or "")
    return best


async def lookup(path: Path, api_key: str) -> Match | None:
    if not api_key:
        return None
    fp = await fingerprint(path)
    if not fp:
        log.info("AcoustID: нет отпечатка (нет fpcalc / chromaprint в ffmpeg) для %s", path.name)
        return None
    data = await http.get_json(API, params={"client": api_key, "meta": "recordings releasegroups",
                                            "duration": str(fp[0]), "fingerprint": fp[1]}, limiter=LIMIT)
    if data and data.get("status") != "ok":
        log.warning("AcoustID: %s", (data.get("error") or {}).get("message") or data)
        return None
    return best_match(data)
