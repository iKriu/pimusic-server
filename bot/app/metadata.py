"""Поиск эталонных метаданных: MusicBrainz (основа) + iTunes Search (жанр, обложка 1000x1000)."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from . import http
from .util import contains_all, norm, primary_artist, similarity, strip_feat, unwanted_variant

log = logging.getLogger(__name__)

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
ITUNES_COUNTRIES = [c.strip() for c in os.environ.get("ITUNES_COUNTRIES", "US,RU").split(",") if c.strip()]

VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"


@dataclass
class Hint:
    """Что мы знаем о треке до поиска: из запроса, ссылки Spotify или тегов скачанного файла."""
    artist: str = ""
    title: str = ""
    album: str = ""
    duration: float = 0.0      # секунды
    isrc: str = ""
    track_no: int = 0
    year: str = ""
    cover_url: str = ""


@dataclass
class TrackMeta:
    title: str
    artist: str
    album_artist: str
    album: str
    artists: list[str] = field(default_factory=list)
    track_no: int = 0
    track_total: int = 0
    disc_no: int = 1
    disc_total: int = 1
    date: str = ""            # YYYY или YYYY-MM-DD
    genres: list[str] = field(default_factory=list)
    isrc: str = ""
    compilation: bool = False
    release_type: str = ""
    label: str = ""
    mb_recording_id: str = ""
    mb_track_id: str = ""
    mb_release_id: str = ""
    mb_release_group_id: str = ""
    mb_artist_ids: list[str] = field(default_factory=list)
    mb_album_artist_ids: list[str] = field(default_factory=list)
    cover_urls: list[str] = field(default_factory=list)
    duration: float = 0.0
    matched: str = "none"     # musicbrainz / itunes / musicbrainz+itunes / none

    @property
    def year(self) -> str:
        return self.date[:4] if self.date else ""


# ---------------------------------------------------------------- MusicBrainz
_LUCENE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _lq(s: str) -> str:
    return _LUCENE.sub(r"\\\1", s)


def _credit(ac: list[dict] | None) -> tuple[str, list[str], list[str]]:
    """artist-credit -> ('A feat. B', ['A','B'], [mbid...])"""
    if not ac:
        return "", [], []
    name = "".join((c.get("name") or c.get("artist", {}).get("name", "")) + (c.get("joinphrase") or "") for c in ac)
    names = [c.get("name") or c.get("artist", {}).get("name", "") for c in ac]
    ids = [c.get("artist", {}).get("id", "") for c in ac if c.get("artist", {}).get("id")]
    return name.strip(), names, ids


_TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2, "Broadcast": 4, "Other": 5}
_BAD_SECONDARY = {"Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo", "Interview", "Spokenword",
                  "Soundtrack"}


_SPECIAL_EDITION = re.compile(r"\b(deluxe|edition|remaster(ed)?|anniversary|expanded|drumless|instrumental|"
                              r"karaoke|box|collector|bonus|demos?|live|sessions?)\b", re.I)


def _release_rank(rel: dict, hint: Hint) -> tuple:
    rg = rel.get("release-group") or {}
    ptype = rg.get("primary-type") or "Other"
    sec = set(rg.get("secondary-types") or [])
    status = rel.get("status") or ""
    date = rel.get("date") or "9999"
    album_bonus = 0
    if hint.album and similarity(hint.album, rel.get("title", "")) >= 0.6:
        album_bonus = -10  # совпадение с альбомом из подсказки (например, Spotify) важнее всего
    special = int(bool(_SPECIAL_EDITION.search(rel.get("title", "") + " " + (rel.get("disambiguation") or ""))))
    return (
        album_bonus,
        special,
        0 if status == "Official" else 1 if not status else 2,
        len(sec & _BAD_SECONDARY),
        _TYPE_RANK.get(ptype, 3),
        # в поиске media содержит только диск с этим треком: трек на 2-м+ диске = бокс-сет/делюкс
        int(int(((rel.get("media") or [{}])[0]).get("position") or 1) > 1),
        (rel.get("release-group") or {}).get("first-release-date") or "9999",
        int(rel.get("track-count") or 999) // 10,  # из одновременных изданий — самое компактное

        date if len(date) >= 4 else "9999",
    )


def _score_recording(rec: dict, hint: Hint) -> float:
    rtitle = rec.get("title", "")
    title_sim = similarity(hint.title, rtitle)
    if norm(hint.title) == norm(rtitle):
        title_sim = 1.0
    elif contains_all(rtitle, strip_feat(hint.title)):
        title_sim = max(title_sim, 0.85)  # "Weird Fishes" -> "Weird Fishes/Arpeggi"
    credit, names, _ = _credit(rec.get("artist-credit"))
    artist_sim = max([similarity(hint.artist, credit)] + [similarity(primary_artist(hint.artist), n) for n in names]) \
        if hint.artist else 0.5
    score = title_sim * 50 + artist_sim * 35 + (rec.get("score", 0) / 100) * 10
    if hint.artist and artist_sim < 0.5:
        score -= 40  # кавер другого исполнителя
    length = (rec.get("length") or 0) / 1000
    if hint.duration and length:
        diff = abs(hint.duration - length)
        score += 10 if diff <= 3 else 5 if diff <= 7 else -30 if diff > 20 else -5
    elif hint.duration and not length:
        score -= 5
    releases = rec.get("releases") or []
    if not releases:
        score -= 20
    # «популярность»: у оригинальной записи десятки релизов, у каверов и бутлегов — один-два
    score += min(len(releases), 30) * 0.8
    # предпочитаем студийную запись: есть официальный альбом/сингл без пометок live/сборник
    clean = any(
        (r.get("status") in (None, "Official"))
        and not (set((r.get("release-group") or {}).get("secondary-types") or []) & _BAD_SECONDARY)
        for r in releases
    )
    score += 12 if clean else -12
    dis = (rec.get("disambiguation") or "").lower()
    if any(w in dis for w in ("live", "demo", "remix", "instrumental", "acoustic", "edit")) \
            and not any(w in norm(hint.title) for w in ("live", "demo", "remix", "instrumental", "acoustic", "edit")):
        score -= 25
    if rec.get("video"):
        score -= 15
    if unwanted_variant(rtitle + " " + (rec.get("disambiguation") or ""), hint.title):
        score -= 40  # «… (Sped Up)», «Slowed + Reverb» — если их не просили, это не та запись
    return score


_ARTIST_IDS: dict[str, str | None] = {}


async def _mb_artist_id(name: str) -> str | None:
    key = norm(name)
    if key in _ARTIST_IDS:
        return _ARTIST_IDS[key]
    data = await http.get_json(f"{MB}/artist", params={"query": f'artist:"{_lq(name)}"', "fmt": "json", "limit": "5"},
                               limiter=http.MB_LIMIT)
    aid = None
    for a in (data or {}).get("artists") or []:
        names = [a.get("name", "")] + [x.get("name", "") for x in a.get("aliases") or []]
        if a.get("score", 0) >= 90 and any(norm(n) == key for n in names):
            aid = a["id"]
            break
    if len(_ARTIST_IDS) > 5000:
        _ARTIST_IDS.clear()
    _ARTIST_IDS[key] = aid
    return aid


async def _mb_search(hint: Hint) -> list[dict]:
    if not hint.title:
        return []
    title = _lq(strip_feat(hint.title))
    queries = []
    if hint.artist:
        aid = await _mb_artist_id(strip_feat(hint.artist))
        if not aid and primary_artist(hint.artist) != strip_feat(hint.artist):
            aid = await _mb_artist_id(primary_artist(hint.artist))
        if aid:  # по MBID артиста — отсекает однофамильцев и каверы
            if not any(w in norm(hint.title) for w in ("live", "remix", "demo", "acoustic")):
                # строгий запрос: студийная запись с официального релиза (иначе у популярных групп
                # первые 100 результатов забиты концертными версиями)
                queries.append(f'recording:"{title}" AND arid:{aid} AND status:official AND video:false '
                               f'AND NOT secondarytype:live AND NOT secondarytype:compilation '
                               f'AND NOT comment:live AND NOT comment:demo AND NOT comment:remix')
            queries.append(f'recording:"{title}" AND arid:{aid}')
            queries.append(f"recording:({title}) AND arid:{aid}")
        queries.append(f'recording:"{title}" AND artist:"{_lq(primary_artist(hint.artist))}"')
        queries.append(f"{_lq(primary_artist(hint.artist))} {title}")
    else:
        queries.append(f'recording:"{title}"')
    # объединяем строгий запрос и обычный по MBID артиста; к остальным переходим, только если пусто
    merged: dict[str, dict] = {}
    for i, q in enumerate(queries):
        if merged and i >= 2:
            break
        data = await http.get_json(f"{MB}/recording", params={"query": q, "fmt": "json", "limit": "100"},
                                   limiter=http.MB_LIMIT)
        for r in (data or {}).get("recordings") or []:
            if " arid:" in q:
                r["_by_arid"] = True  # найдено по MBID исполнителя — это точно он, даже если имя латиницей
            merged.setdefault(r["id"], r)
    return list(merged.values())


async def _mb_by_isrc(isrc: str) -> list[dict]:
    for inc in ("artists+releases+release-groups", "artists+releases"):
        data = await http.get_json(f"{MB}/isrc/{isrc}", params={"inc": inc, "fmt": "json"}, limiter=http.MB_LIMIT)
        if data and data.get("recordings"):
            return data["recordings"]
    return []


async def _mb_release(release_id: str) -> dict | None:
    return await http.get_json(
        f"{MB}/release/{release_id}",
        params={"inc": "artist-credits+release-groups+genres+recordings+isrcs+labels", "fmt": "json"},
        limiter=http.MB_LIMIT,
    )


async def _mb_recording_genres(rec_id: str) -> list[str]:
    data = await http.get_json(f"{MB}/recording/{rec_id}", params={"inc": "genres+isrcs", "fmt": "json"},
                               limiter=http.MB_LIMIT)
    return [g["name"] for g in sorted((data or {}).get("genres") or [], key=lambda g: -g.get("count", 0))]


def _title_case_genre(g: str) -> str:
    special = {"hip hop": "Hip-Hop", "r&b": "R&B", "edm": "EDM", "idm": "IDM", "uk garage": "UK Garage",
               "drum and bass": "Drum & Bass", "k-pop": "K-Pop", "j-pop": "J-Pop", "lo-fi": "Lo-Fi"}
    low = g.lower().strip()
    return special.get(low, " ".join(w[:1].upper() + w[1:] for w in low.split()))


_MB_CACHE: dict[tuple, tuple[float, "TrackMeta | None"]] = {}


async def lookup_musicbrainz(hint: Hint) -> TrackMeta | None:
    """С кэшем на 6 часов: длительность до скачивания и разметка после — один и тот же запрос к MB."""
    import copy
    import time
    key = (norm(hint.artist), norm(hint.title), norm(hint.album), hint.isrc)
    hit = _MB_CACHE.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return copy.deepcopy(hit[1])
    res = await _lookup_musicbrainz(hint)
    if len(_MB_CACHE) > 2000:
        _MB_CACHE.clear()
    _MB_CACHE[key] = (time.time(), copy.deepcopy(res))
    return res


def _plausible(rec: dict, hint: Hint) -> bool:
    """Жёсткая проверка здравого смысла: название И исполнитель найденной записи похожи на запрошенные."""
    rtitle = rec.get("title", "")
    title_ok = (norm(rtitle) == norm(hint.title) or contains_all(rtitle, strip_feat(hint.title))
                or similarity(hint.title, rtitle) >= 0.5)
    if not title_ok and rec.get("_by_arid") and _CYR.search(hint.title or "") and not _CYR.search(rtitle):
        # кириллица vs латиница у того же исполнителя: принимаем, только если совпала длительность
        length = (rec.get("length") or 0) / 1000
        title_ok = bool(hint.duration and length and abs(hint.duration - length) <= 3)
    if unwanted_variant(rtitle + " " + (rec.get("disambiguation") or ""), hint.title):
        return False
    if not hint.artist:
        return title_ok
    credit, names, _ = _credit(rec.get("artist-credit"))
    artist_ok = any(similarity(primary_artist(hint.artist), n) >= 0.5 for n in names) \
        or similarity(hint.artist, credit) >= 0.5 or contains_all(credit, primary_artist(hint.artist))
    if not artist_ok and _CYR.search(hint.artist) and not _CYR.search(credit):
        # «Ленинград» vs транслитерация «Leningrad» — сравнить нельзя; верим, только если
        # поиск шёл по MBID артиста (тогда artist-credit — тот же человек), иначе не рискуем
        artist_ok = bool(rec.get("_by_arid"))
    return title_ok and artist_ok


async def _lookup_musicbrainz(hint: Hint) -> TrackMeta | None:
    recs: list[dict] = []
    via_isrc = False
    if hint.isrc:
        recs = await _mb_by_isrc(hint.isrc)
        for r in recs:
            r.setdefault("score", 100)
        via_isrc = bool(recs)
    if not recs:
        recs = await _mb_search(hint)
    if not recs:
        return None
    scored = sorted(((_score_recording(r, hint), r) for r in recs), key=lambda x: -x[0])
    best_score, rec = scored[0]
    # Порог снимаем ТОЛЬКО для записей, найденных по ISRC. Если по ISRC ничего нет и сработал текстовый
    # поиск, он может вернуть что угодно («Ленинград — Турбобой» -> «Leningrad Cowboys — Leningrad»).
    if not via_isrc and (best_score < 65 or not _plausible(rec, hint)):
        log.info("MB: нет уверенного совпадения для %s - %s (%.1f)", hint.artist, hint.title, best_score)
        return None

    # Одна и та же песня в MB часто — несколько записей (альбомная, сингловая, ремастер).
    # Берём все близкие по оценке записи этого же артиста и выбираем лучший релиз среди всех.
    best_artists = set(_credit(rec.get("artist-credit"))[2])
    pairs: list[tuple[tuple, dict, dict]] = []
    for sc, r in scored[:10]:
        if sc < best_score - 15:
            continue
        r_artists = set(_credit(r.get("artist-credit"))[2])
        if best_artists and r_artists and not (best_artists & r_artists):
            continue
        for rel in r.get("releases") or []:
            rel_artists = set(_credit(rel.get("artist-credit"))[2])
            foreign = bool(rel_artists) and not (rel_artists & r_artists) and VARIOUS_ARTISTS_MBID not in rel_artists
            # релиз «Song (Sped Up)» той же записи — в самый конец, если ускоренную версию не просили
            variant = bool(unwanted_variant(rel.get("title", ""), hint.title))
            pairs.append(((int(variant), int(foreign)) + _release_rank(rel, hint) + (-sc,), rel, r))
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[0])
    _, best_rel, rec = pairs[0]
    release = await _mb_release(best_rel["id"])
    if not release:
        return None

    title = rec.get("title") or hint.title
    artist, artists, artist_ids = _credit(rec.get("artist-credit"))
    album_artist, _, album_artist_ids = _credit(release.get("artist-credit"))
    rg = release.get("release-group") or {}
    media = release.get("media") or []

    track_no = track_total = 0
    disc_no, disc_total = 1, max(1, len(media))
    mb_track_id = ""
    isrc = hint.isrc
    for medium in media:
        for t in medium.get("tracks") or []:
            if (t.get("recording") or {}).get("id") == rec["id"]:
                track_no = int(t.get("position") or 0)
                track_total = int(medium.get("track-count") or len(medium.get("tracks") or []))
                disc_no = int(medium.get("position") or 1)
                mb_track_id = t.get("id", "")
                isrcs = (t.get("recording") or {}).get("isrcs") or []
                if isrcs and not isrc:
                    isrc = isrcs[0]
                break
        if track_no:
            break

    genres = [g["name"] for g in sorted((rg.get("genres") or []) + (release.get("genres") or []),
                                        key=lambda g: -g.get("count", 0))]
    if not genres:
        genres = await _mb_recording_genres(rec["id"])
    if not genres and (album_artist_ids or artist_ids):
        aid = (album_artist_ids or artist_ids)[0]
        if aid != VARIOUS_ARTISTS_MBID:
            data = await http.get_json(f"{MB}/artist/{aid}", params={"inc": "genres", "fmt": "json"},
                                       limiter=http.MB_LIMIT)
            genres = [g["name"] for g in sorted((data or {}).get("genres") or [], key=lambda g: -g.get("count", 0))]
    seen: set[str] = set()
    genres = [g for g in (_title_case_genre(x) for x in genres) if not (g.lower() in seen or seen.add(g.lower()))][:3]

    is_va = VARIOUS_ARTISTS_MBID in album_artist_ids
    compilation = is_va or "Compilation" in (rg.get("secondary-types") or [])
    labels = release.get("label-info") or []
    label = ((labels[0].get("label") or {}).get("name", "")) if labels else ""

    covers = []
    if (release.get("cover-art-archive") or {}).get("front"):
        covers.append(f"{CAA}/release/{release['id']}/front-1200")
    if rg.get("id"):
        covers.append(f"{CAA}/release-group/{rg['id']}/front-1200")

    return TrackMeta(
        title=title, artist=artist or hint.artist, artists=artists or [hint.artist],
        album_artist=album_artist or primary_artist(artist), album=release.get("title") or hint.album or title,
        track_no=track_no, track_total=track_total, disc_no=disc_no, disc_total=disc_total,
        date=rg.get("first-release-date") or release.get("date") or "",  # год оригинального выхода, а не переиздания
        genres=genres, isrc=isrc, compilation=compilation, release_type=(rg.get("primary-type") or "").lower(),
        label=label, mb_recording_id=rec["id"], mb_track_id=mb_track_id, mb_release_id=release["id"],
        mb_release_group_id=rg.get("id", ""), mb_artist_ids=artist_ids, mb_album_artist_ids=album_artist_ids,
        cover_urls=covers, duration=(rec.get("length") or 0) / 1000 or hint.duration, matched="musicbrainz",
    )


# ---------------------------------------------------------------- iTunes
def _itunes_art(url: str) -> str:
    return re.sub(r"/\d+x\d+(bb|-\d+)?\.(jpg|png|webp)$", "/1000x1000bb.jpg", url) if url else ""


DEEZER_API = "https://api.deezer.com"


async def deezer_duration(artist: str, title: str, isrc: str = "") -> float:
    """Каноническая длительность трека из Deezer (по ISRC, иначе поиском с проверкой похожести). 0 — не нашлось.
    Нужна, когда в запросе длительности нет (экспорт Spotify, ссылка): иначе YouTube-версию другой длины
    (клип, укороченная редакция) нечем отличить от нужной."""
    if isrc:
        d = await http.get_json(f"{DEEZER_API}/track/isrc:{isrc}")
        if isinstance(d, dict) and not d.get("error") and d.get("duration"):
            return float(d["duration"])
    if not artist or not title:
        return 0.0
    d = await http.get_json(f"{DEEZER_API}/search", params={"q": f'artist:"{artist}" track:"{title}"', "limit": "5"})
    for r in (d or {}).get("data") or []:
        if not r.get("duration") or unwanted_variant(r.get("title", ""), title):
            continue
        if similarity(title, r.get("title", "")) >= 0.8 and                 similarity(primary_artist(artist), (r.get("artist") or {}).get("name", "")) >= 0.6:
            return float(r["duration"])
    return 0.0


async def lookup_itunes(hint: Hint, *, strict: bool = False) -> dict | None:
    """Лучшее совпадение iTunes или None. strict=True: сетевой сбой — http.FetchError, а не None."""
    if not hint.title:
        return None
    term = f"{primary_artist(hint.artist)} {strip_feat(hint.title)}".strip()
    best, best_score = None, 0.0
    for country in ITUNES_COUNTRIES:
        data = await http.get_json("https://itunes.apple.com/search",
                                   params={"term": term, "entity": "song", "limit": "15", "country": country},
                                   limiter=http.ITUNES_LIMIT, strict=strict)
        for r in (data or {}).get("results") or []:
            if unwanted_variant(f"{r.get('trackName', '')} {r.get('collectionName', '')}", hint.title):
                continue  # «Song - Sped Up» и т.п. — только если просили именно её
            ts = max(similarity(hint.title, r.get("trackName", "")),
                     similarity(hint.title, r.get("trackCensoredName", "")))
            as_ = similarity(primary_artist(hint.artist), r.get("artistName", "")) if hint.artist else 0.6
            sc = ts * 55 + as_ * 40
            if hint.duration and r.get("trackTimeMillis"):
                diff = abs(hint.duration - r["trackTimeMillis"] / 1000)
                sc += 8 if diff <= 3 else -20 if diff > 15 else 0
            if hint.album and similarity(hint.album, r.get("collectionName", "")) > 0.6:
                sc += 6
            if sc > best_score:
                best, best_score = r, sc
        if best_score >= 85:
            break
    if not best or best_score < 70:
        return None
    best["_art"] = _itunes_art(best.get("artworkUrl100", ""))
    return best


def _clean_itunes_album(name: str) -> str:
    return re.sub(r"\s+-\s+(Single|EP)$", "", name or "").strip()


def _from_itunes(it: dict, hint: Hint) -> TrackMeta:
    album = _clean_itunes_album(it.get("collectionName", ""))
    artist = it.get("artistName") or hint.artist
    album_artist = it.get("collectionArtistName") or primary_artist(artist)
    return TrackMeta(
        title=it.get("trackName") or hint.title, artist=artist, artists=[artist], album_artist=album_artist,
        album=album or hint.album or it.get("trackName") or hint.title,
        track_no=int(it.get("trackNumber") or 0), track_total=int(it.get("trackCount") or 0),
        disc_no=int(it.get("discNumber") or 1), disc_total=int(it.get("discCount") or 1),
        date=(it.get("releaseDate") or "")[:10], genres=[it["primaryGenreName"]] if it.get("primaryGenreName") else [],
        compilation=album_artist.lower() in ("various artists", "сборник"),
        cover_urls=[it["_art"]] if it.get("_art") else [], duration=(it.get("trackTimeMillis") or 0) / 1000,
        matched="itunes",
    )


_IGNORED_ITUNES_GENRES = {"music", "музыка"}


_CYR = re.compile(r"[а-яё]", re.I)


def _prefer_hint_script(meta: TrackMeta, hint: Hint) -> None:
    """MusicBrainz иногда отдаёт латинскую транслитерацию («Ruki Vverh!», «Moya Mishel»), хотя исполнитель —
    тот же (мы искали его по MBID). Если пользователь/Spotify дал имя кириллицей — оставляем кириллицу:
    так её ищут в каталогах обложек и так её привычно видеть в медиатеке."""
    if hint.artist and _CYR.search(hint.artist) and not _CYR.search(meta.artist or ""):
        latin = (meta.artists or [meta.artist])[0]
        native = primary_artist(hint.artist)
        if latin and native:
            meta.artist = meta.artist.replace(latin, native, 1) if latin in meta.artist else native
            meta.artists = [native] + list(meta.artists[1:])
            if meta.album_artist == latin:
                meta.album_artist = native
    title_is_translit = (hint.title and _CYR.search(hint.title) and meta.title and not _CYR.search(meta.title)
                         and abs(len(meta.title) - len(hint.title)) <= max(3, len(hint.title) // 3))
    if title_is_translit:
        if meta.album == meta.title:
            meta.album = hint.title
        meta.title = hint.title


async def resolve(hint: Hint) -> TrackMeta:
    """Главная точка входа: возвращает лучшие метаданные, которые удалось собрать."""
    mb = await lookup_musicbrainz(hint)
    if mb:
        _prefer_hint_script(mb, hint)
    it = await lookup_itunes(Hint(artist=mb.artist if mb else hint.artist, title=mb.title if mb else hint.title,
                                  album=mb.album if mb else hint.album, duration=hint.duration))
    if mb and it:
        mb.matched = "musicbrainz+itunes"
        it_genre = it.get("primaryGenreName", "")
        if it_genre and it_genre.lower() not in _IGNORED_ITUNES_GENRES:
            if not mb.genres:
                mb.genres = [it_genre]
            elif it_genre.lower() not in (g.lower() for g in mb.genres):
                mb.genres.append(it_genre)
        same_album = similarity(mb.album, _clean_itunes_album(it.get("collectionName", ""))) >= 0.6
        if it.get("_art"):
            # Обложка iTunes 1000x1000 точнее по размеру; берём её первой, если альбом тот же
            if same_album:
                mb.cover_urls.insert(0, it["_art"])
            else:
                mb.cover_urls.append(it["_art"])
        if not mb.isrc and it.get("isrc"):
            mb.isrc = it["isrc"]
        if not mb.date and it.get("releaseDate"):
            mb.date = it["releaseDate"][:10]
        return mb
    if mb:
        return mb
    if it:
        return _from_itunes(it, hint)
    # Ничего не нашли — оформляем как сингл исполнителя, чтобы Jellyfin аккуратно сгруппировал
    artist = hint.artist or "Unknown Artist"
    title = hint.title or "Unknown Title"
    return TrackMeta(title=title, artist=artist, artists=[artist], album_artist=primary_artist(artist),
                     album=hint.album or title, track_no=hint.track_no or 1, track_total=1 if not hint.album else 0,
                     date=hint.year, cover_urls=[hint.cover_url] if hint.cover_url else [],
                     duration=hint.duration, matched="none")
