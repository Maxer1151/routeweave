#!/usr/bin/env bash
# Routeweave: состояние Tailscale для веб-панели.
#
# Скрипт только читает: `tailscale status --json`, `systemctl is-active
# tailscaled`, `tailscale debug prefs` (только AdvertiseRoutes, без сохранения
# и печати вывода) и счётчики байт из sysfs. Ничего не меняет и не печатает
# пиров, ключи и имена устройств — только агрегаты. Печатает ровно одну строку
# компактного JSON и возвращает 0, если состояние удалось напечатать.
#
# Запускается от root через sudo из allowlist панели: только так доступен
# сокет tailscaled (панель работает от непривилегированного routeweave).
#
# Переменные ROUTEWEAVE_TS_ENV и ROUTEWEAVE_TS_SYSFS переопределяют пути
# только для тестов/чтения; установщик запускает helper без окружения.
set -Eeuo pipefail

ENV_FILE="${ROUTEWEAVE_TS_ENV:-/etc/routeweave/.env}"
SYSFS_DIR="${ROUTEWEAVE_TS_SYSFS:-/sys/class/net}"
IFACE_RE='^[A-Za-z0-9_.:-]{1,15}$'

# .env читаем построчно через sed, без source: значения не исполняются.
ts_iface=""
if [ -r "$ENV_FILE" ]; then
  ts_iface=$(sed -n 's/^[[:space:]]*TS_IFACE=//p' "$ENV_FILE" 2>/dev/null | head -n1) || ts_iface=""
  ts_iface=${ts_iface%$'\r'}
  ts_iface=${ts_iface%\"}
  ts_iface=${ts_iface#\"}
  ts_iface=${ts_iface%\'}
  ts_iface=${ts_iface#\'}
fi
# Невалидное имя интерфейса (или пустое) — дефолт: не даём пути в sysfs
# выйти за пределы каталога и не ломаем чтение счётчиков.
if ! [[ $ts_iface =~ $IFACE_RE ]]; then
  ts_iface="tailscale0"
fi

installed=0
if command -v tailscale >/dev/null 2>&1; then
  installed=1
fi

daemon="unknown"
if [ "$installed" -eq 0 ]; then
  # Без пакета Tailscale демона нет по определению.
  daemon="absent"
elif command -v systemctl >/dev/null 2>&1; then
  # timeout: зависший systemctl не должен вешать панель (sudo-вызов имеет
  # собственный таймаут, но helper обязан уложиться сам). В тестовом PATH
  # утилиты timeout может не быть — тогда вызываем напрямую.
  if command -v timeout >/dev/null 2>&1; then
    daemon=$(timeout 5 systemctl is-active tailscaled 2>/dev/null) || true
  else
    daemon=$(systemctl is-active tailscaled 2>/dev/null) || true
  fi
  case "$daemon" in
    active | inactive | failed | activating) ;;
    *) daemon="unknown" ;;
  esac
fi

# raw — ответ tailscale; error — короткая фиксированная строка по-русски.
# stderr и текст ответа наружу не попадают. timeout 5: повисший tailscaled
# (или ожидание сокета) не должен задерживать /api/status.
raw=""
error=""
if [ "$installed" -eq 1 ]; then
  status_rc=0
  if command -v timeout >/dev/null 2>&1; then
    raw=$(timeout 5 tailscale status --json 2>/dev/null) || status_rc=$?
  else
    raw=$(tailscale status --json 2>/dev/null) || status_rc=$?
  fi
  if [ "$status_rc" -eq 124 ]; then
    raw=""
    error="tailscale status не ответил за 5 с"
  elif [ "$status_rc" -ne 0 ]; then
    raw=""
    error="tailscale status недоступен"
  elif [ -z "$raw" ]; then
    error="tailscale status вернул пустой ответ"
  fi
fi

read_counter() { # read_counter <файл> — счётчик байт или 0
  local value=""
  if [ -r "$1" ]; then
    value=$(head -n1 "$1" 2>/dev/null) || value=""
  fi
  case "$value" in
    "" | *[!0-9]*) value=0 ;;
  esac
  printf '%s' "$value"
}

rx_bytes=$(read_counter "$SYSFS_DIR/$ts_iface/statistics/rx_bytes")
tx_bytes=$(read_counter "$SYSFS_DIR/$ts_iface/statistics/tx_bytes")

# print_fallback <error> — валидный JSON без python3. Поля по умолчанию,
# installed/daemon/iface — по факту.
print_fallback() { # print_fallback <error>
  local installed_json="false"
  if [ "$installed" -eq 1 ]; then
    installed_json="true"
  fi
  printf '{"installed":%s,"daemon":"%s","backend":"","self_ip":"","advertised":false,"approved":false,"peers":0,"iface":"%s","rx_bytes":0,"tx_bytes":0,"error":"%s"}\n' \
    "$installed_json" "$daemon" "$ts_iface" "$1"
  exit 0
}

if ! command -v python3 >/dev/null 2>&1; then
  print_fallback "python3 недоступен"
fi

# Разбор JSON — через python3, jq не нужен. Компактный вывод: одна строка.
json=$(printf '%s' "$raw" | python3 -c '
import json
import subprocess
import sys

installed = sys.argv[1] == "1"
daemon = sys.argv[2]
iface = sys.argv[3]
error = sys.argv[6]


def counter(text):
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


rx_bytes = counter(sys.argv[4])
tx_bytes = counter(sys.argv[5])

raw = sys.stdin.read()
data = None
if installed:
    if raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
        elif not error:
            error = "не удалось разобрать tailscale status --json"
    elif not error:
        error = "пустой ответ tailscale status"

backend = ""
self_ip = ""
advertised = False
approved = False
peers = 0
if data is not None:
    backend = data.get("BackendState") or ""
    if not isinstance(backend, str):
        backend = str(backend)
    self_ = data.get("Self")
    if not isinstance(self_, dict):
        self_ = {}
    ips = self_.get("TailscaleIPs")
    if isinstance(ips, list):
        self_ip = next((ip for ip in ips if isinstance(ip, str) and ":" not in ip), "")
    # ExitNodeOption — авторитетный признак: tailscale выставляет его, когда
    # узел анонсировал exit node и одобрен (offered && approved). Поля Prefs
    # в tailscale status --json нет, а PrimaryRoutes — про subnet-маршруты.
    approved = bool(self_.get("ExitNodeOption"))
    advertised = approved
    # Если одобрения нет, отличаем "анонсирован, но ждёт одобрения" от
    # "не анонсирован" по локальным prefs. Полный вывод содержит
    # чувствительные поля, поэтому читаем его только в памяти и не логируем.
    if not advertised:
        try:
            proc = subprocess.run(
                ["tailscale", "debug", "prefs"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            try:
                prefs = json.loads(proc.stdout)
            except ValueError:
                prefs = None
            if isinstance(prefs, dict):
                routes = prefs.get("AdvertiseRoutes")
                advertised = isinstance(routes, list) and "0.0.0.0/0" in routes
    # В tailscale status --json 1.104.x пиры лежат в объекте Peer (ключ —
    # публичный ключ узла), поля Peers нет. Ветка Peers оставлена только как
    # совместимость с нестандартным ответом.
    peers_raw = data.get("Peer")
    if not isinstance(peers_raw, (list, dict)):
        peers_raw = data.get("Peers")
    if isinstance(peers_raw, (list, dict)):
        peers = len(peers_raw)

print(json.dumps({
    "installed": installed,
    "daemon": daemon,
    "backend": backend,
    "self_ip": self_ip,
    "advertised": advertised,
    "approved": approved,
    "peers": peers,
    "iface": iface,
    "rx_bytes": rx_bytes,
    "tx_bytes": tx_bytes,
    "error": error,
}, ensure_ascii=False, separators=(",", ":")))
' "$installed" "$daemon" "$ts_iface" "$rx_bytes" "$tx_bytes" "$error" 2>/dev/null) || json=""

if [ -z "$json" ]; then
  print_fallback "не удалось получить состояние tailscale"
fi

printf '%s\n' "$json"
