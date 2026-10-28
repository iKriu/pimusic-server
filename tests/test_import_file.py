"""Импорт списков TXT/CSV из Mini App: POST /api/import/file; списки в /api/upload — подсказка про импорт."""
import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import MagicMock

from app import importer, uploads, webapp
from app.db import User
from app.service import Service
from app.uploads import Uploads

from test_webapp import TG_ID, TOKEN, hdr

CSV = ("Track name,Artist name,Album,Playlist name,Type,ISRC\n"
       "Группа крови,Кино,Группа крови,Мой,Playlist,RUA1\n"
       "Кукушка,Кино,Чёрный альбом,Мой,Playlist,RUA2\n"
       "Кукушка,Кино,Чёрный альбом,Мой,Playlist,RUA2\n")


@pytest.fixture
async def client(db, cfg, monkeypatch):
    db.upsert_user(User("alice", TG_ID, "jf-alice", "lib", "stok", False))
    monkeypatch.setattr(uploads, "free_space_ok", lambda extra=0: True)
    svc = Service(db, None)
    svc._ensure_import_feeder = lambda: None  # очередь импорта в тестах не запускаем
    svc.uploads = Uploads(MagicMock(messenger=None), db)
    app = web.Application()
    webapp.setup_webapp(app, db=db, svc=svc, jf=MagicMock(), bot_token=TOKEN)
    c = TestClient(TestServer(app))
    await c.start_server()
    c.db, c.svc = db, svc
    yield c
    await c.close()


def form(name, data, field="file"):
    fd = aiohttp.FormData()
    fd.add_field("note", "не файл", content_type="text/plain")
    fd.add_field(field, data, filename=name, content_type="application/octet-stream")
    return fd


async def test_import_csv_creates_import_and_playlist(client):
    r = await client.post("/api/import/file", data=form("Мой плейлист.csv", CSV.encode("utf-8-sig")), headers=hdr())
    assert r.status == 200
    data = await r.json()
    assert data["total"] == 2 and data["format"].startswith("CSV")
    assert "повторяющихся строк пропущено: 1" in data["format"]
    assert data["name"] == "Мой плейлист" and data["playlist"]["name"] == "Мой плейлист"
    imp = client.db.get_import(data["import_id"])
    assert imp["login"] == "alice" and imp["state"] == "running" and imp["chat_id"] == TG_ID
    assert imp["filename"] == "Мой плейлист.csv" and imp["playlist_id"] == data["playlist"]["id"]
    assert "Мой плейлист" in [p["name"] for p in client.db.user_playlists("alice")]
    # прогресс виден в GET /api/import
    r = await client.get("/api/import", headers=hdr())
    imps = (await r.json())["imports"]
    assert [i["id"] for i in imps] == [data["import_id"]] and imps[0]["total"] == 2 and imps[0]["text"]


async def test_import_txt_cp1251_and_tsv(client, monkeypatch):
    async def keep(items):
        return False
    monkeypatch.setattr(importer, "fix_txt_order", keep)  # без запросов в Deezer
    txt = "# мой список\nКино - Группа крови\n2. Ария — Беспечный ангел\n".encode("cp1251")
    r = await client.post("/api/import/file", data=form("list.txt", txt), headers=hdr())
    data = await r.json()
    assert r.status == 200 and data["total"] == 2 and data["format"].startswith("TXT") and data["name"] == "list"
    items = client.db._q("SELECT artist, title FROM import_items WHERE import_id=? ORDER BY idx", (data["import_id"],))
    assert [(i["artist"], i["title"]) for i in items] == [("Кино", "Группа крови"), ("Ария", "Беспечный ангел")]
    tsv = "Artist\tTitle\nКино\tЗвезда\n".encode()
    r = await client.post("/api/import/file", data=form("x.tsv", tsv), headers=hdr())
    assert r.status == 200 and (await r.json())["total"] == 1


@pytest.mark.parametrize("name,data,status,error", [
    ("a.csv", b"", 400, "empty"),
    ("a.txt", b"  \n\n", 400, "empty"),
    ("a.csv", "Foo,Bar,Baz\n1,2,3\n".encode(), 400, "bad_format"),
    ("a.csv", b"Track name,Artist name\n", 400, "no_tracks"),
    ("a.mp3", b"ID3", 415, "unsupported"),
    ("a.xlsx", b"PK", 415, "unsupported"),
])
async def test_import_file_errors(client, name, data, status, error):
    r = await client.post("/api/import/file", data=form(name, data), headers=hdr())
    body = await r.json()
    assert (r.status, body["error"]) == (status, error) and body["message"]
    assert client.db.running_imports("alice") == []


async def test_import_file_limits_and_auth(client, monkeypatch):
    monkeypatch.setattr(webapp, "IMPORT_MAX_BYTES", 100)
    r = await client.post("/api/import/file", data=form("big.csv", CSV.encode() * 10), headers=hdr())
    assert r.status == 413 and (await r.json())["error"] == "too_large"
    r = await client.post("/api/import/file", data=form("a.csv", CSV.encode(), field="other"), headers=hdr())
    assert r.status == 400 and (await r.json())["error"] == "no_file"
    r = await client.post("/api/import/file", json={"x": 1}, headers=hdr())
    assert r.status == 400 and (await r.json())["error"] == "bad_multipart"
    r = await client.post("/api/import/file", data=form("a.csv", CSV.encode()))
    assert r.status == 401


async def test_upload_list_file_hints_import(client):
    fd = aiohttp.FormData()
    fd.add_field("files", CSV.encode(), filename="list.csv", content_type="text/csv")
    r = await client.post("/api/upload", data=fd, headers=hdr())
    body = await r.json()
    assert r.status == 415 and body["error"] == "use_import" and "импорт" in body["message"]
    assert body["rejected"] == [{"name": "list.csv", "error": "use_import", "message": webapp.USE_IMPORT_MSG}]
    # вперемешку с аудио — аудио принимается, список отклоняется с подсказкой
    fd = aiohttp.FormData()
    fd.add_field("files", b"ID3" * 100, filename="Кино - Звезда.mp3", content_type="audio/mpeg")
    fd.add_field("files", b"a - b", filename="list.txt", content_type="text/plain")
    r = await client.post("/api/upload", data=fd, headers=hdr())
    body = await r.json()
    assert r.status == 200 and [u["name"] for u in body["uploads"]] == ["Кино - Звезда.mp3"]
    assert [(x["name"], x["error"]) for x in body["rejected"]] == [("list.txt", "use_import")]
