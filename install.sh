#!/usr/bin/env bash
# ============================================================
#  Routeweave — установщик для Ubuntu 20.04+/Debian 12+
#  Ядро: mihomo (прозрачный шлюз) + zapret (nfqws) + списки доменов +
#        веб-панель (FastAPI) + firewall (iptables/ufw)
#  Опционально:
#    - tg-ws-proxy-rs (Telegram MTProto, docker) — INSTALL_TGWS=yes
#    - WDTT-Plus (VPN-сервер для Android, amd64) — INSTALL_WDTT=yes
#  Запуск: sudo ./install.sh            (интерактивно)
#          sudo ./install.sh --defaults (без вопросов — только ядро)
# ============================================================
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC=/etc/homegateway
OPT_PANEL=/opt/homegateway-panel
ZAPRET_DIR=/opt/zapret
MIHOMO_FALLBACK_VER="v1.19.13"

C_G='\033[0;32m'; C_Y='\033[1;33m'; C_R='\033[0;31m'; C_B='\033[1;34m'; C_N='\033[0m'
info()  { echo -e "${C_B}[i]${C_N} $*"; }
ok()    { echo -e "${C_G}[+]${C_N} $*"; }
warn()  { echo -e "${C_Y}[!]${C_N} $*"; }
die()   { echo -e "${C_R}[x]${C_N} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo ./install.sh"

DEFAULTS=0
[ "${1:-}" = "--defaults" ] && DEFAULTS=1

ask() { # ask <переменная> <вопрос> <дефолт>
  local __var=$1 __q=$2 __def=${3:-} __ans
  if [ "$DEFAULTS" = 1 ] || [ ! -t 0 ]; then
    printf -v "$__var" '%s' "$__def"; return
  fi
  read -r -p "$__q [$__def]: " __ans || die "прервано"
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

gen_hex() { openssl rand -hex "${1:-16}" 2>/dev/null || head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-$((32)); }

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
WDTT_TMP=""
cleanup_wdtt_tmp() { [ -z "${WDTT_TMP:-}" ] || rm -rf "$WDTT_TMP"; }
trap cleanup_wdtt_tmp EXIT

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
    systemctl restart homegw-firewall >/dev/null 2>&1 || true
    [ -x "$WDTT_DIR/install.sh" ] && bash "$WDTT_DIR/install.sh" status || true
    return 0
  fi
  if [ -e /usr/local/bin/wdtt-server ] || [ -e /etc/wdtt ]; then
    wdtt_fail "найдены файлы WDTT без полной установки — разберите вручную: $WDTT_DIR/install.sh doctor"
    return 1
  fi

  WDTT_TMP=$(mktemp -d /tmp/homegw-wdtt.XXXXXX) || { wdtt_fail "не удалось создать временный каталог"; return 1; }

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
    curl -fsSL --max-time 180 -o "$WDTT_TMP/$name" "$url" || { wdtt_fail "не удалось скачать $url"; return 1; }
    curl -fsSL --max-time 60 -o "$WDTT_TMP/$name.sha256" "$url.sha256" || { wdtt_fail "не удалось скачать контрольную сумму"; return 1; }
    ( cd "$WDTT_TMP" && sha256sum -c "$name.sha256" >/dev/null ) || { wdtt_fail "sha256 архива не совпал"; return 1; }
    tar -xzf "$WDTT_TMP/$name" -C "$WDTT_TMP" || { wdtt_fail "не удалось распаковать архив"; return 1; }
    src=$(find "$WDTT_TMP" -mindepth 1 -maxdepth 1 -type d -name 'WDTT-Plus-server-*' | head -n1)
    [ -n "$src" ] || { wdtt_fail "в архиве нет каталога WDTT-Plus-server-*"; return 1; }
    ( cd "$src" && sha256sum -c SHA256SUMS >/dev/null ) || { wdtt_fail "внутренние sha256 не совпали"; return 1; }
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
  systemctl restart homegw-firewall >/dev/null 2>&1 || true
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

command -v curl >/dev/null || { apt-get update -qq; apt-get install -y -qq curl ca-certificates; }

# При повторной установке берём сетевые параметры из существующего .env:
# иначе ip route get уходит в TUN mihomo (dev Meta) вместо физического NIC.
if [ -f "$ETC/.env" ]; then
  # shellcheck disable=SC1091
  . "$ETC/.env"
fi

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

# ---------- 1. Параметры ----------
echo
info "Параметры установки (Enter = значение по умолчанию)"
ask LAN_IFACE     "LAN-интерфейс сервера"            "$DETECTED_IFACE"
ask SERVER_IP     "IP сервера в локалке"             "$DETECTED_IP"
ask SOCKS_URL     "Socks/https-прокси для гео (можно пусто)" ""
ask BASE_DOMAIN   "Домен для CF Worker (поддомен dpdns.org, можно пусто)" ""
ask WG_SUBNET     "Подсеть WireGuard (клиенты WDTT)" "$WG_SUBNET_DEFAULT"
ask PANEL_PORT    "Порт веб-панели"                  "8080"
PANEL_USER_DEFAULT="admin"
ask PANEL_USER    "Логин панели"                     "$PANEL_USER_DEFAULT"
ask QUIC_DROP     "Резать QUIC (UDP/443)? yes/no"    "yes"

# WDTT-Plus: спрашиваем только на amd64. Переменная объявлена заранее
# (дефолт no), чтобы set -u не падал; по умолчанию ставим только ядро.
INSTALL_WDTT="${INSTALL_WDTT:-no}"
if [ "$WDTT_SUPPORTED" = 1 ]; then
  ask INSTALL_WDTT "Установить сервер WDTT-Plus (VPN для Android)? yes/no" "${INSTALL_WDTT:-no}"
  case "${INSTALL_WDTT,,}" in
    yes|y|да|1) INSTALL_WDTT=yes ;;
    *) INSTALL_WDTT=no ;;
  esac
else
  INSTALL_WDTT=no
fi

# tg-ws-proxy-rs (Telegram MTProto) — только по явному согласию: требует Docker.
INSTALL_TGWS="${INSTALL_TGWS:-no}"
ask INSTALL_TGWS "Установить tg-ws-proxy (Telegram MTProto, нужен Docker)? yes/no" "${INSTALL_TGWS:-no}"
case "${INSTALL_TGWS,,}" in yes|y|да|1) INSTALL_TGWS=yes ;; *) INSTALL_TGWS=no ;; esac

PANEL_PASS="${PANEL_PASS:-$(gen_hex 12)}"
MIHOMO_SECRET="${MIHOMO_SECRET:-$(gen_hex 16)}"
TG_SECRET="${TG_SECRET:-}"
if [ "$INSTALL_TGWS" = "yes" ] && [ -z "$TG_SECRET" ]; then
  TG_SECRET=$(gen_hex 16)
fi

ok "Параметры приняты"

# ---------- 2. Пакеты ----------
info "Установка пакетов (build tools, python3-venv)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  curl git build-essential \
  libnetfilter-queue-dev libmnl-dev zlib1g-dev libcap-dev \
  iptables nftables iproute2 openssl unzip jq wireguard-tools \
  python3 python3-venv python3-yaml ca-certificates >/dev/null || \
  apt-get install -y curl git build-essential libnetfilter-queue-dev libmnl-dev zlib1g-dev libcap-dev \
  iptables nftables iproute2 openssl unzip jq wireguard-tools python3 python3-venv python3-yaml

ok "Пакеты установлены"

# ---------- 3. Порт 53 (systemd-resolved) ----------
if ss -lun 2>/dev/null | grep -q ':53 '; then
  if systemctl is-active --quiet systemd-resolved; then
    info "Освобождаю порт 53 (systemd-resolved stub)..."
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/homegw.conf <<'EOF'
[Resolve]
DNSStubListener=no
EOF
    systemctl restart systemd-resolved
    rm -f /etc/resolv.conf
    printf 'nameserver 77.88.8.8\nnameserver 77.88.8.1\n' > /etc/resolv.conf
    ok "Порт 53 свободен (системный DNS теперь 77.88.8.8, после старта mihomo — он сам)"
  else
    warn "Порт 53 занят неизвестным сервисом — mihomo не сможет слушать DNS!"
    ss -lunp | grep ':53 ' || true
  fi
fi

# ---------- 4. Бинарник mihomo ----------
MIHOMO_INSTALLED_VER=""
if command -v mihomo >/dev/null 2>&1; then
  MIHOMO_INSTALLED_VER=$(mihomo -v 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)
fi

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
  curl -fsSL --max-time 120 -o /tmp/mihomo.gz "$URL" || die "Не удалось скачать mihomo ($URL). Проверьте связь с github."
  gunzip -f /tmp/mihomo.gz
  install -m 0755 /tmp/mihomo /usr/local/bin/mihomo
  rm -f /tmp/mihomo
  mihomo -v >/dev/null 2>&1 || die "mihomo $VER установлен, но не запускается — проверьте /usr/local/bin/mihomo"
  ok "mihomo $VER установлен: $(mihomo -v | head -n1)"
fi

# ---------- 5. Конфигурация /etc/homegateway ----------
info "Создание ${ETC}..."
mkdir -p "$ETC/lists"
cp -n "$APP_DIR"/lists/*.txt "$ETC/lists/" 2>/dev/null || true

if [ ! -f "$ETC/.env" ]; then
  cat > "$ETC/.env" <<EOF
LAN_IFACE=$LAN_IFACE
SERVER_IP=$SERVER_IP
SOCKS_URL=$SOCKS_URL
BASE_DOMAIN=$BASE_DOMAIN
# Домен CF Worker для tg-ws-proxy (опционально; обычно совпадает с BASE_DOMAIN)
TG_CF_WORKER_DOMAIN=$BASE_DOMAIN
# IP/хост для tg://-ссылки (пусто = SERVER_IP)
TG_LINK_IP=
WG_SUBNET=$WG_SUBNET
PANEL_PORT=$PANEL_PORT
PANEL_USER=$PANEL_USER
PANEL_PASS=$PANEL_PASS
MIHOMO_SECRET=$MIHOMO_SECRET
TG_SECRET=$TG_SECRET
QUIC_DROP=$QUIC_DROP
FAKEIP_EXTRA=
EOF
  chmod 600 "$ETC/.env"
  ok ".env создан (правки: nano $ETC/.env)"
else
  chmod 600 "$ETC/.env"
  warn "$ETC/.env уже существует — сохранён"
  # Секреты могли не записаться (ручное создание/старая версия): дописываем
  # отсутствующие, значения в вывод не попадают.
  ENV_SECRETS_UPDATED=0
  for key in PANEL_PASS MIHOMO_SECRET; do
    cur=$(sed -n "s/^${key}=//p" "$ETC/.env" | head -n1)
    if [ -n "$cur" ]; then
      continue
    fi
    if [ "$key" = "PANEL_PASS" ]; then
      val="${PANEL_PASS:-}"
      [ -n "$val" ] || val=$(gen_hex 12)
    else
      val="${MIHOMO_SECRET:-}"
      [ -n "$val" ] || val=$(gen_hex 16)
    fi
    printf -v "$key" '%s' "$val"
    if grep -q "^${key}=" "$ETC/.env"; then
      sed -i "s|^${key}=.*|${key}=${val}|" "$ETC/.env"
    else
      if [ -n "$(tail -c1 "$ETC/.env")" ]; then printf '\n' >> "$ETC/.env"; fi
      printf '%s=%s\n' "$key" "$val" >> "$ETC/.env"
    fi
    ENV_SECRETS_UPDATED=1
  done
  if [ "$ENV_SECRETS_UPDATED" = 1 ]; then
    ok "в .env добавлены отсутствующие секреты (PANEL_PASS/MIHOMO_SECRET)"
  fi
fi

# fw-скрипты и конфиг zapret
install -m 0755 "$APP_DIR/templates/fw-base.sh" "$ETC/fw-base.sh"
install -m 0755 "$APP_DIR/templates/fw-nfq.sh"  "$ETC/fw-nfq.sh"
sed "s/{{WAN_IFACE}}/$LAN_IFACE/" "$APP_DIR/templates/zapret.conf.tpl" > "$ETC/zapret.conf"
chmod 600 "$ETC/zapret.conf"

# render config.yaml
set -a; . "$ETC/.env"; set +a
python3 "$APP_DIR/templates/render_mihomo.py" "$ETC/.env" "$ETC/config.yaml"
chmod 600 "$ETC/config.yaml"
ok "config.yaml сгенерирован"

# ---------- 6. zapret (сборка из исходников) ----------
if [ -x "$ZAPRET_DIR/bin/nfqws" ]; then
  ok "zapret уже собран: $ZAPRET_DIR/bin/nfqws"
else
  info "Сборка zapret (2-5 минут)..."
  [ -d "$ZAPRET_DIR" ] || git clone --depth 1 https://github.com/bol-van/zapret "$ZAPRET_DIR" \
    || die "Не удалось склонировать zapret"
  for d in tpws nfq ip2net mdig; do
    make -C "$ZAPRET_DIR/$d" >/dev/null || die "Сборка zapret/$d не удалась (см. вывод выше)"
  done
  mkdir -p "$ZAPRET_DIR/bin"
  cp -f "$ZAPRET_DIR/tpws/tpws" "$ZAPRET_DIR/nfq/nfqws" "$ZAPRET_DIR/ip2net/ip2net" "$ZAPRET_DIR/mdig/mdig" "$ZAPRET_DIR/bin/"
  [ -x "$ZAPRET_DIR/bin/nfqws" ] || die "nfqws не собрался"
  ok "zapret собран: $ZAPRET_DIR/bin/nfqws"
fi

# ---------- 7. systemd-юниты ----------
info "Установка systemd-юнитов..."
install -m 0644 "$APP_DIR/templates/mihomo.service"            /etc/systemd/system/mihomo.service
install -m 0644 "$APP_DIR/templates/homegw-firewall.service"   /etc/systemd/system/homegw-firewall.service
install -m 0644 "$APP_DIR/templates/zapret-nfqws.service"      /etc/systemd/system/zapret-nfqws.service
install -m 0644 "$APP_DIR/templates/homegw-panel.service"      /etc/systemd/system/homegw-panel.service
install -m 0755 "$APP_DIR/templates/homegw-update-lists.sh"     /usr/local/sbin/homegw-update-lists.sh
install -m 0644 "$APP_DIR/templates/homegw-lists-update.service" /etc/systemd/system/homegw-lists-update.service
install -m 0644 "$APP_DIR/templates/homegw-lists-update.timer"   /etc/systemd/system/homegw-lists-update.timer

# ---------- 8. Панель (venv + статика zashboard) ----------
info "Установка веб-панели..."
mkdir -p "$OPT_PANEL/static"
cp "$APP_DIR/panel/app.py" "$OPT_PANEL/app.py"
cp -r "$APP_DIR/panel/static/." "$OPT_PANEL/static/"
python3 -m venv "$OPT_PANEL/venv"
"$OPT_PANEL/venv/bin/pip" install -q --upgrade pip
"$OPT_PANEL/venv/bin/pip" install -q -r "$APP_DIR/panel/requirements.txt"

info "Загрузка zashboard..."
ZASH_URL="https://github.com/Zephyruso/zashboard/releases/latest/download/dist.zip"
if curl -fsSL --max-time 120 -o /tmp/zash.zip "$ZASH_URL"; then
  rm -rf "$ETC/zashboard"; mkdir -p "$ETC/zashboard"
  unzip -qo /tmp/zash.zip -d /tmp/zash
  # в архиве папка dist/
  cp -r /tmp/zash/dist/. "$ETC/zashboard/" 2>/dev/null || cp -r /tmp/zash/. "$ETC/zashboard/"
  rm -rf /tmp/zash /tmp/zash.zip
  ok "zashboard: http://$SERVER_IP:9090/ui/"
else
  warn "zashboard не скачался — дашборд будет недоступен (панель продолжит работать)"
fi

# ---------- 9. tg-ws-proxy-rs (docker) ----------
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

# ---------- 10. Firewall / ufw ----------
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  info "ufw активен — добавляю разрешения для локалки..."
  ufw route allow in on "$LAN_IFACE" >/dev/null 2>&1 || true
  UFW_PORTS="53 8080 9090"
  if [ "$INSTALL_TGWS" = "yes" ]; then UFW_PORTS="$UFW_PORTS 1443"; fi
  for p in $UFW_PORTS; do ufw allow from "$WG_SUBNET" to any port "$p" >/dev/null 2>&1 || true; done
  LAN_CIDR=$(ip -4 addr show "$LAN_IFACE" | awk '/inet /{print $2; exit}')
  if [ -n "$LAN_CIDR" ]; then
    for p in $UFW_PORTS; do ufw allow from "$LAN_CIDR" to any port "$p" >/dev/null 2>&1 || true; done
  fi
  ok "ufw: правила добавлены"
fi

# ---------- 11. Запуск ----------
info "Запуск сервисов..."
systemctl daemon-reload
systemctl enable --now homegw-firewall mihomo zapret-nfqws homegw-panel >/dev/null 2>&1 || true
systemctl enable --now homegw-lists-update.timer >/dev/null 2>&1 \
  || warn "не удалось включить homegw-lists-update.timer — проверьте: systemctl status homegw-lists-update.timer"
sleep 3

FAIL=0
for u in homegw-firewall mihomo zapret-nfqws homegw-panel homegw-lists-update.timer; do
  st=$(systemctl is-active "$u" || true)
  if [ "$st" = "active" ]; then ok "$u: active"; else warn "$u: $st (journalctl -u $u -n 30)"; FAIL=1; fi
done
[ "$FAIL" = 0 ] || warn "Есть незапущенные сервисы — установка завершена, но проверьте их!"

info "Первый прогон обновления списков..."
systemctl start homegw-lists-update.service \
  || warn "обновление списков завершилось ошибкой — journalctl -u homegw-lists-update -n 50"

# ---------- 12. WDTT-Plus (VPN-сервер для Android) ----------
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

echo
echo -e "${C_G}================ Установка завершена ================${C_N}"
echo -e "Панель:        ${C_B}http://$SERVER_IP:$PANEL_PORT${C_N}  (логин: $PANEL_USER, пароль: в $ETC/.env → PANEL_PASS)"
echo -e "Zashboard:     ${C_B}http://$SERVER_IP:9090/ui/${C_N}  (backend: http://$SERVER_IP:9090, секрет: $ETC/.env → MIHOMO_SECRET)"
echo -e "Конфиг:        $ETC/.env (chmod 600)"
if [ "$INSTALL_TGWS" = "yes" ]; then
  echo -e "TG MTProto:    секрет — ${C_B}docker logs homegw-tgwsproxy${C_N}"
fi
if [ "$WDTT_OK" = 1 ]; then
  echo -e "WDTT-Plus:     ${C_G}сервер установлен${C_N} (приложение WDTT Plus для Android)"
  echo -e "  DTLS:        ${C_B}$SERVER_IP:$WDTT_DTLS_PORT/udp${C_N} (пробросьте UDP на роутере/в панели провайдера)"
  echo -e "  Конфиг:      ${WDTT_INITIAL_CONFIG:-/root/wdtt-initial.json} (chmod 600, главный пароль внутри)"
  echo -e "  Управление:  ${C_B}$WDTT_DIR/install.sh status|doctor|monitor${C_N}"
fi

TAIL=""
STEP=4
if [ "$INSTALL_TGWS" = "yes" ]; then
  STEP=$((STEP + 1))
  TAIL="${TAIL}  $STEP. Telegram: пропишите MTProto-прокси для трёх сетей:
     дом   -> $SERVER_IP:1443
     WDTT  -> 10.66.66.1:1443
     TS    -> <tailscale-имя сервера>:1443
     секрет прокси: docker logs homegw-tgwsproxy
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
cat <<EOF

Дальнейшие шаги (README.md, раздел «После установки»):
  1. На роутере в DHCP выдайте клиентам шлюз и DNS = $SERVER_IP
     (IPv6 в локалке лучше отключить — иначе клиенты пойдут мимо шлюза)
  2. Подбор стратегии zapret: sudo /opt/zapret/blockcheck.sh
     -> результат в $ETC/zapret.conf -> systemctl restart zapret-nfqws
  3. Автообновление списков: homegw-lists-update.timer — ежедневно в 04:15
     + случайная задержка; журнал: journalctl -u homegw-lists-update
  4. Списки доменов и custom-списки правьте в панели (порт $PANEL_PORT);
     файлы — в $ETC/lists/
$TAIL======================================================
EOF

exit "$FAIL"
