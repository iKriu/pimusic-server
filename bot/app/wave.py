"""/wave — «Моя Волна»: похожие треки через Last.fm track.getSimilar (запасной путь — Deezer), загрузка новинок."""
from __future__ import annotations

import logging
import math
import random
import time

from . import http
from .config import CFG
from .db import DB, User
from .jellyfin import Jellyfin
from .sources import TrackRequest
from .util import match_key, norm

log = logging.getLogger(__name__)

LASTFM = "https://ws.audioscrobbler.com/2.0/"


DEEZER = "https://api.deezer.com"
_lastfm_blocked_until = 0.0  # Last.fm API бывает недоступен по IP/региону (403, error 11) — тогда Deezer


async def _lastfm_similar(artist: str, title: str, limit: int) -> list[dict] | None:
    """None — Last.fm недоступен (ключ/блокировка), [] — доступен, но похожих нет."""
    global _lastfm_blocked_until
    if not CFG.lastfm_api_key or time.time() < _lastfm_blocked_until:
        return None
    data = await http.get_json(LASTFM, params={
        "method": "track.getsimilar", "artist": artist, "track": title, "autocorrect": "1",
        "limit": str(limit), "api_key": CFG.lastfm_api_key, "format": "json",
    }, limiter=http.LASTFM_LIMIT)
    if data is None or (isinstance(data, dict) and data.get("error") in (10, 11, 26, 29)):
        _lastfm_blocked_until = time.time() + 3600
        log.warning("Last.fm API недоступен — «Моя Волна» использует Deezer (повтор через час)")
        return None
    if "error" in data:
        return []
    tracks = (data.get("similartracks") or {}).get("track") or []
    return tracks if isinstance(tracks, list) else [tracks]


async def _deezer_similar(artist: str, title: str, limit: int) -> list[dict]:
    """Похожие через Deezer: трек-семя → связанные артисты → их топ-треки (формат как у Last.fm)."""
    q = f'artist:"{artist}" track:"{title}"'
    found = await http.get_json(f"{DEEZER}/search", params={"q": q, "limit": "5"})
    items = (found or {}).get("data") or []
    if not items:
        found = await http.get_json(f"{DEEZER}/search", params={"q": f"{artist} {title}", "limit": "5"})
        items = (found or {}).get("data") or []
    seed = next((t for t in items if norm(t.get("artist", {}).get("name", "")) == norm(artist)), items[0] if items else None)
    if not seed:
        return []
    related = await http.get_json(f"{DEEZER}/artist/{seed['artist']['id']}/related", params={"limit": "12"})
    artists = ((related or {}).get("data") or [])[:12]
    out: list[dict] = []
    for rank, a in enumerate(artists):
        top = await http.get_json(f"{DEEZER}/artist/{a['id']}/top", params={"limit": "4"})
        for t in (top or {}).get("data") or []:
            out.append({"name": t.get("title_short") or t.get("title", ""), "artist": {"name": a.get("name", "")},
                        "match": round(1.0 - rank / (len(artists) + 1), 3), "duration": t.get("duration") or 0})
        if len(out) >= limit:
            break
    return out[:limit]


async def similar(artist: str, title: str, limit: int = 30) -> list[dict]:
    res = await _lastfm_similar(artist, title, limit)
    if res is None:
        try:
            res = await _deezer_similar(artist, title, limit)
        except Exception as e:  # noqa: BLE001
            log.warning("Deezer similar failed: %s", e)
            res = []
    return res


async def seeds(jf: Jellyfin, user: User, n: int = 8) -> list[dict]:
    """Семена: лайки + самые прослушиваемые, с весами."""
    items = await jf.audio_items(user.jf_user_id)
    scored = []
    for it in items:
        ud = it.get("UserData") or {}
        w = (5.0 if ud.get("IsFavorite") else 0.0) + math.log1p(ud.get("PlayCount") or 0) * 2
        if w > 0:
            scored.append((w, it))
    if not scored:  # нет ни лайков, ни прослушиваний — берём случайные треки медиатеки
        scored = [(1.0, it) for it in items]
    if not scored:
        return []
    picked: list[dict] = []
    pool = scored[:]
    while pool and len(picked) < n:
        w = [s for s, _ in pool]
        idx = random.choices(range(len(pool)), weights=w, k=1)[0]
        picked.append(pool.pop(idx)[1])
    return picked


async def build(db: DB, jf: Jellyfin, user: User, size: int | None = None) -> tuple[list[TrackRequest], list[str]]:
    """Возвращает (что качать, на основе каких треков)."""
    size = size or CFG.wave_size
    seed_items = await seeds(jf, user)
    owned = db.user_track_keys(user.login)
    for it in await jf.audio_items(user.jf_user_id):
        owned.add(match_key((it.get("Artists") or [it.get("AlbumArtist") or ""])[0], it.get("Name", "")))
    seen = db.wave_seen(user.login)
    candidates: dict[str, tuple[float, TrackRequest]] = {}
    seed_names = []
    seed_artists = {((it.get("Artists") or [""])[0]).lower() for it in seed_items}
    for it in seed_items:
        artist = (it.get("Artists") or [it.get("AlbumArtist") or ""])[0]
        title = it.get("Name", "")
        if not artist or not title:
            continue
        seed_names.append(f"{artist} — {title}")
        for t in await similar(artist, title):
            a = (t.get("artist") or {}).get("name", "")
            name = t.get("name", "")
            if not a or not name:
                continue
            key = match_key(a, name)
            if key in owned or key in seen:
                continue
            score = float(t.get("match") or 0)
            if a.lower() in seed_artists:
                score *= 0.6  # больше новых артистов, меньше «ещё того же»
            prev = candidates.get(key)
            dur = float(t.get("duration") or 0)
            req = TrackRequest(artist=a, title=name, duration=dur if dur > 30 else 0, origin="wave")
            candidates[key] = ((prev[0] if prev else 0) + score, req)
    ranked = sorted(candidates.values(), key=lambda x: -x[0])
    # не больше двух треков одного артиста в одной волне
    out: list[TrackRequest] = []
    per_artist: dict[str, int] = {}
    for _, req in ranked:
        k = req.artist.lower()
        if per_artist.get(k, 0) >= 2:
            continue
        per_artist[k] = per_artist.get(k, 0) + 1
        out.append(req)
        if len(out) >= size:
            break
    return out, seed_names
