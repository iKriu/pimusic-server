# PiMusic API — руководство для своего клиента

Официальные клиенты PiMusic (Mini App в Telegram, Windows, Android) пользуются **только этим API**: сервер не
делает ничего такого, что доступно лишь «своим». Если клиент поддерживает всё, что описано ниже, он ничего не теряет
по сравнению с официальным.

- **Справочник всех эндпоинтов** — [`bot/app/openapi.json`](../../bot/app/openapi.json) (OpenAPI 3.1). Сервер отдаёт
  его по `GET /api/openapi.json`, файл открывается в Swagger UI / Redoc / Postman / Insomnia, по нему можно
  сгенерировать клиент (`openapi-generator`, `openapi-typescript`, `oapi-codegen`…).
- **Этот документ** — как всё связано: вход, подписанные ссылки, сценарии, ошибки, лимиты и правила совместимости.
- История решений — `docs/design/PLAYER_API.md`, `docs/design/APPS_API.md`.

Адрес сервера в примерах — `https://music.example.com`. На своём сервере подставьте свой origin.

## 1. Быстрый старт

```bash
# 1) что умеет сервер (без входа)
curl https://music.example.com/api/info

# 2) вход: получить код и ссылку на бота
curl -X POST https://music.example.com/api/auth/start \
     -H 'Content-Type: application/json' -d '{"device":"Мой клиент","platform":"linux"}'
# → {"nonce":"…","code":"4821","link":"https://t.me/<бот>?start=login_…","expires_in":300}

# 3) пользователь открывает link и в боте нажимает кнопку с кодом 4821; клиент опрашивает раз в 2 с
curl -X POST https://music.example.com/api/auth/poll -H 'Content-Type: application/json' -d '{"nonce":"…"}'
# → {"status":"pending"} … {"status":"ok","token":"…","login":"alice"}

# 4) дальше — с токеном
curl "https://music.example.com/api/library?limit=20" -H "Authorization: Bearer $TOKEN"
```

Минимальный клиент на JavaScript:

```js
const BASE = "https://music.example.com";
let token = localStorage.getItem("pimusic.token");

async function api(path, { method = "GET", body } = {}) {
  const r = await fetch(BASE + "/api" + path, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) throw Object.assign(new Error("login"), data);     // показать экран входа
  if (!r.ok) throw Object.assign(new Error(data.message || data.error), data, { status: r.status });
  return data;
}

const lib = await api("/library?sort=added&limit=50");
const audio = new Audio(BASE + lib.items[0].stream);   // ссылка уже подписана
audio.play();
```

## 2. Вход и аккаунт

Аккаунт создаётся только в Telegram-боте (`/register ИНВАЙТ логин пароль`): это защищает сервер от посторонних.
Клиенту Telegram нужен только для входа.

| Способ | Когда | Как |
|---|---|---|
| **Токен устройства** (Bearer) | Любой свой клиент | `POST /api/auth/start` → пользователь подтверждает в боте → `POST /api/auth/poll` выдаёт `token`. Шлите `Authorization: Bearer <token>`. |
| Telegram Login Widget | Веб-клиент на своём домене | Если `GET /api/auth/config` → `login_widget: true`: поля виджета в `POST /api/auth/telegram`. Нужен `/setdomain` у @BotFather. |
| `X-Telegram-Init-Data` | Только Mini App внутри Telegram | Заголовок = `Telegram.WebApp.initData`. |

Правила входа через бота:
- `code` (4 цифры) показывайте крупно: в боте у пользователя три кнопки с кодами, верный — только ваш. Неверный
  выбор сразу отклоняет вход (`denied`). Рядом можно показать QR со ссылкой `link` для входа с телефона.
- Опрос — раз в 2 с, не дольше `expires_in` (5 мин). Токен выдаётся **один раз** — первому опросу после подтверждения.
- `too_many_devices` — уже 20 устройств. Nonce при этом не тратится: пользователь выходит на старом устройстве
  (`GET /api/devices`, `POST /api/devices/{id}/revoke`), и тот же опрос вернёт `ok`.
- Токен храните как пароль (Keychain / Keystore / DPAPI / `libsecret`). Он перестаёт действовать после 90 дней без
  запросов и при выходе.
- 401 с `error`: `revoked` (токен отозван), `token_expired` (90 дней), `unauthorized` (испорчен) — показать экран
  входа. 403 `not_registered` — Telegram-аккаунт не зарегистрирован в боте.
- Выход: `POST /api/auth/logout` (этот токен) или `{"all": true}` (все устройства).

Платформа `platform`: `web`, `pwa`, `windows`, `android`, `ios`, `linux`, `macos` — она видна в списке устройств.

## 3. Подписанные ссылки: поток, обложки, фото

`<audio>` и `<img>` не умеют слать заголовки, поэтому поток и картинки отдаются по **подписанным ссылкам без
авторизации**. Они приходят в ответах готовыми и относительными — приклейте к ним origin сервера:

| Поле | Что | Живёт |
|---|---|---|
| `track.stream` → `/api/stream/{id}?u=&e=&s=` | файл трека, Range/206 (перемотка) | 12 ч (в пределах часа не меняется) |
| `track.cover` → `/api/cover/{id}?u=&e=&s=` | обложка JPEG | 24 ч |
| `artist.image` → `/api/artist-image?u=&a=&s=` | фото исполнителя | бессрочно |

- Не собирайте и не меняйте ссылки сами: подпись знает только сервер.
- Любая ошибка ссылки — `404` без подробностей. Тогда обновите трек: `GET /api/tracks?ids=1,2,3` (до 500 id) вернёт
  свежие `stream`/`cover` в том же порядке, недоступные пропустит. Так же обновляйте очередь, восстановленную после
  перезапуска (официальный клиент делает это, если ей больше 6 ч).
- `Content-Length` у потока есть всегда — файл можно кэшировать целиком и играть офлайн.

## 4. Модель данных

**Трек медиатеки** (`Track`) — один и тот же объект везде (медиатека, альбомы, плейлисты, `/api/tracks`, загрузки):
`id`, `artist`, `title`, `album`, `duration` («4:46»), `seconds`, `cover`, `stream`, `owned` (трек в медиатеке
пользователя; в общем плейлисте бывает `false`), `gain`/`peak` (ReplayGain к −14 LUFS — для нормализации
громкости), `genre`, `year`, `added_at`, `broken` (файл перекачивается), `artist_key`/`album_key` (ключи для
«К исполнителю»/«К альбому»), `size`. В плейлистах дополнительно `item_id` (позиция) и `added_by`.

**Трек каталога** (`SearchTrack`) — результат поиска, ещё не скачан. У него нет `id`, есть индекс `i` в результате
и `token` результата; скачивание — по `token` + индексам (см. ниже). `key`/`isrc` сверяйте с
`GET /api/library/keys`, чтобы пометить «✓ уже есть».

**Время** — unix-секунды, кроме `updated_at` настроек (миллисекунды).

## 5. Сценарии

### Найти и скачать
1. `GET /api/search?q=…` → `token`, `tracks[]`, `albums[]`, `artists[]`.
2. Страницы каталога: `GET /api/catalog/album?id=`, `/api/catalog/artist?id=`, `/api/catalog/artist/tracks?id=`
   (у каждой свой `token`); треки альбома из поиска — `GET /api/album?token=&i=`.
3. Скачать: `POST /api/download {"token", "kind", "i"?, "j"?, "idx"?, "playlist_id"?}`:
   `track` — `tracks[i]`; `all` — все `tracks`; `album` — альбом `i`; `tracks` — выбранные `idx`; `album_track` —
   трек `j` альбома `i`. С `playlist_id` треки ещё и встают в этот плейлист по порядку.
4. По ссылке (Spotify, YouTube, SoundCloud, Bandcamp, Яндекс Музыка…): `POST /api/download/link {"url"}`.
5. Ход — `GET /api/activity` раз в 2–5 с, пока `active`/`jobs` не пусты; `log` — журнал событий по-русски.
   Скачанное появляется в `GET /api/library`.

`token` поиска живёт ограниченное время: `404 expired` — повторите поиск.

### Медиатека
- `GET /api/library` — страницы по 50 (до 500): `q`, `genre`, `sort` (`added|title|artist|album|genre|year|duration|
  random`), `order`, `seed`, `offset`, `limit`. `total` — найдено, `library_total` — всего.
- `GET /api/library/artists`, `/albums`, `/genres`; страницы `GET /api/library/artist?a=` и `/album?a=&b=`
  (`a`/`b` — `artist_key`/`album_key`).
- «Слушать»/«Перемешать» текущий вид — `GET /api/library/play` с теми же фильтрами (до 3000 треков).
- Удаление: `POST /api/delete {"track_id"}`, пачкой — `POST /api/library/delete {"track_ids"}`. `gone`/`gone_ids` —
  трек недоступен совсем (ни у кого его не осталось — файл удалён с сервера): уберите его из очереди. Остальные
  удалённые (есть в чужой медиатеке) продолжают играть из плейлистов.
- Плохой файл — `POST /api/tracks/{id}/redownload`.

### Воспроизведение
- Очередь целиком на клиенте: сервер хранит только треки и ссылки. Для `random` передавайте `seed`, чтобы
  страницы шли в одном порядке.
- Нормализация громкости (как в официальном клиенте): множитель `10^(min(gain, 6)/20)`; подъём (множитель > 1)
  не выше `0.99/peak`, чтобы не было клиппинга. `gain: null` — громкость ещё не посчитана, играть как есть.
- Текст: `GET /api/lyrics/{id}` → `synced` (`[{t, text}]`, подсветка строки по времени) или `plain`. Неверный
  текст — `POST /api/tracks/{id}/lyrics/refetch`.
- «Моя Волна»: бесконечный MP3-поток `stream_url` из `GET /api/me` (играет любой плеер); что в эфире —
  `GET /api/wave/now`; подобрать новые треки — `POST /api/wave/discover`.
- Интернет-радио: `GET /api/radio` (у станции есть `url` потока), добавить — `POST /api/radio {"name","url"}`.

### Плейлисты
- Список — `GET /api/playlists`, создать — `POST /api/playlists {"name", "description"?, "shared"?, "track_ids"?,
  "catalog"?}` (`catalog` — как тело `/api/download`: плейлист наполнится по мере загрузки).
- Плейлист — `GET /api/playlists/{id}?sort=manual|…`. Перетаскивание имеет смысл только при `sort=manual`:
  `move {"item_id","to"}`, `move-many {"item_ids","to"}`; ещё `add`, `remove {"item_ids"}`, `update`, `save`
  (недостающее — в свою медиатеку).
- Совместные: владелец включает `share {"enabled": true}` → `invite.app_link` / `invite.link` / `invite.token`.
  Приглашённый: `GET /api/playlists/join?token=` (превью) → `POST /api/playlists/join {"token"}`. Участник может
  `leave`, владелец — `kick {"login"}` и `delete`. Чужой или несуществующий плейлист — всегда `404`.
- Лист «Добавить в плейлист» — `GET /api/playlists/for-track?track_id=`.

### Свои файлы и импорт списков
- Аудио: `POST /api/upload` (multipart, до 50 файлов, любое имя поля). Состояние — `GET /api/uploads`.
  `need_info` — сервер не опознал трек: спросите «Исполнитель / Название / Альбом» и отправьте
  `POST /api/uploads/{id}/info`, либо `skip` (оставить как есть) / `cancel`.
- Список треков TXT/CSV/TSV (Tune My Music, Exportify, «Артист - Песня»): `POST /api/import/file` (поле `file`).
  Ход — `GET /api/import`; `pause` / `resume` / `cancel {"id"}`; ненайденное — `GET /api/import/failed?id=`.
- При входе по токену бот **не пишет** в Telegram о ходе загрузок и импорта — всё видно только через API, поэтому
  эти экраны нужны в клиенте.

### Настройки между устройствами
`GET /api/settings` → `{"items": {"<ключ>": {"value", "updated_at"}}}`, `PUT /api/settings/{ключ} {"value", "base"}`,
`DELETE`. Пишите с `base` = прежний `updated_at`: если на другом устройстве уже изменили, придёт `409 conflict` с
`current` — объедините и повторите. Ключ `[a-z0-9._-]{1,80}`, значение ≤ 32 КБ, не больше 100 ключей.

Чтобы ваш клиент и официальные приложения понимали настройки друг друга, используйте те же ключи:

| Ключ | Значение |
|---|---|
| `player` | `{"normalize": true, "crossfade": 0…12 (с), "speed": 0.5…2}` |
| `eq.presets` | свои пресеты `[{id, name, preamp, bands}]` |
| `eq.shared` | общий профиль `{active, preamp, bands}` |
| `eq.device.<uuid>` | устройство `{name, platform, mode: "sync"\|"local", active, preamp, bands, enabled, limiter}` |

`bands` — 10 полос `{type: "peaking"|"lowshelf"|"highshelf", f: 20…20000, g: −12…12, q: 0.3…8}` на 31, 62, 125,
250, 500, 1k, 2k, 4k, 8k, 16k Гц; `preamp` −12…6 дБ. Свои ключи называйте с префиксом своего клиента
(`myclient.…`), чтобы не пересечься с чужими.

### Устройства и администрирование
- `GET /api/devices`, `POST /api/devices/{id}/revoke`, `POST /api/devices/revoke-all`.
- Администратор (`GET /api/me` → `is_admin`): `GET /api/admin/status` (диск, очередь, Soulseek),
  `GET /api/admin/users`, `POST /api/radio/{id}/delete`. Остальным — `403 forbidden`.

## 6. Ошибки

Любая ошибка — JSON `{"error": "<код>", "message"?: "<текст по-русски>", …}`. Решайте по `error` и статусу;
`message` можно показывать пользователю как есть. Коды каждого эндпоинта — в `openapi.json`.

| Статус | Что делать |
|---|---|
| 400 | Ошибка клиента (`bad_*`, `bad_json`) — исправить запрос |
| 401 | Экран входа |
| 403 | `not_registered` — «зарегистрируйтесь в боте»; `forbidden` — действие не для этой роли |
| 404 | Нет или нет доступа; для ссылок — обновить через `/api/tracks`; `expired` — повторить поиск |
| 409 | Конфликт состояния (`busy`, `bad_state`, `conflict`, `limit`…) |
| 413 / 507 | Слишком большой файл / квота / мало места на сервере |
| 429 | `rate_limited`, `busy`, `quota` — подождать |
| 502 / 504 | Внешний сервис (каталог, Jellyfin, тексты) недоступен — повторить позже |

## 7. Лимиты

Актуальные значения — в `GET /api/info` → `limits`. Основные: до 500 id в одном запросе, очередь «Слушать» до 3000,
200 плейлистов по 5000 треков и 20 участников, 20 устройств, файл до `upload_max_mb` МБ и 50 файлов за раз, импорт
до 2 МБ. Частота: вход — 10 попыток за 10 мин с IP, перекачка — 10 в час, поиск текста — 20 в час.

## 8. Веб-клиент с другого домена (CORS)

CORS для `/api/*` и `/stream/*` включён только для известных Origin: оболочки Tauri/Capacitor и `localhost`. Для
своего веб-клиента администратор сервера добавляет его Origin в `CORS_ORIGINS` в `.env`. Нативным клиентам и
скриптам CORS не нужен. Cookie не используются (`credentials: "omit"`), авторизация — только заголовком.

## 9. Совместимость

- `api_version` в `/api/info` растёт **только** при несовместимых изменениях. Новые поля в ответах и новые
  эндпоинты появляются без смены версии — незнакомые поля игнорируйте.
- Старые варианты параметров продолжают работать (например, `order=shuffle|artist|recent` у `/api/library/play`).
- По списку `features` в `/api/info` можно скрыть в клиенте то, чего нет на старом сервере.

## 10. Чек-лист «без потери функционала»

- [ ] Вход через бота (код, опрос, QR), выход, список устройств и отзыв
- [ ] Поиск, страницы альбома и исполнителя, ✓ по `/api/library/keys`, скачивание (трек / альбом / всё / выбранное),
      по ссылке, сразу в плейлист, ход загрузок (`/api/activity`)
- [ ] Медиатека: страницы, поиск, жанры, сортировки, исполнители, альбомы, «Слушать»/«Перемешать», удаление, перекачка
- [ ] Плеер: очередь, обновление истёкших ссылок, нормализация по `gain`/`peak`, синхронный текст, «Найти заново»
- [ ] «Моя Волна» (поток, что в эфире, подбор), интернет-радио
- [ ] Плейлисты: создание (в т. ч. из поиска), правка, сортировка, перестановка, совместный доступ, вступление,
      участники, «Сохранить в медиатеку»
- [ ] Загрузка своих файлов с ответом на `need_info`, импорт TXT/CSV с паузой и списком ненайденного
- [ ] Настройки через `/api/settings` с теми же ключами (`player`, `eq.*`)
- [ ] Для администратора — состояние сервера, пользователи, удаление станций

## Для разработчиков сервера

Добавили или изменили эндпоинт — опишите его в `tools/api/openapi_gen.py` и перегенерируйте файл:

```bash
.venv/Scripts/python.exe tools/api/openapi_gen.py
```

`tests/test_openapi.py` падает, если маршрут сервера не описан или `openapi.json` устарел.
