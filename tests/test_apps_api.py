"""Фаза 2, бэкенд: текст песни, громкость (ReplayGain), настройки по ключам, вход вне Telegram, устройства, CORS."""
import asyncio
import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import auth, loudness, lyrics, webapp
from app.db import User
from app.service import Service

from test_webapp import TOKEN, FakeSvc, hdr
from test_bot_handlers import bot, ctx, make_cb, make_update  # noqa: F401 — фикстура bot

ALICE, BOB = 4242, 555


def add_user(db, login, tg_id):
    u = User(login, tg_id, "jf-" + login, "lib-" + login, "st" + login, False)
    db.upsert_user(u)
    return u


def add_track(db, cfg, login, artist, title, *, link=True):
    d = cfg.library_dir / artist / "Album"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{title}.mp3"
    p.write_bytes(b"ID3" + b"\0" * 64)
    tid = db.add_track(match_key=f"{artist}|{title}".lower(), mbid=None, isrc=None, artist=artist, title=title,
                       album="Album", duration=100, path=str(p), source="test")
    if link:
        db.link_track(login, tid, str(cfg.users_dir / login / artist / "Album" / p.name))
    return tid


@pytest.fixture
async def client(db, cfg):
    add_user(db, "alice", ALICE)
    add_user(db, "bob", BOB)
    app = web.Application()
    w = webapp.setup_webapp(app, db=db, svc=FakeSvc(), jf=MagicMock(), bot_token=TOKEN)
    w.bot_username = "pimusic_bot"
    c = TestClient(TestServer(app))
    await c.start_server()
    c.w, c.db = w, db
    yield c
    await c.close()


async def jreq(client, method, path, status=200, **kw):
    r = await client.request(method, path, **kw)
    assert r.status == status, (path, r.status, await r.text())
    return await r.json()


# ------------------------------------------------------------------ текст песни
def test_parse_lrc_lines_multi_stamps_and_offset():
    text = "[ar:Кино]\n[offset:+500]\n[00:10.50][01:00.00]Припев\n[00:05.1]Начало\n[00:00.20]Ноль"
    assert lyrics.parse_lrc_lines(text) == [
        {"t": 0.0, "text": "Ноль"}, {"t": 4.6, "text": "Начало"}, {"t": 10.0, "text": "Припев"},
        {"t": 59.5, "text": "Припев"}]
    assert lyrics.parse_lrc_lines("[offset:-1000]\n[00:01.00]a")[0]["t"] == 2.0


async def test_lyrics_endpoint_sources_and_access(client, db, cfg):
    tid = add_track(db, cfg, "alice", "Кино", "Звезда")
    path = Path(db.get_track(tid).path)
    r = await jreq(client, "GET", f"/api/lyrics/{tid}", headers=hdr())
    assert r == {"synced": None, "plain": None, "source": None}
    path.with_suffix(".txt").write_text("Просто текст\nвторая строка\n", encoding="utf-8")
    r = await jreq(client, "GET", f"/api/lyrics/{tid}", headers=hdr())
    assert r == {"synced": None, "plain": "Просто текст\nвторая строка", "source": "lrc"}
    lrc = path.with_suffix(".lrc")
    lrc.write_text("[00:01.00]Раз\n[00:02.00]Два\n[00:03.50]Три\n", encoding="utf-8")
    r = await jreq(client, "GET", f"/api/lyrics/{tid}", headers=hdr())  # кэш сбросился по mtime
    assert r["source"] == "lrc" and [x["t"] for x in r["synced"]] == [1.0, 2.0, 3.5]
    assert r["plain"] == "Раз\nДва\nТри"
    # чужой трек и мусорный id
    other = add_track(db, cfg, "bob", "Земфира", "Хочешь")
    await jreq(client, "GET", f"/api/lyrics/{other}", status=404, headers=hdr())
    await jreq(client, "GET", f"/api/lyrics/{2 ** 70}", status=404, headers=hdr())
    assert (await client.get(f"/api/lyrics/{tid}")).status == 401


def test_lyrics_from_uslt_tag(db, cfg, tmp_path):
    from mutagen.id3 import ID3, USLT
    f = tmp_path / "t.mp3"
    f.write_bytes(b"\0" * 32)
    tags = ID3()
    tags.add(USLT(encoding=3, lang="XXX", desc="", text="[00:01.00]a\n[00:02.00]b\n[00:03.00]c"))
    tags.save(str(f))
    res = lyrics.read_local(777, [None, str(f)])
    assert res["source"] == "tags" and len(res["synced"]) == 3


# ------------------------------------------------------------------ громкость
EBUR = """[Parsed_ebur128_0 @ 0x55] Summary:

  Integrated loudness:
    I:         -9.1 LUFS
    Threshold: -19.4 LUFS

  Loudness range:
    LRA:        4.2 LU

  True peak:
    Peak:       0.4 dBFS
"""


def test_parse_ebur128_and_loudnorm():
    gain, peak = loudness.parse_ebur128(EBUR)
    assert gain == -4.9 and peak == pytest.approx(1.0471, abs=1e-3)
    assert loudness.parse_ebur128("Summary:\n Integrated loudness:\n I: -inf LUFS") is None
    assert loudness.parse_loudnorm('noise\n{"input_i" : "-20.00", "input_tp" : "-1.00"}') == (6.0, 0.8913)
    assert loudness.parse_loudnorm("no json") is None
    assert loudness.parse_ebur128(EBUR.replace("-9.1", "-80.0"))[0] == 20.0  # предел усиления


def test_replaygain_from_tags(tmp_path):
    from mutagen.id3 import ID3, TXXX
    f = tmp_path / "t.mp3"
    f.write_bytes(b"\0" * 32)
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-3.20 dB"))
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text="0.988"))
    tags.save(str(f))
    assert loudness.from_tags(f) == (0.8, 0.988)  # −3.2 дБ от −18 LUFS = +0.8 дБ от −14 LUFS
    assert loudness.from_tags(tmp_path / "missing.mp3") is None


async def test_analyze_ffmpeg_and_graceful_fallback(tmp_path, monkeypatch):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"\0")
    calls = []

    async def fake_run(cmd, timeout=None, cwd=None, **kw):
        calls.append(cmd)
        if "-filters" in cmd:
            return SimpleNamespace(rc=0, out=" ... ebur128 A->N EBU R128 scanner.", err="")
        return SimpleNamespace(rc=0, out="", err=EBUR)
    monkeypatch.setattr(loudness, "run", fake_run)
    monkeypatch.setattr(loudness, "_filters", None)
    assert await loudness.analyze(f) == (-4.9, pytest.approx(1.0471, abs=1e-3))
    assert any("ebur128=peak=true" in c for c in calls[-1])
    # ffmpeg без нужных фильтров: None и available() is False
    async def no_filters(cmd, timeout=None, cwd=None, **kw):
        return SimpleNamespace(rc=0, out="aresample volume", err="")
    monkeypatch.setattr(loudness, "run", no_filters)
    monkeypatch.setattr(loudness, "_filters", None)
    assert await loudness.analyze(f) is None and loudness.available() is False


async def test_loudness_loop_stores_and_stops_without_ffmpeg(db, cfg, monkeypatch):
    add_user(db, "alice", ALICE)
    t1 = add_track(db, cfg, "alice", "A", "one")
    t2 = add_track(db, cfg, "alice", "A", "two")
    seen = []

    async def fake_analyze(path):
        seen.append(Path(path).name)
        return (-2.5, 0.9) if "two" in str(path) else None
    monkeypatch.setattr(loudness, "analyze", fake_analyze)
    monkeypatch.setattr(loudness, "_filters", "ebur128")
    svc = Service(db, None)
    task = asyncio.create_task(svc._loudness_loop(first_delay=0, idle=0.05))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if db.loudness_pending() == 0:
            break
    task.cancel()
    assert seen[:2] == ["two.mp3", "one.mp3"]  # новые первыми
    assert (db.get_track(t2).gain_db, db.get_track(t2).peak) == (-2.5, 0.9)
    assert db.get_track(t1).gain_db is None and db.loudness_pending() == 0
    # нечем считать — цикл выходит, треки не помечаются неудачными
    t3 = add_track(db, cfg, "alice", "A", "three")

    async def nothing(path):
        return None
    monkeypatch.setattr(loudness, "analyze", nothing)
    monkeypatch.setattr(loudness, "_filters", "")
    await asyncio.wait_for(svc._loudness_loop(first_delay=0), 1)
    assert db.loudness_pending() == 1 and db.get_track(t3).gain_db is None


async def test_track_json_has_gain_and_peak(client, db, cfg):
    t1 = add_track(db, cfg, "alice", "A", "one")
    add_track(db, cfg, "alice", "A", "two")
    db.set_loudness(t1, -3.14159, 0.987654)
    items = {i["id"]: i for i in (await jreq(client, "GET", "/api/library", headers=hdr()))["items"]}
    assert items[t1]["gain"] == -3.14 and items[t1]["peak"] == 0.9877
    assert [i["gain"] for i in items.values() if i["id"] != t1] == [None]


# ------------------------------------------------------------------ настройки по ключам
async def test_settings_per_key_with_optimistic_concurrency(client):
    assert await jreq(client, "GET", "/api/settings", headers=hdr()) == {"items": {}}
    r = await jreq(client, "PUT", "/api/settings/eq.presets", json={"value": [{"name": "Бас+"}]}, headers=hdr())
    ts = r["updated_at"]
    assert r["value"] == [{"name": "Бас+"}] and isinstance(ts, int)
    r2 = await jreq(client, "PUT", "/api/settings/eq.presets", json={"value": [], "base": ts}, headers=hdr())
    assert r2["updated_at"] > ts
    # второе устройство с устаревшим base — 409 и актуальное значение
    r = await jreq(client, "PUT", "/api/settings/eq.presets", status=409,
                   json={"value": ["старое"], "base": ts}, headers=hdr())
    assert r["error"] == "conflict" and r["current"] == {"value": [], "updated_at": r2["updated_at"]}
    # base для ещё не существующего ключа — тоже конфликт
    r = await jreq(client, "PUT", "/api/settings/player", status=409, json={"value": 1, "base": 5}, headers=hdr())
    assert r["current"] is None
    await jreq(client, "PUT", "/api/settings/eq.device.0b6e-uuid", json={"value": {"mode": "local"}}, headers=hdr())
    items = (await jreq(client, "GET", "/api/settings", headers=hdr()))["items"]
    assert set(items) == {"eq.presets", "eq.device.0b6e-uuid"} and items["eq.presets"]["value"] == []
    assert (await jreq(client, "GET", "/api/settings", headers=hdr(BOB)))["items"] == {}  # чужие не видны
    assert await jreq(client, "DELETE", "/api/settings/eq.presets", headers=hdr()) == {"ok": True, "deleted": True}
    assert (await jreq(client, "DELETE", "/api/settings/eq.presets", headers=hdr()))["deleted"] is False


async def test_settings_validation_and_limits(client, monkeypatch):
    for key in ("EQ", "a" * 81, "eq presets", "ключ"):
        r = await client.put(f"/api/settings/{key}", json={"value": 1}, headers=hdr())
        assert r.status in (400, 404), key
    await jreq(client, "PUT", "/api/settings/x", status=400, json={"nope": 1}, headers=hdr())
    await jreq(client, "PUT", "/api/settings/x", status=400, json={"value": 1, "base": "1"}, headers=hdr())
    await jreq(client, "PUT", "/api/settings/x", status=400, data=b"{bad", headers=hdr())
    await jreq(client, "PUT", "/api/settings/x", status=413, json={"value": "я" * 20000}, headers=hdr())
    await jreq(client, "PUT", "/api/settings/x", status=413, data=b'{"value":"' + b"a" * 50000 + b'"}',
               headers=hdr())
    monkeypatch.setattr(webapp, "SETTING_MAX_KEYS", 2)
    await jreq(client, "PUT", "/api/settings/a", json={"value": 1}, headers=hdr())
    await jreq(client, "PUT", "/api/settings/b", json={"value": 1}, headers=hdr())
    r = await jreq(client, "PUT", "/api/settings/c", status=409, json={"value": 1}, headers=hdr())
    assert r["error"] == "limit"
    await jreq(client, "PUT", "/api/settings/a", json={"value": 2}, headers=hdr())  # обновление — можно


# ------------------------------------------------------------------ вход по ссылке в бота
async def _approve(db, nonce, login="alice", approve=True):
    row = db.nonce_by_hash(auth.sha256(nonce))
    assert db.decide_nonce(row["id"], login, approve)


async def test_deep_link_login_flow(client, db):
    r = await jreq(client, "POST", "/api/auth/start", json={"device": "Ноутбук\x00", "platform": "windows"})
    nonce = r["nonce"]
    assert r["link"] == f"https://t.me/pimusic_bot?start=login_{nonce}" and r["expires_in"] == 300
    assert len("login_" + nonce) <= 64
    assert db.nonce_by_hash(nonce) is None and db.nonce_by_hash(auth.sha256(nonce))  # в базе только хэш
    assert await jreq(client, "POST", "/api/auth/poll", json={"nonce": nonce}) == {"status": "pending"}
    await _approve(db, nonce)
    r = await jreq(client, "POST", "/api/auth/poll", json={"nonce": nonce})
    assert r["status"] == "ok" and r["login"] == "alice"
    token = r["token"]
    assert await jreq(client, "POST", "/api/auth/poll", json={"nonce": nonce}) == {"status": "expired"}  # один раз
    dev = db.user_devices("alice")[0]
    assert dev["name"] == "Ноутбук" and dev["platform"] == "windows"
    assert dev["token_hash"] == auth.sha256(token) and token not in str(dict(dev))
    # Bearer работает наравне с initData
    bearer = {"Authorization": f"Bearer {token}"}
    me = await jreq(client, "GET", "/api/me", headers=bearer)
    assert me["login"] == "alice"
    devs = (await jreq(client, "GET", "/api/devices", headers=bearer))["items"]
    assert devs[0]["current"] is True and devs[0]["platform"] == "windows"
    assert (await jreq(client, "GET", "/api/devices", headers=hdr()))["items"][0]["current"] is False
    assert await jreq(client, "POST", "/api/auth/logout", headers=bearer) == {"ok": True, "revoked": True}
    assert (await client.get("/api/me", headers=bearer)).status == 401


async def test_poll_denied_expired_and_bad(client, db):
    n1 = (await jreq(client, "POST", "/api/auth/start"))["nonce"]
    await _approve(db, n1, approve=False)
    assert await jreq(client, "POST", "/api/auth/poll", json={"nonce": n1}) == {"status": "denied"}
    n2 = (await jreq(client, "POST", "/api/auth/start"))["nonce"]
    db._x("UPDATE auth_nonces SET expires_at=? WHERE nonce_hash=?", (int(time.time()) - 1, auth.sha256(n2)))
    assert await jreq(client, "POST", "/api/auth/poll", json={"nonce": n2}) == {"status": "expired"}
    row = db.nonce_by_hash(auth.sha256(n2))
    assert not db.decide_nonce(row["id"], "alice", True)  # истёкший уже не разрешить
    assert await jreq(client, "POST", "/api/auth/poll", json={"nonce": "x" * 32}) == {"status": "expired"}
    await jreq(client, "POST", "/api/auth/poll", status=400, json={"nonce": "short"})
    await jreq(client, "POST", "/api/auth/poll", status=400)
    for bad in ("Bearer", "Bearer " + "a" * 43, "Bearer bad token!", "Basic xyz"):
        assert (await client.get("/api/me", headers={"Authorization": bad})).status == 401, bad


async def test_auth_start_rate_limit_per_client_ip(client):
    for i in range(10):
        await jreq(client, "POST", "/api/auth/start", headers={"X-Forwarded-For": "93.184.216.5"})
    r = await jreq(client, "POST", "/api/auth/start", status=429, headers={"X-Forwarded-For": "93.184.216.5"})
    assert r["error"] == "rate_limited"
    # другой клиент за тем же прокси — свой лимит; подделка левой части цепочки не помогает
    await jreq(client, "POST", "/api/auth/start", headers={"X-Forwarded-For": "93.184.216.6"})
    await jreq(client, "POST", "/api/auth/start", status=429,
               headers={"X-Forwarded-For": "1.2.3.4, 93.184.216.5"})


def test_client_ip_trusts_forwarded_only_from_lan():
    assert auth.client_ip("93.184.216.9", "1.1.1.1") == "93.184.216.9"       # не прокси — заголовок игнорируем
    assert auth.client_ip("10.0.0.2", "8.8.8.8") == "10.0.0.2"  # по умолчанию LAN не доверена — только loopback
    lan = auth.parse_networks(auth.PRIVATE_NETWORKS)
    assert auth.client_ip("10.0.0.2", "8.8.8.8", lan) == "8.8.8.8"
    assert auth.client_ip("172.17.0.1", "6.6.6.6, 8.8.8.8, 192.168.1.1", lan) == "8.8.8.8"
    assert auth.client_ip("127.0.0.1", "192.168.1.5", lan) == "127.0.0.1"    # вся цепочка — прокси: адрес соединения
    assert auth.client_ip("127.0.0.1", "192.168.1.5") == "192.168.1.5"       # без LAN в доверенных — это клиент
    assert auth.client_ip("127.0.0.1", None) == "127.0.0.1"
    assert auth.client_ip("::1", "garbage") == "::1"
    # свой список доверенных прокси (TRUSTED_PROXIES): частная сеть больше не доверена
    nets = auth.parse_networks(["203.0.113.7/32", "мусор"])
    assert auth.client_ip("10.0.0.2", "8.8.8.8", nets) == "10.0.0.2"
    assert auth.client_ip("203.0.113.7", "8.8.8.8", nets) == "8.8.8.8"
    assert auth.parse_networks(["мусор"]) == auth.parse_networks(())  # пусто/мусор — частные сети


def test_rate_key_groups_ipv6_by_64():
    assert auth.rate_key("2001:db8:1:2:aaaa::1") == auth.rate_key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert auth.rate_key("2001:db8:1:3::1") != auth.rate_key("2001:db8:1:2::1")
    assert auth.rate_key("::ffff:8.8.8.8") == "8.8.8.8" and auth.rate_key("8.8.4.4") == "8.8.4.4"
    assert auth.rate_key("?") == "?"


def test_rate_limiter_window():
    rl = auth.RateLimiter(2, 10)
    assert rl.allow("a", 0) and rl.allow("a", 1) and not rl.allow("a", 2)
    assert rl.allow("a", 11.5) and rl.allow("b", 2)
    rl.allow("c", 12)
    rl.allow("x", 40)  # прошло окно — устаревшие ключи вычищены разом
    assert set(rl._hits) == {"x"}


def test_device_limit_refuses_and_idle_devices_dont_count(db, cfg):
    from app.db import DeviceLimit
    add_user(db, "alice", ALICE)
    for i in range(3):
        db.add_device("alice", f"d{i}", "web", f"h{i}", max_devices=3)
    with pytest.raises(DeviceLimit):
        db.add_device("alice", "d3", "web", "h3", max_devices=3)
    old = db.user_devices("alice")[-1]
    db.touch_device(old["id"], int(time.time()) - 100 * 86400)  # 100 дней не появлялся
    db.add_device("alice", "d3", "web", "h3", max_devices=3, idle_ttl=auth.DEVICE_IDLE_TTL)
    assert sorted(r["name"] for r in db.user_devices("alice")) == sorted(
        n for n in ("d0", "d1", "d2", "d3") if n != old["name"])


async def test_poll_too_many_devices_keeps_nonce(client, db, monkeypatch):
    monkeypatch.setattr(auth, "MAX_DEVICES", 1)
    db.add_device("alice", "old", "web", "hh")
    nonce = (await jreq(client, "POST", "/api/auth/start"))["nonce"]
    await _approve(db, nonce)
    r = await jreq(client, "POST", "/api/auth/poll", json={"nonce": nonce})
    assert r["status"] == "too_many_devices" and "/devices" in r["message"]
    db.revoke_all_devices("alice")  # вышли на старом — тот же вход завершается
    assert (await jreq(client, "POST", "/api/auth/poll", json={"nonce": nonce}))["status"] == "ok"


async def test_idle_token_expires_and_revoked_codes(client, db):
    token, h = auth.new_token()
    did = db.add_device("alice", "phone", "android", h)
    bearer = {"Authorization": f"Bearer {token}"}
    assert (await jreq(client, "GET", "/api/me", headers=bearer))["login"] == "alice"
    db.touch_device(did, int(time.time()) - auth.DEVICE_IDLE_TTL - 10)
    r = await jreq(client, "GET", "/api/me", status=401, headers=bearer)
    assert r["error"] == "token_expired" and db.user_devices("alice") == []
    r = await jreq(client, "GET", "/api/me", status=401, headers=bearer)
    assert r["error"] == "revoked"


async def test_logout_all_devices(client, db):
    tokens = []
    for i in range(3):
        t, h = auth.new_token()
        db.add_device("alice", f"d{i}", "web", h)
        tokens.append(t)
    db.add_device("bob", "bob", "web", "hb")
    r = await jreq(client, "POST", "/api/auth/logout", json={"all": True},
                   headers={"Authorization": f"Bearer {tokens[0]}"})
    assert r == {"ok": True, "revoked": 3} and db.user_devices("alice") == [] and db.user_devices("bob")
    assert (await client.get("/api/me", headers={"Authorization": f"Bearer {tokens[1]}"})).status == 401
    db.add_device("alice", "x", "web", "hx")
    assert (await jreq(client, "POST", "/api/devices/revoke-all", headers=hdr()))["revoked"] == 1


async def test_bearer_without_telegram_gets_own_search_owner(client, db):
    db.upsert_user(User("solo", None, "jf-solo", "lib", "st", False))
    t, h = auth.new_token()
    db.add_device("solo", "x", "web", h)
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {t}"}
    user, tg, err = client.w.auth(req)
    assert err is None and user.login == "solo" and tg < 0
    assert client.w.auth(req)[1] == tg  # устойчивый псевдо-id: результаты поиска привязаны к логину


async def test_devices_revoke_own_only(client, db):
    did = db.add_device("bob", "Телефон Боба", "android", auth.sha256("t" * 43))
    await jreq(client, "POST", f"/api/devices/{did}/revoke", status=404, headers=hdr())  # чужое
    assert (await client.post("/api/devices/abc/revoke", headers=hdr())).status == 404
    assert await jreq(client, "POST", f"/api/devices/{did}/revoke", headers=hdr(BOB)) == {"ok": True}
    assert db.user_devices("bob") == []
    assert await jreq(client, "POST", "/api/auth/logout", headers=hdr()) == {"ok": True, "revoked": False}


def test_clean_device_strips_bidi_and_invisible():
    name, plat = auth.clean_device("Ноут‮теба​к﻿⁦x⁩\x00  y", "windows")
    assert name == "Ноут теба к x y" and plat == "windows"
    assert auth.clean_device("a" * 100, "evil")[1] == "web"


async def test_start_returns_code_and_stores_ip(client, db):
    r = await jreq(client, "POST", "/api/auth/start", headers={"X-Forwarded-For": "93.184.216.77"})
    assert len(r["code"]) == 4 and r["code"].isdigit()
    row = db.nonce_by_hash(auth.sha256(r["nonce"]))
    assert row["code"] == r["code"] and row["client_ip"] == "93.184.216.77"


async def test_auth_start_global_limit(client):
    client.w._start_global = auth.RateLimiter(2, 600)
    for i in range(2):
        await jreq(client, "POST", "/api/auth/start", headers={"X-Forwarded-For": f"93.184.216.{i + 1}"})
    await jreq(client, "POST", "/api/auth/start", status=429, headers={"X-Forwarded-For": "93.184.216.50"})


async def test_cover_url_has_expiry(client, db, cfg):
    tid = add_track(db, cfg, "alice", "A", "cov")
    (Path(db.get_track(tid).path).parent / "cover.jpg").write_bytes(b"\xff\xd8\xff-c")
    url = webapp.cover_url(TOKEN, "alice", tid)
    assert "&e=" in url and (await client.get(url)).status == 200
    old = int(time.time()) - 10
    sig = webapp.cover_signature(TOKEN, "alice", tid, old)
    assert (await client.get(f"/api/cover/{tid}?u=alice&s={sig}&e={old}")).status == 404
    exp = webapp.cover_expiry()
    sig = webapp.cover_signature(TOKEN, "alice", tid, exp)
    assert (await client.get(f"/api/cover/{tid}?u=alice&s={sig}&e={exp + 3600}")).status == 404
    assert (await client.get(f"/api/cover/{tid}?u=alice&s={sig}")).status == 404


async def test_service_worker_and_manifest_routes(client):
    r = await client.get("/app/sw.js")
    if (webapp.STATIC_DIR / "sw.js").is_file():
        assert r.status == 200 and r.headers["Service-Worker-Allowed"] == "/app/"
        assert r.headers["Content-Type"].startswith("text/javascript") and r.headers["Cache-Control"] == "no-cache"
    else:
        assert r.status == 404
    r = await client.get("/app/manifest.webmanifest")
    assert r.status in (200, 404)
    if r.status == 200:
        assert r.headers["Content-Type"].startswith("application/manifest+json")


# ------------------------------------------------------------------ Telegram Login Widget
def widget(tg_id=ALICE, *, token=TOKEN, auth_date=None, **extra):
    data = {"id": tg_id, "first_name": "Алиса", "username": "alice", "auth_date": auth_date or int(time.time()),
            **extra}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return data


async def test_login_widget(client, db, cfg, monkeypatch):
    from conftest import patch_cfg
    assert await jreq(client, "GET", "/api/auth/config") == {"bot": "pimusic_bot", "login_widget": False}
    await jreq(client, "POST", "/api/auth/telegram", status=404, json=widget())
    patch_cfg(monkeypatch, media_root=cfg.media_root, state_dir=cfg.state_dir, login_widget=True)
    assert (await jreq(client, "GET", "/api/auth/config"))["login_widget"] is True
    r = await jreq(client, "POST", "/api/auth/telegram", json={**widget(), "platform": "pwa", "device": "Chrome"})
    assert r["status"] == "ok" and r["login"] == "alice"
    assert db.user_devices("alice")[0]["platform"] == "pwa"
    assert (await jreq(client, "GET", "/api/me", headers={"Authorization": f"Bearer {r['token']}"}))["login"] == "alice"
    replay = widget(auth_date=int(time.time()) - 1, last_name="Повтор")  # отличается от первого входа
    await jreq(client, "POST", "/api/auth/telegram", json=replay)
    r = await jreq(client, "POST", "/api/auth/telegram", status=401, json=replay)  # те же данные второй раз
    assert "уже использованы" in r["message"]
    await jreq(client, "POST", "/api/auth/telegram", status=401, json=widget(auth_date=int(time.time()) - 400))
    bad = widget()
    bad["first_name"] = "Ева"
    await jreq(client, "POST", "/api/auth/telegram", status=401, json=bad)
    await jreq(client, "POST", "/api/auth/telegram", status=401, json=widget(token="1:other"))
    await jreq(client, "POST", "/api/auth/telegram", status=401, json=widget(auth_date=int(time.time()) - 90000))
    await jreq(client, "POST", "/api/auth/telegram", status=403, json=widget(tg_id=31337))
    await jreq(client, "POST", "/api/auth/telegram", status=401, json={"id": 1})


def test_check_widget_rejects_odd_values():
    assert auth.check_widget({**widget(), "id": True}, TOKEN) is None
    assert auth.check_widget({**widget(), "photo_url": ["x"]}, TOKEN) is None
    assert auth.check_widget(widget(), "") is None
    assert auth.check_widget(widget(photo_url="https://t.me/i/u.jpg"), TOKEN)["photo_url"]


# ------------------------------------------------------------------ CORS
async def test_cors_preflight_and_headers(client, db, cfg):
    tid = add_track(db, cfg, "alice", "A", "one")
    r = await client.options("/api/library", headers={"Origin": "tauri://localhost",
                                                      "Access-Control-Request-Method": "GET"})
    assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "tauri://localhost"
    assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
    r = await client.get("/api/library", headers={**hdr(), "Origin": "capacitor://localhost"})
    assert r.status == 200 and r.headers["Access-Control-Allow-Origin"] == "capacitor://localhost"
    assert "Origin" in r.headers["Vary"]
    url = webapp.stream_url(TOKEN, "alice", tid)
    r = await client.get(url, headers={"Origin": "https://tauri.localhost", "Range": "bytes=0-9"})
    assert r.status == 206 and "Content-Range" in r.headers["Access-Control-Expose-Headers"]
    assert r.headers["Access-Control-Allow-Origin"] == "https://tauri.localhost"
    r = await client.get(f"/api/cover/{tid}?u=alice&s=bad", headers={"Origin": "http://localhost"})
    assert r.status == 404 and r.headers["Access-Control-Allow-Origin"] == "http://localhost"
    r = await client.get("/api/library", headers={**hdr(), "Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    r = await client.options("/api/library", headers={"Origin": "https://evil.example"})
    assert r.status in (403, 405) and "Access-Control-Allow-Origin" not in r.headers
    r = await client.get("/app/", headers={"Origin": "tauri://localhost"})
    assert "Access-Control-Allow-Origin" not in r.headers  # только /api/


# ------------------------------------------------------------------ бот: подтверждение входа, /devices
def _nonce(db, device="", platform="web", code="4821", ip="93.184.216.9"):
    nonce, h = auth.new_nonce()
    db.create_nonce(h, device, platform, 300, 100, client_ip=ip, code=code)
    return nonce, db.nonce_by_hash(h)["id"]


async def test_bot_login_requires_code_from_screen(bot, db):  # noqa: F811
    add_user(db, "alice", 1)
    nonce, rid = _nonce(db, "Ноутбук <b>", "windows")
    upd = make_update(tg_id=1)
    await bot.cmd_start(upd, ctx(f"login_{nonce}"))
    call = upd.effective_message.reply_html.call_args
    text = call.args[0]
    assert "Войти в PiMusic на новом устройстве?" in text and "Ноутбук &lt;b&gt;" in text and "Windows" in text
    assert "93.184.216.9" in text and "виден на экране вашего устройства" in text
    assert "4821" not in text  # код в тексте не показываем — его нужно найти на экране
    rows = call.kwargs["reply_markup"].inline_keyboard
    codes = [b.text for b in rows[0]]
    assert len(codes) == 3 and "4821" in codes and len(set(codes)) == 3
    assert [b.callback_data for b in rows[1]] == [f"auth:no:{rid}"]
    # «Разрешить» без кода не принимается, неверный код — отказ
    await bot.on_auth(make_cb(f"auth:ok:{rid}", tg_id=1), ctx())
    assert db.get_nonce(rid)["status"] == "denied"
    nonce, rid = _nonce(db)
    await bot.cmd_start(make_update(tg_id=1), ctx(f"login_{nonce}"))
    cb = make_cb(f"auth:c:{rid}:4821", tg_id=1)
    await bot.on_auth(cb, ctx())
    assert db.get_nonce(rid)["status"] == "approved" and db.get_nonce(rid)["login"] == "alice"
    assert "разрешён" in cb.callback_query.edit_message_text.call_args.args[0]
    await bot.on_auth(make_cb(f"auth:no:{rid}", tg_id=1), ctx())  # повторное нажатие — уже решено
    assert db.get_nonce(rid)["status"] == "approved"
    nonce, rid = _nonce(db)
    await bot.cmd_start(make_update(tg_id=1), ctx(f"login_{nonce}"))
    cb = make_cb(f"auth:c:{rid}:0000", tg_id=1)
    await bot.on_auth(cb, ctx())
    assert db.get_nonce(rid)["status"] == "denied"
    assert "Код не совпал" in cb.callback_query.edit_message_text.call_args.args[0]


async def test_bot_login_first_opener_owns_link(bot, db):  # noqa: F811
    add_user(db, "alice", 1)
    add_user(db, "bob", 2)
    nonce, rid = _nonce(db)
    await bot.cmd_start(make_update(tg_id=1), ctx(f"login_{nonce}"))
    upd = make_update(tg_id=2)
    await bot.cmd_start(upd, ctx(f"login_{nonce}"))
    assert "другой аккаунт" in upd.effective_message.reply_text.call_args.args[0]
    await bot.on_auth(make_cb(f"auth:c:{rid}:4821", tg_id=2), ctx())  # нажать чужую кнопку тоже нельзя
    assert db.get_nonce(rid)["status"] == "pending"
    # не открывший ссылку вообще не может решить
    nonce2, rid2 = _nonce(db)
    await bot.on_auth(make_cb(f"auth:c:{rid2}:4821", tg_id=1), ctx())
    assert db.get_nonce(rid2)["status"] == "pending"


async def test_bot_login_expired_and_unregistered(bot, db):  # noqa: F811
    add_user(db, "alice", 1)
    nonce, rid = _nonce(db)
    db._x("UPDATE auth_nonces SET expires_at=? WHERE id=?", (int(time.time()) - 1, rid))
    upd = make_update(tg_id=1)
    await bot.cmd_start(upd, ctx(f"login_{nonce}"))
    assert "устарела" in upd.effective_message.reply_text.call_args.args[0]
    n3, rid3 = _nonce(db)
    upd = make_update(tg_id=99)
    await bot.cmd_start(upd, ctx(f"login_{n3}"))
    assert "/register" in upd.effective_message.reply_html.call_args.args[0]
    await bot.on_auth(make_cb(f"auth:c:{rid3}:4821", tg_id=99), ctx())
    assert db.get_nonce(rid3)["status"] == "pending"


async def test_bot_devices_list_and_revoke(bot, db):  # noqa: F811
    add_user(db, "alice", 1)
    add_user(db, "bob", 2)
    d1 = db.add_device("alice", "Ноутбук", "windows", "h1")
    d2 = db.add_device("bob", "Телефон", "android", "h2")
    upd = make_update(tg_id=1)
    await bot.cmd_devices(upd, ctx())
    text = upd.effective_message.reply_html.call_args.args[0]
    assert "Ноутбук" in text and "Windows" in text and "Телефон" not in text
    await bot.on_device(make_cb(f"dev:rv:{d2}", tg_id=1), ctx())  # чужое — нельзя
    assert db.user_devices("bob")
    rows = upd.effective_message.reply_html.call_args.kwargs["reply_markup"].inline_keyboard
    assert rows[-1][0].callback_data == "dev:all:0"
    cb = make_cb(f"dev:rv:{d1}", tg_id=1)
    await bot.on_device(cb, ctx())
    assert db.user_devices("alice") == []
    assert "Устройств вне Telegram нет" in cb.callback_query.edit_message_text.call_args.args[0]
    db.add_device("alice", "a", "web", "ha")
    db.add_device("alice", "b", "web", "hb")
    await bot.on_device(make_cb("dev:all:0", tg_id=1), ctx())
    assert db.user_devices("alice") == [] and db.user_devices("bob")


# ------------------------------------------------------------------ кэш статики Mini App
async def test_static_assets_revalidate_but_api_headers_unchanged(client, db, cfg):
    js = sorted((webapp.STATIC_DIR / "js").glob("*.js"))
    assert js, "в static/js нет модулей"
    r = await client.get(f"/app/static/js/{js[0].name}")
    assert r.status == 200 and r.headers["Cache-Control"] == "no-cache"
    etag = r.headers.get("ETag")
    if etag:  # повторный запрос с ETag — 304, а не скачивание заново
        r2 = await client.get(f"/app/static/js/{js[0].name}", headers={"If-None-Match": etag})
        assert r2.status == 304
    tid = add_track(db, cfg, "alice", "A", "static-cov")
    (Path(db.get_track(tid).path).parent / "cover.jpg").write_bytes(b"\xff\xd8\xff-c")
    r = await client.get(webapp.cover_url(TOKEN, "alice", tid))
    assert r.headers["Cache-Control"] == "max-age=86400"
    r = await client.get(webapp.stream_url(TOKEN, "alice", tid))
    assert r.headers["Cache-Control"] == "private, max-age=3600"
    r = await client.get("/api/library", headers=hdr())
    assert r.headers["Cache-Control"] == "no-store"
    assert (await client.get("/app/")).headers["Cache-Control"] == "no-cache"
