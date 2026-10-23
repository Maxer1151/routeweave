#!/usr/bin/env bash
# Routeweave — удаление (конфиги и списки в /etc/homegateway сохраняются)
set -uo pipefail

ETC=/etc/homegateway
OPT_PANEL=/opt/homegateway-panel
ZAPRET_DIR=/opt/zapret

[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }

echo "[i] Остановка сервисов..."
systemctl disable --now homegw-lists-update.timer homegw-lists-update.service \
  mihomo zapret-nfqws homegw-panel homegw-firewall 2>/dev/null || true

[ -x "$ETC/fw-nfq.sh" ] && "$ETC/fw-nfq.sh" stop 2>/dev/null
[ -x "$ETC/fw-base.sh" ] && "$ETC/fw-base.sh" stop 2>/dev/null
iptables -t mangle -S 2>/dev/null | grep -q NFQUEUE || true

echo "[i] Остановка docker-контейнеров..."
if [ -f "$(dirname "$(readlink -f "$0")")/docker-compose.yml" ]; then
  ( cd "$(dirname "$(readlink -f "$0")")" && docker compose down 2>/dev/null ) || true
fi
docker rm -f homegw-tgwsproxy 2>/dev/null || true

echo "[i] Удаление файлов..."
rm -f /etc/systemd/system/{mihomo,homegw-firewall,zapret-nfqws,homegw-panel}.service
rm -f /etc/systemd/system/homegw-lists-update.service /etc/systemd/system/homegw-lists-update.timer
rm -f /usr/local/sbin/homegw-update-lists.sh
systemctl daemon-reload
rm -rf "$OPT_PANEL" "$ZAPRET_DIR" /usr/local/bin/mihomo

echo
echo "[+] Готово. Конфиги и списки сохранены в $ETC"
echo "    Таймер homegw-lists-update.timer и апдейтер списков удалены,"
echo "    списки в $ETC/lists остались (для полного удаления: rm -rf $ETC)"
echo "    WDTT-Plus не затрагивается: systemctl status wdtt, данные в /etc/wdtt"
echo "    ВНИМАНИЕ: клиенты, у которых шлюз/DNS = IP этого сервера, потеряют интернет —"
echo "    верните им шлюз/DNS роутера (или отключите DHCP-резервацию)."
