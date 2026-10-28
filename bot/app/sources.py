"""Разбор того, что прислал пользователь: текст, ссылки Spotify / YouTube / SoundCloud / Bandcamp."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import http
from .config import CFG
from .util import clean_title, is_public_url, is_public_url_resolved, run, split_artist_title

log = logging.getLogger(__name__)

SPOTIFY_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?|spotify:)(track|album|playlist|artist)[/:]([A-Za-z0-9]{22})")
URL_RE = re.compile(r"https?://\S+")
LONG_SET = 15 * 60  # длиннее 15 минут — DJ-сет/микс, сразу качаем как есть


@dataclass
class TrackRequest:
    artist: str = ""
    title: str = ""
    album: str = ""
    duration: float = 0.0
    isrc: str = ""
    cover_url: str = ""
    track_no: int = 0
    year: str = ""
    query: str = ""                 # свободный текст, если не удалось разделить
    spotify_url: str = ""
    direct_url: str = ""            # ссылка для yt-dlp
    origin: str = "text"            # text / spotify / youtube / soundcloud / bandcamp / url / wave
    duration_from_mb: bool = False  # длительность взята из MusicBrainz (может быть другой редакцией)
    preview: str = ""               # 30-секундный MP3-фрагмент Deezer (только для прослушивания в Mini App)
    redownload: int = 0             # id трека кэша, файл которого заменить («Перекачать трек»)

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        return self.title or self.query or self.direct_url

    @property
    def search_text(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.query or self.title


@dataclass
class Resolved:
    kind: str                       # track / album / playlist / set
    title: str
    tracks: list[TrackRequest] = field(default_factory=list)
    error: str = ""
    note: str = ""                  # предупреждение пользователю (например, Spotify отдал только 100 треков)
    from_link: bool = False         # разобрана ссылка (а не список строк) — для плейлиста создаётся плейлист бота


SPOTIFY_EMBED_CAP = 100
SPOTIFY_CAP_NOTE = ("⚠️ Spotify без входа отдаёт только первые 100 треков плейлиста. Чтобы забрать весь: "
                    "экспортируйте плейлист в CSV через Tune My Music (tunemymusic.com) или Exportify и отправьте "
                    "файл сюда — уже скачанные треки добавятся мгновенно, плейлист создастся автоматически.")
SPOTDL_PLAYLIST_TIMEOUT = 120   # большой плейлист с рабочими ключами читается за минуту
SPOTDL_PLAYLIST_BACKOFF = 6 * 3600
_spotdl_playlists_blocked_until = 0.0  # ключи без OAuth не читают плейлисты (401/403) — не ждём spotdl зря


def classify(text: str) -> str:
    t = text.strip()
    if SPOTIFY_RE.search(t):
        return "spotify"
    m = URL_RE.search(t)
    if not m:
        return "text"
    u = m.group(0).lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "soundcloud.com" in u:
        return "soundcloud"
    if "bandcamp.com" in u:
        return "bandcamp"
    return "url"


# ------------------------------------------------------------------ Spotify
async def _spotdl_save(url: str) -> list[TrackRequest]:
    global _spotdl_playlists_blocked_until
    playlist = "/playlist/" in url
    if playlist and time.time() < _spotdl_playlists_blocked_until:
        return []  # недавно не смог — сразу публичная embed-страница
    out = Path(CFG.incoming_dir) / "work" / f"save_{abs(hash(url))}.spotdl"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["spotdl", "save", url, "--save-file", str(out), "--log-level", "ERROR"]
    if CFG.spotify_client_id and CFG.spotify_client_secret:
        cmd += ["--client-id", CFG.spotify_client_id, "--client-secret", CFG.spotify_client_secret]
    r = await run(cmd, timeout=SPOTDL_PLAYLIST_TIMEOUT if playlist else 300)
    if r.rc != 0 or not out.exists():
        log.info("spotdl save failed: %s", (r.err or r.out)[-400:])
        out.unlink(missing_ok=True)
        if playlist:
            _spotdl_playlists_blocked_until = time.time() + SPOTDL_PLAYLIST_BACKOFF
            log.warning("spotdl не читает плейлисты Spotify — %d ч беру публичную страницу (до 100 треков)",
                        SPOTDL_PLAYLIST_BACKOFF // 3600)
        return []
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except ValueError:
        return []
    finally:
        out.unlink(missing_ok=True)
    res = []
    for s in data:
        artists = s.get("artists") or [s.get("artist", "")]
        res.append(TrackRequest(
            artist=", ".join(a for a in artists if a) if len(artists) > 1 else (artists[0] if artists else ""),
            title=s.get("name", ""), album=s.get("album_name", ""), duration=float(s.get("duration") or 0),
            isrc=s.get("isrc") or "", cover_url=s.get("cover_url") or "", track_no=int(s.get("track_number") or 0),
            year=str(s.get("year") or ""), spotify_url=s.get("url") or "", origin="spotify",
        ))
    return res


async def _spotify_embed(kind: str, sid: str) -> tuple[str, list[TrackRequest]]:
    """Без ключей API: публичная embed-страница Spotify содержит JSON со списком треков."""
    try:
        async with http.session().get(f"https://open.spotify.com/embed/{kind}/{sid}",
                                      headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Firefox/128.0"}) as r:
            html = await r.text()
    except Exception as e:  # noqa: BLE001
        log.info("spotify embed failed: %s", e)
        return "", []
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.S)
    if not m:
        return "", []
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError):
        return "", []
    title = entity.get("name") or entity.get("title") or ""
    items = entity.get("trackList") or ([entity] if kind == "track" else [])
    res = []
    for t in items:
        artist = t.get("subtitle") or ", ".join(a.get("name", "") for a in entity.get("artists") or [])
        res.append(TrackRequest(
            artist=artist.replace(" ", " "), title=t.get("title") or t.get("name") or "",
            album=title if kind == "album" else "", duration=(t.get("duration") or 0) / 1000,
            spotify_url=f"https://open.spotify.com/track/{(t.get('uri') or ':').split(':')[-1]}",
            origin="spotify", track_no=0,
        ))
    return title, res


async def resolve_spotify(text: str) -> Resolved:
    m = SPOTIFY_RE.search(text)
    assert m
    kind, sid = m.group(1), m.group(2)
    url = f"https://open.spotify.com/{kind}/{sid}"
    if kind == "artist":
        return Resolved("playlist", "", error="Ссылки на артистов не поддерживаются — пришлите альбом или плейлист.")
    tracks = await _spotdl_save(url)
    title = ""
    note = ""
    if not tracks:
        title, tracks = await _spotify_embed(kind, sid)
        if kind == "playlist" and len(tracks) == SPOTIFY_EMBED_CAP:
            note = SPOTIFY_CAP_NOTE  # embed-страница отдаёт не больше 100 и не сообщает, сколько всего
    elif kind == "album":
        title = tracks[0].album
    elif kind == "playlist":
        title, _ = await _spotify_embed(kind, sid)
    if not tracks:
        return Resolved(kind, "", error="Не удалось прочитать ссылку Spotify (приватный плейлист?).")
    if kind == "track":
        title = tracks[0].label
    return Resolved(kind, title or kind, tracks[: CFG.max_playlist_tracks], note=note, from_link=True)


# ------------------------------------------------------------------ yt-dlp (YouTube/SoundCloud/Bandcamp/прочее)
def _req_from_entry(e: dict, origin: str) -> TrackRequest:
    title = e.get("track") or e.get("title") or ""
    artist = e.get("artist") or e.get("creator") or ""
    if not artist:
        a, t = split_artist_title(clean_title(title))
        if a:
            artist, title = a, t
        else:
            artist = re.sub(r"\s*-\s*Topic$", "", e.get("uploader") or e.get("channel") or "")
    if artist:
        artist = artist.split(",")[0].strip() if e.get("artists") is None else ", ".join(e["artists"][:3])
    url = e.get("webpage_url") or e.get("url") or ""
    if url and not url.startswith("http") and origin == "youtube":
        url = f"https://www.youtube.com/watch?v={url}"
    return TrackRequest(artist=artist, title=clean_title(title), album=e.get("album") or "",
                        duration=float(e.get("duration") or 0), direct_url=url, origin=origin,
                        year=str(e.get("release_year") or ""))


async def resolve_url(text: str, origin: str) -> Resolved:
    url = URL_RE.search(text).group(0)  # type: ignore[union-attr]
    if not await is_public_url_resolved(url):  # имя должно резолвиться только в публичные адреса
        return Resolved("track", url, error="Эта ссылка не поддерживается (нужен публичный http/https-адрес).")
    r = await run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings", "--playlist-end",
                   str(CFG.max_playlist_tracks), "--", url], timeout=180)
    if r.rc != 0:
        log.info("yt-dlp resolve failed for %s: %s", url, r.err.strip()[-300:])
        return Resolved("track", url, error="Не удалось открыть ссылку (видео недоступно или требует входа).")
    try:
        data = json.loads(r.out)
    except ValueError:
        return Resolved("track", url, error="yt-dlp вернул некорректный ответ")
    if data.get("_type") == "playlist" or data.get("entries"):
        entries = [e for e in data.get("entries") or [] if e]
        tracks = [t for t in (_req_from_entry(e, origin) for e in entries)
                  if not t.direct_url or is_public_url(t.direct_url)]
        # во flat-режиме часто нет длительности/артиста — это нормально, уточним при скачивании
        return Resolved("playlist", data.get("title") or url, tracks, from_link=True)
    req = _req_from_entry(data, origin)
    if req.direct_url and req.direct_url != url and not await is_public_url_resolved(req.direct_url):
        return Resolved("track", url, error="Эта ссылка не поддерживается (нужен публичный http/https-адрес).")
    kind = "set" if req.duration > LONG_SET else "track"
    return Resolved(kind, req.label, [req], from_link=True)


async def resolve(text: str) -> Resolved:
    kind = classify(text)
    if kind == "spotify":
        return await resolve_spotify(text)
    if kind != "text":
        return await resolve_url(text, kind)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tracks = []
    for ln in (x.lstrip("-–— ").strip() for x in lines[: CFG.max_playlist_tracks]):
        if not ln:
            continue
        a, t = split_artist_title(ln)
        tracks.append(TrackRequest(artist=a, title=t, query=ln, origin="text"))
    return Resolved("track" if len(tracks) == 1 else "playlist", tracks[0].label if len(tracks) == 1 else
                    f"{len(tracks)} треков", tracks)
