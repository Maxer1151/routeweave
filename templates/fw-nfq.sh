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

# Метка, которой nfqws помечает поддельные пакеты (--dpi-desync-fwmark).
# По ней же правило ниже исключает фейки из NFQUEUE. Источники по приоритету:
# NFQWS_MARK из zapret.conf, затем значение из EXTRA (старые установки без
# отдельного ключа), затем 0x80000.
DEFAULT_NFQWS_MARK=0x80000
resolve_nfqws_mark() {
  local mark="${NFQWS_MARK:-}" src="NFQWS_MARK"
  if [ -z "$mark" ]; then
    if [[ "${EXTRA:-}" =~ --dpi-desync-fwmark=([^[:space:]]+) ]]; then
      mark="${BASH_REMATCH[1]}"
      src="EXTRA"
    elif [[ "${EXTRA:-}" =~ --dpi-desync-fwmark[[:space:]]+([^[:space:]]+) ]]; then
      mark="${BASH_REMATCH[1]}"
      src="EXTRA"
    fi
  fi
  if [ -z "$mark" ]; then
    NFQWS_MARK="$DEFAULT_NFQWS_MARK"
    return 0
  fi
  if [[ "$mark" =~ ^0[xX][0-9a-fA-F]+$ || "$mark" =~ ^[0-9]+$ ]]; then
    NFQWS_MARK="$mark"
  else
    warn "$src='$mark' не похож на число, беру $DEFAULT_NFQWS_MARK"
    NFQWS_MARK="$DEFAULT_NFQWS_MARK"
  fi
}
resolve_nfqws_mark

# Правило заворачивает в NFQUEUE только начало соединения:
#  - TCP 80/443 — то, что nfqws умеет разбирать (HTTP Host / TLS SNI);
#  - первые 1..10 пакетов original-направления: десинк нужен только на
#    старте, а гонять через userspace весь поток дорого;
#  - пакеты с меткой nfqws (--dpi-desync-fwmark) исключены: без этого фильтра
#    поддельные пакеты возвращаются в ту же очередь, их skb держат
#    sk_wmem_alloc raw-сокета, sendto() у nfqws блокируется навсегда, очередь
#    не дренируется — дедлок, из-за которого TCP 80/443 умирает на минуты.
RULE=(
  -p tcp
  -m multiport --dports "80,443"
  -m connbytes --connbytes 1:10 --connbytes-mode packets --connbytes-dir original
  -m mark ! --mark "$NFQWS_MARK/$NFQWS_MARK"
  -j NFQUEUE --queue-num "$QNUM" --queue-bypass
)

# Удаление правила старых версий — без исключения метки. На обновлённой
# установке оно осталось бы в POSTROUTING, ловило фейки nfqws и возвращало
# дедлок, даже когда новое правило добавлено.
delete_legacy_rule() {
  [ -n "$WAN_IFACE" ] || return 0
  iptables -w 5 -t mangle -D POSTROUTING -o "$WAN_IFACE" \
    -p tcp -m multiport --dports 80,443 \
    -m connbytes --connbytes 1:10 --connbytes-mode packets --connbytes-dir original \
    -j NFQUEUE --queue-num "$QNUM" --queue-bypass 2>/dev/null || true
}

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

  # -w 5 во всех вызовах: Docker/ufw/другие хуки держат xtables-lock, и без
  # ожидания iptables падает с "Resource temporarily unavailable". Раньше это
  # давало частично применённые правила при формально успешном скрипте.
  # Сначала снимаем legacy-правило без метки: иначе после обновления оно
  # продолжит заворачивать поддельные пакеты nfqws в очередь.
  delete_legacy_rule
  if ! iptables -w 5 -t mangle -C POSTROUTING -o "$WAN_IFACE" "${RULE[@]}" 2>/dev/null; then
    # Вставка — критичная операция: без правила очередь не наполняется и
    # десинк молча не работает. Падаем, чтобы юнит ушёл в failed.
    iptables -w 5 -t mangle -I POSTROUTING 1 -o "$WAN_IFACE" "${RULE[@]}" || {
      echo "не удалось вставить правило NFQUEUE в mangle POSTROUTING (WAN_IFACE=$WAN_IFACE, QNUM=$QNUM)" >&2
      exit 1
    }
  fi
}

stop() {
  # Без WAN_IFACE правило не построить (интерфейс исчез/переименован) — не
  # падаем. Заодно снимаем возможные дубли от старых запусков.
  if [ -n "$WAN_IFACE" ]; then
    # Сначала legacy-правило без метки: stop должен снимать и его.
    delete_legacy_rule
    # Удаление необязательно (правила может не быть) — поэтому || true, но с
    # -w 5, чтобы не гонять гонку с параллельными правками iptables.
    while iptables -w 5 -t mangle -D POSTROUTING -o "$WAN_IFACE" "${RULE[@]}" 2>/dev/null; do :; done || true
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
