"""Гонка пиров в Soulseek: качаем у двух сразу, берём первого, второго отменяем."""
import asyncio
from pathlib import Path

from dataclasses import replace

import pytest

import app.config
from app import downloaders
from app.sources import TrackRequest


@pytest.fixture(autouse=True)
def _slskd_key(monkeypatch):
    monkeypatch.setattr(downloaders, "CFG", replace(app.config.CFG, slskd_api_key="k" * 32))


def _resp(user, name="01 Uprising.mp3"):
    return {"username": user, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 2_000_000,
            "files": [{"filename": f"@@x\\Muse\\The Resistance\\{name}", "size": 9_000_000, "extension": "mp3",
                       "bitRate": 320, "isVariableBitRate": False, "length": 304}]}


def test_race_takes_fastest_peer_and_cancels_other(monkeypatch):
    started, cancelled = [], []

    async def fake_search(text, timeout_s):
        return [_resp("slow"), _resp("fast"), _resp("third")]

    async def fake_attempt(user, f, progress):
        started.append(user)
        try:
            await asyncio.sleep(0.05 if user == "fast" else 5)
        except asyncio.CancelledError:
            cancelled.append(user)
            raise
        return downloaders.Downloaded(Path(f"/tmp/{user}.mp3"), "soulseek", user)

    monkeypatch.setattr(downloaders.SLSKD, "search", fake_search)
    monkeypatch.setattr(downloaders, "_attempt_slskd", fake_attempt)

    async def noop(_):
        return None

    req = TrackRequest(artist="Muse", title="Uprising", duration=304)
    res = asyncio.run(downloaders.soulseek_slskd(req, noop))
    assert res and res.detail == "fast"
    assert set(started) == {"slow", "fast"}, "одновременно стартуют ровно два пира"
    assert cancelled == ["slow"], "проигравший пир отменён"


def test_race_moves_to_next_pair_when_both_fail(monkeypatch):
    order = []

    async def fake_search(text, timeout_s):
        return [_resp(u) for u in ("a", "b", "c", "d")]

    async def fake_attempt(user, f, progress):
        order.append(user)
        await asyncio.sleep(0.01)
        return downloaders.Downloaded(Path("/tmp/c.mp3"), "soulseek", user) if user == "c" else None

    monkeypatch.setattr(downloaders.SLSKD, "search", fake_search)
    monkeypatch.setattr(downloaders, "_attempt_slskd", fake_attempt)

    async def noop(_):
        return None

    res = asyncio.run(downloaders.soulseek_slskd(TrackRequest(artist="Muse", title="Uprising", duration=304), noop))
    assert res.detail == "c" and order[:2] == ["a", "b"] and "c" in order


def test_queued_peer_given_up_after_queue_wait(monkeypatch):
    cancels = []

    async def stuck(user, filename):
        return {"id": "t1", "state": "Queued, Remotely", "bytesTransferred": 0, "percentComplete": 0}

    async def cancel(user, tid):
        cancels.append(tid)

    monkeypatch.setattr(downloaders.SLSKD, "transfer_state", stuck)
    monkeypatch.setattr(downloaders.SLSKD, "cancel", cancel)
    monkeypatch.setattr(downloaders, "SOULSEEK_QUEUE_WAIT", 0.1)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(downloaders.asyncio, "sleep", lambda s: real_sleep(0.05))

    async def noop(_):
        return None

    res = asyncio.run(downloaders._wait_slskd("peer", {"filename": "x.mp3"}, "bot/1", noop))
    assert res is None and cancels == ["t1"]


def test_cancelled_wait_cancels_transfer(monkeypatch):
    cancels = []

    async def in_progress(user, filename):
        return {"id": "t2", "state": "InProgress", "bytesTransferred": 100, "percentComplete": 1}

    async def cancel(user, tid):
        cancels.append(tid)

    monkeypatch.setattr(downloaders.SLSKD, "transfer_state", in_progress)
    monkeypatch.setattr(downloaders.SLSKD, "cancel", cancel)

    async def noop(_):
        return None

    async def run():
        t = asyncio.create_task(downloaders._wait_slskd("peer", {"filename": "x.mp3"}, "bot/1", noop))
        await asyncio.sleep(0.1)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert cancels == ["t2"]


def test_attempt_exception_does_not_kill_race(monkeypatch):
    """Исключение у одного пира (aiohttp, таймаут…) — это просто None, гонка идёт дальше."""
    async def fake_search(text, timeout_s):
        return [_resp("bad"), _resp("good")]

    async def enqueue(user, filename, size, dest):
        if user == "bad":
            raise asyncio.TimeoutError()
        return True

    async def wait(user, f, dest, progress):
        await asyncio.sleep(0.02)
        return Path("/tmp/good.mp3")

    monkeypatch.setattr(downloaders.SLSKD, "search", fake_search)
    monkeypatch.setattr(downloaders.SLSKD, "enqueue", enqueue)
    monkeypatch.setattr(downloaders, "_wait_slskd", wait)

    async def noop(_):
        return None

    res = asyncio.run(downloaders.soulseek_slskd(TrackRequest(artist="Muse", title="Uprising", duration=304), noop))
    assert res and res.path == Path("/tmp/good.mp3")


def test_finished_loser_file_is_cleaned(monkeypatch):
    """Оба пира докачали одновременно — файл проигравшего удаляется, победителя остаётся."""
    cleaned = []

    async def fake_search(text, timeout_s):
        return [_resp("a"), _resp("b")]

    async def fake_attempt(user, f, progress):
        await asyncio.sleep(0.01)
        return downloaders.Downloaded(Path(f"/tmp/{user}.mp3"), "soulseek", user)

    monkeypatch.setattr(downloaders.SLSKD, "search", fake_search)
    monkeypatch.setattr(downloaders, "_attempt_slskd", fake_attempt)
    monkeypatch.setattr(downloaders, "cleanup_after", lambda p: cleaned.append(p))

    async def noop(_):
        return None

    res = asyncio.run(downloaders.soulseek_slskd(TrackRequest(artist="Muse", title="Uprising", duration=304), noop))
    assert res is not None
    assert cleaned == [Path(f"/tmp/{'b' if res.detail == 'a' else 'a'}.mp3")]


def test_cancel_during_enqueue_cancels_transfer(monkeypatch):
    cancels = []

    async def slow_enqueue(user, filename, size, dest):
        await asyncio.sleep(5)
        return True

    async def queued(user, filename):
        return {"id": "t3", "state": "Queued, Remotely"}

    async def cancel(user, tid):
        cancels.append(tid)

    monkeypatch.setattr(downloaders.SLSKD, "enqueue", slow_enqueue)
    monkeypatch.setattr(downloaders.SLSKD, "transfer_state", queued)
    monkeypatch.setattr(downloaders.SLSKD, "cancel", cancel)

    async def noop(_):
        return None

    async def run():
        t = asyncio.create_task(downloaders._attempt_slskd("peer", {"filename": "x.mp3", "size": 1}, noop))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(run())
    assert cancels == ["t3"]
