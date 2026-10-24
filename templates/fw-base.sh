#!/usr/bin/env bash
# Базовый firewall шлюза: ip_forward, QUIC-drop, forwarding LAN/VPN.
#
# Все правила живут в собственной цепочке ROUTEWEAVE-FWD, а из FORWARD в неё
# ведёт ровно один прыжок. Своя цепочка нужна, чтобы не воевать за позиции в
# FORWARD с docker/ufw: раньше правила вставлялись по номерам (-I 1/2/3), и
# QUIC_DROP оказывался ниже ACCEPT, из-за чего UDP/443 у VPN-клиентов не
# резался. Порядок правил внутри цепочки:
#   1) ответный трафик из TUN/VPN в LAN (RELATED,ESTABLISHED);
#   2) DROP UDP/443 (QUIC) — без -i/-o, чтобы резать и LAN, и VPN;
#   3) ACCEPT LAN<->TUN, VPN<->TUN, LAN<->LAN, VPN->LAN.
# DROP стоит выше ACCEPT намеренно: iptables останавливается на первом
# совпавшем правиле, а не выбирает самое строгое.
# Использование: fw-base.sh start|stop
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# shellcheck disable=SC1091
if [ -f "$ETC/.env" ]; then . "$ETC/.env"; fi

warn() { echo "warn: $*" >&2; }

FWD_CHAIN="ROUTEWEAVE-FWD"

# QUIC_DROP нормализуем сами: в .env его пишут руками, и "Yes", " true" или
# опечатка не должны молча оставлять QUIC разрешённым. Мусор -> yes (безопасный
# дефолт: браузеры откатятся на TCP и попадут в политику mihomo/zapret).
QUIC_DROP="${QUIC_DROP:-yes}"
QUIC_DROP="${QUIC_DROP,,}"
case "$QUIC_DROP" in
  yes | no) ;;
  *)
    warn "QUIC_DROP='${QUIC_DROP}' не yes/no, считаю yes"
    QUIC_DROP="yes"
    ;;
esac

# автодетект может не найти маршрут (нет LAN_IFACE в .env) — || true, иначе
# pipefail+set -e уронят скрипт ещё до проверок
LAN_IFACE="${LAN_IFACE:-$(ip route get 192.168.0.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true)}"
TUN_IFACE="${TUN_IFACE:-Meta}"
WG_SUBNET="${WG_SUBNET:-10.66.66.0/24}"

# Список VPN-интерфейсов собираем динамически: жёсткий "wg+" не матчит wdtt0
# (это не wg-интерфейс по имени), а имена wg* зависят от установки.
VPN_IFACES=()
add_vpn() {
  local if="${1%%@*}"
  [ -n "$if" ] || return 0
  case " ${VPN_IFACES[*]-} " in *" $if "*) return 0 ;; esac
  VPN_IFACES+=("$if")
}
# Базовые имена добавляем всегда: правило по имени начнёт работать само, как
# только интерфейс появится, перезапускать firewall не придётся.
add_vpn wdtt0
add_vpn tailscale0
# Существующие wg* берём из ip -o link (там имя вида wg0@NONE — суффикс режем).
while read -r if; do
  ip link show "${if%%@*}" >/dev/null 2>&1 && add_vpn "$if"
done < <(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^wg' || true)

start() {
  sysctl -qw net.ipv4.ip_forward=1

  # Цепочку создаём идемпотентно и сразу чистим: start зовут повторно
  # (systemd restart, повторный запуск установщика), дубли не нужны.
  iptables -nL "$FWD_CHAIN" >/dev/null 2>&1 || iptables -N "$FWD_CHAIN"
  iptables -F "$FWD_CHAIN"

  # Снимаем все старые прыжки (в т.ч. оставшиеся от прежних версий) и ставим
  # ровно один: иначе пакет проходит цепочку несколько раз.
  while iptables -D FORWARD -j "$FWD_CHAIN" 2>/dev/null; do :; done
  iptables -I FORWARD 1 -j "$FWD_CHAIN"

  # 1. Ответный трафик из TUN/VPN в LAN — раньше всех, чтобы не зависеть от
  #    последующих DROP/ACCEPT правил.
  if [ -n "$LAN_IFACE" ]; then
    for if in "$TUN_IFACE" ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
      iptables -A "$FWD_CHAIN" -i "$if" -o "$LAN_IFACE" \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    done
  fi

  # 2. QUIC (UDP/443) режем до всех ACCEPT — иначе клиенты продолжат ходить по
  #    QUIC мимо TCP-политики mihomo и мимо zapret. Без -i/-o: и LAN, и VPN.
  if [ "$QUIC_DROP" = "yes" ]; then
    iptables -A "$FWD_CHAIN" -p udp --dport 443 -j DROP
  fi

  if [ -n "$LAN_IFACE" ]; then
    # 3. LAN -> TUN: интернет через mihomo.
    iptables -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT
  fi

  # 4. VPN -> TUN: та же политика, что у LAN.
  for if in ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
    iptables -A "$FWD_CHAIN" -i "$if" -o "$TUN_IFACE" -j ACCEPT
  done

  if [ -n "$LAN_IFACE" ]; then
    # 5. LAN <-> LAN: клиенты ходят через шлюз друг к другу.
    iptables -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$LAN_IFACE" -j ACCEPT

    # 6. VPN -> LAN: клиенты WDTT видят домашнюю сеть.
    for if in ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
      iptables -A "$FWD_CHAIN" -i "$if" -o "$LAN_IFACE" -j ACCEPT
    done
  fi

  # Клиенты WDTT должны ходить в main: policy-routing mihomo (table 2022)
  # иначе заворачивает forwarded-пакеты обратно в TUN Meta. Правила 8990
  # сносим все: при переустановке WG_SUBNET может измениться, а ip rule не
  # умеет заменять по priority — иначе останется мусорное правило на старую
  # подсеть (и пакеты уйдут не туда).
  if [ -n "$WG_SUBNET" ] && { [ -d /etc/wdtt ] || ip link show wdtt0 >/dev/null 2>&1; }; then
    while ip rule del priority 8990 2>/dev/null; do :; done
    ip rule add to "$WG_SUBNET" lookup main priority 8990
  fi

  echo "ok: firewall started (QUIC_DROP=$QUIC_DROP, LAN=$LAN_IFACE, VPN=${VPN_IFACES[*]-нет})"
}

stop() {
  # Прыжков может быть несколько (наследие старых версий) — снимаем все.
  while iptables -D FORWARD -j "$FWD_CHAIN" 2>/dev/null; do :; done
  # Цепочки может уже не быть (stop дважды подряд) — это не ошибка.
  iptables -F "$FWD_CHAIN" 2>/dev/null || true
  iptables -X "$FWD_CHAIN" 2>/dev/null || true
  # 8990 — только наши правила; сносим по priority, не завязываясь на подсеть.
  while ip rule del priority 8990 2>/dev/null; do :; done

  echo "ok: firewall stopped"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 1
    ;;
esac
