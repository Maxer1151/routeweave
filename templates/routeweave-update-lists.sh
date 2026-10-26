#!/usr/bin/env bash
# Routeweave: ежедневное обновление geo/ru-direct для mihomo. DPI-домены исключаются из geo.
#
# Скрипт запускается от root (systemd timer и sudo из панели), но каталог
# списков принадлежит routeweave. Поэтому:
#   * все пути списков проверяются на симлинки до чтения/записи и до сети;
#   * запись идёт через staging-каталог 0700 root:root и атомарный mv, который
#     заменяет ссылку, а не идёт по ней;
#   * .env разбирается без source/eval, секреты не попадают в argv.
set -euo pipefail

ETC_DIR="${ROUTEWEAVE_ETC:-/etc/routeweave}"
L="$ETC_DIR/lists"
E="$ETC_DIR/.env"
STAGING="$ETC_DIR/.staging"

P=""
S=""
T=""

log(){ echo "[$(date '+%F %T')] $*"; }

refuse_symlink(){ # refuse_symlink <path>: работа через симлинк запрещена
  [ -L "$1" ] && { log "ОТКАЗ: $1 — симлинк"; exit 1; }
  return 0
}

ensure_staging(){ # staging 0700 root:root; без root владелец не меняется
  [ -L "$STAGING" ] && { log "ОТКАЗ: $STAGING — симлинк"; return 1; }
  if [ "$(id -u)" = 0 ]; then
    install -d -m 0700 -o root -g root "$STAGING"
  else
    install -d -m 0700 "$STAGING"
  fi
}

env_get(){ # env_get <KEY>: значение из .env без source/eval; нет строки — пусто
  local key=$1 line val
  [ -f "$E" ] || return 0
  line=$(grep -m1 -E "^${key}=" "$E" 2>/dev/null || true)
  [ -n "$line" ] || return 0
  val=${line#*=}
  case "$val" in
    \'*\')
      val=${val#\'}
      val=${val%\'}
      # В одинарных кавычках '\'' — единственный способ записать апостроф.
      val=${val//\'\\\'\'/\'}
      ;;
    \"*\")
      val=${val#\"}
      val=${val%\"}
      ;;
  esac
  printf '%s' "$val"
}

cfg_escape(){ # экранирование значения для строки конфига curl
  local s=${1-}
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '%s' "$s"
}

curl_cfg(){ # curl_cfg <proxy> <bearer>: конфиг для curl -K - (без секретов в argv)
  local proxy=${1-} bearer=${2-}
  if [ -n "$proxy" ]; then
    printf 'proxy = "%s"\n' "$(cfg_escape "$proxy")"
  fi
  if [ -n "$bearer" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$(cfg_escape "$bearer")"
  fi
}

fetch(){ # fetch <url> <out>: прямое соединение, при наличии прокси — через него
  local url=$1 out=$2 cfg
  cfg=$(curl_cfg "$P" "")
  if timeout 100 curl -fsSL --connect-timeout 10 --max-time 90 -K - "$url" -o "$out" \
      <<<"$cfg" && [ -s "$out" ]; then
    return 0
  fi
  if [ -n "$P" ]; then
    cfg=$(curl_cfg "$P" "")
    timeout 100 curl -fsSL --connect-timeout 15 --max-time 120 -K - "$url" -o "$out" \
      <<<"$cfg" && [ -s "$out" ]
  fi
}

norm(){ tr -d '\r' | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//;s/^\*\.//;s/^\+\.//;s/^\.//' | grep -E '^[A-Za-z0-9_.-]+\.[A-Za-z]{2,}$' | LC_ALL=C sort -u || true; }

put(){ # put <new> <dest> <label>: staging + chmod/chown + атомарный mv
  local n=$1 d=$2 l=$3 t
  # cmp только для обычного файла: чтение через симлинк недопустимо.
  if [ ! -L "$d" ] && [ -f "$d" ] && cmp -s "$n" "$d"; then
    log "SAME $l ($(wc -l <"$n"))"
    return 1
  fi
  ensure_staging || { log "ОШИБКА: не удалось создать $STAGING"; return 2; }
  t=$(mktemp "$STAGING/$l.XXXXXX") || { log "ОШИБКА: mktemp в $STAGING"; return 2; }
  if ! cat -- "$n" > "$t"; then
    rm -f -- "$t"
    log "ОШИБКА: копирование в $t"
    return 2
  fi
  if ! chmod 0644 "$t"; then
    rm -f -- "$t"
    log "ОШИБКА: chmod $t"
    return 2
  fi
  if [ "$(id -u)" = 0 ] && ! chown root:root "$t"; then
    rm -f -- "$t"
    log "ОШИБКА: chown $t"
    return 2
  fi
  # mv заменяет симлинк-назначение, а не пишет по нему.
  if ! mv -f -- "$t" "$d"; then
    rm -f -- "$t"
    log "ОШИБКА: mv $t -> $d"
    return 2
  fi
  log "UPD $l ($(wc -l <"$n") строк)"
  return 0
}

write_empty(){ # write_empty <dest> <tag>: пустой файл через staging + mv
  local d=$1 tag=$2 t
  ensure_staging || { log "ОШИБКА: не удалось создать $STAGING"; return 2; }
  t=$(mktemp "$STAGING/$tag.XXXXXX") || { log "ОШИБКА: mktemp в $STAGING"; return 2; }
  if ! : > "$t"; then
    rm -f -- "$t"
    log "ОШИБКА: запись $t"
    return 2
  fi
  if ! chmod 0644 "$t"; then
    rm -f -- "$t"
    log "ОШИБКА: chmod $t"
    return 2
  fi
  if [ "$(id -u)" = 0 ] && ! chown root:root "$t"; then
    rm -f -- "$t"
    log "ОШИБКА: chown $t"
    return 2
  fi
  # mv заменяет симлинк-назначение, а не пишет по нему.
  if ! mv -f -- "$t" "$d"; then
    rm -f -- "$t"
    log "ОШИБКА: mv $t -> $d"
    return 2
  fi
  return 0
}

ensure_custom_files(){ # пустые custom-списки; подменённый симлинк заменяется
  local f dst
  for f in geo-custom.txt ru-direct-custom.txt; do
    dst="$L/$f"
    # Обычный файл не трогаем; симлинк (даже на существующий файл) заменяем.
    if [ -f "$dst" ] && [ ! -L "$dst" ]; then continue; fi
    write_empty "$dst" "${f%.txt}" || return 2
    log "$f: создан пустой файл (custom не заполняется из geo/ru-direct)"
  done
  return 0
}

mihomo_api(){ # базовый адрес API mihomo из .env без завершающего /
  local api
  api=$(env_get MIHOMO_API)
  api=${api:-http://127.0.0.1:9090}
  printf '%s' "${api%/}"
}

main(){
  umask 077
  ensure_staging || { log "ОШИБКА: не удалось создать $STAGING"; exit 1; }

  refuse_symlink "$L"
  [ -d "$L" ] || { log "ОШИБКА: каталог списков не найден: $L"; exit 1; }
  local f
  for f in geo.txt ru-direct.txt geo-custom.txt ru-direct-custom.txt dpi.txt; do
    refuse_symlink "$L/$f"
  done

  local socks MIHOMO_API
  socks=$(env_get SOCKS_URL)
  # Раньше секрет приезжал как ${MIHOMO_SECRET:-} после source .env; теперь
  # значение даёт env_get: кавычки разворачиваются, команды не выполняются,
  # в окружение ничего не экспортируется.
  S=$(env_get MIHOMO_SECRET)
  case "${socks:-}" in
    socks5://*) P="socks5h://${socks#socks5://}";;
    socks5h://*) P="$socks";;
    http://*|https://*) P="$socks";;
  esac

  # Пустые custom создаются в staging и подставляются mv: custom остаётся
  # пользовательским и не заполняется из geo.txt/ru-direct.txt.
  ensure_custom_files || exit 1

  T=$(mktemp -d)
  trap 'rm -rf -- "$T"' EXIT
  local R=0 G=0 n rc=0 p code cfg st=0
  if fetch https://raw.githubusercontent.com/lib4u/amnezia-tunneling-ru/main/amnezia.json "$T/a.json" && jq -e . "$T/a.json" >/dev/null 2>&1; then
    jq -r '.[].hostname // empty' "$T/a.json" | norm > "$T/ru"; n=$(wc -l <"$T/ru")
    if [ "$n" -ge 1000 ]; then norm <"$L/ru-direct-custom.txt" > "$T/ruc" || true
      cat "$T/ru" "$T/ruc" | LC_ALL=C sort -u | sed 's/^/+./' > "$T/ruf"
      put "$T/ruf" "$L/ru-direct.txt" ru-direct || st=$?
      if [ "$st" = 0 ]; then R=1; elif [ "$st" != 1 ]; then
        log "ОШИБКА: запись ru-direct.txt"; exit 1
      fi
    else log "SKIP ru-direct: $n < 1000"; fi
  else log "SKIP ru-direct: fetch/json"; fi
  if fetch https://raw.githubusercontent.com/Internet-Helper/Unblock-for-Russia/main/geoblock.lst "$T/g.lst"; then
    norm <"$T/g.lst" > "$T/g"; n=$(wc -l <"$T/g")
    if [ "$n" -ge 50 ]; then norm <"$L/dpi.txt" > "$T/dpi" || true; norm <"$L/geo-custom.txt" > "$T/gc" || true
      cat "$T/g" "$T/gc" | LC_ALL=C sort -u | awk -v f="$T/dpi" 'BEGIN{while((getline d < f)>0)D[d]=1}{g=$0;s=0;for(x in D)if(g==x||(length(g)>length(x)&&index(g,"."x)==length(g)-length(x))){s=1;break}if(!s)print g}' | sed 's/^/+./' > "$T/gf"
      st=0
      put "$T/gf" "$L/geo.txt" geo || st=$?
      if [ "$st" = 0 ]; then G=1; elif [ "$st" != 1 ]; then
        log "ОШИБКА: запись geo.txt"; exit 1
      fi
    else log "SKIP geo: $n < 50"; fi
  else log "SKIP geo: fetch"; fi
  if [ "$R$G" = "00" ]; then log "Изменений нет"; exit 0; fi
  MIHOMO_API=$(mihomo_api)
  for p in geo-domains ru-domains; do
    cfg=$(curl_cfg "" "$S")
    code=$(printf '%s' "$cfg" | timeout 100 curl -s -o /dev/null -w '%{http_code}' -X PUT -K - "$MIHOMO_API/providers/rules/$p" || true)
    log "reload $p -> ${code:-ERR}"
    [ "$code" = 204 ] || [ "$code" = 200 ] || rc=1
  done
  if [ "$rc" != 0 ]; then
    log "API reload failed -> restart mihomo"
    timeout 20 systemctl restart mihomo || true
  fi
  log "Готово"
}

if [ "${ROUTEWEAVE_LIB_ONLY:-0}" = 1 ]; then
  # При direct-запуске return вне функции завершится ошибкой — тогда exit 0.
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi
main "$@"
