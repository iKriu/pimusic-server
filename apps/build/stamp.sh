#!/bin/bash
# На ПК (Git Bash), перед заливкой apps/ на сервер:
#  * build/source.env — коммит, SOURCE_DATE_EPOCH (время последнего коммита apps/ или bot/app/static) и версия
#    встроенного интерфейса WEBUI_COMMIT (git-дерево bot/app/static); build-*.sh передают их в контейнер сборки;
#  * apps/webui/ — копия bot/app/static (интерфейс Windows 2.0 собирается из неё на сервере; в .gitignore).
set -euo pipefail
cd "$(dirname "$0")/.."
COMMIT=$(git rev-parse HEAD)
EPOCH=$(git log -1 --format=%ct -- . ../bot/app/static)
if [ -n "$(git status --porcelain -- .)" ]; then
  COMMIT="$COMMIT-dirty"
  echo "!! в apps/ есть незакоммиченные изменения — сборка не будет соответствовать коммиту" >&2
fi
WEBUI=$(git rev-parse --short=12 HEAD:bot/app/static)
if [ -n "$(git status --porcelain -- ../bot/app/static)" ]; then
  WEBUI="$WEBUI-dirty"
  echo "!! в bot/app/static есть незакоммиченные изменения — интерфейс не будет соответствовать коммиту" >&2
fi
printf 'SOURCE_COMMIT=%s\nSOURCE_DATE_EPOCH=%s\nWEBUI_COMMIT=%s\n' "$COMMIT" "$EPOCH" "$WEBUI" > build/source.env
rm -rf webui
cp -r ../bot/app/static webui
cat build/source.env
