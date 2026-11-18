#!/usr/bin/env bash
# NFQUEUE-правила для nfqws: первые пакеты исходящих TCP 80/443 через WAN.
# nfqws сам читает SNI/Host и десинкает только домены из hostlist,
# остальной трафик проходит без изменений.
#
# Профили zapret (необязательные): /etc/routeweave/zapret.d/<name>.conf.
# Каждый профиль — свой список доменов, своя стратегия и свой NFQUEUE.
# Правила — клоны с одинаковым матчем, различаются только --queue-num.
# Порядок: default (QNUM из zapret.conf) ПЕРВЫМ, затем профили по имени файла.
# Так проверено вживую: если split-профиль поставить перед default, YouTube
# отдаёт 000/4 (TLS-хендшейк рвётся). hostlist default генерируется из
# SRC_HOSTLIST МИНУС все домены профилей (равные и поддомены), поэтому
# пересечений в норме нет; если домен вручную попал в оба списка, его
# обработает default — он первый.
#
# Использование: fw-nfq.sh start|stop
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
# shellcheck disable=SC1091
if [ -f "$ETC/zapret.conf" ]; then . "$ETC/zapret.conf"; fi

warn() { echo "warn: $*" >&2; }

# Сравнение символов по ASCII: сортировка профилей и разбор имён не должны
# зависеть от локали пользователя/сервиса.
LC_ALL=C

QNUM="${QNUM:-200}"
# || true обязателен: без маршрута ip route get падает, а с pipefail+set -e
# упал бы весь скрипт ещё до проверки WAN_IFACE (stop должен быть безопасным).
WAN_IFACE="${WAN_IFACE:-$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true)}"
SRC_HOSTLIST="${SRC_HOSTLIST:-$ETC/lists/dpi.txt}"
HOSTLIST="${HOSTLIST:-/run/routeweave/dpi-zapret.txt}"
PROFILE_DIR="${PROFILE_DIR:-$ETC/zapret.d}"
HOSTLIST_DIR="${HOSTLIST_DIR:-/run/routeweave}"
LIST_DIR="${LIST_DIR:-$ETC/lists}"

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

# Дефолтная очередь тоже должна быть числом: иначе правило не собрать, а
# ошибка должна быть видна в журнале, а не молча ломать firewall.
if [[ ! $QNUM =~ ^[0-9]+$ ]]; then
  warn "QNUM='$QNUM' не число — беру 200"
  QNUM=200
fi

# Профили читаются вручную (KEY=VALUE, одна пара кавычек), без `.`/source:
# файл профиля — данные, а не shell. Иначе `$(...)` в DESYNC/EXTRA выполнился
# бы от root ещё до проверки QNUM/LIST.
P_LIST=""
P_QNUM=""
load_profile_conf() { # load_profile_conf <файл>
  local file="$1" line key val lineno=0
  P_LIST=""
  P_QNUM=""
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    if [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key=${BASH_REMATCH[1]}
      val=${BASH_REMATCH[2]}
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      if [[ $val == \"*\" && ${#val} -ge 2 ]]; then
        val=${val:1:${#val}-2}
      elif [[ $val == \'*\' && ${#val} -ge 2 ]]; then
        val=${val:1:${#val}-2}
      fi
      case $key in
        LIST) P_LIST="$val" ;;
        QNUM) P_QNUM="$val" ;;
        *) : ;; # DESYNC/FOOLING/EXTRA читает nfqws-launch.sh
      esac
    else
      warn "строка $lineno в $file не похожа на KEY=VALUE — пропущена"
    fi
  done < "$file"
}

# Массивы валидных профилей (порядок — по имени файла, сортировка glob).
PROFILE_NAMES=()
PROFILE_QNUMS=()
PROFILE_LISTS=()
ALL_QNUMS=()

load_profiles() {
  local files=() file name qnum
  local qnum_seen=" $QNUM "
  PROFILE_NAMES=()
  PROFILE_QNUMS=()
  PROFILE_LISTS=()
  # default участвует в проверке очередей наравне с профилями.
  ALL_QNUMS=("$QNUM")
  [ -d "$PROFILE_DIR" ] || return 0

  shopt -s nullglob
  files=("$PROFILE_DIR"/*.conf)
  shopt -u nullglob

  for file in "${files[@]}"; do
    name=${file##*/}
    name=${name%.conf}
    if [[ ! $name =~ ^[a-z0-9][a-z0-9-]{0,15}$ ]] || [ "$name" = "default" ]; then
      warn "профиль '$name': недопустимое имя (a-z0-9/дефис, до 16 символов) — пропущен"
      continue
    fi
    load_profile_conf "$file"

    if [[ ! $P_QNUM =~ ^[0-9]+$ ]]; then
      warn "профиль '$name': QNUM='$P_QNUM' не число — пропущен"
      continue
    fi
    if [[ $qnum_seen == *" $P_QNUM "* ]]; then
      warn "профиль '$name': QNUM=$P_QNUM уже занят (default или другим профилем) — пропущен"
      continue
    fi
    if [ -z "$P_LIST" ] || [[ $P_LIST == */* || $P_LIST == *..* ]] \
       || [[ ! $P_LIST =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
      warn "профиль '$name': LIST должен быть именем файла из $LIST_DIR (без слэшей и '..') — пропущен"
      continue
    fi

    PROFILE_NAMES+=("$name")
    PROFILE_QNUMS+=("$P_QNUM")
    PROFILE_LISTS+=("$P_LIST")
    ALL_QNUMS+=("$P_QNUM")
    qnum_seen+="$P_QNUM "
  done
}

load_profiles

# hostlist для nfqws: убираем комментарии/пробелы, префиксы "+."/"."/"*.",
# приводим к нижнему регистру. Необязательный третий аргумент — файл
# исключений (нормализованные домены профилей): строка пропускается, если она
# равна исключению или является его поддоменом. Пишем во временный файл и
# переименовываем: nfqws читает список в любой момент, полузаписанный файл дал
# бы пропуски в десинке.
write_hostlist() { # write_hostlist <источник> <цель> [исключения]
  local src="$1" dst="$2" exclude="${3:-}" tmp
  [ -f "$src" ] || return 0
  [ "$dst" != "$src" ] || return 0
  install -d -m 0755 "$(dirname "$dst")"
  tmp="$(mktemp "${dst}.XXXXXX")"
  if ! awk -v exclude="$exclude" '
    function norm(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      sub(/^\*\./, "", s)
      sub(/^\*/, "", s)
      sub(/^\+\./, "", s)
      sub(/^\./, "", s)
      return tolower(s)
    }
    function covered(entry, parent,  n) {
      if (entry == parent) return 1
      n = length(parent)
      return length(entry) > n && substr(entry, length(entry) - n) == "." parent
    }
    BEGIN {
      if (exclude != "") {
        while ((getline line < exclude) > 0) {
          line = norm(line)
          if (line != "") excl[line] = 1
        }
        close(exclude)
      }
    }
    /^[[:space:]]*(#|$)/ { next }
    {
      entry = norm($0)
      if (entry == "") next
      if (exclude != "") {
        for (parent in excl) {
          if (covered(entry, parent)) next
        }
      }
      print entry
    }
  ' "$src" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 0644 "$tmp"
  mv -f "$tmp" "$dst"
}

# default-hostlist = lists/dpi.txt минус домены профилей. Исключения собираем
# из уже нормализованных hostlist'ов профилей: так сравнение идёт по тем же
# именам, что увидит nfqws. Профильные списки генерируются первыми, default —
# последним (он и в правилах идёт первым, и получает остаток доменов).
generate_hostlists() {
  local i src dst exclude
  install -d -m 0755 "$HOSTLIST_DIR" "$(dirname "$HOSTLIST")"
  exclude="$(mktemp "$HOSTLIST_DIR/.dpi-exclude.XXXXXX")" || exit 1
  : > "$exclude"
  for i in "${!PROFILE_NAMES[@]}"; do
    src="$LIST_DIR/${PROFILE_LISTS[$i]}"
    dst="$HOSTLIST_DIR/dpi-zapret-${PROFILE_NAMES[$i]}.txt"
    if [ ! -f "$src" ]; then
      warn "профиль ${PROFILE_NAMES[$i]}: список $src не найден — hostlist профиля пуст"
      rm -f "$dst"
      continue
    fi
    if ! write_hostlist "$src" "$dst"; then
      rm -f "$exclude"
      exit 1
    fi
    cat "$dst" >> "$exclude"
  done
  if ! write_hostlist "$SRC_HOSTLIST" "$HOSTLIST" "$exclude"; then
    rm -f "$exclude"
    exit 1
  fi
  rm -f "$exclude"
}

# Сверяем --qnum запущенного nfqws с очередями правил. Если процесс слушает
# очередь, которой нет в конфиге (или наоборот, правило шлёт в очередь без
# читателя), DPI-десинк молча не работает. nfqws может быть ещё не запущен
# (юнит стартует раньше) — тогда тихо.
check_nfqws_qnum() {
  local pid exe line qnum allowed found
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
    found=0
    for allowed in ${ALL_QNUMS[@]+"${ALL_QNUMS[@]}"}; do
      [ "$qnum" = "$allowed" ] && found=1 && break
    done
    if [ "$found" -eq 0 ]; then
      warn "nfqws слушает очередь $qnum, которой нет в правилах (${ALL_QNUMS[*]:-нет}) — перезапустите zapret-nfqws"
    fi
  done < <(pgrep -f nfqws 2>/dev/null || true)
}

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

# Одно и то же правило для всех очередей: различается только --queue-num.
#  - TCP 80/443 — то, что nfqws умеет разбирать (HTTP Host / TLS SNI);
#  - первые 1..10 пакетов original-направления: десинк нужен только на
#    старте, а гонять через userspace весь поток дорого;
#  - пакеты с меткой nfqws (--dpi-desync-fwmark) исключены: без этого фильтра
#    поддельные пакеты возвращаются в ту же очередь, их skb держат
#    sk_wmem_alloc raw-сокета, sendto() у nfqws блокируется навсегда, очередь
#    не дренируется — дедлок, из-за которого TCP 80/443 умирает на минуты.
build_rule() { # build_rule <qnum>
  RULE=(
    -o "$WAN_IFACE"
    -p tcp
    -m multiport --dports "80,443"
    -m connbytes --connbytes 1:10 --connbytes-mode packets --connbytes-dir original
    -m mark ! --mark "$NFQWS_MARK/$NFQWS_MARK"
    -j NFQUEUE --queue-num "$1" --queue-bypass
  )
}

# Удаление ровно такой же спецификации. Цикл — потому что при неудачном
# прошлом запуске или прерванном apply правило могло продублироваться.
delete_rule() {
  [ -n "$WAN_IFACE" ] || return 0
  while iptables -w 5 -t mangle -D POSTROUTING "${RULE[@]}" 2>/dev/null; do :; done || true
}

add_rule() { # add_rule <описание>
  iptables -w 5 -t mangle -A POSTROUTING "${RULE[@]}" || {
    echo "не удалось добавить правило NFQUEUE ($1) в mangle POSTROUTING (WAN_IFACE=$WAN_IFACE)" >&2
    exit 1
  }
}

# Удаление своих правил по номерам: после удаления профиля из zapret.d его
# очередь осталась бы в цепочке, а точечный -D её уже не знает. Признак —
# WAN_IFACE, dports 80,443, connbytes, --queue-bypass и NFQUEUE. Чужие
# NFQUEUE-правила (без нашего connbytes) не трогаем.
delete_all_our_rules() {
  [ -n "$WAN_IFACE" ] || return 0
  local line number=0 numbers=()
  while IFS= read -r line; do
    case $line in -P\ *) continue ;; esac
    number=$((number + 1))
    [[ $line == *"-o $WAN_IFACE "* ]] || continue
    [[ $line == *"--dports 80,443"* ]] || continue
    [[ $line == *"--connbytes 1:10"* ]] || continue
    [[ $line == *"--queue-bypass"* ]] || continue
    [[ $line == *"-j NFQUEUE"* ]] || continue
    numbers+=("$number")
  done < <(iptables -w 5 -t mangle -S POSTROUTING 2>/dev/null || true)

  # Удаляем с конца: номера выше не смещаются от удаления нижних правил.
  local i
  for ((i = ${#numbers[@]} - 1; i >= 0; i--)); do
    iptables -w 5 -t mangle -D POSTROUTING "${numbers[$i]}" 2>/dev/null || true
  done
}

start() {
  [ -n "$WAN_IFACE" ] || { echo "WAN_IFACE not detected" >&2; exit 1; }
  check_nfqws_qnum
  generate_hostlists

  # Сначала убираем legacy-правило без метки: иначе после обновления оно
  # продолжит заворачивать поддельные пакеты nfqws в очередь.
  delete_legacy_rule

  # -w 5 во всех вызовах: Docker/ufw/другие хуки держат xtables-lock, и без
  # ожидания iptables падает с "Resource temporarily unavailable".
  local i
  for i in "${!PROFILE_NAMES[@]}"; do
    build_rule "${PROFILE_QNUMS[$i]}"
    delete_rule
  done
  build_rule "$QNUM"
  delete_rule

  # Хвосты от прежней конфигурации (в т.ч. удалённые профили).
  delete_all_our_rules

  # Вставка — критичная операция: без правила очередь не наполняется и
  # десинк молча не работает. Сначала default, затем профили: при пересечении
  # списков домен должен остаться за default, а не уйти в split-стратегию.
  build_rule "$QNUM"
  add_rule "default, qnum $QNUM"
  for i in "${!PROFILE_NAMES[@]}"; do
    build_rule "${PROFILE_QNUMS[$i]}"
    add_rule "профиль ${PROFILE_NAMES[$i]}, qnum ${PROFILE_QNUMS[$i]}"
  done

  if [ "${#PROFILE_NAMES[@]}" -gt 0 ]; then
    echo "ok: nfq rules started (профили: ${PROFILE_NAMES[*]}; default qnum $QNUM)"
  else
    echo "ok: nfq rule started (qnum $QNUM)"
  fi
}

stop() {
  local i
  # Без WAN_IFACE правило не построить (интерфейс исчез/переименован) — не
  # падаем. Заодно снимаем возможные дубли от старых запусков.
  if [ -n "$WAN_IFACE" ]; then
    # Сначала legacy-правило без метки: stop должен снимать и его.
    delete_legacy_rule
    for i in "${!PROFILE_QNUMS[@]}"; do
      build_rule "${PROFILE_QNUMS[$i]}"
      delete_rule
    done
    build_rule "$QNUM"
    delete_rule
    # Подчищаем всё наше, включая правила удалённых профилей.
    delete_all_our_rules
  fi
  echo "ok: nfq rules stopped"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 1
    ;;
esac
