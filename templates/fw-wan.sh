#!/usr/bin/env bash
# Доступ из WAN к сервисам, опубликованным на шлюзе (DNAT/docker).
#
# Почему это вообще нужно. mihomo в режиме TUN с auto-route создаёт таблицу
# 2022 с единственным маршрутом "default via 198.18.0.2 dev Meta" и правила:
#   9001: not from all dport 53 lookup main suppress_prefixlength 0
#   9001: from all iif Meta goto 9010
#   9002: not from all iif lo lookup 2022
# Входящий из WAN пакет на опубликованный порт доходит до контейнера, но его
# ответ (src 172.21.x.x, dst публичный IP) попадает в 9002 и уходит в TUN:
# соединение не устанавливается. Из LAN спасает 9001 с suppress_prefixlength 0,
# который отдаёт connected-маршруты из main.
#
# Решение — connmark: помечаем входящее из WAN NEW-соединение на нужные порты,
# восстанавливаем метку на всех пакетах этого соединения (ответы контейнера
# приходят на br-интерфейс и проходят PREROUTING -> FORWARD) и уводим
# помеченные пакеты в main правилом с приоритетом НИЖЕ (численно меньше)
# правил mihomo.
#
# restore-mark стоит ПЕРВЫМ в цепочке намеренно: у ответа контейнера src
# приватный (172.21.x.x), и если бы RETURN для приватных сетей срабатывал
# раньше, метка не восстановилась бы — ответ снова ушёл бы в TUN.
#
# Использование: fw-wan.sh start|stop|status|doctor
# start/stop сериализуются через flock: их вызывают два юнита почти одновременно.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# shellcheck disable=SC1091
if [ -f "$ETC/.env" ]; then . "$ETC/.env"; fi

warn() { echo "warn: $*" >&2; }
die() {
  echo "error: $*" >&2
  exit 1
}

# start/stop меняют iptables неатомарно, а вызываются из двух юнитов
# (routeweave-firewall.service и ExecStartPost mihomo.service), которые при
# загрузке стартуют почти одновременно. Без блокировки два параллельных
# запуска могут перемешать -F и -A и оставить дубли правил.
LOCK_FILE="/run/routeweave-fw-wan.lock"

take_lock() {
  # flock из util-linux есть везде, где есть ip/iptables; если его нет —
  # работаем как раньше, но предупреждаем.
  if ! command -v flock >/dev/null 2>&1; then
    warn "flock не найден — параллельный запуск fw-wan.sh не защищён"
    return 0
  fi
  exec 9>>"$LOCK_FILE" || die "не удалось открыть $LOCK_FILE"
  flock -w 30 9 || die "не удалось получить блокировку $LOCK_FILE за 30 с"
}

CHAIN="ROUTEWEAVE-WANMARK"
OUT_CHAIN="ROUTEWEAVE-WANMARK-OUT"

# WAN_INGRESS нормализуем сами: в .env его пишут руками, и "Yes", " true" или
# опечатка не должны молча включать (или выключать) доступ. Мусор -> no:
# fail closed, лучше не открыть порт, чем открыть лишнее.
WAN_INGRESS="${WAN_INGRESS:-no}"
WAN_INGRESS="${WAN_INGRESS,,}"
case "$WAN_INGRESS" in
  yes | no) ;;
  *)
    warn "WAN_INGRESS='${WAN_INGRESS}' не yes/no, считаю no"
    WAN_INGRESS="no"
    ;;
esac

WAN_PORTS="${WAN_PORTS:-}"
WAN_INGRESS_MARK="${WAN_INGRESS_MARK:-0x40000}"
WAN_INGRESS_RULE_PRIO="${WAN_INGRESS_RULE_PRIO:-8993}"
TUN_IFACE="${TUN_IFACE:-Meta}"
LAN_IFACE="${LAN_IFACE:-}"
SERVER_IP="${SERVER_IP:-}"

# Метка уходит в iptables и ip rule как есть, поэтому принимаем только
# канонический вид 0x + 1..8 hex: иначе опечатка превратится в другое число
# или в аргумент, который iptables поймёт не так, как ожидалось.
validate_mark() {
  [[ "$WAN_INGRESS_MARK" =~ ^0x[0-9a-fA-F]{1,8}$ ]] \
    || die "WAN_INGRESS_MARK='${WAN_INGRESS_MARK}' должен быть 0x + 1..8 hex (например 0x40000)"
}
# Приоритет меньше 32766 (main) и больше 0; практический диапазон — до 9000,
# чтобы правило стояло выше policy-routing mihomo.
[[ "$WAN_INGRESS_RULE_PRIO" =~ ^[0-9]{1,5}$ ]] \
  || die "WAN_INGRESS_RULE_PRIO='${WAN_INGRESS_RULE_PRIO}' должен быть целым числом"
if [ "$WAN_INGRESS_RULE_PRIO" -lt 1 ] || [ "$WAN_INGRESS_RULE_PRIO" -gt 32765 ]; then
  die "WAN_INGRESS_RULE_PRIO='${WAN_INGRESS_RULE_PRIO}' вне диапазона 1..32765"
fi

# Разобранные порты: элементы вида "tcp 8081" / "udp 51820" / "tcp 8000:8100".
PORTS=()

# parse_ports — нормализует WAN_PORTS: запятые и пробелы как разделители,
# голое число = tcp, диапазон можно писать и "8000:8100", и "8000-8100"
# (iptables понимает только двоеточие, поэтому дефис приводим к нему).
# Возвращает 1 на любом непонятном токене: молча пропустить опечатку нельзя —
# иначе при пустом результате включился бы режим "пометить все порты".
parse_ports() {
  local raw tok proto port lo hi key
  local -a tokens=()

  PORTS=()
  raw="${WAN_PORTS//,/ }"
  read -r -a tokens <<< "$raw"

  for tok in ${tokens[@]+"${tokens[@]}"}; do
    proto="tcp"
    port="$tok"

    if [[ "$tok" == */* ]]; then
      proto="${tok##*/}"
      port="${tok%/*}"
      proto="${proto,,}"
    fi

    if [[ ! "$port" =~ ^([0-9]{1,5})([-:]([0-9]{1,5}))?$ ]]; then
      warn "WAN_PORTS: не понимаю '${tok}' (ожидается 8081, 8081/tcp, 51820/udp, 8000:8100/tcp)"
      return 1
    fi
    lo="${BASH_REMATCH[1]}"
    hi="${BASH_REMATCH[3]:-}"
    if [ -n "$hi" ]; then
      port="${lo}:${hi}"
    else
      port="$lo"
    fi

    case "$proto" in
      tcp | udp) ;;
      *)
        warn "WAN_PORTS: у '${tok}' протокол '${proto}', а поддерживаются только tcp и udp"
        return 1
        ;;
    esac

    if [ "$lo" -lt 1 ] || [ "$lo" -gt 65535 ] \
      || { [ -n "$hi" ] && { [ "$hi" -lt 1 ] || [ "$hi" -gt 65535 ] || [ "$lo" -gt "$hi" ]; }; }; then
      warn "WAN_PORTS: '${tok}' — порт вне 1..65535 или диапазон задан наоборот"
      return 1
    fi

    # Один и тот же порт могли указать дважды (например, "8081 8081/tcp") —
    # второе правило set-mark бессмысленно и только удлиняет цепочку.
    key="${proto} ${port}"
    case " ${PORTS[*]-} " in
      *" ${key} "*) continue ;;
    esac
    PORTS+=("$proto $port")
  done
  return 0
}

# ports_display — человекочитаемый список для status/сводки: "8081/tcp".
ports_display() {
  local entry
  local -a out=()
  for entry in ${PORTS[@]+"${PORTS[@]}"}; do
    out+=("${entry##* }/${entry%% *}")
  done
  echo "${out[*]-}"
}

# Обе цепочки создаём идемпотентно и сразу чистим: start зовут повторно
# (systemd restart, ExecStartPost mihomo, повторный запуск установщика).
ensure_chain() { # ensure_chain <цепочка>
  iptables -t mangle -nL "$1" >/dev/null 2>&1 || iptables -t mangle -N "$1"
  iptables -t mangle -F "$1"
}

remove_jumps() { # remove_jumps <хук> <цепочка> — снять все прыжки, в т.ч. старые
  while iptables -t mangle -D "$1" -j "$2" 2>/dev/null; do :; done
}

remove_ip_rule() {
  # Правил с одним priority может быть несколько (наследие смены метки) —
  # сносим все, иначе останется мусор на старую метку.
  while ip rule del priority "$WAN_INGRESS_RULE_PRIO" 2>/dev/null; do :; done
}

cleanup() {
  remove_jumps PREROUTING "$CHAIN"
  remove_jumps OUTPUT "$OUT_CHAIN"
  # Цепочки может уже не быть (stop дважды подряд) — это не ошибка.
  iptables -t mangle -F "$CHAIN" 2>/dev/null || true
  iptables -t mangle -X "$CHAIN" 2>/dev/null || true
  iptables -t mangle -F "$OUT_CHAIN" 2>/dev/null || true
  iptables -t mangle -X "$OUT_CHAIN" 2>/dev/null || true
  remove_ip_rule
}

start() {
  take_lock
  if [ "$WAN_INGRESS" != "yes" ]; then
    # Выключение должно быть полным: если раньше WAN_INGRESS был yes, после
    # правки .env и restart не должно остаться ни цепочек, ни правила.
    cleanup
    echo "ok: WAN_INGRESS=no, доступ из WAN не настраивается"
    return 0
  fi

  parse_ports || die "WAN_PORTS='${WAN_PORTS}' содержит недопустимые значения — исправьте $ETC/.env"
  validate_mark

  ensure_chain "$CHAIN"
  ensure_chain "$OUT_CHAIN"

  # Снимаем все старые прыжки (в т.ч. оставшиеся от прежних версий) и ставим
  # ровно один: иначе пакет проходит цепочку несколько раз.
  remove_jumps PREROUTING "$CHAIN"
  iptables -t mangle -I PREROUTING 1 -j "$CHAIN"

  remove_jumps OUTPUT "$OUT_CHAIN"
  iptables -t mangle -I OUTPUT 1 -j "$OUT_CHAIN"

  # 1. Восстановление метки — строго первым. Ответ контейнера приходит на
  #    br-интерфейс с приватным src; если нижестоящий RETURN для приватных
  #    сетей сработает раньше, метка потеряется и ответ уйдёт в TUN.
  iptables -t mangle -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED \
    -j CONNMARK --restore-mark --nfmask "$WAN_INGRESS_MARK" --ctmask "$WAN_INGRESS_MARK"

  # 2. Трафик самого mihomo из TUN не метим: иначе соединение, инициированное
  #    через прокси на тот же порт, получит метку, и его ответ пойдёт мимо
  #    прокси (напрямую из main).
  if [ -n "$TUN_IFACE" ]; then
    iptables -t mangle -A "$CHAIN" -i "$TUN_IFACE" -j RETURN
  fi

  # 3. Локальный трафик не является входящим из WAN.
  iptables -t mangle -A "$CHAIN" -i lo -j RETURN

  # 4. Приватные источники — это LAN/VPN/сам хост, а не WAN. RETURN только
  #    для NEW: ESTABLISHED уже обработан restore-mark выше.
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 \
    100.64.0.0/10 169.254.0.0/16 127.0.0.0/8; do
    iptables -t mangle -A "$CHAIN" -m conntrack --ctstate NEW -s "$net" -j RETURN
  done

  # 5. Помечаем новые входящие соединения на опубликованные порты. Метка
  #    ставится на соединение (CONNMARK), а не на пакет: тогда все пакеты,
  #    включая ответы, получат её через restore-mark.
  if [ "${#PORTS[@]}" -gt 0 ]; then
    for entry in "${PORTS[@]}"; do
      iptables -t mangle -A "$CHAIN" -m conntrack --ctstate NEW \
        -p "${entry%% *}" --dport "${entry##* }" \
        -j CONNMARK --set-mark "$WAN_INGRESS_MARK/$WAN_INGRESS_MARK"
    done
  else
    # Пустой WAN_PORTS при yes — это "все порты". Опасно и почти всегда
    # ошибка конфигурации, поэтому предупреждаем громко, но не отказываем:
    # пользователь мог сознательно открыть шлюз целиком.
    warn "WAN_PORTS пуст при WAN_INGRESS=yes: помечаются ВСЕ входящие NEW-соединения с не-приватных адресов (рекомендуется перечислить порты)"
    iptables -t mangle -A "$CHAIN" -m conntrack --ctstate NEW \
      -j CONNMARK --set-mark "$WAN_INGRESS_MARK/$WAN_INGRESS_MARK"
  fi

  # OUTPUT: только восстановление метки для ответов локальных сервисов.
  # NEW-соединения здесь НЕ метим: иначе исходящие соединения самого хоста
  # на этот порт пошли бы мимо mihomo.
  iptables -t mangle -A "$OUT_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED \
    -j CONNMARK --restore-mark --nfmask "$WAN_INGRESS_MARK" --ctmask "$WAN_INGRESS_MARK"

  # Правило сносим по priority, а не по метке: при смене WAN_INGRESS_MARK
  # старое правило иначе осталось бы и уводило пакеты со старой меткой.
  remove_ip_rule
  ip rule add fwmark "$WAN_INGRESS_MARK/$WAN_INGRESS_MARK" lookup main priority "$WAN_INGRESS_RULE_PRIO"

  if [ "${#PORTS[@]}" -gt 0 ]; then
    echo "ok: доступ из WAN включён (mark=$WAN_INGRESS_MARK, prio=$WAN_INGRESS_RULE_PRIO, ports=$(ports_display), chains=$CHAIN/$OUT_CHAIN)"
  else
    echo "ok: доступ из WAN включён (mark=$WAN_INGRESS_MARK, prio=$WAN_INGRESS_RULE_PRIO, ports=все, chains=$CHAIN/$OUT_CHAIN)"
  fi
}

stop() {
  take_lock
  cleanup
  echo "ok: доступ из WAN отключён"
}

chain_exists() {
  iptables -t mangle -nL "$1" >/dev/null 2>&1
}

status() {
  local jumps_pre jumps_out rules ports_human
  parse_ports || warn "WAN_PORTS содержит недопустимые значения"

  jumps_pre=$(iptables -t mangle -S PREROUTING 2>/dev/null || true)
  jumps_out=$(iptables -t mangle -S OUTPUT 2>/dev/null || true)
  rules=$(ip rule show 2>/dev/null || true)

  if [ "${#PORTS[@]}" -gt 0 ]; then
    ports_human=$(ports_display)
  elif [ -n "$WAN_PORTS" ]; then
    ports_human="$WAN_PORTS (некорректно)"
  else
    ports_human="пусто (при WAN_INGRESS=yes помечаются все порты)"
  fi

  echo "WAN_INGRESS: $WAN_INGRESS"
  echo "WAN_INGRESS_MARK: $WAN_INGRESS_MARK"
  echo "WAN_INGRESS_RULE_PRIO: $WAN_INGRESS_RULE_PRIO"
  echo "WAN_PORTS: $ports_human"
  echo "TUN_IFACE: ${TUN_IFACE:-нет}"

  if chain_exists "$CHAIN"; then
    echo "chain $CHAIN: есть"
  else
    echo "chain $CHAIN: нет"
  fi
  if [[ "$jumps_pre" == *"-j $CHAIN"* ]]; then
    echo "jump PREROUTING -> $CHAIN: есть"
  else
    echo "jump PREROUTING -> $CHAIN: нет"
  fi

  if chain_exists "$OUT_CHAIN"; then
    echo "chain $OUT_CHAIN: есть"
  else
    echo "chain $OUT_CHAIN: нет"
  fi
  if [[ "$jumps_out" == *"-j $OUT_CHAIN"* ]]; then
    echo "jump OUTPUT -> $OUT_CHAIN: есть"
  else
    echo "jump OUTPUT -> $OUT_CHAIN: нет"
  fi

  if [[ "$rules" == *"$WAN_INGRESS_RULE_PRIO:"*"fwmark $WAN_INGRESS_MARK/$WAN_INGRESS_MARK lookup main"* ]]; then
    echo "ip rule $WAN_INGRESS_RULE_PRIO: есть"
  else
    echo "ip rule $WAN_INGRESS_RULE_PRIO: нет"
  fi

  if chain_exists "$CHAIN"; then
    echo "--- counters $CHAIN ---"
    iptables -t mangle -L "$CHAIN" -vn 2>/dev/null || true
  fi
  if chain_exists "$OUT_CHAIN"; then
    echo "--- counters $OUT_CHAIN ---"
    iptables -t mangle -L "$OUT_CHAIN" -vn 2>/dev/null || true
  fi
  return 0
}

doctor() {
  local fails=0 line prio min_2022="" route_out dev_if addr_out ip_present=0 nft_out
  local rules jumps_pre jumps_out
  local -a route_args=()

  if [ "$WAN_INGRESS" != "yes" ]; then
    echo "OK: WAN_INGRESS=no, доступ из WAN выключен (настраивать нечего)"
    return 0
  fi
  echo "OK: WAN_INGRESS=yes"
  validate_mark

  # 2-3. Цепочки и прыжки.
  jumps_pre=$(iptables -t mangle -S PREROUTING 2>/dev/null || true)
  if chain_exists "$CHAIN" && [[ "$jumps_pre" == *"-j $CHAIN"* ]]; then
    echo "OK: цепочка $CHAIN есть, прыжок из PREROUTING есть"
  else
    echo "FAIL: нет цепочки $CHAIN или прыжка из PREROUTING — входящие соединения не помечаются; выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  fi

  jumps_out=$(iptables -t mangle -S OUTPUT 2>/dev/null || true)
  if chain_exists "$OUT_CHAIN" && [[ "$jumps_out" == *"-j $OUT_CHAIN"* ]]; then
    echo "OK: цепочка $OUT_CHAIN есть, прыжок из OUTPUT есть"
  else
    echo "FAIL: нет цепочки $OUT_CHAIN или прыжка из OUTPUT — ответы локальных сервисов могут уйти в TUN; выполните systemctl restart routeweave-firewall"
    fails=$((fails + 1))
  fi

  # 4. Наше правило должно существовать ровно с той меткой и приоритетом.
  rules=$(ip rule show 2>/dev/null || true)
  if [[ "$rules" == *"$WAN_INGRESS_RULE_PRIO:"*"fwmark $WAN_INGRESS_MARK/$WAN_INGRESS_MARK lookup main"* ]]; then
    echo "OK: ip rule priority $WAN_INGRESS_RULE_PRIO с fwmark $WAN_INGRESS_MARK/$WAN_INGRESS_MARK -> main есть"
  else
    echo "FAIL: нет ip rule priority $WAN_INGRESS_RULE_PRIO с fwmark $WAN_INGRESS_MARK/$WAN_INGRESS_MARK -> main; помеченные пакеты уйдут в TUN"
    fails=$((fails + 1))
  fi

  # 5. Приоритет должен быть численно меньше правил mihomo на таблицу 2022:
  #    ip rule смотрит правила по возрастанию priority, иначе мы просто не
  #    успеем увести пакет в main.
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
  elif [ "$WAN_INGRESS_RULE_PRIO" -lt "$min_2022" ]; then
    echo "OK: priority $WAN_INGRESS_RULE_PRIO меньше минимального priority таблицы 2022 ($min_2022)"
  else
    echo "FAIL: priority $WAN_INGRESS_RULE_PRIO >= $min_2022 (правила таблицы 2022) — пакеты уйдут в TUN mihomo, а не в main; уменьшите WAN_INGRESS_RULE_PRIO"
    fails=$((fails + 1))
  fi

  # 6. Реальный маршрут помеченного пакета. SERVER_IP добавляем как from,
  #    только если он действительно есть на интерфейсе: иначе ip route get
  #    вернёт ошибку на несвязанном адресе.
  route_args=(route get 1.1.1.1 mark "$WAN_INGRESS_MARK")
  if [ -n "$SERVER_IP" ]; then
    addr_out=$(ip -4 -o addr show 2>/dev/null || true)
    if awk -v ip="$SERVER_IP" '{split($4, a, "/"); if (a[1] == ip) found = 1} END {exit !found}' <<< "$addr_out"; then
      ip_present=1
      route_args+=(from "$SERVER_IP")
    fi
  fi

  if route_out=$(ip "${route_args[@]}" 2>&1); then
    dev_if=$(awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}' <<< "$route_out")
    if [ -n "$TUN_IFACE" ] && [ "$dev_if" = "$TUN_IFACE" ]; then
      echo "FAIL: ip route get 1.1.1.1 mark $WAN_INGRESS_MARK уходит в $dev_if (TUN): метка не работает, ответы сервисов уйдут в mihomo"
      fails=$((fails + 1))
    elif [ -n "$LAN_IFACE" ] && [ "$dev_if" != "$LAN_IFACE" ]; then
      echo "WARN: помеченный маршрут идёт через ${dev_if:-неизвестный интерфейс}, а LAN_IFACE=$LAN_IFACE — проверьте маршруты (это не обязательно ошибка при нескольких NIC)"
    else
      echo "OK: ip route get 1.1.1.1 mark $WAN_INGRESS_MARK -> dev ${dev_if:-?}"
    fi
    if [ -n "$SERVER_IP" ] && [ "$ip_present" -eq 0 ]; then
      echo "WARN: SERVER_IP=$SERVER_IP не найден на интерфейсах — проверка from пропущена"
    fi
  else
    echo "FAIL: ip route get 1.1.1.1 mark $WAN_INGRESS_MARK завершился ошибкой: $route_out"
    fails=$((fails + 1))
  fi

  # 7. auto-redirect mihomo заворачивает входящий TCP в TUN ещё в PREROUTING
  #    (nat/mangle), до нашей маркировки в mangle: тогда метка не спасёт.
  if command -v nft >/dev/null 2>&1; then
    if nft_out=$(nft list ruleset 2>/dev/null); then
      if grep -i redirect <<< "$nft_out" >/dev/null; then
        echo "WARN: в nft ruleset есть правила redirect — при auto-redirect mihomo входящий трафик может перехватываться до маркировки; проверьте auto-redirect в config.yaml"
      else
        echo "OK: правил redirect в nft ruleset не найдено"
      fi
    else
      echo "WARN: nft list ruleset недоступен — проверка redirect пропущена"
    fi
  fi

  # 8. Явный auto-redirect: true в конфиге — частая причина неработающего
  #    WAN-доступа даже при корректной маркировке.
  if [ -f "$ETC/config.yaml" ]; then
    if grep -Eq '^[[:space:]]*auto-redirect:[[:space:]]*true([[:space:]]|$)' "$ETC/config.yaml"; then
      echo "WARN: в $ETC/config.yaml включён auto-redirect: true — если WAN-доступ не работает, поставьте false и перезапустите mihomo"
    else
      echo "OK: auto-redirect в $ETC/config.yaml не включён"
    fi
  fi

  # 9. Счётчики: по ним видно, доходят ли пакеты до цепочки и метятся ли они.
  if chain_exists "$CHAIN"; then
    echo "--- counters $CHAIN ---"
    iptables -t mangle -L "$CHAIN" -vn 2>/dev/null || true
  fi
  if chain_exists "$OUT_CHAIN"; then
    echo "--- counters $OUT_CHAIN ---"
    iptables -t mangle -L "$OUT_CHAIN" -vn 2>/dev/null || true
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
  status) status ;;
  doctor) doctor ;;
  *)
    echo "usage: $0 start|stop|status|doctor" >&2
    exit 1
    ;;
esac
