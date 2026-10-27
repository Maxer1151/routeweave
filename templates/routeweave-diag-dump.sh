#!/usr/bin/env bash
# Routeweave: текстовый снимок состояния маршрутизации для разбора сбоя
# «LAN-клиенты без интернета, сервисы на шлюзе живы».
#
# В снимок попадают только маршруты, правила, счётчики и журнал. Файлы
# /etc/routeweave/config.yaml и /etc/routeweave/.env НЕ включаются: в них
# секреты (пароль панели, ключи mihomo и прокси). Скрипт ничего не меняет.
#
# stdout: предупреждение об обрезке (если была), путь к снимку и его размер.
# Остальная диагностика идёт в stderr. Запускается от root через sudo из
# панели; без root тоже работает, chown просто пропускается.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
LOG_DIR="${DIAG_LOG_DIR:-/var/log/routeweave}"
GUARD="${ROUTEWEAVE_GUARD:-/usr/local/sbin/routeweave-guard.sh}"
MAX_BYTES="${DIAG_MAX_BYTES:-262144}"

warn(){ printf 'warn: %s\n' "$*" >&2; }
ok(){ printf 'ok: %s\n' "$*" >&2; }
fail(){ printf 'fail: %s\n' "$*" >&2; exit 1; }

TMP=""
cleanup(){ if [ -n "$TMP" ]; then rm -f -- "$TMP" 2>/dev/null || true; fi; }
trap cleanup EXIT

section(){ # section <заголовок> <команда...>: rc пишется в файл, не в stdout
  local title=$1 out="" rc=0
  shift
  printf '\n===== %s =====\n' "$title" >>"$DUMP"
  out=$(timeout 5 "$@" 2>&1) || rc=$?
  if [ -n "$out" ]; then
    printf '%s\n' "$out" >>"$DUMP"
  fi
  if [ "$rc" -ne 0 ]; then
    printf '(команда не выполнена, rc=%s)\n' "$rc" >>"$DUMP"
  fi
}

section_pipe(){ # section_pipe <заголовок> "<команда с пайпом>" [таймаут, с]
  local title=$1 script=$2 limit="${3:-5}" out="" rc=0
  printf '\n===== %s =====\n' "$title" >>"$DUMP"
  out=$(timeout "$limit" bash -o pipefail -c "$script" 2>&1) || rc=$?
  if [ -n "$out" ]; then
    printf '%s\n' "$out" >>"$DUMP"
  fi
  if [ "$rc" -ne 0 ]; then
    printf '(команда не выполнена, rc=%s)\n' "$rc" >>"$DUMP"
  fi
}

note_section(){ # note_section <заголовок> <пояснение>
  printf '\n===== %s =====\n%s\n' "$1" "$2" >>"$DUMP"
}

case "$MAX_BYTES" in
  '' | *[!0-9]*) fail "DIAG_MAX_BYTES должен быть положительным числом" ;;
esac
if [ "$MAX_BYTES" -le 0 ]; then
  fail "DIAG_MAX_BYTES должен быть больше нуля"
fi

umask 027
install -d -m 0750 "$LOG_DIR" || fail "не удалось создать каталог $LOG_DIR"
chown root:routeweave "$LOG_DIR" 2>/dev/null || true

STAMP=$(date +%Y%m%d-%H%M%S)
DUMP="$LOG_DIR/dump-$STAMP.txt"
: >"$DUMP" || fail "не удалось создать файл снимка"
chmod 0640 "$DUMP" || fail "не удалось выставить права на $DUMP"
chown root:routeweave "$DUMP" 2>/dev/null || true

note_section "Снимок Routeweave" "Создан: $(date '+%Y-%m-%d %H:%M:%S %z').
Файлы $ETC/.env и $ETC/config.yaml в снимок не включены: там секреты.
Содержимое: маршруты, правила firewall, счётчики и журнал за 10 минут."

section_pipe "Дата, uptime, uname" 'date; uptime; uname -a'
section "ip -br addr" ip -br addr
section "ip -4 rule" ip -4 rule
section "ip -4 route show table 2022" ip -4 route show table 2022
section "ip -4 route show default" ip -4 route show default

section_pipe "nft list tables" 'nft list tables | tail -n 200'
if ! command -v nft >/dev/null 2>&1; then
  note_section "nft list table inet mihomo" "nft не установлен — таблица не проверялась"
elif timeout 5 nft list tables 2>/dev/null | grep -q 'table inet mihomo'; then
  section_pipe "nft list table inet mihomo" 'nft list table inet mihomo | tail -n 300'
else
  note_section "nft list table inet mihomo" "таблицы inet mihomo нет"
fi

section_pipe "iptables -t mangle -S POSTROUTING" 'iptables -w 5 -t mangle -S POSTROUTING | tail -n 200'
section_pipe "iptables -t nat -S" 'iptables -w 5 -t nat -S | tail -n 200'
section_pipe "iptables -S FORWARD" 'iptables -w 5 -S FORWARD | tail -n 200'
section_pipe "iptables -S INPUT" 'iptables -w 5 -S INPUT | tail -n 200'

section "nfnetlink_queue" cat /proc/net/netfilter/nfnetlink_queue
section_pipe "ss -lntu" 'ss -lntu | head -n 100'
section "sysctl ip_forward и conntrack" sysctl \
  net.ipv4.ip_forward net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max

if command -v conntrack >/dev/null 2>&1; then
  section_pipe "conntrack -S" 'conntrack -S | tail -n 100'
else
  note_section "conntrack -S" "conntrack не установлен — раздел пропущен"
fi

for unit in mihomo zapret-nfqws routeweave-firewall routeweave-panel; do
  section "systemctl is-active $unit" systemctl is-active "$unit"
  section "systemctl show NRestarts,ActiveEnterTimestamp ($unit)" \
    systemctl show -p NRestarts,ActiveEnterTimestamp "$unit"
done

# Сторож: его проверка показывает, видит ли он сам поломку, а журнал — что он
# уже пытался чинить. Проверка ходит в сеть (пробы трафика), поэтому таймаут
# больше обычного; её ошибки не должны ронять снимок — section_pipe их печатает.
section_pipe "Сторож: $GUARD check" "timeout 25 '$GUARD' check" 30
section "journalctl -u routeweave-guard -n 30" \
  journalctl -u routeweave-guard -n 30 -o short-iso

section_pipe "journalctl -u mihomo -u zapret-nfqws -u routeweave-firewall" \
  "journalctl -u mihomo -u zapret-nfqws -u routeweave-firewall --since '-10 min' -o short-iso | tail -n 300"

size=$(wc -c <"$DUMP")
truncated=0
if [ "$size" -gt "$MAX_BYTES" ]; then
  TMP=$(mktemp "$LOG_DIR/.dump-trunc.XXXXXX") || fail "не удалось создать временный файл"
  tail -c "$MAX_BYTES" "$DUMP" >"$TMP" || fail "не удалось обрезать снимок"
  chmod 0640 "$TMP" || fail "не удалось выставить права на временный файл"
  chown root:routeweave "$TMP" 2>/dev/null || true
  mv -f -- "$TMP" "$DUMP" || fail "не удалось заменить обрезанный снимок"
  TMP=""
  truncated=1
fi

TMP=$(mktemp "$LOG_DIR/.last-dump.XXXXXX") || fail "не удалось создать временный файл"
cp -- "$DUMP" "$TMP" || fail "не удалось обновить last-dump.txt"
chmod 0640 "$TMP" || fail "не удалось выставить права на временный файл"
chown root:routeweave "$TMP" 2>/dev/null || true
mv -f -- "$TMP" "$LOG_DIR/last-dump.txt" || fail "не удалось обновить last-dump.txt"
TMP=""

size=$(wc -c <"$DUMP")
if [ "$truncated" -eq 1 ]; then
  printf 'warn: снимок обрезан до %s байт\n' "$MAX_BYTES"
fi
printf '%s\n' "$DUMP"
printf 'размер: %s байт\n' "$size"
ok "снимок сохранён: $DUMP"
