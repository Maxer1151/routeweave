#!/usr/bin/env bash
# NFQUEUE-правило для nfqws: первые пакеты исходящих TCP 80/443 через WAN.
# nfqws сам читает SNI/Host и десинкает только домены из hostlist,
# остальной трафик проходит без изменений.
# Использование: fw-nfq.sh start|stop
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# shellcheck disable=SC1091
if [ -f "$ETC/zapret.conf" ]; then . "$ETC/zapret.conf"; fi

warn() { echo "warn: $*" >&2; }

QNUM="${QNUM:-200}"
# || true обязателен: без маршрута ip route get падает, а с pipefail+set -e
# упал бы весь скрипт ещё до проверки WAN_IFACE (stop должен быть безопасным).
WAN_IFACE="${WAN_IFACE:-$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true)}"
SRC_HOSTLIST="${SRC_HOSTLIST:-$ETC/lists/dpi.txt}"
HOSTLIST="${HOSTLIST:-/run/routeweave/dpi-zapret.txt}"

# Правило заворачивает в NFQUEUE только начало соединения:
#  - TCP 80/443 — то, что nfqws умеет разбирать (HTTP Host / TLS SNI);
#  - первые 1..10 пакетов original-направления: десинк нужен только на
#    старте, а гонять через userspace весь поток дорого.
RULE=(
  -p tcp
  -m multiport --dports "80,443"
  -m connbytes --connbytes 1:10 --connbytes-mode packets --connbytes-dir original
  -j NFQUEUE --queue-num "$QNUM" --queue-bypass
)

# Сверяем --qnum запущенного nfqws с QNUM. Если они разошлись (поправили
# zapret.conf, но не перезапустили nfqws), пакеты уходят в очередь, которую
# никто не читает: DPI-десинк молча не работает, и это трудно заметить.
# nfqws может быть ещё не запущен (юнит стартует раньше) — тогда тихо.
check_nfqws_qnum() {
  local pid exe line qnum
  # pgrep ищем по имени, но cmdline читаем только у процессов, чей exe и есть
  # nfqws: под шаблон иначе попадут шеллы, в тексте которых есть слово nfqws.
  while read -r pid; do
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null)" || continue
    [ "${exe##*/}" = "nfqws" ] || continue
    line="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || continue
    qnum="$(printf '%s\n' "$line" | awk '
      { for (i = 1; i <= NF; i++) {
          if ($i ~ /^--qnum=/) { sub(/^--qnum=/, "", $i); print $i; exit }
          if ($i == "--qnum") { print $(i + 1); exit }
      } }')"
    [ -n "$qnum" ] || continue
    [ "$qnum" = "$QNUM" ] || \
      warn "nfqws слушает очередь $qnum, а правило шлёт в $QNUM — перезапустите zapret-nfqws"
  done < <(pgrep -f nfqws 2>/dev/null || true)
}

start() {
  [ -n "$WAN_IFACE" ] || { echo "WAN_IFACE not detected" >&2; exit 1; }
  check_nfqws_qnum

  # hostlist для nfqws: убираем комментарии/пробелы и префиксы "+."/".".
  # Пишем во временный файл и переименовываем: nfqws читает список в любой
  # момент, полузаписанный файл дал бы пропуски в десинке.
  install -d -m 0755 "$(dirname "$HOSTLIST")"
  if [ -f "$SRC_HOSTLIST" ] && [ "$HOSTLIST" != "$SRC_HOSTLIST" ]; then
    local tmp
    tmp="$(mktemp "${HOSTLIST}.XXXXXX")"
    if ! awk '
      /^[[:space:]]*(#|$)/ { next }
      {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "")
        sub(/^\+\./, "")
        sub(/^\./, "")
        if ($0 != "") print
      }
    ' "$SRC_HOSTLIST" > "$tmp"; then
      rm -f "$tmp"
      exit 1
    fi
    chmod 0644 "$tmp"
    mv -f "$tmp" "$HOSTLIST"
  fi

  iptables -t mangle -C POSTROUTING -o "$WAN_IFACE" "${RULE[@]}" 2>/dev/null || \
    iptables -t mangle -I POSTROUTING 1 -o "$WAN_IFACE" "${RULE[@]}"
}

stop() {
  # Без WAN_IFACE правило не построить (интерфейс исчез/переименован) — не
  # падаем. Заодно снимаем возможные дубли от старых запусков.
  if [ -n "$WAN_IFACE" ]; then
    while iptables -t mangle -D POSTROUTING -o "$WAN_IFACE" "${RULE[@]}" 2>/dev/null; do :; done || true
  fi
  echo "ok: nfq rule stopped"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 1
    ;;
esac
