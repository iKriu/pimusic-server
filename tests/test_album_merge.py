"""Альбомы не должны делиться на «Альбом (2010)» и «Альбом (2011)»."""
import os
import sys

import pytest
from pathlib import Path

from mutagen.id3 import ID3, TALB, TDRC, TPE2, TRCK, TXXX

from app import library, tagger
from app.db import User
from app.metadata import TrackMeta
from app.util import match_key


def _mp3(path: Path, album: str, year: str, track: int, mbid: str = "rel-1", album_artist: str = "Adele"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb\x90\x64" + b"\0" * 4000)
    t = ID3()
    t.add(TALB(encoding=3, text=album))
    t.add(TPE2(encoding=3, text=album_artist))
    t.add(TDRC(encoding=3, text=year))
    t.add(TRCK(encoding=3, text=f"{track}/11"))
    t.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=mbid))
    t.save(str(path), v2_version=4)


def test_album_folder_has_no_year(cfg):
    meta = TrackMeta(title="Hello", artist="Adele", album_artist="Adele", album="25", date="2015-11-20", track_no=1)
    assert library.cache_path_for(meta).parent.name == "25"


def test_harmonize_copies_album_tags_only(tmp_path):
    ref, new = tmp_path / "a.mp3", tmp_path / "b.mp3"
    _mp3(ref, "21", "2011", 1, mbid="rel-A")
    _mp3(new, "21", "2010", 5, mbid="rel-B", album_artist="Adele feat. X")
    tagger.harmonize_album_tags(new, ref)
    t = ID3(str(new))
    assert str(t["TDRC"]) == "2011" and str(t["TPE2"]) == "Adele"
    assert t["TXXX:MusicBrainz Album Id"].text == ["rel-A"]
    assert str(t["TRCK"]) == "5/11", "номер трека не трогаем"


def test_merge_split_albums_moves_files_links_and_db(db, cfg):
    db.upsert_user(User(login="alice", tg_id=1, jf_user_id="jf", library_id=None, stream_token="t", is_admin=False))
    lib = cfg.library_dir / "Adele"
    files = [(lib / "21 (2011)" / "01 Rolling in the Deep.mp3", "2011", 1),
             (lib / "21 (2011)" / "02 Rumour Has It.mp3", "2011", 2),
             (lib / "21 (2010)" / "04 Set Fire to the Rain.mp3", "2010", 4)]
    ids = []
    for f, year, n in files:
        _mp3(f, "21", year, n, mbid="rel-2011" if year == "2011" else "rel-2010")
        f.with_suffix(".lrc").write_text("[00:01.00]x\n[00:02.00]y\n[00:03.00]z\n", encoding="utf-8")
        (f.parent / "cover.jpg").write_bytes(b"jpg")
        tid = db.add_track(match_key=match_key("Adele", f.stem), mbid=None, isrc=None, artist="Adele", title=f.stem,
                           album="21", duration=200, path=str(f), source="t")
        t = db.track_by_path(str(f))
        link = library.link_to_user(t, "alice")
        db.link_track("alice", tid, str(link))
        ids.append(tid)

    assert library.merge_split_albums(db) == 3
    target = lib / "21"
    assert sorted(p.name for p in target.glob("*.mp3")) == [
        "01 Rolling in the Deep.mp3", "02 Rumour Has It.mp3", "04 Set Fire to the Rain.mp3"]
    assert not (lib / "21 (2010)").exists() and not (lib / "21 (2011)").exists()
    assert (target / "cover.jpg").exists()
    # теги выровнены по папке, где треков больше (2011)
    t = ID3(str(target / "04 Set Fire to the Rain.mp3"))
    assert str(t["TDRC"]) == "2011" and t["TXXX:MusicBrainz Album Id"].text == ["rel-2011"]
    # БД и жёсткие ссылки пользователя переехали
    user_album = cfg.users_dir / "alice" / "Adele" / "21"
    for tid in ids:
        tr = db.get_track(tid)
        assert Path(tr.path).parent == target
        link = Path(db.user_has_track("alice", tid))
        assert link.parent == user_album and os.path.samefile(link, tr.path)
        assert link.with_suffix(".lrc").exists()
    assert (user_album / "cover.jpg").exists()
    assert not (cfg.users_dir / "alice" / "Adele" / "21 (2010)").exists()
    # повторный запуск ничего не делает
    assert library.merge_split_albums(db) == 0


def test_merge_keeps_different_albums_apart(db, cfg):
    lib = cfg.library_dir / "Dua Lipa"
    _mp3(lib / "Dua Lipa (Deluxe) (2017)" / "01 New Rules.mp3", "Dua Lipa (Deluxe)", "2017", 1)
    _mp3(lib / "Future Nostalgia (2020)" / "05 Levitating.mp3", "Future Nostalgia", "2020", 5)
    library.merge_split_albums(db)
    assert sorted(p.name for p in lib.iterdir()) == ["Dua Lipa (Deluxe)", "Future Nostalgia"]


def test_merge_album_name_spelling_variants(db, cfg):
    lib = cfg.library_dir / "Fall Out Boy"
    _mp3(lib / "American Beauty_American Psycho" / "03 Centuries.mp3", "American Beauty/American Psycho", "2015", 3)
    _mp3(lib / "American Beauty_American Psycho" / "04 Uma Thurman.mp3", "American Beauty/American Psycho", "2015", 4)
    _mp3(lib / "American Beauty _ American Psycho" / "01 Irresistible.mp3", "American Beauty / American Psycho",
         "2015", 1)
    assert library.merge_split_albums(db) == 1
    assert [p.name for p in lib.iterdir()] == ["American Beauty_American Psycho"]
    t = ID3(str(lib / "American Beauty_American Psycho" / "01 Irresistible.mp3"))
    assert str(t["TALB"]) == "American Beauty/American Psycho"


def test_new_track_goes_into_existing_spelling_variant(cfg):
    (cfg.library_dir / "Fall Out Boy" / "American Beauty_American Psycho").mkdir(parents=True)
    meta = TrackMeta(title="Novocaine", artist="Fall Out Boy", album_artist="Fall Out Boy",
                     album="American Beauty / American Psycho", track_no=8)
    assert library.cache_path_for(meta).parent.name == "American Beauty_American Psycho"


# ---------------------------------------------------------------- дубли по исполнителю и сборникам
@pytest.mark.skipif(sys.platform == "win32", reason="NTFS не различает регистр — дубль воспроизводится только на Linux")
def test_merge_artist_case_variants(db, cfg):
    lib = cfg.library_dir
    _mp3(lib / "Panic! At the Disco" / "Too Weird" / "01 This Is Gospel.mp3", "Too Weird", "2013", 1,
         album_artist="Panic! At the Disco")
    _mp3(lib / "Panic! At the Disco" / "Pray" / "04 High Hopes.mp3", "Pray", "2018", 4, album_artist="Panic! At the Disco")
    _mp3(lib / "Panic! At The Disco" / "Too Weird" / "02 Miss Jackson.mp3", "Too Weird", "2013", 2,
         album_artist="Panic! At The Disco")
    assert library.merge_artist_variants(db) == 1
    assert [p.name for p in lib.iterdir()] == ["Panic! At the Disco"]
    assert len(list((lib / "Panic! At the Disco" / "Too Weird").glob("*.mp3"))) == 2


def test_merge_feat_artist_folder(db, cfg):
    lib = cfg.library_dir
    _mp3(lib / "Raving George" / "Other" / "01 A.mp3", "Other", "2016", 1, album_artist="Raving George")
    _mp3(lib / "Raving George feat. Oscar and the Wolf" / "You're Mine" / "01 You're Mine.mp3", "You're Mine",
         "2016", 1, album_artist="Raving George feat. Oscar and the Wolf")
    library.merge_artist_variants(db)
    assert sorted(p.name for p in lib.iterdir()) == ["Raving George"]
    assert (lib / "Raving George" / "You're Mine" / "01 You're Mine.mp3").exists()


def test_merge_compilation_split_by_release_id(db, cfg):
    lib = cfg.library_dir
    album = "Sing 2 (Original Motion Picture Soundtrack)"
    for n in (1, 2, 3):
        _mp3(lib / "Various Artists" / album / f"0{n} VA {n}.mp3", album, "2021", n, mbid="sing2",
             album_artist="Various Artists")
    _mp3(lib / "Tori Kelly" / album / "05 Tori.mp3", album, "2021", 5, mbid="sing2", album_artist="Tori Kelly")
    _mp3(lib / "Tori Kelly" / "Unbreakable Smile" / "01 Nobody Love.mp3", "Unbreakable Smile", "2015", 1,
         mbid="other", album_artist="Tori Kelly")
    assert library.merge_compilation_splits(db) == 1
    assert len(list((lib / "Various Artists" / album).glob("*.mp3"))) == 4
    assert not (lib / "Tori Kelly" / album).exists() and (lib / "Tori Kelly" / "Unbreakable Smile").exists()
    t = ID3(str(lib / "Various Artists" / album / "05 Tori.mp3"))
    assert str(t["TPE2"]) == "Various Artists"


def test_same_title_different_albums_not_merged(db, cfg):
    lib = cfg.library_dir
    _mp3(lib / "Various Artists" / "30" / "01 X.mp3", "30", "2020", 1, mbid="va-30", album_artist="Various Artists")
    _mp3(lib / "Adele" / "30" / "01 Strangers.mp3", "30", "2021", 1, mbid="adele-30", album_artist="Adele")
    assert library.merge_compilation_splits(db) == 0
    assert (lib / "Adele" / "30" / "01 Strangers.mp3").exists()


def test_new_track_joins_existing_compilation_folder(cfg):
    album = "Sing 2 (Original Motion Picture Soundtrack)"
    (cfg.library_dir / "Various Artists" / album).mkdir(parents=True)
    meta = TrackMeta(title="Let's Go Crazy", artist="Taron Egerton", album_artist="Taron Egerton", album=album,
                     track_no=7)
    assert library.cache_path_for(meta).parent == cfg.library_dir / "Various Artists" / album


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS не различает регистр — проверяется на Linux")
def test_new_track_reuses_artist_folder_case(cfg):
    (cfg.library_dir / "Panic! At the Disco").mkdir(parents=True)
    meta = TrackMeta(title="Victorious", artist="Panic! At The Disco", album_artist="Panic! At The Disco",
                     album="Death of a Bachelor", track_no=1)
    assert library.cache_path_for(meta).parent.parent.name == "Panic! At the Disco"


def test_feat_only_artist_folder_is_renamed(db, cfg):
    lib = cfg.library_dir
    _mp3(lib / "Raving George feat. Oscar and the Wolf" / "You're Mine" / "01 You're Mine.mp3", "You're Mine",
         "2016", 1, album_artist="Raving George feat. Oscar and the Wolf")
    _mp3(lib / "Oscar and the Wolf" / "You're Mine" / "01 You're Mine.mp3", "You're Mine", "2016", 1,
         mbid="otw", album_artist="Oscar and the Wolf")
    library.merge_artist_variants(db)
    assert sorted(p.name for p in lib.iterdir()) == ["Oscar and the Wolf", "Raving George"]
