#!/usr/bin/env bash
# Диагностика входа Tailscale (tailscale0) в RouteWeave.
#
# Сервер работает exit node. В режиме policy (дефолт) forwarded-трафик из
# tailnet приходит на tailscale0 и уходит в mihomo TUN (Meta) по
# policy-routing table 2022 — так же, как трафик wdtt0. В режиме nat трафик
# метится 0x100000 в mangle PREROUTING и уходит в main (WAN) правилом
# priority 8992, а затем маскарадится; TUN-правила при этом не используются.
# Ответы exit-node-клиентам в обоих режимах резолвятся через main: это
# обеспечивает ip rule priority 8991 из fw-base.sh.
#
# Скрипт только читает состояние (status|doctor): сам Tailscale он не устанавливает,
# не авторизует и не меняет — ни iptables, ни ip rule, ни настройки tailscaled.
# Пакет может поставить install.sh (INSTALL_TAILSCALE=yes); авторизацию
# выполняет tailscale up/set.
#
# Использование: fw-tailscale.sh status|doctor
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# shellcheck disable=SC1091
if [ -f "$ETC/.env" ]; then . "$ETC/.env"; fi

warn() { echo "warn: $*" >&2; }

TS_IFACE="${TS_IFACE:-tailscale0}"
TS_SUBNET="${TS_SUBNET:-100.64.0.0/10}"
TUN_IFACE="${TUN_IFACE:-Meta}"
PANEL_PORT="${PANEL_PORT:-8080}"
WAN_INGRESS="${WAN_INGRESS:-no}"
WAN_INGRESS="${WAN_INGRESS,,}"
WAN_INGRESS_MARK="${WAN_INGRESS_MARK:-0x40000}"
LAN_IFACE="${LAN_IFACE:-}"
FWD_CHAIN="ROUTEWEAVE-FWD"
TS_RULE_PRIO=8991
# Метка, которую tailscale использует для subnet-route в netfilter-mode=off.
TS_MARK="0x40000"

# Режим exit node нормализуем так же, как fw-base.sh: мусор -> policy, чтобы
# диагностика не расходилась с фактическим поведением firewall.
TS_EXIT_MODE="${TS_EXIT_MODE:-policy}"
TS_EXIT_MODE="${TS_EXIT_MODE,,}"
case "$TS_EXIT_MODE" in
  policy | nat) ;;
  *)
    warn "TS_EXIT_MODE='${TS_EXIT_MODE}' не policy/nat, считаю policy"
    TS_EXIT_MODE="policy"
    ;;
esac

# Артефакты NAT-режима (см. fw-base.sh).
TS_NAT_MARK="0x100000"
TS_NAT_RULE_PRIO=8992
TS_MARK_CHAIN="ROUTEWEAVE-TSMARK"
TS_NAT_CHAIN="ROUTEWEAVE-TSNAT"

# chain_exists — есть ли цепочка в таблице (без прав iptables -> нет).
chain_exists() { # chain_exists <таблица> <цепочка>
  iptables -t "$1" -nL "$2" >/dev/null 2>&1
}


# route_dev — имя dev из вывода `ip route get`.
route_dev() {
  awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

yes_no() { # yes_no <0|1>
  if [ "$1" = "1" ]; then echo "да"; else echo "нет"; fi
}

kv_get() { # kv_get "<key=value...>" <key>
  printf '%s\n' "$1" | sed -n "s/^$2=//p"
}

# ts_status_kv — разбирает `tailscale status --json` через python3 (jq не
# нужен) и печатает стабильные пары key=value. Ошибка парсинга/JSON -> rc=1.
ts_status_kv() {
  local raw
  command -v tailscale >/dev/null 2>&1 || return 1
  raw=$(tailscale status --json 2>/dev/null) || return 1
  printf '%s' "$raw" | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)

self_ = data.get("Self") or {}
prefs = data.get("Prefs") or {}
ips = self_.get("TailscaleIPs") or []
ipv4 = next((ip for ip in ips if ":" not in ip), "")
advertised_routes = prefs.get("AdvertiseRoutes") or []
advertised = bool(self_.get("ExitNodeOption")) or "0.0.0.0/0" in advertised_routes
approved = "0.0.0.0/0" in (self_.get("PrimaryRoutes") or [])
print("state=" + str(data.get("BackendState") or ""))
print("ipv4=" + str(ipv4))
print("advertised=" + ("1" if advertised else "0"))
print("approved=" + ("1" if approved else "0"))
'
}

# rule_8991_count — сколько правил priority 8991 указывают TS_SUBNET в main.
rule_8991_count() {
  local line count=0
  while IFS= read -r line; do
    [[ "$line" =~ ^${TS_RULE_PRIO}: ]] || continue
    if [[ "$line" == *"to $TS_SUBNET"* && "$line" == *"lookup main"* ]]; then
      count=$((count + 1))
    fi
  done
  echo "$count"
}

# fwd_flags — три флага (1/0) по правилам в ROUTEWEAVE-FWD:
#   вход tailscale0 -> TUN, возврат TUN -> tailscale0, CGNAT-спуф DROP.
fwd_flags() {
  local line in_meta=0 meta_out=0 spoof=0
  while IFS= read -r line; do
    if [[ "$line" == *"-i $TS_IFACE"* && "$line" == *"-o $TUN_IFACE"* \
      && "$line" == *"-j ACCEPT"* ]]; then
      in_meta=1
    fi
    if [[ "$line" == *"-i $TUN_IFACE"* && "$line" == *"-o $TS_IFACE"* \
      && "$line" == *"-j ACCEPT"* ]]; then
      meta_out=1
    fi
    if [[ "$line" == *"! -i $TS_IFACE"* && "$line" == *"-s $TS_SUBNET"* \
      && "$line" == *"-j DROP"* ]]; then
      spoof=1
    fi
  done
  printf '%s %s %s\n' "$in_meta" "$meta_out" "$spoof"
}

status() {
  local ts_active ts_kv ts_state ts_ip advertised approved
  local rules fwd_rules rule_count has_in has_out has_spoof
  local nat_rules nat_mangle nat_mangle_hook nat_chain nat_nat_hook

  echo "TS_IFACE: $TS_IFACE"
  echo "TS_SUBNET: $TS_SUBNET"
  echo "TUN_IFACE: $TUN_IFACE"
  echo "TS_EXIT_MODE: $TS_EXIT_MODE"

  if command -v tailscale >/dev/null 2>&1; then
    echo "tailscale: установлен ($(command -v tailscale))"
  else
    echo "tailscale: не установлен"
  fi
  ts_active=$(systemctl is-active tailscaled 2>/dev/null || true)
  echo "tailscaled: ${ts_active:-нет ответа systemctl}"

  if ts_kv=$(ts_status_kv 2>/dev/null); then
    ts_state=$(kv_get "$ts_kv" state)
    ts_ip=$(kv_get "$ts_kv" ipv4)
    advertised=$(kv_get "$ts_kv" advertised)
    approved=$(kv_get "$ts_kv" approved)
    echo "BackendState: ${ts_state:-?}"
    echo "Tailscale IPv4: ${ts_ip:-нет}"
    echo "exit node advertised: $(yes_no "$advertised")"
    echo "exit node approved: $(yes_no "$approved")"
  else
    echo "статус tailscale: недоступен (демон не отвечает или узел не авторизован)"
  fi

  rules=$(ip rule show 2>/dev/null || true)
  rule_count=$(printf '%s\n' "$rules" | rule_8991_count)
  echo "ip rule $TS_RULE_PRIO to $TS_SUBNET lookup main: $(yes_no "$((rule_count > 0))")"

  fwd_rules=$(iptables -S "$FWD_CHAIN" 2>/dev/null || true)
  read -r has_in has_out has_spoof <<< "$(fwd_flags <<< "$fwd_rules")"
  echo "правило $TS_IFACE -> $TUN_IFACE: $(yes_no "$has_in")"
  echo "правило $TUN_IFACE -> $TS_IFACE: $(yes_no "$has_out")"
  echo "CGNAT-спуф DROP (src $TS_SUBNET не из $TS_IFACE): $(yes_no "$has_spoof")"

  if [ "$TS_EXIT_MODE" = "nat" ]; then
    nat_rules=$(ip rule show 2>/dev/null || true)
    if [[ "$nat_rules" == *"$TS_NAT_RULE_PRIO:"*"fwmark $TS_NAT_MARK/$TS_NAT_MARK lookup main"* ]]; then
      echo "ip rule $TS_NAT_RULE_PRIO (fwmark $TS_NAT_MARK/$TS_NAT_MARK lookup main): есть"
    else
      echo "ip rule $TS_NAT_RULE_PRIO (fwmark $TS_NAT_MARK/$TS_NAT_MARK lookup main): нет"
    fi

    nat_mangle=$(iptables -t mangle -S "$TS_MARK_CHAIN" 2>/dev/null || true)
    nat_mangle_hook=$(iptables -t mangle -S PREROUTING 2>/dev/null || true)
    if chain_exists mangle "$TS_MARK_CHAIN"; then
      echo "chain mangle $TS_MARK_CHAIN: есть"
    else
      echo "chain mangle $TS_MARK_CHAIN: нет"
    fi
    if [[ "$nat_mangle_hook" == *"-j $TS_MARK_CHAIN"* ]]; then
      echo "jump PREROUTING -> $TS_MARK_CHAIN: есть"
    else
      echo "jump PREROUTING -> $TS_MARK_CHAIN: нет"
    fi
    if [[ "$nat_mangle" == *"-i $TS_IFACE"* \
      && "$nat_mangle" == *"--set-xmark $TS_NAT_MARK/$TS_NAT_MARK"* ]]; then
      echo "правило MARK --set-xmark $TS_NAT_MARK/$TS_NAT_MARK для -i $TS_IFACE: есть"
    else
      echo "правило MARK --set-xmark $TS_NAT_MARK/$TS_NAT_MARK для -i $TS_IFACE: нет"
    fi

    nat_chain=$(iptables -t nat -S "$TS_NAT_CHAIN" 2>/dev/null || true)
    nat_nat_hook=$(iptables -t nat -S POSTROUTING 2>/dev/null || true)
    if chain_exists nat "$TS_NAT_CHAIN"; then
      echo "chain nat $TS_NAT_CHAIN: есть"
    else
      echo "chain nat $TS_NAT_CHAIN: нет"
    fi
    if [[ "$nat_nat_hook" == *"-j $TS_NAT_CHAIN"* ]]; then
      echo "jump POSTROUTING -> $TS_NAT_CHAIN: есть"
    else
      echo "jump POSTROUTING -> $TS_NAT_CHAIN: нет"
    fi
    if [[ "$nat_chain" == *"-s $TS_SUBNET"* && "$nat_chain" == *"-o $LAN_IFACE"* \
      && "$nat_chain" == *"--mark $TS_NAT_MARK/$TS_NAT_MARK"* \
      && "$nat_chain" == *"MASQUERADE"* ]]; then
      echo "правило MASQUERADE -s $TS_SUBNET -o $LAN_IFACE: есть"
    else
      echo "правило MASQUERADE -s $TS_SUBNET -o $LAN_IFACE: нет"
    fi

    echo "режим nat: трафик tailnet идёт напрямую в WAN (main), правила $TS_IFACE -> $TUN_IFACE и $TUN_IFACE -> $TS_IFACE не используются"
    echo "geo/SOCKS/DPI-политика mihomo для tailnet не применяется; zapret на WAN-egress продолжает работать"
  fi
  return 0
}

doctor() {
  local fails=0 line prio min_2022="" rules ts_active ip_forward
  local ts_kv="" ts_state="" ts_ip="" advertised="" approved=""
  local fwd_rules has_in has_out has_spoof jumps_ts ufw_out route_out dev_if
  local missing_ports="" p found route_ok mark_lc
  local nat_mangle nat_mangle_hook nat_chain nat_nat_hook nat_rules

  echo "== Tailscale exit node: диагностика =="
  echo "TS_EXIT_MODE: $TS_EXIT_MODE"

  # 1. Пакет и демон.
  if command -v tailscale >/dev/null 2>&1; then
    echo "OK: tailscale найден ($(command -v tailscale))"
  else
    echo "FAIL: tailscale не найден в PATH — установите пакет (INSTALL_TAILSCALE=yes) или вручную и авторизуйте его"
    fails=$((fails + 1))
  fi
  ts_active=$(systemctl is-active tailscaled 2>/dev/null || true)
  if [ "$ts_active" = "active" ]; then
    echo "OK: tailscaled active"
  else
    echo "FAIL: tailscaled не active (${ts_active:-нет ответа systemctl}) — проверьте systemctl status tailscaled"
    fails=$((fails + 1))
  fi

  # 2. Forwarding.
  ip_forward=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true)
  if [ "$ip_forward" = "1" ]; then
    echo "OK: net.ipv4.ip_forward=1"
  else
    echo "FAIL: net.ipv4.ip_forward=${ip_forward:-?} (нужно 1) — sysctl -w net.ipv4.ip_forward=1"
    fails=$((fails + 1))
  fi

  # 3. Наше правило и его приоритет относительно policy-routing mihomo.
  rules=$(ip rule show 2>/dev/null || true)
  if [ "$(printf '%s\n' "$rules" | rule_8991_count)" -gt 0 ]; then
    echo "OK: ip rule priority $TS_RULE_PRIO: to $TS_SUBNET lookup main есть"
  else
    echo "FAIL: нет ip rule priority $TS_RULE_PRIO: to $TS_SUBNET lookup main — ответы exit-node-клиентам уйдут в TUN; выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  fi

  while IFS= read -r line; do
    [[ "$line" =~ ^([0-9]+): ]] || continue
    prio="${BASH_REMATCH[1]}"
    if [[ "$line" == *"lookup 2022"* || "$line" == *"table 2022"* ]]; then
      if [ -z "$min_2022" ] || [ "$prio" -lt "$min_2022" ]; then
        min_2022="$prio"
      fi
    fi
  done <<< "$rules"

  if [ -z "$min_2022" ]; then
    echo "WARN: правил с таблицей 2022 не найдено — проверка приоритета пропущена (mihomo не запущен?)"
  elif [ "$TS_RULE_PRIO" -lt "$min_2022" ]; then
    echo "OK: priority $TS_RULE_PRIO меньше минимального priority таблицы 2022 ($min_2022)"
  else
    echo "FAIL: priority $TS_RULE_PRIO >= $min_2022 (правила таблицы 2022) — tailnet уйдёт в TUN mihomo"
    fails=$((fails + 1))
  fi

  # 4. Правила в цепочке ROUTEWEAVE-FWD. CGNAT-спуф нужен в обоих режимах,
  #    а ACCEPT в TUN — только в policy: в nat трафик уходит в WAN и эти
  #    правила не используются (могут оставаться, это не ошибка).
  fwd_rules=$(iptables -S "$FWD_CHAIN" 2>/dev/null || true)
  if [ -z "$fwd_rules" ]; then
    echo "FAIL: нет цепочки $FWD_CHAIN (или нет прав на iptables) — выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  else
    read -r has_in has_out has_spoof <<< "$(fwd_flags <<< "$fwd_rules")"
    if [ "$has_in" = "1" ]; then
      echo "OK: в $FWD_CHAIN есть ACCEPT $TS_IFACE -> $TUN_IFACE"
    elif [ "$TS_EXIT_MODE" = "nat" ]; then
      echo "инфо: ACCEPT $TS_IFACE -> $TUN_IFACE отсутствует — в nat-режиме не требуется"
    else
      echo "FAIL: в $FWD_CHAIN нет ACCEPT $TS_IFACE -> $TUN_IFACE — exit-node-трафик не пойдёт в mihomo"
      fails=$((fails + 1))
    fi
    if [ "$has_out" = "1" ]; then
      echo "OK: в $FWD_CHAIN есть ACCEPT $TUN_IFACE -> $TS_IFACE (возвратный путь)"
    elif [ "$TS_EXIT_MODE" = "nat" ]; then
      echo "инфо: ACCEPT $TUN_IFACE -> $TS_IFACE отсутствует — в nat-режиме не требуется"
    else
      echo "FAIL: в $FWD_CHAIN нет ACCEPT $TUN_IFACE -> $TS_IFACE — ответы exit-node-клиентам потеряются"
      fails=$((fails + 1))
    fi
    if [ "$has_spoof" = "1" ]; then
      echo "OK: в $FWD_CHAIN есть DROP для src $TS_SUBNET не из $TS_IFACE"
    else
      echo "FAIL: в $FWD_CHAIN нет DROP для src $TS_SUBNET не из $TS_IFACE — возможен CGNAT-спуфинг"
      fails=$((fails + 1))
    fi
  fi

  # 5. В netfilter-mode=nodivert tailscale не должен вставлять свои цепочки:
  #    иначе его MARK/MASQUERADE сработают раньше правил RouteWeave.
  jumps_ts=$(
    {
      iptables -S INPUT 2>/dev/null || true
      iptables -S FORWARD 2>/dev/null || true
      iptables -t nat -S POSTROUTING 2>/dev/null || true
    } | grep -E -- '-j (ts-input|ts-forward|ts-postrouting)' || true
  )
  if [ -n "$jumps_ts" ]; then
    echo "WARN: tailscale вставил свои цепочки (ts-input/ts-forward/ts-postrouting) — его MARK $TS_MARK и MASQUERADE могут сработать раньше RouteWeave"
    echo "WARN: переведите tailscale в nodivert: sudo tailscale set --netfilter-mode=nodivert"
  else
    echo "OK: прыжков в ts-input/ts-forward/ts-postrouting нет"
  fi

  # 6. Авторизация и exit node.
  if command -v tailscale >/dev/null 2>&1; then
    if ts_kv=$(ts_status_kv 2>/dev/null); then
      ts_state=$(kv_get "$ts_kv" state)
      ts_ip=$(kv_get "$ts_kv" ipv4)
      advertised=$(kv_get "$ts_kv" advertised)
      approved=$(kv_get "$ts_kv" approved)

      if [ "$ts_state" = "Running" ]; then
        echo "OK: BackendState=Running"
      else
        echo "FAIL: BackendState=${ts_state:-?} (нужно Running) — узел не авторизован или демон не работает"
        fails=$((fails + 1))
      fi
      if [ -n "$ts_ip" ]; then
        echo "OK: Tailscale IPv4=$ts_ip"
      else
        echo "FAIL: у узла нет IPv4-адреса Tailscale"
        fails=$((fails + 1))
      fi
      if [ "$advertised" = "1" ]; then
        echo "OK: exit node анонсирован (0.0.0.0/0)"
      else
        echo "FAIL: exit node не анонсирован — sudo tailscale set --advertise-exit-node и подтвердите узел в админ-консоли"
        fails=$((fails + 1))
      fi
      if [ "$approved" = "1" ]; then
        echo "OK: маршрут 0.0.0.0/0 одобрен (Self.PrimaryRoutes)"
      else
        echo "FAIL: маршрут 0.0.0.0/0 не одобрен — подтвердите exit node в админ-консоли Tailscale"
        fails=$((fails + 1))
      fi
    else
      echo "FAIL: tailscale status --json недоступен — демон не отвечает или узел не авторизован"
      fails=$((fails + 1))
    fi
  else
    echo "WARN: проверка статуса tailscale пропущена (команды нет)"
  fi

  # 7. Реальные маршруты и артефакты выбранного режима.
  if [ "$TS_EXIT_MODE" = "nat" ]; then
    # 7a. Правило 8992 и его место относительно mihomo.
    nat_rules=$(ip rule show 2>/dev/null || true)
    if [[ "$nat_rules" == *"$TS_NAT_RULE_PRIO:"*"fwmark $TS_NAT_MARK/$TS_NAT_MARK lookup main"* ]]; then
      echo "OK: ip rule priority $TS_NAT_RULE_PRIO: fwmark $TS_NAT_MARK/$TS_NAT_MARK lookup main есть"
    else
      echo "FAIL: нет ip rule priority $TS_NAT_RULE_PRIO с fwmark $TS_NAT_MARK/$TS_NAT_MARK -> main — помеченный трафик уйдёт в TUN; выполните systemctl restart routeweave-firewall"
      fails=$((fails + 1))
    fi
    if [ -n "$min_2022" ] && [ "$TS_NAT_RULE_PRIO" -ge "$min_2022" ]; then
      echo "FAIL: priority $TS_NAT_RULE_PRIO >= $min_2022 (правила таблицы 2022) — помеченный трафик не успеет уйти в main"
      fails=$((fails + 1))
    fi

    # 7b. mangle: цепочка, прыжок, правило MARK.
    nat_mangle_hook=$(iptables -t mangle -S PREROUTING 2>/dev/null || true)
    nat_mangle=$(iptables -t mangle -S "$TS_MARK_CHAIN" 2>/dev/null || true)
    if chain_exists mangle "$TS_MARK_CHAIN" \
      && [[ "$nat_mangle_hook" == *"-j $TS_MARK_CHAIN"* ]]; then
      echo "OK: цепочка mangle $TS_MARK_CHAIN есть, прыжок из PREROUTING есть"
    else
      echo "FAIL: нет цепочки mangle $TS_MARK_CHAIN или прыжка из PREROUTING — трафик tailnet не помечается; выполните systemctl restart routeweave-firewall"
      fails=$((fails + 1))
    fi
    if [[ "$nat_mangle" == *"-i $TS_IFACE"* \
      && "$nat_mangle" == *"--set-xmark $TS_NAT_MARK/$TS_NAT_MARK"* ]]; then
      echo "OK: в $TS_MARK_CHAIN есть MARK --set-xmark $TS_NAT_MARK/$TS_NAT_MARK для -i $TS_IFACE"
    else
      echo "FAIL: в $TS_MARK_CHAIN нет MARK --set-xmark $TS_NAT_MARK/$TS_NAT_MARK для -i $TS_IFACE"
      fails=$((fails + 1))
    fi

    # 7c. nat: цепочка, прыжок, MASQUERADE.
    nat_nat_hook=$(iptables -t nat -S POSTROUTING 2>/dev/null || true)
    nat_chain=$(iptables -t nat -S "$TS_NAT_CHAIN" 2>/dev/null || true)
    if chain_exists nat "$TS_NAT_CHAIN" \
      && [[ "$nat_nat_hook" == *"-j $TS_NAT_CHAIN"* ]]; then
      echo "OK: цепочка nat $TS_NAT_CHAIN есть, прыжок из POSTROUTING есть"
    else
      echo "FAIL: нет цепочки nat $TS_NAT_CHAIN или прыжка из POSTROUTING — трафик tailnet уйдёт в WAN без подмены источника; выполните systemctl restart routeweave-firewall"
      fails=$((fails + 1))
    fi
    if [[ "$nat_chain" == *"-s $TS_SUBNET"* && "$nat_chain" == *"-o $LAN_IFACE"* \
      && "$nat_chain" == *"--mark $TS_NAT_MARK/$TS_NAT_MARK"* \
      && "$nat_chain" == *"MASQUERADE"* ]]; then
      echo "OK: в $TS_NAT_CHAIN есть MASQUERADE для -s $TS_SUBNET -o $LAN_IFACE с меткой $TS_NAT_MARK"
    else
      echo "FAIL: в $TS_NAT_CHAIN нет MASQUERADE для -s $TS_SUBNET -o $LAN_IFACE с меткой $TS_NAT_MARK — проверьте LAN_IFACE='$LAN_IFACE'"
      fails=$((fails + 1))
    fi

    # 7d. Помеченный пакет обязан уходить не в TUN. LAN_IFACE — ожидание, а не
    #     догма: при нескольких NIC маршрут может идти через другой WAN.
    if route_out=$(ip route get 1.1.1.1 mark "$TS_NAT_MARK" 2>&1); then
      dev_if=$(route_dev <<< "$route_out")
      if [ -n "$TUN_IFACE" ] && [ "$dev_if" = "$TUN_IFACE" ]; then
        echo "FAIL: ip route get 1.1.1.1 mark $TS_NAT_MARK уходит в $dev_if (TUN) — метка или правило $TS_NAT_RULE_PRIO не работают"
        fails=$((fails + 1))
      elif [ -n "$LAN_IFACE" ] && [ "$dev_if" != "$LAN_IFACE" ]; then
        echo "WARN: помеченный маршрут идёт через ${dev_if:-неизвестный интерфейс}, а LAN_IFACE=$LAN_IFACE — проверьте маршруты (при нескольких NIC это не обязательно ошибка)"
      else
        echo "OK: ip route get 1.1.1.1 mark $TS_NAT_MARK -> dev ${dev_if:-?}"
      fi
    else
      echo "FAIL: ip route get 1.1.1.1 mark $TS_NAT_MARK завершился ошибкой: $route_out"
      fails=$((fails + 1))
    fi

    if ! ip link show "$TS_IFACE" >/dev/null 2>&1; then
      echo "WARN: интерфейса $TS_IFACE нет — проверка маршрута exit-node-трафика пропущена"
    elif [ -z "$ts_ip" ]; then
      echo "WARN: IPv4 tailscale неизвестен — проверка маршрута exit-node-трафика пропущена"
    elif route_out=$(ip route get 1.1.1.1 from "$ts_ip" iif "$TS_IFACE" mark "$TS_NAT_MARK" 2>&1); then
      dev_if=$(route_dev <<< "$route_out")
      if [ -n "$TUN_IFACE" ] && [ "$dev_if" = "$TUN_IFACE" ]; then
        echo "FAIL: exit-node-трафик (from $ts_ip iif $TS_IFACE mark $TS_NAT_MARK) идёт в dev $dev_if (TUN), а должен в WAN — проверьте правило $TS_NAT_RULE_PRIO"
        fails=$((fails + 1))
      else
        echo "OK: exit-node-трафик (from $ts_ip iif $TS_IFACE mark $TS_NAT_MARK) идёт в dev ${dev_if:-?}, не в TUN"
      fi
    else
      # На части ядер ip route get с локальным src и iif отдаёт EINVAL:
      # это ограничение проверки, а не обязательно поломка маршрутизации.
      echo "WARN: не удалось проверить exit-node-маршрут: $route_out"
      echo "WARN: проверьте вручную с адресом клиента: ip route get 1.1.1.1 from <TS-IP клиента> iif $TS_IFACE mark $TS_NAT_MARK (ожидается не $TUN_IFACE)"
    fi

    if route_out=$(ip route get 100.64.0.1 2>&1); then
      dev_if=$(route_dev <<< "$route_out")
      if [ -n "$TUN_IFACE" ] && [ "$dev_if" = "$TUN_IFACE" ]; then
        echo "FAIL: маршрут к 100.64.0.1 уходит в dev $dev_if (TUN) — ответы exit-node-клиентам потеряются, проверьте ip rule $TS_RULE_PRIO"
        fails=$((fails + 1))
      else
        echo "OK: маршрут к 100.64.0.1 идёт в dev ${dev_if:-?}, а не в TUN"
      fi
    else
      echo "FAIL: ip route get 100.64.0.1 завершился ошибкой: $route_out"
      fails=$((fails + 1))
    fi

    echo "WARN: TS_EXIT_MODE=nat: geo/SOCKS/DPI-политика mihomo для tailnet не применяется; zapret на WAN-egress продолжает работать"
  elif ! ip link show "$TS_IFACE" >/dev/null 2>&1; then
    echo "WARN: интерфейса $TS_IFACE нет — проверка маршрутов пропущена"
  elif [ -z "$ts_ip" ]; then
    echo "WARN: IPv4 tailscale неизвестен — проверка маршрутов пропущена"
  else
    if route_out=$(ip route get 1.1.1.1 from "$ts_ip" iif "$TS_IFACE" 2>&1); then
      dev_if=$(route_dev <<< "$route_out")
      if [ -n "$TUN_IFACE" ] && [ "$dev_if" = "$TUN_IFACE" ]; then
        echo "OK: exit-node-трафик (from $ts_ip iif $TS_IFACE) идёт в dev $TUN_IFACE"
      else
        echo "FAIL: exit-node-трафик (from $ts_ip iif $TS_IFACE) идёт в dev ${dev_if:-?}, а должен в $TUN_IFACE — проверьте policy-routing mihomo"
        fails=$((fails + 1))
      fi
    else
      # На части ядер ip route get с локальным src и iif отдаёт EINVAL:
      # это ограничение проверки, а не обязательно поломка маршрутизации.
      echo "WARN: не удалось проверить exit-node-маршрут: $route_out"
      echo "WARN: проверьте вручную с адресом клиента: ip route get 1.1.1.1 from <TS-IP клиента> iif $TS_IFACE (ожидается dev $TUN_IFACE)"
    fi

    if route_out=$(ip route get 100.64.0.1 2>&1); then
      dev_if=$(route_dev <<< "$route_out")
      if [ -n "$TUN_IFACE" ] && [ "$dev_if" = "$TUN_IFACE" ]; then
        echo "FAIL: маршрут к 100.64.0.1 уходит в dev $dev_if (TUN) — ответы exit-node-клиентам потеряются, проверьте ip rule $TS_RULE_PRIO"
        fails=$((fails + 1))
      else
        echo "OK: маршрут к 100.64.0.1 идёт в dev ${dev_if:-?}, а не в TUN"
      fi
    else
      echo "FAIL: ip route get 100.64.0.1 завершился ошибкой: $route_out"
      fails=$((fails + 1))
    fi
  fi

  # 8. ufw: tailnet должен получать те же порты, что LAN/WDTT.
  ufw_out=""
  if command -v ufw >/dev/null 2>&1; then
    ufw_out=$(ufw status 2>/dev/null || true)
  fi
  if grep -q '^Status: active' <<< "$ufw_out"; then
    for p in 53 "$PANEL_PORT" 7890 9090; do
      found=0
      while IFS= read -r line; do
        [[ "$line" == *ALLOW* && "$line" == *"$TS_SUBNET"* ]] || continue
        if [[ "$line" == "$p"/* || "$line" == "$p "* ]]; then
          found=1
          break
        fi
      done <<< "$ufw_out"
      if [ "$found" -eq 0 ]; then
        missing_ports="$missing_ports $p"
      fi
    done
    if [ -z "$missing_ports" ]; then
      echo "OK: ufw: порты 53/$PANEL_PORT/7890/9090 открыты для $TS_SUBNET"
    else
      echo "WARN: ufw: нет allow from $TS_SUBNET на порты:$missing_ports — добавьте вручную или перезапустите install.sh"
    fi

    route_ok=0
    while IFS= read -r line; do
      if [[ "$line" == *"on $TS_IFACE"* && "$line" == *"FWD"* ]]; then
        route_ok=1
        break
      fi
    done <<< "$ufw_out"
    if [ "$route_ok" -eq 1 ]; then
      echo "OK: ufw: route allow на $TS_IFACE есть"
    else
      echo "WARN: ufw: нет route allow на $TS_IFACE — forwarded-трафик tailnet может блокироваться"
    fi
  else
    echo "OK: ufw не активен — проверка ufw не требуется"
  fi

  # 9. Конфликт метки WAN_INGRESS с меткой, которую использует exit node.
  mark_lc="${WAN_INGRESS_MARK,,}"
  if [ "$TS_EXIT_MODE" = "nat" ]; then
    if [ "$WAN_INGRESS" = "yes" ] && [ "$mark_lc" = "$TS_NAT_MARK" ]; then
      echo "WARN: WAN_INGRESS_MARK=$WAN_INGRESS_MARK совпадает с меткой NAT-режима tailnet ($TS_NAT_MARK) — выберите другую, например 0x200000"
    else
      echo "OK: WAN_INGRESS_MARK=$WAN_INGRESS_MARK с меткой NAT-режима tailnet ($TS_NAT_MARK) не конфликтует"
    fi
  elif [ "$WAN_INGRESS" = "yes" ] && [ "$mark_lc" = "$TS_MARK" ]; then
    echo "WARN: WAN_INGRESS_MARK=$WAN_INGRESS_MARK совпадает с меткой subnet-route tailscale ($TS_MARK) — выберите другую, например 0x200000"
  else
    echo "OK: WAN_INGRESS_MARK=$WAN_INGRESS_MARK с меткой tailscale ($TS_MARK) не конфликтует"
  fi

  if [ "$fails" -gt 0 ]; then
    echo "FAIL: проверок с ошибкой: $fails"
    return 1
  fi
  echo "OK: проверки пройдены"
  return 0
}

case "${1:-}" in
  status) status ;;
  doctor) doctor ;;
  *)
    echo "usage: $0 status|doctor" >&2
    exit 1
    ;;
esac
