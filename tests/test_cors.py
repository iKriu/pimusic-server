"""CORS для нативных оболочек (Windows 2.0 — http://tauri.localhost): заголовки у ВСЕХ ответов /api/*."""
import time

import aiohttp

from app import webapp

from test_webapp import TOKEN, add_track, alice, client, hdr  # noqa: F401 — фикстуры

ORIGIN = "http://tauri.localhost"
O = {"Origin": ORIGIN}


def has_cors(r, origin=ORIGIN):
    h = r.headers
    return (h.get("Access-Control-Allow-Origin") == origin
            and {"Content-Range", "Accept-Ranges", "Content-Length"} <= {x.strip() for x in
                                                                          h["Access-Control-Expose-Headers"].split(",")}
            and "Origin" in h.get("Vary", ""))


async def test_cors_on_errors_and_json(client):
    r = await client.get("/api/me", headers=O)  # 401
    assert r.status == 401 and has_cors(r)
    r = await client.get("/api/me", headers={**O, "Authorization": "Bearer " + "a" * 43})  # 401 revoked
    assert r.status == 401 and has_cors(r)
    r = await client.get("/api/me", headers={**O, **hdr(777)})  # 403
    assert r.status == 403 and has_cors(r)
    r = await client.get("/api/nope", headers=O)  # 404 роутера
    assert r.status == 404 and has_cors(r)
    r = await client.get("/api/me", headers={**O, **hdr()})
    assert r.status == 200 and has_cors(r)


async def test_cors_on_rate_limit(client):
    for _ in range(12):
        r = await client.post("/api/auth/start", json={}, headers={**O, "X-Forwarded-For": "93.184.216.7"})
    assert r.status == 429 and has_cors(r)


async def test_cors_on_internal_and_bad_request_errors(client, db, cfg):
    async def boom(*a, **kw):
        raise RuntimeError("x")
    client.svc.delete_track = boom
    tid = add_track(db, cfg, "alice", "A", "B")
    r = await client.post("/api/delete", json={"track_id": tid}, headers={**O, **hdr()})
    assert r.status == 500 and has_cors(r)
    r = await client.post("/api/delete", json={"track_id": "x"}, headers={**O, **hdr()})
    assert r.status == 400 and has_cors(r)
    r = await client.post("/api/delete", data=b"not json", headers={**O, **hdr()})  # HTTPBadRequest из body()
    assert r.status == 400 and has_cors(r)


async def test_cors_on_stream_and_cover(client, db, cfg):
    tid = add_track(db, cfg, "alice", "A", "B")
    url = webapp.stream_url(TOKEN, "alice", tid)
    r = await client.get(url, headers={**O, "Range": "bytes=0-1"})
    assert r.status == 206 and has_cors(r)
    r = await client.head(url, headers=O)
    assert r.status == 200 and has_cors(r)
    r = await client.get(url.replace("&s=", "&s=00"), headers=O)
    assert r.status == 404 and has_cors(r)
    r = await client.get(webapp.cover_url(TOKEN, "alice", tid), headers=O)
    assert r.status == 200 and has_cors(r)


async def test_cors_preflight_and_multipart(client):
    for path in ("/api/upload", "/api/import/file", "/api/stream/1", "/api/library"):
        r = await client.options(path, headers={**O, "Access-Control-Request-Method": "POST",
                                                "Access-Control-Request-Headers": "authorization,content-type"})
        assert r.status == 204 and has_cors(r), path
        assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
        assert "POST" in r.headers["Access-Control-Allow-Methods"]
    fd = aiohttp.FormData()
    fd.add_field("file", b"Track name,Artist name\n", filename="a.csv", content_type="text/csv")
    r = await client.post("/api/import/file", data=fd, headers={**O, **hdr()})
    assert r.status in (400, 500, 503) and has_cors(r)  # у FakeSvc нет импорта — важен сам заголовок
    fd = aiohttp.FormData()
    fd.add_field("files", b"ID3", filename="a.mp3", content_type="audio/mpeg")
    r = await client.post("/api/upload", data=fd, headers={**O, **hdr()})
    assert has_cors(r)  # у FakeSvc нет загрузок — 503, но с CORS


async def test_no_cors_for_foreign_origin_or_non_api(client):
    r = await client.get("/api/me", headers={"Origin": "https://evil.example", **hdr()})
    assert "Access-Control-Allow-Origin" not in r.headers
    r = await client.options("/api/me", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    r = await client.get("/app/", headers=O)
    assert "Access-Control-Allow-Origin" not in r.headers


def test_stream_links_live_long_enough():
    now = time.time()
    url = webapp.stream_url(TOKEN, "alice", 1, now=now)
    exp = int(url.split("&e=")[1].split("&")[0])
    assert exp - now >= 6 * 3600  # хватает на полную загрузку и долгое прослушивание
