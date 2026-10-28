"""Исправления P2 по ревью 2026-10-20: жанровый бэкфилл при сбоях сети, ingest вне event loop, признаки удаления
в ответах API, CSP и заголовки безопасности, доверие X-Forwarded-For."""
import asyncio
import re
import threading
from pathlib import Path

import aiohttp
import pytest

from app import auth, http, library, metadata, service, webapp
from app.metadata import Hint
from app.service import Service
from app.db import User

from test_review_fixes import add_user, cache_track
from test_webapp import alice, client, hdr  # noqa: F401 — фикстуры


# ------------------------------------------------------------------ 1. жанры: сбой сети ≠ пустой ответ
class _Resp:
    def __init__(self, status, data=None):
        self.status, self._data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._data


class _Sess:
    def __init__(self, answers):
        self.answers = list(answers)

    def get(self, *a, **kw):
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _use(monkeypatch, sess):
    monkeypatch.setattr(http, "session", lambda: sess)


@pytest.fixture
def no_sleep(monkeypatch):
    real = asyncio.sleep

    async def fast(s, *a, **kw):
        await real(0)
    monkeypatch.setattr(http.asyncio, "sleep", fast)


async def test_get_json_strict_raises_only_on_failure(monkeypatch, no_sleep):
    monkeypatch.setattr(http, "session", lambda: _Sess([aiohttp.ClientConnectionError("down")] * 2))
    assert await http.get_json("https://x", retries=2) is None  # по умолчанию — как раньше
    monkeypatch.setattr(http, "session", lambda: _Sess([aiohttp.ClientConnectionError("down")] * 2))
    with pytest.raises(http.FetchError):
        await http.get_json("https://x", retries=2, strict=True)
    _use(monkeypatch, _Sess([_Resp(502), _Resp(503)]))
    with pytest.raises(http.FetchError):
        await http.get_json("https://x", retries=2, strict=True)
    # 404/400 — ответ «нет данных», не сбой; 500, потом 200 — данные
    _use(monkeypatch, _Sess([_Resp(404)]))
    assert await http.get_json("https://x", strict=True) is None
    _use(monkeypatch, _Sess([_Resp(400)]))
    assert await http.get_json("https://x", strict=True) is None
    _use(monkeypatch, _Sess([_Resp(500), _Resp(200, {"ok": 1})]))
    assert await http.get_json("https://x", strict=True) == {"ok": 1}


async def test_lookup_itunes_strict_propagates_network_error(monkeypatch):
    async def down(url, **kw):
        if kw.get("strict"):
            raise http.FetchError("нет сети")
        return None
    monkeypatch.setattr(metadata.http, "get_json", down)
    assert await metadata.lookup_itunes(Hint(artist="A", title="T")) is None
    with pytest.raises(http.FetchError):
        await metadata.lookup_itunes(Hint(artist="A", title="T"), strict=True)


async def test_genre_step_real_network_error_keeps_cursor(db, cfg, monkeypatch, no_sleep):
    """Настоящая цепочка: get_json не дозвонился → курсор не двигается (раньше трек пропускался навсегда)."""
    add_user(db)
    cache_track(db, cfg, "A", "T")
    monkeypatch.setattr(http, "session", lambda: _Sess([aiohttp.ClientConnectionError("down")] * 50))
    monkeypatch.setattr(http.ITUNES_LIMIT, "interval", 0)
    svc = Service(db, None)
    assert await svc.genres_online_step(10) == (0, False)
    assert db.kv_get(svc.GENRE_ONLINE_CURSOR) is None


async def test_genre_step_empty_answer_advances_cursor(db, cfg, monkeypatch):
    add_user(db)
    t = cache_track(db, cfg, "A", "T")

    async def nothing(hint, **kw):
        assert kw.get("strict") is True
        return None
    monkeypatch.setattr(service.metadata, "lookup_itunes", nothing)
    svc = Service(db, None)
    assert await svc.genres_online_step(10) == (0, True)
    assert db.kv_get(svc.GENRE_ONLINE_CURSOR) == str(t.id)  # пустой ответ — трек обработан


async def test_genre_step_backoff_and_skip_after_limit(db, cfg, monkeypatch):
    add_user(db)
    t1 = cache_track(db, cfg, "A", "T1")
    t2 = cache_track(db, cfg, "A", "T2")
    asked = []

    async def flaky(hint, **kw):
        asked.append(hint.title)
        if hint.title == "T1":
            raise http.FetchError("нет сети")
        return {"primaryGenreName": "Rock"}
    monkeypatch.setattr(service.metadata, "lookup_itunes", flaky)
    svc = Service(db, None)
    pauses = []
    for _ in range(svc.GENRE_ONLINE_MAX_FAILS - 1):
        assert await svc.genres_online_step(10) == (0, False)
        pauses.append(svc._genre_pause())
    assert db.kv_get(svc.GENRE_ONLINE_CURSOR) is None and asked == ["T1"] * (svc.GENRE_ONLINE_MAX_FAILS - 1)
    assert pauses == sorted(pauses) and pauses[0] == 3600 and pauses[-1] <= svc.GENRE_ONLINE_MAX_PAUSE
    # лимит исчерпан — трек пропускается, проход идёт дальше, счётчик сброшен
    assert await svc.genres_online_step(10) == (1, True)
    assert db.kv_get(svc.GENRE_ONLINE_CURSOR) == str(t2.id) and db.get_track(t2.id).genre == "Rock"
    assert db.get_track(t1.id).genre is None and svc._genre_fails() == (0, 0) and svc._genre_pause() == 3600


async def test_genre_fail_counter_resets_after_success(db, cfg, monkeypatch):
    add_user(db)
    cache_track(db, cfg, "A", "T1")
    state = {"down": True}

    async def sometimes(hint, **kw):
        if state["down"]:
            raise http.FetchError("нет сети")
        return None
    monkeypatch.setattr(service.metadata, "lookup_itunes", sometimes)
    svc = Service(db, None)
    await svc.genres_online_step(10)
    await svc.genres_online_step(10)
    assert svc._genre_fails()[1] == 2
    state["down"] = False
    assert await svc.genres_online_step(10) == (0, True)
    assert svc._genre_fails() == (0, 0)


# ------------------------------------------------------------------ 3. ingest: файловые операции вне event loop
async def test_ingest_blocking_io_runs_in_threads(db, cfg, monkeypatch, tmp_path):
    from app.metadata import TrackMeta
    from app.util import AudioInfo
    src = tmp_path / "in.mp3"
    src.write_bytes(b"ID3")
    main = threading.get_ident()
    seen = {}

    async def fake_probe(p):
        return AudioInfo("mp3", 320000, 200, 44100, {}, format_name="mp3")

    async def yes(p):
        return True

    async def fake_resolve(hint):
        return TrackMeta(title="T", artist="A", album_artist="A", album="Al")

    async def fake_tag(path, meta, work, **_):
        return {}

    def spy(name, fn):
        def wrapped(*a, **kw):
            seen[name] = threading.get_ident()
            return fn(*a, **kw)
        return wrapped
    monkeypatch.setattr(library, "probe", fake_probe)
    monkeypatch.setattr(library, "mp3_is_cbr320", yes)
    monkeypatch.setattr(library.metadata, "resolve", fake_resolve)
    monkeypatch.setattr(library.tagger, "tag_file", fake_tag)
    names = ("cache_path_for", "find_same_recording", "attach_to_user", "share_add", "_take_mp3")
    for name in names:
        monkeypatch.setattr(library, name, spy(name, getattr(library, name)))
    add_user(db, "bob", 9)
    res = await library.ingest(db, src, Hint(artist="A", title="T"), source="test", login="bob")
    assert set(seen) == set(names) and main not in seen.values()
    # транзакционная дисциплина сохранена: строка пользователя есть, ссылка на месте
    assert res.link_path.is_file() and db.user_has_track("bob", res.track.id) == str(res.link_path)
    # тот же трек другому пользователю — из кэша, тоже в потоке
    src2 = tmp_path / "in2.mp3"
    src2.write_bytes(b"ID3")
    add_user(db, "carol", 10)
    seen.clear()
    res2 = await library.ingest(db, src2, Hint(artist="A", title="T"), source="test", login="carol")
    assert res2.reused and res2.track.id == res.track.id and seen and main not in seen.values()


async def test_tag_file_writes_off_loop(cfg, monkeypatch, tmp_path):
    from app import tagger
    from app.metadata import TrackMeta
    main = threading.get_ident()
    seen = []

    async def no_cover(meta, d):
        return None

    async def lyr(*a):
        return "[00:01.00] la", ""
    monkeypatch.setattr(tagger, "fetch_cover", no_cover)
    monkeypatch.setattr(tagger.lyrics, "fetch", lyr)
    monkeypatch.setattr(tagger, "write_tags", lambda *a: seen.append(threading.get_ident()))
    p = tmp_path / "t.mp3"
    p.write_bytes(b"")
    out = await tagger.tag_file(p, TrackMeta(title="T", artist="A", album_artist="A", album="Al"), tmp_path)
    assert out["synced_lyrics"] and p.with_suffix(".lrc").read_text(encoding="utf-8").startswith("[00:01.00]")
    assert seen and main not in seen


# ------------------------------------------------------------------ 4. удаление: признаки для клиента
def _lib_track(db, cfg, login, artist, title):
    from test_webapp import add_track
    return add_track(db, cfg, login, artist, title)


def _real_unlink(client, db):
    async def unlink(user, tid):
        library.unlink_from_user(db, db.get_track(tid), user.login)
        return "🗑"
    client.svc.delete_track.side_effect = unlink


def _also_bob(db, cfg, tid):
    db.upsert_user(User("bob", 555, "jf-bob", None, "bt", False))
    db.link_track("bob", tid, str(cfg.users_dir / "bob" / f"{tid}.mp3"))


async def test_delete_reports_gone(client, db, cfg):
    _real_unlink(client, db)
    solo = _lib_track(db, cfg, "alice", "A", "Solo")
    in_pl = _lib_track(db, cfg, "alice", "A", "InPl")
    kept = _lib_track(db, cfg, "alice", "A", "Kept")
    _also_bob(db, cfg, kept)
    db.create_playlist("alice", "P", track_ids=[in_pl, kept])
    data = await (await client.post("/api/delete", json={"track_id": solo}, headers=hdr())).json()
    assert data["ok"] is True and data["track_id"] == solo and data["gone"] is True  # нет нигде — из очереди
    path = db.get_track(in_pl).path
    data = await (await client.post("/api/delete", json={"track_id": in_pl}, headers=hdr())).json()
    assert data["gone"] is True and db.get_track(in_pl) is None and not Path(path).exists()  # плейлист файл не держит
    data = await (await client.post("/api/delete", json={"track_id": kept}, headers=hdr())).json()
    assert data["gone"] is False  # есть у bob — в плейлисте остался, играть можно


async def test_bulk_delete_reports_ids(client, db, cfg):
    _real_unlink(client, db)
    a = _lib_track(db, cfg, "alice", "A", "T1")
    b = _lib_track(db, cfg, "alice", "A", "T2")
    _also_bob(db, cfg, b)
    db.create_playlist("alice", "P", track_ids=[a, b])
    r = await client.post("/api/library/delete", json={"track_ids": [a, b, 999999, a]}, headers=hdr())
    assert await r.json() == {"deleted": 2, "skipped": 1, "deleted_ids": [a, b], "gone_ids": [a]}


# ------------------------------------------------------------------ 5. CSP и заголовки безопасности
async def test_security_headers_on_app_and_api(client):
    for path in ("/app/", "/app/static/js/app.js", "/app/sw.js", "/app/manifest.webmanifest"):
        r = await client.get(path)
        assert r.status == 200, path
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert r.headers["Content-Security-Policy"] == webapp.CSP_APP, path
    r = await client.get("/app", allow_redirects=False)
    assert "Content-Security-Policy" in r.headers
    r = await client.get("/api/me")  # 401 — тоже с заголовками
    assert r.status == 401
    assert r.headers["Content-Security-Policy"] == webapp.CSP_API
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    r = await client.get("/api/me", headers=hdr())
    assert r.status == 200 and r.headers["Content-Security-Policy"] == webapp.CSP_API
    assert "X-Frame-Options" not in r.headers  # встраивание в Telegram Web регулирует frame-ancestors


def test_csp_app_allows_what_mini_app_needs():
    d = dict((p.split(" ", 1) + [""])[:2] for p in webapp.CSP_APP.split("; "))
    assert "https://telegram.org" in d["script-src"] and "'unsafe-inline'" not in d["script-src"]
    assert "https://fonts.googleapis.com" in d["style-src"] and "https://fonts.gstatic.com" in d["font-src"]
    assert {"'self'", "data:", "blob:", "https:"} <= set(d["img-src"].split())
    assert {"'self'", "blob:", "data:", "https://*.dzcdn.net"} <= set(d["media-src"].split())  # + фрагменты Deezer
    assert d["connect-src"].startswith("'self'") and d["worker-src"] == "'self'"
    assert "https://web.telegram.org" in d["frame-ancestors"] and d["object-src"] == "'none'"


def test_static_has_no_inline_scripts_or_handlers():
    """CSP без 'unsafe-inline' для скриптов: в index.html только внешние <script src>, без on…= обработчиков."""
    html = (webapp.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    scripts = list(re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I))
    assert scripts
    for m in scripts:
        assert "src=" in m.group(1) and not m.group(2).strip()
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert "javascript:" not in html


# ------------------------------------------------------------------ 6. X-Forwarded-For
def test_client_ip_default_trusts_only_loopback():
    assert auth.parse_networks(()) == auth.parse_networks(auth.DEFAULT_TRUSTED_PROXIES)
    assert auth.parse_networks(["мусор"]) == auth.parse_networks(auth.DEFAULT_TRUSTED_PROXIES)
    for lan in ("192.168.1.2", "192.168.1.50", "10.88.0.1", "172.17.0.1"):
        assert auth.client_ip(lan, "8.8.8.8") == lan  # LAN без явного TRUSTED_PROXIES — заголовку не верим
    assert auth.client_ip("::1", "8.8.8.8") == "8.8.8.8"


def test_client_ip_rightmost_untrusted_with_npm():
    npm = auth.parse_networks(["192.168.1.2/32"])
    assert auth.client_ip("192.168.1.2", "8.8.8.8", npm) == "8.8.8.8"
    assert auth.client_ip("192.168.1.2", "1.2.3.4, 8.8.8.8", npm) == "8.8.8.8"  # левую часть подделал клиент
    assert auth.client_ip("192.168.1.50", "8.8.8.8", npm) == "192.168.1.50"  # другой хост LAN — не прокси
    assert auth.client_ip("192.168.1.2", "192.168.1.2", npm) == "192.168.1.2"
    assert auth.client_ip("192.168.1.2", "8.8.8.8, мусор", npm) == "192.168.1.2"  # мусор справа — соединение
    assert auth.client_ip("192.168.1.2", "192.168.1.7", npm) == "192.168.1.7"  # клиент из LAN через NPM


def test_client_ip_ipv4_mapped_remote():
    npm = auth.parse_networks(["192.168.1.2/32"])
    assert auth.client_ip("::ffff:192.168.1.2", "8.8.8.8", npm) == "8.8.8.8"
    assert auth.client_ip("192.168.1.2", "::ffff:8.8.8.8", npm) == "8.8.8.8"
    assert auth.client_ip("::ffff:192.168.1.50", "8.8.8.8", npm) == "::ffff:192.168.1.50"


# ------------------------------------------------------------------ автообновление: GET /updates/{name}
@pytest.fixture
def upd(cfg):
    d = cfg.updates_dir
    d.mkdir(parents=True)
    (d / "latest.json").write_text('{"version":"1.0.4"}', encoding="utf-8")
    (d / "android.json").write_text('{"version":"1.0.4"}', encoding="utf-8")
    (d / "PiMusic_1.0.4_x64-setup.exe").write_bytes(bytes(range(256)) * 4)
    (d / "PiMusic_1.0.4_x64-setup.exe.sig").write_text("sig", encoding="utf-8")
    (d / "pimusic-1.0.4.apk").write_bytes(b"PK" + b"\0" * 100)
    (d / "notes.txt").write_text("x", encoding="utf-8")
    (d / ".hidden.json").write_text("{}", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "a.json").write_text("{}", encoding="utf-8")
    (cfg.media_root / "secret.json").write_text('{"s":1}', encoding="utf-8")
    return d


async def test_updates_types_and_cache(client, upd):
    r = await client.get("/updates/latest.json")  # без авторизации
    assert r.status == 200 and r.headers["Content-Type"].startswith("application/json")
    assert r.headers["Cache-Control"] == "no-cache" and await r.json() == {"version": "1.0.4"}
    assert "Content-Security-Policy" not in r.headers  # CSP /app сюда не относится
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    r = await client.get("/updates/PiMusic_1.0.4_x64-setup.exe")
    assert r.status == 200 and r.headers["Content-Type"] == "application/octet-stream"
    assert r.headers["Cache-Control"] == webapp.UPDATE_BINARY_CACHE and len(await r.read()) == 1024
    r = await client.get("/updates/PiMusic_1.0.4_x64-setup.exe.sig")
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/plain") and await r.text() == "sig"
    r = await client.get("/updates/pimusic-1.0.4.apk")
    assert r.status == 200 and r.headers["Content-Type"] == "application/vnd.android.package-archive"


async def test_updates_range(client, upd):
    r = await client.get("/updates/PiMusic_1.0.4_x64-setup.exe", headers={"Range": "bytes=10-19"})
    assert r.status == 206 and await r.read() == bytes(range(10, 20))
    assert r.headers["Content-Range"] == "bytes 10-19/1024"


async def test_updates_rejects_bad_names(client, upd):
    bad = ["notes.txt", ".hidden.json", "sub", "sub/a.json", "sub%2Fa.json", "..%2Fsecret.json", "../secret.json",
           "..", "...json", "a..json", "missing.json", "%2E%2E%2Fsecret.json", "latest.json%00", "лат.json",
           "a b.json", "x" * 200 + ".json"]
    for name in bad:
        r = await client.get("/updates/" + name)
        assert r.status == 404, name
    assert (await client.get("/updates/")).status == 404  # листинга нет


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="нет symlink")
async def test_updates_symlink_outside_not_served(client, upd, cfg):
    import os
    try:
        os.symlink(cfg.media_root / "secret.json", upd / "evil.json")
        os.symlink(upd / "latest.json", upd / "alias.json")
    except OSError:
        pytest.skip("симлинки недоступны (Windows без прав)")
    assert (await client.get("/updates/evil.json")).status == 404
    assert (await client.get("/updates/alias.json")).status == 200  # внутри каталога — можно


async def test_updates_dir_missing_is_404(client, cfg):
    assert not cfg.updates_dir.exists()
    assert (await client.get("/updates/latest.json")).status == 404


def test_updates_dir_from_env(cfg, monkeypatch, tmp_path):
    from conftest import patch_cfg
    new = patch_cfg(monkeypatch, media_root=cfg.media_root, state_dir=cfg.state_dir,
                    updates_dir_env=str(tmp_path / "upd"))
    assert new.updates_dir == tmp_path / "upd"
    assert cfg.updates_dir == cfg.media_root / "updates"
