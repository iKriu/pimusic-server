"""Публичный API для сторонних клиентов: openapi.json полон и актуален, /api/info, радио и админ через API."""
import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import downloaders, webapp
from app.db import User

from test_webapp import TOKEN, FakeSvc, hdr

ROOT = Path(__file__).resolve().parents[1]
ALICE, ADMIN = 4242, 777


def _gen():
    spec = importlib.util.spec_from_file_location("openapi_gen", ROOT / "tools" / "api" / "openapi_gen.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
async def client(db, cfg):
    db.upsert_user(User("alice", ALICE, "jf-a", "lib-a", "sta", False))
    db.upsert_user(User("root", ADMIN, "jf-r", "lib-r", "str", True))
    app = web.Application()
    w = webapp.setup_webapp(app, db=db, svc=FakeSvc(), jf=MagicMock(), bot_token=TOKEN)
    w.bot_username = "pimusic_bot"
    c = TestClient(TestServer(app))
    await c.start_server()
    c.app_ = app
    yield c
    await c.close()


# ------------------------------------------------------------------ спецификация
def test_openapi_file_matches_generator():
    """bot/app/openapi.json перегенерирован после правок tools/api/openapi_gen.py."""
    assert webapp.OPENAPI_PATH.read_text(encoding="utf-8") == _gen().render(), \
        "перегенерируйте: python tools/api/openapi_gen.py"


async def test_every_route_is_documented(client):
    """Каждый маршрут API описан в спецификации — клиенту не нужно читать код сервера."""
    spec = json.loads(webapp.OPENAPI_PATH.read_text(encoding="utf-8"))
    documented = {(path, m.upper()) for path, ops in spec["paths"].items() for m in ops}
    routes = set()
    for r in client.app_.router.routes():
        info = r.resource.get_info() if r.resource else {}
        path = info.get("formatter") or info.get("path") or ""
        if not path.startswith(("/api/", "/updates/")) or r.method in ("HEAD", "OPTIONS"):
            continue
        routes.add((re.sub(r"\{(\w+):[^}]+\}", r"{\1}", path), r.method))
    missing = routes - documented
    assert not missing, f"не описаны в tools/api/openapi_gen.py: {sorted(missing)}"
    assert not {x for x in documented - routes if x[0].startswith("/api/")}, "описаны несуществующие маршруты"
    assert ("/stream/{login}/{token}.mp3", "GET") in documented  # поток «Моей Волны» живёт в radio.py


def test_openapi_refs_resolve():
    spec = json.loads(webapp.OPENAPI_PATH.read_text(encoding="utf-8"))
    names = set(spec["components"]["schemas"])
    refs = set(re.findall(r'"#/components/schemas/(\w+)"', json.dumps(spec)))
    assert refs <= names
    ids = [o["operationId"] for ops in spec["paths"].values() for o in ops.values()]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------ /api/info и /api/openapi.json
async def test_info_without_auth(client):
    r = await client.get("/api/info")
    assert r.status == 200
    data = await r.json()
    assert data["api_version"] == webapp.API_VERSION
    assert data["auth"]["bot"] == "pimusic_bot" and "bearer" in data["auth"]["methods"]
    assert data["limits"]["tracks_per_request"] == webapp.MAX_TRACK_IDS
    assert "radio" in data["features"]


async def test_openapi_served(client):
    r = await client.get("/api/openapi.json")
    assert r.status == 200 and r.headers["Content-Type"].startswith("application/json")
    assert (await r.json())["openapi"].startswith("3.1")


# ------------------------------------------------------------------ интернет-радио
async def test_radio_add_list_delete(client, db):
    r = await client.post("/api/radio", json={"name": "Джаз FM", "url": "https://stream.example.com/jazz"},
                          headers=hdr(ALICE))
    assert r.status == 200, await r.text()
    st = await r.json()
    assert st["name"] == "Джаз FM" and st["url"] == "https://stream.example.com/jazz"
    assert st["jellyfin"] is False  # MagicMock-Jellyfin не принял список — станция всё равно сохранена
    assert Path(db.get_radio(st["id"])["path"]).read_text(encoding="utf-8").strip() == st["url"]

    items = (await (await client.get("/api/radio", headers=hdr(ALICE))).json())["items"]
    assert [x["id"] for x in items] == [st["id"]]

    r = await client.post(f"/api/radio/{st['id']}/delete", headers=hdr(ALICE))
    assert r.status == 403  # удаляет только администратор
    r = await client.post(f"/api/radio/{st['id']}/delete", headers=hdr(ADMIN))
    assert r.status == 200 and db.get_radio(st["id"]) is None
    r = await client.post(f"/api/radio/{st['id']}/delete", headers=hdr(ADMIN))
    assert r.status == 404


@pytest.mark.parametrize("body", [{"name": "x", "url": "http://192.168.0.1/s"}, {"name": "", "url": "https://a.b/s"},
                                  {"name": "x", "url": "ftp://a.b/s"}, {"name": "x"}, {"name": "a\nb", "url": "https://a.b"}])
async def test_radio_add_rejects_bad_station(client, body):
    r = await client.post("/api/radio", json=body, headers=hdr(ALICE))
    assert r.status == 400 and (await r.json())["error"] == "bad_station"


async def test_radio_requires_auth(client):
    assert (await client.get("/api/radio")).status == 401


# ------------------------------------------------------------------ администратор
async def test_admin_endpoints(client, monkeypatch):
    async def online():
        return True
    monkeypatch.setattr(downloaders.SLSKD, "online", online)
    for path in ("/api/admin/status", "/api/admin/users"):
        assert (await client.get(path, headers=hdr(ALICE))).status == 403
    st = await (await client.get("/api/admin/status", headers=hdr(ADMIN))).json()
    assert st["users"] == 2 and st["soulseek_online"] is True and st["queue"] == 0
    assert st["disk"]["total"] >= st["disk"]["free"] > 0
    users = (await (await client.get("/api/admin/users", headers=hdr(ADMIN))).json())["items"]
    assert {u["login"]: u["is_admin"] for u in users} == {"alice": False, "root": True}
