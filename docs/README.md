# Документация

- [`api/README.md`](api/README.md) — **API для своего клиента**: вход, подписанные ссылки, сценарии, ошибки, лимиты,
  совместимость. Справочник эндпоинтов — `bot/app/openapi.json` (OpenAPI 3.1, сервер отдаёт его по `/api/openapi.json`;
  генерирует `tools/api/openapi_gen.py`, новый эндпоинт без описания там роняет `tests/test_openapi.py`).
- `design/` — дизайн-контракт Mini App ([`DESIGN.md`](design/DESIGN.md), Material 3 Expressive), контракты API
  ([`PLAYER_API.md`](design/PLAYER_API.md), [`APPS_API.md`](design/APPS_API.md)), нативный Android-клиент
  ([`ANDROID_NATIVE.md`](design/ANDROID_NATIVE.md)), скриншоты интерфейса (`showcase-*.png`).

Остальное: [`../apps/README.md`](../apps/README.md) — приложения Windows/Android и их сборка,
[`../deploy/`](../deploy) — пример обратного прокси, резервные копии и уборка (systemd-таймеры).
