import pytest

from app import http, metadata
from app.metadata import (
    MB, VARIOUS_ARTISTS_MBID, Hint, _release_rank, _score_recording, lookup_musicbrainz, resolve,
)

RH = ("Radiohead", "A-RH")
VA = ("Various Artists", VARIOUS_ARTISTS_MBID)


def credit(artist):
    return [{"name": artist[0], "joinphrase": "", "artist": {"id": artist[1], "name": artist[0]}}]


def rel(rid, title, rgid, ptype="Album", sec=(), status="Official", date="1993-02-22", first="1993-02-22",
        count=12, pos=1, artist=RH, disamb=""):
    return {
        "id": rid, "title": title, "status": status, "date": date, "track-count": count, "disambiguation": disamb,
        "release-group": {"id": rgid, "primary-type": ptype, "secondary-types": list(sec),
                          "first-release-date": first},
        "media": [{"position": pos}], "artist-credit": credit(artist),
    }


def rec(rid, title, releases, artist=RH, length=238000, score=100, disamb="", video=False):
    return {"id": rid, "title": title, "length": length, "score": score, "disambiguation": disamb, "video": video,
            "artist-credit": credit(artist), "releases": releases}


def detail(rid, title, rgid, media, artist=RH, genres=(("alternative rock", 5),), ptype="Album", sec=(),
           first="1993-02-22"):
    return {
        "id": rid, "title": title, "artist-credit": credit(artist), "date": first,
        "release-group": {"id": rgid, "primary-type": ptype, "secondary-types": list(sec), "first-release-date": first,
                          "genres": [{"name": g, "count": c} for g, c in genres]},
        "media": media, "label-info": [{"label": {"name": "Parlophone"}}], "cover-art-archive": {"front": True},
    }


def medium(pos, tracks, total=None):
    return {"position": pos, "track-count": total or len(tracks),
            "tracks": [{"id": f"trk-{r}", "position": i, "recording": {"id": r, **({"isrcs": ["GBAYE9300106"]} if r == "R1" else {})}}
                       for i, r in tracks]}


def install(monkeypatch, recordings, releases, artists=None, itunes=None):
    metadata._ARTIST_IDS.clear()
    calls = []

    async def fake_get_json(url, *, params=None, **kw):
        calls.append((url, params))
        if url == f"{MB}/artist":
            return {"artists": artists if artists is not None else [
                {"id": RH[1], "name": "Radiohead", "score": 100}]}
        if url == f"{MB}/recording":
            return {"recordings": recordings}
        if url.startswith(f"{MB}/release/"):
            return releases.get(url.rsplit("/", 1)[1])
        if url.startswith(f"{MB}/recording/") or url.startswith(f"{MB}/artist/"):
            return {"genres": []}
        if "itunes.apple.com" in url:
            return {"results": itunes or []}
        return None

    async def fake_bytes(url, **kw):
        return None

    monkeypatch.setattr(http, "get_json", fake_get_json)
    monkeypatch.setattr(http, "get_bytes", fake_bytes)
    return calls


@pytest.fixture(autouse=True)
def _clear_cache():
    metadata._ARTIST_IDS.clear()
    yield
    metadata._ARTIST_IDS.clear()


def creep_fixture():
    extra = [rel(f"rel-extra-{i}", "Pablo Honey", "rg-pablo", date=f"{1994 + i}-01-01", count=12) for i in range(8)]
    original = rec("R1", "Creep", [
        rel("rel-pablo-1993", "Pablo Honey", "rg-pablo", date="1993-02-22", count=12),
        rel("rel-single", "Creep", "rg-single", ptype="Single", date="1992-09-21", first="1992-09-21", count=4),
        rel("rel-deluxe", "Pablo Honey (Collector's Edition)", "rg-pablo", date="2009-01-01", count=40),
        rel("rel-best", "The Best Of", "rg-best", sec=["Compilation"], date="2008-06-01", first="2008-06-01", count=18),
        rel("rel-box", "Radiohead Box", "rg-box", ptype="Album", date="1999-01-01", first="1999-01-01", count=50, pos=2),
        *extra,
    ])
    live = rec("R2", "Creep", [rel("rel-bootleg", "Live in Tokyo", "rg-live", ptype="Album", sec=["Live"],
                                   status="Bootleg", date="1994-01-01", first="1994-01-01", count=15)],
               length=245000, disamb="live")
    cover = rec("R3", "Creep", [rel("rel-cover", "Hymns", "rg-cover", artist=("Scala", "A-SC"), date="2012-01-01",
                                    first="2012-01-01", count=10)],
                artist=("Scala & Kolacny Brothers", "A-SC"), length=240000, score=95)
    releases = {
        "rel-pablo-1993": detail("rel-pablo-1993", "Pablo Honey", "rg-pablo", [
            medium(1, [(1, "R0"), (2, "R1"), (3, "R9")], total=12)]),
        "rel-single": detail("rel-single", "Creep", "rg-single", [medium(1, [(1, "R1")])], ptype="Single"),
    }
    return [live, cover, original], releases


class TestScoreRecording:
    HINT = Hint(artist="Radiohead", title="Creep", duration=238)

    def test_original_beats_live_and_cover(self):
        (live, cover, original), _ = creep_fixture()
        sc = {r["id"]: _score_recording(r, self.HINT) for r in (live, cover, original)}
        assert sc["R1"] > sc["R2"] + 15 and sc["R1"] > sc["R3"] + 30
        assert sc["R1"] >= 65
        assert sc["R3"] < 65

    def test_no_releases_penalised(self):
        r = rec("X", "Creep", [])
        assert _score_recording(r, self.HINT) < _score_recording(rec("X", "Creep", [rel("r", "A", "g")]), self.HINT)

    def test_duration_far_off_penalised(self):
        near = _score_recording(rec("X", "Creep", [rel("r", "A", "g")], length=239000), self.HINT)
        far = _score_recording(rec("X", "Creep", [rel("r", "A", "g")], length=400000), self.HINT)
        assert near - far >= 35

    def test_video_penalised(self):
        a = _score_recording(rec("X", "Creep", [rel("r", "A", "g")]), self.HINT)
        b = _score_recording(rec("X", "Creep", [rel("r", "A", "g")], video=True), self.HINT)
        assert a - b == 15

    def test_live_disambiguation_penalised_unless_requested(self):
        live = rec("X", "Creep (Live)", [rel("r", "A", "g")], disamb="live")
        studio = rec("X", "Creep (Live)", [rel("r", "A", "g")])
        plain_hint = Hint(artist="Radiohead", title="Creep (Live)", duration=238)  # live requested -> no penalty
        assert _score_recording(live, plain_hint) == _score_recording(studio, plain_hint)
        live2 = rec("X", "Creep", [rel("r", "A", "g")], disamb="live")
        studio2 = rec("X", "Creep", [rel("r", "A", "g")])
        assert _score_recording(studio2, self.HINT) - _score_recording(live2, self.HINT) == 25

    def test_title_containing_hint(self):
        r = rec("X", "Weird Fishes/Arpeggi", [rel("r", "A", "g")])
        s = _score_recording(r, Hint(artist="Radiohead", title="Weird Fishes", duration=0))
        assert s > 65

    def test_no_artist_hint(self):
        assert _score_recording(rec("X", "Creep", [rel("r", "A", "g")]), Hint(title="Creep")) > 65


class TestReleaseRank:
    def test_original_album_beats_everything(self):
        h = Hint(artist="Radiohead", title="Creep")
        album = rel("album", "Pablo Honey", "g1")
        others = [
            rel("single", "Creep", "g2", ptype="Single", date="1992-01-01", first="1992-09-21", count=4),
            rel("comp", "The Best Of", "g3", sec=["Compilation"], first="2008-01-01"),
            rel("box2", "Pablo Honey", "g4", pos=2, first="1990-01-01"),
            rel("deluxe", "Pablo Honey (Deluxe Edition)", "g1", first="1990-01-01"),
            rel("promo", "Pablo Honey", "g1", status="Promotion"),
            rel("bootleg", "Pablo Honey", "g1", status="Bootleg"),
            rel("live", "Pablo Honey", "g5", sec=["Live"]),
        ]
        ranked = sorted([album] + others, key=lambda r: _release_rank(r, h))
        assert ranked[0]["id"] == "album"

    def test_album_hint_overrides(self):
        comp = rel("comp", "Greatest Hits", "g3", sec=["Compilation"], first="2008-01-01")
        album = rel("album", "Pablo Honey", "g1")
        h = Hint(artist="Radiohead", title="Creep", album="Greatest Hits")
        assert min([album, comp], key=lambda r: _release_rank(r, h))["id"] == "comp"
        assert min([album, comp], key=lambda r: _release_rank(r, Hint()))["id"] == "album"

    def test_earlier_release_group_wins_then_compact(self):
        a = rel("old", "X", "g1", first="1990-01-01", count=12)
        b = rel("new", "X", "g2", first="2000-01-01", count=12)
        assert min([b, a], key=lambda r: _release_rank(r, Hint()))["id"] == "old"
        big = rel("big", "X", "g1", first="1990-01-01", count=50)
        assert min([big, a], key=lambda r: _release_rank(r, Hint()))["id"] == "old"

    def test_missing_fields_do_not_crash(self):
        assert _release_rank({}, Hint())


class TestLookupMusicbrainz:
    async def test_picks_original_studio_album(self, monkeypatch):
        recs, releases = creep_fixture()
        install(monkeypatch, recs, releases)
        m = await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238))
        assert m is not None
        assert m.mb_recording_id == "R1"
        assert m.mb_release_id == "rel-pablo-1993"
        assert m.album == "Pablo Honey" and m.title == "Creep" and m.artist == "Radiohead"
        assert (m.track_no, m.track_total, m.disc_no, m.disc_total) == (2, 12, 1, 1)
        assert m.mb_track_id == "trk-R1"
        assert m.isrc == "GBAYE9300106"
        assert m.date == "1993-02-22" and m.year == "1993"
        assert m.genres == ["Alternative Rock"]
        assert m.label == "Parlophone"
        assert not m.compilation and m.release_type == "album"
        assert m.mb_artist_ids == ["A-RH"] and m.mb_album_artist_ids == ["A-RH"]
        assert m.cover_urls[0].endswith("/release/rel-pablo-1993/front-1200")
        assert m.matched == "musicbrainz"

    async def test_cover_by_other_artist_only_is_rejected(self, monkeypatch):
        recs, releases = creep_fixture()
        install(monkeypatch, [recs[1]], releases)
        assert await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238)) is None

    async def test_live_bootleg_only_is_rejected(self, monkeypatch):
        recs, releases = creep_fixture()
        install(monkeypatch, [recs[0]], releases)
        m = await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238))
        assert m is None

    async def test_nothing_found(self, monkeypatch):
        install(monkeypatch, [], {})
        assert await lookup_musicbrainz(Hint(artist="Nobody", title="Nothing")) is None

    async def test_empty_title(self, monkeypatch):
        install(monkeypatch, [], {})
        assert await lookup_musicbrainz(Hint(artist="Nobody")) is None

    async def test_release_fetch_failure(self, monkeypatch):
        recs, _ = creep_fixture()
        install(monkeypatch, recs, {})
        assert await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238)) is None

    async def test_box_set_disc_two_numbers(self, monkeypatch):
        r = rec("R1", "Creep", [rel("rel-box", "Box", "rg-box", date="1999-01-01", first="1999-01-01", count=20, pos=2)])
        releases = {"rel-box": detail("rel-box", "Box", "rg-box", [
            medium(1, [(1, "Rx"), (2, "Ry")]),
            medium(2, [(1, "Ra"), (2, "Rb"), (3, "Rc"), (4, "Rd"), (5, "R1")], total=10),
        ])}
        install(monkeypatch, [r], releases)
        m = await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238))
        assert (m.disc_no, m.disc_total, m.track_no, m.track_total) == (2, 2, 5, 10)

    async def test_prefers_normal_album_over_box_disc2_release(self, monkeypatch):
        r = rec("R1", "Creep", [
            rel("rel-box", "Box", "rg-box", date="1990-01-01", first="1990-01-01", count=20, pos=2),
            rel("rel-pablo-1993", "Pablo Honey", "rg-pablo", date="1993-02-22", first="1993-02-22", count=12),
        ])
        _, releases = creep_fixture()
        install(monkeypatch, [r], releases)
        m = await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238))
        assert m.mb_release_id == "rel-pablo-1993"

    async def test_various_artists_compilation_flag(self, monkeypatch):
        r = rec("R7", "Song 2", [rel("rel-now", "Now 50", "rg-now", sec=["Compilation"], artist=VA,
                                     date="2001-01-01", first="2001-01-01", count=40)],
                artist=("Blur", "A-BLUR"), length=122000)
        releases = {"rel-now": detail("rel-now", "Now 50", "rg-now", [
            {"position": 1, "track-count": 40, "tracks": [{"id": "t", "position": 7, "recording": {"id": "R7"}}]}],
            artist=VA, ptype="Album", sec=["Compilation"], first="2001-01-01", genres=())}
        install(monkeypatch, [r], releases, artists=[{"id": "A-BLUR", "name": "Blur", "score": 100}])
        m = await lookup_musicbrainz(Hint(artist="Blur", title="Song 2", duration=122))
        assert m is not None
        assert m.compilation is True
        assert m.album_artist == "Various Artists"
        assert m.artist == "Blur"
        assert m.mb_album_artist_ids == [VARIOUS_ARTISTS_MBID]
        assert m.track_no == 7

    async def test_secondary_compilation_without_va(self, monkeypatch):
        r = rec("R1", "Creep", [rel("rel-best", "The Best Of", "rg-best", sec=["Compilation"])])
        releases = {"rel-best": detail("rel-best", "The Best Of", "rg-best", [medium(1, [(1, "R1")])],
                                       sec=["Compilation"])}
        install(monkeypatch, [r], releases)
        m = await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", duration=238))
        assert m.compilation is True

    async def test_isrc_lookup_path(self, monkeypatch):
        recs, releases = creep_fixture()
        calls = install(monkeypatch, [], releases)
        orig = http.get_json

        async def isrc_aware(url, **kw):
            if "/isrc/" in url:
                return {"recordings": [recs[2]]}
            return await orig(url, **kw)

        monkeypatch.setattr(http, "get_json", isrc_aware)
        m = await lookup_musicbrainz(Hint(artist="Radiohead", title="Creep", isrc="GBAYE9300106"))
        assert m.mb_recording_id == "R1"

    async def test_feat_credit_names(self, monkeypatch):
        ac = [{"name": "Artist", "joinphrase": " feat. ", "artist": {"id": "A1", "name": "Artist"}},
              {"name": "Guest", "joinphrase": "", "artist": {"id": "A2", "name": "Guest"}}]
        r = rec("R1", "Song", [rel("rel1", "Alb", "rg1", artist=("Artist", "A1"))], artist=("x", "A1"))
        r["artist-credit"] = ac
        releases = {"rel1": detail("rel1", "Alb", "rg1", [medium(1, [(1, "R1")])], artist=("Artist", "A1"))}
        install(monkeypatch, [r], releases, artists=[{"id": "A1", "name": "Artist", "score": 100}])
        m = await lookup_musicbrainz(Hint(artist="Artist feat. Guest", title="Song", duration=238))
        assert m.artist == "Artist feat. Guest"
        assert m.artists == ["Artist", "Guest"]
        assert m.mb_artist_ids == ["A1", "A2"]


class TestResolve:
    async def test_fallback_when_nothing_found(self, monkeypatch):
        install(monkeypatch, [], {})
        m = await resolve(Hint(artist="Nobody", title="Nothing", duration=100, year="2020", cover_url="http://c/x.jpg"))
        assert m.matched == "none"
        assert (m.artist, m.title, m.album, m.album_artist) == ("Nobody", "Nothing", "Nothing", "Nobody")
        assert m.track_no == 1 and m.track_total == 1 and m.date == "2020"
        assert m.cover_urls == ["http://c/x.jpg"] and m.duration == 100

    async def test_fallback_with_album_hint_and_unknowns(self, monkeypatch):
        install(monkeypatch, [], {})
        m = await resolve(Hint(album="Some Album", track_no=4))
        assert m.artist == "Unknown Artist" and m.title == "Unknown Title"
        assert m.album == "Some Album" and m.track_no == 4 and m.track_total == 0

    async def test_fallback_collab_album_artist_is_primary(self, monkeypatch):
        install(monkeypatch, [], {})
        m = await resolve(Hint(artist="A feat. B", title="T"))
        assert m.artist == "A feat. B" and m.album_artist == "A"

    async def test_itunes_only(self, monkeypatch):
        install(monkeypatch, [], {}, itunes=[{
            "trackName": "Creep", "artistName": "Radiohead", "collectionName": "Pablo Honey - Single",
            "trackTimeMillis": 238000, "trackNumber": 2, "trackCount": 12, "primaryGenreName": "Alternative",
            "releaseDate": "1993-02-22T08:00:00Z", "artworkUrl100": "https://x/100x100bb.jpg"}])
        m = await resolve(Hint(artist="Radiohead", title="Creep", duration=238))
        assert m.matched == "itunes" and m.album == "Pablo Honey" and m.genres == ["Alternative"]
        assert m.cover_urls == ["https://x/1000x1000bb.jpg"] and m.date == "1993-02-22"

    async def test_mb_plus_itunes_merge(self, monkeypatch):
        recs, releases = creep_fixture()
        install(monkeypatch, recs, releases, itunes=[{
            "trackName": "Creep", "artistName": "Radiohead", "collectionName": "Pablo Honey",
            "trackTimeMillis": 238000, "primaryGenreName": "Alternative", "artworkUrl100": "https://x/100x100bb.jpg"}])
        m = await resolve(Hint(artist="Radiohead", title="Creep", duration=238))
        assert m.matched == "musicbrainz+itunes"
        assert m.genres == ["Alternative Rock", "Alternative"]
        assert m.cover_urls[0] == "https://x/1000x1000bb.jpg"
        assert m.mb_recording_id == "R1"


# ---------------------------------------------------------------- регрессия: ISRC не отключает порог для текстового поиска
def test_isrc_miss_does_not_accept_garbage_text_match(monkeypatch):
    """«Ленинград — Турбобой» с ISRC, которого нет в MusicBrainz: текстовый поиск вернул «Leningrad Cowboys —
    Leningrad». Раньше порог отключался из-за наличия ISRC, и трек получал чужие теги."""
    import asyncio as _a
    from app import metadata as M

    async def no_isrc(isrc):
        return []

    async def garbage_search(hint):
        return [{"id": "x", "score": 100, "title": "Leningrad", "length": 183000,
                 "artist-credit": [{"name": "Leningrad Cowboys", "artist": {"id": "lc"}}],
                 "releases": [{"id": f"r{i}", "title": "Go Space", "status": "Official",
                               "release-group": {"primary-type": "Album"}} for i in range(30)]},
                {"id": "y", "score": 90, "title": "Trailer for \"The Dramatised Agatha Christie Mysteries\"",
                 "artist-credit": [{"name": "Agatha Christie starring June Whitfield", "artist": {"id": "ac"}}],
                 "releases": [{"id": "r", "title": "Miss Marple", "status": "Official"}]}]

    monkeypatch.setattr(M, "_mb_by_isrc", no_isrc)
    monkeypatch.setattr(M, "_mb_search", garbage_search)
    hint = M.Hint(artist="Ленинград", title="Турбобой", isrc="RUA1D1234567", duration=183)
    assert _a.run(M._lookup_musicbrainz(hint)) is None
    hint2 = M.Hint(artist="Агата Кристи", title="Чёрная луна", isrc="RUA1D7654321")
    assert _a.run(M._lookup_musicbrainz(hint2)) is None


def test_plausible_accepts_transliteration_only_by_arid_and_duration():
    from app import metadata as M
    rec = {"title": "Turboboj", "length": 183000, "_by_arid": True,
           "artist-credit": [{"name": "Leningrad", "artist": {"id": "l"}}]}
    assert M._plausible(rec, M.Hint(artist="Ленинград", title="Турбобой", duration=184))
    assert not M._plausible(dict(rec, _by_arid=False), M.Hint(artist="Ленинград", title="Турбобой", duration=184))
    assert not M._plausible(rec, M.Hint(artist="Ленинград", title="Турбобой", duration=250))
