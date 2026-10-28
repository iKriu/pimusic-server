# Шрифты

- `MaterialSymbolsRounded.woff2` — **подмножество** Material Symbols Rounded (variable: FILL, GRAD, opsz, wght;
  лицензия Apache 2.0 — `LICENSE-MaterialSymbols.txt`) только с иконками, которые используются в `static/js`.
  Иконки работают без сети (Windows-оболочка с локальным интерфейсом, офлайн PWA).
- **Добавили новую иконку — пересоберите подмножество**, иначе она покажется текстом/запасным символом:
  ```
  python -m venv /tmp/fv && /tmp/fv/bin/pip install fonttools brotli   # отдельно от .venv проекта
  /tmp/fv/bin/python -X utf8 tools/dev/subset_icons.py путь/к/MaterialSymbolsRounded[FILL,GRAD,opsz,wght].woff2
  ```
  Полный шрифт — github.com/google/material-design-icons, `variablefont/`. Скрипт сам находит имена иконок в коде
  и падает с кодом 1, если какой-то иконки нет в шрифте (опечатка) — это и есть проверка.
- Roboto Flex грузится с Google Fonts; без сети — системный шрифт (`system-ui`, Segoe UI, Noto Sans…).
