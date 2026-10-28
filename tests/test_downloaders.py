import os
import time

import pytest

from app import downloaders
from app.downloaders import _locate_slskd_file, _score_file, cleanup_after
from app.sources import TrackRequest

REQ = TrackRequest(artist="Radiohead", title="Creep", duration=238.0)


def mp3(name=r"@@u\Music\Radiohead\Pablo Honey\02 - Creep.mp3", **kw):
    f = {"filename": name, "extension": "mp3", "bitRate": 320, "isVariableBitRate": False, "length": 238,
         "size": 9_500_000}
    f.update(kw)
    return f


RESP = {"hasFreeUploadSlot": False, "queueLength": 0, "uploadSpeed": 0}


class TestScoreFile:
    def test_accepts_cbr320(self):
        assert _score_file(mp3(), RESP, REQ) is not None

    def test_rejects_vbr(self):
        assert _score_file(mp3(isVariableBitRate=True), RESP, REQ) is None

    @pytest.mark.parametrize("br", [128, 192, 256, 319])
    def test_rejects_low_bitrate(self, br):
        assert _score_file(mp3(bitRate=br), RESP, REQ) is None

    def test_unknown_bitrate_estimated_from_size(self):
        assert _score_file(mp3(bitRate=0, size=9_500_000), RESP, REQ) is not None
        assert _score_file(mp3(bitRate=0, size=3_000_000), RESP, REQ) is None

    def test_unknown_bitrate_no_size_rejected(self):
        assert _score_file(mp3(bitRate=0, size=0, length=0), RESP, TrackRequest(artist="Radiohead", title="Creep")) is None

    def test_flac_allowed(self, monkeypatch):
        from conftest import patch_cfg
        patch_cfg(monkeypatch, allow_flac_transcode=True)
        f = mp3(r"@@u\Radiohead\02 - Creep.flac", extension="flac", bitRate=0)
        s = _score_file(f, RESP, REQ)
        assert s is not None and s < _score_file(mp3(), RESP, REQ)

    def test_flac_disallowed(self, monkeypatch):
        from conftest import patch_cfg
        patch_cfg(monkeypatch, allow_flac_transcode=False)
        assert _score_file(mp3(r"@@u\Radiohead\Creep.flac", extension="flac"), RESP, REQ) is None

    @pytest.mark.parametrize("ext", ["ogg", "m4a", "wav", "wma"])
    def test_other_formats_rejected(self, ext):
        assert _score_file(mp3(rf"@@u\Radiohead\Creep.{ext}", extension=ext), RESP, REQ) is None

    def test_extension_derived_from_filename(self):
        f = mp3()
        del f["extension"]
        assert _score_file(f, RESP, REQ) is not None

    def test_wrong_title(self):
        assert _score_file(mp3(r"@@u\Radiohead\Karma Police.mp3"), RESP, REQ) is None

    def test_wrong_artist(self):
        assert _score_file(mp3(r"@@u\Stone Temple Pilots\Creep.mp3"), RESP, REQ) is None

    def test_artist_in_directory_counts(self):
        assert _score_file(mp3(r"@@u\Radiohead\Creep.mp3"), RESP, REQ) is not None

    @pytest.mark.parametrize("name", [
        "Radiohead - Creep (Remix).mp3", "Radiohead - Creep (Live).mp3", "Radiohead - Creep (Karaoke).mp3",
        "Radiohead - Creep (Cover).mp3", "Radiohead - Creep (Instrumental).mp3", "Radiohead - Creep (Sped Up).mp3",
        "Radiohead - Creep (Nightcore).mp3",
    ])
    def test_rejects_unrequested_variants(self, name):
        assert _score_file(mp3(rf"@@u\x\{name}"), RESP, REQ) is None

    def test_variant_allowed_when_requested(self):
        req = TrackRequest(artist="Radiohead", title="Creep (Live)", duration=238.0)
        assert _score_file(mp3(r"@@u\x\Radiohead - Creep (Live).mp3"), RESP, req) is not None

    def test_substring_false_positive_live_in_artist_name(self):
        req = TrackRequest(artist="Oliver Heldens", title="Gecko", duration=0)
        f = mp3(r"@@u\x\Oliver Heldens - Gecko.mp3", length=0)
        assert _score_file(f, RESP, req) is not None

    def test_duration_mismatch_rejected(self):
        assert _score_file(mp3(length=260), RESP, REQ) is None
        assert _score_file(mp3(length=240), RESP, REQ) is not None

    def test_prefers_free_upload_slot(self):
        free = _score_file(mp3(), {**RESP, "hasFreeUploadSlot": True}, REQ)
        busy = _score_file(mp3(), RESP, REQ)
        assert free > busy

    def test_queue_penalty_and_speed_bonus(self):
        base = _score_file(mp3(), RESP, REQ)
        assert _score_file(mp3(), {**RESP, "queueLength": 30}, REQ) < base
        assert _score_file(mp3(), {**RESP, "uploadSpeed": 5_000_000}, REQ) > base

    def test_closer_duration_scores_higher(self):
        assert _score_file(mp3(length=238), RESP, REQ) > _score_file(mp3(length=243), RESP, REQ)

    def test_free_text_request_requires_similarity(self):
        req = TrackRequest(query="radiohead creep")
        assert _score_file(mp3(r"@@u\Radiohead - Creep.mp3"), RESP, req) is not None
        assert _score_file(mp3(r"@@u\Other\Totally Different.mp3"), RESP, req) is None

    def test_free_text_request_matches_file_in_album_folder(self):
        req = TrackRequest(query="radiohead creep")
        assert _score_file(mp3(), RESP, req) is not None  # @@u\Music\Radiohead\Pablo Honey\02 - Creep.mp3

    def test_feat_in_request_ignored(self):
        req = TrackRequest(artist="Radiohead feat. Someone", title="Creep (feat. Other)", duration=238.0)
        assert _score_file(mp3(), RESP, req) is not None


class TestLocateSlskd:
    def test_glob_metachars_in_name(self, cfg):
        dest = "bot/abc123"
        d = cfg.slskd_downloads_dir / dest / "Folder [2020]"
        d.mkdir(parents=True)
        f = d / "01 - Song (feat. X) [HD] *?.mp3" if os.name != "nt" else d / "01 - Song (feat. X) [HD].mp3"
        f.write_bytes(b"x")
        remote = r"@@user\Music\Folder [2020]\\" + f.name
        assert _locate_slskd_file(remote, dest) == f

    def test_prefers_newest_and_dest_dir(self, cfg):
        dest = "bot/job1"
        old = cfg.slskd_downloads_dir / "Song.mp3"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        new = cfg.slskd_downloads_dir / dest / "Song.mp3"
        new.parent.mkdir(parents=True)
        new.write_bytes(b"new")
        os.utime(old, (time.time() + 100, time.time() + 100))  # even if root copy is newer, dest wins
        assert _locate_slskd_file(r"@@u\x\Song.mp3", dest) == new

    def test_fallback_to_root(self, cfg):
        cfg.slskd_downloads_dir.mkdir(parents=True)
        f = cfg.slskd_downloads_dir / "peer" / "Song.mp3"
        f.parent.mkdir()
        f.write_bytes(b"x")
        assert _locate_slskd_file(r"@@u\x\Song.mp3", "bot/missing") == f

    def test_fuzzy_prefix_match(self, cfg):
        dest = "bot/j"
        f = cfg.slskd_downloads_dir / dest / "Song_123.mp3"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        assert _locate_slskd_file(r"@@u\x\Song.mp3", dest) == f

    def test_not_found(self, cfg):
        assert _locate_slskd_file(r"@@u\x\Song.mp3", "bot/none") is None
        cfg.slskd_downloads_dir.mkdir(parents=True)
        assert _locate_slskd_file(r"@@u\x\Song.mp3", "bot/none") is None


class TestCleanupAfter:
    @pytest.mark.parametrize("tool", ["ytdlp", "spotdl", "sockseek"])
    def test_removes_only_own_job_dir(self, cfg, tool):
        a = cfg.incoming_dir / tool / "job_a"
        b = cfg.incoming_dir / tool / "job_b"
        for d in (a, b):
            d.mkdir(parents=True)
            (d / "x.mp3").write_bytes(b"x")
        cleanup_after(a / "x.mp3")
        assert not a.exists()
        assert (b / "x.mp3").exists()
        assert (cfg.incoming_dir / tool).exists()

    def test_nested_file_removes_job_root(self, cfg):
        a = cfg.incoming_dir / "sockseek" / "job_a"
        (a / "Artist" / "Album").mkdir(parents=True)
        f = a / "Artist" / "Album" / "x.mp3"
        f.write_bytes(b"x")
        other = cfg.incoming_dir / "sockseek" / "job_b"
        other.mkdir()
        cleanup_after(f)
        assert not a.exists() and other.exists()

    def test_slskd_batch_dir(self, cfg):
        a = cfg.slskd_downloads_dir / "bot" / "job_a"
        b = cfg.slskd_downloads_dir / "bot" / "job_b"
        for d in (a, b):
            (d / "Folder").mkdir(parents=True)
            (d / "Folder" / "x.mp3").write_bytes(b"x")
        cleanup_after(a / "Folder" / "x.mp3")
        assert not a.exists()
        assert (b / "Folder" / "x.mp3").exists()

    def test_old_slskd_peer_folder_removed_only_if_empty(self, cfg):
        peer = cfg.slskd_downloads_dir / "peerfolder"
        peer.mkdir(parents=True)
        (peer / "other.mp3").write_bytes(b"x")
        f = peer / "x.mp3"
        f.write_bytes(b"x")
        f.unlink()
        cleanup_after(f)
        assert peer.exists()  # sibling file belongs to another job
        (peer / "other.mp3").unlink()
        cleanup_after(f)
        assert not peer.exists()

    def test_unrelated_path_untouched(self, cfg, tmp_path):
        f = tmp_path / "elsewhere" / "x.mp3"
        f.parent.mkdir()
        f.write_bytes(b"x")
        cleanup_after(f)
        assert f.exists()

    def test_never_removes_incoming_root(self, cfg):
        cfg.incoming_dir.mkdir(parents=True)
        f = cfg.incoming_dir / "x.mp3"
        f.write_bytes(b"x")
        cleanup_after(f)
        assert cfg.incoming_dir.exists() and f.exists()
