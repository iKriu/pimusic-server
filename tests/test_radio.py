import random
from pathlib import Path

import pytest

from app import radio
from app.db import User
from app.radio import Picker, valid_station, write_m3u


class TestValidStation:
    def test_ok(self):
        assert valid_station("Radio X", "https://stream.example.com/live.mp3")
        assert valid_station("Radio X", "http://8.8.8.8:8000/live")

    @pytest.mark.parametrize("name,url", [
        ("Bad\nname", "https://example.com/a"),
        ("Bad\rname", "https://example.com/a"),
        ("name", "https://example.com/a\n#EXTINF:-1,evil"),
        ("name", "https://example.com/a\x00"),
        ("", "https://example.com/a"),
        ("n" * 101, "https://example.com/a"),
        ("name", "https://example.com/" + "a" * 2000),
        ("name", "http://localhost:8096/x"),
        ("name", "http://jellyfin:8096/x"),
        ("name", "http://slskd:5030/x"),
        ("name", "http://127.0.0.1/x"),
        ("name", "http://192.168.0.10/x"),
        ("name", "http://10.1.2.3/x"),
        ("name", "http://169.254.169.254/x"),
        ("name", "http://[::1]/x"),
        ("name", "http://nas.local/x"),
        ("name", "http://x.internal/x"),
        ("name", "file:///etc/passwd"),
        ("name", "ftp://example.com/x"),
        ("name", "not a url"),
        ("name", "http:///nohost"),
    ])
    def test_rejected(self, name, url):
        assert not valid_station(name, url)

    @pytest.mark.parametrize("url", ["http://redis/x", "http://foo.localhost/x", "http://2130706433/x"])
    def test_other_internal_forms(self, url):
        assert not valid_station("name", url)


class TestWriteM3u:
    def test_format_and_skips_invalid(self, db, cfg, tmp_path):
        db.add_radio(None, "Good FM", "https://a.example.com/s.mp3", str(tmp_path / "1.strm"))
        db.add_radio(None, "Evil\nFM", "https://b.example.com/s.mp3", str(tmp_path / "2.strm"))
        db.add_radio(None, "Internal", "http://jellyfin:8096/x", str(tmp_path / "3.strm"))
        db.add_radio(None, 'Rock, "Hits"', "https://c.example.com/s.mp3", str(tmp_path / "4.strm"))
        db.add_radio("someuser", "Private", "https://d.example.com/s.mp3", str(tmp_path / "5.strm"))
        p = write_m3u(db)
        assert p == cfg.radio_dir / "radio.m3u"
        lines = p.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "#EXTM3U"
        body = lines[1:]
        assert len(body) % 2 == 0
        pairs = list(zip(body[0::2], body[1::2]))
        urls = [u for _, u in pairs]
        assert urls == ["https://a.example.com/s.mp3", "https://c.example.com/s.mp3"]
        for inf, url in pairs:
            assert inf.startswith("#EXTINF:-1 ")
            assert 'group-title="Радио",' in inf
            assert url.startswith("https://")
        assert pairs[0][0].endswith(",Good FM")
        title = pairs[1][0].rsplit(",", 1)[1]
        assert '"' not in title and "," not in title and "Rock" in title and "Hits" in title
        assert "Evil" not in p.read_text(encoding="utf-8")
        assert not p.with_suffix(".tmp").exists()

    def test_empty_db(self, db):
        p = write_m3u(db)
        assert p.read_text(encoding="utf-8") == "#EXTM3U\n"

    def test_wave_in_livetv(self, db, monkeypatch):
        from conftest import patch_cfg
        patch_cfg(monkeypatch, wave_in_livetv=True)
        db.upsert_user(User("bob", 1, "jf1", "lib", "tok123", False))
        text = write_m3u(db).read_text(encoding="utf-8")
        assert 'tvg-id="wave-bob"' in text
        assert "/stream/bob/tok123.mp3" in text

    def test_default_stations_all_valid(self):
        for name, url in radio.DEFAULT_STATIONS:
            assert valid_station(name, url)


class FakeJF:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def audio_items(self, user_id, **kw):
        self.calls += 1
        return list(self.items)


def make_items(tmp_path, n, favorites=(), same_artist=False):
    items = []
    for i in range(n):
        f = tmp_path / f"t{i}.mp3"
        f.write_bytes(b"x")
        items.append({
            "Id": f"id{i}", "Name": f"T{i}", "Path": str(f),
            "Artists": ["Same" if same_artist else f"Artist{i}"], "Genres": [f"g{i % 5}"],
            "UserData": {"IsFavorite": i in favorites, "PlayCount": 5 if i in favorites else 0},
        })
    return items


USER = User("bob", 1, "jf1", "lib", "tok", False)


class TestPicker:
    async def test_favorites_picked_more_often(self, tmp_path):
        random.seed(1234)
        favs = {0, 1, 2}
        picker = Picker(FakeJF(make_items(tmp_path, 60, favs)), USER)
        counts: dict[str, int] = {}
        for _ in range(1500):
            it = await picker.next()
            counts[it["Id"]] = counts.get(it["Id"], 0) + 1
        fav_avg = sum(counts.get(f"id{i}", 0) for i in favs) / len(favs)
        other_avg = sum(counts.get(f"id{i}", 0) for i in range(60) if i not in favs) / 57
        assert fav_avg > other_avg * 1.2

    async def test_weights_favorite_higher(self, tmp_path):
        picker = Picker(FakeJF(make_items(tmp_path, 10, {3})), USER)
        await picker._load()
        w = dict(zip((i["Id"] for i in picker.items), picker.weights))
        assert w["id3"] > max(v for k, v in w.items() if k != "id3")
        assert all(v > 0 for v in w.values())

    async def test_dislike_lowers_weight(self, tmp_path):
        items = make_items(tmp_path, 3)
        items[1]["UserData"]["Likes"] = False
        picker = Picker(FakeJF(items), USER)
        await picker._load()
        assert picker.weights[1] < picker.weights[0] * 0.2

    async def test_no_immediate_repeats_large_catalog(self, tmp_path):
        random.seed(7)
        picker = Picker(FakeJF(make_items(tmp_path, 60, {0})), USER)
        prev = None
        for _ in range(600):
            it = await picker.next()
            assert it["Id"] != prev
            prev = it["Id"]

    async def test_no_repeats_within_recent_window(self, tmp_path):
        random.seed(3)
        picker = Picker(FakeJF(make_items(tmp_path, 60)), USER)
        seq = [(await picker.next())["Id"] for _ in range(200)]
        for i in range(len(seq)):
            assert seq[i] not in seq[max(0, i - 40):i]

    async def test_no_immediate_repeats_small_catalog(self, tmp_path):
        random.seed(11)
        picker = Picker(FakeJF(make_items(tmp_path, 4)), USER)
        prev = None
        for _ in range(300):
            it = await picker.next()
            assert it["Id"] != prev
            prev = it["Id"]

    async def test_same_artist_penalised(self, tmp_path):
        random.seed(5)
        items = make_items(tmp_path, 20)
        for it in items[:10]:
            it["Artists"] = ["Dup"]
        picker = Picker(FakeJF(items), USER)
        same_in_row = total = 0
        last = None
        for _ in range(300):
            a = (await picker.next())["Artists"][0]
            total += 1
            same_in_row += a == last == "Dup"
            last = a
        # half the catalog is "Dup": without penalty ~25% of picks would be Dup->Dup
        assert same_in_row / total < 0.2

    async def test_empty_catalog(self):
        assert await Picker(FakeJF([]), USER).next() is None

    async def test_missing_files_filtered(self, tmp_path):
        items = make_items(tmp_path, 3)
        Path(items[0]["Path"]).unlink()
        items[1]["Path"] = str(tmp_path / "x.txt")
        (tmp_path / "x.txt").write_bytes(b"x")
        picker = Picker(FakeJF(items), USER)
        for _ in range(10):
            assert (await picker.next())["Id"] == "id2"

    async def test_load_failure_returns_none(self):
        class Boom:
            async def audio_items(self, *a, **k):
                raise RuntimeError("down")
        assert await Picker(Boom(), USER).next() is None

    async def test_refresh_wave_playlist_unique_ids(self, tmp_path):
        random.seed(9)
        replaced = {}

        class JF(FakeJF):
            async def replace_playlist(self, uid, name, ids, old_id=None):
                replaced["ids"] = ids

        n = await radio.refresh_wave_playlist(JF(make_items(tmp_path, 8)), USER, size=5)
        assert n == 5 == len(replaced["ids"]) == len(set(replaced["ids"]))
