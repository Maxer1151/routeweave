#!/usr/bin/env bash
# Routeweave — обёртка запуска nfqws (zapret) из /etc/routeweave/zapret.conf.
#
# Зачем нужна обёртка, если есть systemd:
#   Раньше юнит запускал `/bin/sh -c '... --dpi-desync=${DESYNC} ...
#   ${AUTOTTL_ARG} ${TTL_ARG} ${EXTRA} ...'`. Переменные подставлял шелл и он
#   же повторно разбирал результат на слова: `;`, `&&`, `$(...)`, обратные
#   кавычки, `|`, `>`, `<` в zapret.conf исполнялись от root при следующем
#   рестарте сервиса. То есть правка конфига (через бэкап, панель, чужой
#   доступ к файлу) превращалась в root-инъекцию.
#   Здесь шелл не собирает командную строку по тексту: значения проверяются
#   по whitelist символов, разбиваются на массив слов и передаются nfqws
#   напрямую через exec. Никакого eval и никакого `sh -c` — символы из
#   конфига шеллом больше не интерпретируются.
#
# Whitelist (см. RE_WORDS/RE_TOKEN ниже): A-Za-z0-9, пробел и _.,:=/+@%-.
# Всё остальное (`;`, `$`, обратная кавычка, кавычки, `&`, `|`, `>`, `<`,
# скобки, фигурные скобки, перевод строки, ...) — отказ запуска с сообщением
# в stderr и кодом 1. Лучше не запустить десинк, чем исполнить инъекцию:
# сервис останется в failed и это видно в `systemctl status zapret-nfqws`.
#
# NFQWS_BIN — законное расширение для тестов и нестандартной сборки: путь к
# бинарнику можно переопределить переменной окружения (по умолчанию
# /opt/zapret/bin/nfqws). В production юнит переменную не задаёт, поэтому
# путь остаётся прежним; в песочнице подставляется фейковый nfqws.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
NFQWS_BIN="${NFQWS_BIN:-/opt/zapret/bin/nfqws}"
CONF="$ETC/zapret.conf"

die() { echo "nfqws-launch: $*" >&2; exit 1; }
warn() { echo "nfqws-launch: предупреждение: $*" >&2; }

[ -r "$CONF" ] || die "конфиг $CONF не найден или не читается"

# Значения могут отсутствовать в конфиге — под set -u обращаемся только к
# заранее инициализированным переменным.
QNUM=""
DESYNC=""
FOOLING=""
AUTOTTL_ARG=""
TTL_ARG=""
EXTRA=""
HOSTLIST=""

# Конфиг разбирается ВРУЧНУЮ, без `. "$CONF"`.
# Почему не source: точка выполнила бы содержимое файла шеллом ДО любой
# проверки — `$(...)` и обратные кавычки в значении сработали бы от root
# раньше, чем whitelist успел бы их заметить, и вся защита ниже потеряла бы
# смысл. Разбор повторяет семантику systemd EnvironmentFile: KEY=VALUE,
# одна пара кавычек снимается, подстановок и исполнения нет.
load_conf() { # load_conf <файл>
  local file="$1" line key val lineno=0
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}                       # конфиг мог быть сохранён в CRLF
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    if [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key=${BASH_REMATCH[1]}
      val=${BASH_REMATCH[2]}
      # пробелы вокруг значения не значимы
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      # снимаем ровно одну пару обрамляющих кавычек
      if [[ $val == \"*\" && ${#val} -ge 2 ]]; then
        val=${val:1:${#val}-2}
      elif [[ $val == \'*\' && ${#val} -ge 2 ]]; then
        val=${val:1:${#val}-2}
      fi
      case $key in
        QNUM | DESYNC | FOOLING | AUTOTTL_ARG | TTL_ARG | EXTRA | HOSTLIST)
          printf -v "$key" '%s' "$val"
          ;;
        *) : ;; # WAN_IFACE, SRC_HOSTLIST и прочее лаунчеру не нужны
      esac
    else
      warn "строка $lineno в $CONF не похожа на KEY=VALUE — пропущена"
    fi
  done < "$file"
}

load_conf "$CONF"

# Сравнение символов по ASCII: локаль пользователя/сервиса не должна влиять
# на диапазоны A-Z/a-z в регулярках.
LC_ALL=C

# RE_WORDS — значения, которые разрешено разбивать на несколько аргументов
# (EXTRA и т.п.). RE_TOKEN — значения, уходящие в nfqws ровно одним argv
# (--opt=value), поэтому пробелы и управляющие символы в них запрещены.
RE_WORDS='^[A-Za-z0-9 _.,:=/+@%-]*$'
RE_TOKEN='^[A-Za-z0-9_.,:=/+@%-]+$'

check_words() { # check_words <имя> <значение>
  local name="$1" val="${2-}"
  [[ $val =~ $RE_WORDS ]] && return 0
  # %q — чтобы недопустимые байты (в т.ч. ESC) не попали в терминал как есть.
  die "недопустимые символы в $name (разрешены только A-Za-z0-9, пробел и _.,:=/+@%-): $(printf '%q' "$val")"
}

check_token() { # check_token <имя> <значение> (пустое значение допустимо)
  local name="$1" val="${2-}"
  [ -n "$val" ] || return 0
  [[ $val =~ $RE_TOKEN ]] && return 0
  die "недопустимое значение $name (разрешены только A-Za-z0-9 и _.,:=/+@%-): $(printf '%q' "$val")"
}

# Значения, которые уходят в nfqws массивом слов.
check_words EXTRA "$EXTRA"
check_words AUTOTTL_ARG "$AUTOTTL_ARG"
check_words TTL_ARG "$TTL_ARG"
# Значения, которые уходят в nfqws одним аргументом ( --opt=value ).
check_token QNUM "$QNUM"
check_token DESYNC "$DESYNC"
check_token FOOLING "$FOOLING"
check_token HOSTLIST "$HOSTLIST"

[ -n "$QNUM" ] || die "QNUM пустой — укажите номер NFQUEUE в $CONF (например QNUM=200)"
[ -n "$DESYNC" ] || die "DESYNC пустой — укажите стратегию в $CONF (например DESYNC=fake,multidisorder)"
[ -n "$HOSTLIST" ] || die "HOSTLIST пустой — укажите путь к списку доменов в $CONF"
[[ $QNUM =~ ^[0-9]+$ ]] || die "QNUM должен быть числом, получено: $(printf '%q' "$QNUM")"

# Globbing выключен: даже если whitelist когда-то расширят, `*` из конфига не
# развернётся в файлы текущего каталога.
set -f

# Порядок аргументов — как в прежнем ExecStart (qnum, desync, fooling,
# autottl, ttl, extra, hostlist): nfqws принимает опции в любом порядке, но
# сохраняем привычный вид `ps`/логов.
args=(
  --qnum="$QNUM"
  --dpi-desync="$DESYNC"
  --dpi-desync-fooling="$FOOLING"
)

# ${arr[@]+"${arr[@]}"} — безопасное добавление пустого массива под set -u.
if [ -n "$AUTOTTL_ARG" ]; then
  read -r -a tmp <<< "$AUTOTTL_ARG"
  args+=(${tmp[@]+"${tmp[@]}"})
fi
if [ -n "$TTL_ARG" ]; then
  read -r -a tmp <<< "$TTL_ARG"
  args+=(${tmp[@]+"${tmp[@]}"})
fi
if [ -n "$EXTRA" ]; then
  read -r -a tmp <<< "$EXTRA"
  args+=(${tmp[@]+"${tmp[@]}"})
fi
args+=(--hostlist="$HOSTLIST")

# hostlist готовит ExecStartPre (fw-nfq.sh start). Его может ещё не быть при
# ручном запуске — это не повод падать, но в логах должно быть видно.
[ -f "$HOSTLIST" ] || warn "hostlist $HOSTLIST пока отсутствует (nfqws прочитает его при появлении)"

[ -x "$NFQWS_BIN" ] || die "бинарник nfqws не найден или не исполняемый: $NFQWS_BIN"

exec "$NFQWS_BIN" "${args[@]}"
