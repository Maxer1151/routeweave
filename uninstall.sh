#!/usr/bin/env bash
# Routeweave — удаление шлюза.
#
# Принцип: откат должен дойти до конца, даже если отдельный шаг не сработал
# (сервиса нет, ufw выключен, docker не установлен, бэкапа DNS нет). Поэтому
# здесь НЕТ `set -e` — только `set -uo pipefail`, а каждая операция обёрнута:
# ошибки считаются в ERRORS, удаление продолжается, но код возврата в конце
# будет 1. Повторный запуск безопасен.
#
# `--dry-run` только печатает план (`DRY: ...`) и ничего не меняет: его можно
# запускать без root и на временном каталоге через ROUTEWEAVE_ETC.
set -uo pipefail

# Пути можно переопределить для тестов/нестандартной установки. В проде
# значения по умолчанию совпадают с install.sh.
ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
BACKUP_DIR="${ROUTEWEAVE_BACKUP_DIR:-$ETC/backup}"
ENV_FILE="$ETC/.env"
OPT_PANEL=/opt/routeweave-panel
ZAPRET_DIR=/opt/zapret
MIHOMO_BIN=/usr/local/bin/mihomo
SUDOERS_PANEL=/etc/sudoers.d/routeweave-panel
RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/routeweave.conf
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER=routeweave
SERVICE_GROUP=routeweave
WDTT_BIN=/usr/local/bin/wdtt-server
WDTT_DIR=/etc/wdtt
OWNED_MARKER="$ETC/owned"
IP_FORWARD_ORIG="$ETC/ip_forward.orig"

# Ровно известный список юнитов: чужие *.service не трогаем, даже если они
# лежат рядом в /etc/systemd/system. mihomo/zapret обрабатываются отдельно:
# они могли существовать до установки Routeweave.
UNITS=(
  routeweave-lists-update.timer
  routeweave-lists-update.service
  routeweave-firewall.service
  zapret-nfqws.service
  mihomo.service
  routeweave-panel.service
)
HELPERS=(
  /usr/local/sbin/routeweave-tgws-restart.sh
  /usr/local/sbin/routeweave-tgws-state.sh
  /usr/local/sbin/routeweave-tailscale-state.sh
  /usr/local/sbin/routeweave-update-lists.sh
)

C_R='\033[0;31m'; C_G='\033[0;32m'; C_Y='\033[1;33m'; C_B='\033[1;34m'; C_N='\033[0m'
info() { echo -e "${C_B}[i]${C_N} $*"; }
ok()   { echo -e "${C_G}[+]${C_N} $*"; }
warn() { echo -e "${C_Y}[!]${C_N} $*"; }
err()  { ERRORS=$((ERRORS + 1)); echo -e "${C_R}[x]${C_N} $*"; }

usage() {
  cat <<'EOF'
Routeweave — удаление шлюза.

Использование:
  sudo ./uninstall.sh [--dry-run] [--purge]
  ./uninstall.sh --help

Флаги:
  --dry-run   показать план (строки "DRY: ...") и ничего не менять; root не нужен;
  --purge     удалить /etc/routeweave целиком, включая .env (секреты панели/
              прокси), config.yaml, zapret.conf, lists и backup. Явный флаг —
              согласие, отдельного подтверждения не требует;
  -h, --help  эта справка.

Без --purge:
  - если stdin — терминал, скрипт спросит: «Удалить /etc/routeweave
    (секреты панели/прокси)? [y/N]»;
  - при неинтерактивном запуске каталог НЕ удаляется, печатается
    предупреждение о ротации секретов и команда с --purge.

Переменные окружения (для тестов/нестандартной установки):
  ROUTEWEAVE_ETC         каталог конфигурации (по умолчанию /etc/routeweave);
  ROUTEWEAVE_BACKUP_DIR  каталог бэкапов (по умолчанию $ROUTEWEAVE_ETC/backup).

Что делает:
  - останавливает и отключает systemd-юниты Routeweave;
  - снимает правила firewall (fw-nfq.sh stop, fw-wan.sh stop, fw-base.sh stop);
  - возвращает net.ipv4.ip_forward из /etc/routeweave/ip_forward.orig;
  - удаляет юниты, helper-скрипты, sudoers панели, лаунчер nfqws, панель;
  - удаляет /opt/zapret и /usr/local/bin/mihomo только если маркер
    /etc/routeweave/owned подтверждает, что их поставил Routeweave;
  - останавливает и удаляет docker-контейнер routeweave-tgwsproxy;
  - возвращает DNS (/etc/resolv.conf и drop-in systemd-resolved) из бэкапа;
  - удаляет пользователя и группу routeweave;
  - убирает только scoped-правила ufw, добавленные установщиком
    (без принудительного сброса ufw и без глобальных allow).

Что НЕ делает:
  - не удаляет WDTT-Plus: если wdtt0//etc/wdtt/wdtt-server есть, ip rule 8990
    и ufw-правила WDTT сохраняются;
  - не удаляет чужие /opt/zapret, /usr/local/bin/mihomo и их юниты без
    маркера владения; если есть backup/<unit>.orig — восстанавливает его;
  - не удаляет пакет Tailscale и состояние /var/lib/tailscale;
  - не сбрасывает ufw целиком и не удаляет чужие правила;
  - без --purge не удаляет /etc/routeweave (конфиги, списки и секреты).
EOF
}

PURGE=0
DRY_RUN=0
ERRORS=0
WDTT_PRESENT=0
PURGE_REFUSED=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h | --help) usage; exit 0 ;;
    --purge) PURGE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *)
      echo "Неизвестный аргумент: $1 (см. --help)" >&2
      exit 1
      ;;
  esac
  shift
done

# Защита от опечатки в ROUTEWEAVE_ETC: purge не должен снести систему.
if [ "$PURGE" = 1 ]; then
  ETC_CANON=$ETC
  if command -v realpath >/dev/null 2>&1; then
    ETC_CANON=$(realpath -m -- "$ETC" 2>/dev/null || printf '%s' "$ETC")
  fi
  case "$ETC_CANON" in
    /*/*)
      case "$ETC_CANON" in
        /etc | /usr | /var | /root | /boot | /opt | /srv | /bin | /sbin | /lib | /lib64 | /tmp)
          err "отказ от --purge для каталога '$ETC'"
          PURGE=0
          PURGE_REFUSED=1
          ;;
      esac
      ;;
    *)
      err "отказ от --purge для каталога '$ETC' (слишком высоко в дереве)"
      PURGE=0
      PURGE_REFUSED=1
      ;;
  esac
fi

if [ "$DRY_RUN" -ne 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root (sudo ./uninstall.sh)" >&2
  exit 1
fi

# Явный --purge не спрашиваем: это уже согласие. Вопрос задаём только в
# терминале, иначе (cron/pipe/CI) молча оставляем конфиги и предупреждаем.
if [ "$PURGE" -ne 1 ] && [ "$PURGE_REFUSED" -ne 1 ] && [ -t 0 ]; then
  if [ "$ETC" = /etc/routeweave ]; then
    printf 'Удалить /etc/routeweave (секреты панели/прокси)? [y/N] '
  else
    printf 'Удалить %s (секреты панели/прокси)? [y/N] ' "$ETC"
  fi
  answer=""
  read -r answer || answer=""
  case "$answer" in
    [yY] | [yY][eE][sS] | да | Да | ДА) PURGE=1 ;;
  esac
fi

if [ "$DRY_RUN" = 1 ]; then
  info "режим --dry-run: печатаю план, изменения не выполняются"
fi

# ------------------------------------------------------- обёртки для dry-run
# Все изменяющие операции идут через них: при --dry-run печатается "DRY: ..."
# и команда не вызывается.
do_rm() {
  local p
  for p in "$@"; do
    if [ "$DRY_RUN" = 1 ]; then
      printf 'DRY: rm -f %s\n' "$p"
    elif ! rm -f -- "$p"; then
      err "не удалось удалить $p"
      return 1
    fi
  done
}

do_rm_rf() {
  local p
  for p in "$@"; do
    if [ "$DRY_RUN" = 1 ]; then
      printf 'DRY: rm -rf %s\n' "$p"
    elif ! rm -rf -- "$p"; then
      err "не удалось удалить $p"
      return 1
    fi
  done
}

do_cp_a() {
  local src=$1 dst=$2
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: cp -a %s %s\n' "$src" "$dst"
    return 0
  fi
  if ! cp -a -- "$src" "$dst"; then
    err "не удалось скопировать $src -> $dst"
    return 1
  fi
}

do_mv() {
  local src=$1 dst=$2
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: mv -f %s %s\n' "$src" "$dst"
    return 0
  fi
  if ! mv -f -- "$src" "$dst"; then
    err "не удалось переименовать $src -> $dst"
    return 1
  fi
}

do_systemctl() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: systemctl %s\n' "$*"
    return 0
  fi
  systemctl "$@" >/dev/null 2>&1
}

do_ufw() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: ufw %s\n' "$*"
    return 0
  fi
  ufw "$@" >/dev/null 2>&1
}

do_sysctl() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: sysctl %s\n' "$*"
    return 0
  fi
  sysctl "$@" >/dev/null 2>&1
}

do_ip() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: ip %s\n' "$*"
    return 0
  fi
  ip "$@" >/dev/null 2>&1
}

do_chown() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: chown %s\n' "$*"
    return 0
  fi
  chown "$@" 2>/dev/null
}

do_chmod() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: chmod %s\n' "$*"
    return 0
  fi
  chmod "$@" 2>/dev/null
}

env_get() { # env_get <ключ> [файл] -> значение из .env без кавычек
  local key="$1" file="${2:-$ETC/.env}" val=""
  [ -r "$file" ] || return 0
  val=$(sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -n1)
  val=${val%\"}
  val=${val#\"}
  printf '%s' "$val"
}

# Только имена ключей, без значений: uninstall не должен печатать секреты.
secret_keys_present() {
  local k keys=""
  for k in PANEL_PASS MIHOMO_SECRET TG_SECRET; do
    if [ -n "$(env_get "$k")" ]; then
      keys="$keys $k"
    fi
  done
  printf '%s' "${keys# }"
}

# ------------------------------------------------------- маркер владения (M-08)
# Формат /etc/routeweave/owned: "<абсолютный путь>\trouteweave|preexisting",
# по строке на путь. Комментарии (#) и пустые строки игнорируются.
# Печатает routeweave | preexisting | unknown.
owned_state() {
  local p=$1 state=""
  [ -r "$OWNED_MARKER" ] || { printf 'unknown'; return 0; }
  state=$(awk -F '\t' -v p="$p" '$1 == p { print $2; exit }' "$OWNED_MARKER" 2>/dev/null || true)
  case "$state" in
    routeweave | preexisting) printf '%s' "$state" ;;
    *) printf 'unknown' ;;
  esac
}

remove_owned_path() {
  local p=$1 state
  if [ ! -e "$p" ]; then
    info "$p отсутствует — шаг пропущен"
    return 0
  fi
  state=$(owned_state "$p")
  case "$state" in
    routeweave)
      if do_rm_rf "$p"; then
        ok "$p удалён (владелец Routeweave)"
      fi
      ;;
    preexisting)
      info "$p существовал до установки Routeweave — оставлен"
      ;;
    *)
      warn "$p: не удаляю, признак владения отсутствует в $OWNED_MARKER; удалите вручную"
      ;;
  esac
}

# Восстанавливает бэкап юнита, если установщик перезаписал чужой файл; иначе
# удаляет юнит только при наличии маркера routeweave.
remove_unit() {
  local unit=$1 path="/etc/systemd/system/$1" bak="$BACKUP_DIR/$1.orig" state
  [ -e "$path" ] || return 0
  case "$unit" in
    mihomo.service | zapret-nfqws.service)
      if [ -e "$bak" ]; then
        if do_cp_a "$bak" "$path"; then
          ok "$path восстановлен из $bak (юнит был чужим)"
          do_rm "$bak" || true
        fi
        return 0
      fi
      state=$(owned_state "$path")
      case "$state" in
        routeweave)
          if do_rm "$path"; then
            ok "$path удалён (владелец Routeweave)"
          fi
          ;;
        preexisting)
          info "$path существовал до установки Routeweave — оставлен"
          ;;
        *)
          warn "$path: не удаляю, признак владения отсутствует в $OWNED_MARKER; удалите вручную"
          ;;
      esac
      ;;
    *)
      if do_rm "$path"; then
        ok "$path удалён"
      fi
      ;;
  esac
}

# ---------------------------------------------------------------- DNS (L-07)
# Хелпер синхронизирован с install.sh: drop-in, созданный Routeweave, помечен
# маркером (или, для старых установок, имеет ровно наше содержимое).
RESOLVED_MARKER='# Generated by Routeweave installer'
is_routeweave_dropin() {
  local f=${1:-$RESOLVED_DROPIN}
  [ -f "$f" ] || return 1
  grep -qxF "$RESOLVED_MARKER" "$f" 2>/dev/null && return 0
  [ "$(grep -vE '^[[:space:]]*(#|$)' "$f" 2>/dev/null)" = "$(printf '[Resolve]\nDNSStubListener=no')" ]
}

# Что делать с drop-in: restore — вернуть чужой бэкап, remove — удалить наш,
# keep — не трогать чужой текущий файл, none — файла нет.
plan_dropin_action() {
  local cur=${1:-${RESOLVED_DROPIN:-/etc/systemd/resolved.conf.d/routeweave.conf}}
  local bak=${2:-${BACKUP_DIR:-/etc/routeweave/backup}/resolved-routeweave.conf.orig}
  if [ ! -e "$cur" ]; then
    if [ -e "$bak" ] && ! is_routeweave_dropin "$bak"; then
      printf 'restore'
    else
      printf 'none'
    fi
  elif is_routeweave_dropin "$cur"; then
    if [ -e "$bak" ] && ! is_routeweave_dropin "$bak"; then
      printf 'restore'
    else
      printf 'remove'
    fi
  else
    printf 'keep'
  fi
}

restore_dropin() {
  local cur=${1:-${RESOLVED_DROPIN:-/etc/systemd/resolved.conf.d/routeweave.conf}}
  local bak=${2:-${BACKUP_DIR:-/etc/routeweave/backup}/resolved-routeweave.conf.orig}
  local action=""
  action=$(plan_dropin_action "$cur" "$bak")
  case "$action" in
    restore)
      # Бэкап без маркера — чужая настройка, её нужно вернуть, даже если
      # текущий drop-in записан Routeweave.
      if [ "${DRY_RUN:-0}" = 1 ]; then
        printf 'DRY: cp -a %s %s (restore foreign drop-in / восстановить чужой drop-in)\n' "$bak" "$cur"
        printf 'DRY: rm -f %s\n' "$bak"
        return 0
      fi
      if do_cp_a "$bak" "$cur"; then
        ok "drop-in $cur восстановлен из бэкапа (чужая настройка)"
        do_rm "$bak" || true
      fi
      ;;
    remove)
      if [ "${DRY_RUN:-0}" = 1 ]; then
        printf 'DRY: rm -f %s (drop-in Routeweave)\n' "$cur"
        printf 'DRY: rm -f %s\n' "$bak"
        return 0
      fi
      if do_rm "$cur"; then
        ok "drop-in $cur удалён (создавался Routeweave)"
        do_rm "$bak" || true
      fi
      ;;
    keep)
      info "drop-in $cur не принадлежит Routeweave — оставлен без изменений"
      ;;
    none)
      info "drop-in $cur отсутствует — шаг пропущен"
      if [ -e "$bak" ]; then
        do_rm "$bak" || true
      fi
      ;;
  esac
}

# Восстанавливает симлинк resolv.conf, если он был симлинком до установки.
# Коды: 0 — ссылка восстановлена, 1 — записанной ссылки нет,
#       2 — цель ссылки отсутствует (оставляем обычный файл),
#       3/4 — ошибка создания/подмены.
restore_resolv_symlink() {
  local etc=${1:-/etc} linkf=$2 target abs tmp
  [ -s "$linkf" ] || return 1
  if [ "${DRY_RUN:-0}" = 1 ]; then
    target=$(head -n1 "$linkf")
    [ -n "$target" ] || return 1
    printf 'DRY: ln -sfn %s %s/resolv.conf (из %s)\n' "$target" "$etc" "$linkf"
    return 0
  fi
  target=$(head -n1 "$linkf")
  [ -n "$target" ] || return 1
  case $target in
    /*) abs=$target ;;
    *)  abs="$etc/$target" ;;
  esac
  [ -e "$abs" ] || return 2
  tmp="$etc/.resolv.conf.lnk.$$"
  ln -sfn "$target" "$tmp" 2>/dev/null || return 3
  mv -f "$tmp" "$etc/resolv.conf" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 4; }
  return 0
}

# Если wdtt0//etc/wdtt/wdtt-server есть — WDTT-Plus не наш, его маршрутизацию
# и firewall-правила сохраняем.
detect_wdtt() {
  WDTT_PRESENT=0
  if [ -d "$WDTT_DIR" ] || [ -e "$WDTT_BIN" ]; then
    WDTT_PRESENT=1
    return 0
  fi
  if command -v ip >/dev/null 2>&1 && ip link show wdtt0 >/dev/null 2>&1; then
    WDTT_PRESENT=1
  fi
}

# fw-base.sh stop сносит все ip rule priority 8990, включая правило WDTT.
# Запоминаем их до остановки firewall и возвращаем после.
capture_wdtt_ip_rules() {
  WDTT_IP_RULES=""
  [ "$WDTT_PRESENT" = 1 ] || return 0
  command -v ip >/dev/null 2>&1 || return 0
  WDTT_IP_RULES=$(ip rule show 2>/dev/null | awk '/^8990:/ && /lookup main/ { print }' || true)
}

restore_wdtt_ip_rules() {
  local line dst
  if [ -z "$WDTT_IP_RULES" ]; then
    warn "WDTT: ip rule 8990 в таблице main не найдено — восстанавливать нечего"
    return 0
  fi
  while IFS= read -r line; do
    dst=$(printf '%s\n' "$line" | sed -n 's/.* to \([^ ]*\) .*/\1/p')
    [ -n "$dst" ] || continue
    if [ "$DRY_RUN" -ne 1 ] && command -v ip >/dev/null 2>&1 \
       && ip rule show 2>/dev/null | grep -q "8990:.* to $dst .*lookup main"; then
      info "WDTT: ip rule 8990 для $dst уже на месте"
      continue
    fi
    if do_ip rule add to "$dst" lookup main priority 8990; then
      ok "WDTT: ip rule 8990 для $dst восстановлено"
    else
      err "WDTT: не удалось восстановить ip rule 8990 для $dst (ip rule add to $dst lookup main priority 8990)"
    fi
  done <<< "$WDTT_IP_RULES"
}

# Восстановление ip_forward — договор с install.sh: файл ip_forward.orig
# содержит прежнее значение 0/1. Нет файла — не угадываем и не трогаем.
restore_ip_forward() {
  local val=""
  if [ ! -e "$IP_FORWARD_ORIG" ]; then
    info "$IP_FORWARD_ORIG не найден — net.ipv4.ip_forward не меняю"
    return 0
  fi
  val=$(head -n1 "$IP_FORWARD_ORIG" 2>/dev/null | tr -d '[:space:]')
  case "$val" in
    0 | 1)
      if do_sysctl -qw "net.ipv4.ip_forward=$val"; then
        ok "net.ipv4.ip_forward возвращён в $val"
        do_rm "$IP_FORWARD_ORIG" || true
      else
        err "не удалось вернуть net.ipv4.ip_forward=$val (sysctl -w net.ipv4.ip_forward=$val)"
      fi
      ;;
    *)
      warn "в $IP_FORWARD_ORIG неожиданное значение '$val' (ожидается 0 или 1) — ip_forward не меняю"
      ;;
  esac
}

# ---------------------------------------------------------------- 1. сервисы
info "Остановка и отключение сервисов..."
for unit in "${UNITS[@]}"; do
  do_systemctl disable --now "$unit" || true
done
# Сервисы без юнитов (на случай, если юнит уже удалён, а процесс жив) —
# просто глушим, ошибки не важны.
for svc in mihomo zapret-nfqws routeweave-panel; do
  do_systemctl stop "$svc" || true
done
ok "systemd: юниты остановлены"

# ------------------------------------------------------------- 2. firewall
# До удаления файлов: скрипты идемпотентны (stop при отсутствии правила/цепи
# не падает), поэтому порядок «сначала снять правила, потом rm» безопасен.
stop_fw_script() {
  local script=$1 hint=$2
  if [ ! -x "$script" ]; then
    warn "$script не найден — $hint"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: %s stop\n' "$script"
    return 0
  fi
  "$script" stop >/dev/null 2>&1 || err "$script stop завершился с ошибкой ($hint)"
}

detect_wdtt
capture_wdtt_ip_rules

info "Снятие правил firewall..."
stop_fw_script "$ETC/fw-nfq.sh" "правило NFQUEUE снимите вручную (iptables -t mangle -S | grep NFQUEUE)"
stop_fw_script "$ETC/fw-wan.sh" "цепочки ROUTEWEAVE-WANMARK* и ip rule priority 8993 снимите вручную"
stop_fw_script "$ETC/fw-base.sh" "цепочку ROUTEWEAVE-FWD и ip rule 8990/8991/8992 снимите вручную"
ok "firewall: правила сняты"

if [ "$WDTT_PRESENT" = 1 ]; then
  warn "WDTT-Plus обнаружен и оставлен: ip rule 8990 и ufw-правила WDTT сохраняются"
  restore_wdtt_ip_rules
else
  info "WDTT-Plus не обнаружен — ip rule 8990 не восстанавливаю"
fi

# ip_forward возвращаем сразу после остановки firewall, пока .env и файлы целы.
restore_ip_forward

# --------------------------------------------------------------- 3. юниты
info "Удаление systemd-юнитов..."
for unit in "${UNITS[@]}"; do
  remove_unit "$unit"
done
do_systemctl daemon-reload || err "systemctl daemon-reload завершился с ошибкой"
do_systemctl reset-failed || true
ok "systemd: юниты удалены, конфигурация перечитана"

# --------------------------------------------------------------- 4. docker
info "Остановка tg-ws-proxy (docker)..."
# Compose интерполирует docker-compose.yml и для удаления контейнера, а порт
# объявлен как "${SERVER_IP:?...}:1443:1443". Без SERVER_IP удаление падает
# ещё до остановки контейнера, поэтому переменную надо подставить.
#
# --env-file здесь намеренно НЕ используется: .env пишется установщиком в
# одинарных кавычках с shell-экранированием '\'', а парсер env-файлов compose
# (godotenv) не обязан понимать такую запись и может упасть с «unexpected
# character» на значении с апострофом (например, пароль в SOCKS_URL). Для
# down из всего .env нужен ровно один ключ — SERVER_IP, а это IPv4-адрес без
# спецсимволов, поэтому передаём его инлайном: из окружения, иначе из .env,
# иначе 127.0.0.1 (порт при down не публикуется, значение нужно только
# модели compose и на удаление контейнера не влияет).
COMPOSE_SERVER_IP="${SERVER_IP:-}"
if [ -z "$COMPOSE_SERVER_IP" ]; then
  COMPOSE_SERVER_IP=$(env_get SERVER_IP "$ENV_FILE")
fi
# Значение из .env может быть в кавычках — для подстановки они лишние.
COMPOSE_SERVER_IP=${COMPOSE_SERVER_IP//\'/}
COMPOSE_SERVER_IP=${COMPOSE_SERVER_IP//\"/}
COMPOSE_SERVER_IP="${COMPOSE_SERVER_IP:-127.0.0.1}"
if [ "$DRY_RUN" = 1 ]; then
  printf 'DRY: (cd %s && SERVER_IP=%s docker compose down)\n' \
    "$SELF_DIR" "$COMPOSE_SERVER_IP"
  printf 'DRY: docker rm -f routeweave-tgwsproxy\n'
  ok "docker: удаление контейнера запланировано"
elif command -v docker >/dev/null 2>&1; then
  if [ -f "$SELF_DIR/docker-compose.yml" ]; then
    if ! ( cd "$SELF_DIR" &&
           SERVER_IP="$COMPOSE_SERVER_IP" docker compose down >/dev/null 2>&1 ); then
      err "docker compose down завершился с ошибкой (контейнер удалим принудительно)"
    fi
  else
    warn "$SELF_DIR/docker-compose.yml не найден — compose down пропущен"
  fi
  docker rm -f routeweave-tgwsproxy >/dev/null 2>&1 || true
  ok "docker: контейнер routeweave-tgwsproxy удалён (если был)"
else
  warn "docker не установлен — шаг пропущен"
fi

# ------------------------------------------------------------------ 5. DNS
# install.sh заменил /etc/resolv.conf обычным файлом и положил drop-in
# systemd-resolved. Возвращаем исходное состояние: сначала файл (атомарно,
# через временный файл в /etc + mv), потом drop-in, потом рестарт юнита.
info "Восстановление DNS..."
if [ -e "$BACKUP_DIR/resolv.conf.orig" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: cp -aL %s /etc/resolv.conf\n' "$BACKUP_DIR/resolv.conf.orig"
    printf 'DRY: mv -f %s %s.restored\n' \
      "$BACKUP_DIR/resolv.conf.orig" "$BACKUP_DIR/resolv.conf.orig"
  else
    tmp=""
    if tmp=$(mktemp /etc/.resolv.conf.restore.XXXXXX 2>/dev/null); then
      if cp -aL "$BACKUP_DIR/resolv.conf.orig" "$tmp" 2>/dev/null; then
        chmod 0644 "$tmp" >/dev/null 2>&1 || true
        chown root:root "$tmp" >/dev/null 2>&1 || true
        if mv -f "$tmp" /etc/resolv.conf 2>/dev/null; then
          ok "/etc/resolv.conf восстановлен из $BACKUP_DIR/resolv.conf.orig"
          # Повторный uninstall не должен снова перезаписывать текущий
          # resolv.conf: помечаем бэкап как использованный.
          if do_mv "$BACKUP_DIR/resolv.conf.orig" \
                   "$BACKUP_DIR/resolv.conf.orig.restored"; then
            info "бэкап переименован в $BACKUP_DIR/resolv.conf.orig.restored"
          fi
        else
          err "не удалось заменить /etc/resolv.conf — верните DNS вручную"
          rm -f "$tmp" || true
        fi
      else
        err "не удалось скопировать бэкап resolv.conf — верните DNS вручную"
        rm -f "$tmp" || true
      fi
    else
      err "не удалось создать временный файл в /etc — верните /etc/resolv.conf вручную"
    fi
  fi
elif [ -e "$BACKUP_DIR/resolv.conf.orig.restored" ]; then
  info "resolv.conf уже восстанавливался ранее — текущий файл не перезаписываю"
else
  warn "бэкапа $BACKUP_DIR/resolv.conf.orig нет — верните DNS вручную (например, nameserver 192.168.1.1 или 77.88.8.8)"
fi

restore_dropin "$RESOLVED_DROPIN" "$BACKUP_DIR/resolved-routeweave.conf.orig"

if command -v systemctl >/dev/null 2>&1 \
   && do_systemctl cat systemd-resolved.service; then
  do_systemctl restart systemd-resolved \
    || err "не удалось перезапустить systemd-resolved (systemctl restart systemd-resolved)"
  do_systemctl daemon-reload || true
else
  info "юнита systemd-resolved нет — рестарт пропущен"
fi

# Симлинк возвращаем после рестарта: цель /run/systemd/resolve/stub-resolv.conf
# появляется только при работающем systemd-resolved. При ошибке остаётся
# обычный файл, восстановленный выше, — DNS продолжает работать.
if [ "$DRY_RUN" = 1 ]; then
  if [ -s "$BACKUP_DIR/resolv.conf.orig.link" ]; then
    printf 'DRY: восстановить симлинк /etc/resolv.conf из %s\n' \
      "$BACKUP_DIR/resolv.conf.orig.link"
  fi
else
  restore_rc=0
  restore_resolv_symlink /etc "$BACKUP_DIR/resolv.conf.orig.link" || restore_rc=$?
  case "$restore_rc" in
    0)
      ok "/etc/resolv.conf восстановлен как симлинк на $(readlink /etc/resolv.conf 2>/dev/null)"
      ;;
    1)
      : # записанной ссылки нет — обычный случай
      ;;
    2)
      link_target=$(head -n1 "$BACKUP_DIR/resolv.conf.orig.link" 2>/dev/null || true)
      case "$link_target" in
        /*) link_abs=$link_target ;;
        *)  link_abs="/etc/$link_target" ;;
      esac
      info "цель $link_abs пока недоступна — оставляю /etc/resolv.conf обычным файлом с тем же содержимым"
      ;;
    *)
      warn "не удалось восстановить симлинк /etc/resolv.conf — оставлен обычный файл"
      ;;
  esac
fi

# Согласованность после рестарта, без сетевых проверок: если resolv.conf
# указывает на stub (127.0.0.53/127.0.0.1), а порт 53 никто не слушает, DNS
# мёртв. В этом случае оставляем рабочий статический resolv.conf.
resolv_on_stub=0
if grep -qE '^[[:space:]]*nameserver[[:space:]]+(127\.0\.0\.53|127\.0\.0\.1)([[:space:]]|$)' \
  /etc/resolv.conf 2>/dev/null; then
  resolv_on_stub=1
fi
if [ "$resolv_on_stub" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: проверить DNS stub (fallback в /etc/resolv.conf не записывается)\n'
  else
    port53_out=$({ ss -lun 2>/dev/null || true; ss -ltn 2>/dev/null || true; } \
      | grep ':53 ' || true)
    if [ -n "$port53_out" ]; then
      ok "DNS восстановлен (stub слушает 53)"
    else
      dns_tmp=""
      if dns_tmp=$(mktemp /etc/.resolv.conf.fallback.XXXXXX 2>/dev/null); then
        if printf 'nameserver 77.88.8.8\nnameserver 77.88.8.1\n' > "$dns_tmp" 2>/dev/null; then
          chmod 0644 "$dns_tmp" >/dev/null 2>&1 || true
          chown root:root "$dns_tmp" >/dev/null 2>&1 || true
          if mv -f "$dns_tmp" /etc/resolv.conf 2>/dev/null; then
            warn "systemd-resolved stub не поднялся (53 никто не слушает) — в /etc/resolv.conf записан публичный DNS 77.88.8.8/77.88.8.1"
          else
            err "не удалось заменить /etc/resolv.conf — верните DNS вручную"
            rm -f "$dns_tmp" || true
          fi
        else
          err "не удалось записать временный resolv.conf — верните DNS вручную"
          rm -f "$dns_tmp" || true
        fi
      else
        err "не удалось создать временный файл в /etc — верните /etc/resolv.conf вручную"
      fi
    fi
  fi
fi

# ------------------------------------------- 6. sudoers и helper-скрипты
info "Удаление sudoers и helper-скриптов..."
do_rm "$SUDOERS_PANEL" || true
for helper in "${HELPERS[@]}"; do
  do_rm "$helper" || true
done
do_rm "$ETC/nfqws-launch.sh" || true
do_rm "$ETC/fw-tailscale.sh" || true
do_rm "$ETC/.env.tgws" || true
ok "sudoers и helper-скрипты удалены"

# ------------------------------------------------------- 7. пользователь
# Конфиги в /etc/routeweave сохраняются, но install.sh выставлял на них
# владельца routeweave:routeweave (lists) и root:routeweave (.env). После
# userdel/groupdel это дало бы висячие uid/gid, а .env остался бы 0640 с
# несуществующей группой — то есть секреты могли бы читать те, кому достанется
# этот gid. Поэтому сначала возвращаем root и ужесточаем .env, потом удаляем
# пользователя.
info "Удаление пользователя $SERVICE_USER..."
if [ -e "$ETC/lists" ]; then
  do_chown -R root:root "$ETC/lists" || err "не удалось вернуть root:root на $ETC/lists"
fi
if [ -e "$ETC/.env" ]; then
  do_chown root:root "$ETC/.env" || err "не удалось вернуть root:root на $ETC/.env"
  do_chmod 0600 "$ETC/.env" || err "не удалось выставить 0600 на $ETC/.env"
fi

if [ "$DRY_RUN" = 1 ]; then
  printf 'DRY: pkill -u %s; userdel %s (если существует); groupdel %s (если пуста)\n' \
    "$SERVICE_USER" "$SERVICE_USER" "$SERVICE_GROUP"
else
  pkill -u "$SERVICE_USER" 2>/dev/null || true
  # Дадим процессам умереть: userdel откажется удалять пользователя с живыми
  # процессами (а панель/venv могут успеть форкнуться).
  for _ in 1 2 3 4 5; do
    pgrep -u "$SERVICE_USER" >/dev/null 2>&1 || break
    sleep 1
    pkill -9 -u "$SERVICE_USER" 2>/dev/null || true
  done

  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    if userdel "$SERVICE_USER" 2>/dev/null; then
      ok "пользователь $SERVICE_USER удалён"
    else
      err "не удалось удалить пользователя $SERVICE_USER (остались процессы или файлы вне /opt: userdel -f $SERVICE_USER)"
    fi
  else
    info "пользователя $SERVICE_USER уже нет"
  fi

  # Группу удаляем только если она пустая: иначе groupdel откажется, и это
  # нормально — не вычищаем чужие вхождения вручную.
  if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    if groupdel "$SERVICE_GROUP" 2>/dev/null; then
      ok "группа $SERVICE_GROUP удалена"
    else
      warn "группа $SERVICE_GROUP не пуста или используется — оставлена (getent group $SERVICE_GROUP)"
    fi
  fi
fi

# ------------------------------------------------------------------ 8. ufw
# Только scoped-правила, которые ставит install.sh. Принудительный сброс ufw
# НЕ вызывается, глобальные allow пользователя (53/7890/9090/...) не трогаем.
# Значения берём из .env; если файла нет — зависимые правила пропускаем.
# Активен ли ufw. В dry-run не вызываем ufw даже для чтения: план показываем
# для случая установленного ufw, а реальные правила не трогаем.
ufw_is_active() {
  command -v ufw >/dev/null 2>&1 || return 1
  if [ "$DRY_RUN" = 1 ]; then
    return 0
  fi
  ufw status 2>/dev/null | grep -q "Status: active"
}

if ufw_is_active; then
  info "ufw активен — убираю scoped-правила Routeweave..."
  # 41641/udp ставил установщик для Tailscale (прямые соединения).
  do_ufw delete allow 41641/udp || true

  if [ -r "$ETC/.env" ]; then
    LAN_IFACE=$(env_get LAN_IFACE)
    WG_SUBNET=$(env_get WG_SUBNET)
    LAN_CIDR=$(env_get LAN_CIDR)
    PANEL_PORT=$(env_get PANEL_PORT)
    PANEL_PORT=${PANEL_PORT:-8080}
    TS_SUBNET=$(env_get TS_SUBNET)
    TS_CIDR=${TS_SUBNET:-100.64.0.0/10}
    TS_IFACE=$(env_get TS_IFACE)
    TS_IFACE=${TS_IFACE:-tailscale0}

    if [ -n "$LAN_IFACE" ]; then
      do_ufw route delete allow in on "$LAN_IFACE" || true
      do_ufw delete deny in on "$LAN_IFACE" from "$TS_CIDR" || true
    fi

    # Tailscale-правила (если пакет был установлен на момент install.sh).
    # Сам tailscale и его состояние не трогаем.
    do_ufw route delete allow in on "$TS_IFACE" || true
    do_ufw route delete allow out on "$TS_IFACE" || true

    # Подсеть LAN в .env не хранится (install.sh вычисляет её на месте) —
    # пробуем вывести из интерфейса.
    if [ -z "$LAN_CIDR" ] && [ -n "$LAN_IFACE" ] && command -v ip >/dev/null 2>&1; then
      LAN_CIDR=$(ip -4 addr show "$LAN_IFACE" 2>/dev/null | awk '/inet /{print $2; exit}')
    fi

    # WDTT-правила не снимаем, если WDTT-Plus оставлен.
    if [ "$WDTT_PRESENT" -ne 1 ]; then
      do_ufw route delete allow in on wdtt0 || true
      do_ufw route delete allow out on wdtt0 || true
    fi

    # install.sh выдавал доступ не «всем на порт», а с конкретных подсетей —
    # снимаем только эти scoped-правила. Если WDTT-Plus оставлен, его подсеть
    # (WG_SUBNET) не трогаем: эти же правила обслуживают WDTT-клиентов.
    seen_src=" "
    for src in "$WG_SUBNET" "$LAN_CIDR" "$TS_CIDR"; do
      [ -n "$src" ] || continue
      if [ "$WDTT_PRESENT" -eq 1 ] && [ "$src" = "$WG_SUBNET" ]; then
        info "ufw: правила для WDTT ($src) сохранены"
        continue
      fi
      case "$seen_src" in
        *" $src "*) continue ;;
      esac
      seen_src="$seen_src$src "
      for p in 53 7890 9090 "$PANEL_PORT" 1443; do
        do_ufw delete allow from "$src" to any port "$p" proto tcp || true
      done
      do_ufw delete allow from "$src" to any port 53 proto udp || true
    done
    ok "ufw: scoped-правила Routeweave удалены (остальные не тронуты)"
  else
    warn "$ETC/.env не найден — scoped-правила ufw (подсети/порты) не снимаю; проверьте: ufw status numbered"
  fi
else
  info "ufw не активен или не установлен — шаг пропущен"
fi

# ---------------------------------------------------------------- 9. файлы
info "Удаление файлов..."
do_rm_rf "$OPT_PANEL" || true
remove_owned_path "$ZAPRET_DIR"
remove_owned_path "$MIHOMO_BIN"
do_rm_rf /run/routeweave || true
do_rm_rf "$ETC/.staging" || true
do_rm_rf "$ETC/zashboard" || true
do_rm "$ETC/zapret.commit" || true
ok "панель, zapret, mihomo, рантайм-каталог и staging удалены (по маркеру владения)"

# --------------------------------------------------- 10. конфиги / --purge
if [ "$PURGE" = 1 ]; then
  info "Удаление $ETC целиком (--purge)..."
  if do_rm_rf "$ETC"; then
    ok "$ETC удалён вместе с .env, config.yaml, zapret.conf, lists и backup"
  fi
else
  # Маркер больше не нужен: все решения по владению уже приняты.
  do_rm "$OWNED_MARKER" || true
  secrets=$(secret_keys_present)
  if [ -n "$secrets" ]; then
    warn "Секреты ($secrets) оставлены в $ETC/.env — значения не печатаю."
  else
    warn "Конфигурация оставлена в $ETC."
  fi
  warn "Смените секреты (ротация) или удалите каталог: sudo ./uninstall.sh --purge"
fi

# ------------------------------------------------------------- 11. итоги
echo
if [ "$DRY_RUN" = 1 ]; then
  echo -e "${C_G}[+] --dry-run: план показан, изменения не выполнялись.${C_N}"
elif [ "$PURGE" = 1 ]; then
  echo -e "${C_G}[+] Routeweave удалён, $ETC очищен.${C_N}"
else
  echo -e "${C_G}[+] Routeweave удалён.${C_N}"
fi
echo
echo "Удалено:"
echo "  - systemd-юниты: routeweave-firewall, routeweave-panel,"
echo "    routeweave-lists-update.service/.timer; mihomo/zapret-nfqws — только"
echo "    если не были чужими; scoped-правила firewall сняты, включая"
echo "    WAN-маркировку и ip rule 8993;"
echo "  - контейнер routeweave-tgwsproxy (docker compose down + docker rm -f);"
echo "  - /etc/sudoers.d/routeweave-panel и helper-скрипты в /usr/local/sbin,"
echo "    включая routeweave-tailscale-state.sh;"
echo "  - /etc/routeweave/nfqws-launch.sh, /etc/routeweave/.env.tgws,"
echo "    /etc/routeweave/fw-tailscale.sh, .staging, zapret.commit, zashboard;"
echo "  - /opt/routeweave-panel, /run/routeweave;"
echo "  - /opt/zapret и /usr/local/bin/mihomo — только при маркере routeweave;"
echo "  - пользователь и группа $SERVICE_USER (если не были заняты);"
echo "  - scoped-правила ufw, добавленные установщиком."
if [ "$PURGE" -ne 1 ]; then
  echo
  echo "Осталось (удаляется вручную):"
  echo "  - конфиги, списки и секреты: $ETC/.env, config.yaml, zapret.conf, lists;"
  echo "    полное удаление: sudo ./uninstall.sh --purge (или rm -rf $ETC);"
  echo "  - бэкапы в $BACKUP_DIR (кроме уже использованных);"
  echo "  - WDTT-Plus: systemctl status wdtt, данные в /etc/wdtt, ip rule 8990;"
  echo "  - пакет Tailscale и состояние /var/lib/tailscale не удалялись;"
  echo "  - пакеты, поставленные установщиком (iptables, nftables, docker и т.д.),"
  echo "    не удалялись — они могут быть нужны другим сервисам."
else
  echo
  echo "Осталось:"
  echo "  - WDTT-Plus: systemctl status wdtt, данные в /etc/wdtt, ip rule 8990;"
  echo "  - пакет Tailscale и состояние /var/lib/tailscale не удалялись;"
  echo "  - пакеты, поставленные установщиком (iptables, nftables, docker и т.д.),"
  echo "    не удалялись — они могут быть нужны другим сервисам."
fi
echo
echo -e "${C_Y}ВНИМАНИЕ:${C_N} клиенты, у которых шлюз или DNS = IP этого сервера,"
echo "потеряют интернет — верните им шлюз/DNS роутера (или отключите"
echo "DHCP-резервацию на этот хост)."

if [ "$ERRORS" -gt 0 ]; then
  echo
  echo "Удаление завершено с ошибками: $ERRORS (см. сообщения [x] выше)." >&2
  exit 1
fi
exit 0
