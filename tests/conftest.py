"""Test bootstrap: env vars must be set BEFORE any `app.*` import (CFG is built at import time)."""
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="pimusic-tests-"))
os.environ["MEDIA_ROOT"] = str(_SESSION_DIR / "media")
os.environ["STATE_DIR"] = str(_SESSION_DIR / "state")
os.environ["INVITE_CODE"] = "secret-invite"
os.environ["JELLYFIN_ADMIN_USER"] = "rootadmin"
os.environ["ADMIN_TELEGRAM_IDS"] = "999"
for _k in ("TELEGRAM_BOT_TOKEN", "SLSKD_API_KEY", "JELLYFIN_API_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import pytest  # noqa: E402

import app.config  # noqa: E402,F401
from app.config import CFG as ORIGINAL_CFG  # noqa: E402


def patch_cfg(monkeypatch, **overrides):
    """Replace the (frozen) CFG object in every loaded `app.*` module."""
    new = dataclasses.replace(ORIGINAL_CFG, **overrides)
    for name, mod in list(sys.modules.items()):
        if name.startswith("app") and mod is not None and isinstance(getattr(mod, "CFG", None), type(ORIGINAL_CFG)):
            monkeypatch.setattr(mod, "CFG", new, raising=False)
    return new


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Per-test CFG with its own media/state dirs (all app modules are imported first so they get patched)."""
    import app.db  # noqa: F401
    import app.downloaders  # noqa: F401
    import app.http  # noqa: F401
    import app.library  # noqa: F401
    import app.metadata  # noqa: F401
    import app.radio  # noqa: F401
    import app.service  # noqa: F401
    import app.sources  # noqa: F401
    import app.tagger  # noqa: F401
    media, state = tmp_path / "media", tmp_path / "state"
    media.mkdir()
    state.mkdir()
    return patch_cfg(monkeypatch, media_root=media, state_dir=state)


@pytest.fixture
def db(cfg):
    from app.db import DB
    d = DB(cfg.db_path)
    yield d
    d._conn.close()


@pytest.fixture(autouse=True)
def _clear_mb_cache():
    """lookup_musicbrainz кэширует ответы — между тестами с разными фикстурами кэш надо сбрасывать."""
    from app import metadata
    metadata._MB_CACHE.clear()
    metadata._ARTIST_IDS.clear()
    yield
    metadata._MB_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Тесты не ходят в DNS: любое имя «резолвится» в публичный адрес (тесты SSRF подменяют это сами)."""
    from app import util

    async def fake_resolve(host):
        return {"93.184.216.34"}
    monkeypatch.setattr(util, "resolve_host", fake_resolve)


@pytest.fixture(autouse=True)
def _no_deezer_duration(monkeypatch):
    """Длительность из Deezer в _process — без сети: по умолчанию «не нашлось» (тесты подменяют сами)."""
    from app import metadata

    async def none(artist, title, isrc=""):
        return 0.0
    monkeypatch.setattr(metadata, "deezer_duration", none)
