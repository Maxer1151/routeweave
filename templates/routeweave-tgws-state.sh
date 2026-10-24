#!/usr/bin/env bash
# Routeweave: статус контейнера tg-ws-proxy для веб-панели.
# Печатает одно слово в нижнем регистре: absent | running | exited | created | ...
# Код возврата 0 всегда, когда статус напечатан: docker недоступен или контейнера
# нет — это "absent" (панель трактует unknown только при rc != 0).
set -Eeuo pipefail

CONTAINER="routeweave-tgwsproxy"

if ! command -v docker >/dev/null 2>&1; then
  echo absent
  exit 0
fi

state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null) || state=""
if [ -z "$state" ]; then
  echo absent
  exit 0
fi

printf '%s\n' "$state" | tr '[:upper:]' '[:lower:]'
