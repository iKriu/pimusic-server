import os
from pathlib import Path

import pytest

from app import library
from app.db import User
from app.metadata import TrackMeta
from app.util import match_key


def add(db, cfg, name="01 Song.mp3", artist="Artist", title="Song", album="Album", **kw):
    p = cfg.library_dir / safe(artist) / album / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"audio")
    tid = db.add_track(match_key=kw.get("match_key", match_key(artist, title)), mbid=kw.get("mbid"),
                       isrc=kw.get("isrc"), artist=artist, title=title, album=album, duration=200,
                       path=str(p), source="test")
    return tid, p


def safe(s):
    return s.replace("/", "_")


def mkuser(db, login):
    db.upsert_user(User(login, hash(login) % 10_000, f"jf-{login}", f"lib-{login}", "tok", False))


class TestDb:
    def test_add_and_find_by_each_key(self, db, cfg):
        tid, _ = add(db, cfg, mbid="mb-1", isrc="ISRC1")
        assert db.find_track(mbid="mb-1").id == tid
        assert db.find_track(isrc="ISRC1").id == tid
        assert db.find_track(match_key=match_key("Artist", "Song")).id == tid
        assert db.find_track(mbid="nope") is None
        assert db.find_track() is None

    def test_alias_priority(self, db, cfg):
        t1, _ = add(db, cfg, "01 A.mp3", title="A", mbid="m1")
        t2, _ = add(db, cfg, "02 B.mp3", title="B", mbid="m2")
        db.add_alias("whatever user typed", t2)
        assert db.find_track(alias_key="whatever user typed", mbid="m1").id == t2
        assert db.find_track(alias_key="unknown", mbid="m1").id == t1
        db.add_alias("", t1)  # ignored
        assert db.find_track(alias_key="") is None

    def test_add_same_path_is_idempotent(self, db, cfg):
        tid, p = add(db, cfg)
        again = db.add_track(match_key="x", mbid=None, isrc=None, artist="a", title="t", album=None,
                             duration=None, path=str(p), source=None)
        assert again == tid
        assert db.stats()["tracks"] == 1

    def test_alias_removed_with_track(self, db, cfg):
        tid, _ = add(db, cfg)
        db.add_alias("k", tid)
        db.delete_track(tid)
        assert db.find_track(alias_key="k") is None

    def test_user_roundtrip_and_tg_rebinding(self, db):
        mkuser(db, "Bob")
        assert db.user_by_login("bob").login == "Bob"
        u = db.user_by_login("bob")
        db.upsert_user(User("alice", u.tg_id, "jf2", "lib2", "t2", False))
        assert db.user_by_tg(u.tg_id).login == "alice"
        assert db.user_by_login("Bob").tg_id is None

    def test_user_by_token(self, db):
        mkuser(db, "bob")
        assert db.user_by_token("BOB", "tok").login == "bob"
        assert db.user_by_token("bob", "wrong") is None

    def test_find_user_tracks_like(self, db, cfg):
        mkuser(db, "u")
        tid, p = add(db, cfg, artist="Daft Punk", title="Around")
        db.link_track("u", tid, str(p))
        assert [t.id for t, _ in db.find_user_tracks("u", "DAFT")] == [tid]
        assert db.find_user_tracks("u", "zzz") == []


def meta(**kw):
    base = dict(title="Song", artist="Artist", album_artist="Artist", album="Album", artists=["Artist"],
                track_no=3, track_total=10, date="2001-05-01")
    base.update(kw)
    return TrackMeta(**base)


class TestCachePath:
    def test_normal(self, cfg):
        p = library.cache_path_for(meta())
        assert p == cfg.library_dir / "Artist" / "Album" / "03 Song.mp3"

    def test_compilation_uses_various_artists(self, cfg):
        p = library.cache_path_for(meta(compilation=True, album_artist="DJ Mix", artist="Foo", title="Bar"))
        assert p == cfg.library_dir / "Various Artists" / "Album" / "03 Foo - Bar.mp3"

    def test_multi_disc_prefix(self, cfg):
        p = library.cache_path_for(meta(disc_no=1, disc_total=2, track_no=3))
        assert p.name == "1-03 Song.mp3"
        p = library.cache_path_for(meta(disc_no=2, disc_total=2, track_no=12))
        assert p.name == "2-12 Song.mp3"

    def test_no_track_number_no_prefix(self, cfg):
        assert library.cache_path_for(meta(track_no=0)).name == "Song.mp3"

    def test_no_year(self, cfg):
        assert library.cache_path_for(meta(date="")).parent.name == "Album"

    def test_hostile_names_stay_inside_library(self, cfg):
        p = library.cache_path_for(meta(album_artist="../../etc", album="../x", title="a/../../b"))
        assert cfg.library_dir.resolve() in p.resolve().parents

    def test_unique(self, cfg, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"1")
        assert library._unique(tmp_path / "b.mp3") == tmp_path / "b.mp3"
        assert library._unique(f) == tmp_path / "a (2).mp3"


class TestLinking:
    def test_hardlink_and_sidecars(self, db, cfg):
        mkuser(db, "bob")
        tid, p = add(db, cfg, "01 Song.mp3")
        (p.parent / "cover.jpg").write_bytes(b"jpg")
        p.with_suffix(".lrc").write_text("[00:01.00]x")
        track = db.track_by_path(str(p))
        link = library.link_to_user(track, "bob")
        assert link == cfg.users_dir / "bob" / "Artist" / "Album" / "01 Song.mp3"
        assert os.stat(p).st_nlink == 2
        assert os.path.samefile(p, link)
        assert (link.parent / "cover.jpg").read_bytes() == b"jpg"
        assert os.stat(p.parent / "cover.jpg").st_nlink == 2
        assert link.with_suffix(".lrc").read_text() == "[00:01.00]x"

    def test_relink_is_idempotent(self, db, cfg):
        tid, p = add(db, cfg)
        track = db.track_by_path(str(p))
        l1 = library.link_to_user(track, "bob")
        l2 = library.link_to_user(track, "bob")
        assert l1 == l2 and os.stat(p).st_nlink == 2

    def test_relink_replaces_stale_file(self, db, cfg):
        tid, p = add(db, cfg)
        track = db.track_by_path(str(p))
        dst = cfg.users_dir / "bob" / "Artist" / "Album" / "01 Song.mp3"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"stale")
        library.link_to_user(track, "bob")
        assert os.path.samefile(p, dst)

    def test_unlink_refcounting(self, db, cfg):
        for u in ("u1", "u2"):
            mkuser(db, u)
        tid, p = add(db, cfg)
        (p.parent / "cover.jpg").write_bytes(b"jpg")
        track = db.track_by_path(str(p))
        l1 = library.give_cached(db, track, "u1").link_path
        l2 = library.give_cached(db, track, "u2").link_path
        assert os.stat(p).st_nlink == 3
        assert db.track_refcount(tid) == 2

        library.unlink_from_user(db, track, "u1")
        assert not l1.exists() and l2.exists() and p.exists()
        assert db.track_refcount(tid) == 1
        assert db.track_by_path(str(p)) is not None
        assert not (cfg.users_dir / "u1").exists() or not any((cfg.users_dir / "u1").rglob("*.mp3"))

        library.unlink_from_user(db, track, "u2")
        assert not l2.exists()
        assert not p.exists()
        assert not p.parent.exists()  # empty album folder (with cover.jpg) removed
        assert db.track_by_path(str(p)) is None
        assert db.stats()["tracks"] == 0

    def test_unlink_keeps_other_tracks_in_album(self, db, cfg):
        mkuser(db, "u1")
        t1, p1 = add(db, cfg, "01 A.mp3", title="A")
        t2, p2 = add(db, cfg, "02 B.mp3", title="B")
        (p1.parent / "cover.jpg").write_bytes(b"jpg")
        for t in (t1, t2):
            library.give_cached(db, db.track_by_path(str(p1 if t == t1 else p2)), "u1")
        library.unlink_from_user(db, db.track_by_path(str(p1)), "u1")
        assert not p1.exists() and p2.exists()
        assert (p1.parent / "cover.jpg").exists()
        assert (cfg.users_dir / "u1" / "Artist" / "Album" / "cover.jpg").exists()

    def test_unlink_not_linked_user_does_not_delete_when_others_have_it(self, db, cfg):
        mkuser(db, "u1")
        mkuser(db, "u2")
        tid, p = add(db, cfg)
        track = db.track_by_path(str(p))
        library.give_cached(db, track, "u1")
        library.unlink_from_user(db, track, "u2")  # u2 never had it
        assert p.exists() and db.track_refcount(tid) == 1

    def test_lrc_removed_with_last_user(self, db, cfg):
        mkuser(db, "u1")
        tid, p = add(db, cfg)
        p.with_suffix(".lrc").write_text("[00:01.00]x")
        track = db.track_by_path(str(p))
        link = library.give_cached(db, track, "u1").link_path
        library.unlink_from_user(db, track, "u1")
        assert not p.with_suffix(".lrc").exists()
        assert not link.with_suffix(".lrc").exists()


class TestLookupCached:
    def test_hit_by_match_key(self, db, cfg):
        tid, p = add(db, cfg)
        assert library.lookup_cached(db, "Artist feat. X", "Song (Remastered)").id == tid

    def test_hit_by_alias(self, db, cfg):
        tid, _ = add(db, cfg)
        db.add_alias(match_key("Weird Query", "Thing"), tid)
        assert library.lookup_cached(db, "Weird Query", "Thing").id == tid

    def test_hit_by_isrc_without_artist(self, db, cfg):
        tid, _ = add(db, cfg, isrc="XX123")
        assert library.lookup_cached(db, "", "whatever", isrc="XX123").id == tid

    def test_no_artist_no_isrc_is_miss(self, db, cfg):
        add(db, cfg)
        assert library.lookup_cached(db, "", "Song") is None

    def test_missing_file_is_miss(self, db, cfg):
        tid, p = add(db, cfg)
        p.unlink()
        assert library.lookup_cached(db, "Artist", "Song") is None

    def test_unknown(self, db, cfg):
        assert library.lookup_cached(db, "Nobody", "Nothing") is None
