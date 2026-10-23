#!/usr/bin/env bash
# Базовый firewall шлюза: ip_forward, QUIC-drop, FORWARD-правила
# (важно: docker ставит policy DROP на FORWARD и убивает forwarding локалки,
#  поэтому свои ACCEPT-правила добавляем в начало цепочки).
# Использование: fw-base.sh start|stop
set -euo pipefail

# shellcheck disable=SC1091
[ -f /etc/routeweave/.env ] && . /etc/routeweave/.env
QUIC_DROP="${QUIC_DROP:-yes}"
WDTT_SUBNET="10.66.66.0/24"
LAN_IFACE="${LAN_IFACE:-$(ip route get 192.168.0.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')}"
TUN_IFACE="${TUN_IFACE:-Meta}"

case "${1:-}" in
  start)
    sysctl -qw net.ipv4.ip_forward=1

    # Трафик к клиентам WDTT (wdtt0) должен идти в main: policy-routing mihomo
    # (table 2022) иначе уводит forwarded-пакеты обратно в TUN Meta.
    if [ -d /etc/wdtt ] || ip link show wdtt0 >/dev/null 2>&1; then
      ip rule show | grep -q '8990:.*10\.66\.66\.0/24.*lookup main' || \
        ip rule add to "$WDTT_SUBNET" lookup main priority 8990 2>/dev/null || true
    fi

    # локалка -> локалка (клиенты ходят через сервер как шлюз)
    if [ -n "$LAN_IFACE" ]; then
      iptables -C FORWARD -i "$LAN_IFACE" -o "$LAN_IFACE" -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD 1 -i "$LAN_IFACE" -o "$LAN_IFACE" -j ACCEPT
      # WireGuard-клиенты (WDTT) получают ту же политику
      iptables -C FORWARD -i "wg+" -o "$LAN_IFACE" -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD 2 -i "wg+" -o "$LAN_IFACE" -j ACCEPT

      # Docker ставит policy DROP на FORWARD, и без этих правил клиенты LAN
      # не ходят в интернет через mihomo: forward в TUN Meta и обратно режется.
      iptables -C FORWARD -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD 1 -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT
      iptables -C FORWARD -i "$TUN_IFACE" -o "$LAN_IFACE" \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD 1 -i "$TUN_IFACE" -o "$LAN_IFACE" \
          -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

      # то же для WireGuard/WDTT-клиентов (wg+ -> TUN Meta)
      iptables -C FORWARD -i "wg+" -o "$TUN_IFACE" -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD 1 -i "wg+" -o "$TUN_IFACE" -j ACCEPT
      iptables -C FORWARD -i "$TUN_IFACE" -o "wg+" \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
        iptables -I FORWARD 1 -i "$TUN_IFACE" -o "wg+" \
          -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    fi

    # QUIC (UDP/443) режем, чтобы всё жило в TCP и попадало в политику/zapret
    if [ "$QUIC_DROP" = "yes" ]; then
      iptables -C FORWARD -p udp --dport 443 -j DROP 2>/dev/null || \
        iptables -I FORWARD 3 -p udp --dport 443 -j DROP
    fi
    ;;
  stop)
    if [ -n "$LAN_IFACE" ]; then
      iptables -D FORWARD -i "$LAN_IFACE" -o "$LAN_IFACE" -j ACCEPT 2>/dev/null || true
      iptables -D FORWARD -i "wg+" -o "$LAN_IFACE" -j ACCEPT 2>/dev/null || true
      iptables -D FORWARD -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT 2>/dev/null || true
      iptables -D FORWARD -i "$TUN_IFACE" -o "$LAN_IFACE" \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
      iptables -D FORWARD -i "wg+" -o "$TUN_IFACE" -j ACCEPT 2>/dev/null || true
      iptables -D FORWARD -i "$TUN_IFACE" -o "wg+" \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
    fi
    iptables -D FORWARD -p udp --dport 443 -j DROP 2>/dev/null || true
    ip rule del to "$WDTT_SUBNET" lookup main priority 8990 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 1
    ;;
esac
