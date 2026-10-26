#!/usr/bin/env bash
# ============================================================
#  Routeweave — установщик для Ubuntu 20.04+/Debian 12+
#  Ядро: mihomo (прозрачный шлюз) + zapret (nfqws) + списки доменов +
#        веб-панель (FastAPI) + firewall (iptables/ufw)
#  Опционально:
#    - tg-ws-proxy-rs (Telegram MTProto, docker) — INSTALL_TGWS=yes
#    - WDTT-Plus (VPN-сервер для Android, amd64) — INSTALL_WDTT=yes
#    - Tailscale (exit node для tailnet, официальный apt) — INSTALL_TAILSCALE=yes
#  Запуск: sudo ./install.sh            (интерактивно)
#          sudo ./install.sh --defaults (без вопросов — только ядро)
#          ./install.sh --help          (справка, root не нужен)
# ============================================================
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC=/etc/routeweave
OPT_PANEL=/opt/routeweave-panel
ZAPRET_DIR=/opt/zapret
BACKUP_DIR="$ETC/backup"
SUDOERS_PANEL=/etc/sudoers.d/routeweave-panel
TGWS_RESTART=/usr/local/sbin/routeweave-tgws-restart.sh
TGWS_STATE=/usr/local/sbin/routeweave-tgws-state.sh
TAILSCALE_STATE=/usr/local/sbin/routeweave-tailscale-state.sh
MIHOMO_FALLBACK_VER="v1.19.13"
TS_STATUS="не устанавливался"
TS_REPO_KEYRING=/usr/share/keyrings/tailscale-archive-keyring.gpg
TS_REPO_LIST=/etc/apt/sources.list.d/tailscale.list
FAIL=0

C_G='\033[0;32m'; C_Y='\033[1;33m'; C_R='\033[0;31m'; C_B='\033[1;34m'; C_N='\033[0m'
info()  { echo -e "${C_B}[i]${C_N} $*"; }
ok()    { echo -e "${C_G}[+]${C_N} $*"; }
warn()  { echo -e "${C_Y}[!]${C_N} $*"; }
die()   { echo -e "${C_R}[x]${C_N} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Routeweave — установщик шлюза (mihomo + zapret + веб-панель).

Использование:
  sudo ./install.sh [--defaults]
  ./install.sh --help

Опции:
  -h, --help    показать эту справку и выйти (root не нужен)
  --defaults    не задавать вопросов: взять значения по умолчанию
                (ядро; INSTALL_WDTT=no, INSTALL_TGWS=no, INSTALL_TAILSCALE=no)

Переменные окружения времени установки (НЕ из /etc/routeweave/.env):
  INSTALL_WDTT=yes|no     поставить WDTT-Plus (VPN для Android, только amd64)
  INSTALL_TGWS=yes|no     поставить tg-ws-proxy (Telegram MTProto, нужен Docker)
  INSTALL_TAILSCALE=yes|no
                          поставить Tailscale (exit node; официальный apt,
                          только Ubuntu/Debian)
  TS_AUTHKEY_FILE=<path>  auth key Tailscale в файле (предпочтительно)
  TS_AUTHKEY=<key>        auth key строкой (только на время установки)
  ZAPRET_REF=<ref>        ветка/тег zapret при клонировании и обновлении
                          (по умолчанию master)
  MIHOMO_SHA256=<hex>     ожидаемая sha256 архива mihomo, если апстрим не
                          публикует <url>.sha256
  ZASHBOARD_SHA256=<hex>  то же для dist.zip дашборда zashboard
  TG_PINNED_UPSTREAM=<tiers>
                          порядок апстримов tg-ws-proxy через запятую
                          (ws, cfworker, cfproxy, mtproto, tcp); по умолчанию
                          cfworker,ws,tcp, если задан домен CF Worker

Что делает установщик:
  1. ставит пакеты, создаёт системного пользователя routeweave и минимальный
     sudoers (/etc/sudoers.d/routeweave-panel, 0640 root:root);
  2. пишет /etc/routeweave/.env (0640 root:routeweave) со значениями в кавычках;
  3. скачивает mihomo (сверяя sha256, если апстрим его публикует), собирает
     zapret из исходников (/opt/zapret, ветка ZAPRET_REF);
  4. освобождает порт 53 (systemd-resolved), настраивает DNS и firewall (ufw,
     если активен), не затирая ручные правки /etc/routeweave/zapret.conf;
  5. ставит systemd-юниты, helper-скрипты в /usr/local/sbin и FastAPI-панель
     в venv (/opt/routeweave-panel);
  6. при желании — zashboard, tg-ws-proxy (docker), WDTT-Plus и Tailscale.

Пароли (генерируются один раз, если их нет в существующем .env):
  PANEL_PASS     — пароль входа в веб-панель (пишется в .env);
  MIHOMO_SECRET  — Bearer-секрет API mihomo/zashboard;
  TG_SECRET      — секрет MTProto-прокси (только при INSTALL_TGWS=yes).
Сгенерированный PANEL_PASS печатается в итоговой сводке; остальные секреты
остаются только в .env.

После установки: панель http://<SERVER_IP>:<PANEL_PORT>, логин PANEL_USER.
EOF
}

DEFAULTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --defaults) DEFAULTS=1 ;;
    *) echo "Неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo ./install.sh"

# Единый временный каталог на весь прогон (никаких фиксированных /tmp/...).
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/routeweave-install.XXXXXX")"
WDTT_TMP=""
# cleanup вызывается через trap cleanup EXIT. SC2317 — прежнее имя SC2329
# в shellcheck 0.9.x (в CI ставится 0.9.0, локально 0.11.0), нужны оба кода.
# shellcheck disable=SC2329,SC2317
cleanup() {
  [ -z "${WORK_DIR:-}" ] || rm -rf "$WORK_DIR"
  [ -z "${WDTT_TMP:-}" ] || rm -rf "$WDTT_TMP"
}
trap cleanup EXIT

# >>> pure-functions >>>
# Чистые функции без побочных эффектов на систему: их можно вынести в
# песочницу и проверить отдельно (см. tests / отчёт).

esc() { # экранирование значения для двойных кавычек в .env: \ и "
  local s=${1-}
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '%s' "$s"
}

esc_sed() { # экранирование для replacement в sed (s|...|...|)
  local s=${1-}
  s=${s//\\/\\\\}
  s=${s//&/\\&}
  s=${s//|/\\|}
  printf '%s' "$s"
}

env_line() { # env_line <KEY> <value> -> KEY="value"
  printf '%s="%s"\n' "$1" "$(esc "${2-}")"
}

# Валидаторы вызываются по имени из ask_valid ("$__validator"), поэтому для
# линтера они выглядят неиспользуемыми — это ложное срабатывание SC2329.
# SC2317 — прежнее имя SC2329 в shellcheck 0.9.x (в CI ставится 0.9.0,
# локально 0.11.0), поэтому отключаем оба кода.
# shellcheck disable=SC2329,SC2317
valid_iface() { # имя сетевого интерфейса
  [[ ${1-} =~ ^[a-zA-Z0-9_.:-]+$ ]]
}

# shellcheck disable=SC2329,SC2317
valid_ipv4() {
  python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$1" 2>/dev/null
}

# shellcheck disable=SC2329,SC2317
valid_port() {
  local p=${1-}
  [[ $p =~ ^[0-9]{1,5}$ ]] || return 1
  (( 10#$p >= 1 && 10#$p <= 65535 ))
}

# shellcheck disable=SC2329,SC2317
valid_user() {
  [[ ${1-} =~ ^[A-Za-z0-9._-]{1,32}$ ]]
}

# shellcheck disable=SC2329,SC2317
valid_socks() { # пусто допустимо; иначе scheme://host:port
  [ -z "${1-}" ] && return 0
  python3 -c '
import sys
from urllib.parse import urlsplit
try:
    p = urlsplit(sys.argv[1])
    port = p.port
except ValueError:
    sys.exit(1)
if p.scheme not in ("socks5", "socks5h", "socks", "http", "https"):
    sys.exit(1)
if not p.hostname:
    sys.exit(1)
sys.exit(0 if port and 1 <= port <= 65535 else 1)
' "$1" 2>/dev/null
}

# shellcheck disable=SC2329,SC2317
valid_cidr() {
  python3 -c 'import ipaddress,sys; ipaddress.IPv4Network(sys.argv[1], strict=False)' "$1" 2>/dev/null
}

normalize_yn() { # yes/no (регистронезависимо); rc=1 если мусор
  case "${1,,}" in
    yes|y|да|1|true)  printf 'yes' ;;
    no|n|нет|0|false) printf 'no' ;;
    *) return 1 ;;
  esac
}

# tgws_pinned_upstream <cf_worker_domain> — порядок тиров tg-ws-proxy
# (--pinned-upstream): имена через запятую; пусто = пин не задавать.
# Если домен CF Worker задан, Worker идёт первым: прямой WS к Telegram DC
# обычно режется на SYN, и каждая новая сессия тратит 2-5 с впустую.
# Env TG_PINNED_UPSTREAM имеет приоритет; невалидный список -> rc=2.
tgws_pinned_upstream() {
  local cf_domain=${1-}
  local pin=${TG_PINNED_UPSTREAM:-}
  local tiers='ws|cfworker|cfproxy|mtproto|tcp'
  if [ -z "$pin" ] && [ -n "$cf_domain" ]; then
    pin="cfworker,ws,tcp"
  fi
  if [ -n "$pin" ] && [[ ! $pin =~ ^($tiers)(,($tiers))*$ ]]; then
    return 2
  fi
  printf '%s' "$pin"
}

tailscale_repo_urls() { # tailscale_repo_urls <id> <codename> -> два URL apt-репозитория
  local id=${1-} codename=${2-}
  case "$id" in
    ubuntu|debian) ;;
    *) return 1 ;;
  esac
  # codename — строчная латиница/цифры; пустое значение и пути недопустимы.
  [[ $codename =~ ^[a-z][a-z0-9]*$ ]] || return 1
  printf 'https://pkgs.tailscale.com/stable/%s/%s.noarmor.gpg\n' "$id" "$codename"
  printf 'https://pkgs.tailscale.com/stable/%s/%s.tailscale-keyring.list\n' "$id" "$codename"
}

gen_hex() { # gen_hex <байт> -> hex-строка длиной 2*<байт>
  local n=${1:-16}
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$n" 2>/dev/null && return 0
  fi
  head -c "$n" /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-$((n * 2))
}

backup_file() { # backup_file <файл> -> <файл>.bak-<UTC timestamp>, rc=0 всегда
  local f=$1 b
  [ -f "$f" ] || return 0
  b="$f.bak-$(date -u +%Y%m%d-%H%M%S)"
  cp -a "$f" "$b"
  warn "бэкап: $b"
}

preflight() { # preflight <каталог репозитория> — всё, что установщик копирует
  # Ранняя проверка целостности: лучше упасть до изменения системы, чем
  # посреди установки (под set -e отсутствующий файл роняет шаг без объяснений).
  local root=$1 missing="" f
  for f in \
    templates/zapret.conf.tpl \
    templates/render_mihomo.py \
    templates/nfqws-launch.sh \
    templates/routeweave-update-lists.sh \
    templates/routeweave-tgws-restart.sh \
    templates/routeweave-tgws-state.sh \
    templates/routeweave-tailscale-state.sh \
    templates/fw-base.sh \
    templates/fw-wan.sh \
    templates/fw-tailscale.sh \
    templates/fw-nfq.sh \
    templates/mihomo.service \
    templates/routeweave-firewall.service \
    templates/zapret-nfqws.service \
    templates/routeweave-panel.service \
    templates/routeweave-lists-update.service \
    templates/routeweave-lists-update.timer \
    panel/app.py \
    panel/requirements.txt \
    panel/static/index.html \
    panel/static/app.css \
    panel/static/app.js
  do
    [ -f "$root/$f" ] || missing="$missing $f"
  done
  [ -d "$root/lists" ] || missing="$missing lists/"
  [ -z "$missing" ] \
    || die "В каталоге установщика нет обязательных файлов:$missing. Проверьте целостность репозитория (git status; git checkout -- .) и повторите."
  ok "preflight: обязательные файлы установщика на месте"
}

merge_zapret_conf() { # merge_zapret_conf <шаблон> <цель> [iface]
  # Создаёт zapret.conf из шаблона, если файла нет. Если файл есть — сохраняет
  # ручные правки (DESYNC/FOOLING/...) и обновляет только строку WAN_IFACE=.
  local tpl=$1 target=$2 iface=${3:-${LAN_IFACE:-}} rendered cur
  [ -f "$tpl" ] || die "не найден шаблон zapret: $tpl"
  if [ -z "$iface" ]; then
    # Нельзя затирать рабочий WAN_IFACE пустым значением.
    warn "zapret.conf: LAN_IFACE не определён — WAN_IFACE не изменяю"
    return 0
  fi

  rendered=$(mktemp "${TMPDIR:-/tmp}/routeweave-zapret.XXXXXX")
  sed "s|{{WAN_IFACE}}|$(esc_sed "$iface")|g" "$tpl" > "$rendered"

  if [ ! -f "$target" ]; then
    install -m 0600 "$rendered" "$target"
    rm -f "$rendered"
    ok "zapret.conf создан из шаблона (WAN_IFACE=$iface)"
    return 0
  fi

  if cmp -s "$rendered" "$target"; then
    rm -f "$rendered"
    info "zapret.conf уже соответствует шаблону (WAN_IFACE=$iface)"
    return 0
  fi

  backup_file "$target"

  if grep -q '^WAN_IFACE=' "$target"; then
    cur=$(sed -n 's/^WAN_IFACE=//p' "$target" | head -n1)
    if [ "$cur" != "$iface" ]; then
      sed -i "s|^WAN_IFACE=.*|WAN_IFACE=$(esc_sed "$iface")|" "$target"
      ok "zapret.conf: WAN_IFACE=$cur -> $iface (остальные настройки сохранены)"
    else
      info "zapret.conf: WAN_IFACE уже $iface — файл не изменён"
    fi
  else
    printf 'WAN_IFACE=%s\n' "$iface" >> "$target"
    ok "zapret.conf: добавлена строка WAN_IFACE=$iface"
  fi

  chmod 0600 "$target"
  rm -f "$rendered"
}
# <<< pure-functions <<<

preflight "$APP_DIR"

ask() { # ask <переменная> <вопрос> <дефолт>
  local __var=$1 __q=$2 __def=${3:-} __ans
  if [ "$DEFAULTS" = 1 ] || [ ! -t 0 ]; then
    printf -v "$__var" '%s' "$__def"
    return 0
  fi
  read -r -p "$__q [$__def]: " __ans || die "прервано"
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

ask_valid() { # ask_valid <переменная> <вопрос> <дефолт> <валидатор> [подсказка]
  local __var=$1 __q=$2 __def=${3:-} __validator=$4 __hint=${5:-} __ans
  if [ "$DEFAULTS" = 1 ] || [ ! -t 0 ]; then
    if ! "$__validator" "$__def"; then
      die "Неинтерактивный режим: значение «$__def» для «$__q» недопустимо.${__hint:+ $__hint.} Запустите установщик интерактивно."
    fi
    printf -v "$__var" '%s' "$__def"
    return 0
  fi
  while :; do
    read -r -p "$__q [$__def]: " __ans || die "прервано"
    __ans=${__ans:-$__def}
    if "$__validator" "$__ans"; then
      printf -v "$__var" '%s' "$__ans"
      return 0
    fi
    warn "Недопустимое значение «$__ans»${__hint:+ — $__hint}"
  done
}

# ---------- WDTT-Plus (сервер для Android) ----------
WDTT_REPO="Ivan4537/WDTT-Plus"
WDTT_FALLBACK_TAG="v20"
WDTT_FALLBACK_NAME="WDTT-Plus-server-v20-installer-0.20.3-linux-amd64.tar.gz"
WDTT_DIR=/opt/wdtt-installer
WDTT_DTLS_PORT="${WDTT_DTLS_PORT:-56000}"
WDTT_WG_PORT="${WDTT_WG_PORT:-56001}"
WDTT_CLIENT_PORT="${WDTT_CLIENT_PORT:-9000}"
WDTT_SUBNET="10.66.66.0/24"
WDTT_MASTER_PASS=""
WDTT_INITIAL_CONFIG=""
WDTT_STATUS="не устанавливался"

wdtt_fail() { warn "WDTT-Plus: $*"; WDTT_STATUS="ошибка"; return 1; }

# mihomo (TUN Meta) держит policy-routing: 9000 -> table 2022 -> TUN Meta.
# Правило 8990 на подсеть WDTT проверяется раньше и уводит трафик в main (wdtt0).
wdtt_ensure_ip_rule() {
  if ! ip rule show 2>/dev/null | grep -q '8990:.*10\.66\.66\.0/24.*lookup main'; then
    ip rule add to "$WDTT_SUBNET" lookup main priority 8990 2>/dev/null || true
  fi
}

install_wdtt() {
  if [ -x /usr/local/bin/wdtt-server ] && [ -f /etc/systemd/system/wdtt.service ]; then
    WDTT_STATUS="уже установлен"
    ok "WDTT-Plus уже установлен — обновление не требуется"
    wdtt_ensure_ip_rule
    systemctl restart routeweave-firewall >/dev/null 2>&1 || true
    if [ -x "$WDTT_DIR/install.sh" ]; then
      bash "$WDTT_DIR/install.sh" status || true
    fi
    return 0
  fi
  if [ -e /usr/local/bin/wdtt-server ] || [ -e /etc/wdtt ]; then
    wdtt_fail "найдены файлы WDTT без полной установки — разберите вручную: $WDTT_DIR/install.sh doctor"
    return 1
  fi

  WDTT_TMP=$(mktemp -d "${TMPDIR:-/tmp}/routeweave-wdtt.XXXXXX") \
    || { wdtt_fail "не удалось создать временный каталог"; return 1; }

  if [ ! -x "$WDTT_DIR/install.sh" ] || [ ! -x "$WDTT_DIR/wdtt-server" ]; then
    local asset tag name url src binver
    asset=$(curl -fsSL --max-time 20 "https://api.github.com/repos/$WDTT_REPO/releases/latest" 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for item in data.get("assets", []):
    n = item.get("name", "")
    if n.startswith("WDTT-Plus-server-") and n.endswith("-linux-amd64.tar.gz"):
        print(data.get("tag_name", ""))
        print(n)
        break
' 2>/dev/null) || true
    tag=$(printf '%s\n' "$asset" | sed -n '1p')
    name=$(printf '%s\n' "$asset" | sed -n '2p')
    if [ -z "$tag" ] || [ -z "$name" ]; then
      warn "GitHub API недоступен — использую закреплённый выпуск $WDTT_FALLBACK_TAG"
      tag="$WDTT_FALLBACK_TAG"
      name="$WDTT_FALLBACK_NAME"
    fi
    case "$name" in
      WDTT-Plus-server-*-linux-amd64.tar.gz) ;;
      *) wdtt_fail "неожиданное имя ассета: $name"; return 1 ;;
    esac
    url="https://github.com/$WDTT_REPO/releases/download/$tag/$name"
    info "Загрузка WDTT-Plus ($tag)..."
    curl -fsSL --retry 2 --max-time 180 -o "$WDTT_TMP/$name" "$url" \
      || { wdtt_fail "не удалось скачать $url"; return 1; }
    curl -fsSL --retry 2 --max-time 60 -o "$WDTT_TMP/$name.sha256" "$url.sha256" \
      || { wdtt_fail "не удалось скачать контрольную сумму"; return 1; }
    ( cd "$WDTT_TMP" && sha256sum -c "$name.sha256" >/dev/null ) \
      || { wdtt_fail "sha256 архива не совпал"; return 1; }
    tar -xzf "$WDTT_TMP/$name" -C "$WDTT_TMP" \
      || { wdtt_fail "не удалось распаковать архив"; return 1; }
    src=$(find "$WDTT_TMP" -mindepth 1 -maxdepth 1 -type d -name 'WDTT-Plus-server-*' | head -n1)
    [ -n "$src" ] || { wdtt_fail "в архиве нет каталога WDTT-Plus-server-*"; return 1; }
    ( cd "$src" && sha256sum -c SHA256SUMS >/dev/null ) \
      || { wdtt_fail "внутренние sha256 не совпали"; return 1; }
    binver=$("$src/wdtt-server" --version 2>/dev/null | head -n1 || true)
    [ -n "$binver" ] || { wdtt_fail "wdtt-server не сообщил версию"; return 1; }
    ok "WDTT-Plus: сервер v$binver, установщик $(bash "$src/install.sh" --version)"
    install -d -m 0755 "$WDTT_DIR"
    install -m 0755 "$src/install.sh" "$WDTT_DIR/install.sh"
    install -m 0755 "$src/wdtt-server" "$WDTT_DIR/wdtt-server"
    install -m 0644 "$src/SHA256SUMS" "$WDTT_DIR/SHA256SUMS"
  fi

  local passfile="$WDTT_TMP/wdtt-pass" sha
  ( umask 077; gen_hex 16 > "$passfile" )
  # shellcheck disable=SC2034  # главный пароль уходит в конфиг WDTT, в .env не пишется
  WDTT_MASTER_PASS=$(cat "$passfile")
  WDTT_INITIAL_CONFIG=/root/wdtt-initial.json
  if [ -e "$WDTT_INITIAL_CONFIG" ]; then
    WDTT_INITIAL_CONFIG="/root/wdtt-initial-$(date +%Y%m%d-%H%M%S).json"
  fi

  info "Создание начальной конфигурации WDTT-Plus..."
  bash "$WDTT_DIR/install.sh" init-config \
    --output "$WDTT_INITIAL_CONFIG" \
    --password-file "$passfile" \
    --dtls-port "$WDTT_DTLS_PORT" --wg-port "$WDTT_WG_PORT" --client-port "$WDTT_CLIENT_PORT" \
    --dns 1.1.1.1 --max-passwords 50 --yes \
    || { wdtt_fail "init-config завершился ошибкой"; return 1; }
  rm -f "$passfile"

  sha=$(sha256sum "$WDTT_DIR/wdtt-server" | awk '{print $1}')
  info "Установка сервера WDTT-Plus (UDP $WDTT_DTLS_PORT)..."
  if bash "$WDTT_DIR/install.sh" install \
      --binary "$WDTT_DIR/wdtt-server" \
      --config "$WDTT_INITIAL_CONFIG" \
      --sha256 "$sha" \
      --dtls-port "$WDTT_DTLS_PORT" --wg-port "$WDTT_WG_PORT" --client-port "$WDTT_CLIENT_PORT" \
      --dns 1.1.1.1 --max-passwords 50 --firewall none --yes; then
    WDTT_STATUS="установлен"
    ok "WDTT-Plus установлен: DTLS $WDTT_DTLS_PORT/udp"
  else
    wdtt_fail "установка не удалась — шлюз продолжит работать без WDTT"
    return 1
  fi

  wdtt_ensure_ip_rule
  systemctl restart routeweave-firewall >/dev/null 2>&1 || true
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$WDTT_DTLS_PORT"/udp >/dev/null 2>&1 || true
    ufw route allow in on wdtt0 >/dev/null 2>&1 || true
    ufw route allow out on wdtt0 >/dev/null 2>&1 || true
  fi
  bash "$WDTT_DIR/install.sh" status || true
  return 0
}

# ---------- 0. Система ----------
info "Проверка системы..."
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ok "Дистрибутив: ${PRETTY_NAME:-$ID}" ;;
  *) warn "Дистрибутив '$ID' официально не поддерживается — продолжаю на свой страх" ;;
esac

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  MIHOMO_ARCH=amd64; MIHOMO_ASSET_ARCH=amd64-compatible ;;
  aarch64) MIHOMO_ARCH=arm64; MIHOMO_ASSET_ARCH=arm64 ;;
  *) die "Не поддерживаемая архитектура: $ARCH" ;;
esac

WDTT_SUPPORTED=0
if [ "$ARCH" = "x86_64" ]; then WDTT_SUPPORTED=1; fi
if [ "$WDTT_SUPPORTED" != 1 ]; then
  warn "WDTT-Plus поддерживается только на amd64 — установка WDTT будет пропущена"
fi

# python3 нужен уже для валидации ввода, ip/ss/curl — для определения сети.
if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 \
   || ! command -v ip >/dev/null 2>&1; then
  info "Установка базовых утилит (curl, python3, iproute2)..."
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates python3 iproute2
fi

install -d -m 0755 "$ETC"
install -d -m 0700 "$BACKUP_DIR"

# При повторной установке берём сетевые параметры из существующего .env:
# иначе ip route get уходит в TUN mihomo (dev Meta) вместо физического NIC.
if [ -f "$ETC/.env" ]; then
  # shellcheck disable=SC1091
  . "$ETC/.env"
fi

# Auth key Tailscale приходит только из окружения времени установки;
# в /etc/routeweave/.env он не пишется.
TS_AUTHKEY_FILE="${TS_AUTHKEY_FILE:-}"
TS_AUTHKEY="${TS_AUTHKEY:-}"

DETECTED_IFACE="${LAN_IFACE:-$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')}"
[ -n "$DETECTED_IFACE" ] || die "Не удалось определить сетевой интерфейс"
DETECTED_IP="${SERVER_IP:-$(ip -4 addr show "$DETECTED_IFACE" | awk '/inet /{print $2; exit}' | cut -d/ -f1)}"
[ -n "$DETECTED_IP" ] || die "Не удалось определить IP интерфейса $DETECTED_IFACE"

# WDTT/WG уже настроен у пользователя?
WG_DETECTED=$(ip link show type wireguard 2>/dev/null | awk -F: '/^[0-9]+:/{print $2}' | tr -d ' ' | head -n1 || true)
WG_SUBNET_DEFAULT="10.66.66.0/24"
if [ -n "$WG_DETECTED" ]; then
  info "Обнаружен WireGuard-интерфейс: $WG_DETECTED (WDTT)"
  WG_SUBNET_DEFAULT=$(ip -4 addr show "$WG_DETECTED" | awk '/inet /{print $2; exit}')
  [ -n "$WG_SUBNET_DEFAULT" ] || WG_SUBNET_DEFAULT="10.66.66.0/24"
elif [ -d /etc/wdtt ] || ip link show wdtt0 >/dev/null 2>&1; then
  info "Обнаружен сервер WDTT (wdtt0) — подсеть 10.66.66.0/24"
fi

# ---------- 1. Пользователь routeweave и sudoers ----------
ensure_routeweave_user() {
  groupadd -r routeweave 2>/dev/null || true
  if ! id -u routeweave >/dev/null 2>&1; then
    useradd --system --gid routeweave --no-create-home --shell /usr/sbin/nologin routeweave
  fi
}

write_panel_sudoers() {
  local systemctl_bin visudo_bin tmp
  systemctl_bin=$(command -v systemctl || true)
  [ -n "$systemctl_bin" ] \
    || die "systemctl не найден — без него панель не сможет управлять сервисами"
  visudo_bin=$(command -v visudo || true)
  [ -n "$visudo_bin" ] || die "visudo не найден (пакет sudo) — не могу проверить sudoers"

  tmp="$WORK_DIR/sudoers.routeweave-panel"
  cat > "$tmp" <<EOF
# Routeweave: минимальные права веб-панели. Файл генерируется install.sh.
# Пользователь routeweave может только перезапускать перечисленные юниты и
# запускать четыре helper-скрипта; is-active проверяется без root.
routeweave ALL=(root) NOPASSWD: $systemctl_bin restart mihomo, $systemctl_bin restart zapret-nfqws, $systemctl_bin restart routeweave-panel, $systemctl_bin start routeweave-lists-update.service
routeweave ALL=(root) NOPASSWD: /usr/local/sbin/routeweave-update-lists.sh, /usr/local/sbin/routeweave-tgws-restart.sh, /usr/local/sbin/routeweave-tgws-state.sh, /usr/local/sbin/routeweave-tailscale-state.sh
EOF
  chmod 0640 "$tmp"
  "$visudo_bin" -cf "$tmp" >/dev/null \
    || die "сгенерированный sudoers не прошёл проверку visudo — права панели не выданы"
  install -m 0640 -o root -g root "$tmp" "$SUDOERS_PANEL"
  ok "sudoers: $SUDOERS_PANEL (0640 root:root, без wildcard)"
}

info "Пользователь routeweave и sudoers..."
ensure_routeweave_user
ok "пользователь routeweave готов (системный, без home и shell)"
write_panel_sudoers

# ---------- 2. Параметры ----------
echo
info "Параметры установки (Enter = значение по умолчанию)"
ask_valid LAN_IFACE  "LAN-интерфейс сервера" "$DETECTED_IFACE" valid_iface \
  "ожидается имя интерфейса: буквы, цифры, точка, двоеточие, дефис, подчёркивание"
ask_valid SERVER_IP  "IP сервера в локалке"  "$DETECTED_IP" valid_ipv4 \
  "ожидается IPv4, например 192.168.1.10"
ask_valid SOCKS_URL  "Socks/https-прокси для гео (можно пусто)" "${SOCKS_URL:-}" valid_socks \
  "формат socks5h://host:port (допустимы socks5, socks, http, https)"
ask BASE_DOMAIN     "Домен для CF Worker (поддомен dpdns.org, можно пусто)" "${BASE_DOMAIN:-}"
ask_valid WG_SUBNET  "Подсеть WireGuard (клиенты WDTT)" "$WG_SUBNET_DEFAULT" valid_cidr \
  "ожидается IPv4-подсеть, например 10.66.66.0/24"
ask_valid PANEL_PORT "Порт веб-панели" "${PANEL_PORT:-8080}" valid_port \
  "целое число 1..65535"
ask_valid PANEL_USER "Логин панели" "${PANEL_USER:-admin}" valid_user \
  "1-32 символа: латиница, цифры, точка, дефис, подчёркивание"
ask QUIC_DROP       "Резать QUIC (UDP/443)? yes/no" "${QUIC_DROP:-yes}"

# QUIC_DROP: мусор не должен ломать конфиг — трактуем как yes с предупреждением.
if ! QUIC_NORM=$(normalize_yn "$QUIC_DROP"); then
  warn "QUIC_DROP: нераспознанное значение «$QUIC_DROP» — использую yes"
  QUIC_NORM=yes
fi
QUIC_DROP="$QUIC_NORM"

# WDTT-Plus: спрашиваем только на amd64. Переменная объявлена заранее
# (дефолт no), чтобы set -u не падал; по умолчанию ставим только ядро.
INSTALL_WDTT="${INSTALL_WDTT:-no}"
if [ "$WDTT_SUPPORTED" = 1 ]; then
  ask INSTALL_WDTT "Установить сервер WDTT-Plus (VPN для Android)? yes/no" "${INSTALL_WDTT:-no}"
  if ! INSTALL_WDTT=$(normalize_yn "$INSTALL_WDTT"); then
    warn "INSTALL_WDTT: нераспознанное значение — считаю no"
    INSTALL_WDTT=no
  fi
else
  INSTALL_WDTT=no
fi

# tg-ws-proxy-rs (Telegram MTProto) — только по явному согласию: требует Docker.
INSTALL_TGWS="${INSTALL_TGWS:-no}"
ask INSTALL_TGWS "Установить tg-ws-proxy (Telegram MTProto, нужен Docker)? yes/no" "${INSTALL_TGWS:-no}"
if ! INSTALL_TGWS=$(normalize_yn "$INSTALL_TGWS"); then
  warn "INSTALL_TGWS: нераспознанное значение — считаю no"
  INSTALL_TGWS=no
fi

# Tailscale (exit node для tailnet) — только по явному согласию: ставится из
# официального apt-репозитория, поддерживаются Ubuntu/Debian.
INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-no}"
ask INSTALL_TAILSCALE "Установить Tailscale (exit node для tailnet)? yes/no" "${INSTALL_TAILSCALE:-no}"
if ! INSTALL_TAILSCALE=$(normalize_yn "$INSTALL_TAILSCALE"); then
  warn "INSTALL_TAILSCALE: нераспознанное значение — считаю no"
  INSTALL_TAILSCALE=no
fi

PANEL_PASS_GENERATED=0
PANEL_PASS="${PANEL_PASS:-}"
if [ -z "$PANEL_PASS" ]; then
  PANEL_PASS=$(gen_hex 12)
  PANEL_PASS_GENERATED=1
fi
MIHOMO_SECRET="${MIHOMO_SECRET:-}"
if [ -z "$MIHOMO_SECRET" ]; then
  MIHOMO_SECRET=$(gen_hex 16)
fi
TG_SECRET="${TG_SECRET:-}"
if [ "$INSTALL_TGWS" = "yes" ] && [ -z "$TG_SECRET" ]; then
  TG_SECRET=$(gen_hex 16)
fi

ok "Параметры приняты"

# ---------- 3. Пакеты ----------
info "Установка пакетов (build tools, python3-venv, sudo)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  curl git build-essential \
  libnetfilter-queue-dev libmnl-dev zlib1g-dev libcap-dev \
  iptables nftables iproute2 openssl unzip jq wireguard-tools sudo \
  python3 python3-venv python3-yaml ca-certificates >/dev/null || \
  apt-get install -y curl git build-essential libnetfilter-queue-dev libmnl-dev zlib1g-dev libcap-dev \
  iptables nftables iproute2 openssl unzip jq wireguard-tools sudo \
  python3 python3-venv python3-yaml

ok "Пакеты установлены"
command -v visudo >/dev/null || die "visudo всё ещё недоступен — установите пакет sudo и повторите"

# ---------- 4. Порт 53 (systemd-resolved) ----------
# Drop-in помечается маркером: install/uninstall отличают свой файл от чужого.
# Без этого повторный install «бэкапит» собственную конфигурацию, а uninstall
# возвращает DNSStubListener=no — и система остаётся без DNS.
RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/routeweave.conf
RESOLVED_MARKER='# Generated by Routeweave installer'

write_resolved_dropin() {
  mkdir -p /etc/systemd/resolved.conf.d
  cat > "$RESOLVED_DROPIN" <<'EOF'
# Generated by Routeweave installer
[Resolve]
DNSStubListener=no
EOF
}

# 0 — файл создан Routeweave. Совместимость: до появления маркера файл имел
# ровно наше содержимое, такие бэкапы тоже считаем своими.
is_routeweave_dropin() {
  local f=${1:-$RESOLVED_DROPIN}
  [ -f "$f" ] || return 1
  grep -qxF "$RESOLVED_MARKER" "$f" 2>/dev/null && return 0
  [ "$(grep -vE '^[[:space:]]*(#|$)' "$f" 2>/dev/null)" = "$(printf '[Resolve]\nDNSStubListener=no')" ]
}

# Запоминает цель симлинка resolv.conf, чтобы uninstall вернул ссылку, а не
# обычный файл с её содержимым. <etc-dir> <файл-назначение>
save_resolv_symlink() {
  local etc=${1:-/etc} out=$2 target
  rm -f "$out" 2>/dev/null || true
  [ -L "$etc/resolv.conf" ] || return 1
  target=$(readlink "$etc/resolv.conf" 2>/dev/null) || return 1
  [ -n "$target" ] || return 1
  printf '%s\n' "$target" > "$out" || return 1
}

# Кто слушает 53: free | mihomo | resolved | other
port53_owner() {
  local out
  out=$({ ss -lunp 2>/dev/null; ss -ltnp 2>/dev/null; } | grep ':53 ' || true)
  if [ -z "$out" ]; then printf 'free'
  elif printf '%s\n' "$out" | grep -q '"mihomo"'; then printf 'mihomo'
  elif printf '%s\n' "$out" | grep -q '"systemd-resolve'; then printf 'resolved'
  else printf 'other'; fi
}

case "$(port53_owner)" in
  free)
    : # порт свободен — systemd-resolved не трогаем
    ;;
  mihomo)
    info "Порт 53 уже слушает mihomo — systemd-resolved не трогаю"
    ;;
  resolved)
    info "Освобождаю порт 53 (systemd-resolved stub)..."
    # Бэкап исходного resolv.conf — один раз, повторные запуски не перезаписывают.
    if [ ! -e "$BACKUP_DIR/resolv.conf.orig" ] && [ -e /etc/resolv.conf ]; then
      cp -aL /etc/resolv.conf "$BACKUP_DIR/resolv.conf.orig"
      # Цель ссылки сохраняем один раз вместе с бэкапом: при повторной
      # установке /etc/resolv.conf уже обычный файл, и перезапись потеряла бы
      # исходную цель.
      save_resolv_symlink /etc "$BACKUP_DIR/resolv.conf.orig.link" || true
    fi
    # Бэкапим drop-in только если он чужой: свой файл восстанавливать не нужно.
    if [ -f "$RESOLVED_DROPIN" ] && ! is_routeweave_dropin "$RESOLVED_DROPIN" \
       && [ ! -e "$BACKUP_DIR/resolved-routeweave.conf.orig" ]; then
      cp -a "$RESOLVED_DROPIN" "$BACKUP_DIR/resolved-routeweave.conf.orig"
    fi
    write_resolved_dropin
    systemctl restart systemd-resolved
    # Атомарная замена: временный файл в /etc + mv (никаких rm -f + запись).
    RESOLV_TMP=$(mktemp /etc/.resolv.conf.XXXXXX)
    printf 'nameserver 77.88.8.8\nnameserver 77.88.8.1\n' > "$RESOLV_TMP"
    chmod 0644 "$RESOLV_TMP"
    mv -f "$RESOLV_TMP" /etc/resolv.conf
    ok "Порт 53 свободен (системный DNS теперь 77.88.8.8, после старта mihomo — он сам)"
    ;;
  other)
    warn "Порт 53 занят (UDP/TCP) неизвестным сервисом — mihomo не сможет слушать DNS!"
    { ss -lunp 2>/dev/null; ss -ltnp 2>/dev/null; } | grep ':53 ' || true
    ;;
esac

# ---------- 5. Бинарник mihomo ----------
MIHOMO_INSTALLED_VER=""
if command -v mihomo >/dev/null 2>&1; then
  MIHOMO_INSTALLED_VER=$(mihomo -v 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)
fi

fetch_sha() { # fetch_sha <url> [запасная сумма] -> sha256 (может быть пусто)
  local url=$1 fallback=${2:-} got=""
  got=$(curl -fsSL --max-time 20 "$url.sha256" 2>/dev/null | head -n1 | awk '{print $1}') || got=""
  got=${got:0:60}
  if [ -n "$got" ] && [ -z "${got//[0-9a-fA-F]/}" ]; then
    printf '%s' "$got"
    return 0
  fi
  printf '%s' "$fallback"
}

verify_sha() { # verify_sha <файл> <сумма> <метка>
  local file=$1 sum=$2 label=$3 actual
  if [ -z "$sum" ] || [ "${#sum}" -lt 32 ]; then
    warn "$label: контрольная сумма недоступна — целостность НЕ проверена"
    return 0
  fi
  actual=$(sha256sum "$file" | awk '{print $1}')
  case "$actual" in
    "$sum"*) ok "$label: sha256 подтверждена (${sum:0:12}…)" ;;
    *) die "$label: sha256 не совпала (ожидалось ${sum:0:12}…, получено ${actual:0:12}…) — файл повреждён или подменён" ;;
  esac
}

VER=""
VER=$(curl -fsSL --max-time 15 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])' 2>/dev/null) || VER=""

if [ -z "$VER" ]; then
  if [ -n "$MIHOMO_INSTALLED_VER" ]; then
    warn "GitHub API недоступен — оставляю установленный mihomo $MIHOMO_INSTALLED_VER"
  else
    VER="$MIHOMO_FALLBACK_VER"
    warn "GitHub API недоступен — ставлю закреплённый выпуск $VER"
  fi
fi

if [ -n "$VER" ] && [ "$VER" = "$MIHOMO_INSTALLED_VER" ]; then
  ok "mihomo $VER уже установлен — обновление не требуется"
elif [ -n "$VER" ]; then
  info "Загрузка mihomo $VER ($MIHOMO_ARCH / $MIHOMO_ASSET_ARCH)..."
  URL="https://github.com/MetaCubeX/mihomo/releases/download/${VER}/mihomo-linux-${MIHOMO_ASSET_ARCH}-${VER}.gz"
  curl -fsSL --retry 2 --max-time 180 -o "$WORK_DIR/mihomo.gz" "$URL" \
    || die "Не удалось скачать mihomo ($URL). Проверьте связь с github."
  verify_sha "$WORK_DIR/mihomo.gz" "$(fetch_sha "$URL" "${MIHOMO_SHA256:-}")" "mihomo $VER"
  gunzip -c "$WORK_DIR/mihomo.gz" > "$WORK_DIR/mihomo" \
    || die "Не удалось распаковать архив mihomo ($URL)"
  install -m 0755 "$WORK_DIR/mihomo" /usr/local/bin/mihomo
  mihomo -v >/dev/null 2>&1 \
    || die "mihomo $VER установлен, но не запускается — проверьте /usr/local/bin/mihomo"
  ok "mihomo $VER установлен: $(mihomo -v | head -n1)"
fi

# ---------- 6. Конфигурация /etc/routeweave ----------
info "Создание ${ETC}..."
install -d -m 0755 "$ETC/lists"
cp -n "$APP_DIR"/lists/*.txt "$ETC/lists/" 2>/dev/null || true

TG_CF_WORKER_DOMAIN="${TG_CF_WORKER_DOMAIN:-$BASE_DOMAIN}"
TG_LINK_IP="${TG_LINK_IP:-}"
FAKEIP_EXTRA="${FAKEIP_EXTRA:-}"
# Порядок апстримов tg-ws-proxy; проверяется до записи .env/.env.tgws.
TG_PINNED_UPSTREAM=$(tgws_pinned_upstream "$TG_CF_WORKER_DOMAIN") \
  || die "TG_PINNED_UPSTREAM: ожидается список тиров через запятую: ws, cfworker, cfproxy, mtproto, tcp"

if [ ! -f "$ETC/.env" ]; then
  {
    echo "# Routeweave: сгенерировано install.sh. Значения в двойных кавычках."
    echo "# Права 0640 root:routeweave; PANEL_PASS обязателен для запуска панели."
    env_line LAN_IFACE "$LAN_IFACE"
    env_line SERVER_IP "$SERVER_IP"
    env_line SOCKS_URL "$SOCKS_URL"
    env_line BASE_DOMAIN "$BASE_DOMAIN"
    echo "# Домен CF Worker для tg-ws-proxy (опционально; обычно совпадает с BASE_DOMAIN)"
    env_line TG_CF_WORKER_DOMAIN "$TG_CF_WORKER_DOMAIN"
    if [ -n "$TG_PINNED_UPSTREAM" ]; then
      echo "# Порядок апстримов tg-ws-proxy (--pinned-upstream), дублируется в .env.tgws"
      env_line TG_PINNED_UPSTREAM "$TG_PINNED_UPSTREAM"
    fi
    echo "# IP/хост для tg://-ссылки (пусто = SERVER_IP)"
    env_line TG_LINK_IP "$TG_LINK_IP"
    env_line WG_SUBNET "$WG_SUBNET"
    env_line PANEL_PORT "$PANEL_PORT"
    env_line PANEL_USER "$PANEL_USER"
    env_line PANEL_PASS "$PANEL_PASS"
    env_line MIHOMO_SECRET "$MIHOMO_SECRET"
    env_line TG_SECRET "$TG_SECRET"
    env_line QUIC_DROP "$QUIC_DROP"
    env_line FAKEIP_EXTRA "$FAKEIP_EXTRA"
  } > "$WORK_DIR/env.new"
  install -m 0640 -o root -g routeweave "$WORK_DIR/env.new" "$ETC/.env"
  ok ".env создан: $ETC/.env (0640 root:routeweave)"
else
  warn "$ETC/.env уже существует — не перезаписываю"
  ENV_SECRETS_UPDATED=0
  for key in PANEL_PASS MIHOMO_SECRET; do
    cur=$(sed -n "s/^${key}=//p" "$ETC/.env" | head -n1)
    cur=${cur%\"}
    cur=${cur#\"}
    if [ -n "$cur" ]; then
      continue
    fi
    if [ "$key" = "PANEL_PASS" ]; then
      val="$PANEL_PASS"
      [ -n "$val" ] || { val=$(gen_hex 12); PANEL_PASS=$val; PANEL_PASS_GENERATED=1; }
    else
      val="$MIHOMO_SECRET"
      [ -n "$val" ] || { val=$(gen_hex 16); MIHOMO_SECRET=$val; }
    fi
    if grep -q "^${key}=" "$ETC/.env"; then
      sed -i "s|^${key}=.*|$(env_line "$key" "$val")|" "$ETC/.env"
    else
      [ -z "$(tail -c1 "$ETC/.env")" ] || printf '\n' >> "$ETC/.env"
      env_line "$key" "$val" >> "$ETC/.env"
    fi
    ENV_SECRETS_UPDATED=1
  done
  if [ "$ENV_SECRETS_UPDATED" = 1 ]; then
    ok "в .env добавлены отсутствующие секреты (PANEL_PASS/MIHOMO_SECRET)"
  fi
  # Пин апстримов нужен и старым установкам: compose читает общий .env,
  # а устаревший пин (без домена CF Worker) лучше убрать, чем оставить.
  if [ "$INSTALL_TGWS" = "yes" ]; then
    if [ -n "$TG_PINNED_UPSTREAM" ]; then
      if grep -q '^TG_PINNED_UPSTREAM=' "$ETC/.env"; then
        sed -i "s|^TG_PINNED_UPSTREAM=.*|$(env_line TG_PINNED_UPSTREAM "$TG_PINNED_UPSTREAM")|" "$ETC/.env"
      else
        [ -z "$(tail -c1 "$ETC/.env")" ] || printf '\n' >> "$ETC/.env"
        env_line TG_PINNED_UPSTREAM "$TG_PINNED_UPSTREAM" >> "$ETC/.env"
      fi
      ok "в .env закреплён порядок апстримов tg-ws-proxy: $TG_PINNED_UPSTREAM"
    elif grep -q '^TG_PINNED_UPSTREAM=' "$ETC/.env"; then
      sed -i '/^TG_PINNED_UPSTREAM=/d' "$ETC/.env"
      ok "из .env удалён устаревший TG_PINNED_UPSTREAM (домен CF Worker не задан)"
    fi
  fi
fi

chown root:routeweave "$ETC/.env"
chmod 0640 "$ETC/.env"

# Панель работает от routeweave: ей нужны .env (чтение) и каталог списков (запись).
chown -R routeweave:routeweave "$ETC/lists"
chmod 0755 "$ETC/lists"
find "$ETC/lists" -maxdepth 1 -type f -exec chmod 0644 {} +

# .env.tgws — отдельный файл для docker compose (без секретов панели/mihomo).
if [ "$INSTALL_TGWS" = "yes" ]; then
  {
    env_line TG_SECRET "$TG_SECRET"
    env_line BASE_DOMAIN "$BASE_DOMAIN"
    env_line TG_CF_WORKER_DOMAIN "$TG_CF_WORKER_DOMAIN"
    if [ -n "$TG_PINNED_UPSTREAM" ]; then
      env_line TG_PINNED_UPSTREAM "$TG_PINNED_UPSTREAM"
    fi
  } > "$WORK_DIR/env.tgws"
  install -m 0640 -o root -g root "$WORK_DIR/env.tgws" "$ETC/.env.tgws"
  ok "$ETC/.env.tgws создан (0640 root:root)"
fi

# fw-скрипты и конфиг zapret
install -m 0755 "$APP_DIR/templates/fw-base.sh" "$ETC/fw-base.sh"
install -m 0755 "$APP_DIR/templates/fw-wan.sh" "$ETC/fw-wan.sh"
install -m 0755 "$APP_DIR/templates/fw-tailscale.sh" "$ETC/fw-tailscale.sh"
install -m 0755 "$APP_DIR/templates/fw-nfq.sh"  "$ETC/fw-nfq.sh"

# .env — источник истины для config.yaml и LAN_IFACE; из LAN_IFACE ниже
# рендерится WAN_IFACE= в zapret.conf (это одно и то же — WAN-интерфейс шлюза).
set -a
# shellcheck disable=SC1091
. "$ETC/.env"
set +a

# Старый/рукописный .env может не содержать LAN_IFACE — иначе в zapret.conf
# попал бы пустой WAN_IFACE=. Подстраховываемся автодетектом (как в fw-nfq.sh).
if [ -z "${LAN_IFACE:-}" ]; then
  LAN_IFACE=$(ip route get 1.1.1.1 2>/dev/null \
    | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}') || true
  [ -n "${LAN_IFACE:-}" ] \
    || die "LAN_IFACE не задан в $ETC/.env и не определён автоматически — добавьте LAN_IFACE=\"eth0\" и повторите"
  warn "LAN_IFACE отсутствовал в .env — использую автодетект: $LAN_IFACE"
fi

merge_zapret_conf "$APP_DIR/templates/zapret.conf.tpl" "$ETC/zapret.conf"

# config.yaml: рендер во временный файл, сравнение, бэкап, атомарная установка.
info "Рендер config.yaml..."
if ! python3 "$APP_DIR/templates/render_mihomo.py" "$ETC/.env" "$WORK_DIR/config.yaml" >/dev/null; then
  die "render_mihomo.py завершился ошибкой — проверьте $ETC/.env"
fi
chmod 0600 "$WORK_DIR/config.yaml"
if [ -f "$ETC/config.yaml" ] && cmp -s "$WORK_DIR/config.yaml" "$ETC/config.yaml"; then
  info "config.yaml не изменился"
else
  if [ -f "$ETC/config.yaml" ]; then
    backup_file "$ETC/config.yaml"
  fi
  install -m 0600 "$WORK_DIR/config.yaml" "$ETC/config.yaml"
  ok "config.yaml сгенерирован (0600 root)"
fi

# ---------- 7. zapret (сборка из исходников) ----------
ZAPRET_REF="${ZAPRET_REF:-master}"
if [ ! -d "$ZAPRET_DIR" ]; then
  info "Клонирование zapret ($ZAPRET_REF)..."
  git clone --depth 1 --branch "$ZAPRET_REF" https://github.com/bol-van/zapret "$ZAPRET_DIR" \
    || die "Не удалось склонировать zapret (ветка $ZAPRET_REF)"
elif [ -d "$ZAPRET_DIR/.git" ]; then
  if git -C "$ZAPRET_DIR" fetch --depth 1 origin "$ZAPRET_REF" 2>/dev/null \
     && git -C "$ZAPRET_DIR" checkout -q FETCH_HEAD; then
    ok "zapret обновлён до $ZAPRET_REF"
  else
    warn "не удалось обновить zapret до $ZAPRET_REF — продолжаю с текущим кодом"
  fi
else
  warn "$ZAPRET_DIR существует, но это не git-репозиторий — обновление пропущено"
fi

if [ -d "$ZAPRET_DIR/.git" ]; then
  git -C "$ZAPRET_DIR" rev-parse HEAD > "$ETC/zapret.commit" 2>/dev/null || true
  chmod 0644 "$ETC/zapret.commit"
fi

if [ -x "$ZAPRET_DIR/bin/nfqws" ]; then
  ok "zapret уже собран: $ZAPRET_DIR/bin/nfqws"
else
  info "Сборка zapret (2-5 минут)..."
  [ -d "$ZAPRET_DIR" ] || die "каталог $ZAPRET_DIR отсутствует — сборка невозможна"
  for d in tpws nfq ip2net mdig; do
    make -C "$ZAPRET_DIR/$d" >/dev/null || die "Сборка zapret/$d не удалась (см. вывод выше)"
  done
  mkdir -p "$ZAPRET_DIR/bin"
  cp -f "$ZAPRET_DIR/tpws/tpws" "$ZAPRET_DIR/nfq/nfqws" "$ZAPRET_DIR/ip2net/ip2net" "$ZAPRET_DIR/mdig/mdig" "$ZAPRET_DIR/bin/"
  [ -x "$ZAPRET_DIR/bin/nfqws" ] || die "nfqws не собрался"
  ok "zapret собран: $ZAPRET_DIR/bin/nfqws"
fi

install -m 0755 "$APP_DIR/templates/nfqws-launch.sh" "$ETC/nfqws-launch.sh"

# ---------- 8. systemd-юниты и helper-скрипты ----------
info "Установка systemd-юнитов..."
install -m 0644 "$APP_DIR/templates/mihomo.service"                  /etc/systemd/system/mihomo.service
install -m 0644 "$APP_DIR/templates/routeweave-firewall.service"     /etc/systemd/system/routeweave-firewall.service
install -m 0644 "$APP_DIR/templates/zapret-nfqws.service"            /etc/systemd/system/zapret-nfqws.service
install -m 0644 "$APP_DIR/templates/routeweave-panel.service"        /etc/systemd/system/routeweave-panel.service
install -m 0644 "$APP_DIR/templates/routeweave-lists-update.service" /etc/systemd/system/routeweave-lists-update.service
install -m 0644 "$APP_DIR/templates/routeweave-lists-update.timer"   /etc/systemd/system/routeweave-lists-update.timer

install -m 0755 "$APP_DIR/templates/routeweave-update-lists.sh"      /usr/local/sbin/routeweave-update-lists.sh
install -m 0755 "$APP_DIR/templates/routeweave-tgws-restart.sh"      "$TGWS_RESTART"
install -m 0755 "$APP_DIR/templates/routeweave-tgws-state.sh"        "$TGWS_STATE"
install -m 0755 "$APP_DIR/templates/routeweave-tailscale-state.sh"   "$TAILSCALE_STATE"
ok "helper-скрипты: /usr/local/sbin/routeweave-{update-lists,tgws-restart,tgws-state,tailscale-state}.sh"

# ---------- 9. Панель (venv + статика zashboard) ----------
info "Установка веб-панели..."
install -d -m 0755 "$OPT_PANEL" "$OPT_PANEL/static"
install -m 0644 "$APP_DIR/panel/app.py" "$OPT_PANEL/app.py"
for f in index.html app.css app.js; do
  install -m 0644 "$APP_DIR/panel/static/$f" "$OPT_PANEL/static/$f"
done
python3 -m venv "$OPT_PANEL/venv"
"$OPT_PANEL/venv/bin/pip" install -q --upgrade pip
"$OPT_PANEL/venv/bin/pip" install -q -r "$APP_DIR/panel/requirements.txt"
chown -R root:root "$OPT_PANEL"
find "$OPT_PANEL" -type d -exec chmod 0755 {} +
ok "панель установлена: $OPT_PANEL (root:root, каталоги 0755)"

info "Загрузка zashboard..."
ZASH_URL="https://github.com/Zephyruso/zashboard/releases/latest/download/dist.zip"
if curl -fsSL --retry 2 --max-time 120 -o "$WORK_DIR/dist.zip" "$ZASH_URL"; then
  verify_sha "$WORK_DIR/dist.zip" "$(fetch_sha "$ZASH_URL" "${ZASHBOARD_SHA256:-}")" "zashboard"
  rm -rf "$ETC/zashboard"
  install -d -m 0755 "$ETC/zashboard"
  unzip -q -o "$WORK_DIR/dist.zip" -d "$WORK_DIR/zash" \
    || die "не удалось распаковать $ZASH_URL"
  # в архиве папка dist/
  if [ -d "$WORK_DIR/zash/dist" ]; then
    cp -a "$WORK_DIR/zash/dist/." "$ETC/zashboard/"
  else
    cp -a "$WORK_DIR/zash/." "$ETC/zashboard/"
  fi
  ok "zashboard: http://$SERVER_IP:9090/ui/"
else
  warn "zashboard не скачался ($ZASH_URL) — дашборд будет недоступен (панель продолжит работать)"
fi

# ---------- 10. tg-ws-proxy-rs (docker) ----------
if [ "$INSTALL_TGWS" = "yes" ]; then
  info "Запуск tg-ws-proxy-rs (docker)..."
  command -v docker >/dev/null 2>&1 || die "tg-ws-proxy требует Docker Engine и compose v2. Установите Docker (https://docs.docker.com/engine/install/) и повторите, либо запустите без INSTALL_TGWS=yes."
  docker compose version >/dev/null 2>&1 || die "docker compose v2 не найден. Установите плагин compose v2 (docker-compose-plugin) и повторите."
  systemctl enable --now docker >/dev/null 2>&1 || warn "не удалось включить docker.service — проверьте вручную"
  ( cd "$APP_DIR" && docker compose up -d ) || die "docker compose не смог поднять tg-ws-proxy"
  ok "tg-ws-proxy-rs запущен"
else
  info "tg-ws-proxy: пропущен (INSTALL_TGWS=no)"
fi

# ---------- 11. Tailscale (exit node для tailnet) ----------
ts_fail() { warn "Tailscale: $*"; TS_STATUS="ошибка"; return 1; }

# BackendState через python3: jq не обязателен (хотя и есть в пакетах).
ts_backend_state() {
  tailscale status --json 2>/dev/null | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(data.get("BackendState", ""))
' 2>/dev/null
}

# Демон после enable --now отвечает не сразу: до ~10 секунд ждём
# определённого состояния, чтобы не уйти в авторизацию по пустому ответу.
ts_wait_state() {
  local state=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    state=$(ts_backend_state) || state=""
    case "$state" in
      Running|NeedsLogin|NeedsMachineAuth|Stopped) break ;;
    esac
    sleep 1
  done
  printf '%s' "$state"
}

install_tailscale() {
  local os_id="${ID:-}" os_codename="${VERSION_CODENAME:-}"
  local urls key_url list_url auth_arg="" auth_tmp="" state

  if command -v tailscale >/dev/null 2>&1; then
    info "Tailscale уже установлен — пакет не переустанавливаю"
    TS_STATUS="установлен"
  else
    if ! urls=$(tailscale_repo_urls "$os_id" "$os_codename"); then
      ts_fail "официальный apt-репозиторий поддерживается только для Ubuntu/Debian с известным codename (ID=${os_id:-?}, VERSION_CODENAME=${os_codename:-?}); шаг пропущен"
      return 1
    fi
    key_url=$(printf '%s\n' "$urls" | sed -n '1p')
    list_url=$(printf '%s\n' "$urls" | sed -n '2p')
    info "Добавление apt-репозитория Tailscale ($os_id/$os_codename)..."
    install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d \
      || { ts_fail "не удалось создать каталоги apt"; return 1; }
    curl -fsSL --retry 2 --max-time 60 -o "$WORK_DIR/tailscale-archive-keyring.gpg" "$key_url" \
      || { ts_fail "не удалось скачать $key_url"; return 1; }
    curl -fsSL --retry 2 --max-time 60 -o "$WORK_DIR/tailscale.list" "$list_url" \
      || { ts_fail "не удалось скачать $list_url"; return 1; }
    install -m 0644 "$WORK_DIR/tailscale-archive-keyring.gpg" "$TS_REPO_KEYRING" \
      || { ts_fail "не удалось установить $TS_REPO_KEYRING"; return 1; }
    install -m 0644 "$WORK_DIR/tailscale.list" "$TS_REPO_LIST" \
      || { ts_fail "не удалось установить $TS_REPO_LIST"; return 1; }
    apt-get update -qq \
      || { ts_fail "apt-get update завершился ошибкой"; return 1; }
    apt-get install -y -qq tailscale tailscale-archive-keyring \
      || { ts_fail "не удалось установить пакет tailscale"; return 1; }
    TS_STATUS="установлен"
    ok "Tailscale установлен из официального apt-репозитория"
  fi

  systemctl enable --now tailscaled >/dev/null 2>&1 \
    || { ts_fail "не удалось запустить tailscaled (systemctl status tailscaled)"; return 1; }

  state=$(ts_wait_state)
  info "Tailscale BackendState: ${state:-неизвестно}"

  if [ "$state" = "Running" ]; then
    tailscale set --advertise-exit-node --netfilter-mode=nodivert --accept-dns=false \
      || { ts_fail "tailscale set завершился ошибкой"; return 1; }
    TS_STATUS="настроен"
    ok "Tailscale: exit node анонсирован, netfilter-mode=nodivert, accept-dns=false"
  else
    if [ -n "$TS_AUTHKEY_FILE" ]; then
      case "$TS_AUTHKEY_FILE" in
        /*) ;;
        *) ts_fail "TS_AUTHKEY_FILE должен быть абсолютным путём: $TS_AUTHKEY_FILE"; return 1 ;;
      esac
      if [ ! -f "$TS_AUTHKEY_FILE" ] || [ ! -r "$TS_AUTHKEY_FILE" ]; then
        ts_fail "TS_AUTHKEY_FILE не читается: $TS_AUTHKEY_FILE"
        return 1
      fi
      auth_arg="--auth-key=file:$TS_AUTHKEY_FILE"
    elif [ -n "$TS_AUTHKEY" ]; then
      auth_tmp="$WORK_DIR/ts-authkey"
      ( umask 077; printf '%s\n' "$TS_AUTHKEY" > "$auth_tmp" ) \
        || { ts_fail "не удалось записать временный файл ключа"; return 1; }
      auth_arg="--auth-key=file:$auth_tmp"
    fi

    if [ -n "$auth_arg" ]; then
      info "Авторизация Tailscale..."
      if ! tailscale up "$auth_arg" \
          --advertise-exit-node --netfilter-mode=nodivert --accept-dns=false --timeout=60s; then
        if [ -n "$auth_tmp" ]; then rm -f "$auth_tmp" 2>/dev/null || true; fi
        unset TS_AUTHKEY
        ts_fail "tailscale up завершился ошибкой — проверьте ключ и журнал tailscaled"
        return 1
      fi
      TS_STATUS="настроен"
      ok "Tailscale авторизован: exit node анонсирован"
    else
      TS_STATUS="требуется авторизация"
      warn "Tailscale: требуется авторизация — ключ не задан, tailscale up не запускаю"
      echo "  Выполните вручную:"
      echo "  sudo tailscale up --advertise-exit-node --netfilter-mode=nodivert --accept-dns=false --timeout=60s"
    fi
  fi

  if [ -n "$auth_tmp" ]; then rm -f "$auth_tmp" 2>/dev/null || true; fi
  unset TS_AUTHKEY
  # fw-base.sh при старте смотрит наличие tailscale0; перезапускаем, чтобы
  # правила для exit-node появились сразу после установки.
  systemctl restart routeweave-firewall >/dev/null 2>&1 || true
  return 0
}

if [ "$INSTALL_TAILSCALE" = "yes" ]; then
  info "Шаг Tailscale..."
  install_tailscale || FAIL=1
else
  info "Tailscale: пропущен (INSTALL_TAILSCALE=no)"
  TS_STATUS="пропущен"
fi

# ---------- 12. Firewall / ufw ----------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  info "ufw активен — добавляю разрешения для локалки..."
  ufw route allow in on "$LAN_IFACE" >/dev/null 2>&1 || true

  LAN_CIDR=$(ip -4 addr show "$LAN_IFACE" | awk '/inet /{print $2; exit}')
  PORTS_TCP="53 7890 $PANEL_PORT 9090"
  if [ "$INSTALL_TGWS" = "yes" ]; then
    PORTS_TCP="$PORTS_TCP 1443"
  fi

  # Tailscale может быть поставлен установщиком (INSTALL_TAILSCALE=yes) или
  # уже стоять; если команда есть, открываем tailnet те же порты, что LAN/WDTT,
  # и разрешаем forwarded-трафик.
  TS_CIDR=""
  if command -v tailscale >/dev/null 2>&1; then
    TS_CIDR="${TS_SUBNET:-100.64.0.0/10}"
    TS_IFACE="${TS_IFACE:-tailscale0}"
    ufw route allow in on "$TS_IFACE" >/dev/null 2>&1 || true
    ufw route allow out on "$TS_IFACE" >/dev/null 2>&1 || true
    # Прямые соединения tailscale (когда DERP не нужен).
    ufw allow 41641/udp >/dev/null 2>&1 || true
    # CGNAT-спуфинг с LAN: пакет с tailnet-источником не должен приходить
    # из локальной сети. insert 1 — deny обязан стоять выше allow-правил.
    ufw insert 1 deny in on "$LAN_IFACE" from "$TS_CIDR" >/dev/null 2>&1 || true
  fi

  # Один и тот же набор портов — для WDTT-подсети, LAN-подсети и tailnet.
  for src in "$WG_SUBNET" "$LAN_CIDR" "${TS_CIDR:-}"; do
    [ -n "$src" ] || continue
    for p in $PORTS_TCP; do
      ufw allow from "$src" to any port "$p" proto tcp >/dev/null 2>&1 || true
    done
    ufw allow from "$src" to any port 53 proto udp >/dev/null 2>&1 || true
  done
  if [ -n "$TS_CIDR" ]; then
    ok "ufw: правила добавлены (tcp: $PORTS_TCP; udp: 53; tailscale: $TS_CIDR через $TS_IFACE, 41641/udp; спуф с $LAN_IFACE запрещён)"
  else
    ok "ufw: правила добавлены (tcp: $PORTS_TCP; udp: 53)"
  fi
fi

# ---------- 13. Запуск ----------
info "Запуск сервисов..."
systemctl daemon-reload
systemctl enable --now routeweave-firewall mihomo zapret-nfqws routeweave-panel >/dev/null 2>&1 || true
systemctl enable --now routeweave-lists-update.timer >/dev/null 2>&1 \
  || warn "не удалось включить routeweave-lists-update.timer — проверьте: systemctl status routeweave-lists-update.timer"
sleep 3

for u in routeweave-firewall mihomo zapret-nfqws routeweave-panel routeweave-lists-update.timer; do
  st=$(systemctl is-active "$u" || true)
  if [ "$st" = "active" ]; then ok "$u: active"; else warn "$u: $st (journalctl -u $u -n 30)"; FAIL=1; fi
done
[ "$FAIL" = 0 ] || warn "Есть незапущенные сервисы — установка завершена, но проверьте их!"

info "Первый прогон обновления списков..."
systemctl start routeweave-lists-update.service \
  || warn "обновление списков завершилось ошибкой — journalctl -u routeweave-lists-update -n 50"

# ---------- 14. WDTT-Plus (VPN-сервер для Android) ----------
if [ "$INSTALL_WDTT" = "yes" ]; then
  info "Шаг WDTT-Plus..."
  install_wdtt || FAIL=1
else
  info "WDTT-Plus: пропущен (INSTALL_WDTT=no)"
  WDTT_STATUS="пропущен"
fi

# ---------- Итог ----------
WDTT_OK=0
if [ "$INSTALL_WDTT" = "yes" ]; then
  case "$WDTT_STATUS" in установлен|"уже установлен") WDTT_OK=1 ;; esac
fi
TS_OK=0
if [ "$INSTALL_TAILSCALE" = "yes" ]; then
  case "$TS_STATUS" in установлен|настроен) TS_OK=1 ;; esac
fi

echo
echo -e "${C_G}================ Установка завершена ================${C_N}"
echo -e "Панель:        ${C_B}http://$SERVER_IP:$PANEL_PORT${C_N}  (логин: $PANEL_USER)"
if [ "$PANEL_PASS_GENERATED" = 1 ]; then
  echo -e "Пароль панели: ${C_Y}$PANEL_PASS${C_N}  (сохранён в $ETC/.env → PANEL_PASS)"
else
  echo -e "Пароль панели: см. $ETC/.env → PANEL_PASS"
fi
echo -e "Zashboard:     ${C_B}http://$SERVER_IP:9090/ui/${C_N}  (backend: http://$SERVER_IP:9090, секрет: $ETC/.env → MIHOMO_SECRET)"
echo -e "Конфиг:        $ETC/.env (0640 root:routeweave)"
echo -e "Sudoers:       $SUDOERS_PANEL (панель работает от пользователя routeweave)"
if [ "$INSTALL_TGWS" = "yes" ]; then
  echo -e "TG MTProto:    секрет — ${C_B}docker logs routeweave-tgwsproxy${C_N}"
fi
if [ "$WDTT_OK" = 1 ]; then
  echo -e "WDTT-Plus:     ${C_G}сервер установлен${C_N} (приложение WDTT Plus для Android)"
  echo -e "  DTLS:        ${C_B}$SERVER_IP:$WDTT_DTLS_PORT/udp${C_N} (пробросьте UDP на роутере/в панели провайдера)"
  echo -e "  Конфиг:      ${WDTT_INITIAL_CONFIG:-/root/wdtt-initial.json} (chmod 600, главный пароль внутри)"
  echo -e "  Управление:  ${C_B}$WDTT_DIR/install.sh status|doctor|monitor${C_N}"
fi
if [ "$INSTALL_TAILSCALE" = "yes" ]; then
  echo -e "Tailscale:     $TS_STATUS"
  case "$TS_STATUS" in
    установлен|настроен)
      echo -e "  Exit node:   ${C_Y}одобрите узел в админ-консоли Tailscale${C_N} (Machines → ... → Edit route settings)"
      echo -e "  DNS:         включите «Use with exit node» для этого узла, если нужен DNS через шлюз"
      ;;
    "требуется авторизация")
      echo -e "  Авторизация: ${C_B}sudo tailscale up --advertise-exit-node --netfilter-mode=nodivert --accept-dns=false --timeout=60s${C_N}"
      echo -e "  Exit node:   после входа одобрите узел в админ-консоли Tailscale"
      ;;
    ошибка)
      echo -e "  Установка Tailscale не удалась — шлюз работает без него; повторите с INSTALL_TAILSCALE=yes"
      ;;
  esac
fi

TAIL=""
STEP=4
if [ "$INSTALL_TGWS" = "yes" ]; then
  STEP=$((STEP + 1))
  TAIL="${TAIL}  $STEP. Telegram: пропишите MTProto-прокси для трёх сетей:
     дом   -> $SERVER_IP:1443
     WDTT  -> 10.66.66.1:1443
     TS    -> <tailscale-имя сервера>:1443
     секрет прокси: docker logs routeweave-tgwsproxy
"
  STEP=$((STEP + 1))
  TAIL="${TAIL}  $STEP. CF Worker для tg-ws-proxy (опционально): docs/cf-worker-tg.md
"
fi
if [ "$WDTT_OK" = 1 ]; then
  STEP=$((STEP + 1))
  TAIL="${TAIL}  $STEP. WDTT-Plus (Android): внешний IP/домен, UDP $WDTT_DTLS_PORT, главный пароль
     в ${WDTT_INITIAL_CONFIG:-/root/wdtt-initial.json}; управление —
     sudo $WDTT_DIR/install.sh status|doctor|monitor
"
fi
if [ "$TS_OK" = 1 ]; then
  STEP=$((STEP + 1))
  TAIL="${TAIL}  $STEP. Tailscale: одобрите exit node в админ-консоли (Machines -> ...
     -> Edit route settings) и включите DNS «Use with exit node» для узла.
"
fi
cat <<EOF

Дальнейшие шаги (README.md, раздел «После установки»):
  1. На роутере в DHCP выдайте клиентам шлюз и DNS = $SERVER_IP
     (IPv6 в локалке лучше отключить — иначе клиенты пойдут мимо шлюза)
  2. Подбор стратегии zapret: sudo /opt/zapret/blockcheck.sh
     -> результат в $ETC/zapret.conf -> systemctl restart zapret-nfqws
  3. Автообновление списков: routeweave-lists-update.timer — ежедневно в 04:15
     + случайная задержка; журнал: journalctl -u routeweave-lists-update
  4. Списки доменов и custom-списки правьте в панели (порт $PANEL_PORT);
     файлы — в $ETC/lists/
$TAIL======================================================
EOF

exit "$FAIL"
