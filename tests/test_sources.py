import pytest

from app import sources
from app.sources import TrackRequest, _req_from_entry, classify

SID = "4uLU6hMCjMI75M1A2tKUQC"


class TestClassify:
    @pytest.mark.parametrize("text", [
        f"https://open.spotify.com/track/{SID}",
        f"https://open.spotify.com/intl-ru/track/{SID}?si=abc",
        f"https://open.spotify.com/intl-de/album/{SID}",
        f"https://open.spotify.com/playlist/{SID}",
        f"spotify:track:{SID}",
        f"spotify:playlist:{SID}",
        f"listen to this https://open.spotify.com/track/{SID}",
    ])
    def test_spotify(self, text):
        assert classify(text) == "spotify"

    def test_spotify_bad_id_is_not_spotify(self):
        assert classify("https://open.spotify.com/track/short") == "url"

    @pytest.mark.parametrize("text,kind", [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://music.youtube.com/watch?v=abc", "youtube"),
        ("https://soundcloud.com/artist/track", "soundcloud"),
        ("https://artist.bandcamp.com/track/x", "bandcamp"),
        ("https://example.com/file.mp3", "url"),
        ("Radiohead - Creep", "text"),
        ("", "text"),
    ])
    def test_others(self, text, kind):
        assert classify(text) == kind


class TestResolveText:
    async def test_single_line(self):
        r = await sources.resolve("Radiohead - Creep")
        assert r.kind == "track" and len(r.tracks) == 1
        t = r.tracks[0]
        assert (t.artist, t.title, t.origin) == ("Radiohead", "Creep", "text")
        assert r.title == "Radiohead — Creep"

    async def test_multiline_strips_leading_dashes_and_blanks(self):
        text = "- Radiohead - Creep\n\n– Queen - Bohemian Rhapsody\n— Just a title\n   \n-- Muse - Hysteria"
        r = await sources.resolve(text)
        assert r.kind == "playlist"
        assert [(t.artist, t.title) for t in r.tracks] == [
            ("Radiohead", "Creep"), ("Queen", "Bohemian Rhapsody"), ("", "Just a title"), ("Muse", "Hysteria")]
        assert r.title == "4 треков"
        assert r.tracks[2].query == "Just a title"

    async def test_empty_input(self):
        r = await sources.resolve("  \n \n")
        assert r.tracks == [] and not r.error

    async def test_only_dashes(self):
        r = await sources.resolve("---")
        assert r.tracks == []

    async def test_limit_max_playlist_tracks(self, monkeypatch):
        from conftest import patch_cfg
        patch_cfg(monkeypatch, max_playlist_tracks=3)
        r = await sources.resolve("\n".join(f"A - T{i}" for i in range(10)))
        assert len(r.tracks) == 3

    async def test_spotify_artist_link_unsupported(self):
        r = await sources.resolve(f"https://open.spotify.com/artist/{SID}")
        assert r.error and not r.tracks

    async def test_private_url_rejected_without_running_ytdlp(self, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("must not run yt-dlp")
        monkeypatch.setattr(sources, "run", boom)
        r = await sources.resolve("https://localhost/secret")
        assert r.error


class TestReqFromEntry:
    def test_artist_title_with_official_video(self):
        r = _req_from_entry({"title": "Artist - Title (Official Video)", "uploader": "VEVO", "duration": 200,
                             "webpage_url": "https://youtube.com/watch?v=1"}, "youtube")
        assert (r.artist, r.title) == ("Artist", "Title")
        assert r.duration == 200 and r.direct_url == "https://youtube.com/watch?v=1" and r.origin == "youtube"

    def test_uses_explicit_artist_track_fields(self):
        r = _req_from_entry({"artist": "A, B", "track": "Song", "title": "ignored", "album": "Alb"}, "soundcloud")
        assert (r.artist, r.title, r.album) == ("A", "Song", "Alb")

    def test_artists_list_joined(self):
        r = _req_from_entry({"artist": "A", "artists": ["A", "B", "C", "D"], "title": "S"}, "youtube")
        assert r.artist == "A, B, C"

    def test_topic_channel_fallback(self):
        r = _req_from_entry({"title": "Just Song", "uploader": "Some Band - Topic"}, "youtube")
        assert (r.artist, r.title) == ("Some Band", "Just Song")

    def test_relative_youtube_id_becomes_url(self):
        r = _req_from_entry({"url": "dQw4w9WgXcQ", "title": "A - B"}, "youtube")
        assert r.direct_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_non_youtube_relative_not_rewritten(self):
        r = _req_from_entry({"url": "abc", "title": "A - B"}, "soundcloud")
        assert r.direct_url == "abc"

    def test_missing_everything(self):
        r = _req_from_entry({}, "youtube")
        assert (r.artist, r.title, r.duration, r.direct_url) == ("", "", 0.0, "")

    def test_none_values(self):
        r = _req_from_entry({"title": None, "uploader": None, "duration": None}, "url")
        assert r.title == "" and r.duration == 0.0


class TestTrackRequest:
    def test_label_and_search_text(self):
        assert TrackRequest(artist="A", title="T").label == "A — T"
        assert TrackRequest(artist="A", title="T").search_text == "A - T"
        assert TrackRequest(query="free text").label == "free text"
        assert TrackRequest(direct_url="http://x").label == "http://x"
