#!/usr/bin/env bash
# Routeweave: статус контейнера tg-ws-proxy для веб-панели.
# Печатает одно слово в нижнем регистре: absent | running | exited | created |
# restarting | paused | dead (whitelist в panel/app.py: TGWS_STATES).
# Код возврата 0, когда статус напечатан: docker недоступен или контейнера нет —
# это "absent". Таймаут docker inspect — код 124 и диагностика в stderr:
# панель трактует любой rc != 0 как unknown, а не как остановленный прокси.
set -Eeuo pipefail

CONTAINER="routeweave-tgwsproxy"

if ! command -v docker >/dev/null 2>&1; then
  echo absent
  exit 0
fi

state=""
inspect_rc=0
# timeout 10: зависший docker-демон не должен держать /api/status.
state=$(timeout 10 docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null) || inspect_rc=$?
if [ "$inspect_rc" -eq 124 ]; then
  echo "routeweave-tgws-state: docker inspect не ответил за 10 с" >&2
  exit 124
fi
if [ -z "$state" ]; then
  echo absent
  exit 0
fi

printf '%s\n' "$state" | tr '[:upper:]' '[:lower:]'
