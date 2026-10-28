"""Живой поток «Моей Волны» (/stream/{login}/{token}.mp3): CORS для интерфейса Windows 2.0 (http://tauri.localhost
ставит <audio crossorigin> — без Access-Control-Allow-Origin WebView2 рвал поток сразу), диагностика отключений."""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app import radio, webapp

import test_webapp
from test_webapp import TOKEN, FakeSvc

alice = test_webapp.alice  # фикстура

ORIGIN = "http://tauri.localhost"


@pytest.fixture
async def stream_client(db, alice, monkeypatch):
    """RadioServer + Mini App на одном aiohttp-приложении, как в боте; станция без ffmpeg."""
    feed: list = [b"ID3-mp3-chunk", None]

    def subscribe(self):
        q = asyncio.Queue()
        for x in feed:
            q.put_nowait(x)
        return q
    monkeypatch.setattr(radio.Station, "subscribe", subscribe)
    monkeypatch.setattr(radio.Station, "unsubscribe", lambda self, q: None)
    rs = radio.RadioServer(db, MagicMock())
    webapp.setup_webapp(rs.app, db=db, svc=FakeSvc(), jf=MagicMock(), bot_token=TOKEN, radio=rs)
    c = TestClient(TestServer(rs.app))
    await c.start_server()
    c.feed = feed
    yield c
    await c.close()


async def test_wave_stream_has_cors_for_native_shell(stream_client, alice):
    r = await stream_client.get(f"/stream/alice/{alice.stream_token}.mp3", headers={"Origin": ORIGIN})
    assert r.status == 200
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Origin" in r.headers.get("Vary", "")
    assert await r.read() == b"ID3-mp3-chunk"
    r = await stream_client.head(f"/stream/alice/{alice.stream_token}.mp3", headers={"Origin": ORIGIN})
    assert r.headers["Access-Control-Allow-Origin"] == ORIGIN


async def test_wave_stream_no_cors_for_foreign_origin(stream_client, alice):
    r = await stream_client.get(f"/stream/alice/{alice.stream_token}.mp3", headers={"Origin": "https://evil.example"})
    assert r.status == 200 and "Access-Control-Allow-Origin" not in r.headers
    r = await stream_client.get(f"/stream/alice/{alice.stream_token}.mp3")
    assert "Access-Control-Allow-Origin" not in r.headers


async def test_wave_stream_bad_token_404_with_cors(stream_client):
    r = await stream_client.get("/stream/alice/wrong.mp3", headers={"Origin": ORIGIN})
    assert r.status == 404 and r.headers["Access-Control-Allow-Origin"] == ORIGIN


async def test_wave_stream_logs_why_listener_left(stream_client, alice, monkeypatch, caplog):
    stream_client.feed[:] = []  # станция молчит
    monkeypatch.setattr(radio, "NO_DATA_TIMEOUT", 0.05)
    with caplog.at_level(logging.INFO, logger="app.radio"):
        r = await stream_client.get(f"/stream/alice/{alice.stream_token}.mp3", headers={"Origin": ORIGIN})
        assert await r.read() == b""
    msgs = [x.getMessage() for x in caplog.records]
    assert any("слушатель подключился (origin http://tauri.localhost)" in m for m in msgs)
    assert any("слушатель отключился" in m and "нет данных" in m for m in msgs)


def test_slow_listener_dropped_with_warning(db, alice, caplog):
    st = radio.Station(MagicMock(), alice, db)
    q = asyncio.Queue(maxsize=2)
    st.listeners.add(q)
    with caplog.at_level(logging.WARNING, logger="app.radio"):
        for _ in range(3):
            st._fanout(b"x")
    assert q not in st.listeners
    assert q.get_nowait() is None  # слушателю — конец потока, он переподключится
    assert any("не успевает" in r.getMessage() for r in caplog.records)
