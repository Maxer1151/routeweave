#!/usr/bin/env bash
# Routeweave — удаление шлюза (конфиги и списки в /etc/routeweave сохраняются).
#
# Принцип: откат должен дойти до конца, даже если отдельный шаг не сработал
# (сервиса нет, ufw выключен, docker не установлен, бэкапа DNS нет). Поэтому
# здесь НЕТ `set -e` — только `set -uo pipefail`, а каждая критичная операция
# обёрнута в `|| warn`. В конце всегда `exit 0`: повторный запуск безопасен и
# не должен пугать пользователя «упавшим» удалением.
set -uo pipefail

ETC=/etc/routeweave
BACKUP_DIR="$ETC/backup"
OPT_PANEL=/opt/routeweave-panel
ZAPRET_DIR=/opt/zapret
MIHOMO_BIN=/usr/local/bin/mihomo
SUDOERS_PANEL=/etc/sudoers.d/routeweave-panel
RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/routeweave.conf
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER=routeweave
SERVICE_GROUP=routeweave

# Ровно известный список юнитов: чужие *.service не трогаем, даже если они
# лежат рядом в /etc/systemd/system.
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
  /usr/local/sbin/routeweave-update-lists.sh
)

C_G='\033[0;32m'; C_Y='\033[1;33m'; C_B='\033[1;34m'; C_N='\033[0m'
info() { echo -e "${C_B}[i]${C_N} $*"; }
ok()   { echo -e "${C_G}[+]${C_N} $*"; }
warn() { echo -e "${C_Y}[!]${C_N} $*"; }

usage() {
  cat <<'EOF'
Routeweave — удаление шлюза.

Использование:
  sudo ./uninstall.sh
  ./uninstall.sh --help

Что делает:
  - останавливает и отключает systemd-юниты Routeweave;
  - снимает правила firewall (fw-nfq.sh stop, fw-base.sh stop);
  - удаляет юниты, helper-скрипты, sudoers панели, лаунчер nfqws, панель,
    zapret и mihomo;
  - останавливает и удаляет docker-контейнер routeweave-tgwsproxy;
  - возвращает DNS (/etc/resolv.conf и drop-in systemd-resolved) из бэкапа;
  - удаляет пользователя и группу routeweave;
  - убирает добавленные установщиком правила ufw (без ufw --force reset!).

Что НЕ делает:
  - не удаляет конфиги и списки в /etc/routeweave (удалить вручную: rm -rf
    /etc/routeweave);
  - не трогает WDTT-Plus (/etc/wdtt, systemd-юнит wdtt);
  - не сбрасывает ufw целиком и не удаляет чужие правила.
EOF
}

case "${1:-}" in
  -h | --help) usage; exit 0 ;;
  "") ;;
  *)
    echo "Неизвестный аргумент: $1 (см. --help)" >&2
    exit 1
    ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root (sudo ./uninstall.sh)" >&2
  exit 1
fi

env_get() { # env_get <ключ> [файл] -> значение из .env без кавычек
  local key="$1" file="${2:-$ETC/.env}" val=""
  [ -r "$file" ] || return 0
  val=$(sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -n1)
  val=${val%\"}
  val=${val#\"}
  printf '%s' "$val"
}

# ---------------------------------------------------------------- 1. сервисы
info "Остановка и отключение сервисов..."
for unit in "${UNITS[@]}"; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
done
# Сервисы без юнитов (на случай, если юнит уже удалён, а процесс жив) —
# просто глушим, ошибки не важны.
for svc in mihomo zapret-nfqws routeweave-panel; do
  systemctl stop "$svc" >/dev/null 2>&1 || true
done
ok "systemd: юниты остановлены"

# ------------------------------------------------------------- 2. firewall
# До удаления файлов: скрипты идемпотентны (stop при отсутствии правила/цепи
# не падает), поэтому порядок «сначала снять правила, потом rm» безопасен.
info "Снятие правил firewall..."
if [ -x "$ETC/fw-nfq.sh" ]; then
  "$ETC/fw-nfq.sh" stop >/dev/null 2>&1 || warn "fw-nfq.sh stop завершился с ошибкой (проверьте iptables -t mangle -S)"
else
  warn "$ETC/fw-nfq.sh не найден — правило NFQUEUE снимите вручную (iptables -t mangle -S | grep NFQUEUE)"
fi
if [ -x "$ETC/fw-base.sh" ]; then
  "$ETC/fw-base.sh" stop >/dev/null 2>&1 || warn "fw-base.sh stop завершился с ошибкой (проверьте iptables -S ROUTEWEAVE-FWD)"
else
  warn "$ETC/fw-base.sh не найден — цепочку ROUTEWEAVE-FWD снимите вручную"
fi
ok "firewall: правила сняты"

# --------------------------------------------------------------- 3. юниты
info "Удаление systemd-юнитов..."
for unit in "${UNITS[@]}"; do
  rm -f "/etc/systemd/system/$unit" || warn "не удалось удалить /etc/systemd/system/$unit"
done
systemctl daemon-reload >/dev/null 2>&1 || warn "systemctl daemon-reload завершился с ошибкой"
systemctl reset-failed >/dev/null 2>&1 || true
ok "systemd: юниты удалены, конфигурация перечитана"

# --------------------------------------------------------------- 4. docker
info "Остановка tg-ws-proxy (docker)..."
if command -v docker >/dev/null 2>&1; then
  if [ -f "$SELF_DIR/docker-compose.yml" ]; then
    ( cd "$SELF_DIR" && docker compose down >/dev/null 2>&1 ) \
      || warn "docker compose down завершился с ошибкой (контейнер удалим принудительно)"
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
  tmp=""
  if tmp=$(mktemp /etc/.resolv.conf.restore.XXXXXX 2>/dev/null); then
    if cp -aL "$BACKUP_DIR/resolv.conf.orig" "$tmp" 2>/dev/null; then
      chmod 0644 "$tmp" >/dev/null 2>&1 || true
      chown root:root "$tmp" >/dev/null 2>&1 || true
      if mv -f "$tmp" /etc/resolv.conf 2>/dev/null; then
        ok "/etc/resolv.conf восстановлен из $BACKUP_DIR/resolv.conf.orig"
      else
        warn "не удалось заменить /etc/resolv.conf — верните DNS вручную"
        rm -f "$tmp" || true
      fi
    else
      warn "не удалось скопировать бэкап resolv.conf — верните DNS вручную"
      rm -f "$tmp" || true
    fi
  else
    warn "не удалось создать временный файл в /etc — верните /etc/resolv.conf вручную"
  fi
else
  warn "бэкапа $BACKUP_DIR/resolv.conf.orig нет — верните DNS вручную (например, nameserver 192.168.1.1 или 77.88.8.8)"
fi

if [ -e "$RESOLVED_DROPIN" ]; then
  if [ -e "$BACKUP_DIR/resolved-routeweave.conf.orig" ]; then
    if cp -a "$BACKUP_DIR/resolved-routeweave.conf.orig" "$RESOLVED_DROPIN" 2>/dev/null; then
      ok "drop-in $RESOLVED_DROPIN восстановлен из бэкапа"
    else
      warn "не удалось восстановить $RESOLVED_DROPIN — проверьте вручную"
    fi
  else
    if rm -f "$RESOLVED_DROPIN"; then
      ok "drop-in $RESOLVED_DROPIN удалён"
    else
      warn "не удалось удалить $RESOLVED_DROPIN"
    fi
  fi
else
  info "drop-in $RESOLVED_DROPIN отсутствует — шаг пропущен"
fi

if systemctl cat systemd-resolved.service >/dev/null 2>&1; then
  systemctl restart systemd-resolved >/dev/null 2>&1 \
    || warn "не удалось перезапустить systemd-resolved (systemctl restart systemd-resolved)"
  systemctl daemon-reload >/dev/null 2>&1 || true
else
  info "юнита systemd-resolved нет — рестарт пропущен"
fi

# ------------------------------------------- 6. sudoers и helper-скрипты
info "Удаление sudoers и helper-скриптов..."
rm -f "$SUDOERS_PANEL" || warn "не удалось удалить $SUDOERS_PANEL"
for helper in "${HELPERS[@]}"; do
  rm -f "$helper" || warn "не удалось удалить $helper"
done
rm -f "$ETC/nfqws-launch.sh" || warn "не удалось удалить $ETC/nfqws-launch.sh"
rm -f "$ETC/.env.tgws" || warn "не удалось удалить $ETC/.env.tgws"
ok "sudoers и helper-скрипты удалены"

# ------------------------------------------------------- 7. пользователь
# Конфиги в /etc/routeweave сохраняются, но install.sh выставлял на них
# владельца routeweave:routeweave (lists) и root:routeweave (.env). После
# userdel/groupdel это дало бы висячие uid/gid, а .env остался бы 0640 с
# несуществующей группой — то есть секреты могли бы читать те, кому достанется
# этот gid. Поэтому сначала возвращаем root и ужесточаем .env, потом удаляем
# пользователя.
info "Удаление пользователя $SERVICE_USER..."
chown -R root:root "$ETC/lists" 2>/dev/null || true
chown root:root "$ETC/.env" 2>/dev/null || true
chmod 0600 "$ETC/.env" 2>/dev/null || true
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
    warn "не удалось удалить пользователя $SERVICE_USER (остались процессы или файлы вне /opt: userdel -f $SERVICE_USER)"
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

# ------------------------------------------------------------------ 8. ufw
# Только точечное удаление своих правил. ufw --force reset НЕ вызывается:
# он снёс бы все правила пользователя, включая чужие.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  info "ufw активен — убираю правила Routeweave..."
  for rule in 53/tcp 53/udp 7890/tcp 9090/tcp 1443/tcp 56000/udp 56001/udp; do
    ufw delete allow "$rule" >/dev/null 2>&1 || true
  done

  LAN_IFACE=$(env_get LAN_IFACE)
  WG_SUBNET=$(env_get WG_SUBNET)
  LAN_CIDR=$(env_get LAN_CIDR)
  PANEL_PORT=$(env_get PANEL_PORT)
  PANEL_PORT=${PANEL_PORT:-8080}

  if [ -n "$LAN_IFACE" ]; then
    ufw route delete allow in on "$LAN_IFACE" >/dev/null 2>&1 || true
  fi

  # Эти route-правила ставил install.sh при установке WDTT (INSTALL_WDTT=yes).
  # Сам WDTT-Plus не удаляем, поэтому правила снимаем только свои; `|| true`
  # безопасен, если WDTT не устанавливался и правил нет.
  ufw route delete allow in on wdtt0 >/dev/null 2>&1 || true
  ufw route delete allow out on wdtt0 >/dev/null 2>&1 || true

  # Подсеть LAN в .env не хранится (install.sh вычисляет её на месте) —
  # пробуем вывести из интерфейса.
  if [ -z "$LAN_CIDR" ] && [ -n "$LAN_IFACE" ] && command -v ip >/dev/null 2>&1; then
    LAN_CIDR=$(ip -4 addr show "$LAN_IFACE" 2>/dev/null | awk '/inet /{print $2; exit}')
  fi

  # install.sh выдавал доступ не «всем на порт», а с конкретных подсетей —
  # снимаем именно эти правила, плюс общие (на случай ручных правок).
  for src in "$WG_SUBNET" "$LAN_CIDR"; do
    [ -n "$src" ] || continue
    for p in 53 7890 9090 "$PANEL_PORT" 1443; do
      ufw delete allow from "$src" to any port "$p" proto tcp >/dev/null 2>&1 || true
    done
    ufw delete allow from "$src" to any port 53 proto udp >/dev/null 2>&1 || true
  done
  ok "ufw: правила Routeweave удалены (остальные правила не тронуты)"
else
  info "ufw не активен или не установлен — шаг пропущен"
fi

# ---------------------------------------------------------------- 9. файлы
info "Удаление файлов..."
rm -rf "$OPT_PANEL" || warn "не удалось удалить $OPT_PANEL"
rm -rf "$ZAPRET_DIR" || warn "не удалось удалить $ZAPRET_DIR"
rm -f "$MIHOMO_BIN" || warn "не удалось удалить $MIHOMO_BIN"
rm -rf /run/routeweave || warn "не удалось удалить /run/routeweave"
ok "панель, zapret, mihomo и рантайм-каталог удалены"

# ------------------------------------------------------------- 10. итоги
echo
echo -e "${C_G}[+] Routeweave удалён.${C_N}"
echo
echo "Удалено:"
echo "  - systemd-юниты: routeweave-firewall, mihomo, zapret-nfqws, routeweave-panel,"
echo "    routeweave-lists-update.service/.timer (+ правила firewall сняты);"
echo "  - контейнер routeweave-tgwsproxy (docker compose down + docker rm -f);"
echo "  - /etc/sudoers.d/routeweave-panel и helper-скрипты в /usr/local/sbin;"
echo "  - /etc/routeweave/nfqws-launch.sh, /etc/routeweave/.env.tgws;"
echo "  - $OPT_PANEL, $ZAPRET_DIR, $MIHOMO_BIN, /run/routeweave;"
echo "  - пользователь и группа $SERVICE_USER (если не были заняты);"
echo "  - добавленные установщиком правила ufw."
echo
echo "Осталось (удаляется вручную):"
echo "  - конфиги и списки: $ETC (полное удаление: rm -rf $ETC);"
echo "  - WDTT-Plus не затрагивался: systemctl status wdtt, данные в /etc/wdtt;"
echo "  - пакеты, поставленные установщиком (iptables, nftables, docker и т.д.),"
echo "    не удалялись — они могут быть нужны другим сервисам."
echo
echo -e "${C_Y}ВНИМАНИЕ:${C_N} клиенты, у которых шлюз или DNS = IP этого сервера,"
echo "потеряют интернет — верните им шлюз/DNS роутера (или отключите"
echo "DHCP-резервацию на этот хост)."

exit 0
