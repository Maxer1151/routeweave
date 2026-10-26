#!/usr/bin/env bash
# Базовый firewall шлюза: ip_forward, QUIC-drop, forwarding LAN/VPN/Tailscale.
#
# Все правила живут в собственной цепочке ROUTEWEAVE-FWD, а из FORWARD в неё
# ведёт ровно один прыжок. Своя цепочка нужна, чтобы не воевать за позиции в
# FORWARD с docker/ufw: раньше правила вставлялись по номерам (-I 1/2/3), и
# QUIC_DROP оказывался ниже ACCEPT, из-за чего UDP/443 у VPN-клиентов не
# резался. Порядок правил внутри цепочки:
#   1) ответный трафик из TUN/VPN в LAN и из TUN в tailnet
#      (RELATED,ESTABLISHED);
#   2) DROP UDP/443 (QUIC) — без -i/-o, чтобы резать и LAN, и VPN;
#   3) DROP пакетов с src из TS_SUBNET, пришедших не из tailscale0
#      (CGNAT-спуф-гард: в netfilter-mode=nodivert tailscale свои drop-правила
#      не ставит);
#   4) ACCEPT LAN<->TUN, VPN<->TUN, tailnet<->tailnet, LAN->tailnet,
#      LAN<->LAN, VPN->LAN.
# DROP стоит выше ACCEPT намеренно: iptables останавливается на первом
# совпавшем правиле, а не выбирает самое строгое.
#
# Tailnet-адреса (TS_SUBNET) уходят в main правилом priority 8991: иначе
# policy-routing mihomo (table 2022) заворачивает ответы exit-node-клиентам
# обратно в TUN. 8991 свободен: 8990 — WDTT, 8993 — WAN, 9001/9002 — mihomo.
#
# Exit node работает в одном из двух режимов (TS_EXIT_MODE):
#   policy (дефолт) — forwarded-трафик из tailscale0 уходит в TUN Meta
#     (table 2022) и получает ту же политику mihomo, что LAN и WDTT:
#     geo -> SOCKS, dpi -> zapret, RU -> direct;
#   nat — трафик из tailscale0 метится в mangle PREROUTING меткой 0x100000
#     и по правилу priority 8992 уходит в main (WAN), затем маскарадится в
#     LAN_IFACE. Быстрее (нет userspace-стека mihomo), но geo/SOCKS/DPI-
#     политика для tailnet не применяется, а источник в LAN маскарадится
#     (LAN видит адрес шлюза). Правила policy-режима остаются на месте —
#     в NAT они безвредны.
# Метка 0x100000 свободна: 0x40000 — tailscale subnet-route/WAN_INGRESS,
# 0x80000 — zapret, 0x200000 — рекомендуемая замена WAN_INGRESS при
# Tailscale. Приоритет 8992 свободен: 8990 — WDTT, 8991 — tailnet,
# 8993 — WAN, 9001/9002 — mihomo.
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
# Вход Tailscale: сервер работает exit node, forwarded-трафик из tailscale0
# получает ту же политику, что LAN и wdtt0.
TS_IFACE="${TS_IFACE:-tailscale0}"
TS_SUBNET="${TS_SUBNET:-100.64.0.0/10}"

# Режим exit node нормализуем сами: в .env его пишут руками, и "Nat" или
# опечатка не должны молча оставить другой режим. Мусор -> policy: это
# историческое поведение, где tailnet получает политику mihomo.
TS_EXIT_MODE="${TS_EXIT_MODE:-policy}"
TS_EXIT_MODE="${TS_EXIT_MODE,,}"
case "$TS_EXIT_MODE" in
  policy | nat) ;;
  *)
    warn "TS_EXIT_MODE='${TS_EXIT_MODE}' не policy/nat, считаю policy"
    TS_EXIT_MODE="policy"
    ;;
esac

# Артефакты NAT-режима. Метка и приоритет не настраиваются: 0x100000 и 8992
# не заняты другими компонентами (см. шапку), а смена метки ломала бы
# диагностику fw-tailscale.sh.
TS_NAT_MARK="0x100000"
TS_NAT_RULE_PRIO=8992
TS_MARK_CHAIN="ROUTEWEAVE-TSMARK"
TS_NAT_CHAIN="ROUTEWEAVE-TSNAT"

# Список VPN-интерфейсов собираем динамически: жёсткий "wg+" не матчит wdtt0
# (это не wg-интерфейс по имени), а имена wg* зависят от установки.
VPN_IFACES=()
add_vpn() {
  local if="${1%%@*}"
  [ -n "$if" ] || return 0
  case " ${VPN_IFACES[*]-} " in *" $if "*) return 0 ;; esac
  VPN_IFACES+=("$if")
}
# Базовые имена (wdtt0 и TS_IFACE) добавляем всегда: правило по имени начнёт
# работать само, как только интерфейс появится, перезапускать firewall не
# придётся.
add_vpn wdtt0
add_vpn "$TS_IFACE"
# Существующие wg* берём из ip -o link (там имя вида wg0@NONE — суффикс режем).
while read -r if; do
  ip link show "${if%%@*}" >/dev/null 2>&1 && add_vpn "$if"
done < <(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^wg' || true)

# ts_nat_cleanup — идемпотентно снимает всё, что создаёт NAT-режим: прыжки,
# цепочки и правило 8992. Вызывается в начале start (переключение режимов и
# повторный запуск не должны оставлять дублей) и в stop. Ошибки глушим: при
# первом запуске цепочек/правил ещё нет, и это не должно валить скрипт.
ts_nat_cleanup() {
  while iptables -t mangle -D PREROUTING -j "$TS_MARK_CHAIN" 2>/dev/null; do :; done
  while iptables -t nat -D POSTROUTING -j "$TS_NAT_CHAIN" 2>/dev/null; do :; done
  iptables -t mangle -F "$TS_MARK_CHAIN" 2>/dev/null || true
  iptables -t mangle -X "$TS_MARK_CHAIN" 2>/dev/null || true
  iptables -t nat -F "$TS_NAT_CHAIN" 2>/dev/null || true
  iptables -t nat -X "$TS_NAT_CHAIN" 2>/dev/null || true
  # 8992 — только наше правило; сносим по priority, не завязываясь на метку.
  while ip rule del priority "$TS_NAT_RULE_PRIO" 2>/dev/null; do :; done
}

start() {
  sysctl -qw net.ipv4.ip_forward=1

  # Сначала всегда снимаем артефакты NAT-режима: start зовут повторно, а
  # режим могли переключить правкой .env + restart.
  ts_nat_cleanup

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

  # 1a. Возвратный путь exit node: mihomo пишет ответ в TUN Meta, и пакет
  #     должен уйти в tailnet. Без этого правила ufw с FORWARD policy DROP
  #     съедает ответ, и соединение exit-node-клиента не устанавливается.
  if [ -n "$TUN_IFACE" ] && [ -n "$TS_IFACE" ]; then
    iptables -A "$FWD_CHAIN" -i "$TUN_IFACE" -o "$TS_IFACE" \
      -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  fi

  # 2. QUIC (UDP/443) режем до всех ACCEPT — иначе клиенты продолжат ходить по
  #    QUIC мимо TCP-политики mihomo и мимо zapret. Без -i/-o: и LAN, и VPN.
  if [ "$QUIC_DROP" = "yes" ]; then
    iptables -A "$FWD_CHAIN" -p udp --dport 443 -j DROP
  fi

  # 3. CGNAT-спуф-гард: tailscale в netfilter-mode=nodivert свои CGNAT-drop
  #    правила не применяет, поэтому закрываем спуфинг сами. Пакет с
  #    tailnet-источником, пришедший не из tailscale0, — подделка.
  if [ -n "$TS_IFACE" ] && [ -n "$TS_SUBNET" ]; then
    iptables -A "$FWD_CHAIN" ! -i "$TS_IFACE" -s "$TS_SUBNET" -j DROP
  fi

  if [ -n "$LAN_IFACE" ]; then
    # 4. LAN -> TUN: интернет через mihomo.
    iptables -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT
  fi

  # 5. VPN -> TUN: та же политика, что у LAN.
  for if in ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
    iptables -A "$FWD_CHAIN" -i "$if" -o "$TUN_IFACE" -j ACCEPT
  done

  if [ -n "$LAN_IFACE" ]; then
    # 6. LAN <-> LAN: клиенты ходят через шлюз друг к другу.
    iptables -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$LAN_IFACE" -j ACCEPT

    # 7. VPN -> LAN: клиенты WDTT видят домашнюю сеть.
    for if in ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
      iptables -A "$FWD_CHAIN" -i "$if" -o "$LAN_IFACE" -j ACCEPT
    done
  fi

  # 8. Tailnet: клиенты Tailscale ходят друг к другу и в LAN. Обратный путь
  #    tailnet -> LAN уже разрешён выше циклом по VPN_IFACES.
  if [ -n "$TS_IFACE" ]; then
    iptables -A "$FWD_CHAIN" -i "$TS_IFACE" -o "$TS_IFACE" -j ACCEPT
    if [ -n "$LAN_IFACE" ]; then
      iptables -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$TS_IFACE" -j ACCEPT
    fi
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

  # Tailnet-адреса должны резолвиться через main: иначе mihomo (table 2022)
  # уводит пакеты к 100.64.0.0/10 в TUN, и ответы exit-node-клиентам теряются.
  # 8991 свободен: 8990 — WDTT, 8993 — WAN, 9001/9002 — mihomo.
  # Блок безусловный (не зависит от наличия tailscale): правило по подсети
  # безопасно и без установленного клиента, а после его установки заработает
  # без перезапуска firewall.
  if [ -n "$TS_SUBNET" ]; then
    while ip rule del priority 8991 2>/dev/null; do :; done
    ip rule add to "$TS_SUBNET" lookup main priority 8991
  fi

  # NAT-режим exit node: без LAN_IFACE/TS_IFACE/TS_SUBNET он не имеет смысла
  # (нечего метить и некуда маскарадить). Не отказываем — warn и policy:
  # шлюз продолжает работать, а не падает из-за одной переменной в .env.
  if [ "$TS_EXIT_MODE" = "nat" ] \
    && { [ -z "$LAN_IFACE" ] || [ -z "$TS_IFACE" ] || [ -z "$TS_SUBNET" ]; }; then
    warn "TS_EXIT_MODE=nat, но LAN_IFACE='$LAN_IFACE'/TS_IFACE='$TS_IFACE'/TS_SUBNET='$TS_SUBNET' пусты — работаю как policy"
    TS_EXIT_MODE="policy"
  fi

  if [ "$TS_EXIT_MODE" = "nat" ]; then
    # 1. Метка на всём, что пришло из tailscale0. Метка ставится в PREROUTING
    #    до выбора таблицы маршрутизации, поэтому правило 8992 ниже успевает
    #    увести пакет в main.
    iptables -t mangle -N "$TS_MARK_CHAIN"
    iptables -t mangle -A "$TS_MARK_CHAIN" -i "$TS_IFACE" \
      -j MARK --set-xmark "$TS_NAT_MARK/$TS_NAT_MARK"
    iptables -t mangle -I PREROUTING 1 -j "$TS_MARK_CHAIN"

    # 2. MASQUERADE в WAN. Под -s TS_SUBNET попадает и tailnet -> LAN: LAN
    #    увидит адрес шлюза, а не 100.x клиента. Это осознанно: обратный
    #    маршрут к CGNAT из LAN всё равно был бы битым.
    iptables -t nat -N "$TS_NAT_CHAIN"
    iptables -t nat -A "$TS_NAT_CHAIN" -s "$TS_SUBNET" -o "$LAN_IFACE" \
      -m mark --mark "$TS_NAT_MARK/$TS_NAT_MARK" -j MASQUERADE
    iptables -t nat -I POSTROUTING 1 -j "$TS_NAT_CHAIN"

    # 3. Помеченные пакеты — в main (WAN), мимо table 2022. Приоритет 8992
    #    ниже 8991, поэтому адреса tailnet по-прежнему резолвятся в main
    #    напрямую, а не через это правило.
    ip rule add fwmark "$TS_NAT_MARK/$TS_NAT_MARK" lookup main priority "$TS_NAT_RULE_PRIO"
  fi

  echo "ok: firewall started (QUIC_DROP=$QUIC_DROP, LAN=$LAN_IFACE, VPN=${VPN_IFACES[*]-нет}, TS=$TS_IFACE, TS_MODE=$TS_EXIT_MODE)"
}

stop() {
  # NAT-режим: снимаем прыжки/цепочки/8992, даже если режим уже переключён
  # на policy — иначе останется мусор от прошлой конфигурации.
  ts_nat_cleanup
  # Прыжков может быть несколько (наследие старых версий) — снимаем все.
  while iptables -D FORWARD -j "$FWD_CHAIN" 2>/dev/null; do :; done
  # Цепочки может уже не быть (stop дважды подряд) — это не ошибка.
  iptables -F "$FWD_CHAIN" 2>/dev/null || true
  iptables -X "$FWD_CHAIN" 2>/dev/null || true
  # 8990 — только наши правила; сносим по priority, не завязываясь на подсеть.
  while ip rule del priority 8990 2>/dev/null; do :; done
  # 8991 — tailnet; тоже только наше правило.
  while ip rule del priority 8991 2>/dev/null; do :; done

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
