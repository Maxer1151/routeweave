#!/usr/bin/env bash
# Routeweave: перезапуск контейнера tg-ws-proxy.
# Вызывается панелью через sudo -n (allowlist в /etc/sudoers.d/routeweave-panel).
# Аргументы не принимаются: имя контейнера зафиксировано в установщике.
set -Eeuo pipefail

CONTAINER="routeweave-tgwsproxy"

if ! command -v docker >/dev/null 2>&1; then
  echo "routeweave-tgws-restart: docker не найден — tg-ws-proxy не установлен?" >&2
  exit 1
fi

restart_rc=0
# timeout 60: restart не должен висеть дольше таймаута панели.
timeout 60 docker restart "$CONTAINER" || restart_rc=$?
if [ "$restart_rc" -eq 124 ]; then
  echo "routeweave-tgws-restart: docker restart не уложился в 60 с" >&2
  exit 124
fi
exit "$restart_rc"
