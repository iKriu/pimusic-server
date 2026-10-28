"""Загрузка своих аудиофайлов: приём (бот, Mini App), конвейер опознания, need_info, уведомления, лимиты."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import acoustid, library, metadata, uploads, webapp
from app.db import User
from app.metadata import TrackMeta
from app.uploads import Uploads, UploadError
from app.util import AudioInfo

from test_webapp import TOKEN, FakeSvc, hdr
from test_bot_handlers import bot, ctx, make_cb, make_update  # noqa: F401 — фикстура bot

ALICE = 4242


def add_user(db, login="alice", tg_id=ALICE):
    u = User(login, tg_id, "jf-" + login, "lib-" + login, "st" + login, False)
    db.upsert_user(u)
    return u


class Messenger:
    def __init__(self):
        self.sent: list[tuple] = []
        self.n = 100

    async def send(self, chat_id, text, buttons=None, **_):
        self.n += 1
        self.sent.append((chat_id, text, buttons))
        return self.n


def info(*, artist="", title="", album="", codec="mp3", bitrate=320_000, duration=200.0, fmt=""):
    tags = {k: v for k, v in (("artist", artist), ("title", title), ("album", album)) if v}
    return AudioInfo(codec=codec, bitrate=bitrate, duration=duration, sample_rate=44100, tags=tags,
                     format_name=fmt or {"mp3": "mp3", "flac": "flac"}.get(codec, "mov,mp4,m4a,3gp,3g2,mj2"))


@pytest.fixture
def env(db, cfg, monkeypatch):
    """Uploads с поддельными probe/ingest/MusicBrainz/iTunes: ffmpeg и сеть не нужны."""
    user = add_user(db)
    svc = SimpleNamespace(messenger=Messenger(), _schedule_refresh=MagicMock(), loudness_changed=MagicMock(),
                          _schedule_artwork=MagicMock())
    up = Uploads(svc, db)
    state = SimpleNamespace(info=info(artist="Кино", title="Группа крови"), hints=[], reused=False, mb=None, it=None,
                            keep=[])

    async def fake_probe(path):
        return state.info if Path(path).is_file() else None

    async def fake_ingest(db_, path, hint, *, source, login, keep_mp3=False):
        state.hints.append(hint)
        state.keep.append(keep_mp3)
        Path(path).unlink()
        d = cfg.library_dir / (hint.artist or "Unknown") / "A"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{hint.title or 'x'}.mp3"
        dst.write_bytes(b"ID3")
        tid = db_.add_track(match_key=f"{hint.artist}|{hint.title}", mbid=None, isrc=None, artist=hint.artist,
                            title=hint.title, album=hint.album, duration=200, path=str(dst), source=source)
        db_.link_track(login, tid, str(cfg.users_dir / login / "x.mp3"))
        return library.IngestResult(db_.get_track(tid), state.reused, None, {}, dst)

    async def fake_mb(hint):
        return state.mb

    async def fake_it(hint):
        return state.it
    monkeypatch.setattr(uploads, "probe", fake_probe)
    monkeypatch.setattr(uploads.library, "ingest", fake_ingest)
    monkeypatch.setattr(uploads.metadata, "lookup_musicbrainz", fake_mb)
    monkeypatch.setattr(uploads.metadata, "lookup_itunes", fake_it)
    monkeypatch.setattr(uploads, "free_space_ok", lambda extra=0: True)
    return SimpleNamespace(db=db, up=up, user=user, svc=svc, st=state, cfg=cfg)


def put(env, name="song.mp3", *, data=b"ID3data", origin="bot", batch="b1", chat=ALICE):
    uid, path = env.up.begin(env.user, name, origin=origin, chat_id=chat, batch=batch)
    path.write_bytes(data)
    env.up.received(uid, len(data))
    return uid, path


async def run_one(env):
    row = env.db.claim_upload()
    assert row is not None
    return await env.up.process(row)


# ------------------------------------------------------------------ мелочи
def test_names_and_types():
    assert uploads.clean_name("../../etc/passwd") == "passwd"
    assert uploads.clean_name("C:\\x\\a\x00b.mp3") == "a b.mp3"
    assert uploads.clean_name(None) == "file"
    assert uploads.allowed_ext("A.FLAC") == ".flac" and uploads.allowed_ext("x.exe") is None
    assert uploads.allowed_ext("noext") is None
    assert uploads.guess_from_name("03. Кино - Группа крови (Official Audio).flac") == ("Кино", "Группа крови")
    assert uploads.guess_from_name("track01.wav") == ("", "track01")


def test_begin_paths_are_server_generated(env):
    uid, path = env.up.begin(env.user, "../../evil name.mp3", origin="app", chat_id=None, batch="")
    assert path.parent == uploads.upload_dir() and path.suffix == ".mp3" and "evil" not in path.name
    row = env.db.get_upload(uid)
    assert row["orig_name"] == "evil name.mp3" and row["state"] == "receiving"
    with pytest.raises(UploadError) as e:
        env.up.begin(env.user, "virus.exe", origin="app", chat_id=None, batch="")
    assert e.value.args[0] == "bad_type"


def test_accept_limits(env, monkeypatch):
    with pytest.raises(UploadError) as e:
        env.up.check_accept(env.user, 301 * 1024 * 1024)
    assert e.value.args[0] == "too_large"
    monkeypatch.setattr(uploads, "MAX_ACTIVE_PER_USER", 1)
    put(env)
    with pytest.raises(UploadError) as e:
        env.up.check_accept(env.user, 10)
    assert e.value.args[0] == "too_many"
    monkeypatch.setattr(uploads, "MAX_ACTIVE_PER_USER", 100)
    monkeypatch.setattr(uploads, "free_space_ok", lambda extra=0: False)
    with pytest.raises(UploadError) as e:
        env.up.check_accept(env.user, 10)
    assert e.value.args[0] == "disk_full"


def test_free_space_threshold(cfg, monkeypatch):
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda p: SimpleNamespace(free=6 * 1024 ** 3))
    assert uploads.free_space_ok() and not uploads.free_space_ok(2 * 1024 ** 3)


# ------------------------------------------------------------------ конвейер
async def test_tagged_file_is_saved(env):
    uid, path = put(env, "x.mp3")
    env.st.info = info(artist="Кино", title="Группа крови", album="Группа крови", bitrate=128_000)
    assert await run_one(env) == "done"
    row = env.db.get_upload(uid)
    assert row["track_id"] and row["guess_title"] == "Группа крови" and "128 kbps" in row["note"]
    h = env.st.hints[0]
    assert (h.artist, h.title, h.album, h.duration) == ("Кино", "Группа крови", "Группа крови", 200.0)
    assert env.st.keep == [True]  # MP3 не перекодируется (без «апскейла»)
    env.svc._schedule_refresh.assert_called_once()
    env.svc.loudness_changed.assert_called_once()
    assert env.svc.messenger.sent[-1][1].startswith("✅ Кино — Группа крови")


async def test_duplicate_and_not_audio(env):
    env.st.reused = True
    uid, _ = put(env, "a.mp3", batch="d1")
    assert await run_one(env) == "duplicate"
    assert env.db.get_upload(uid)["state"] == "duplicate"
    uid2, path2 = put(env, "b.flac", batch="d2")
    env.st.info = None
    assert await run_one(env) == "failed"
    assert "не аудио" in env.db.get_upload(uid2)["note"] and not path2.exists()


async def test_acoustid_used_when_no_tags(env, monkeypatch):
    from conftest import patch_cfg
    patch_cfg(monkeypatch, media_root=env.cfg.media_root, state_dir=env.cfg.state_dir, acoustid_api_key="k")
    env.st.info = info()

    async def fake_lookup(path, key):
        assert key == "k"
        return acoustid.Match("Земфира", "Хочешь", "Пятнадцать", 0.93)
    monkeypatch.setattr(uploads.acoustid, "lookup", fake_lookup)
    put(env, "unknown.wav")
    assert await run_one(env) == "done"
    h = env.st.hints[0]
    assert (h.artist, h.title, h.album) == ("Земфира", "Хочешь", "Пятнадцать")


async def test_filename_with_musicbrainz_duration_check(env):
    env.st.info = info(duration=286)
    env.st.mb = TrackMeta(title="Группа крови", artist="Кино", album_artist="Кино", album="Группа крови",
                          duration=287, isrc="RUA1")
    put(env, "01. кино - группа крови.flac")
    assert await run_one(env) == "done"
    assert (env.st.hints[0].artist, env.st.hints[0].title, env.st.hints[0].isrc) == ("Кино", "Группа крови", "RUA1")
    # длительность не та (другая запись) и iTunes ничего — спрашиваем пользователя
    env.st.mb = TrackMeta(title="Группа крови", artist="Кино", album_artist="Кино", album="Live", duration=400)
    uid, _ = put(env, "02 Кино - Группа крови.flac", batch="b2")
    assert await run_one(env) == "need_info"
    row = env.db.get_upload(uid)
    assert (row["guess_artist"], row["guess_title"]) == ("Кино", "Группа крови")
    chat, text, buttons = env.svc.messenger.sent[-1]
    assert "Не смог опознать «02 Кино - Группа крови.flac» (4:46)" in text
    assert buttons == [[("Оставить как есть", f"upl:keep:{uid}"), ("Отмена", f"upl:cancel:{uid}")]]
    assert row["prompt_msg"] == env.svc.messenger.n
    # iTunes с подходящей длительностью — сохраняем
    env.st.it = {"artistName": "Кино", "trackName": "Группа крови", "trackTimeMillis": 286500}
    put(env, "03 Кино - Группа крови.flac", batch="b3")
    assert await run_one(env) == "done"


async def test_need_info_answer_skip_cancel(env):
    env.st.info = info()
    uid, path = put(env, "track01.wav", batch="n1")
    assert await run_one(env) == "need_info"
    assert not env.up.provide_info("bob", uid, "A", "B")  # чужой
    assert env.up.provide_info("alice", uid, "Сплин", "Орбит без сахара", "Гранатовый альбом")
    assert not env.up.provide_info("alice", uid, "x", "y")  # уже в очереди
    assert await run_one(env) == "done"
    h = env.st.hints[-1]
    assert (h.artist, h.title, h.album) == ("Сплин", "Орбит без сахара", "Гранатовый альбом")
    assert env.svc.messenger.sent[-1][1].startswith("✅ Сплин — Орбит без сахара")
    # «Оставить как есть» — название из имени файла
    uid2, _ = put(env, "my recording.ogg", batch="n2")
    await run_one(env)
    assert env.up.skip("alice", uid2)
    await run_one(env)
    assert env.st.hints[-1].title == "my recording" and env.db.get_upload(uid2)["state"] == "done"
    # отмена
    uid3, p3 = put(env, "z.mp3", batch="n3")
    await run_one(env)
    assert env.up.cancel("alice", uid3) and not p3.exists()
    assert env.db.get_upload(uid3)["state"] == "cancelled" and not env.up.cancel("alice", uid3)


async def test_batch_summary_once(env):
    for i in range(3):
        put(env, f"f{i}.mp3", batch="grp")
    env.st.info = info(artist="A", title="T0")
    await run_one(env)
    env.st.info = info()  # второй — не опознан
    await run_one(env)
    assert len(env.svc.messenger.sent) == 1  # только вопрос по второму, итога ещё нет
    env.st.info = info(artist="A", title="T2")
    await run_one(env)
    texts = [t for _, t, _ in env.svc.messenger.sent]
    assert len(texts) == 2 and texts[1].startswith("📤 Загружено файлов: 3 — ✅ 2") and "❓ 1 ждут названия" in texts[1]
    uid = env.db.batch_uploads("grp")[1]["id"]
    env.up.provide_info("alice", uid, "B", "T1")
    await run_one(env)
    assert len(env.svc.messenger.sent) == 3 and env.svc.messenger.sent[-1][1] == "✅ B — T1"


async def test_app_uploads_notify_without_prompt(env):
    env.st.info = info()
    put(env, "x.mp3", origin="app", batch="app1")
    assert await run_one(env) == "need_info"
    assert env.svc.messenger.sent == []  # вопрос — в Mini App
    put(env, "y.mp3", origin="app", batch="app2", chat=None)
    env.st.info = info(artist="A", title="B")
    await run_one(env)
    assert env.svc.messenger.sent == []  # некуда писать


def test_restart_reset_and_claim(env):
    u1, _ = put(env, "a.mp3")
    u2, _ = put(env, "b.mp3")
    env.up.begin(env.user, "c.mp3", origin="bot", chat_id=1, batch="x")  # недокачан
    assert env.db.claim_upload()["id"] == u1
    assert env.db.claim_upload()["id"] == u2
    assert env.db.claim_upload() is None
    assert env.db.reset_uploads() == (2, 1)
    assert env.db.get_upload(u1)["state"] == "queued"


def test_cleanup_stale_and_orphans(env):
    env.st.info = info()
    uid, path = put(env, "a.mp3")
    env.db.set_upload(uid, state="need_info")
    env.db._x("UPDATE uploads SET updated_at=? WHERE id=?", (int(time.time()) - 20 * 86400, uid))
    orphan = uploads.upload_dir() / "orphan.mp3"
    orphan.write_bytes(b"x")
    keep_uid, keep = put(env, "b.mp3")
    old = time.time() - 2 * 86400
    import os
    for f in (orphan, keep):
        os.utime(f, (old, old))
    assert env.up.cleanup() == 2
    assert env.db.get_upload(uid)["state"] == "cancelled" and not path.exists()
    assert not orphan.exists() and keep.exists()


async def test_worker_processes_queue(env):
    env.st.info = info(artist="A", title="W")
    put(env, "w.mp3")
    env.up.start()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if env.st.hints:
            break
    await env.up.stop()
    assert env.st.hints and env.st.hints[0].title == "W"


def test_acoustid_best_match():
    data = {"status": "ok", "results": [
        {"score": 0.5, "recordings": [{"title": "Low", "artists": [{"name": "X"}]}]},
        {"score": 0.95, "recordings": [{"id": "r1", "title": "Хочешь", "artists": [{"name": "Земфира"}],
                                        "releasegroups": [{"type": "Single", "title": "S"},
                                                          {"type": "Album", "title": "Пятнадцать"}]}]}]}
    m = acoustid.best_match(data)
    assert (m.artist, m.title, m.album, m.mbid) == ("Земфира", "Хочешь", "Пятнадцать", "r1")
    assert acoustid.best_match({"results": [data["results"][0]]}) is None
    assert acoustid.best_match(None) is None


async def test_acoustid_lookup_without_key_or_fingerprint(tmp_path, monkeypatch):
    assert await acoustid.lookup(tmp_path / "x.mp3", "") is None

    async def no_fp(path):
        return None
    monkeypatch.setattr(acoustid, "fingerprint", no_fp)
    assert await acoustid.lookup(tmp_path / "x.mp3", "key") is None


async def test_ingest_keep_mp3_skips_transcode(db, cfg, monkeypatch, tmp_path):
    add_user(db)
    src = tmp_path / "in.mp3"
    src.write_bytes(b"ID3low")

    async def fake_probe(p):
        return info(artist="A", title="Low", bitrate=128_000)

    async def boom(*a, **k):
        raise AssertionError("не должен перекодировать")

    async def fake_resolve(hint):
        return TrackMeta(title="Low", artist="A", album_artist="A", album="Al")

    async def fake_tag(path, meta, work, **_):
        return {}
    monkeypatch.setattr(library, "probe", fake_probe)
    monkeypatch.setattr(library, "mp3_is_cbr320", boom)
    monkeypatch.setattr(library, "transcode_mp3_320", boom)
    monkeypatch.setattr(library.metadata, "resolve", fake_resolve)
    monkeypatch.setattr(library.tagger, "tag_file", fake_tag)
    res = await library.ingest(db, src, metadata.Hint(artist="A", title="Low"), source="upload", login="alice",
                               keep_mp3=True)
    assert Path(res.track.path).read_bytes() == b"ID3low"


# ------------------------------------------------------------------ Mini App
class UpSvc(FakeSvc):
    def __init__(self, db):
        super().__init__()
        self.uploads = Uploads(SimpleNamespace(messenger=None), db)


@pytest.fixture
async def client(db, cfg, monkeypatch):
    add_user(db)
    monkeypatch.setattr(uploads, "free_space_ok", lambda extra=0: True)
    app = web.Application()
    svc = UpSvc(db)
    webapp.setup_webapp(app, db=db, svc=svc, jf=MagicMock(), bot_token=TOKEN)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.db, c.up = db, svc.uploads
    yield c
    await c.close()


def form(*files):
    fd = aiohttp.FormData()
    for name, data in files:
        fd.add_field("files", data, filename=name, content_type="application/octet-stream")
    fd.add_field("note", "не файл", content_type="text/plain")  # поле без файла пропускается
    return fd


async def test_upload_endpoint_streams_to_disk(client, monkeypatch):
    from conftest import patch_cfg
    patch_cfg(monkeypatch, media_root=webapp.CFG.media_root, state_dir=webapp.CFG.state_dir, upload_max_mb=1)
    r = await client.post("/api/upload", data=form(("Кино - Звезда.mp3", b"ID3" * 1000), ("evil.exe", b"MZ"),
                                                   ("empty.ogg", b"")), headers=hdr())
    assert r.status == 200
    data = await r.json()
    assert [u["name"] for u in data["uploads"]] == ["Кино - Звезда.mp3"]
    assert {x["name"]: x["error"] for x in data["rejected"]} == {"evil.exe": "bad_type", "empty.ogg": "empty"}
    row = client.db.get_upload(data["uploads"][0]["id"])
    assert row["state"] == "queued" and row["size"] == 3000 and row["origin"] == "app" and row["chat_id"] == ALICE
    assert Path(row["path"]).read_bytes() == b"ID3" * 1000
    assert len(list(uploads.upload_dir().iterdir())) == 1  # отклонённые не оставили файлов
    # слишком большой файл — запрос обрывается сразу (остаток не дочитываем), принятое до него остаётся
    try:
        r = await client.post("/api/upload", data=form(("ok.mp3", b"ID3"), ("big.flac", b"\0" * (3 * 1024 * 1024))),
                              headers=hdr())
        assert r.status == 413 and r.headers.get("Connection", "").lower() == "close"
        data = await r.json()
        assert data["error"] == "too_large" and [u["name"] for u in data["uploads"]] == ["ok.mp3"]
    except aiohttp.ClientError:
        pass  # сервер закрыл соединение раньше, чем клиент дослал тело — тоже правильно
    assert not [r for r in client.db.user_uploads("alice") if r["orig_name"] == "big.flac" and r["state"] != "failed"]
    assert len(list(uploads.upload_dir().iterdir())) == 2
    r = await client.post("/api/upload", data=b"x", headers={**hdr(), "Content-Type": "text/plain"})
    assert r.status == 400
    r = await client.post("/api/upload", data=form(), headers=hdr())
    assert r.status == 400 and (await r.json())["error"] == "no_files"
    assert (await client.post("/api/upload", data=form(("a.mp3", b"1")))).status == 401


async def test_upload_concurrency_limits(client):
    w = client.app[webapp._STATE_KEY]
    w._upload_users["alice"] = webapp.UPLOAD_USER_CONCURRENCY
    r = await client.post("/api/upload", data=form(("a.mp3", b"ID3")), headers=hdr())
    assert r.status == 429 and (await r.json())["error"] == "busy"
    w._upload_users.clear()
    for _ in range(webapp.UPLOAD_GLOBAL_CONCURRENCY):
        await w._upload_global.acquire()
    try:
        r = await client.post("/api/upload", data=form(("a.mp3", b"ID3")), headers=hdr())
        assert r.status == 429
    finally:
        for _ in range(webapp.UPLOAD_GLOBAL_CONCURRENCY):
            w._upload_global.release()
    r = await client.post("/api/upload", data=form(("a.mp3", b"ID3")), headers=hdr())
    assert r.status == 200 and w._upload_users == {}


async def test_upload_user_quota(client, monkeypatch):
    from conftest import patch_cfg
    patch_cfg(monkeypatch, media_root=webapp.CFG.media_root, state_dir=webapp.CFG.state_dir, upload_user_quota_gb=1)
    uid = client.db.create_upload("alice", "x.mp3", "/nonexistent", size=1024 ** 3 - 100, state="queued")
    try:
        r = await client.post("/api/upload", data=form(("a.mp3", b"\0" * 1000)), headers=hdr())
        assert r.status == 413 and (await r.json())["error"] == "quota"
    except aiohttp.ClientError:
        pass
    client.db.set_upload(uid, state="done")  # обработан — квота освободилась
    assert (await client.post("/api/upload", data=form(("a.mp3", b"\0" * 1000)), headers=hdr())).status == 200


async def test_upload_disk_rechecked_while_writing(client, monkeypatch):
    monkeypatch.setattr(webapp, "UPLOAD_SPACE_STEP", 1000)
    calls = []

    def space(extra=0):
        calls.append(extra)
        return len(calls) < 2  # при приёме — место есть, при перепроверке посреди файла — уже нет
    monkeypatch.setattr(uploads, "free_space_ok", space)
    try:
        r = await client.post("/api/upload", data=form(("a.flac", b"\0" * 5000)), headers=hdr())
        assert r.status == 507 and (await r.json())["error"] == "disk_full"
    except aiohttp.ClientError:
        pass
    assert all(r["state"] == "failed" for r in client.db.user_uploads("alice"))


async def test_container_whitelist_and_generic_notes(env, monkeypatch, caplog):
    env.st.info = info(artist="A", title="B", fmt="hls,applehttp")
    uid, path = put(env, "playlist.mp3")
    assert await run_one(env) == "failed"
    assert env.db.get_upload(uid)["note"] == "это не аудио или формат не поддерживается" and not path.exists()

    async def boom(*a, **k):
        raise RuntimeError("секретный путь /media/library/x и стек")
    monkeypatch.setattr(uploads.library, "ingest", boom)
    env.st.info = info(artist="A", title="B")
    uid, _ = put(env, "b.mp3", batch="g2")
    assert await run_one(env) == "failed"
    assert env.db.get_upload(uid)["note"] == uploads.FAIL_TEXT and "секретный" in caplog.text


def test_stale_receiving_cleanup(env):
    uid, path = env.up.begin(env.user, "a.mp3", origin="bot", chat_id=1, batch="r")
    path.write_bytes(b"part")
    env.db._x("UPDATE uploads SET updated_at=? WHERE id=?", (int(time.time()) - 2 * 3600, uid))
    assert env.up.cleanup() >= 1
    assert env.db.get_upload(uid)["state"] == "failed" and not path.exists()


def test_quota_in_check_accept(env, monkeypatch):
    from conftest import patch_cfg
    patch_cfg(monkeypatch, media_root=env.cfg.media_root, state_dir=env.cfg.state_dir, upload_user_quota_gb=1)
    env.db.create_upload("alice", "x.mp3", "/x", size=1024 ** 3, state="need_info")
    with pytest.raises(UploadError) as e:
        env.up.check_accept(env.user, 10)
    assert e.value.args[0] == "quota"


def test_replace_upload_copy_relinks(db, cfg, tmp_path):
    add_user(db)
    d = cfg.library_dir / "A" / "Al"
    d.mkdir(parents=True)
    f = d / "t.mp3"
    f.write_bytes(b"UPLOADED")
    tid = db.add_track(match_key="a|t", mbid=None, isrc=None, artist="A", title="t", album="Al", duration=1,
                       path=str(f), source="upload")
    db.set_loudness(tid, -1.0, 0.5)
    link = library.link_to_user(db.get_track(tid), "alice")
    db.link_track("alice", tid, str(link))
    new = tmp_path / "dl.mp3"
    new.write_bytes(b"REAL320")
    library.replace_upload_copy(db, db.get_track(tid), new, "soulseek")
    t = db.get_track(tid)
    assert f.read_bytes() == b"REAL320" and link.read_bytes() == b"REAL320"
    assert t.source == "soulseek" and t.gain_db is None and db.loudness_pending() == 1


async def test_download_gives_uploaded_copy_without_fetching(db, cfg, monkeypatch):
    """Загруженный вручную файл главнее: трек выдаётся из кэша сразу, без скачивания из сети."""
    from app import service, sources
    u = add_user(db)
    add_user(db, "bob", 555)
    d = cfg.library_dir / "Кино" / "A"
    d.mkdir(parents=True)
    (d / "Звезда.mp3").write_bytes(b"UP")
    from app.util import match_key
    tid = db.add_track(match_key=match_key("Кино", "Звезда"), mbid=None, isrc=None, artist="Кино", title="Звезда",
                       album="A", duration=1, path=str(d / "Звезда.mp3"), source="upload")
    db.link_track("bob", tid, str(cfg.users_dir / "bob" / "x.mp3"))
    svc = service.Service(db, None)
    svc._schedule_refresh = lambda *a, **k: None
    fetched = []

    async def no_download(req, progress):
        fetched.append(req.title)
        return None
    monkeypatch.setattr(service.downloaders, "fetch", no_download)
    job = service.Job(user=u, text="x", notify=AsyncMock())
    out = await svc._process(job, sources.TrackRequest(artist="Кино", title="Звезда", duration=1))
    assert fetched == [] and out.ok and out.reused and db.user_has_track("alice", tid)
    assert (d / "Звезда.mp3").read_bytes() == b"UP"
    fetched.clear()
    out = await svc._process(job, sources.TrackRequest(artist="Кино", title="Звезда", duration=1))
    assert fetched == [] and "Уже в вашей медиатеке" in out.text


async def test_run_caps_output_and_keeps_stderr_tail(tmp_path):
    import sys
    from app import util
    code = ("import sys; sys.stdout.write('x' * 300000); "
            "sys.stderr.write('y' * 300000 + 'Summary: END')")
    r = await util.run([sys.executable, "-c", code], timeout=60, max_stdout=1000)
    assert len(r.out) == 1000 and r.err.endswith("Summary: END") and r.rc == 0
    assert util._rlimits(None) is None


async def test_upload_count_limit(client, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_FILES_PER_REQUEST", 2)
    r = await client.post("/api/upload", data=form(*[(f"{i}.mp3", b"ID3") for i in range(3)]), headers=hdr())
    data = await r.json()
    assert len(data["uploads"]) == 2 and data["rejected"][0]["error"] == "too_many"


async def test_upload_disk_full(client, monkeypatch):
    monkeypatch.setattr(uploads, "free_space_ok", lambda extra=0: False)
    data = await (await client.post("/api/upload", data=form(("a.mp3", b"ID3")), headers=hdr())).json()
    assert data["uploads"] == [] and data["rejected"][0]["error"] == "disk_full"


async def test_uploads_list_and_actions(client, db, cfg):
    up = client.up
    u = db.user_by_login("alice")
    uid, path = up.begin(u, "song.mp3", origin="app", chat_id=None, batch="")
    path.write_bytes(b"ID3")
    up.received(uid, 3)
    db.set_upload(uid, state="need_info", guess_artist="", guess_title="song", duration=61.0)
    items = (await (await client.get("/api/uploads", headers=hdr())).json())["items"]
    assert items[0]["state"] == "need_info" and items[0]["state_text"] == "ждёт названия"
    assert items[0]["title"] == "song" and items[0]["duration"] == 61.0 and items[0]["track"] is None
    r = await client.post(f"/api/uploads/{uid}/info", json={"artist": "", "title": "x"}, headers=hdr())
    assert r.status == 400
    r = await client.post(f"/api/uploads/{uid}/info", json={"artist": "Кино", "title": "Звезда"}, headers=hdr())
    assert r.status == 200 and (await r.json())["state"] == "queued"
    r = await client.post(f"/api/uploads/{uid}/skip", headers=hdr())
    assert r.status == 409  # уже не need_info
    assert (await client.post(f"/api/uploads/{uid}/cancel", headers=hdr())).status == 200
    assert not path.exists()
    assert (await client.post("/api/uploads/999/cancel", headers=hdr())).status == 404
    # готовая загрузка показывает трек
    d = cfg.library_dir / "K" / "A"
    d.mkdir(parents=True)
    (d / "t.mp3").write_bytes(b"ID3")
    tid = db.add_track(match_key="k|t", mbid=None, isrc=None, artist="K", title="t", album="A", duration=10,
                       path=str(d / "t.mp3"), source="upload")
    db.link_track("alice", tid, str(cfg.users_dir / "alice" / "K" / "A" / "t.mp3"))
    uid2 = db.create_upload("alice", "t.mp3", str(path), state="done")
    db.set_upload(uid2, track_id=tid)
    items = (await (await client.get("/api/uploads", headers=hdr())).json())["items"]
    assert items[0]["track"]["id"] == tid and items[0]["track"]["stream"]


# ------------------------------------------------------------------ бот
@pytest.fixture
def bot_up(bot, db, monkeypatch):  # noqa: F811
    add_user(db, "alice", 1)
    monkeypatch.setattr(uploads, "free_space_ok", lambda extra=0: True)
    bot.svc.uploads = Uploads(SimpleNamespace(messenger=None), db)
    return bot


def audio_update(name="Кино - Звезда.mp3", size=1000, group=None, data=b"ID3abc"):
    upd = make_update(tg_id=1)
    msg = upd.effective_message
    f = MagicMock()

    async def download(custom_path):
        Path(custom_path).write_bytes(data)
    f.download_to_drive = download
    att = SimpleNamespace(file_name=name, file_size=size, mime_type="audio/mpeg", performer=None, title=None,
                          get_file=AsyncMock(return_value=f))
    msg.audio, msg.document, msg.media_group_id, msg.chat_id, msg.message_id = att, None, group, 1, 77
    return upd


async def test_bot_accepts_audio(bot_up, db):
    upd = audio_update()
    await bot_up.on_audio_file(upd, ctx())
    row = db.user_uploads("alice")[0]
    assert row["state"] == "queued" and row["origin"] == "bot" and row["chat_id"] == 1 and row["batch"] == "tg:1:77"
    assert Path(row["path"]).read_bytes() == b"ID3abc"
    assert "Принял «Кино - Звезда.mp3»" in upd.effective_message.reply_text.call_args.args[0]
    # пачка (альбом сообщений) — одна пачка, без ответа на каждый файл
    g = audio_update("b.flac", group="G1")
    await bot_up.on_audio_file(g, ctx())
    assert db.user_uploads("alice")[0]["batch"] == "tg:1:G1"
    g.effective_message.reply_text.assert_not_awaited()


async def test_bot_rejects_big_and_bad_files(bot_up, db):
    upd = audio_update(size=25 * 1024 * 1024)
    await bot_up.on_audio_file(upd, ctx())
    assert "больше 20 МБ" in upd.effective_message.reply_html.call_args.args[0]
    upd = audio_update(name="x.exe")
    await bot_up.on_audio_file(upd, ctx())
    assert "не аудиофайл" in upd.effective_message.reply_text.call_args.args[0]
    assert db.user_uploads("alice") == []


def test_audio_name_from_mime():
    from app.bot import PiMusicBot
    att = SimpleNamespace(file_name=None, mime_type="audio/flac", performer="Кино", title="Звезда")
    assert PiMusicBot._audio_name(att) == "Кино - Звезда.flac"
    assert PiMusicBot._audio_name(SimpleNamespace(file_name=None, mime_type="", performer=None,
                                                  title=None)) == "audio.mp3"


async def test_bot_reply_and_buttons(bot_up, db):
    up = bot_up.svc.uploads
    u = db.user_by_login("alice")
    uid, path = up.begin(u, "rec.mp3", origin="bot", chat_id=1, batch="x")
    path.write_bytes(b"ID3")
    up.received(uid, 3)
    db.set_upload(uid, state="need_info", prompt_msg=555)
    upd = make_update(tg_id=1, text="Кино — Группа крови — Группа крови")
    msg = upd.effective_message
    msg.chat_id, msg.reply_to_message = 1, SimpleNamespace(message_id=555)
    await bot_up.on_text(upd, ctx())
    row = db.get_upload(uid)
    assert row["state"] == "queued" and (row["guess_artist"], row["guess_title"], row["guess_album"]) == (
        "Кино", "Группа крови", "Группа крови")
    bot_up.svc.submit.assert_not_awaited()
    # неправильный формат — подсказка, обычный поиск не запускается
    db.set_upload(uid, state="need_info")
    upd = make_update(tg_id=1, text="просто текст")
    upd.effective_message.chat_id, upd.effective_message.reply_to_message = 1, SimpleNamespace(message_id=555)
    await bot_up.on_text(upd, ctx())
    assert "Исполнитель — Название" in upd.effective_message.reply_text.call_args.args[0]
    # кнопки
    cb = make_cb(f"upl:keep:{uid}", tg_id=1)
    await bot_up.on_upload_choice(cb, ctx())
    assert db.get_upload(uid)["state"] == "queued" and db.get_upload(uid)["manual"] == 1
    db.set_upload(uid, state="need_info")
    cb = make_cb(f"upl:cancel:{uid}", tg_id=1)
    await bot_up.on_upload_choice(cb, ctx())
    assert db.get_upload(uid)["state"] == "cancelled" and not path.exists()
    assert "отменена" in cb.callback_query.edit_message_text.call_args.args[0]
