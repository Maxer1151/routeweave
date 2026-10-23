#!/usr/bin/env bash
# NFQUEUE-правило для nfqws: первые пакеты исходящих TCP 80/443 через WAN.
# nfqws сам читает SNI/Host и десинким только домены из hostlist,
# остальной трафик проходит без изменений.
# Использование: fw-nfq.sh start|stop
set -euo pipefail

# shellcheck disable=SC1091
[ -f /etc/routeweave/zapret.conf ] && . /etc/routeweave/zapret.conf
QNUM="${QNUM:-200}"
WAN_IFACE="${WAN_IFACE:-$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')}"
SRC_HOSTLIST="${SRC_HOSTLIST:-/etc/routeweave/lists/dpi.txt}"
HOSTLIST="${HOSTLIST:-/run/routeweave/dpi-zapret.txt}"

RULE=(-p tcp -m multiport --dports 80,443
      -m connbytes --connbytes 1:10 --connbytes-mode packets --connbytes-dir original
      -j NFQUEUE --queue-num "$QNUM" --queue-bypass)

case "${1:-}" in
  start)
    [ -n "$WAN_IFACE" ] || { echo "WAN_IFACE not detected" >&2; exit 1; }
    install -d -m 0755 "$(dirname "$HOSTLIST")"
    if [ -f "$SRC_HOSTLIST" ] && [ "$HOSTLIST" != "$SRC_HOSTLIST" ]; then
      awk '
        /^[[:space:]]*(#|$)/ { next }
        {
          gsub(/^[[:space:]]+|[[:space:]]+$/, "")
          sub(/^\+\./, "")
          sub(/^\./, "")
          if ($0 != "") print
        }
      ' "$SRC_HOSTLIST" > "$HOSTLIST"
    fi
    iptables -t mangle -C POSTROUTING -o "$WAN_IFACE" "${RULE[@]}" 2>/dev/null || \
      iptables -t mangle -I POSTROUTING 1 -o "$WAN_IFACE" "${RULE[@]}"
    ;;
  stop)
    iptables -t mangle -D POSTROUTING -o "$WAN_IFACE" "${RULE[@]}" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 1
    ;;
esac
