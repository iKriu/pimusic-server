"""Генератор bot/app/openapi.json — описания API PiMusic для сторонних клиентов (OpenAPI 3.1).

Запуск: .venv/Scripts/python.exe tools/api/openapi_gen.py
Тест tests/test_openapi.py проверяет, что файл совпадает с выводом генератора и что в нём описан каждый маршрут
сервера. Добавили эндпоинт — опишите его здесь и перегенерируйте файл. Человеческое руководство — docs/api/README.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "bot" / "app" / "openapi.json"
API_VERSION = "1"


# ------------------------------------------------------------------ помощники
def ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def arr(item: Any) -> dict:
    return {"type": "array", "items": item}


def obj(props: dict, required: list[str] | None = None, desc: str | None = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    if desc:
        out["description"] = desc
    out.update(extra)
    return out


def nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


S = {"type": "string"}
I = {"type": "integer"}  # noqa: E741
N = {"type": "number"}
B = {"type": "boolean"}
NS, NI, NN = nullable(S), nullable(I), nullable(N)
ANY: dict = {}
BIN = {"type": "string", "format": "binary"}


def s(desc: str, base: dict | None = None, **extra: Any) -> dict:
    return {**(base or S), "description": desc, **extra}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def q(name: str, desc: str, schema: dict | None = None, required: bool = False) -> dict:
    return {"name": name, "in": "query", "required": required, "description": desc, "schema": schema or S}


def p(name: str, desc: str, schema: dict | None = None) -> dict:
    return {"name": name, "in": "path", "required": True, "description": desc, "schema": schema or I}


SORT_TRACKS = ("added", "title", "artist", "album", "genre", "year", "duration", "random")
SORT_GROUPS = ("name", "count", "added", "year")
ORDER = q("order", "asc / desc. По умолчанию added и year — по убыванию, остальное — по возрастанию.",
          enum("asc", "desc"))
VIEW = [
    q("q", "Поиск по исполнителю, названию, альбому (без регистра, ё = е), до 200 символов."),
    q("genre", "Точное имя жанра (без учёта регистра); трек подходит, если это любой из его жанров."),
    ORDER,
    q("seed", "Для sort=random: одно значение — один порядок (нужно для подгрузки страниц).", I),
]

ERR_DOC = {
    "400": "Неверный запрос (bad_json и коды конкретного эндпоинта)",
    "401": "Нет входа: unauthorized / revoked / token_expired — показать экран входа",
    "403": "not_registered (Telegram-аккаунт не зарегистрирован в боте) или forbidden",
    "404": "Не найдено или нет доступа (существование чужого не раскрывается)",
}


def op(tag: str, summary: str, desc: str = "", *, op_id: str, auth: bool = True, params: list | None = None,
       body: dict | None = None, body_type: str = "application/json", body_required: bool = True,
       ok: dict | None = None, ok_type: str = "application/json", errors: dict[str, str] | None = None) -> dict:
    out: dict[str, Any] = {"tags": [tag], "summary": summary, "operationId": op_id}
    if desc:
        out["description"] = desc
    if not auth:
        out["security"] = []
    if params:
        out["parameters"] = params
    if body is not None:
        out["requestBody"] = {"required": body_required, "content": {body_type: {"schema": body}}}
    resp: dict[str, Any] = {"200": {"description": "Успех"}}
    if ok is not None:
        resp["200"]["content"] = {ok_type: {"schema": ok}}
    codes = dict(errors or {})
    if auth:
        codes.setdefault("401", "")
    for code in sorted(codes):
        resp[code] = {"description": codes[code] or ERR_DOC.get(code, "Ошибка"),
                      "content": {"application/json": {"schema": ref("Error")}}}
    out["responses"] = resp
    return out


# ------------------------------------------------------------------ схемы
SCHEMAS: dict[str, Any] = {
    "Error": obj({
        "error": s("Машинный код ошибки — по нему клиент решает, что делать."),
        "message": s("Текст по-русски, можно показывать пользователю как есть (есть не всегда)."),
    }, ["error"], additionalProperties=True),
    "Track": obj({
        "id": I, "artist": S, "title": S, "album": S,
        "duration": s("Длительность для показа, «4:46»"), "seconds": I,
        "cover": s("Подписанная ссылка на обложку (24 ч) или null — обложки нет", NS),
        "stream": s("Подписанная ссылка на файл (12 ч): /api/stream/{id}?u=&e=&s="),
        "owned": s("Трек в медиатеке пользователя (в общих плейлистах может быть false)", B),
        "gain": s("ReplayGain к −14 LUFS, дБ; null — ещё не посчитан", NN),
        "peak": s("Пиковый уровень 0…1", NN),
        "genre": NS, "year": NI,
        "added_at": s("Когда появился в медиатеке (unix); null вне списков медиатеки", NI),
        "broken": s("Файл помечен проблемным (идёт или не удалась перекачка)", B),
        "artist_key": s("Папка исполнителя: a для /api/library/artist и /api/library/album", NS),
        "album_key": s("Папка альбома: b для /api/library/album", NS),
        "size": s("Размер файла, байт", NI),
    }, ["id", "artist", "title", "stream"], "Трек медиатеки — одинаковый во всех ответах."),
    "PlaylistTrack": {"allOf": [{"$ref": "#/components/schemas/Track"},
                                obj({"item_id": s("Позиция в плейлисте (для remove/move)", I), "added_by": NS})]},
    "SearchTrack": obj({
        "i": s("Индекс в результате (для /api/download)", I), "artist": S, "title": S, "album": NS,
        "duration": S, "key": s("match_key «артист|название» — сверка с /api/library/keys", NS),
        "isrc": NS, "cover": NS, "preview": s("30-секундный фрагмент (https)", NS),
        "year": NI, "no": s("Номер трека в альбоме (страницы каталога)", NI),
    }, ["i", "artist", "title"], "Трек каталога (ещё не скачан)."),
    "SearchAlbum": obj({"i": I, "id": NS, "title": S, "year": NI, "kind": NS, "artist": S, "artist_id": NS,
                        "cover": NS}),
    "SearchArtist": obj({"id": S, "name": S, "picture": NS, "fans": NI}),
    "PlaylistBrief": obj({
        "id": I, "name": S, "description": S, "count": I, "seconds": I,
        "covers": s("До 4 обложек для мозаики", arr(S)),
        "shared": s("Совместный: есть ссылка или участники (бейдж «Общий»)", B),
        "invite_enabled": s("Ссылка-приглашение сейчас работает", B),
        "role": enum("owner", "member"), "owner": S,
        "members": arr(obj({"login": S})), "updated_at": I,
        "invite": {"$ref": "#/components/schemas/Invite"},
    }, ["id", "name", "role"]),
    "Invite": obj({"token": S, "link": s("t.me/<бот>?start=pl_<token> или null", NS),
                   "app_link": s("<сервер>/app/#join=<token>")}, desc="Только у владельца общего плейлиста."),
    "Lyrics": obj({
        "synced": nullable(arr(obj({"t": s("Секунды от начала", N), "text": S}))),
        "plain": NS, "source": nullable(enum("lrc", "tags")),
    }),
    "Upload": obj({
        "id": I, "name": S,
        "state": enum("receiving", "queued", "analyzing", "need_info", "processing", "done", "duplicate", "failed",
                      "cancelled"),
        "state_text": s("Состояние по-русски"), "artist": S, "title": S, "album": S, "duration": NN, "size": NI,
        "note": NS, "created_at": I, "updated_at": I, "track": nullable({"$ref": "#/components/schemas/Track"}),
    }),
    "Device": obj({"id": I, "name": S, "platform": S, "created_at": I, "last_seen": I,
                   "current": s("Это устройство (вошли по его токену)", B)}),
    "Setting": obj({"value": s("Любой JSON до 32 КБ", ANY), "updated_at": s("Миллисекунды, строго растут", I)}),
    "Station": obj({"id": I, "name": S, "url": s("Поток станции — можно играть напрямую")}),
    "Rejected": obj({"name": S, "error": S, "message": S}),
}

CATALOG_PICK = obj({
    "token": s("token из ответа поиска/каталога (живёт ограниченное время → 404 expired)"),
    "kind": s("track — tracks[i]; all — все tracks; album — albums[i] целиком; tracks — индексы idx; "
              "album_track — трек j альбома i", enum("track", "all", "album", "tracks", "album_track")),
    "i": I, "j": I, "idx": s("Индексы tracks для kind=tracks (до 500)", arr(I)),
}, ["token", "kind"])


# ------------------------------------------------------------------ пути
def paths() -> dict:
    P: dict[str, dict] = {}

    def add(path: str, method: str, spec: dict) -> None:
        P.setdefault(path, {})[method] = spec

    # --- сервис и вход
    add("/api/info", "get", op(
        "Сервис", "Версия API, способы входа, лимиты", op_id="getInfo", auth=False,
        desc="Первый запрос клиента: что умеет сервер. api_version растёт только при несовместимых изменениях.",
        ok=obj({"name": S, "api_version": I, "openapi": S,
                "auth": obj({"bot": NS, "login_widget": B, "methods": arr(S), "platforms": arr(S),
                             "max_devices": I, "login_ttl": I}),
                "limits": {"type": "object", "additionalProperties": I}, "features": arr(S)})))
    add("/api/openapi.json", "get", op("Сервис", "Это описание (OpenAPI 3.1)", op_id="getOpenapi", auth=False,
                                       ok={"type": "object"}))
    add("/api/me", "get", op(
        "Сервис", "Текущий пользователь", op_id="getMe",
        ok=obj({"login": S, "tracks": I,
                "stream_url": s("Живой поток «Моей Волны» (mp3; секрет в пути — не показывать посторонним)"),
                "jellyfin_url": NS, "is_admin": B})))
    add("/api/auth/config", "get", op("Вход", "Настройки входа", op_id="getAuthConfig", auth=False,
                                      ok=obj({"bot": NS, "login_widget": B})))
    add("/api/auth/start", "post", op(
        "Вход", "Начать вход через бота", op_id="authStart", auth=False,
        desc="Показать code крупно и открыть link (или QR с ним). В боте пользователь выбирает этот код. Затем "
             "опрашивать /api/auth/poll раз в 2 с, не дольше expires_in. 10 запросов за 10 мин на IP.",
        body=obj({"device": s("Имя устройства, до 64 символов"),
                  "platform": enum("web", "pwa", "windows", "android", "ios", "linux", "macos")}),
        body_required=False, ok=obj({"nonce": S, "code": s("4 цифры"), "link": NS, "expires_in": I}),
        errors={"429": "rate_limited", "503": "busy"}))
    add("/api/auth/poll", "post", op(
        "Вход", "Опрос подтверждения входа", op_id="authPoll", auth=False,
        desc="pending — ждать; denied — отказ; expired — истёк или уже использован; too_many_devices — уже 20 "
             "устройств (nonce не тратится); ok — token (выдаётся один раз) и login.",
        body=obj({"nonce": S}, ["nonce"]),
        ok=obj({"status": enum("pending", "denied", "expired", "too_many_devices", "ok"),
                "token": S, "login": S, "message": S}, ["status"]),
        errors={"400": "bad_nonce", "429": "rate_limited"}))
    add("/api/auth/telegram", "post", op(
        "Вход", "Вход через Telegram Login Widget", op_id="authWidget", auth=False,
        desc="Поля виджета как есть (id, first_name, auth_date, hash, …) + device, platform. auth_date не старше "
             "5 мин, повтор тех же данных — 401. Только если /api/auth/config вернул login_widget: true.",
        body={"type": "object", "additionalProperties": True}, ok=obj({"status": S, "token": S, "login": S}),
        errors={"401": "unauthorized", "403": "not_registered", "404": "disabled", "409": "too_many_devices",
                "429": "rate_limited"}))
    add("/api/auth/logout", "post", op(
        "Вход", "Выйти", op_id="authLogout",
        desc="Отзывает токен этого устройства; {\"all\": true} — все токены пользователя, включая этот.",
        body=obj({"all": B}), body_required=False, ok=obj({"ok": B, "revoked": {"anyOf": [B, I]}})))
    add("/api/devices", "get", op("Вход", "Устройства пользователя", op_id="listDevices",
                                  ok=obj({"items": arr(ref("Device"))})))
    add("/api/devices/{did}/revoke", "post", op("Вход", "Отозвать устройство", op_id="revokeDevice",
                                                params=[p("did", "id устройства")], ok=obj({"ok": B}),
                                                errors={"404": "not_found"}))
    add("/api/devices/revoke-all", "post", op("Вход", "Выйти на всех устройствах", op_id="revokeAllDevices",
                                              ok=obj({"ok": B, "revoked": I})))

    # --- поиск и загрузка
    add("/api/search", "get", op(
        "Поиск", "Поиск в каталоге", op_id="search",
        desc="Результат хранится на сервере под token — им пользуются /api/album и /api/download.",
        params=[q("q", "Запрос, до 200 символов", required=True)],
        ok=obj({"token": S, "artist": NS, "artist_id": NS, "picture": NS, "fans": NI,
                "tracks": arr(ref("SearchTrack")), "albums": arr(ref("SearchAlbum")),
                "artists": arr(ref("SearchArtist"))}),
        errors={"400": "empty_query"}))
    add("/api/album", "get", op(
        "Поиск", "Треки альбома из результата поиска", op_id="searchAlbum",
        params=[q("token", "token поиска", required=True), q("i", "Индекс альбома", I, True)],
        ok=obj({"i": I, "title": S, "artist": S, "year": NI, "tracks": arr(ref("SearchTrack"))}),
        errors={"400": "bad_index", "403": "forbidden (чужой token)", "404": "expired"}))
    cat_err = {"400": "bad_id", "404": "not_found", "502": "catalog_failed"}
    add("/api/catalog/album", "get", op(
        "Поиск", "Страница альбома каталога", op_id="catalogAlbum", params=[q("id", "id альбома", required=True)],
        ok=obj({"token": S, "id": S, "title": S, "artist": S, "artist_id": NS, "year": NI, "kind": NS,
                "cover": NS, "tracks": arr(ref("SearchTrack"))}), errors=cat_err))
    add("/api/catalog/artist", "get", op(
        "Поиск", "Страница исполнителя каталога", op_id="catalogArtist",
        params=[q("id", "id исполнителя", required=True)],
        ok=obj({"token": S, "id": S, "name": S, "picture": NS, "fans": NI, "tracks": arr(ref("SearchTrack")),
                "albums": arr(ref("SearchAlbum"))}), errors=cat_err))
    add("/api/catalog/artist/tracks", "get", op(
        "Поиск", "Все треки исполнителя", op_id="catalogArtistTracks",
        params=[q("id", "id исполнителя", required=True)],
        ok=obj({"token": S, "id": S, "name": S, "albums": I, "capped": s("Список обрезан", B),
                "tracks": arr(ref("SearchTrack"))}), errors=cat_err))
    add("/api/download", "post", op(
        "Загрузка", "Скачать из результата поиска/каталога", op_id="download",
        desc="Ставит в очередь; ход — /api/activity. playlist_id — заодно добавлять в плейлист по порядку.",
        body={"allOf": [CATALOG_PICK, obj({"playlist_id": I})]},
        ok=obj({"queued": I, "title": S, "playlist": obj({"id": I, "name": S, "added_now": I})}),
        errors={"400": "bad_kind / bad_index", "403": "forbidden",
                "404": "expired / nothing_to_download / playlist_not_found", "429": "quota", "507": "disk_full"}))
    add("/api/download/link", "post", op(
        "Загрузка", "Скачать по ссылке", op_id="downloadLink",
        desc="Spotify / YouTube / SoundCloud / Bandcamp / Яндекс Музыка… — как ссылка, присланная боту. Ссылка на "
             "плейлист создаёт плейлист.",
        body=obj({"url": S}, ["url"]),
        ok=obj({"queued": I, "title": S, "kind": S, "note": NS, "playlist": nullable(obj({"id": I, "name": S}))}),
        errors={"400": "bad_url", "404": "nothing_to_download", "422": "resolve_error", "429": "quota",
                "502": "resolve_failed", "507": "disk_full"}))
    add("/api/activity", "get", op(
        "Загрузка", "Что качается и журнал", op_id="getActivity",
        desc="Опрашивать раз в 2–5 с, пока есть active/jobs. log — последние 50 событий, новые первыми.",
        ok=obj({"active": arr(S),
                "active_items": arr(obj({"text": S, "artist": NS, "title": NS, "track_id": NI, "progress": NS})),
                "jobs": arr(obj({"title": S, "total": I, "started": I})),
                "log": arr(obj({"ts": I, "text": S})), "queue": I})))

    # --- медиатека
    add("/api/library", "get", op(
        "Медиатека", "Треки медиатеки (страница)", op_id="getLibrary",
        params=VIEW + [q("sort", "По умолчанию added", enum(*SORT_TRACKS)), q("offset", "С какого", I),
                       q("limit", "1…500, по умолчанию 50", I)],
        ok=obj({"total": s("Найдено после фильтра", I), "library_total": I, "offset": I, "limit": I, "sort": S,
                "items": arr(ref("Track"))}), errors={"400": "bad_sort / bad_order"}))
    add("/api/library/play", "get", op(
        "Медиатека", "Очередь «Слушать»/«Перемешать» по текущему виду", op_id="libraryPlay",
        params=VIEW + [q("sort", "По умолчанию artist", enum(*SORT_TRACKS)),
                       q("limit", "До 3000, по умолчанию 1000", I)],
        ok=obj({"total": I, "sort": S, "items": arr(ref("Track"))}), errors={"400": "bad_sort / bad_order"}))
    add("/api/library/genres", "get", op("Медиатека", "Жанры медиатеки", op_id="libraryGenres",
                                         ok=obj({"items": arr(obj({"name": S, "count": I}))})))
    grp = [q("genre", "Фильтр по жанру"), q("sort", "По умолчанию name", enum(*SORT_GROUPS)), ORDER]
    add("/api/library/artists", "get", op(
        "Медиатека", "Исполнители медиатеки", op_id="libraryArtists", params=grp,
        ok=obj({"items": arr(obj({"key": S, "name": S, "count": I, "year": NI, "added_at": NI, "image": NS}))}),
        errors={"400": "bad_sort / bad_order"}))
    add("/api/library/albums", "get", op(
        "Медиатека", "Альбомы медиатеки", op_id="libraryAlbums", params=grp,
        ok=obj({"items": arr(obj({"artist_key": S, "key": S, "title": S, "artist": S, "count": I, "year": NI,
                                  "added_at": NI, "cover": NS}))}),
        errors={"400": "bad_sort / bad_order"}))
    album_group = obj({"key": S, "title": S, "cover": NS, "tracks": arr(ref("Track"))})
    add("/api/library/artist", "get", op(
        "Медиатека", "Исполнитель медиатеки (по альбомам)", op_id="libraryArtist",
        params=[q("a", "artist_key", required=True)],
        ok=obj({"key": S, "name": S, "count": I, "image": NS, "albums": arr(album_group)}),
        errors={"400": "bad_artist", "404": "not_found"}))
    add("/api/library/album", "get", op(
        "Медиатека", "Альбом медиатеки", op_id="libraryAlbum",
        params=[q("a", "artist_key", required=True), q("b", "album_key", required=True)],
        ok={"allOf": [album_group, obj({"artist_key": S, "artist": S})]},
        errors={"400": "bad_album", "404": "not_found"}))
    add("/api/library/keys", "get", op(
        "Медиатека", "Ключи медиатеки (✓ в поиске)", op_id="libraryKeys",
        desc="Сравнивайте key и isrc треков поиска с этими множествами.", ok=obj({"keys": arr(S), "isrc": arr(S)})))
    add("/api/tracks", "get", op(
        "Медиатека", "Треки по id (свежие ссылки)", op_id="getTracks",
        desc="Подписанные stream/cover истекают — обновляйте ими очередь. Недоступные id пропускаются.",
        params=[q("ids", "id через запятую, до 500", required=True)], ok=obj({"items": arr(ref("Track"))}),
        errors={"400": "too_many / bad_ids"}))
    add("/api/delete", "post", op(
        "Медиатека", "Удалить трек из медиатеки", op_id="deleteTrack", body=obj({"track_id": I}, ["track_id"]),
        ok=obj({"ok": B, "text": S, "track_id": I, "gone": s("Недоступен совсем — убрать из очереди", B)}),
        errors={"400": "bad_track_id", "404": "not_found"}))
    add("/api/library/delete", "post", op(
        "Медиатека", "Удалить пачку из медиатеки", op_id="deleteTracks",
        body=obj({"track_ids": arr(I)}, ["track_ids"]),
        ok=obj({"deleted": I, "skipped": I, "deleted_ids": arr(I), "gone_ids": arr(I)}),
        errors={"400": "bad_track_ids"}))
    add("/api/tracks/{tid}/redownload", "post", op(
        "Медиатека", "Перекачать трек", op_id="redownloadTrack",
        desc="Новый файл после проверки заменит старый у всех держателей; ход — /api/activity. 10 в час.",
        params=[p("tid", "id трека")], ok=obj({"ok": B, "text": S, "job": obj({"title": S, "track_id": I})}),
        errors={"404": "not_found", "409": "busy", "429": "rate_limited / quota", "507": "disk_full"}))

    # --- воспроизведение
    signed = [q("u", "логин", required=True), q("e", "срок (unix)", I, True), q("s", "подпись", required=True)]
    add("/api/stream/{track_id}", "get", op(
        "Воспроизведение", "Файл трека", op_id="stream", auth=False,
        desc="Берите готовую ссылку из поля stream трека, не собирайте сами. Range/206 поддерживается. Любая "
             "ошибка (истекла, нет доступа) — 404: обновите ссылку через /api/tracks.",
        params=[p("track_id", "id трека")] + signed, ok=BIN, ok_type="audio/mpeg",
        errors={"404": "Нет или истекла"}))
    add("/api/cover/{track_id}", "get", op(
        "Воспроизведение", "Обложка трека", op_id="cover", auth=False, desc="Из поля cover. JPEG, ссылка на 24 ч.",
        params=[p("track_id", "id трека")] + signed, ok=BIN, ok_type="image/jpeg",
        errors={"404": "Нет или истекла"}))
    add("/api/artist-image", "get", op(
        "Воспроизведение", "Фото исполнителя", op_id="artistImage", auth=False,
        desc="Из поля image исполнителя медиатеки.",
        params=[q("u", "логин", required=True), q("a", "artist_key", required=True),
                q("s", "подпись", required=True)], ok=BIN, ok_type="image/jpeg", errors={"404": "Нет"}))
    add("/api/lyrics/{track_id}", "get", op("Воспроизведение", "Текст песни", op_id="getLyrics",
                                            params=[p("track_id", "id трека")], ok=ref("Lyrics"),
                                            errors={"404": "not_found"}))
    add("/api/tracks/{tid}/lyrics/refetch", "post", op(
        "Воспроизведение", "Найти текст заново", op_id="refetchLyrics", desc="Синхронно, до 100 с. 20 раз в час.",
        params=[p("tid", "id трека")], body=obj({"clear": B}), body_required=False,
        ok={"allOf": [ref("Lyrics"), obj({"status": enum("updated", "same", "cleared", "not_found")})]},
        errors={"404": "not_found", "409": "busy", "429": "rate_limited", "504": "timeout"}))
    add("/stream/{login}/{token}.mp3", "get", op(
        "Воспроизведение", "Живой поток «Моя Волна»", op_id="waveStream", auth=False,
        desc="Бесконечный MP3 (icy). Ссылка — stream_url из /api/me; секрет в пути.",
        params=[p("login", "логин", S), p("token", "токен потока", S)], ok=BIN, ok_type="audio/mpeg",
        errors={"404": "Нет"}))

    # --- волна и радио
    add("/api/wave/now", "get", op(
        "Волна и радио", "Что сейчас в эфире «Моей Волны»", op_id="waveNow",
        ok=obj({"live": B, "listeners": I, "artist": NS, "title": NS, "album": NS, "duration": NI,
                "started_at": NI, "cover": NS, "track_id": NI})))
    add("/api/wave/discover", "post", op(
        "Волна и радио", "Подобрать и скачать новые треки «Моей Волны»", op_id="waveDiscover",
        body=obj({"size": s("1…50", I)}), body_required=False, ok=obj({"seeds": arr(S), "queued": arr(S)}),
        errors={"409": "busy", "502": "wave_failed"}))
    add("/api/wave/refresh", "post", op("Волна и радио", "Обновить плейлист «Моя Волна» в Jellyfin",
                                        op_id="waveRefresh", ok=obj({"tracks": I}),
                                        errors={"502": "jellyfin_failed"}))
    add("/api/radio", "get", op("Волна и радио", "Станции интернет-радио", op_id="listRadio",
                                ok=obj({"items": arr(ref("Station"))})))
    add("/api/radio", "post", op(
        "Волна и радио", "Добавить станцию", op_id="addRadio",
        desc="Только публичные http(s)-потоки. jellyfin: false — Jellyfin не принял список (станция сохранена).",
        body=obj({"name": S, "url": S}, ["name", "url"]),
        ok={"allOf": [ref("Station"), obj({"jellyfin": B})]}, errors={"400": "bad_station"}))
    add("/api/radio/{rid}/delete", "post", op(
        "Волна и радио", "Удалить станцию (администратор)", op_id="deleteRadio", params=[p("rid", "id станции")],
        ok=obj({"ok": B, "jellyfin": B}), errors={"403": "forbidden", "404": "not_found"}))

    # --- плейлисты
    pid = [p("pid", "id плейлиста")]
    nf = {"404": "not_found"}
    add("/api/playlists", "get", op("Плейлисты", "Свои и совместные плейлисты", op_id="listPlaylists",
                                    ok=obj({"items": arr(ref("PlaylistBrief"))})))
    add("/api/playlists", "post", op(
        "Плейлисты", "Создать плейлист", op_id="createPlaylist",
        desc="catalog — наполнить из поиска/каталога (треки скачиваются и добавляются по мере загрузки).",
        body=obj({"name": S, "description": S, "shared": B, "track_ids": arr(I), "catalog": CATALOG_PICK},
                 ["name"]),
        ok={"allOf": [ref("PlaylistBrief"), obj({"queued": I, "added_now": I})]},
        errors={"400": "bad_name / bad_description / bad_track_ids / bad_catalog / bad_kind / bad_index",
                "404": "expired", "409": "limit", "429": "quota", "507": "disk_full"}))
    add("/api/playlists/{pid}", "get", op(
        "Плейлисты", "Плейлист с треками", op_id="getPlaylist",
        params=pid + [q("sort", "manual — порядок плейлиста (по умолчанию) или как у медиатеки",
                        enum("manual", *SORT_TRACKS)), ORDER, q("seed", "Для sort=random", I)],
        ok={"allOf": [ref("PlaylistBrief"), obj({"sort": S, "tracks": arr(ref("PlaylistTrack"))})]},
        errors={"400": "bad_sort / bad_order", **nf}))
    add("/api/playlists/{pid}/update", "post", op(
        "Плейлисты", "Переименовать / описание", op_id="updatePlaylist", params=pid,
        body=obj({"name": S, "description": S}), ok=ref("PlaylistBrief"),
        errors={"400": "bad_name / bad_description", **nf}))
    add("/api/playlists/{pid}/share", "post", op(
        "Плейлисты", "Общий доступ (владелец)", op_id="sharePlaylist", params=pid,
        body=obj({"enabled": B, "regenerate": B}, ["enabled"]), ok=obj({"shared": B, "invite": nullable(ref("Invite"))}),
        errors={"400": "bad_enabled", "403": "forbidden", **nf}))
    add("/api/playlists/{pid}/add", "post", op(
        "Плейлисты", "Добавить треки", op_id="addToPlaylist", params=pid,
        desc="Одно из: track_ids (до 5000), album {a, b} или artist (папки своей медиатеки).",
        body=obj({"track_ids": arr(I), "album": obj({"a": S, "b": S}), "artist": S}),
        ok=obj({"added": I, "skipped": I, "count": I, "full": B}),
        errors={"400": "bad_track_ids / bad_album / bad_artist / nothing_to_add", **nf}))
    add("/api/playlists/{pid}/remove", "post", op(
        "Плейлисты", "Убрать позиции", op_id="removeFromPlaylist", params=pid,
        body=obj({"item_ids": arr(I)}, ["item_ids"]), ok=obj({"removed": I, "count": I}),
        errors={"400": "bad_item_ids", **nf}))
    add("/api/playlists/{pid}/move", "post", op(
        "Плейлисты", "Переставить позицию", op_id="movePlaylistItem", params=pid,
        body=obj({"item_id": I, "to": s("Индекс в итоговом списке", I)}, ["item_id", "to"]), ok=obj({"ok": B}),
        errors={"400": "bad_move", **nf}))
    add("/api/playlists/{pid}/move-many", "post", op(
        "Плейлисты", "Переставить выделение блоком", op_id="movePlaylistItems", params=pid,
        body=obj({"item_ids": arr(I), "to": I}, ["item_ids", "to"]), ok=obj({"ok": B, "moved": I}),
        errors={"400": "bad_move", **nf}))
    add("/api/playlists/{pid}/delete", "post", op("Плейлисты", "Удалить (владелец)", op_id="deletePlaylist",
                                                  params=pid, ok=obj({"ok": B}),
                                                  errors={"403": "forbidden", **nf}))
    add("/api/playlists/{pid}/leave", "post", op("Плейлисты", "Выйти (участник)", op_id="leavePlaylist",
                                                 params=pid, ok=obj({"ok": B}),
                                                 errors={"409": "owner_cannot_leave", **nf}))
    add("/api/playlists/{pid}/kick", "post", op(
        "Плейлисты", "Убрать участника (владелец)", op_id="kickMember", params=pid,
        body=obj({"login": S}, ["login"]), ok=ref("PlaylistBrief"),
        errors={"403": "forbidden", "404": "not_found / not_member"}))
    add("/api/playlists/{pid}/save", "post", op(
        "Плейлисты", "Сохранить недостающие треки в медиатеку", op_id="savePlaylist", params=pid,
        ok=obj({"linked": I, "missing": I, "failed": I}), errors=nf))
    add("/api/playlists/join", "get", op(
        "Плейлисты", "Превью приглашения", op_id="joinPreview", params=[q("token", "Токен", required=True)],
        ok=obj({"id": I, "name": S, "owner": S, "count": I, "covers": arr(S), "already": B}), errors=nf))
    add("/api/playlists/join", "post", op(
        "Плейлисты", "Вступить по приглашению", op_id="joinPlaylist", body=obj({"token": S}, ["token"]),
        ok=ref("PlaylistBrief"), errors={"409": "members_limit", **nf}))
    add("/api/playlists/for-track", "get", op(
        "Плейлисты", "В каких плейлистах трек", op_id="playlistsForTrack",
        params=[q("track_id", "id трека", I, True)], ok=obj({"items": arr(obj({"id": I, "name": S, "has": B}))}),
        errors={"400": "bad_track_id"}))

    # --- свои файлы и импорт
    add("/api/upload", "post", op(
        "Файлы и импорт", "Загрузить аудиофайлы", op_id="upload",
        desc="multipart/form-data, любое имя поля, до 50 файлов. Ход — /api/uploads. При 413/429/507 ответ приходит "
             "сразу с Connection: close — уже принятые файлы есть в uploads.",
        body={"type": "object", "additionalProperties": BIN}, body_type="multipart/form-data",
        ok=obj({"uploads": arr(obj({"id": I, "name": S, "state": S})), "rejected": arr(ref("Rejected"))}),
        errors={"400": "bad_multipart / no_files", "408": "timeout", "413": "too_large / too_many / quota",
                "415": "use_import — это список треков, отправьте в /api/import/file", "429": "busy / too_many",
                "503": "unavailable", "507": "disk_full"}))
    add("/api/uploads", "get", op("Файлы и импорт", "Мои загрузки (последние 100)", op_id="listUploads",
                                  ok=obj({"items": arr(ref("Upload"))})))
    uid = [p("uid", "id загрузки")]
    up_err = {"404": "not_found", "409": "bad_state"}
    add("/api/uploads/{uid}/info", "post", op(
        "Файлы и импорт", "Ответить на need_info", op_id="uploadInfo", params=uid,
        body=obj({"artist": S, "title": S, "album": S}, ["artist", "title"]), ok=ref("Upload"),
        errors={"400": "bad_info", **up_err}))
    add("/api/uploads/{uid}/skip", "post", op("Файлы и импорт", "Оставить как есть", op_id="uploadSkip",
                                              params=uid, ok=ref("Upload"), errors=up_err))
    add("/api/uploads/{uid}/cancel", "post", op("Файлы и импорт", "Отменить загрузку", op_id="uploadCancel",
                                                params=uid, ok=ref("Upload"), errors=up_err))
    add("/api/import", "get", op(
        "Файлы и импорт", "Идущие импорты списков", op_id="listImports",
        ok=obj({"imports": arr(obj({"id": I, "filename": S, "total": I, "state": S,
                                    "counts": {"type": "object", "additionalProperties": I}, "text": S}))})))
    add("/api/import/file", "post", op(
        "Файлы и импорт", "Импорт списка TXT/CSV/TSV", op_id="importFile",
        desc="multipart, поле file, до 2 МБ. Импорт сразу запускается и создаёт плейлист.",
        body=obj({"file": BIN}, ["file"]), body_type="multipart/form-data",
        ok=obj({"import_id": I, "format": S, "total": I, "name": S,
                "playlist": nullable(obj({"id": I, "name": S}))}),
        errors={"400": "bad_multipart / no_file / empty / bad_format / no_tracks / bad_file", "408": "timeout",
                "413": "too_large", "415": "unsupported"}))
    add("/api/import/pause", "post", op("Файлы и импорт", "Пауза импорта", op_id="importPause",
                                        body=obj({"id": I}, ["id"]), ok=obj({"ok": B, "state": S}),
                                        errors={"400": "bad_id", "409": "bad_state"}))
    add("/api/import/resume", "post", op("Файлы и импорт", "Продолжить импорт", op_id="importResume",
                                         body=obj({"id": I}, ["id"]), ok=obj({"ok": B, "state": S}),
                                         errors={"400": "bad_id", "409": "bad_state"}))
    add("/api/import/cancel", "post", op(
        "Файлы и импорт", "Остановить импорт", op_id="importCancel", desc="С id — один импорт, без тела — все.",
        body=obj({"id": I}), body_required=False, ok=obj({"cancelled": I}),
        errors={"400": "bad_id", "404": "not_found"}))
    add("/api/import/failed", "get", op(
        "Файлы и импорт", "Ненайденные треки импорта", op_id="importFailed", params=[q("id", "id импорта", I, True)],
        ok=obj({"items": arr(obj({"artist": S, "title": S, "album": S, "note": S}))}),
        errors={"400": "bad_id", "404": "not_found"}))

    # --- настройки
    key = [p("key", "Ключ: [a-z0-9._-]{1,80}", S)]
    add("/api/settings", "get", op(
        "Настройки", "Все настройки пользователя", op_id="getSettings",
        desc="Хранилище «ключ → JSON», общее для устройств пользователя. Сервер значения не толкует.",
        ok=obj({"items": {"type": "object", "additionalProperties": ref("Setting")}})))
    add("/api/settings/{key}", "put", op(
        "Настройки", "Записать настройку", op_id="putSetting", params=key,
        desc="base — updated_at, от которого вы меняли: если на сервере уже другое, 409 conflict с current.",
        body=obj({"value": ANY, "base": I}, ["value"]), ok=obj({"key": S, "value": ANY, "updated_at": I}),
        errors={"400": "bad_key / bad_value / bad_base / bad_json", "409": "conflict (current) / limit",
                "413": "too_large"}))
    add("/api/settings/{key}", "delete", op("Настройки", "Удалить настройку", op_id="deleteSetting", params=key,
                                            ok=obj({"ok": B, "deleted": B}), errors={"400": "bad_key"}))

    # --- администратор
    add("/api/admin/status", "get", op(
        "Администратор", "Состояние сервера", op_id="adminStatus",
        ok=obj({"disk": obj({"total": I, "used": I, "free": I}), "users": I, "tracks": I, "links": I,
                "soulseek_online": nullable(B), "queue": I, "jellyfin_url": NS}), errors={"403": "forbidden"}))
    add("/api/admin/users", "get", op(
        "Администратор", "Пользователи", op_id="adminUsers",
        ok=obj({"items": arr(obj({"login": S, "tracks": I, "is_admin": B, "telegram": B}))}),
        errors={"403": "forbidden"}))

    # --- обновления приложений
    add("/updates/{name}", "get", op(
        "Обновления", "Файлы обновлений приложений", op_id="updates", auth=False,
        desc="latest.json (Windows, формат tauri-updater v2), android.json, установщики и подписи.",
        params=[p("name", "Имя файла", S)], ok=BIN, ok_type="application/octet-stream", errors={"404": "Нет"}))
    return P


def build() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "PiMusic API", "version": API_VERSION,
            "description": "API домашнего музыкального сервиса PiMusic: поиск и загрузка музыки, медиатека, плейлисты, "
                           "поток, тексты, «Моя Волна», радио, свои файлы. Тот же API использует официальный клиент "
                           "(Mini App, Windows, Android). Руководство — docs/api/README.md в репозитории.",
        },
        "servers": [{"url": "https://music.example.com"}],
        "security": [{"bearer": []}, {"telegramInitData": []}],
        "tags": [{"name": t} for t in ("Сервис", "Вход", "Поиск", "Загрузка", "Медиатека", "Воспроизведение",
                                       "Волна и радио", "Плейлисты", "Файлы и импорт", "Настройки", "Администратор",
                                       "Обновления")],
        "paths": paths(),
        "components": {
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer",
                           "description": "Токен устройства из /api/auth/poll или /api/auth/telegram"},
                "telegramInitData": {"type": "apiKey", "in": "header", "name": "X-Telegram-Init-Data",
                                     "description": "Telegram.WebApp.initData — только для Mini App внутри Telegram"},
            },
            "schemas": SCHEMAS,
        },
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    OUT.write_bytes(render().encode("utf-8"))
    sys.stdout.write(f"{OUT.relative_to(ROOT)}: {len(build()['paths'])} путей\n")
