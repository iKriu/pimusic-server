# Участие в разработке

Спасибо за интерес к PiMusic! Интерфейс, комментарии в коде и документация — **на русском**.

## Окружение

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q tests
```

- `bot/app/` — бот и API (aiohttp, python-telegram-bot, sqlite); `bot/app/static/` — Mini App/PWA (ES-модули без
  сборки). Приложения — `apps/` (сборка в контейнерах, см. [`apps/README.md`](apps/README.md)).
- Новый эндпоинт API — описать в `tools/api/openapi_gen.py` и перегенерировать `bot/app/openapi.json`
  (`python tools/api/openapi_gen.py`), иначе упадёт `tests/test_openapi.py`.
- Интерфейс Mini App следует дизайн-контракту [`docs/design/DESIGN.md`](docs/design/DESIGN.md) (Material 3 Expressive).

## Правила

- Файлы — UTF-8 без BOM, переводы строк LF (`.gitattributes`).
- Перед pull request: тесты зелёные, новые функции покрыты тестами.
- Никаких секретов в коммитах: `.env`, токены, ключи подписи (`*.jks`, `*-release.properties`, `updater.key*`) —
  только локально. Пример настроек — `.env.example` с пустыми значениями.
- Ошибки и предложения — в Issues; уязвимости — не в открытых Issues, а через приватный отчёт (Security → Report a
  vulnerability).

Отправляя изменения, вы соглашаетесь, что они распространяются под лицензией проекта — [AGPL-3.0](LICENSE).
