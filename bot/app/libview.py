"""Сортировка и фильтры медиатеки для Mini App (трек, альбом, исполнитель, жанр): чистые функции без I/O.

Строки — (трек, ссылка пользователя, когда добавлен). Сравнение по-русски: без регистра, ё = е; у исполнителей
не учитывается «The » в начале. Пустые значения (нет жанра/года/длительности) — всегда в конце списка.
"""
from __future__ import annotations

import json
import random
from typing import Any, Callable, Iterable

from .db import DB
from .util import artist_sort_key, collate

TRACK_SORTS = ("added", "title", "artist", "album", "genre", "year", "duration", "random")
GROUP_SORTS = ("name", "count", "added", "year")
DESC_BY_DEFAULT = {"added", "year", "count"}

Row = tuple[Any, str, int]  # (Track, link_path, added_at)


def genres_of(t: Any) -> list[str]:
    if getattr(t, "genres", None):
        try:
            g = json.loads(t.genres)
            if isinstance(g, list):
                return [str(x) for x in g if x]
        except ValueError:
            pass
    return [t.genre] if getattr(t, "genre", None) else []


def default_desc(sort: str) -> bool:
    return sort in DESC_BY_DEFAULT


def filter_rows(rows: Iterable[Row], *, q: str = "", genre: str = "") -> list[Row]:
    q, g = collate(q), collate(genre)
    out = []
    for row in rows:
        t = row[0]
        if q and q not in collate(f"{t.artist} {t.title} {t.album or ''}"):
            continue
        if g and g not in (collate(x) for x in genres_of(t)):
            continue
        out.append(row)
    return out


def _sorted(items: list, key: Callable, present: Callable, desc: bool, tie: Callable | None = None) -> list:
    """«Нет значения» — всегда в конце; при равных — tie по возрастанию (и при desc): две стабильные сортировки."""
    have = [x for x in items if present(x)]
    missing = [x for x in items if not present(x)]
    if tie is not None:
        have.sort(key=tie)
        missing.sort(key=tie)
    return sorted(have, key=key, reverse=desc) + missing


def sort_rows(rows: list[Row], sort: str, desc: bool | None = None, seed: int | None = None) -> list[Row]:
    if sort == "random":
        out = list(rows)
        random.Random(seed).shuffle(out)
        return out
    desc = default_desc(sort) if desc is None else desc

    def place(r: Row) -> tuple:  # внутри одинаковых: исполнитель → альбом → порядок файлов
        return artist_sort_key(r[0].artist), collate(DB.library_folders(r[1])[1] if r[1] else r[0].album), r[1] or ""
    keys: dict[str, tuple[Callable, Callable]] = {
        "added": (lambda r: r[2] or 0, lambda r: True),
        "title": (lambda r: collate(r[0].title), lambda r: True),
        "artist": (place, lambda r: True),
        "album": (lambda r: collate(r[0].album), lambda r: bool(r[0].album)),
        "genre": (lambda r: collate(r[0].genre), lambda r: bool(r[0].genre)),
        "year": (lambda r: r[0].year or 0, lambda r: bool(r[0].year)),
        "duration": (lambda r: r[0].duration or 0, lambda r: bool(r[0].duration)),
    }
    key, present = keys.get(sort, keys["added"])
    return _sorted(list(rows), key, present, desc, tie=place)


def group_rows(rows: list[Row], by: str) -> list[dict]:
    """Исполнители (by='artist') или альбомы (by='album') по папкам медиатеки, как их видит Jellyfin."""
    groups: dict[Any, list[Row]] = {}
    for r in rows:
        a, b = DB.library_folders(r[1])
        groups.setdefault(a if by == "artist" else (a, b), []).append(r)
    out = []
    for key, items in groups.items():
        tracks = [t for t, _, _ in items]
        a = key if by == "artist" else key[0]
        g = {"artist_key": a, "artist": DB.display_name(a, [t.artist for t in tracks]), "count": len(items),
             "added_at": max(r[2] or 0 for r in items), "year": max((t.year or 0 for t in tracks), default=0) or None,
             "rows": items}
        if by == "album":
            g["key"] = key[1]
            g["title"] = DB.display_name(key[1], [t.album or "" for t in tracks])
        out.append(g)
    return out


def sort_groups(groups: list[dict], sort: str, desc: bool | None = None, by: str = "artist") -> list[dict]:
    desc = default_desc(sort) if desc is None else desc

    def name(g: dict) -> tuple:
        return (artist_sort_key(g["artist"]), collate(g.get("title", "")))
    keys = {"name": (name, lambda g: True),
            "count": (lambda g: g["count"], lambda g: True),
            "added": (lambda g: g["added_at"], lambda g: True),
            "year": (lambda g: g["year"] or 0, lambda g: bool(g["year"]))}
    key, present = keys.get(sort, keys["name"])
    return _sorted(groups, key, present, desc, tie=name)


def genre_counts(rows: Iterable[Row]) -> list[dict]:
    """[{name, count}] по всем жанрам треков (у трека может быть несколько), частые первыми."""
    counts: dict[str, list] = {}
    for r in rows:
        for g in genres_of(r[0]):
            k = collate(g)
            if k not in counts:
                counts[k] = [g, 0]
            counts[k][1] += 1
    return sorted(({"name": n, "count": c} for n, c in counts.values()), key=lambda x: (-x["count"], collate(x["name"])))
