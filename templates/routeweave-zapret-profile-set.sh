#!/usr/bin/env bash
# Routeweave: атомарно обновить стратегию default или профиля zapret.
#
# Панель работает от непривилегированного routeweave и НЕ пишет zapret.conf
# напрямую: fw-nfq.sh делает `. zapret.conf` от root, поэтому право записи в
# этот файл равносильно выполнению shell от root. Панель вызывает helper через
# sudo -n, а helper сам проверяет значения и меняет только разрешённые ключи.
#
# Использование:
#   routeweave-zapret-profile-set.sh <default|profile-name> \
#     --desync <v> --fooling <v> --extra <v> [--autottl <v>] [--ttl <v>]
#
# default пишет $ETC/zapret.conf; профиль — $ETC/zapret.d/<name>.conf (файл
# должен существовать). Остальные строки и комментарии сохраняются, права и
# владелец — тоже. Никакого source/eval: файл — данные, а не скрипт.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
PROFILE_DIR="${ROUTEWEAVE_PROFILE_DIR:-$ETC/zapret.d}"

die() { echo "routeweave-zapret-profile-set: $*" >&2; exit 1; }

[ "$#" -ge 1 ] || die "укажите default или имя профиля"

TARGET="$1"
shift

# Белые списки повторяют то, что умеет nfqws-launch.sh. fake нужен для
# штатной стратегии шаблона: DESYNC=fake,multidisorder.
DESYNC_VALUES="fake multisplit multidisorder fakedsplit fakeddisorder hostfakesplit ipfrag2 udplen tamper"
FOOLING_VALUES="none md5sig badseq badsum datanoack ts hopbyhop hopbyhop2"

DESYNC=""; FOOLING=""; EXTRA=""; AUTOTTL_ARG=""; TTL_ARG=""
HAS_DESYNC=0; HAS_FOOLING=0; HAS_EXTRA=0; HAS_AUTOTTL=0; HAS_TTL=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --desync)  [ "$#" -ge 2 ] || die "для --desync нужно значение"
               [ "$HAS_DESYNC" -eq 0 ] || die "--desync указан дважды"
               DESYNC="$2"; HAS_DESYNC=1; shift 2 ;;
    --fooling) [ "$#" -ge 2 ] || die "для --fooling нужно значение"
               [ "$HAS_FOOLING" -eq 0 ] || die "--fooling указан дважды"
               FOOLING="$2"; HAS_FOOLING=1; shift 2 ;;
    --extra)   [ "$#" -ge 2 ] || die "для --extra нужно значение"
               [ "$HAS_EXTRA" -eq 0 ] || die "--extra указан дважды"
               EXTRA="$2"; HAS_EXTRA=1; shift 2 ;;
    --autottl) [ "$#" -ge 2 ] || die "для --autottl нужно значение"
               [ "$HAS_AUTOTTL" -eq 0 ] || die "--autottl указан дважды"
               AUTOTTL_ARG="$2"; HAS_AUTOTTL=1; shift 2 ;;
    --ttl)     [ "$#" -ge 2 ] || die "для --ttl нужно значение"
               [ "$HAS_TTL" -eq 0 ] || die "--ttl указан дважды"
               TTL_ARG="$2"; HAS_TTL=1; shift 2 ;;
    *) die "неизвестный аргумент: $(printf '%q' "$1")" ;;
  esac
done

[ "$HAS_DESYNC" -eq 1 ] && [ "$HAS_FOOLING" -eq 1 ] && [ "$HAS_EXTRA" -eq 1 ] \
  || die "нужны --desync, --fooling и --extra"

if [ "$TARGET" = "default" ]; then
  CONF="$ETC/zapret.conf"
else
  [[ $TARGET =~ ^[a-z0-9][a-z0-9-]{0,15}$ ]] \
    || die "недопустимое имя профиля (a-z0-9 и дефис, до 16 символов): $(printf '%q' "$TARGET")"
  CONF="$PROFILE_DIR/$TARGET.conf"
  [ -f "$CONF" ] || die "профиль '$TARGET' не найден: файл отсутствует"
fi

[ -f "$CONF" ] || die "файл конфигурации не найден: $CONF"
[ -r "$CONF" ] || die "файл конфигурации не читается: $CONF"

validate_enum() { # validate_enum <ключ> <значение> <разрешённые через пробел>
  local name="$1" value="${2-}" allowed="$3" item
  local -a items=()
  [ -n "$value" ] || die "$name пустой"
  [[ $value == *[$' \t\r\n']* ]] && die "$name не должен содержать пробелов"
  IFS=',' read -r -a items <<< "$value"
  for item in "${items[@]}"; do
    [ -n "$item" ] || die "$name: пустой элемент в списке"
    case " $allowed " in
      *" $item "*) ;;
      *) die "$name: недопустимое значение '$item'" ;;
    esac
  done
}

validate_chars() { # validate_chars <ключ> <значение> (пустое допустимо)
  local name="$1" value="${2-}" probe
  [ ${#value} -le 1024 ] || die "$name длиннее 1024 символов"
  [ -n "$value" ] || return 0
  [[ $value == *[$'\r\n']* ]] && die "$name не должен содержать переводов строки"
  # Литерал $NFQWS_MARK подставляет лаунчер; для проверки символов заменяем
  # его на число. Любой другой '$' останется и будет отклонён.
  probe=${value//\$NFQWS_MARK/0x0}
  [[ $probe =~ ^[A-Za-z0-9\ _.,:=/+@%-]*$ ]] \
    || die "$name: недопустимые символы (разрешены A-Za-z0-9, пробел и _.,:=/+@%-)"
}

validate_enum "DESYNC" "$DESYNC" "$DESYNC_VALUES"
validate_enum "FOOLING" "$FOOLING" "$FOOLING_VALUES"
validate_chars "EXTRA" "$EXTRA"
validate_chars "AUTOTTL_ARG" "$AUTOTTL_ARG"
validate_chars "TTL_ARG" "$TTL_ARG"

DIR=$(dirname "$CONF")
MODE=$(stat -c '%a' "$CONF") || die "не удалось прочитать права $CONF"
OWNER=$(stat -c '%u:%g' "$CONF") || die "не удалось прочитать владельца $CONF"

TMP=$(mktemp "$DIR/.zapret-strategy.XXXXXX") || die "не удалось создать временный файл в $DIR"
trap 'rm -f "$TMP"' EXIT

# Заменяем только указанные ключи. Неуказанные optional-ключи (autottl/ttl)
# остаются как были: helper не должен молча стирать ручную настройку.
if ! awk \
  -v desync="$DESYNC" -v fooling="$FOOLING" -v extra="$EXTRA" \
  -v autottl="$AUTOTTL_ARG" -v ttl="$TTL_ARG" \
  -v set_autottl="$HAS_AUTOTTL" -v set_ttl="$HAS_TTL" '
    BEGIN {
      n = split("DESYNC FOOLING EXTRA AUTOTTL_ARG TTL_ARG", keys, " ")
      val["DESYNC"] = desync
      val["FOOLING"] = fooling
      val["EXTRA"] = extra
      val["AUTOTTL_ARG"] = autottl
      val["TTL_ARG"] = ttl
      do_set["DESYNC"] = 1
      do_set["FOOLING"] = 1
      do_set["EXTRA"] = 1
      do_set["AUTOTTL_ARG"] = set_autottl
      do_set["TTL_ARG"] = set_ttl
    }
    {
      line = $0
      sub(/\r$/, "", line)
      for (i = 1; i <= n; i++) {
        k = keys[i]
        if (line ~ ("^[[:space:]]*" k "[[:space:]]*=")) {
          if (!done[k]) {
            done[k] = 1
            if (do_set[k]) print k "=\"" val[k] "\""
            else print line
          }
          next
        }
      }
      print line
    }
    END {
      for (i = 1; i <= n; i++) {
        k = keys[i]
        if (do_set[k] && !done[k]) print k "=\"" val[k] "\""
      }
    }
  ' "$CONF" > "$TMP"; then
  die "не удалось обновить $CONF"
fi

chmod "$MODE" "$TMP" || die "не удалось сохранить права $CONF"
# В тестах helper запускается не от root: временный файл уже принадлежит тому
# же пользователю, а chown чужого uid без CAP_CHOWN всё равно не нужен.
if [ "$(id -u)" -eq 0 ]; then
  chown "$OWNER" "$TMP" || die "не удалось сохранить владельца $CONF"
fi

mv -f "$TMP" "$CONF" || die "не удалось заменить $CONF"
trap - EXIT
exit 0
