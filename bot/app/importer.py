"""Импорт медиатеки из файлов экспорта (Tune My Music, Exportify, свои списки): CSV и TXT.

Формат Tune My Music CSV (проверен на реальном экспорте Spotify, UTF-8 с BOM):
    Track name,Artist name,Album,Playlist name,Type,ISRC,Spotify - id
Колонки ищутся по смыслу названия, поэтому подходят и другие CSV (Exportify, Soundiiz, свои таблицы).
TXT — строка на трек: «Артист - Песня» (или «Песня - Артист»: порядок определяется по каталогу Deezer).
"""
from __future__ import annotations

import csv
import io
import logging
import re

from . import http
from .util import norm, split_artist_title

log = logging.getLogger(__name__)

MAX_ITEMS = 10000

# ключ -> варианты заголовков (в нижнем регистре, без пробелов по краям)
_COLS = {
    "title": ("track name", "track", "title", "song", "song name", "name", "название", "трек", "песня"),
    "artist": ("artist name", "artist name(s)", "artist", "artists", "artist(s)", "исполнитель", "артист"),
    "album": ("album", "album name", "альбом"),
    "isrc": ("isrc",),
    "duration": ("duration", "duration (ms)", "track duration (ms)", "length", "time", "длительность"),
    "spotify": ("spotify - id", "spotify id", "track uri", "spotify uri", "uri", "spotify url", "url"),
}


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 только с BOM — иначе cp1251 «угадывается» как UTF-16
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _map_header(header: list[str]) -> dict[str, int]:
    low = [h.strip().lower().lstrip("﻿") for h in header]
    found: dict[str, int] = {}
    for key, names in _COLS.items():
        for name in names:  # точные совпадения важнее частичных
            if name in low:
                found[key] = low.index(name)
                break
        else:
            for i, h in enumerate(low):
                if key in ("title", "artist") and any(n in h for n in names[:2]) and i not in found.values():
                    found[key] = i
                    break
    return found


def _duration(v: str) -> float:
    v = (v or "").strip()
    if not v:
        return 0.0
    if ":" in v:  # 3:45 или 1:02:03
        parts = [int(p) for p in v.split(":") if p.isdigit()]
        sec = 0
        for p in parts:
            sec = sec * 60 + p
        return float(sec)
    try:
        x = float(v.replace(",", "."))
    except ValueError:
        return 0.0
    return x / 1000 if x > 3600 else x  # миллисекунды (Exportify) или секунды


def _spotify_url(v: str) -> str:
    v = (v or "").strip()
    m = re.search(r"(?:track[:/])?([A-Za-z0-9]{22})$", v)
    return f"https://open.spotify.com/track/{m.group(1)}" if m else ""


def parse_csv(text: str) -> list[dict]:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    cols = _map_header(rows[0])
    if "title" not in cols or "artist" not in cols:
        raise ValueError("в CSV не нашлось колонок с названием трека и исполнителем")

    def cell(row: list[str], key: str) -> str:
        i = cols.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    out = []
    for row in rows[1:]:
        title, artist = cell(row, "title"), cell(row, "artist")
        if not title or not artist:
            continue
        # Exportify пишет нескольких артистов через запятую — для поиска нужен первый
        artist = artist.split(";")[0].strip()
        out.append({"artist": artist, "title": title, "album": cell(row, "album"), "isrc": cell(row, "isrc").upper(),
                    "duration": _duration(cell(row, "duration")), "spotify_url": _spotify_url(cell(row, "spotify"))})
    return out


_TXT_SKIP = re.compile(r"^\s*(#|//|track name|artist name|название|playlist:)", re.I)


def parse_txt(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("﻿").strip("-•*\t ")
        line = re.sub(r"^\d{1,4}[.)]\s+", "", line)  # «12. Artist - Title»
        if not line or _TXT_SKIP.match(line):
            continue
        if "\t" in line:
            a, t = (x.strip() for x in line.split("\t", 1))
        else:
            a, t = split_artist_title(line)
        if a and t:
            out.append({"artist": a, "title": t, "album": "", "isrc": "", "duration": 0.0, "spotify_url": ""})
    return out


async def _deezer_hits(artist: str, title: str) -> int:
    data = await http.get_json("https://api.deezer.com/search",
                               params={"q": f'artist:"{artist}" track:"{title}"', "limit": "1"})
    return int((data or {}).get("total") or 0)


async def fix_txt_order(items: list[dict]) -> bool:
    """Если строки записаны как «Песня - Артист», меняем поля местами. Решение — по 6 строкам и Deezer."""
    sample = items[:: max(1, len(items) // 6)][:6]
    direct = swapped = 0
    for it in sample:
        try:
            direct += bool(await _deezer_hits(it["artist"], it["title"]))
            swapped += bool(await _deezer_hits(it["title"], it["artist"]))
        except Exception:  # noqa: BLE001
            return False
    if swapped > direct:
        for it in items:
            it["artist"], it["title"] = it["title"], it["artist"]
        return True
    return False


def dedupe(items: list[dict]) -> list[dict]:
    """Убираем только буквальные повторы строк (тот же Spotify id или совпадение всех полей).
    Один и тот же ISRC в разных релизах (альбом / Deluxe / сингл) НЕ выбрасываем: такие строки пройдут очередь,
    а общий кэш сам отдаст уже скачанный файл, не скачивая второй раз. Разные версии песни («Levitating» и
    «Levitating (feat. DaBaby)») имеют разные ISRC и скачиваются отдельно."""
    seen: set[tuple] = set()
    out = []
    for it in items:
        k = ("sp", it["spotify_url"]) if it.get("spotify_url") else \
            ("row", norm(it["artist"]), norm(it["title"]), norm(it.get("album", "")), it.get("isrc", ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out[:MAX_ITEMS]


def same_recording_count(items: list[dict]) -> int:
    """Сколько строк — повторы уже встречавшейся записи (тот же ISRC в другом релизе)."""
    isrcs = [it["isrc"] for it in items if it.get("isrc")]
    return len(isrcs) - len(set(isrcs))


async def parse_file(data: bytes, filename: str) -> tuple[list[dict], str]:
    """-> (уникальные треки, описание формата)."""
    text = _decode(data)
    name = filename.lower()
    first = text.lstrip("﻿").split("\n", 1)[0]
    looks_csv = name.endswith((".csv", ".tsv")) or (first.count(",") >= 2 and _map_header(next(csv.reader([first]))))
    if looks_csv:
        items = parse_csv(text)
        fmt = "CSV"
    else:
        items = parse_txt(text)
        fmt = "TXT «Артист - Песня»"
        if items and await fix_txt_order(items):
            fmt = "TXT «Песня - Артист»"
    total = len(items)
    items = dedupe(items)
    dupes = total - len(items)
    return items, fmt + (f", повторяющихся строк пропущено: {dupes}" if dupes else "")


__all__ = ["parse_file", "parse_csv", "parse_txt", "dedupe", "norm"]
