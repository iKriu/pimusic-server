"""Массовые операции и плейлисты из каталога; зеркало Jellyfin без потолка; запасной источник при битом файле."""
import asyncio
from pathlib import Path
from app import auth, downloaders, library, service
from app.db import User
from app.jellyfin import Jellyfin, JellyfinError
from app.service import Service, TrackOutcome
from app.sources import TrackRequest
from app.util import match_key

from test_webapp import alice, client, fake_search, hdr  # noqa: F401 — фикстуры


def add_track(db, cfg, login, artist, title):
    d = cfg.library_dir / artist / "Album"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{title}.mp3"
    p.write_bytes(b"ID3")
    tid = db.add_track(match_key=match_key(artist, title), mbid=None, isrc=None, artist=artist, title=title,
                       album="Album", duration=100, path=str(p), source="test")
    if login:
        db.link_track(login, tid, str(cfg.users_dir / login / artist / "Album" / p.name))
    return tid


async def search_token(client):
    r = await client.get("/api/search", params={"q": "кино"}, headers=hdr())
    return (await r.json())["token"]


async def wait_job(client, n=1):
    for _ in range(100):
        if len(client.svc.jobs) >= n:
            return client.svc.jobs[n - 1][0]
        await asyncio.sleep(0.02)
    raise AssertionError("задание не запустилось")


# ------------------------------------------------------------------ скачать прямо в плейлист
async def test_download_into_playlist_keeps_order(client, fake_search, db, cfg):
    token = await search_token(client)
    pid = db.create_playlist("alice", "Мой")
    first = add_track(db, cfg, "alice", "Земфира", "Хочешь")
    db.add_playlist_tracks(pid, [first], "alice", 5000)
    have = add_track(db, cfg, "alice", "Кино", "Кукушка")  # уже в медиатеке — в плейлист сразу
    r = await client.post("/api/download", json={"token": token, "kind": "all", "playlist_id": pid}, headers=hdr())
    data = await r.json()
    assert r.status == 200 and data["queued"] == 2
    assert data["playlist"] == {"id": pid, "name": "Мой", "added_now": 1}
    assert db.playlist_track_ids(pid) == [first, have]
    job = await wait_job(client)
    assert job.playlist_id == pid and job.playlist_base > 1024  # дописываем после уже имеющихся
    # воркер: первый трек (Группа крови) скачался позже — встаёт перед «Кукушкой», как в результатах поиска
    blood = add_track(db, cfg, "alice", "Кино", "Группа крови")
    svc = Service(db, None)
    svc._job_track_done(job, job.tracks[0], TrackOutcome(job.tracks[0], True, "✅", track=db.get_track(blood)))
    svc._job_track_done(job, job.tracks[1], TrackOutcome(job.tracks[1], True, "✅", track=db.get_track(have)))
    assert db.playlist_track_ids(pid) == [first, blood, have]


async def test_download_into_foreign_playlist_is_404(client, fake_search, db):
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    token = await search_token(client)
    pid = db.create_playlist("bob", "Чужой")
    for bad in (pid, 999999, "x", True):
        r = await client.post("/api/download", json={"token": token, "kind": "track", "i": 0, "playlist_id": bad},
                              headers=hdr())
        assert r.status == 404 and (await r.json())["error"] == "playlist_not_found", bad
    db.add_playlist_member(pid, "alice")  # участник — может
    r = await client.post("/api/download", json={"token": token, "kind": "track", "i": 0, "playlist_id": pid},
                          headers=hdr())
    assert r.status == 200


async def test_create_playlist_from_catalog(client, fake_search, db, cfg):
    token = await search_token(client)
    have = add_track(db, cfg, "alice", "Кино", "Кукушка")
    r = await client.post("/api/playlists", json={"name": "Из поиска", "catalog": {"token": token, "kind": "tracks",
                                                                                  "idx": [1, 0]}}, headers=hdr())
    data = await r.json()
    assert r.status == 200 and data["name"] == "Из поиска" and data["queued"] == 2 and data["added_now"] == 1
    assert data["count"] == 1 and db.playlist_track_ids(data["id"]) == [have]
    job = await wait_job(client)
    assert job.playlist_id == data["id"] and [t.title for t in job.tracks] == ["Группа крови", "Кукушка"]
    # плохой каталог — плейлист не создаётся
    n = len(db.user_playlists("alice"))
    for bad in ({"token": "nope", "kind": "all"}, {"token": token, "kind": "zzz"}, "строка"):
        r = await client.post("/api/playlists", json={"name": "x", "catalog": bad}, headers=hdr())
        assert r.status in (400, 404), bad
    assert len(db.user_playlists("alice")) == n


# ------------------------------------------------------------------ массовые операции
async def test_bulk_library_delete(client, db, cfg):
    ids = [add_track(db, cfg, "alice", "A", f"T{i}") for i in range(3)]
    r = await client.post("/api/library/delete", json={"track_ids": ids + [999999, ids[0]]}, headers=hdr())
    # заглушка delete_track ничего не удаляет — треки остаются доступны, gone_ids пуст
    assert await r.json() == {"deleted": 3, "skipped": 1, "deleted_ids": ids, "gone_ids": []}
    assert [c.args[1] for c in client.svc.delete_track.await_args_list] == ids
    for bad in ([], "1", [0], list(range(1, 502))):
        r = await client.post("/api/library/delete", json={"track_ids": bad}, headers=hdr())
        assert r.status == 400, bad


async def test_move_many_and_remove_500(client, db, cfg):
    pid = db.create_playlist("alice", "Большой")
    ids = [add_track(db, cfg, "alice", "A", f"T{i:03d}") for i in range(520)]
    db.add_playlist_tracks(pid, ids, "alice", 5000)
    items = [r[0] for r in db._q("SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY position", (pid,))]
    # выделение 5, 2, 9 (в текущем порядке: 2, 5, 9) — блоком на индекс 0
    r = await client.post(f"/api/playlists/{pid}/move-many", json={"item_ids": [items[9], items[2], items[5]],
                                                                   "to": 0}, headers=hdr())
    assert await r.json() == {"ok": True, "moved": 3}
    order = db.playlist_track_ids(pid)
    assert order[:4] == [ids[2], ids[5], ids[9], ids[0]] and len(order) == 520
    r = await client.post(f"/api/playlists/{pid}/move-many", json={"item_ids": [items[0]], "to": 10 ** 9},
                          headers=hdr())
    assert db.playlist_track_ids(pid)[-1] == ids[0]
    for bad in ({"item_ids": [], "to": 0}, {"item_ids": [items[0]]}, {"item_ids": [items[0]], "to": True}):
        assert (await client.post(f"/api/playlists/{pid}/move-many", json=bad, headers=hdr())).status == 400
    assert (await client.post(f"/api/playlists/{pid}/move-many", json={"item_ids": [424242], "to": 0},
                              headers=hdr())).status == 404
    r = await client.post(f"/api/playlists/{pid}/remove", json={"item_ids": items[:500]}, headers=hdr())
    assert await r.json() == {"removed": 500, "count": 20}


async def test_add_to_playlist_with_device_token(client, db, cfg):
    tid = add_track(db, cfg, "alice", "A", "B")
    pid = db.create_playlist("alice", "P")
    token, h = auth.new_token()
    db.add_device("alice", "phone", "android", h)
    r = await client.post(f"/api/playlists/{pid}/add", json={"track_ids": [tid]},
                          headers={"Authorization": f"Bearer {token}"})
    assert await r.json() == {"added": 1, "skipped": 0, "count": 1}
    r = await client.post(f"/api/playlists/{pid}/add", json={"track_ids": [tid]}, headers=hdr())
    assert (await r.json())["skipped"] == 1  # initData — тот же пользователь


# ------------------------------------------------------------------ зеркало Jellyfin без потолка
async def test_create_playlist_full_appends_in_batches(monkeypatch):
    jf = Jellyfin("http://jf", "key")
    calls = []

    async def fake_request(method, path, *, params=None, json=None, **_):
        calls.append((method, path, list((json or {}).get("Ids") or []) or list(params.get("ids") or [])))
        if path == "/Playlists" and method == "POST":
            return {"Id": "pl1"}
    monkeypatch.setattr(jf, "request", fake_request)
    ids = [f"i{n}" for n in range(1293)]
    assert await jf.create_playlist_full("u1", "Большой", ids) == "pl1"
    assert calls[0][0:2] == ("POST", "/Playlists") and calls[0][2] == ids[:500]
    appended = [c[2] for c in calls[1:]]
    assert all(len(b) <= 100 for b in appended) and sum(appended, []) == ids[500:]  # порядок сохранён


async def test_create_playlist_full_falls_back_when_append_forbidden(monkeypatch):
    jf = Jellyfin("http://jf", "key")
    created = []

    async def fake_request(method, path, *, params=None, json=None, **_):
        if method == "POST" and path == "/Playlists":
            created.append(len(json["Ids"]))
            return {"Id": f"pl{len(created)}"}
        if method == "POST":
            raise JellyfinError(403, "forbidden")
        return None
    monkeypatch.setattr(jf, "request", fake_request)
    assert await jf.create_playlist_full("u1", "Большой", [str(n) for n in range(700)]) == "pl2"
    assert created == [500, 700]


# ------------------------------------------------------------------ битый файл — следующий источник
async def test_bad_file_falls_back_to_next_source(db, cfg, monkeypatch):
    u = User("alice", 1, "jf", None, "t", False)
    db.upsert_user(u)
    calls = []
    work = cfg.incoming_dir / "ytdlp" / "x"
    work.mkdir(parents=True)

    async def fake_fetch(req, progress, exclude=frozenset()):
        calls.append(set(exclude))
        work.mkdir(parents=True, exist_ok=True)  # cleanup_after удаляет папку прошлой попытки
        f = work / f"f{len(calls)}.mp3"
        f.write_bytes(b"x")
        return downloaders.Downloaded(f, "soulseek" if not exclude else "yt-dlp", "peer")

    async def fake_ingest(db_, path, hint, *, source, login, keep_mp3=False):
        if source == "soulseek":
            raise library.BadAudio("файл не читается ffprobe")
        tid = add_track(db_, cfg, login, "Кино", "Звезда")
        return library.IngestResult(db_.get_track(tid), False, None, {}, Path(path))
    monkeypatch.setattr(service.downloaders, "fetch", fake_fetch)
    monkeypatch.setattr(service.library, "ingest", fake_ingest)
    monkeypatch.setattr(service.library, "lookup_cached", lambda *a, **k: None)
    svc = Service(db, None)
    svc._schedule_refresh = lambda *a, **k: None
    progress = []

    async def notify(text):
        progress.append(text)
    out = await svc._process(service.Job(user=u, text="x", notify=notify),
                             TrackRequest(artist="Кино", title="Звезда", duration=1))
    assert out.ok and calls == [set(), {"soulseek_slskd"}]
    # все источники дают битые файлы — честный провал с пояснением, без исключения
    async def always_bad(*a, **k):
        raise library.BadAudio("битый")
    monkeypatch.setattr(service.library, "ingest", always_bad)
    calls.clear()
    out = await svc._process(service.Job(user=u, text="x", notify=notify),
                             TrackRequest(artist="Кино", title="Звезда", duration=1))
    assert not out.ok and "повреждены" in out.text and len(calls) == 3


def test_fetch_skips_excluded_steps(monkeypatch):
    called = []

    def make(name):
        async def step(req, progress):
            called.append(name)
            return None
        step.__name__ = name
        return step
    for name in ("soulseek_slskd", "soulseek_sockseek", "spotdl", "ytdlp"):
        monkeypatch.setattr(downloaders, name, make(name))
    asyncio.run(downloaders.fetch(TrackRequest(artist="A", title="B"), exclude={"soulseek_slskd", "spotdl"}))
    assert called == ["soulseek_sockseek", "ytdlp"]


def test_probe_limits_are_not_tight():
    from app import util
    assert util.PROBE_LIMITS[1] >= 8 * 1024 ** 3 and util.FFMPEG_LIMITS[1] >= 8 * 1024 ** 3
