from app.lyrics import is_synced, lrc_to_plain, parse_lrc


class TestParseLrc:
    def test_multiple_stamps_sorted(self):
        out = parse_lrc("[01:02.50][00:12.34]Hello\n[00:01.123]Intro")
        assert out == [(1123, "Intro"), (12340, "Hello"), (62500, "Hello")]

    def test_two_digit_fraction_is_centiseconds(self):
        assert parse_lrc("[00:01.50]x") == [(1500, "x")]
        assert parse_lrc("[00:01.05]x") == [(1050, "x")]

    def test_three_digit_fraction(self):
        assert parse_lrc("[00:01.005]x") == [(1005, "x")]
        assert parse_lrc("[00:01.999]x") == [(1999, "x")]

    def test_one_digit_and_no_fraction(self):
        assert parse_lrc("[00:05.5]x") == [(5500, "x")]
        assert parse_lrc("[00:05]x") == [(5000, "x")]

    def test_minutes_over_59(self):
        assert parse_lrc("[100:00.00]x") == [(6_000_000, "x")]

    def test_metadata_and_garbage_ignored(self):
        assert parse_lrc("[ar:Artist]\n[ti:Title]\nplain text\n\n") == []

    def test_empty_text_line_kept(self):
        assert parse_lrc("[00:30.00]") == [(30000, "")]

    def test_empty_input(self):
        assert parse_lrc("") == []

    def test_crlf(self):
        assert parse_lrc("[00:01.00]a\r\n[00:02.00]b\r\n") == [(1000, "a"), (2000, "b")]


class TestPlain:
    def test_strips_stamps_and_meta(self):
        src = "[ar:A]\n[ti:T]\n[00:01.00]Line one\n[00:02.00][00:03.00]Line two\n"
        assert lrc_to_plain(src) == "Line one\nLine two"

    def test_no_stamps(self):
        assert lrc_to_plain("a\nb") == "a\nb"

    def test_empty(self):
        assert lrc_to_plain("") == ""


class TestIsSynced:
    def test_needs_three_stamped_lines(self):
        assert is_synced("[00:01.00]a\n[00:02.00]b\n[00:03.00]c")
        assert not is_synced("[00:01.00]a\n[00:02.00]b")

    def test_none_empty_plain(self):
        assert not is_synced(None)
        assert not is_synced("")
        assert not is_synced("just\nplain\ntext")
