import pytest

from app.util import (
    clean_title, contains_all, find_audio_files, human_size, is_public_url, match_key, norm, primary_artist,
    safe_name, similarity, split_artist_title, strip_feat,
)


class TestNorm:
    def test_none_and_empty(self):
        assert norm(None) == ""
        assert norm("") == ""

    def test_diacritics_case_yo(self):
        assert norm("Beyoncé") == "beyonce"
        assert norm("Ёлка") == "елка"

    def test_article_ampersand_punctuation(self):
        assert norm("The Beatles") == "beatles"
        assert norm("AC/DC") == "ac dc"
        assert norm("Simon & Garfunkel") == "simon and garfunkel"

    def test_feat_and_remaster_removed(self):
        assert norm("Song (feat. Someone)") == "song"
        assert norm("Song (2011 Remastered Version)") == "song"

    def test_live_marker_is_kept(self):
        assert norm("Song (Live)") != norm("Song")


class TestStripFeat:
    @pytest.mark.parametrize("src,out", [
        ("Song (feat. X)", "Song"),
        ("Song [ft. X]", "Song"),
        ("Song ft. X", "Song"),
        ("Song featuring X", "Song"),
        ("Song feat X", "Song"),
        ("Песня при участии Иванова", "Песня"),
    ])
    def test_strips(self, src, out):
        assert strip_feat(src) == out

    @pytest.mark.parametrize("s", ["Daft Punk", "Loft", "Left Behind", "Soft Cell", "Feature Creep", "Aftermath", "Craft"])
    def test_does_not_strip_ft_inside_words(self, s):
        assert strip_feat(s) == s

    def test_keeps_trailing_bracket_after_feat(self):
        assert strip_feat("Song (feat. A) [Remix]") == "Song [Remix]"


class TestPrimaryArtist:
    @pytest.mark.parametrize("src,out", [
        ("A feat. B", "A"), ("A, B", "A"), ("A & B", "A"), ("A x B", "A"), ("A; B", "A"),
        ("A / B", "A"), ("A vs. B", "A"), ("Solo", "Solo"), ("Daft Punk", "Daft Punk"),
    ])
    def test_cases(self, src, out):
        assert primary_artist(src) == out


class TestMatchKey:
    def test_collab_and_remaster_collapse(self):
        assert match_key("Queen & David Bowie", "Under Pressure") == match_key(
            "Queen", "Under Pressure (Remastered 2011)")

    def test_format(self):
        assert match_key("The Beatles", "Help!") == "beatles|help"

    def test_different_titles_differ(self):
        assert match_key("A", "Song") != match_key("A", "Song (Live)")


class TestSplit:
    def test_hyphen_and_dashes(self):
        assert split_artist_title("A - B") == ("A", "B")
        assert split_artist_title("A – B") == ("A", "B")
        assert split_artist_title("A — B") == ("A", "B")

    def test_only_first_separator(self):
        assert split_artist_title("A - B - C") == ("A", "B - C")

    def test_no_separator(self):
        assert split_artist_title("Jay-Z") == ("", "Jay-Z")
        assert split_artist_title("  just title ") == ("", "just title")


class TestSafeName:
    @pytest.mark.parametrize("s", ["../x", "..\\x", "../../etc/passwd", "a/b", "..", "."])
    def test_traversal(self, s):
        r = safe_name(s)
        assert "/" not in r and "\\" not in r
        assert r not in ("..", ".")
        assert not r.startswith(".")

    def test_reserved_chars(self):
        assert safe_name('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"

    def test_control_chars(self):
        assert safe_name("a\tb\nc\x00d") == "a_b_c_d"

    def test_empty_and_dots(self):
        assert safe_name("") == "_"
        assert safe_name(None) == "_"
        assert safe_name("...") == "_"
        assert safe_name("  ") == "_"

    def test_trailing_dot_space(self):
        assert safe_name("name. . ") == "name"

    def test_nfc(self):
        assert safe_name("é") == "é"

    def test_long_cyrillic_truncated_on_byte_boundary(self):
        r = safe_name("я" * 100)
        assert len(r.encode()) <= 120
        assert r == "я" * 60

    def test_odd_boundary_not_broken(self):
        r = safe_name("яяя", maxlen=5)
        assert r == "яя"

    def test_emoji_boundary(self):
        r = safe_name("😀" * 40)
        assert len(r.encode()) <= 120
        assert set(r) == {"😀"}

    def test_truncation_does_not_leave_trailing_space(self):
        r = safe_name("a" * 119 + " b")
        assert not r.endswith((" ", "."))


class TestCleanTitle:
    @pytest.mark.parametrize("src,out", [
        ("Song (Official Video)", "Song"),
        ("Song (Official Music Video)", "Song"),
        ("Song [Official Audio]", "Song"),
        ("Song (Official Lyric Video)", "Song"),
        ("Song [HD]", "Song"),
        ("Song (Lyrics)", "Song"),
        ("Song (Explicit)", "Song"),
        ("Песня (Премьера клипа, 2020)", "Песня"),
        ("Song (feat. X) (Official Video)", "Song (feat. X)"),
        ("Song (Live)", "Song (Live)"),
        ("  - Song - ", "Song"),
    ])
    def test_cases(self, src, out):
        assert clean_title(src) == out


class TestIsPublicUrl:
    @pytest.mark.parametrize("url", [
        "https://example.com/a.mp3", "http://stream.radioparadise.com/mp3-320", "https://8.8.8.8/x",
        "https://sub.example.org:8443/path?q=1",
    ])
    def test_ok(self, url):
        assert is_public_url(url)

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1:8096/", "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/",
        "http://localhost/", "http://localhost:8096/", "http://jellyfin:8096/", "http://slskd:5030/",
        "http://pimusic-bot/", "http://printer.local/", "http://x.internal/", "http://router.lan/",
        "http://foo.localhost/", "http://intranet/",
        "file:///etc/passwd", "ftp://example.com/", "gopher://example.com/", "javascript:alert(1)", "",
        "not a url", "http://", "http://example.com/a b", "http://example.com/\n",
        "http://evil.com@127.0.0.1/",
    ])
    def test_blocked(self, url):
        assert not is_public_url(url)

    @pytest.mark.parametrize("url", ["http://localhost./", "http://jellyfin./", "http://slskd.:5030/"])
    def test_trailing_dot_internal(self, url):
        assert not is_public_url(url)

    @pytest.mark.parametrize("url", ["http://127.1/", "http://0x7f.0.0.1/", "http://010.0.0.1/"])
    def test_ip_shorthand(self, url):
        assert not is_public_url(url)


class TestMisc:
    def test_similarity(self):
        assert similarity("a b", "a b") == 1.0
        assert similarity("", "a") == 0.0
        assert 0 < similarity("a b c", "a b d") < 1

    def test_contains_all(self):
        assert contains_all("Radiohead - Creep (Remastered)", "creep")
        assert not contains_all("Radiohead - Creep", "creep live")

    def test_find_audio_files(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x")
        (tmp_path / "b.txt").write_bytes(b"x")
        (tmp_path / "c.part.mp3").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "d.FLAC").write_bytes(b"x")
        names = sorted(p.name for p in find_audio_files(tmp_path))
        assert names == ["a.mp3", "d.FLAC"]
        assert find_audio_files(tmp_path / "b.txt") == []

    def test_human_size(self):
        assert human_size(512) == "512.0 Б"
        assert human_size(2048) == "2.0 КБ"
