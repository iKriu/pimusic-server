"""Исправления по ревью 2026-10-20: сохранность медиатеки, SSRF, гонки кэша, импорт, воркеры, секреты, квоты,
раздача Soulseek."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import http, library, loudness, radio, service, sources, util
from app.db import User
from app.service import Service, TrackOutcome
from app.sources import TrackRequest


def add_user(db, login="alice", tg_id=1):
    u = User(login, tg_id, "jf-" + login, None, "st" + login, False)
    db.upsert_user(u)
    return u


def cache_track(db, cfg, artist, title, *, source="test"):
    d = cfg.library_dir / artist / "Album"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{title}.mp3"
    p.write_bytes(b"ID3")
    tid = db.add_track(match_key=util.match_key(artist, title), mbid=None, isrc=None, artist=artist, title=title,
                       album="Album", duration=100, path=str(p), source=source)
    return db.get_track(tid)


def give(db, track, login):
    return library.give_cached(db, track, login)


def svc_for(db):
    svc = Service(db, None)
    svc._schedule_refresh = lambda *a, **k: None
    return svc


# ------------------------------------------------------------------ P0-1: reconcile_deleted
async def test_reconcile_skips_when_users_dir_missing(db, cfg):
    add_user(db)
    t = cache_track(db, cfg, "A", "T")
    give(db, t, "alice")
    import shutil
    shutil.rmtree(cfg.users_dir)  # диск с медиатеками не смонтировался
    svc = svc_for(db)
    for _ in range(3):
        assert await svc.reconcile_deleted() == 0
    assert db.user_has_track("alice", t.id) and Path(t.path).exists()


async def test_reconcile_skips_login_without_root_and_mass_loss(db, cfg):
    add_user(db, "alice", 1)
    add_user(db, "bob", 2)
    tracks = [cache_track(db, cfg, "A", f"T{i}") for i in range(15)]
    for t in tracks:
        give(db, t, "alice")
    give(db, tracks[0], "bob")
    import shutil
    shutil.rmtree(library.user_root("bob"))  # папки bob нет вовсе — его не трогаем
    for t in tracks[:12]:  # у alice пропало 12 из 15 (> 20 % и > 10) — это сбой, не удаление
        Path(db.user_has_track("alice", t.id)).unlink()
    svc = svc_for(db)
    for _ in range(3):
        assert await svc.reconcile_deleted() == 0
    assert all(db.user_has_track("alice", t.id) for t in tracks) and db.user_has_track("bob", tracks[0].id)


async def test_reconcile_deletes_only_after_two_passes(db, cfg):
    add_user(db)
    tracks = [cache_track(db, cfg, "A", f"T{i}") for i in range(5)]
    for t in tracks:
        give(db, t, "alice")
    link = Path(db.user_has_track("alice", tracks[0].id))
    link.unlink()
    svc = svc_for(db)
    assert await svc.reconcile_deleted() == 0  # первый раз — только кандидат
    link.parent.mkdir(parents=True, exist_ok=True)
    os.link(tracks[0].path, link)  # файл вернулся (сеть/диск моргнул) — кандидат снимается
    assert await svc.reconcile_deleted() == 0
    link.unlink()
    assert await svc.reconcile_deleted() == 0
    assert await svc.reconcile_deleted() == 1
    assert db.user_has_track("alice", tracks[0].id) is None


# ------------------------------------------------------------------ P0-3: SSRF
@pytest.fixture
def dns(monkeypatch):
    table = {}

    async def fake(host):
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]
    monkeypatch.setattr(util, "resolve_host", fake)
    return table


async def test_resolved_url_must_be_public(dns):
    dns.update({"good.example": {"93.184.216.34"}, "lan.example": {"192.168.1.20"},
                "mixed.example": {"93.184.216.34", "10.88.0.1"}, "cgnat.example": {"100.64.1.2"},
                "mapped.example": {"::ffff:10.0.0.1"}, "v6.example": {"2606:2800:220:1::1"}})
    assert await util.is_public_url_resolved("https://good.example/x")
    assert await util.is_public_url_resolved("https://v6.example/x")
    for host in ("lan.example", "mixed.example", "cgnat.example", "mapped.example", "missing.example"):
        assert not await util.is_public_url_resolved(f"https://{host}/x"), host
    assert not await util.is_public_url_resolved("http://10.88.0.1:3128/")
    assert not await util.is_public_url_resolved("http://jellyfin:8096/")  # внутренние имена — никогда


async def test_get_bytes_rechecks_every_redirect(dns, monkeypatch):
    dns.update({"cdn.example": {"93.184.216.34"}, "evil.example": {"192.168.0.1"}})
    calls = []

    class Resp:
        def __init__(self, url):
            self.url = url
            self.status = 302 if "cdn" in url else 200
            self.headers = {"Location": "http://evil.example/admin"}
            self.content = SimpleNamespace(iter_chunked=self._chunks)

        async def _chunks(self, n):
            yield b"secret"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        def get(self, url, allow_redirects=True):
            assert allow_redirects is False
            calls.append(url)
            return Resp(url)
    monkeypatch.setattr(http, "session", lambda: Sess())
    assert await http.get_bytes("https://cdn.example/cover.jpg") is None
    assert calls == ["https://cdn.example/cover.jpg"]  # на внутренний адрес запрос не ушёл


async def test_resolve_url_and_ytdlp_reject_private_hosts(dns, monkeypatch):
    dns.update({"intranet.example": {"10.0.0.5"}})
    r = await sources.resolve_url("https://intranet.example/track", "url")
    assert r.error
    run = AsyncMock()
    monkeypatch.setattr(service.downloaders, "run", run)
    monkeypatch.setattr(service.downloaders.shutil, "which", lambda _x: "/usr/bin/yt-dlp")
    assert await service.downloaders.ytdlp(TrackRequest(direct_url="https://intranet.example/v"), AsyncMock()) is None
    run.assert_not_awaited()


# ------------------------------------------------------------------ P1: гонка выдачи из кэша и очистки
def test_give_cached_fails_cleanly_when_track_gone(db, cfg):
    add_user(db)
    t = cache_track(db, cfg, "A", "T")
    db.delete_track(t.id)  # очистка успела удалить трек
    with pytest.raises(library.TrackGone):
        give(db, t, "alice")
    assert db.user_has_track("alice", t.id) is None
    assert not library.user_link_path(t, "alice").exists()  # сиротской ссылки нет


def test_give_cached_rolls_back_row_on_fs_error(db, cfg, monkeypatch):
    add_user(db)
    t = cache_track(db, cfg, "A", "T")

    def boom(*a, **k):
        raise OSError("read-only fs")
    monkeypatch.setattr(library, "link_to_user", boom)
    with pytest.raises(OSError):
        give(db, t, "alice")
    assert db.user_has_track("alice", t.id) is None


# ------------------------------------------------------------------ P1: пустые ключи, импорт
def test_empty_normalized_keys_never_match(db, cfg):
    t = cache_track(db, cfg, "!!!", "%%%")  # имена без букв: нормализованный ключ пустой
    assert library.find_same_recording(db, artist="!!!", title="%%%") is None
    assert library.find_same_recording(db, artist="@@@", title="###") is None
    assert library.usable_key("кино|звезда") and not library.usable_key("|") and not library.usable_key("a|")
    (cfg.library_dir / "~~~").mkdir()
    assert library._existing_artist_dir("@@@") == cfg.library_dir / "@@@"  # не «~~~» и не «!!!»
    (cfg.library_dir / "!!!" / "x.mp3").write_bytes(b"1")
    (cfg.library_dir / "~~~" / "y.mp3").write_bytes(b"2")
    assert library.merge_artist_variants(db) == 0
    assert (cfg.library_dir / "!!!").is_dir() and (cfg.library_dir / "~~~").is_dir()
    assert Path(t.path).exists()


def test_create_import_is_atomic(db):
    with pytest.raises(Exception):
        db.create_import("alice", 1, "x.csv", [{"artist": "A", "title": "T"}, {"artist": object(), "title": "T"}])
    assert db._q("SELECT count(*) c FROM imports")[0]["c"] == 0
    assert db._q("SELECT count(*) c FROM import_items")[0]["c"] == 0


def test_next_import_item_laggard_first_and_exclusions(db):
    a = db.create_import("alice", 1, "a.csv", [{"artist": "A", "title": f"T{i}"} for i in range(4)])
    b = db.create_import("bob", 2, "b.csv", [{"artist": "B", "title": f"T{i}"} for i in range(4)])
    for _ in range(2):
        db.set_import_item(db.next_import_item()["id"], "done")
    first = db.next_import_item()
    assert first is not None
    lag = b if db.import_counts(a)["done"] > db.import_counts(b)["done"] else a
    if db.import_counts(a)["done"] != db.import_counts(b)["done"]:
        assert first["import_id"] == lag
    other = db.next_import_item(exclude_imports={first["import_id"]})
    assert other["import_id"] != first["import_id"]
    assert db.next_import_item(exclude_imports={a, b}) is not None  # все заняты — всё равно есть что взять


# ------------------------------------------------------------------ P1: воркеры не умирают молча
async def test_worker_survives_unexpected_errors_and_respawns(db, monkeypatch):
    svc = svc_for(db)
    calls = []

    async def flaky(self, n, queue):
        calls.append(n)
        if len(calls) == 1:
            raise RuntimeError("неожиданно")
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, "_worker_step", flaky)
    task = asyncio.create_task(svc._worker(0))
    for _ in range(30):  # после ошибки воркер ждёт 1 с и продолжает
        await asyncio.sleep(0.05)
        if len(calls) >= 2:
            break
    assert len(calls) >= 2
    task.cancel()
    monkeypatch.undo()

    starts = []

    async def quits(self, n, queue=None):
        starts.append(n)
        if len(starts) == 1:
            return  # воркер «вышел»
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, "_worker", quits)
    svc._workers = [svc._spawn_worker(3)]
    for _ in range(10):
        await asyncio.sleep(0)
    assert starts == [3, 3] and not svc._workers[0].done()  # перезапущен и стоит в списке
    svc._workers[0].cancel()


# ------------------------------------------------------------------ P1: фоновые задачи
async def test_genre_cursor_not_advanced_on_network_error(db, cfg, monkeypatch):
    add_user(db)
    cache_track(db, cfg, "A", "T")

    async def down(hint, **kw):
        raise ConnectionError("нет сети")
    monkeypatch.setattr(service.metadata, "lookup_itunes", down)
    svc = svc_for(db)
    assert await svc.genres_online_step(10) == (0, False)
    assert db.kv_get(svc.GENRE_ONLINE_CURSOR) is None


async def test_loudness_transient_errors_retry_later(db, cfg, monkeypatch):
    t = cache_track(db, cfg, "A", "T")
    attempts = []

    async def killed(path):
        attempts.append(path)
        raise loudness.Transient("ffmpeg rc=-9")
    monkeypatch.setattr(loudness, "analyze", killed)
    monkeypatch.setattr(loudness, "_filters", "ebur128")
    monkeypatch.setattr(service, "LOUDNESS_RETRY_DELAY", 0.0)
    svc = svc_for(db)
    task = asyncio.create_task(svc._loudness_loop(first_delay=0, idle=0.05))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if db.loudness_pending() == 0:
            break
    task.cancel()
    assert len(attempts) == service.LOUDNESS_MAX_RETRIES  # не отмечен после первой неудачи
    assert db.loudness_pending() == 0 and db.get_track(t.id).gain_db is None  # после N раз — пропуск


def test_loudness_transient_detection():
    with pytest.raises(loudness.Transient):
        loudness._check(SimpleNamespace(rc=-9, err="timeout"))
    with pytest.raises(loudness.Transient):
        loudness._check(SimpleNamespace(rc=137, err=""))
    loudness._check(SimpleNamespace(rc=1, err="Invalid data"))  # битый файл — обычная неудача


# ------------------------------------------------------------------ P1: секреты
def test_scrub_secret_env_and_captured_key(monkeypatch, cfg):
    from app import config
    monkeypatch.setenv("JELLYFIN_API_KEY", "jf-key")
    monkeypatch.setenv("SLSK_PASSWORD", "p")
    c = config.Config()
    removed = config.scrub_secret_env()
    assert {"JELLYFIN_API_KEY", "SLSK_PASSWORD"} <= set(removed)
    assert "JELLYFIN_API_KEY" not in os.environ and c.jellyfin_api_key() == "jf-key"


def test_sockseek_credentials_in_private_file(cfg, monkeypatch):
    from conftest import patch_cfg
    from app import downloaders
    patch_cfg(monkeypatch, media_root=cfg.media_root, state_dir=cfg.state_dir, sldl_username="u",
              sldl_password="secret-pass")
    path = downloaders._sockseek_config()
    assert path.read_text(encoding="utf-8") == "username = u\npassword = secret-pass\n"
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


async def test_sockseek_password_not_in_argv(cfg, monkeypatch):
    from conftest import patch_cfg
    from app import downloaders
    patch_cfg(monkeypatch, media_root=cfg.media_root, state_dir=cfg.state_dir, sldl_username="u",
              sldl_password="secret-pass")
    seen = []

    async def fake_run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(rc=1, out="", err="")
    monkeypatch.setattr(downloaders, "_sockseek_bin", lambda: "/usr/bin/sockseek")
    monkeypatch.setattr(downloaders, "run", fake_run)
    await downloaders.soulseek_sockseek(TrackRequest(artist="A", title="T"), AsyncMock())
    assert seen and "secret-pass" not in " ".join(seen[0]) and "--config" in seen[0]


# ------------------------------------------------------------------ P1: квоты и место
async def test_download_quota_and_disk(db, cfg, monkeypatch):
    u = add_user(db)
    svc = svc_for(db)
    monkeypatch.setattr(service, "MAX_QUEUED_PER_USER", 3)
    monkeypatch.setattr(service.uploads, "free_space_ok", lambda extra=0: True)
    assert svc.admission(u, 3) is None
    svc._count_queued("alice", 2)
    assert svc.admission(u, 2)[0] == "quota"
    notify = AsyncMock()
    job = service.Job(user=u, text="x", notify=notify, tracks=[TrackRequest(title=f"{i}") for i in range(2)])
    await svc.run_job(job)
    assert "максимум 3" in notify.await_args.args[0] and svc.queue.qsize() == 0
    monkeypatch.setattr(service.uploads, "free_space_ok", lambda extra=0: False)
    assert svc.admission(u, 1)[0] == "disk_full"


async def test_webapp_refuses_download_over_quota(db, cfg):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from app import search, webapp
    from test_webapp import TOKEN, FakeSvc, hdr
    db.upsert_user(User("alice", 4242, "jf", None, "t", False))
    fsvc = FakeSvc()
    fsvc.admission = lambda user, n: ("quota", "В очереди уже 2000 ваших треков")
    app = web.Application()
    webapp.setup_webapp(app, db=db, svc=fsvc, jf=MagicMock(), bot_token=TOKEN)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        res = search.Result(query="q", owner=4242, tracks=[TrackRequest(artist="A", title="T")])
        r = await c.post("/api/download", json={"token": search.store(res), "kind": "all"}, headers=hdr())
        assert r.status == 429 and (await r.json())["error"] == "quota" and fsvc.jobs == []
        fsvc.admission = lambda user, n: ("disk_full", "мало места")
        r = await c.post("/api/download", json={"token": search.store(res), "kind": "all"}, headers=hdr())
        assert r.status == 507
    finally:
        await c.close()


# ------------------------------------------------------------------ мелочи: /health, плейлист, радио, индексы
async def test_health_is_minimal(db, cfg):
    rs = radio.RadioServer(db, MagicMock())
    r = await rs.health(MagicMock())
    import json
    assert json.loads(r.body) == {"ok": True}


def test_import_track_not_added_after_leaving_playlist(db, cfg):
    add_user(db, "alice", 1)
    add_user(db, "bob", 2)
    t = cache_track(db, cfg, "A", "T")
    pid = db.create_playlist("alice", "Общий")
    db.add_playlist_member(pid, "bob")
    svc = svc_for(db)
    db.remove_playlist_member(pid, "bob")  # bob вышел, пока качалось
    job = service.Job(user=db.user_by_login("bob"), text="x", notify=AsyncMock(), tracks=[TrackRequest()])
    job.playlist_id = pid
    svc._job_track_done(job, job.tracks[0], TrackOutcome(job.tracks[0], True, "✅", track=t))
    assert db.playlist_track_ids(pid) == []


def test_radio_subscribe_drops_stale_burst(db):
    st = radio.Station(MagicMock(), User("alice", 1, "jf", None, "t", False), db)
    st.burst.extend([b"old1", b"old2"])

    async def go():
        st._run = AsyncMock()  # не играем по-настоящему
        q = st.subscribe()
        return q.qsize()
    assert asyncio.run(go()) == 0  # станция стояла — старый буфер новому слушателю не отдаётся


def test_indexes_exist(db):
    names = {r["name"] for r in db._q("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_user_tracks_track", "idx_aliases_track"} <= names


# ------------------------------------------------------------------ раздача Soulseek
def test_share_tracks_but_not_uploads(db, cfg):
    add_user(db)
    t = cache_track(db, cfg, "A", "Shared")
    up = cache_track(db, cfg, "B", "Private", source="upload")
    library.share_add(t)
    library.share_add(up)
    st, su = library.share_path(t.path), library.share_path(up.path)
    assert st.exists() and os.path.samefile(st, t.path) and not su.exists()
    # загруженный файл заменили скачанным — теперь раздаётся
    new = cfg.media_root / "dl.mp3"
    new.write_bytes(b"REAL")
    library.replace_upload_copy(db, db.get_track(up.id), new, "soulseek")
    assert su.exists() and su.read_bytes() == b"REAL"
    # трек больше никому не нужен — из раздачи тоже уходит
    assert library.purge_if_unused(db, db.get_track(t.id))
    assert not st.exists() and not st.parent.exists()


def test_rebuild_share(db, cfg):
    t = cache_track(db, cfg, "A", "T1")
    cache_track(db, cfg, "A", "T2", source="upload")
    stray = cfg.share_dir / "Old" / "Album" / "gone.mp3"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    added, removed = library.rebuild_share(db)
    assert (added, removed) == (1, 1)
    assert library.share_path(t.path).exists() and not stray.exists() and not (cfg.share_dir / "Old").exists()
    assert library.rebuild_share(db) == (0, 0)  # повторно — ничего


def test_compose_shares_only_share_dir():
    text = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    slskd = text.split("  slskd:")[1].split("\n  bot:")[0]
    assert "SLSKD_SHARED_DIR=${SLSKD_SHARED_DIR:-/media/share}" in slskd
    assert "/media/share:/media/share:ro" in slskd and "${DATA_DIR}/media:/media" not in slskd
