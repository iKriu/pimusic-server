import io
import shutil

import pytest
from mutagen.id3 import ID3
from PIL import Image

from app import http, lyrics, tagger
from app.metadata import TrackMeta
from app.tagger import _normalize_cover, _sort_name, write_tags

LRC = "[00:01.00]one\n[00:02.50]two\n[00:03.00][00:10.00]three\n"


def jpeg(w=300, h=300, color=(200, 30, 30), fmt="JPEG"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, fmt)
    return out.getvalue()


def dummy(tmp_path, name="a.mp3"):
    p = tmp_path / name
    p.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 2000)
    return p


def full_meta(**kw):
    base = dict(
        title="Song", artist="Artist feat. Guest", artists=["Artist", "Guest"], album_artist="Artist", album="Album",
        track_no=3, track_total=12, disc_no=2, disc_total=3, date="2001-05-01", genres=["Rock", "Pop"],
        isrc="GBAYE0000001", label="Label", release_type="album", mb_recording_id="rec-id", mb_track_id="trk-id",
        mb_release_id="rel-id", mb_release_group_id="rg-id", mb_artist_ids=["a1", "a2"], mb_album_artist_ids=["a1"],
    )
    base.update(kw)
    return TrackMeta(**base)


class TestWriteTags:
    def test_full_roundtrip(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(), jpeg(), LRC, "plain")
        t = ID3(str(p))
        assert t.version == (2, 4, 0)
        assert t["TIT2"].text == ["Song"]
        assert t["TPE1"].text == ["Artist", "Guest"]
        assert t["TXXX:ARTISTS"].text == ["Artist", "Guest"]
        assert t["TXXX:ARTIST_CREDIT"].text == ["Artist feat. Guest"]
        assert t["TPE2"].text == ["Artist"]
        assert t["TALB"].text == ["Album"]
        assert str(t["TRCK"]) == "3/12"
        assert str(t["TPOS"]) == "2/3"
        assert str(t["TDRC"]) == "2001-05-01"
        assert t["TCON"].text == ["Rock", "Pop"]
        assert str(t["TSRC"]) == "GBAYE0000001"
        assert str(t["TPUB"]) == "Label"
        assert t["TXXX:MusicBrainz Track Id"].text == ["rec-id"]
        assert t["TXXX:MusicBrainz Release Track Id"].text == ["trk-id"]
        assert t["TXXX:MusicBrainz Album Id"].text == ["rel-id"]
        assert t["TXXX:MusicBrainz Release Group Id"].text == ["rg-id"]
        assert t["TXXX:MusicBrainz Artist Id"].text == ["a1", "a2"]
        assert t["TXXX:MusicBrainz Album Artist Id"].text == ["a1"]
        assert t["TXXX:MusicBrainz Album Type"].text == ["album"]
        assert t["UFID:http://musicbrainz.org"].data == b"rec-id"
        assert "TCMP" not in t

    def test_no_id3v1_and_audio_intact(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(), None, "", "")
        raw = p.read_bytes()
        assert raw[:3] == b"ID3" and raw[3] == 4
        assert b"TAG" not in raw[-128:-125]
        assert raw.endswith(b"\xff\xfb\x90\x64" + b"\x00" * 2000)

    def test_cover_apic(self, tmp_path):
        p = dummy(tmp_path)
        data = jpeg()
        write_tags(p, full_meta(), data, "", "")
        apics = ID3(str(p)).getall("APIC")
        assert len(apics) == 1 and apics[0].type == 3 and apics[0].mime == "image/jpeg" and apics[0].data == data

    def test_no_cover_no_apic(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(), None, "", "")
        assert ID3(str(p)).getall("APIC") == []

    def test_uslt_and_sylt_from_lrc(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(), None, LRC, "")
        t = ID3(str(p))
        assert t.getall("USLT")[0].text == LRC
        sylt = t.getall("SYLT")[0]
        assert sylt.format == 2 and sylt.type == 1
        assert sylt.text == [("one", 1000), ("two", 2500), ("three", 3000), ("three", 10000)]

    def test_plain_lyrics_only(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(), None, "", "just words")
        t = ID3(str(p))
        assert t.getall("USLT")[0].text == "just words"
        assert t.getall("SYLT") == []

    def test_no_lyrics(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(), None, "", "")
        t = ID3(str(p))
        assert t.getall("USLT") == [] and t.getall("SYLT") == []

    def test_minimal_meta(self, tmp_path):
        p = dummy(tmp_path)
        m = TrackMeta(title="T", artist="A", album_artist="A", album="T")
        write_tags(p, m, None, "", "")
        t = ID3(str(p))
        assert t["TPE1"].text == ["A"]
        assert "TRCK" not in t and "TDRC" not in t and "TCON" not in t and "TSRC" not in t
        assert str(t["TPOS"]) == "1/1"
        assert "TXXX:ARTIST_CREDIT" not in t

    def test_track_without_total(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(track_total=0), None, "", "")
        assert str(ID3(str(p))["TRCK"]) == "3"

    def test_compilation_flag(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(compilation=True), None, "", "")
        assert str(ID3(str(p))["TCMP"]) == "1"

    def test_overwrites_old_tags(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(title="Old", genres=["Jazz"]), jpeg(), "", "old lyrics")
        write_tags(p, full_meta(title="New", genres=[]), None, "", "")
        t = ID3(str(p))
        assert t["TIT2"].text == ["New"]
        assert "TCON" not in t and t.getall("APIC") == [] and t.getall("USLT") == []

    def test_unicode_and_special_chars(self, tmp_path):
        p = dummy(tmp_path)
        title = "Пæсня 🎵 'quote' \"dq\" ; DROP TABLE;"
        write_tags(p, full_meta(title=title, artists=["Ёлка"], artist="Ёлка", album="日本語"), None, "", "")
        t = ID3(str(p))
        assert t["TIT2"].text == [title] and t["TPE1"].text == ["Ёлка"] and t["TALB"].text == ["日本語"]

    def test_empty_artist_list_falls_back(self, tmp_path):
        p = dummy(tmp_path)
        write_tags(p, full_meta(artists=[], artist="Solo"), None, "", "")
        assert ID3(str(p))["TPE1"].text == ["Solo"]

    def test_sort_name(self):
        assert _sort_name("The Beatles") == "Beatles, The"
        assert _sort_name("A Tribe Called Quest") == "Tribe Called Quest, A"
        assert _sort_name("Theory") == "Theory"
        assert _sort_name("The ") == "The "


class TestNormalizeCover:
    def open(self, data):
        return Image.open(io.BytesIO(data))

    def test_square_small_kept(self):
        im = self.open(_normalize_cover(jpeg(300, 300)))
        assert im.format == "JPEG" and im.size == (300, 300)

    def test_non_square_cropped_to_center(self):
        # left half red, right half blue; 600x300 -> center 300x300 contains both halves
        img = Image.new("RGB", (600, 300), (255, 0, 0))
        img.paste((0, 0, 255), (300, 0, 600, 300))
        out = io.BytesIO()
        img.save(out, "PNG")
        im = self.open(_normalize_cover(out.getvalue()))
        assert im.size == (300, 300)
        assert im.getpixel((10, 150))[0] > 200  # red on the left
        assert im.getpixel((290, 150))[2] > 200  # blue on the right

    def test_tall_cropped(self):
        assert self.open(_normalize_cover(jpeg(300, 900))).size == (300, 300)

    def test_large_downsized_to_1000(self):
        assert self.open(_normalize_cover(jpeg(2000, 1500))).size == (1000, 1000)
        assert self.open(_normalize_cover(jpeg(1600, 1600))).size == (1000, 1000)

    def test_exactly_1000_untouched(self):
        assert self.open(_normalize_cover(jpeg(1000, 1000))).size == (1000, 1000)

    def test_too_small_rejected(self):
        assert _normalize_cover(jpeg(150, 150)) is None
        assert _normalize_cover(jpeg(500, 199)) is None

    def test_garbage_rejected(self):
        assert _normalize_cover(b"not an image") is None
        assert _normalize_cover(b"") is None

    def test_png_with_alpha_converted(self):
        out = io.BytesIO()
        Image.new("RGBA", (400, 400), (1, 2, 3, 0)).save(out, "PNG")
        assert self.open(_normalize_cover(out.getvalue())).format == "JPEG"


class TestTagFile:
    async def test_tag_file_writes_everything(self, tmp_path, monkeypatch):
        async def fake_fetch(*a, **k):
            return LRC, "plain"

        async def fake_bytes(url, **k):
            return jpeg(800, 600)

        monkeypatch.setattr(lyrics, "fetch", fake_fetch)
        monkeypatch.setattr(http, "get_bytes", fake_bytes)
        p = dummy(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        info = await tagger.tag_file(p, full_meta(cover_urls=["http://x/c.jpg"]), work)
        assert info == {"cover": True, "synced_lyrics": True, "lyrics": True}
        assert Image.open(work / "cover.jpg").size == (600, 600)
        assert p.with_suffix(".lrc").read_text(encoding="utf-8") == LRC.strip() + "\n"
        assert ID3(str(p)).getall("APIC")

    async def test_plain_lyrics_go_to_txt(self, tmp_path, monkeypatch):
        async def fake_fetch(*a, **k):
            return "", "plain words"

        async def fake_bytes(url, **k):
            return None

        monkeypatch.setattr(lyrics, "fetch", fake_fetch)
        monkeypatch.setattr(http, "get_bytes", fake_bytes)
        p = dummy(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        info = await tagger.tag_file(p, full_meta(cover_urls=["http://x/c.jpg"]), work)
        assert info == {"cover": False, "synced_lyrics": False, "lyrics": True}
        assert p.with_suffix(".txt").read_text(encoding="utf-8") == "plain words\n"
        assert not (work / "cover.jpg").exists()

    async def test_own_cover_and_lyrics_used_when_nothing_found(self, tmp_path, monkeypatch):
        """Свой файл пользователя: в сети пусто — остаются его обложка и текст, приведённые к виду сервера."""
        async def nothing(*a, **k):
            return "", ""

        monkeypatch.setattr(lyrics, "fetch", nothing)
        p = dummy(tmp_path)
        write_tags(p, full_meta(), jpeg(1600, 1200), LRC, "")  # «чужие» теги загруженного файла
        own = tagger.read_own_art(p)
        assert own.cover and lyrics.parse_lrc(own.synced)
        work = tmp_path / "work"
        work.mkdir()
        info = await tagger.tag_file(p, full_meta(cover_urls=[], duration=0), work, own=own)
        assert info == {"cover": True, "synced_lyrics": True, "lyrics": True}
        assert Image.open(work / "cover.jpg").size == (1000, 1000)  # квадрат до 1000×1000
        assert ID3(str(p)).getall("APIC") and ID3(str(p)).getall("SYLT") and p.with_suffix(".lrc").exists()

    async def test_found_online_beats_own(self, tmp_path, monkeypatch):
        async def fake_fetch(*a, **k):
            return "", "из сети"

        monkeypatch.setattr(lyrics, "fetch", fake_fetch)
        p = dummy(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        await tagger.tag_file(p, full_meta(cover_urls=[], duration=0), work, own=tagger.OwnArt(plain="своё"))
        assert p.with_suffix(".txt").read_text(encoding="utf-8") == "из сети\n"

    def test_own_synced_out_of_track_becomes_plain(self):
        own = tagger.OwnArt(synced="[00:01.00]раз\n[00:02.00]два\n[00:03.00]три\n[09:00.00]четыре")
        synced, plain = tagger._own_lyrics(own, 120)
        assert synced == "" and plain == "раз\nдва\nтри\nчетыре"

    def test_read_own_art_of_garbage_is_empty(self, tmp_path):
        p = tmp_path / "x.mp3"
        p.write_bytes(b"not audio")
        assert tagger.read_own_art(p) == tagger.OwnArt()

    async def test_fetch_cover_prefers_existing_big_file(self, tmp_path, monkeypatch):
        async def fake_bytes(url, **k):
            raise AssertionError("no network expected")

        monkeypatch.setattr(http, "get_bytes", fake_bytes)
        (tmp_path / "cover.jpg").write_bytes(b"x" * 6000)
        assert await tagger.fetch_cover(full_meta(cover_urls=["http://x"]), tmp_path) == b"x" * 6000

    async def test_fetch_cover_skips_bad_urls(self, tmp_path, monkeypatch):
        good = jpeg(400, 400)
        data = {"http://bad": b"garbage", "http://none": None, "http://good": good}

        async def fake_bytes(url, **k):
            return data[url]

        monkeypatch.setattr(http, "get_bytes", fake_bytes)
        out = await tagger.fetch_cover(full_meta(cover_urls=["http://none", "http://bad", "http://good"]))
        assert Image.open(io.BytesIO(out)).size == (400, 400)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
async def test_probe_missing_file_returns_none(tmp_path):
    from app.util import probe
    assert await probe(tmp_path / "nope.mp3") is None


async def test_run_missing_binary_returns_127():
    from app.util import run
    r = await run(["definitely-not-a-binary-xyz"])
    assert r.rc == 127


async def test_mp3_is_cbr320_on_dummy_is_false(tmp_path):
    from app.util import mp3_is_cbr320
    assert await mp3_is_cbr320(dummy(tmp_path)) is False
    assert await mp3_is_cbr320(tmp_path / "missing.mp3") is False
