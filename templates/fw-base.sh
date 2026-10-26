#!/usr/bin/env bash
# Базовый firewall шлюза: ip_forward, QUIC-drop, forwarding LAN/VPN/Tailscale,
# вход на сам шлюз (INPUT).
#
# Все правила forwarding живут в собственной цепочке ROUTEWEAVE-FWD, а из
# FORWARD в неё ведёт ровно один прыжок. Своя цепочка нужна, чтобы не воевать
# за позиции в FORWARD с docker/ufw: раньше правила вставлялись по номерам
# (-I 1/2/3), и QUIC_DROP оказывался ниже ACCEPT, из-за чего UDP/443 у
# VPN-клиентов не резался. Прыжок обязан стоять первым в FORWARD (выше
# ufw-before-forward и DOCKER-USER) — это проверяется после вставки и в
# doctor. Порядок правил внутри цепочки:
#   1) ответный трафик из TUN/VPN в LAN и из TUN в tailnet
#      (RELATED,ESTABLISHED);
#   2) DROP INVALID: битые/вне-окна пакеты не должны доходить до ACCEPT.
#      Стоит после ESTABLISHED/RELATED и до всех ACCEPT — так INVALID не
#      отрезает легитимный ответный трафик, но и не обходит фильтрацию;
#   3) DROP UDP/443 (QUIC) — без -i/-o, чтобы резать и LAN, и VPN. Помеченный
#      WAN_INGRESS трафик (опубликованные из WAN порты, см. fw-wan.sh) из
#      DROP исключён: у него своя политика и своя метка;
#   4) DROP пакетов с src из TS_SUBNET, пришедших не из tailscale0
#      (CGNAT-спуф-гард: в netfilter-mode=nodivert tailscale свои drop-правила
#      не ставит);
#   5) ACCEPT LAN<->TUN, VPN<->TUN, tailnet<->tailnet, LAN->tailnet,
#      LAN<->LAN, VPN->LAN.
# DROP стоит выше ACCEPT намеренно: iptables останавливается на первом
# совпавшем правиле, а не выбирает самое строгое.
#
# Вход на сам шлюз (INPUT) — цепочка ROUTEWEAVE-IN, прыжок в INPUT 1. Политику
# INPUT скрипт не меняет (её держат ufw/docker), поэтому ограничения заданы
# явными правилами: lo, ESTABLISHED/RELATED, ICMP, SSH и сервисы шлюза
# (DNS 53, панель PANEL_PORT, API mihomo 9090, прокси 7890) — только из
# доверенных зон (LAN_IFACE, tailscale0, wdtt0, WG_SUBNET, TS_SUBNET,
# 100.64.0.0/10). Для этих портов стоит и явный DROP из остальных источников:
# при INPUT policy ACCEPT (docker) иначе панель и API были бы открыты в WAN.
# Финальный RETURN возвращает пакет в INPUT, чтобы не менять чужую политику.
#
# QUIC_DROP исключает помеченный WAN_INGRESS трафик: WAN_INGRESS_MARK (по
# умолчанию 0x200000, читается из .env) ставит fw-wan.sh на входящие из WAN
# NEW-соединения; без RETURN в ROUTEWEAVE-FWD DROP UDP/443 резал бы
# опубликованный из интернета QUIC-порт.
#
# IPv6: скрипт намеренно управляет только IPv4 и не трогает ip6tables —
# смена политики/правил IPv6 может порвать чужую сеть (docker, tailscale,
# провайдерский RA). Это осознанный отказ, а не недосмотр: `doctor`
# предупреждает, что IPv6 не фильтруется и не идёт через mihomo. Хотите
# фильтровать — настройте ip6tables отдельно или выключите IPv6.
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
# Метка 0x100000 свободна: 0x40000 — tailscale subnet-route, 0x80000 —
# zapret, 0x200000 — дефолт WAN_INGRESS. Приоритет 8992 свободен: 8990 —
# WDTT, 8991 — tailnet, 8993 — WAN, 9001/9002 — mihomo.
# Использование: fw-base.sh start|stop|doctor
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# shellcheck disable=SC1091
if [ -f "$ETC/.env" ]; then . "$ETC/.env"; fi

warn() { echo "warn: $*" >&2; }
die() {
  echo "error: $*" >&2
  exit 1
}

# Цепочки: FWD — forwarded-трафик, IN — вход на сам шлюз.
FWD_CHAIN="ROUTEWEAVE-FWD"
IN_CHAIN="ROUTEWEAVE-IN"

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

# WAN_INGRESS нужен только для исключения помеченного трафика из QUIC-drop.
# Мусор -> no: тогда исключения нет и UDP/443 режется строже — fail closed.
WAN_INGRESS="${WAN_INGRESS:-no}"
WAN_INGRESS="${WAN_INGRESS,,}"
case "$WAN_INGRESS" in
  yes | no) ;;
  *)
    warn "WAN_INGRESS='${WAN_INGRESS}' не yes/no, считаю no"
    WAN_INGRESS="no"
    ;;
esac
# Метка WAN_INGRESS. 0x40000 занят tailscale subnet-route, 0x80000 — zapret,
# 0x100000 — NAT-режим exit node, поэтому дефолт 0x200000. Значение должно
# совпадать с fw-wan.sh: он ставит метку, мы по ней исключаем QUIC.
WAN_INGRESS_MARK="${WAN_INGRESS_MARK:-0x200000}"

# SSH_PORT и PANEL_PORT приходят из .env; опечатка не должна открыть SSH на
# случайный порт или уронить iptables невнятной ошибкой — валидируем явно.
SSH_PORT="${SSH_PORT:-22}"
PANEL_PORT="${PANEL_PORT:-8080}"

valid_port() { # valid_port <имя переменной> <значение>
  local name="$1" value="$2"
  [[ "$value" =~ ^[0-9]{1,5}$ ]] || die "$name='$value' должен быть целым числом 1..65535"
  if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
    die "$name='$value' вне диапазона 1..65535"
  fi
}

valid_port SSH_PORT "$SSH_PORT"
valid_port PANEL_PORT "$PANEL_PORT"

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

# --- Вход на шлюз: доверенные зоны и порты сервисов ------------------------
# Пустые значения не добавляем: -i "" iptables не поймёт. LAN_IFACE может быть
# пуст (не нашли NIC) — тогда остаются tailscale0/wdtt0 и подсети, а DROP для
# сервисных портов всё равно ставится.
IN_IFACES=()
add_in_iface() {
  local iface="${1:-}"
  [ -n "$iface" ] || return 0
  case " ${IN_IFACES[*]-} " in *" $iface "*) return 0 ;; esac
  IN_IFACES+=("$iface")
}
IN_NETS=()
add_in_net() {
  local net="${1:-}"
  [ -n "$net" ] || return 0
  case " ${IN_NETS[*]-} " in *" $net "*) return 0 ;; esac
  IN_NETS+=("$net")
}
add_in_iface "$LAN_IFACE"
add_in_iface "$TS_IFACE"
add_in_iface "wdtt0"
add_in_net "$WG_SUBNET"
add_in_net "$TS_SUBNET"
add_in_net "100.64.0.0/10"

# Порты сервисов: DNS (tcp+udp), панель, API mihomo, mixed-proxy.
# Дубли (PANEL_PORT=9090) убираем, чтобы не плодить одинаковые правила.
IN_TCP_PORTS=()
add_in_tcp() { # add_in_tcp <порт>
  local port="$1"
  case " ${IN_TCP_PORTS[*]-} " in *" $port "*) return 0 ;; esac
  IN_TCP_PORTS+=("$port")
}
add_in_tcp 53
add_in_tcp "$PANEL_PORT"
add_in_tcp 9090
add_in_tcp 7890
IN_UDP_PORTS=(53)

# has_all <текст> <подстрока>... — все подстроки присутствуют.
has_all() {
  local text="$1" needle
  shift
  for needle in "$@"; do
    case "$text" in *"$needle"*) ;; *) return 1 ;; esac
  done
  return 0
}

# ctstates_in_line <строка> <состояние>... — в строке есть --ctstate со всеми
# перечисленными состояниями. Порядок состояний в выводе iptables не фиксирован:
# iptables-nft печатает RELATED,ESTABLISHED там, где iptables-legacy печатал
# ESTABLISHED,RELATED. Сравнение подстрокой из-за этого давало ложный FAIL
# в doctor на современной системе, поэтому список разбирается и сверяется
# поэлементно.
ctstates_in_line() {
  local line="$1" state found item
  shift
  local states
  states="$(printf '%s\n' "$line" \
    | grep -oE '(--ctstate|ctstate)[[:space:]]+[A-Z_,]+' \
    | sed -E 's/^(--ctstate|ctstate)[[:space:]]+//' \
    | tr ',' '\n' || true)"
  for state in "$@"; do
    found=0
    while IFS= read -r item; do
      if [ "$item" = "$state" ]; then
        found=1
        break
      fi
    done <<< "$states"
    [ "$found" -eq 1 ] || return 1
  done
  return 0
}

# has_ctstate_rule <текст> <подстрока той же строки> <состояние>... — правило,
# в котором совпали и подстрока (например "-j ACCEPT"), и набор состояний.
# Проверка идёт по строке, а не по всему выводу: иначе ACCEPT из одного
# правила и INVALID из другого складывались бы в ложное OK.
has_ctstate_rule() {
  local text="$1" needle="$2" line
  shift 2
  while IFS= read -r line; do
    case "$line" in
      *"$needle"*) ;;
      *) continue ;;
    esac
    ctstates_in_line "$line" "$@" && return 0
  done <<< "$text"
  return 1
}

# fwd_jump_on_top — прыжок ROUTEWEAVE-FWD в FORWARD стоит выше чужих прыжков
# (ufw-before-forward/ufw-reject-forward/DOCKER-USER). При -I 1 это всегда так,
# но проверяем: ufw reload и docker могут вставить своё правило в начало уже
# после нас, и тогда QUIC-drop/спуф-гард перестанут применяться.
fwd_jump_on_top() {
  local line
  while IFS= read -r line; do
    case "$line" in
      *"-j $FWD_CHAIN"*) return 0 ;;
      *"-j ufw-before-forward"* | *"-j ufw-reject-forward"* | *"-j DOCKER-USER"*) return 1 ;;
    esac
  done < <(iptables -w 5 -S FORWARD 2>/dev/null || true)
  # Прыжка нет вовсе.
  return 1
}

# ts_nat_cleanup — идемпотентно снимает всё, что создаёт NAT-режим: прыжки,
# цепочки и правило 8992. Вызывается в начале start (переключение режимов и
# повторный запуск не должны оставлять дублей) и в stop. Ошибки глушим: при
# первом запуске цепочек/правил ещё нет, и это не должно валить скрипт.
ts_nat_cleanup() {
  while iptables -w 5 -t mangle -D PREROUTING -j "$TS_MARK_CHAIN" 2>/dev/null; do :; done
  while iptables -w 5 -t nat -D POSTROUTING -j "$TS_NAT_CHAIN" 2>/dev/null; do :; done
  iptables -w 5 -t mangle -F "$TS_MARK_CHAIN" 2>/dev/null || true
  iptables -w 5 -t mangle -X "$TS_MARK_CHAIN" 2>/dev/null || true
  iptables -w 5 -t nat -F "$TS_NAT_CHAIN" 2>/dev/null || true
  iptables -w 5 -t nat -X "$TS_NAT_CHAIN" 2>/dev/null || true
  # 8992 — только наше правило; сносим по priority, не завязываясь на метку.
  while ip rule del priority "$TS_NAT_RULE_PRIO" 2>/dev/null; do :; done
}

start() {
  sysctl -qw net.ipv4.ip_forward=1

  # Сначала всегда снимаем артефакты NAT-режима: start зовут повторно, а
  # режим могли переключить правкой .env + restart.
  ts_nat_cleanup

  # Цепочки создаём идемпотентно и сразу чистим: start зовут повторно
  # (systemd restart, повторный запуск установщика), дубли не нужны.
  iptables -w 5 -nL "$FWD_CHAIN" >/dev/null 2>&1 || iptables -w 5 -N "$FWD_CHAIN" \
    || die "не удалось создать цепочку $FWD_CHAIN"
  iptables -w 5 -F "$FWD_CHAIN" || die "не удалось очистить цепочку $FWD_CHAIN"

  iptables -w 5 -nL "$IN_CHAIN" >/dev/null 2>&1 || iptables -w 5 -N "$IN_CHAIN" \
    || die "не удалось создать цепочку $IN_CHAIN"
  iptables -w 5 -F "$IN_CHAIN" || die "не удалось очистить цепочку $IN_CHAIN"

  # Прыжок из INPUT ставим первым: правила ufw/docker в INPUT не должны
  # оказаться выше наших ограничений (иначе при INPUT policy ACCEPT панель и
  # API останутся открытыми). Политику INPUT не меняем.
  while iptables -w 5 -D INPUT -j "$IN_CHAIN" 2>/dev/null; do :; done
  iptables -w 5 -I INPUT 1 -j "$IN_CHAIN" || die "не удалось вставить прыжок в INPUT"

  # Снимаем все старые прыжки (в т.ч. оставшиеся от прежних версий) и ставим
  # ровно один: иначе пакет проходит цепочку несколько раз.
  while iptables -w 5 -D FORWARD -j "$FWD_CHAIN" 2>/dev/null; do :; done
  iptables -w 5 -I FORWARD 1 -j "$FWD_CHAIN" || die "не удалось вставить прыжок в FORWARD"
  if ! fwd_jump_on_top; then
    # Теоретически невозможно сразу после -I 1, но если между нашими
    # командами кто-то вставил своё правило (ufw reload) — повторяем один раз.
    while iptables -w 5 -D FORWARD -j "$FWD_CHAIN" 2>/dev/null; do :; done
    iptables -w 5 -I FORWARD 1 -j "$FWD_CHAIN" || die "не удалось вставить прыжок в FORWARD"
    fwd_jump_on_top || die "прыжок $FWD_CHAIN не первый в FORWARD (выше него ufw-before-forward/DOCKER-USER) — правила firewall не применяются"
  fi

  # --- ROUTEWEAVE-IN: вход на сам шлюз ---
  # 1. lo — локальные сервисы и панель.
  iptables -w 5 -A "$IN_CHAIN" -i lo -j ACCEPT
  # 2. Ответный трафик: без него ломаются уже установленные соединения
  #    (в т.ч. SSH-сессия, из которой запустили перезапуск firewall).
  iptables -w 5 -A "$IN_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  # 3. ICMP: ping/PMTU. Не ограничиваем echo — диагностика важнее.
  iptables -w 5 -A "$IN_CHAIN" -p icmp -j ACCEPT
  # 4. SSH: единственный сервис, открытый из любой зоны (иначе шлюз не
  #    администрировать). Порт из .env, уже проверен выше.
  iptables -w 5 -A "$IN_CHAIN" -p tcp --dport "$SSH_PORT" -j ACCEPT
  # 5. INVALID — после ответного трафика, до всех ACCEPT.
  iptables -w 5 -A "$IN_CHAIN" -m conntrack --ctstate INVALID -j DROP
  # 6. Антиспуф CGNAT на входе: пакет с tailnet-источником, пришедший не из
  #    tailscale0, — подделка. Стоит до разрешений, чтобы не обойти их.
  if [ -n "$TS_IFACE" ] && [ -n "$TS_SUBNET" ]; then
    iptables -w 5 -A "$IN_CHAIN" ! -i "$TS_IFACE" -s "$TS_SUBNET" -j DROP
  fi

  # 7. Сервисы шлюза — только из доверенных зон. Сначала разрешения, затем
  #    DROP для этих же портов из всех остальных источников.
  local port iface net
  for port in ${IN_TCP_PORTS[@]+"${IN_TCP_PORTS[@]}"}; do
    for iface in ${IN_IFACES[@]+"${IN_IFACES[@]}"}; do
      iptables -w 5 -A "$IN_CHAIN" -i "$iface" -p tcp --dport "$port" -j ACCEPT
    done
    for net in ${IN_NETS[@]+"${IN_NETS[@]}"}; do
      iptables -w 5 -A "$IN_CHAIN" -s "$net" -p tcp --dport "$port" -j ACCEPT
    done
  done
  for port in ${IN_UDP_PORTS[@]+"${IN_UDP_PORTS[@]}"}; do
    for iface in ${IN_IFACES[@]+"${IN_IFACES[@]}"}; do
      iptables -w 5 -A "$IN_CHAIN" -i "$iface" -p udp --dport "$port" -j ACCEPT
    done
    for net in ${IN_NETS[@]+"${IN_NETS[@]}"}; do
      iptables -w 5 -A "$IN_CHAIN" -s "$net" -p udp --dport "$port" -j ACCEPT
    done
  done
  for port in ${IN_TCP_PORTS[@]+"${IN_TCP_PORTS[@]}"}; do
    iptables -w 5 -A "$IN_CHAIN" -p tcp --dport "$port" -j DROP
  done
  for port in ${IN_UDP_PORTS[@]+"${IN_UDP_PORTS[@]}"}; do
    iptables -w 5 -A "$IN_CHAIN" -p udp --dport "$port" -j DROP
  done
  # 8. Остальное — обратно в INPUT: политику INPUT держат ufw/docker, мы её
  #    не переопределяем.
  iptables -w 5 -A "$IN_CHAIN" -j RETURN

  # 1. Ответный трафик из TUN/VPN в LAN — раньше всех, чтобы не зависеть от
  #    последующих DROP/ACCEPT правил.
  if [ -n "$LAN_IFACE" ]; then
    for if in "$TUN_IFACE" ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
      iptables -w 5 -A "$FWD_CHAIN" -i "$if" -o "$LAN_IFACE" \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    done
  fi

  # 1a. Возвратный путь exit node: mihomo пишет ответ в TUN Meta, и пакет
  #     должен уйти в tailnet. Без этого правила ufw с FORWARD policy DROP
  #     съедает ответ, и соединение exit-node-клиента не устанавливается.
  if [ -n "$TUN_IFACE" ] && [ -n "$TS_IFACE" ]; then
    iptables -w 5 -A "$FWD_CHAIN" -i "$TUN_IFACE" -o "$TS_IFACE" \
      -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  fi

  # 1b. INVALID — до всех ACCEPT (после ответного трафика: он не INVALID).
  iptables -w 5 -A "$FWD_CHAIN" -m conntrack --ctstate INVALID -j DROP

  # 2. QUIC (UDP/443) режем до всех ACCEPT — иначе клиенты продолжат ходить по
  #    QUIC мимо TCP-политики mihomo и мимо zapret. Без -i/-o: и LAN, и VPN.
  #    Помеченный WAN_INGRESS трафик исключаем: это опубликованные из WAN
  #    порты, у них своя политика (fw-wan.sh); метка стоит на пакете с NEW.
  if [ "$QUIC_DROP" = "yes" ]; then
    if [ "$WAN_INGRESS" = "yes" ]; then
      [[ "$WAN_INGRESS_MARK" =~ ^0x[0-9a-fA-F]{1,8}$ ]] \
        || die "WAN_INGRESS_MARK='${WAN_INGRESS_MARK}' должен быть 0x + 1..8 hex (как в fw-wan.sh)"
      iptables -w 5 -A "$FWD_CHAIN" \
        -m mark --mark "$WAN_INGRESS_MARK/$WAN_INGRESS_MARK" -j RETURN
    fi
    iptables -w 5 -A "$FWD_CHAIN" -p udp --dport 443 -j DROP
  fi

  # 3. CGNAT-спуф-гард: tailscale в netfilter-mode=nodivert свои CGNAT-drop
  #    правила не применяет, поэтому закрываем спуфинг сами. Пакет с
  #    tailnet-источником, пришедший не из tailscale0, — подделка.
  if [ -n "$TS_IFACE" ] && [ -n "$TS_SUBNET" ]; then
    iptables -w 5 -A "$FWD_CHAIN" ! -i "$TS_IFACE" -s "$TS_SUBNET" -j DROP
  fi

  if [ -n "$LAN_IFACE" ]; then
    # 4. LAN -> TUN: интернет через mihomo.
    iptables -w 5 -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT
  fi

  # 5. VPN -> TUN: та же политика, что у LAN.
  for if in ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
    iptables -w 5 -A "$FWD_CHAIN" -i "$if" -o "$TUN_IFACE" -j ACCEPT
  done

  if [ -n "$LAN_IFACE" ]; then
    # 6. LAN <-> LAN: клиенты ходят через шлюз друг к другу.
    iptables -w 5 -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$LAN_IFACE" -j ACCEPT

    # 7. VPN -> LAN: клиенты WDTT видят домашнюю сеть.
    for if in ${VPN_IFACES[@]+"${VPN_IFACES[@]}"}; do
      iptables -w 5 -A "$FWD_CHAIN" -i "$if" -o "$LAN_IFACE" -j ACCEPT
    done
  fi

  # 8. Tailnet: клиенты Tailscale ходят друг к другу и в LAN. Обратный путь
  #    tailnet -> LAN уже разрешён выше циклом по VPN_IFACES.
  if [ -n "$TS_IFACE" ]; then
    iptables -w 5 -A "$FWD_CHAIN" -i "$TS_IFACE" -o "$TS_IFACE" -j ACCEPT
    if [ -n "$LAN_IFACE" ]; then
      iptables -w 5 -A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$TS_IFACE" -j ACCEPT
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
    iptables -w 5 -t mangle -N "$TS_MARK_CHAIN"
    iptables -w 5 -t mangle -A "$TS_MARK_CHAIN" -i "$TS_IFACE" \
      -j MARK --set-xmark "$TS_NAT_MARK/$TS_NAT_MARK"
    iptables -w 5 -t mangle -I PREROUTING 1 -j "$TS_MARK_CHAIN"

    # 2. MASQUERADE в WAN. Под -s TS_SUBNET попадает и tailnet -> LAN: LAN
    #    увидит адрес шлюза, а не 100.x клиента. Это осознанно: обратный
    #    маршрут к CGNAT из LAN всё равно был бы битым.
    iptables -w 5 -t nat -N "$TS_NAT_CHAIN"
    iptables -w 5 -t nat -A "$TS_NAT_CHAIN" -s "$TS_SUBNET" -o "$LAN_IFACE" \
      -m mark --mark "$TS_NAT_MARK/$TS_NAT_MARK" -j MASQUERADE
    iptables -w 5 -t nat -I POSTROUTING 1 -j "$TS_NAT_CHAIN"

    # 3. Помеченные пакеты — в main (WAN), мимо table 2022. Приоритет 8992
    #    ниже 8991, поэтому адреса tailnet по-прежнему резолвятся в main
    #    напрямую, а не через это правило.
    ip rule add fwmark "$TS_NAT_MARK/$TS_NAT_MARK" lookup main priority "$TS_NAT_RULE_PRIO"
  fi

  echo "ok: firewall started (QUIC_DROP=$QUIC_DROP, LAN=$LAN_IFACE, VPN=${VPN_IFACES[*]-нет}, TS=$TS_IFACE, TS_MODE=$TS_EXIT_MODE, SSH=$SSH_PORT, IN=${IN_TCP_PORTS[*]}/udp:${IN_UDP_PORTS[*]})"
}

stop() {
  # NAT-режим: снимаем прыжки/цепочки/8992, даже если режим уже переключён
  # на policy — иначе останется мусор от прошлой конфигурации.
  ts_nat_cleanup
  # Прыжков может быть несколько (наследие старых версий) — снимаем все.
  while iptables -w 5 -D FORWARD -j "$FWD_CHAIN" 2>/dev/null; do :; done
  # Цепочки может уже не быть (stop дважды подряд) — это не ошибка.
  iptables -w 5 -F "$FWD_CHAIN" 2>/dev/null || true
  iptables -w 5 -X "$FWD_CHAIN" 2>/dev/null || true
  # INPUT: снимаем прыжок и цепочку (политику INPUT не трогали).
  while iptables -w 5 -D INPUT -j "$IN_CHAIN" 2>/dev/null; do :; done
  iptables -w 5 -F "$IN_CHAIN" 2>/dev/null || true
  iptables -w 5 -X "$IN_CHAIN" 2>/dev/null || true
  # 8990 — только наши правила; сносим по priority, не завязываясь на подсеть.
  while ip rule del priority 8990 2>/dev/null; do :; done
  # 8991 — tailnet; тоже только наше правило.
  while ip rule del priority 8991 2>/dev/null; do :; done

  echo "ok: firewall stopped"
}

# doctor — только читает состояние: ни iptables, ни ip rule не меняет.
# Нужен, чтобы после restart/обновления было видно, что прыжки на месте,
# стоят выше ufw/docker и сервисные порты закрыты от WAN.
doctor() {
  local fails=0
  local fwd_rules in_rules input_rules
  local port iface

  echo "== Базовый firewall: диагностика =="

  # 1. IPv4 forwarding.
  if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true)" = "1" ]; then
    echo "OK: net.ipv4.ip_forward=1"
  else
    echo "FAIL: net.ipv4.ip_forward не 1 — forwarded-трафик LAN/VPN не пойдёт; выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  fi

  # 2. Цепочка FWD и позиция прыжка.
  fwd_rules=$(iptables -w 5 -S "$FWD_CHAIN" 2>/dev/null || true)
  if [ -z "$fwd_rules" ]; then
    echo "FAIL: нет цепочки $FWD_CHAIN (или нет прав на iptables) — выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  else
    if fwd_jump_on_top; then
      echo "OK: прыжок $FWD_CHAIN в FORWARD есть и стоит выше ufw-before-forward/DOCKER-USER"
    elif iptables -w 5 -S FORWARD 2>/dev/null | grep -qF -- "-j $FWD_CHAIN"; then
      echo "FAIL: прыжок $FWD_CHAIN есть, но ниже ufw-before-forward/DOCKER-USER — правила firewall могут не применяться; выполните systemctl restart routeweave-firewall"
      fails=$((fails + 1))
    else
      echo "FAIL: в FORWARD нет прыжка в $FWD_CHAIN — правила firewall не применяются; выполните systemctl restart routeweave-firewall"
      fails=$((fails + 1))
    fi

    if has_ctstate_rule "$fwd_rules" "-j DROP" INVALID; then
      echo "OK: в $FWD_CHAIN есть DROP для INVALID"
    else
      echo "FAIL: в $FWD_CHAIN нет DROP для INVALID — битые пакеты доходят до ACCEPT"
      fails=$((fails + 1))
    fi
    if [ -n "$TS_IFACE" ] && [ -n "$TS_SUBNET" ] \
      && has_all "$fwd_rules" "! -i $TS_IFACE" "-s $TS_SUBNET" "-j DROP"; then
      echo "OK: в $FWD_CHAIN есть DROP для src $TS_SUBNET не из $TS_IFACE"
    else
      echo "FAIL: в $FWD_CHAIN нет DROP для src $TS_SUBNET не из $TS_IFACE — возможен CGNAT-спуфинг"
      fails=$((fails + 1))
    fi
    if [ "$QUIC_DROP" = "yes" ]; then
      if has_all "$fwd_rules" "-p udp" "--dport 443" "-j DROP"; then
        echo "OK: в $FWD_CHAIN есть DROP UDP/443"
      else
        echo "FAIL: в $FWD_CHAIN нет DROP UDP/443 — QUIC обойдёт политику mihomo/zapret"
        fails=$((fails + 1))
      fi
      if [ "$WAN_INGRESS" = "yes" ]; then
        if has_all "$fwd_rules" "-m mark --mark $WAN_INGRESS_MARK/$WAN_INGRESS_MARK" "-j RETURN"; then
          echo "OK: UDP/443 не режется для помеченного WAN_INGRESS трафика (mark $WAN_INGRESS_MARK)"
        else
          echo "FAIL: нет RETURN по метке $WAN_INGRESS_MARK перед DROP UDP/443 — опубликованный из WAN QUIC будет резаться"
          fails=$((fails + 1))
        fi
      fi
    fi
  fi

  # 3. Цепочка INPUT.
  in_rules=$(iptables -w 5 -S "$IN_CHAIN" 2>/dev/null || true)
  if [ -z "$in_rules" ]; then
    echo "FAIL: нет цепочки $IN_CHAIN — вход на шлюз не ограничен нашими правилами; выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  else
    input_rules=$(iptables -w 5 -S INPUT 2>/dev/null || true)
    if printf '%s\n' "$input_rules" | grep -qF -- "-j $IN_CHAIN"; then
      echo "OK: прыжок $IN_CHAIN в INPUT есть"
    else
      echo "FAIL: в INPUT нет прыжка в $IN_CHAIN — ограничения входа не работают; выполните systemctl restart routeweave-firewall"
      fails=$((fails + 1))
    fi
    # Порядок в INPUT: наше правило должно идти первым, иначе ufw/docker
    # успеют принять пакет до проверки зон.
    if printf '%s\n' "$input_rules" | awk 'NR==1{next} /^-A /{print; exit}' \
      | grep -qF -- "-j $IN_CHAIN"; then
      echo "OK: прыжок $IN_CHAIN первый в INPUT"
    else
      echo "WARN: прыжок $IN_CHAIN не первый в INPUT — ufw/docker может принять пакет раньше; выполните systemctl restart routeweave-firewall"
    fi

    if has_all "$in_rules" "-i lo" "-j ACCEPT"; then
      echo "OK: вход с lo разрешён"
    else
      echo "FAIL: нет ACCEPT для -i lo — локальные сервисы (панель, DNS) не работают"
      fails=$((fails + 1))
    fi
    if has_ctstate_rule "$in_rules" "-j ACCEPT" ESTABLISHED RELATED; then
      echo "OK: ответный трафик (ESTABLISHED,RELATED) разрешён"
    else
      echo "FAIL: нет ACCEPT для ESTABLISHED,RELATED — установленные соединения рвутся на входе"
      fails=$((fails + 1))
    fi
    if has_all "$in_rules" "-p tcp" "--dport $SSH_PORT" "-j ACCEPT"; then
      echo "OK: SSH на порт $SSH_PORT разрешён"
    else
      echo "FAIL: нет ACCEPT для SSH (порт $SSH_PORT) — можно потерять доступ к шлюзу"
      fails=$((fails + 1))
    fi
    if has_all "$in_rules" "! -i $TS_IFACE" "-s $TS_SUBNET" "-j DROP"; then
      echo "OK: вход с $TS_SUBNET не из $TS_IFACE дропается (антиспуф CGNAT)"
    else
      echo "FAIL: нет DROP для src $TS_SUBNET не из $TS_IFACE на входе"
      fails=$((fails + 1))
    fi
    for port in ${IN_TCP_PORTS[@]+"${IN_TCP_PORTS[@]}"}; do
      if has_all "$in_rules" "-p tcp" "--dport $port" "-j DROP"; then
        echo "OK: tcp/$port закрыт для недоверенных источников"
      else
        echo "FAIL: нет DROP для tcp/$port — при INPUT policy ACCEPT порт открыт из WAN"
        fails=$((fails + 1))
      fi
    done
    for port in ${IN_UDP_PORTS[@]+"${IN_UDP_PORTS[@]}"}; do
      if has_all "$in_rules" "-p udp" "--dport $port" "-j DROP"; then
        echo "OK: udp/$port закрыт для недоверенных источников"
      else
        echo "FAIL: нет DROP для udp/$port — DNS открыт из WAN при INPUT policy ACCEPT"
        fails=$((fails + 1))
      fi
    done
    if [ -n "$LAN_IFACE" ]; then
      for iface in ${IN_IFACES[@]+"${IN_IFACES[@]}"}; do
        if has_all "$in_rules" "-i $iface" "-p tcp" "--dport 7890" "-j ACCEPT"; then
          echo "OK: tcp/7890 разрешён из зоны $iface"
        else
          echo "WARN: нет ACCEPT tcp/7890 из зоны $iface — проверьте LAN_IFACE/tailscale0/wdtt0"
        fi
      done
    fi
  fi

  # 4. IPv6: осознанно не фильтруем (см. шапку), но обязаны предупредить.
  if [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo "1")" = "1" ] \
    || [ ! -r /proc/net/if_inet6 ]; then
    echo "OK: IPv6 выключен — фильтрация IPv6 не требуется"
  elif [ -s /proc/net/if_inet6 ]; then
    echo "WARN: IPv6 включён, а fw-base.sh управляет только IPv4: трафик IPv6 не фильтруется и не идёт через mihomo. Отключите IPv6 (net.ipv6.conf.all.disable_ipv6=1) или настройте ip6tables отдельно"
  else
    echo "OK: IPv6 включён, но адресов нет — фильтровать нечего"
  fi

  if [ "$fails" -gt 0 ]; then
    echo "FAIL: проверок с ошибкой: $fails"
    return 1
  fi
  echo "OK: проверки пройдены"
  return 0
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  doctor) doctor ;;
  *)
    echo "usage: $0 start|stop|doctor" >&2
    exit 1
    ;;
esac
