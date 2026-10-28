"""Минимальный асинхронный клиент REST API Jellyfin 10.11."""
from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import Any
from urllib.parse import quote

import aiohttp


log = logging.getLogger(__name__)

CLIENT = "PiMusicBot"
VERSION = "1.0.0"
DEVICE_ID = "pimusic-bot-" + uuid.uuid5(uuid.NAMESPACE_DNS, "pimusic").hex[:12]


class JellyfinError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"Jellyfin HTTP {status}: {text[:300]}")
        self.status = status


def auth_header(token: str | None = None) -> str:
    h = f'MediaBrowser Client="{CLIENT}", Device="PiMusic", DeviceId="{DEVICE_ID}", Version="{VERSION}"'
    if token:
        h += f', Token="{token}"'
    return h


class Jellyfin:
    def __init__(self, base_url: str, token: str | None = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                      json: Any = None, token: str | None = None, ok404: bool = False) -> Any:
        s = await self._s()
        headers = {"Authorization": auth_header(token if token is not None else self.token),
                   "Accept": "application/json"}
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        for k, v in list(clean.items()):
            if isinstance(v, bool):
                clean[k] = "true" if v else "false"
            elif isinstance(v, (list, tuple)):
                clean[k] = ",".join(map(str, v))
        async with s.request(method, self.base + path, params=clean, json=json, headers=headers) as r:
            text = await r.text()
            if r.status == 404 and ok404:
                return None
            if r.status >= 400:
                raise JellyfinError(r.status, text)
            if not text:
                return None
            try:
                return await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                return text

    # ------------------------------------------------------------ system
    async def public_info(self) -> dict | None:
        try:
            return await self.request("GET", "/System/Info/Public", token="")
        except (aiohttp.ClientError, asyncio.TimeoutError, JellyfinError):
            return None

    async def health(self) -> str:
        try:
            s = await self._s()
            async with s.get(self.base + "/health") as r:
                return (await r.text()).strip()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ""

    async def wait_ready(self, timeout: float = 900) -> dict:
        """В 10.11 при старте/миграциях отвечает временный setup-сервер (503, /health = Degraded).
        Пока /health не вернёт Healthy, /System/Info/Public может врать про StartupWizardCompleted."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self.health() == "Healthy":
                info = await self.public_info()
                if isinstance(info, dict) and info.get("Version"):
                    return info
            await asyncio.sleep(3)
        raise TimeoutError("Jellyfin не поднялся вовремя")

    # ------------------------------------------------------------ auth
    async def authenticate(self, username: str, password: str) -> dict:
        return await self.request("POST", "/Users/AuthenticateByName", json={"Username": username, "Pw": password},
                                  token="")

    async def check_password(self, username: str, password: str) -> dict | None:
        try:
            return await self.authenticate(username, password)
        except JellyfinError:
            return None

    # ------------------------------------------------------------ users
    async def users(self) -> list[dict]:
        return await self.request("GET", "/Users") or []

    async def user_by_name(self, name: str) -> dict | None:
        for u in await self.users():
            if u.get("Name", "").lower() == name.lower():
                return u
        return None

    async def create_user(self, name: str, password: str) -> dict:
        return await self.request("POST", "/Users/New", json={"Name": name, "Password": password})

    async def get_user(self, user_id: str) -> dict:
        return await self.request("GET", f"/Users/{user_id}")

    async def set_policy(self, user_id: str, **changes: Any) -> None:
        user = await self.get_user(user_id)
        policy = dict(user.get("Policy") or {})
        policy.update(changes)
        await self.request("POST", f"/Users/{user_id}/Policy", json=policy)

    async def delete_user(self, user_id: str) -> None:
        await self.request("DELETE", f"/Users/{user_id}")

    async def set_password(self, user_id: str, new_pw: str) -> None:
        await self.request("POST", f"/Users/{user_id}/Password", json={"NewPw": new_pw, "ResetPassword": False})

    # ------------------------------------------------------------ libraries
    async def virtual_folders(self) -> list[dict]:
        return await self.request("GET", "/Library/VirtualFolders") or []

    async def library_by_name(self, name: str) -> dict | None:
        for f in await self.virtual_folders():
            if f.get("Name") == name:
                return f
        return None

    async def create_library(self, name: str, path: str, collection_type: str = "music",
                             options: dict | None = None) -> dict:
        existing = await self.library_by_name(name)
        if existing:
            return existing
        await self.request(
            "POST", "/Library/VirtualFolders",
            params={"name": name, "collectionType": collection_type, "paths": path, "refreshLibrary": True},
            json={"LibraryOptions": options or music_library_options(path)},
        )
        for _ in range(20):
            lib = await self.library_by_name(name)
            if lib and lib.get("ItemId"):
                return lib
            await asyncio.sleep(1.5)
        raise JellyfinError(500, f"библиотека {name} не появилась")

    async def delete_library(self, name: str) -> None:
        await self.request("DELETE", "/Library/VirtualFolders", params={"name": name, "refreshLibrary": True})

    async def refresh_item(self, item_id: str) -> None:
        await self.request("POST", f"/Items/{item_id}/Refresh",
                           params={"metadataRefreshMode": "Default", "imageRefreshMode": "Default",
                                   "replaceAllMetadata": False, "replaceAllImages": False})

    async def refresh_all(self) -> None:
        await self.request("POST", "/Library/Refresh")

    # ------------------------------------------------------------ items
    async def items(self, user_id: str, **params: Any) -> list[dict]:
        out: list[dict] = []
        start, page = 0, 500
        while True:
            data = await self.request("GET", "/Items", params={"userId": user_id, "StartIndex": start,
                                                               "Limit": page, **params})
            items = (data or {}).get("Items") or []
            out.extend(items)
            total = (data or {}).get("TotalRecordCount", len(out))
            start += len(items)
            if not items or start >= total:
                return out

    async def audio_items(self, user_id: str, *, favorites_only: bool = False) -> list[dict]:
        params: dict[str, Any] = {
            "IncludeItemTypes": "Audio", "Recursive": True,
            "Fields": "Path,Genres,UserData,ParentId",  # без MediaSources: на 1000 треков это секунды
            "EnableImages": False, "EnableUserData": True,
        }
        if favorites_only:
            params["Filters"] = "IsFavorite"
        return await self.items(user_id, **params)

    async def upload_image(self, item_id: str, data: bytes, image_type: str = "Primary",
                           content_type: str = "image/jpeg") -> None:
        """POST /Items/{id}/Images/{type}: в 10.11 ImageController читает тело как base64-строку,
        тип картинки берёт из Content-Type (нужны права администратора — API-ключ подходит)."""
        s = await self._s()
        headers = {"Authorization": auth_header(self.token), "Content-Type": content_type}
        async with s.post(f"{self.base}/Items/{item_id}/Images/{image_type}", data=base64.b64encode(data),
                          headers=headers) as r:
            if r.status >= 400:
                raise JellyfinError(r.status, await r.text())

    # ------------------------------------------------------------ playlists (личная «Моя Волна»)
    async def find_playlist(self, user_id: str, name: str) -> str | None:
        data = await self.request("GET", "/Items", params={"userId": user_id, "IncludeItemTypes": "Playlist",
                                                           "Recursive": True, "searchTerm": name})
        for it in (data or {}).get("Items") or []:
            if it.get("Name") == name:
                return it["Id"]
        return None

    async def user_playlists(self, user_id: str) -> list[dict]:
        data = await self.request("GET", "/Items", params={"userId": user_id, "IncludeItemTypes": "Playlist",
                                                           "Recursive": True})
        return (data or {}).get("Items") or []

    async def delete_orphan_playlists(self, keep_ids: set[str], prefix: str = "Моя Волна") -> int:
        """Удаляет «Моя Волна…», которых бот не создавал или которые остались от удалённых пользователей
        (Jellyfin 10.11 показывает такие всем). Владельца по API-ключу не узнать (/Playlists/{id} отвечает 400
        на любой плейлист), поэтому ориентир — id, которые бот сохранил у себя в базе.
        Плейлисты, созданные пользователями вручную, не трогаются."""
        keep = {k.replace("-", "") for k in keep_ids if k}
        removed = 0
        seen: set[str] = set()
        for u in await self.users():
            for pl in await self.user_playlists(u["Id"]):
                pid = pl["Id"].replace("-", "")
                if pid in seen or pid in keep or not (pl.get("Name") or "").startswith(prefix):
                    continue
                seen.add(pid)
                try:
                    await self.request("DELETE", f"/Items/{pl['Id']}")
                    removed += 1
                except JellyfinError as e:
                    log.warning("не удалось удалить лишний плейлист %s: %s", pl.get("Name"), e)
        return removed

    async def create_playlist(self, user_id: str, name: str, ids: list[str]) -> str:
        data = await self.request("POST", "/Playlists", json={"Name": name, "Ids": ids, "UserId": user_id,
                                                              "MediaType": "Audio", "IsPublic": False})
        return data["Id"]

    async def create_playlist_full(self, user_id: str, name: str, ids: list[str], first: int = 500,
                                   batch: int = 100) -> str:
        """Плейлист с любым числом треков в заданном порядке: первые `first` — при создании, остальные —
        POST /Playlists/{id}/Items пачками по `batch`. Если дописывать нельзя (в 10.11 API-ключом правит только
        владелец — 401/403), плейлист пересоздаётся одним запросом со всем списком в теле."""
        pid = await self.create_playlist(user_id, name, ids[:first])
        try:
            for i in range(first, len(ids), batch):
                await self.request("POST", f"/Playlists/{pid}/Items",
                                   params={"ids": ids[i:i + batch], "userId": user_id})
        except JellyfinError as e:
            if e.status not in (401, 403):
                raise
            log.info("дописать плейлист %s нельзя (%s) — создаю одним запросом", name, e.status)
            try:
                await self.request("DELETE", f"/Items/{pid}")
            except JellyfinError:
                pass
            pid = await self.create_playlist(user_id, name, ids)
        return pid

    async def playlist_entries(self, playlist_id: str, user_id: str) -> list[dict]:
        data = await self.request("GET", f"/Playlists/{playlist_id}/Items", params={"userId": user_id})
        return (data or {}).get("Items") or []

    async def replace_playlist(self, user_id: str, name: str, ids: list[str], old_id: str | None = None,
                               *, find_existing: bool = True) -> str:
        # В 10.11 менять состав чужого плейлиста API-ключом нельзя (403 — правит только владелец),
        # а создавать от имени пользователя (UserId) и удалять — можно. Поэтому пересоздаём.
        # find_existing=False — не искать по имени (зеркала плейлистов бота: одноимённый ручной не трогаем)
        old = old_id or (await self.find_playlist(user_id, name) if find_existing else None)
        pid = await self.create_playlist(user_id, name, ids[:500])
        if old:
            try:
                await self.request("DELETE", f"/Items/{old}")
            except JellyfinError as e:
                log.warning("не удалось удалить старый плейлист %s: %s", old, e)
        return pid

    # ------------------------------------------------------------ Live TV (интернет-радио через M3U-тюнер)
    async def tuner_hosts(self) -> list[dict]:
        cfg = await self.request("GET", "/System/Configuration/livetv")
        return (cfg or {}).get("TunerHosts") or []

    async def upsert_m3u_tuner(self, url: str, name: str) -> dict:
        body = {"Type": "m3u", "Url": url, "FriendlyName": name, "ImportFavoritesOnly": False,
                "AllowHWTranscoding": False, "AllowFmp4TranscodingContainer": False, "AllowStreamSharing": True,
                "EnableStreamLooping": False, "TunerCount": 0, "IgnoreDts": True, "ReadAtNativeFramerate": False,
                "Source": "", "UserAgent": ""}
        for t in await self.tuner_hosts():
            if t.get("Url") == url:
                body["Id"] = t.get("Id")
                break
        return await self.request("POST", "/LiveTv/TunerHosts", json=body)

    async def refresh_guide(self) -> None:
        for task in await self.request("GET", "/ScheduledTasks") or []:
            if task.get("Key") == "RefreshGuide":
                await self.request("POST", f"/ScheduledTasks/Running/{task['Id']}")
                return

    # ------------------------------------------------------------ API keys
    async def api_keys(self) -> list[dict]:
        data = await self.request("GET", "/Auth/Keys")
        return (data or {}).get("Items") or []

    async def create_api_key(self, app: str) -> str:
        await self.request("POST", "/Auth/Keys", params={"app": app})
        for k in await self.api_keys():
            if k.get("AppName") == app:
                return k["AccessToken"]
        raise JellyfinError(500, "API-ключ не создан")


def music_library_options(path: str) -> dict:
    return {
        "Enabled": True,
        "EnableRealtimeMonitor": True,
        "EnablePhotos": False,
        "SaveLocalMetadata": False,
        "EnableAutomaticSeriesGrouping": False,
        "EnableEmbeddedTitles": True,
        "EnableEmbeddedExtrasTitles": False,
        "EnableEmbeddedEpisodeInfos": False,
        "AutomaticRefreshIntervalDays": 0,
        "PreferredMetadataLanguage": "",
        "MetadataCountryCode": "",
        "SaveLyricsWithMedia": False,
        "PreferNonstandardArtistsTag": False,
        "UseCustomTagDelimiters": False,
        "EnableLUFSScan": True,
        "PathInfos": [{"Path": path}],
        # Основные метаданные уже идеально лежат в тегах; онлайн-провайдеры нужны только для фото артистов
        "TypeOptions": [
            {"Type": "MusicArtist", "MetadataFetchers": ["MusicBrainz", "TheAudioDB"],
             "ImageFetchers": ["TheAudioDB"]},
            {"Type": "MusicAlbum", "MetadataFetchers": ["MusicBrainz"], "ImageFetchers": []},
            {"Type": "Audio", "MetadataFetchers": [], "ImageFetchers": ["Image Extractor"]},
        ],
    }


def jf_path_quote(p: str) -> str:
    return quote(p, safe="")
