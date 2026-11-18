#!/usr/bin/env bash
# Routeweave: применить списки и стратегии zapret.
#
# Панель вызывает helper через sudo -n после сохранения списка/профиля:
#   1. routeweave-update-lists.sh — сгенерировать lists/dpi.txt и прочие;
#   2. systemctl restart zapret-nfqws — пересобрать цепочку правил и
#      перезапустить default-процесс nfqws;
#   3. systemctl restart zapret-nfqws@<name> — перечитать профили.
#
# Ошибка одного профиля не мешает остальным; итоговый код 1, чтобы панель
# показала проблему в задании применения.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
UPDATER="${ROUTEWEAVE_UPDATER:-/usr/local/sbin/routeweave-update-lists.sh}"
PROFILE_DIR="$ETC/zapret.d"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
rc=0

if [ -r "$ETC/zapret.conf" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    [[ $line =~ ^[[:space:]]*PROFILE_DIR=(.*)$ ]] || continue
    value=${BASH_REMATCH[1]}
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ $value == \"*\" && ${#value} -ge 2 ]]; then
      value=${value:1:${#value}-2}
    elif [[ $value == \'*\' && ${#value} -ge 2 ]]; then
      value=${value:1:${#value}-2}
    fi
    [ -n "$value" ] && PROFILE_DIR="$value"
  done < "$ETC/zapret.conf"
fi

if ! "$UPDATER"; then
  echo "routeweave-zapret-apply: routeweave-update-lists.sh завершился ошибкой" >&2
  rc=1
fi

if ! "$SYSTEMCTL" restart zapret-nfqws; then
  echo "routeweave-zapret-apply: не удалось перезапустить zapret-nfqws" >&2
  rc=1
fi

if [ -d "$PROFILE_DIR" ]; then
  shopt -s nullglob
  for conf in "$PROFILE_DIR"/*.conf; do
    name=${conf##*/}
    name=${name%.conf}
    if [[ ! $name =~ ^[a-z0-9][a-z0-9-]{0,15}$ ]]; then
      echo "routeweave-zapret-apply: профиль '$name' пропущен (недопустимое имя)" >&2
      rc=1
      continue
    fi
    if ! "$SYSTEMCTL" restart "zapret-nfqws@$name.service"; then
      echo "routeweave-zapret-apply: не удалось перезапустить zapret-nfqws@$name.service" >&2
      rc=1
    fi
  done
fi

exit "$rc"
