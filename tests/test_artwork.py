"""Догрузка обложек и фото исполнителей (офлайн: http подменён фикстурами)."""
import base64
import io
import os

import pytest
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPE2, TXXX, USLT, Encoding
from PIL import Image

from app import artwork, http, library
from app.db import User
from app.jellyfin import Jellyfin, JellyfinError


def jpeg(color=(200, 30, 30), size=(600, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def make_mp3(path, *, album, album_artist, title="Song", artist=None, release="", group="", apic=None, lyrics=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 2000)
    t = ID3()
    enc = Encoding.UTF8
    t.add(TIT2(encoding=enc, text=title))
    t.add(TPE1(encoding=enc, text=artist or album_artist))
    t.add(TPE2(encoding=enc, text=album_artist))
    t.add(TALB(encoding=enc, text=album))
    if release:
        t.add(TXXX(encoding=enc, desc="MusicBrainz Album Id", text=release))
    if group:
        t.add(TXXX(encoding=enc, desc="MusicBrainz Release Group Id", text=group))
    if lyrics:
        t.add(USLT(encoding=enc, lang="XXX", desc="", text=lyrics))
    if apic:
        t.add(APIC(encoding=enc, mime="image/jpeg", type=3, desc="Cover", data=apic))
    t.save(str(path), v2_version=4, v1=0)
    return path


class Net:
    """Подмена http.get_json / get_bytes: json — функция (url, params) -> ответ, images — url -> bytes."""

    def __init__(self, monkeypatch, json=None, images=None):
        self.json_calls, self.byte_calls = [], []
        self.json = json or (lambda url, params: None)
        self.images = images or {}

        async def get_json(url, *, params=None, **kw):
            self.json_calls.append((url, params))
            return self.json(url, params or {})

        async def get_bytes(url, **kw):
            self.byte_calls.append(url)
            return self.images.get(url)

        monkeypatch.setattr(http, "get_json", get_json)
        monkeypatch.setattr(http, "get_bytes", get_bytes)


@pytest.fixture(autouse=True)
def _clear_misses():
    artwork._misses.clear()
    yield
    artwork._misses.clear()


def add_user_copy(db, login, f):
    if not db.user_by_login(login):
        add_users(db, login)
    tid = db.add_track(match_key=f.stem, mbid=None, isrc=None, artist="a", title=f.stem, album="x",
                       duration=1, path=str(f), source="test")
    track = db.track_by_path(str(f))
    link = library.link_to_user(track, login)
    db.link_track(login, tid, str(link))
    return link


# ---------------------------------------------------------------- обложки
async def test_cover_from_embedded_apic_and_embed_into_others(cfg, db, monkeypatch):
    net = Net(monkeypatch)
    d = cfg.library_dir / "Uru" / "アンビバレント"
    a = make_mp3(d / "01 A.mp3", album="アンビバレント", album_artist="Uru", apic=jpeg((10, 200, 10)))
    b = make_mp3(d / "02 B.mp3", album="アンビバレント", album_artist="Uru", title="B", release="rel-1",
                 lyrics="[00:01.00]la")
    img = await artwork.ensure_album_cover(d, db)
    assert img and (d / "cover.jpg").read_bytes() == img
    assert net.json_calls == [] and net.byte_calls == []  # сеть не нужна
    tb = ID3(str(b))
    assert tb.getall("APIC") and tb.getall("APIC")[0].data == img
    # прочие кадры не тронуты
    assert str(tb["TIT2"].text[0]) == "B" and str(tb["TXXX:MusicBrainz Album Id"].text[0]) == "rel-1"
    assert tb.getall("USLT")[0].text == "[00:01.00]la"
    assert len(ID3(str(a)).getall("APIC")) == 1  # у трека с обложкой вторая не появилась


async def test_existing_cover_returned_without_network(cfg, monkeypatch):
    net = Net(monkeypatch)
    d = cfg.library_dir / "X" / "Y"
    d.mkdir(parents=True)
    (d / "cover.jpg").write_bytes(b"old")
    assert await artwork.ensure_album_cover(d) == b"old"
    assert not net.json_calls and not net.byte_calls


async def test_cover_from_caa_release_then_group(cfg, db, monkeypatch):
    caa_group = "https://coverartarchive.org/release-group/rg-9/front-1200"
    net = Net(monkeypatch, images={caa_group: jpeg((1, 2, 250), (1400, 1200))})
    d = cfg.library_dir / "ZHU" / "BLACK MIDAS"
    make_mp3(d / "01 X.mp3", album="BLACK MIDAS", album_artist="ZHU", release="rel-9", group="rg-9")
    img = await artwork.ensure_album_cover(d, db)
    assert img
    assert net.byte_calls[:2] == ["https://coverartarchive.org/release/rel-9/front-1200", caa_group]
    assert Image.open(io.BytesIO(img)).size == (1000, 1000)  # квадрат, не больше 1000
    assert not net.json_calls


async def test_cover_from_deezer_album_japanese_title(cfg, db, monkeypatch):
    good = "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000-000000-80-0-0.jpg"

    def js(url, params):
        if "itunes" in url:
            return {"results": [{"collectionName": "Other Album", "artistName": "Mrs. GREEN APPLE",
                                 "artworkUrl100": "https://is1.mzstatic.com/x/100x100bb.jpg"}]}
        if url.endswith("/search/album"):
            return {"data": [
                {"title": "ケセラセラ", "artist": {"name": "Mrs. GREEN APPLE"}, "cover_xl": "https://bad/1.jpg",
                 "md5_image": "z"},
                {"title": "クスシキ", "artist": {"name": "Mrs. GREEN APPLE"}, "cover_xl": good, "md5_image": "abc"},
            ]}
        return None

    net = Net(monkeypatch, json=js, images={good: jpeg((5, 5, 5))})
    d = cfg.library_dir / "Mrs. GREEN APPLE" / "クスシキ"
    f = make_mp3(d / "01 クスシキ.mp3", album="クスシキ", album_artist="Mrs. GREEN APPLE", title="クスシキ")
    link = add_user_copy(db, "alice", f)
    img = await artwork.ensure_album_cover(d, db)
    assert img
    assert good in net.byte_calls and "https://bad/1.jpg" not in net.byte_calls
    q = next(p["q"] for u, p in net.json_calls if u.endswith("/search/album"))
    assert q == 'artist:"Mrs. GREEN APPLE" album:"クスシキ"'
    # обложка — жёсткой ссылкой в медиатеку пользователя
    user_cover = link.parent / "cover.jpg"
    assert user_cover.exists() and os.path.samefile(user_cover, d / "cover.jpg")
    assert ID3(str(f)).getall("APIC")


async def test_cover_from_itunes(cfg, monkeypatch):
    art = "https://is1.mzstatic.com/image/thumb/x/1000x1000bb.jpg"

    def js(url, params):
        if "itunes" in url:
            assert params["entity"] == "album"
            return {"results": [{"collectionName": "Apricot Princess", "artistName": "Rex Orange County",
                                 "artworkUrl100": "https://is1.mzstatic.com/image/thumb/x/100x100bb.jpg"}]}
        return None

    net = Net(monkeypatch, json=js, images={art: jpeg()})
    d = cfg.library_dir / "Rex Orange County" / "Apricot Princess"
    make_mp3(d / "01 A.mp3", album="Apricot Princess", album_artist="Rex Orange County")
    assert await artwork.ensure_album_cover(d)
    assert art in net.byte_calls


async def test_cover_from_deezer_track_fallback(cfg, monkeypatch):
    url = "https://cdn-images.dzcdn.net/images/cover/t1/1000x1000.jpg"

    def js(url_, params):
        if url_ == "https://api.deezer.com/search":
            return {"data": [{"title": "Song", "artist": {"name": "IOWA"},
                              "album": {"cover_xl": url, "md5_image": "t1"}}]}
        if "deezer" in url_:
            return {"data": []}
        return {"results": []}

    Net(monkeypatch, json=js, images={url: jpeg()})
    d = cfg.library_dir / "IOWA" / "Atomic Heart, Vol.6 (Original Game Soundtrack)"
    make_mp3(d / "01 Song.mp3", album=d.name, album_artist="IOWA", title="Song")
    assert await artwork.ensure_album_cover(d)


async def test_cover_sources_failing_do_not_raise(cfg, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(http, "get_json", boom)
    monkeypatch.setattr(http, "get_bytes", boom)
    d = cfg.library_dir / "A" / "B"
    make_mp3(d / "01 A.mp3", album="B", album_artist="A", release="r")
    assert await artwork.ensure_album_cover(d) is None
    assert not (d / "cover.jpg").exists()


# ---------------------------------------------------------------- исполнители
def _borns_search(url, params):
    if url.endswith("/search/artist"):
        return {"data": [
            {"name": "BORNS", "nb_fan": 10, "md5_image": "",
             "picture_xl": "https://e-cdns-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg"},
            {"name": "BØRNS", "nb_fan": 500000, "md5_image": "abc",
             "picture_xl": "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg"},
            {"name": "Borns and Friends Orchestra", "nb_fan": 9999999, "md5_image": "zzz",
             "picture_xl": "https://cdn-images.dzcdn.net/images/artist/zzz/1000x1000.jpg"},
        ]}
    return None


async def test_artist_image_from_deezer_with_placeholder_and_links(cfg, db, monkeypatch):
    good = "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg"
    net = Net(monkeypatch, json=_borns_search, images={good: jpeg((9, 9, 200), (1000, 800))})
    ad = cfg.library_dir / "BØRNS"
    f = make_mp3(ad / "Dopamine" / "01 Electric Love.mp3", album="Dopamine", album_artist="BØRNS")
    link = add_user_copy(db, "bob", f)
    img = await artwork.ensure_artist_image(ad, db)
    assert img and (ad / "folder.jpg").exists()
    assert net.byte_calls == [good]  # заглушка и чужой «Borns and Friends» отброшены
    assert Image.open(io.BytesIO(img)).size[0] == Image.open(io.BytesIO(img)).size[1]
    user_img = link.parent.parent / "folder.jpg"
    assert user_img.exists() and os.path.samefile(user_img, ad / "folder.jpg")


async def test_artist_name_matching_ascii_folder(cfg, monkeypatch):
    # латинская запись без Ø тоже находит исполнителя
    good = "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg"
    Net(monkeypatch, json=_borns_search, images={good: jpeg()})
    assert await artwork.fetch_artist_image("Borns")


async def test_various_artists_skipped(cfg, monkeypatch):
    net = Net(monkeypatch)
    ad = cfg.library_dir / "Various Artists"
    make_mp3(ad / "Hits" / "01 x.mp3", album="Hits", album_artist="Various Artists")
    assert await artwork.ensure_artist_image(ad) is None
    assert not net.json_calls


async def test_artist_placeholder_only_gives_nothing(cfg, monkeypatch):
    def js(url, params):
        return {"data": [{"name": "Nobody", "nb_fan": 1, "picture_xl":
                          "https://e-cdns-images.dzcdn.net/images/artist//1000x1000.jpg"}]}

    net = Net(monkeypatch, json=js)
    assert await artwork.fetch_artist_image("Nobody") is None
    assert not net.byte_calls


# ---------------------------------------------------------------- Jellyfin
class FakeJF:
    def __init__(self, albums, artists, fail_upload=()):
        self.albums, self.artists = albums, artists
        self.uploads: list[tuple[str, bytes]] = []
        self.fail_upload = set(fail_upload)

    async def items(self, user_id, **params):
        assert params["IncludeItemTypes"] == "MusicAlbum" and params["Fields"] == "Path"
        return self.albums

    async def request(self, method, path, *, params=None, **kw):
        assert (method, path) == ("GET", "/Artists")
        return {"Items": self.artists, "TotalRecordCount": len(self.artists)}

    async def upload_image(self, item_id, data, image_type="Primary"):
        if item_id in self.fail_upload:
            raise JellyfinError(500, "nope")
        self.uploads.append((item_id, data))


def add_users(db, *logins):
    for i, login in enumerate(logins):
        db.upsert_user(User(login=login, tg_id=100 + i, jf_user_id=f"jf-{login}", library_id=f"lib-{login}",
                            stream_token=f"tok{i}", is_admin=False))


async def test_sync_uploads_only_missing_and_maps_paths(cfg, db, monkeypatch):
    good = "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg"
    Net(monkeypatch, json=_borns_search, images={good: jpeg()})
    add_users(db, "alice", "bob")
    # у альбома в кэше обложка уже есть (Jellyfin её почему-то не подхватил)
    d1 = cfg.library_dir / "BØRNS" / "Dopamine"
    make_mp3(d1 / "01 a.mp3", album="Dopamine", album_artist="BØRNS")
    (d1 / "cover.jpg").write_bytes(jpeg((1, 1, 1)))
    # у второго — нет, но трек со вшитой обложкой
    d2 = cfg.library_dir / "Uru" / "アンビバレント"
    make_mp3(d2 / "01 b.mp3", album="アンビバレント", album_artist="Uru", apic=jpeg((3, 3, 3)))
    albums = [
        {"Id": "al1", "Name": "Dopamine", "Path": "/media/users/alice/BØRNS/Dopamine", "ImageTags": {}},
        {"Id": "al2", "Name": "アンビバレント", "Path": str(cfg.users_dir / "alice" / "Uru" / "アンビバレント"),
         "ImageTags": {}},
        {"Id": "al3", "Name": "Has", "Path": "/media/users/alice/X/Has", "ImageTags": {"Primary": "t"}},
        {"Id": "al4", "Name": "Gone", "Path": "/media/users/alice/Nope/Gone", "ImageTags": {}},
    ]
    artists = [
        {"Id": "ar1", "Name": "BØRNS", "ImageTags": {}},
        {"Id": "ar2", "Name": "Uru", "ImageTags": {"Primary": "t"}},
        {"Id": "ar3", "Name": "Various Artists", "ImageTags": {}},
    ]
    jf = FakeJF(albums, artists)
    counts = await artwork.sync_jellyfin_images(db, jf)
    ids = [i for i, _ in jf.uploads]
    assert sorted(ids) == ["al1", "al2", "ar1"]  # второй пользователь видит те же Id — не дублируем
    assert dict(jf.uploads)["al1"] == (d1 / "cover.jpg").read_bytes()
    assert (d2 / "cover.jpg").exists()  # обложка второго альбома записана в кэш
    assert (cfg.library_dir / "BØRNS" / "folder.jpg").exists()
    assert counts["albums"] == 2 and counts["artists"] == 1 and counts["albums_missing"] == 1


async def test_sync_errors_do_not_propagate(cfg, db, monkeypatch):
    Net(monkeypatch)
    add_users(db, "alice")
    d = cfg.library_dir / "A" / "B"
    make_mp3(d / "01 a.mp3", album="B", album_artist="A")
    (d / "cover.jpg").write_bytes(jpeg())

    class Broken(FakeJF):
        async def request(self, *a, **k):
            raise JellyfinError(500, "artists broken")

    jf = Broken([{"Id": "al1", "Path": "/media/users/alice/A/B", "ImageTags": {}}], [], fail_upload={"al1"})
    counts = await artwork.sync_jellyfin_images(db, jf)
    assert counts["errors"] == 2 and jf.uploads == []


def test_cache_dir_mapping(cfg):
    assert artwork.cache_dir_for_jf_path("/media/users/alice/Uru/アンビバレント") == \
        cfg.library_dir / "Uru" / "アンビバレント"
    assert artwork.cache_dir_for_jf_path(str(cfg.users_dir / "bob" / "A" / "B")) == cfg.library_dir / "A" / "B"
    assert artwork.cache_dir_for_jf_path("/media/users/alice") is None
    assert artwork.cache_dir_for_jf_path("/somewhere/else") is None
    assert artwork.cache_dir_for_jf_path("") is None


async def test_backfill_covers_artists_and_skips_recent_misses(cfg, db, monkeypatch):
    good = "https://cdn-images.dzcdn.net/images/artist/abc/1000x1000.jpg"
    net = Net(monkeypatch, json=_borns_search, images={good: jpeg()})
    make_mp3(cfg.library_dir / "BØRNS" / "Dopamine" / "01 a.mp3", album="Dopamine", album_artist="BØRNS",
             apic=jpeg((4, 4, 4)))
    make_mp3(cfg.library_dir / "Nobody" / "Nothing" / "01 n.mp3", album="Nothing", album_artist="Nobody")
    counts = await artwork.backfill(db, None)
    assert counts == {"covers": 1, "artist_images": 1}
    assert (cfg.library_dir / "BØRNS" / "Dopamine" / "cover.jpg").exists()
    assert (cfg.library_dir / "BØRNS" / "folder.jpg").exists()
    calls = len(net.json_calls)
    await artwork.backfill(db, None)  # то, что не нашлось, повторно не ищем сразу же
    assert len(net.json_calls) == calls


def test_needs_artwork(cfg):
    d = cfg.library_dir / "A" / "B"
    d.mkdir(parents=True)
    f = d / "01 x.mp3"
    assert artwork.needs_artwork(f)
    (d / "cover.jpg").write_bytes(b"x")
    assert artwork.needs_artwork(f)
    (d.parent / "folder.jpg").write_bytes(b"x")
    assert not artwork.needs_artwork(f)
    v = cfg.library_dir / "Various Artists" / "Hits"
    v.mkdir(parents=True)
    (v / "cover.jpg").write_bytes(b"x")
    assert not artwork.needs_artwork(v / "01 y.mp3")


# ---------------------------------------------------------------- Jellyfin.upload_image
class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "err"


class _Session:
    def __init__(self, status=204):
        self.calls = []
        self.status = status

    def post(self, url, *, data=None, headers=None):
        self.calls.append((url, data, headers))
        return _Resp(self.status)


async def test_upload_image_sends_base64_jpeg(monkeypatch):
    jf = Jellyfin("http://jf:8096/", token="KEY")
    s = _Session()

    async def fake_s():
        return s

    monkeypatch.setattr(jf, "_s", fake_s)
    data = jpeg()
    await jf.upload_image("abc123", data)
    url, body, headers = s.calls[0]
    assert url == "http://jf:8096/Items/abc123/Images/Primary"
    assert base64.b64decode(body) == data
    assert headers["Content-Type"] == "image/jpeg" and 'Token="KEY"' in headers["Authorization"]

    s.status = 500
    with pytest.raises(JellyfinError):
        await jf.upload_image("abc123", data)


# ---------------------------------------------------------------- транслитерация и запасные поиски
def test_prefer_cyrillic_from_hint():
    from app.metadata import Hint, TrackMeta, _prefer_hint_script
    m = TrackMeta(title="Kurtochka", artist="Moya Mishel", artists=["Moya Mishel"], album_artist="Moya Mishel",
                  album="Kurtochka")
    _prefer_hint_script(m, Hint(artist="Моя Мишель", title="Курточка"))
    assert (m.artist, m.album_artist, m.title, m.album) == ("Моя Мишель", "Моя Мишель", "Курточка", "Курточка")
    m2 = TrackMeta(title="Hello", artist="Adele", artists=["Adele"], album_artist="Adele", album="25")
    _prefer_hint_script(m2, Hint(artist="Adele", title="Hello"))
    assert m2.artist == "Adele"


def test_deezer_album_title_only_fallback(monkeypatch):
    import asyncio as _a
    from app import artwork

    async def fake_deezer(path, q):
        assert path == "/search/album" and q == "Курточка"
        return [{"title": "Курточка", "artist": {"name": "Моя Мишель"}, "cover_xl": "https://e/1.jpg",
                 "md5_image": "abc"}]

    async def fake_fetch(url):
        return b"img" if url == "https://e/1.jpg" else None

    monkeypatch.setattr(artwork, "_deezer", fake_deezer)
    monkeypatch.setattr(artwork, "_fetch_image", fake_fetch)
    info = {"album": "Курточка", "artist": "Moya Mishel", "title": "Курточка", "track_artist": ""}
    assert _a.run(artwork._from_deezer_album_title(info)) == b"img"
