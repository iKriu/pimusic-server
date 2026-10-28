"""Регрессии, найденные при живом тесте на одноплатнике."""
import asyncio

from app import downloaders
from app.sources import TrackRequest


def _f(name, br=320, length=None):
    return {"filename": name, "size": 9_000_000, "extension": "mp3", "bitRate": br, "isVariableBitRate": False,
            "length": length}


RESP = {"username": "peer", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1_000_000}


def test_radio_edit_in_brackets_rejected_when_not_requested():
    req = TrackRequest(artist="Daft Punk", title="Get Lucky")
    f = _f("@@x\\Daft Punk\\Hits\\01 - Get Lucky (Radio Edit) [feat. Pharrell Williams].mp3")
    assert downloaders._score_file(f, RESP, req) is None


def test_radio_edit_accepted_when_requested():
    req = TrackRequest(artist="Daft Punk", title="Get Lucky (Radio Edit)")
    f = _f("@@x\\Daft Punk\\Hits\\01 - Get Lucky (Radio Edit).mp3")
    assert downloaders._score_file(f, RESP, req) is not None


def test_album_version_preferred_by_mb_duration():
    req = TrackRequest(artist="Daft Punk", title="Get Lucky", duration=369, duration_from_mb=True)
    album = _f("@@x\\Daft Punk\\Random Access Memories\\08 Get Lucky.mp3", length=369)
    single = _f("@@x\\Daft Punk\\Singles\\Get Lucky.mp3", length=248)
    assert downloaders._score_file(album, RESP, req) is not None
    assert downloaders._score_file(single, RESP, req) is None


def test_mb_duration_tolerance_wider():
    req = TrackRequest(artist="A", title="Song", duration=200, duration_from_mb=True)
    assert downloaders._score_file(_f("@@x\\A\\Album\\01 Song.mp3", length=209), RESP, req) is not None
    req.duration_from_mb = False
    assert downloaders._score_file(_f("@@x\\A\\Album\\01 Song.mp3", length=209), RESP, req) is None


def test_oliver_not_treated_as_live():
    req = TrackRequest(artist="Oliver Heldens", title="Gecko")
    assert downloaders._score_file(_f("@@x\\Oliver Heldens\\Gecko.mp3"), RESP, req) is not None


def test_sockseek_input_with_comma_falls_back_to_free_text(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        from app.util import ProcResult
        return ProcResult(1, "", "no results")

    monkeypatch.setattr(downloaders, "run", fake_run)
    monkeypatch.setattr(downloaders, "_sockseek_bin", lambda: "/usr/local/bin/sockseek")
    from dataclasses import replace
    import app.config
    cfg = replace(app.config.CFG, sldl_username="u", sldl_password="p")
    monkeypatch.setattr(downloaders, "CFG", cfg)

    async def noop(_):
        return None

    asyncio.run(downloaders.soulseek_sockseek(TrackRequest(artist="Tyler, The Creator", title="EARFQUAKE"), noop))
    cmd = captured["cmd"]
    inp = cmd[cmd.index("--input") + 1]
    assert "artist=" not in inp and "Tyler" in inp and "EARFQUAKE" in inp


def test_free_text_query_does_not_leak_into_tags(monkeypatch, tmp_path):
    """Запрос «Panic! At the disco» скачал High Hopes, а теги взялись от «Into the Unknown»: подсказка для
    разметки должна строиться по самому файлу, а не по тексту запроса."""
    from app import library, service
    from app.metadata import Hint

    hint = library._hint_from_file(Hint(), {}, tmp_path / "011. Panic! At the Disco - High Hopes.mp3")
    assert (hint.artist, hint.title) == ("Panic! At the Disco", "High Hopes")
    hint2 = library._hint_from_file(Hint(), {"artist": "Panic! At the Disco", "title": "High Hopes"},
                                    tmp_path / "x.mp3")
    assert hint2.title == "High Hopes"

    captured = {}

    async def fake_fetch(req, progress):
        p = tmp_path / "011. Panic! At the Disco - High Hopes.mp3"
        p.write_bytes(b"x")
        return downloaders.Downloaded(p, "soulseek", "peer")

    async def fake_ingest(db, src, hint, *, source, login):
        captured["hint"] = hint
        raise RuntimeError("stop")

    monkeypatch.setattr(service.downloaders, "fetch", fake_fetch)
    monkeypatch.setattr(service.library, "ingest", fake_ingest)
    monkeypatch.setattr(service.library, "lookup_cached", lambda *a, **k: None)
    svc = service.Service.__new__(service.Service)
    svc.db = None
    svc._refresh_pending = {}
    job = service.Job(user=SimpleUser(), text="Panic! At the disco", notify=None,
                      tracks=[TrackRequest(title="Panic! At the disco", query="Panic! At the disco")])
    try:
        asyncio.run(svc._process(job, job.tracks[0]))
    except RuntimeError:
        pass
    assert captured["hint"].title == "" and captured["hint"].artist == ""


class SimpleUser:
    login = "alice"
    library_id = None


def test_spotdl_dash_only_target_skipped(monkeypatch):
    called = []

    async def fake_run(cmd, **kw):
        called.append(cmd)
        from app.util import ProcResult
        return ProcResult(1, "", "")

    monkeypatch.setattr(downloaders, "run", fake_run)
    monkeypatch.setattr(downloaders.shutil, "which", lambda x: "/usr/bin/" + x)

    async def noop(_):
        return None

    assert asyncio.run(downloaders.spotdl(TrackRequest(query="--"), noop)) is None
    assert not called
