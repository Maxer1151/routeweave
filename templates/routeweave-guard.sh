#!/usr/bin/env bash
# Сторож инвариантов маршрутизации Routeweave.
#
# Зачем он нужен. mihomo умеет перезапускаться внутри процесса (POST /restart),
# но хуки systemd при этом не выполняются: ExecStartPost=-/etc/routeweave/fw-wan.sh
# start остаётся в стороне, а nfqws после такого рестарта 1-2 секунды сыпет
# "nfq_handle_packet result -1", пока правило NFQUEUE не вернётся. В старых
# установках iptables без -w 5 мог молча не вставить правило NFQUEUE, а юнит
# оставался зелёным: ошибки не было ни в логе, ни в systemctl status. Наконец,
# ключевое состояние маршрутизации (ip_forward, nft-таблица mihomo с цепочками,
# default в таблице 2022, привязка очереди nfqws, ip rule 8993 для WAN_INGRESS)
# никто не контролирует после установки: доктора запускаются руками.
#
# Сторож периодически проверяет эти инварианты и чинит пропавшее, журналируя,
# что именно пропало. Починка ограничена кулдауном на вид действия и общим
# предохранителем, чтобы не устроить шторм рестартов при неустранимой поломке.
#
# Использование: routeweave-guard.sh [check|repair]
#   check  — только проверить, ничего не менять и не создавать;
#   repair — проверить и починить (по умолчанию).
#
# Внешние команды (systemctl, nft, iptables, ip, sysctl, timeout) вызываются
# строго через PATH: в тестах и на стендах PATH подменяется фейками. Скрипты
# самого Routeweave — только по абсолютным путям из /etc/routeweave.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# Каталог состояния сторожа. В check не создаётся вовсе.
STATE_DIR="${GUARD_STATE_DIR:-/run/routeweave/guard}"
NFQ_SCRIPT="${NFQ_SCRIPT:-$ETC/fw-nfq.sh}"
WAN_SCRIPT="${WAN_SCRIPT:-$ETC/fw-wan.sh}"
NFQ_QUEUE_FILE="${NFQ_QUEUE_FILE:-/proc/net/netfilter/nfnetlink_queue}"
COOLDOWN="${GUARD_COOLDOWN:-600}"
MAX_ACTIONS="${GUARD_MAX_ACTIONS:-4}"
# Окно предохранителя: сколько секунд живут записи о действиях.
FUSE_WINDOW=3600
WAIT_TRIES="${GUARD_WAIT_TRIES:-10}"
WAIT_SLEEP="${GUARD_WAIT_SLEEP:-1}"
# Пауза после неэффективного рестарта: если рестарт не восстановил трафик,
# значит причина не в nfqws, и повторять его каждые 600 с бессмысленно — это
# рвёт очередь клиентам и засоряет журнал. Маркер живёт STUCK_TTL секунд.
STUCK_TTL="${GUARD_STUCK_TTL:-21600}"
# Функциональные пробы: структурные инварианты могут быть в порядке, а трафик
# через очередь 200 — нет. Тогда это видно только реальным запросом.
PROBES="${GUARD_PROBE:-yes}"
PROBE_PING_HOST="${GUARD_PROBE_PING_HOST:-1.1.1.1}"
PROBE_PLAIN_URL="${GUARD_PROBE_PLAIN_URL:-https://ya.ru/}"
PROBE_DPI_URL="${GUARD_PROBE_DPI_URL:-https://discord.com/}"
# Адрес, через который проверяется десинк: домен из dpi.txt. Без десинка он
# в РФ не открывается, с работающим nfqws — отдаёт 200.

# QNUM читаем ровно как fw-nfq.sh: тот же trust-конфиг, тот же номер очереди.
# Источник истины один, иначе сторож проверял бы не ту очередь.
# shellcheck disable=SC1091
if [ -f "$ETC/zapret.conf" ]; then . "$ETC/zapret.conf"; fi

warn() { echo "warn: $*" >&2; }
ok() { echo "ok: $*"; }
fail() { echo "error: $*" >&2; }

QNUM="${QNUM:-200}"
if [[ ! "$QNUM" =~ ^[0-9]+$ ]]; then
  warn "QNUM='$QNUM' из $ETC/zapret.conf не число, проверяю очередь 200"
  QNUM=200
fi

# Числовые настройки: мусор в окружении не должен ломать арифметику ниже.
[[ "$COOLDOWN" =~ ^[0-9]+$ ]] || {
  warn "GUARD_COOLDOWN='$COOLDOWN' не число, беру 600"
  COOLDOWN=600
}
[[ "$MAX_ACTIONS" =~ ^[0-9]+$ ]] || {
  warn "GUARD_MAX_ACTIONS='$MAX_ACTIONS' не число, беру 4"
  MAX_ACTIONS=4
}
[[ "$WAIT_TRIES" =~ ^[0-9]+$ ]] || {
  warn "GUARD_WAIT_TRIES='$WAIT_TRIES' не число, беру 10"
  WAIT_TRIES=10
}
[[ "$WAIT_SLEEP" =~ ^[0-9]+$ ]] || {
  warn "GUARD_WAIT_SLEEP='$WAIT_SLEEP' не число, беру 1"
  WAIT_SLEEP=1
}

# Разумные границы паузы: меньше пяти минут она не имеет смысла (кулдаун и
# так 600 с), больше недели — уже вечное «не чиню». Не больше семи цифр —
# чтобы гигантское число не сломало арифметику bash.
if ! [[ "$STUCK_TTL" =~ ^[0-9]{1,7}$ ]] || [ "$STUCK_TTL" -lt 300 ] \
  || [ "$STUCK_TTL" -gt 604800 ]; then
  warn "GUARD_STUCK_TTL='$STUCK_TTL' вне 300..604800, беру 21600"
  STUCK_TTL=21600
fi

# --- проверка настроек проб ------------------------------------------------
# URL должен быть https-адресом: так в curl не попадёт флаг вместо адреса и не
# уйдёт запрос по http. Ошибка в guard.env не должна выключать пробы молча.
https_url_ok() {
  case "$1" in
    https://?*) return 0 ;;
    *) return 1 ;;
  esac
}

if ! https_url_ok "$PROBE_PLAIN_URL"; then
  warn "GUARD_PROBE_PLAIN_URL='$PROBE_PLAIN_URL' не https-URL — беру https://ya.ru/"
  PROBE_PLAIN_URL="https://ya.ru/"
fi
if ! https_url_ok "$PROBE_DPI_URL"; then
  warn "GUARD_PROBE_DPI_URL='$PROBE_DPI_URL' не https-URL — беру https://discord.com/"
  PROBE_DPI_URL="https://discord.com/"
fi

# Хост ping — только буквы, цифры, точка, дефис, двоеточие: значение из
# окружения не должно превратиться в дополнительный флаг ping.
case "$PROBE_PING_HOST" in
  ''|-*|*[!A-Za-z0-9._:-]*)
    warn "GUARD_PROBE_PING_HOST='$PROBE_PING_HOST' не похож на хост — беру 1.1.1.1"
    PROBE_PING_HOST="1.1.1.1"
    ;;
esac

case "$PROBES" in
  yes|no) ;;
  *)
    warn "GUARD_PROBE='$PROBES' не yes/no — пробы включены"
    PROBES=yes
    ;;
esac

PROBLEMS=0
PROBLEM_TEXTS=()
PROBE_OUT=""
PROBE_RC=0
REASON=""
STATE=0
# 1 — подавить вывод problem/report (на время внутренних проверок).
QUIET=0
# 1 — предохранитель уже сработал: больше ни одного действия до конца прогона.
FUSE_BLOWN=0
# Заполняется проверкой nfqws: юнит inactive (выключен руками), проверку
# пропустили. Для failed это поле не заполняется — это поломка.
NFQWS_SKIPPED=""
# Заполняется проверкой WAN: WAN_INGRESS из вывода fw-wan.sh status.
WAN_STATUS_ON=""
# 1 — пробы уже выполнены в этом запуске: второй раз в сеть не ходим и
# счётчик неудач десинка не удваиваем.
PROBE_DONE=0
# Факты и итог проб. PROBE_STATE: off | skip | ok | plain | dpi | external.
PROBE_PING=""
PROBE_PLAIN=""
PROBE_STATE=""
PROBE_SKIP_REASON=""

problem() {
  if [ "$QUIET" -eq 0 ]; then warn "$*"; fi
  PROBLEMS=$((PROBLEMS + 1))
  PROBLEM_TEXTS+=("$*")
}

# report — строка об успешной проверке в отчётном режиме.
report() {
  if [ "$QUIET" -eq 0 ]; then ok "$*"; fi
}

# probe — единственная точка запуска внешних команд в проверках: всегда через
# timeout 10, вывод в PROBE_OUT, код в PROBE_RC. Никогда не роняет скрипт:
# отсутствие команды (127) и таймаут (124) — это проблема, а не падение.
probe() {
  PROBE_OUT=""
  PROBE_RC=0
  if PROBE_OUT=$(timeout 10 "$@" 2>&1); then
    PROBE_RC=0
  else
    PROBE_RC=$?
  fi
}

# probe_reason — почему probe не удался, по-человечески.
probe_reason() {
  case "$PROBE_RC" in
    0) echo "ok" ;;
    124) echo "таймаут 10 с" ;;
    127) echo "команда недоступна (rc=127)" ;;
    *) echo "rc=$PROBE_RC" ;;
  esac
}

# state <проверка> — результат в STATE (0 — порядок, 1 — поломка, 2 — проверить
# нельзя). Нужна, чтобы set -e не ронял сторож на ненулевом коде проверки.
state() {
  STATE=0
  if "$@"; then
    STATE=0
  else
    STATE=$?
  fi
}

# wait_until <проверка> — ждёт, пока проверка не покажет порядок: WAIT_TRIES
# попыток по WAIT_SLEEP секунд. Рестарты асинхронны: systemctl возвращается
# раньше, чем mihomo успевает создать таблицы и выполнить ExecStartPost.
wait_until() {
  local tries="$WAIT_TRIES"
  while [ "$tries" -gt 0 ]; do
    if "$@"; then return 0; fi
    tries=$((tries - 1))
    if [ "$tries" -gt 0 ]; then sleep "$WAIT_SLEEP"; fi
  done
  return 1
}

# --- состояние сторожа и защита от шторма ---------------------------------

ensure_state_dir() {
  # Создаётся только в repair: check обязан быть read-only и ничего не писать
  # в /run, иначе непривилегированный запуск смог бы оставить мусор.
  mkdir -p "$STATE_DIR" 2>/dev/null || {
    fail "не удалось создать $STATE_DIR"
    return 1
  }
  chmod 0700 "$STATE_DIR" 2>/dev/null || true
}

# prune_actions <now> — выкидывает из actions записи старше FUSE_WINDOW, чтобы
# файл не рос. Пишем во временный файл и переименовываем.
prune_actions() {
  local now="$1" file="$STATE_DIR/actions" tmp line
  [ -f "$file" ] || return 0
  tmp="$(mktemp "$STATE_DIR/actions.XXXXXX" 2>/dev/null)" || return 1
  while IFS= read -r line; do
    [[ "$line" =~ ^[0-9]+$ ]] || continue
    if [ $((now - line)) -lt "$FUSE_WINDOW" ]; then
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$file"
  chmod 0600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$file" || return 1
}

# fuse_allows <now> — не исчерпан ли предохранитель: не больше MAX_ACTIONS
# действий за FUSE_WINDOW секунд.
fuse_allows() {
  local now="$1" count=0
  # Сбой очистки не отменяет подсчёт: старые записи просто останутся в файле,
  # и предохранитель сработает раньше — это безопасная сторона.
  prune_actions "$now" || warn "не удалось обновить $STATE_DIR/actions"
  if [ -f "$STATE_DIR/actions" ]; then
    count="$(grep -cE '^[0-9]+$' "$STATE_DIR/actions" 2>/dev/null || true)"
  fi
  [ "${count:-0}" -lt "$MAX_ACTIONS" ]
}

# cooldown_active <key> <now> — было ли действие этого вида меньше COOLDOWN
# секунд назад.
cooldown_active() {
  local key="$1" now="$2" file last
  file="$STATE_DIR/$key.last"
  [ -f "$file" ] || return 1
  last="$(cat "$file" 2>/dev/null || true)"
  [[ "$last" =~ ^[0-9]+$ ]] || return 1
  [ $((now - last)) -lt "$COOLDOWN" ]
}

# --- пауза после неэффективного рестарта ----------------------------------
# Маркер <key>.stuck означает: рестарт этого вида уже пробовали, трафик не
# восстановился. Пока маркер свежее STUCK_TTL, повторно не перезапускаем.
# Пишутся маркеры только из repair; check состояние не читает и не трогает.

stuck_active() { # stuck_active <key> <now>
  local key="$1" now="$2" file last
  file="$STATE_DIR/$key.stuck"
  [ -f "$file" ] || return 1
  last="$(cat "$file" 2>/dev/null || true)"
  [[ "$last" =~ ^[0-9]+$ ]] || return 1
  [ $((now - last)) -lt "$STUCK_TTL" ]
}

mark_stuck() { # mark_stuck <key>
  local file="$STATE_DIR/$1.stuck" now
  now="$(date +%s)"
  if ! printf '%s\n' "$now" > "$file" 2>/dev/null; then
    warn "не удалось записать маркер $file"
    return 1
  fi
  chmod 0600 "$file" 2>/dev/null || warn "не удалось выставить права на $file"
}

clear_stuck() { # clear_stuck <key> — отсутствие маркера не ошибка
  rm -f "$STATE_DIR/$1.stuck" 2>/dev/null || true
}

# perform <key> <что пропало> <команда...>
# Печатает ровно одну строку "guard: <что пропало> → <действие>", пишет epoch
# в actions и <key>.last (только когда действие реально запускается) и
# выполняет команду. Кулдаун и предохранитель — отказ с сообщением в stderr.
perform() {
  local key="$1" reason="$2" now
  shift 2
  now="$(date +%s)"

  if [ "$FUSE_BLOWN" -eq 0 ] && ! fuse_allows "$now"; then
    fail "предохранитель: за последний час уже $MAX_ACTIONS действий сторожа ($STATE_DIR/actions) — больше не чиню"
    FUSE_BLOWN=1
  fi
  if [ "$FUSE_BLOWN" -eq 1 ]; then
    return 1
  fi
  if cooldown_active "$key" "$now"; then
    warn "кулдаун ${COOLDOWN}с на действие '$key' ($STATE_DIR/$key.last): повторно не выполняю"
    return 1
  fi

  # Запись состояния — часть действия: без неё кулдаун и предохранитель не
  # работают, поэтому при ошибке записи действие не запускаем.
  if ! printf '%s\n' "$now" > "$STATE_DIR/$key.last" \
    || ! printf '%s\n' "$now" >> "$STATE_DIR/actions"; then
    fail "не удалось записать состояние в $STATE_DIR — действие не выполняю"
    return 1
  fi
  echo "guard: $reason → $*"
  "$@" || {
    fail "действие не удалось: $*"
    return 1
  }
}

# --- проверки инвариантов -------------------------------------------------
# Каждая возвращает 0 (порядок), 1 (поломка, чинится) или 2 (проверить
# нельзя: нет команды или таймаут) и кладёт причину в REASON. Вслепую не
# чиним: если nft/iptables недоступны, рестарт сервиса ничего не изменит.

ip_forward_problem() {
  REASON=""
  probe sysctl -n net.ipv4.ip_forward
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="sysctl net.ipv4.ip_forward: $(probe_reason)"
    return 2
  fi
  if [ "$PROBE_OUT" != "1" ]; then
    REASON="net.ipv4.ip_forward=$PROBE_OUT (ожидается 1)"
    return 1
  fi
  return 0
}

mihomo_problem() {
  REASON=""
  probe nft list tables
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="nft list tables: $(probe_reason)"
    return 2
  fi
  if ! grep -qF 'table inet mihomo' <<< "$PROBE_OUT"; then
    REASON="нет nft-таблицы 'table inet mihomo'"
    return 1
  fi
  probe nft list table inet mihomo
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="nft list table inet mihomo: $(probe_reason)"
    return 2
  fi
  if ! grep -qF 'chain prerouting' <<< "$PROBE_OUT" \
    || ! grep -qF 'redirect' <<< "$PROBE_OUT"; then
    REASON="в таблице inet mihomo нет chain prerouting/redirect"
    return 1
  fi
  return 0
}

route2022_problem() {
  REASON=""
  probe ip -4 route show table 2022
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="ip -4 route show table 2022: $(probe_reason)"
    return 2
  fi
  if ! grep -qE '(^|[[:space:]])default([[:space:]]|$)' <<< "$PROBE_OUT"; then
    REASON="в таблице 2022 нет маршрута default"
    return 1
  fi
  return 0
}

# nfq_rule_present <текст правил> — есть ли NFQUEUE именно на нашу очередь.
# Учитываем обе формы: "--queue-num 200" и "--queue-num=200".
nfq_rule_present() {
  local text="$1" re
  grep -qF 'NFQUEUE' <<< "$text" || return 1
  re="--queue-num[= ]+${QNUM}([^0-9]|$)"
  grep -qE -e "$re" <<< "$text"
}

nfq_rule_problem() {
  REASON=""
  probe iptables -w 5 -t mangle -S POSTROUTING
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="iptables -w 5 -t mangle -S POSTROUTING: $(probe_reason)"
    return 2
  fi
  if ! nfq_rule_present "$PROBE_OUT"; then
    REASON="нет правила NFQUEUE в mangle POSTROUTING (queue $QNUM)"
    return 1
  fi
  return 0
}

# nfqws_queue_bound — в /proc/net/netfilter/nfnetlink_queue есть строка, где
# первое поле (номер очереди) равно QNUM, а второе (portid подписчика) не 0.
# Ноль означает, что очередь никто не читает: пакеты уходят в --queue-bypass.
nfqws_queue_bound() {
  local f1 f2 rest
  [ -r "$NFQ_QUEUE_FILE" ] || return 1
  while read -r f1 f2 rest; do
    [[ "$f1" =~ ^[0-9]+$ && "$f2" =~ ^[0-9]+$ ]] || continue
    [ "$f1" -eq "$QNUM" ] || continue
    if [ "$f2" -ne 0 ]; then return 0; fi
  done < "$NFQ_QUEUE_FILE"
  return 1
}

nfqws_problem() {
  REASON=""
  NFQWS_SKIPPED=""
  probe systemctl is-active zapret-nfqws.service
  # is-active для неактивного юнита возвращает ненулевой код, и это норма:
  # отделяем только «команды нет/зависла».
  if [ "$PROBE_RC" -eq 124 ] || [ "$PROBE_RC" -eq 127 ]; then
    REASON="systemctl is-active zapret-nfqws.service: $(probe_reason)"
    return 2
  fi
  if [ "$PROBE_OUT" != "active" ]; then
    # failed — это не «выключили руками» (stop даёт inactive), а упавший юнит,
    # например исчерпавший StartLimitBurst. Очередь в таком состоянии никто не
    # читает: DPI-обход молча не работает, поэтому это поломка, а не пропуск.
    if [ "$PROBE_OUT" = "failed" ]; then
      REASON="zapret-nfqws.service в состоянии failed — очередь $QNUM не читается"
      return 1
    fi
    NFQWS_SKIPPED="${PROBE_OUT:-unknown}"
    return 0
  fi
  if nfqws_queue_bound; then return 0; fi
  REASON="nfqws active, но очередь $QNUM не привязана"
  return 1
}

wan_problem() {
  REASON=""
  WAN_STATUS_ON=""
  probe "$WAN_SCRIPT" status
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="$WAN_SCRIPT status: $(probe_reason)"
    return 2
  fi
  if ! grep -qF 'WAN_INGRESS: yes' <<< "$PROBE_OUT"; then
    WAN_STATUS_ON="no"
    return 0
  fi
  WAN_STATUS_ON="yes"
  probe ip -4 rule
  if [ "$PROBE_RC" -ne 0 ]; then
    REASON="ip -4 rule: $(probe_reason)"
    return 2
  fi
  if ! grep -qE '(^|[[:space:]])8993:.*lookup main' <<< "$PROBE_OUT"; then
    REASON="WAN_INGRESS=yes, но нет ip rule 8993 с lookup main"
    return 1
  fi
  return 0
}

# mihomo_ok — оба инварианта mihomo: таблица с цепочками и default в 2022.
# Один рестарт чинит и то, и другое, поэтому и проверка общая.
mihomo_ok() {
  state mihomo_problem
  if [ "$STATE" -ne 0 ]; then return "$STATE"; fi
  route2022_problem >/dev/null
}

# --- функциональные пробы -------------------------------------------------
# Структура может быть целой, а трафик через очередь 200 — нет: правило
# NFQUEUE на месте, nfqws привязан к очереди, но новые TCP-соединения 80/443
# не открываются (так было 27.10: 765 отказов i/o timeout за 4 минуты при
# полностью зелёных структурных проверках). Поэтому после структурных
# проверок сторож делает реальные запросы.

# http_code_ok <url> — curl вернул 2xx/3xx. Код печатается в stdout через
# -w '%{http_code}', поэтому берём последнюю строку вывода и режем пробелы.
http_code_ok() {
  local code
  probe curl --max-time 8 -sS -o /dev/null -w '%{http_code}' --url "$1"
  [ "$PROBE_RC" -eq 0 ] || return 1
  code="$(printf '%s\n' "$PROBE_OUT" | tail -n 1)"
  code="${code//[[:space:]]/}"
  [[ "$code" =~ ^[23][0-9][0-9]$ ]]
}

ping_ok_now() {
  probe ping -c1 -W2 "$PROBE_PING_HOST"
  [ "$PROBE_RC" -eq 0 ]
}

plain_ok_now() { http_code_ok "$PROBE_PLAIN_URL"; }
dpi_ok_now() { http_code_ok "$PROBE_DPI_URL"; }

# probe_cycle — одна серия измерений. Никаких действий и записей: в режиме
# check сторож обязан оставаться read-only, в том числе для счётчика
# неудач десинка.
probe_cycle() {
  PROBE_PING=""
  PROBE_PLAIN=""
  PROBE_STATE=""
  PROBE_SKIP_REASON=""

  if [ "$PROBES" = "no" ]; then
    PROBE_STATE=off
    return 0
  fi
  # Пробы имеют смысл только при живом nfqws: без него TCP 80/443 не идёт
  # через очередь, и «поломка» была бы ожидаемой.
  probe systemctl is-active zapret-nfqws.service
  if [ "$PROBE_OUT" != "active" ]; then
    PROBE_STATE=skip
    PROBE_SKIP_REASON="zapret-nfqws.service не active (${PROBE_OUT:-unknown})"
    return 0
  fi

  if ping_ok_now; then PROBE_PING=ok; else PROBE_PING=fail; fi
  if plain_ok_now; then PROBE_PLAIN=ok; else PROBE_PLAIN=fail; fi

  if [ "$PROBE_PLAIN" = "fail" ]; then
    # ICMP отделяет «сломана очередь» от «нет внешней сети»: во втором случае
    # сторож не должен ничего чинить и не должен считать это своей проблемой.
    if [ "$PROBE_PING" = "ok" ]; then PROBE_STATE=plain; else PROBE_STATE=external; fi
    return 0
  fi

  if dpi_ok_now; then
    PROBE_STATE=ok
  else
    PROBE_STATE=dpi
  fi
}

# dpi_fails_next — счётчик подряд идущих неудач десинка. Возвращает 0, когда
# неудач уже минимум две: один сбой на 60-секундном периоде может быть
# случайным, а рестарт nfqws рвёт очередь всем клиентам.
dpi_fails_next() {
  local file="$STATE_DIR/probe-dpi.fail" fails=0
  if [ -r "$file" ]; then
    fails="$(cat "$file" 2>/dev/null || true)"
    [[ "$fails" =~ ^[0-9]+$ ]] || fails=0
  fi
  fails=$((fails + 1))
  printf '%s\n' "$fails" > "$file" 2>/dev/null \
    || warn "не удалось записать $file"
  [ "$fails" -ge 2 ]
}

reset_dpi_fails() {
  rm -f "$STATE_DIR/probe-dpi.fail" 2>/dev/null || true
}

# report_probes — строка отчёта по уже собранным фактам, без обращений в сеть.
report_probes() {
  case "$PROBE_STATE" in
    off) report "пробы трафика выключены (GUARD_PROBE=no)" ;;
    skip) report "пропуск: $PROBE_SKIP_REASON" ;;
    ok) report "трафик через очередь и десинк проходят" ;;
    plain) problem "TCP 80/443 через очередь не открывается (ICMP до $PROBE_PING_HOST проходит)" ;;
    dpi) problem "десинк не работает: $PROBE_DPI_URL не открывается (TCP 80/443 проходит)" ;;
    external)
      # Сбой провайдера — не поломка шлюза: warn, но без счётчика проблем,
      # иначе check падал бы на каждом обрыве связи.
      if [ "$QUIET" -eq 0 ]; then
        warn "внешняя сеть недоступна: нет ICMP до $PROBE_PING_HOST — трафик не проверяю, проблем не считаю"
      fi
      ;;
    *) warn "внутренняя ошибка: неизвестное состояние проб '$PROBE_STATE'" ;;
  esac
}

# structural_ok_quiet — структурные проверки без вывода (для гейта проб).
structural_ok_quiet() {
  local saved="$QUIET" rc=0
  QUIET=1
  check_structural
  [ "$PROBLEMS" -eq 0 ] || rc=1
  QUIET="$saved"
  return "$rc"
}

# probe_repair — пробы с починкой. Вызывается только из repair и только после
# структурных действий; в check этот код не выполняется.
probe_repair() {
  if [ "$PROBES" = "no" ]; then
    PROBE_STATE=off
    return 0
  fi
  if ! structural_ok_quiet; then
    PROBE_STATE=skip
    PROBE_SKIP_REASON="сначала структурные проверки"
    return 0
  fi

  probe_cycle
  case "$PROBE_STATE" in
    ok)
      reset_dpi_fails
      clear_stuck probe-plain
      clear_stuck probe-dpi
      return 0
      ;;
    off|skip|external) return 0 ;;
  esac

  local now
  now="$(date +%s)"

  if [ "$PROBE_STATE" = "plain" ]; then
    # Путь TCP через очередь сломан при живом ICMP — чиним сразу, как
    # структурную поломку: ждать второй неудачи здесь незачем. Но если
    # прошлый рестарт не помог, причина не в nfqws: пауза вместо шторма.
    if stuck_active probe-plain "$now"; then
      warn "путь TCP 80/443 не восстанавливается рестартом zapret-nfqws — пауза до истечения $STUCK_TTL с (маркер $STATE_DIR/probe-plain.stuck)"
      return 0
    fi
    perform probe-plain "TCP 80/443 через очередь не открывается (ICMP проходит)" \
      systemctl restart zapret-nfqws || return 1
    if ! wait_until plain_ok_now; then
      mark_stuck probe-plain || true
      fail "после рестарта zapret-nfqws TCP 80/443 не открывается; повторный рестарт — не раньше чем через $STUCK_TTL с"
      PROBE_STATE=plain
      return 1
    fi
    ok "после рестарта zapret-nfqws TCP 80/443 открывается"
    clear_stuck probe-plain
    probe_cycle
    case "$PROBE_STATE" in
      ok)
        reset_dpi_fails
        clear_stuck probe-dpi
        return 0
        ;;
      dpi) : ;;
      *) return 0 ;;
    esac
  fi

  if [ "$PROBE_STATE" != "dpi" ]; then return 0; fi

  if stuck_active probe-dpi "$now"; then
    warn "десинк не восстанавливается рестартом zapret-nfqws — пауза до истечения $STUCK_TTL с (маркер $STATE_DIR/probe-dpi.stuck)"
    return 0
  fi

  if dpi_fails_next; then
    perform probe-dpi "десинк не работает: $PROBE_DPI_URL не открывается" \
      systemctl restart zapret-nfqws || return 1
    if wait_until dpi_ok_now; then
      ok "после рестарта zapret-nfqws десинк работает"
      clear_stuck probe-dpi
      reset_dpi_fails
      PROBE_STATE=ok
      return 0
    fi
    mark_stuck probe-dpi || true
    fail "после рестарта zapret-nfqws десинк не работает; повторный рестарт — не раньше чем через $STUCK_TTL с"
    return 1
  fi
  warn "десинк не работает (первая неудача подряд) — перезапуск со второй, счётчик: $STATE_DIR/probe-dpi.fail"
  return 0
}

# --- отчёт и починка ------------------------------------------------------

check_structural() {
  PROBLEMS=0
  PROBLEM_TEXTS=()

  state ip_forward_problem
  if [ "$STATE" -eq 0 ]; then
    report "ip_forward=1"
  else
    problem "$REASON"
  fi

  state mihomo_problem
  if [ "$STATE" -eq 0 ]; then
    report "nft table inet mihomo: prerouting и redirect на месте"
  else
    problem "$REASON"
  fi

  state route2022_problem
  if [ "$STATE" -eq 0 ]; then
    report "default в таблице 2022 есть"
  else
    problem "$REASON"
  fi

  state nfq_rule_problem
  if [ "$STATE" -eq 0 ]; then
    report "правило NFQUEUE (queue $QNUM) в mangle POSTROUTING есть"
  else
    problem "$REASON"
  fi

  state nfqws_problem
  if [ "$STATE" -ne 0 ]; then
    problem "$REASON"
  elif [ -n "$NFQWS_SKIPPED" ]; then
    if [ "$QUIET" -eq 0 ]; then
      ok "пропуск: zapret-nfqws.service не active ($NFQWS_SKIPPED) — привязка очереди $QNUM не проверяется"
    fi
  else
    report "nfqws читает очередь $QNUM"
  fi

  state wan_problem
  if [ "$STATE" -ne 0 ]; then
    problem "$REASON"
  elif [ "$WAN_STATUS_ON" = "yes" ]; then
    report "WAN_INGRESS=yes и ip rule 8993 на месте"
  else
    report "WAN_INGRESS выключен — ip rule 8993 не требуется"
  fi
}

check_all() { # check_all — структурные проверки и пробы трафика
  QUIET=0
  check_structural

  if [ "$PROBE_DONE" -eq 0 ]; then
    if [ "$PROBLEMS" -eq 0 ]; then
      probe_cycle
    else
      # Структурная поломка объясняет и отсутствие трафика: в сеть не ходим,
      # чтобы не принять её за отдельную проблему.
      PROBE_STATE=skip
      PROBE_SKIP_REASON="сначала структурные проверки"
    fi
    PROBE_DONE=1
  fi
  report_probes
}

# summary — итог по счётчику проблем. 0 — всё в порядке, 1 — есть список.
summary() {
  local text
  if [ "$PROBLEMS" -eq 0 ]; then
    ok "инварианты маршрутизации в порядке"
    return 0
  fi
  fail "проблем: $PROBLEMS"
  for text in ${PROBLEM_TEXTS[@]+"${PROBLEM_TEXTS[@]}"}; do
    echo "  - $text" >&2
  done
  return 1
}

# stage <key> <проверка> <команда...>
# Условие сломано — одно действие и ожидание результата. Условие проверяется
# заново прямо перед действием: после рестарта mihomo часть проблем уходит сама
# через ExecStartPost. STATE=2 (нет команды/таймаут) — не чиним.
stage() {
  local key="$1" check="$2"
  shift 2
  state "$check"
  case "$STATE" in
    0) return 0 ;;
    2)
      warn "$REASON — действие '$key' пропущено"
      return 1
      ;;
  esac
  perform "$key" "$REASON" "$@" || return 1
  if wait_until "$check"; then return 0; fi
  fail "после действия '$key' не восстановилось: $REASON"
  return 1
}

repair() {
  ensure_state_dir || return 1
  # Порядок важен: ip_forward — база для всего; рестарт mihomo возвращает
  # таблицу nft, таблицу 2022 и (через ExecStartPost) правило WAN; NFQUEUE
  # чинится после mihomo, nfqws — после правила, WAN — последним.
  stage ipforward ip_forward_problem sysctl -w net.ipv4.ip_forward=1 || true
  stage mihomo mihomo_ok systemctl restart mihomo || true
  stage nfq nfq_rule_problem "$NFQ_SCRIPT" start || true
  stage nfqws nfqws_problem systemctl restart zapret-nfqws || true
  stage wan wan_problem "$WAN_SCRIPT" start || true
  # Функциональные пробы — после структурных: чинить нечего, пока нет правила,
  # таблицы и привязки очереди. Итог проб отчётом ниже повторно не меряем,
  # иначе счётчик неудач десинка вырос бы дважды за один запуск.
  probe_repair || true
  PROBE_DONE=1
  check_all
  summary
}

case "${1:-repair}" in
  check)
    check_all
    summary
    ;;
  repair)
    repair
    ;;
  *)
    echo "usage: $0 [check|repair]" >&2
    exit 2
    ;;
esac
