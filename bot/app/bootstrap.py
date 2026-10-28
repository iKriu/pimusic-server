"""Zero-touch: первичный мастер Jellyfin через REST API, API-ключ для бота, библиотеки, радио."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from . import library, radio
from .config import CFG
from .db import DB, User
from .jellyfin import Jellyfin, JellyfinError
from .service import library_name

log = logging.getLogger("bootstrap")

APP_NAME = "PiMusicBot"


async def _wizard(jf: Jellyfin) -> None:
    lang = os.environ.get("JELLYFIN_UI_CULTURE", "ru")
    await jf.request("POST", "/Startup/Configuration", token="", json={
        "ServerName": CFG.server_name, "UICulture": lang,
        "MetadataCountryCode": os.environ.get("JELLYFIN_COUNTRY", "RU"),
        "PreferredMetadataLanguage": os.environ.get("JELLYFIN_METADATA_LANG", "ru"),
    })
    # В 10.11 первый пользователь создаётся лениво именно в GET /Startup/User — без него POST вернёт 404
    await jf.request("GET", "/Startup/User", token="")
    await jf.request("POST", "/Startup/User", token="",
                     json={"Name": CFG.jellyfin_admin_user, "Password": CFG.jellyfin_admin_password})
    await jf.request("POST", "/Startup/RemoteAccess", token="",
                     json={"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    await jf.request("POST", "/Startup/Complete", token="")
    log.info("мастер первичной настройки пройден")


async def _server_tuning(jf: Jellyfin) -> None:
    """Щадящие настройки Jellyfin: подходят и для слабых машин (одноплатники, SD-карта/USB-диск)."""
    cfg = await jf.request("GET", "/System/Configuration")
    cfg.update({
        "ServerName": CFG.server_name,
        "EnableFolderView": False,
        "QuickConnectAvailable": True,
        "LibraryScanFanoutConcurrency": 1,
        "LibraryMetadataRefreshConcurrency": 1,
        "ParallelImageEncodingLimit": 1,
        "ActivityLogRetentionDays": 30,
        "EnableSlowResponseWarning": False,
        "LibraryMonitorDelay": 30,
    })
    await jf.request("POST", "/System/Configuration", json=cfg)
    with_enc = await jf.request("GET", "/System/Configuration/encoding")
    if isinstance(with_enc, dict):
        with_enc.update({"EncodingThreadCount": 2, "EnableThrottling": True, "EnableSegmentDeletion": True})
        await jf.request("POST", "/System/Configuration/encoding", json=with_enc)


async def bootstrap() -> int:
    jf = Jellyfin(CFG.jellyfin_url)
    try:
        log.info("жду Jellyfin %s …", CFG.jellyfin_url)
        info = await jf.wait_ready()
        log.info("Jellyfin %s готов, мастер пройден: %s", info.get("Version"), info.get("StartupWizardCompleted"))
        if not info.get("StartupWizardCompleted"):
            await _wizard(jf)
            await asyncio.sleep(3)
            await jf.wait_ready()

        auth = None
        for _ in range(10):
            try:
                auth = await jf.authenticate(CFG.jellyfin_admin_user, CFG.jellyfin_admin_password)
                break
            except JellyfinError as e:
                if e.status in (401, 403):
                    log.error("Не удалось войти в Jellyfin как %s — пароль в .env не совпадает с сервером",
                              CFG.jellyfin_admin_user)
                    return 2
                await asyncio.sleep(3)
        if not auth:
            return 2
        jf.token = auth["AccessToken"]
        admin_id = auth["User"]["Id"]

        # API-ключ для бота (идемпотентно)
        key = CFG.jellyfin_api_key()
        keys = await jf.api_keys()
        if not key or not any(k.get("AccessToken") == key for k in keys):
            existing = [k for k in keys if k.get("AppName") == APP_NAME]
            key = existing[0]["AccessToken"] if existing else await jf.create_api_key(APP_NAME)
            CFG.state_dir.mkdir(parents=True, exist_ok=True)
            CFG.jellyfin_api_key_file.write_text(key)
            os.chmod(CFG.jellyfin_api_key_file, 0o600)
        log.info("API-ключ бота готов")
        jf.token = key

        await _server_tuning(jf)

        # Папки, библиотека администратора, радио
        library.ensure_dirs()
        db = DB(CFG.db_path)
        admin_root = library.user_root(CFG.jellyfin_admin_user)
        admin_root.mkdir(parents=True, exist_ok=True)
        lib = await jf.create_library(library_name(CFG.jellyfin_admin_user), str(admin_root))
        admin_local = db.user_by_login(CFG.jellyfin_admin_user)
        if not admin_local:
            import secrets
            admin_local = User(login=CFG.jellyfin_admin_user, tg_id=None, jf_user_id=admin_id,
                               library_id=lib["ItemId"], stream_token=secrets.token_urlsafe(18), is_admin=True)
            db.upsert_user(admin_local)
        if os.environ.get("ADMIN_SEES_ALL_LIBRARIES", "false").lower() not in ("1", "true", "yes"):
            # даже администратор в плеере видит только свою медиатеку (управление сервером остаётся)
            await jf.set_policy(admin_id, EnableAllFolders=False, EnabledFolders=[lib["ItemId"]],
                                EnableLiveTvAccess=True, EnableLiveTvManagement=True,
                                EnableContentDeletionFromFolders=[lib["ItemId"]])
        radio.ensure_user_wave(admin_local)
        radio.ensure_default_stations(db)
        try:
            await radio.sync_tuner(db, jf)
        except JellyfinError as e:
            log.warning("M3U-тюнер радио не настроен: %s", e)

        summary = {"jellyfin_version": info.get("Version"), "admin_user": CFG.jellyfin_admin_user,
                   "api_key_file": str(CFG.jellyfin_api_key_file)}
        (CFG.state_dir / "bootstrap.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        log.info("bootstrap завершён")
        return 0
    finally:
        await jf.close()
