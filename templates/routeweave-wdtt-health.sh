#!/usr/bin/env bash
# Routeweave: лёгкий монитор WDTT.
#
# Раз в минуту пишет одну строку в /var/log/routeweave/wdtt-health.log:
# счётчики wdtt0, дельты за минуту, число событий клиентов WDTT в логе
# tg-ws-proxy и состояние сервисов. Без journalctl и чтения больших файлов:
# только /proc/net/dev, /sys/class/net, `docker logs --since 2m --tail 5000`
# и `systemctl is-active`. При превышении MAX_BYTES файл укорачивается.
set -Eeuo pipefail

ETC="${ROUTEWEAVE_ETC:-/etc/routeweave}"
IFACE="${WDTT_IFACE:-wdtt0}"
NET_DEV="${WDTT_NET_DEV:-/proc/net/dev}"
LOG="${WDTT_HEALTH_LOG:-/var/log/routeweave/wdtt-health.log}"
CONTAINER="${TGWS_CONTAINER:-routeweave-tgwsproxy}"
MAX_BYTES="${WDTT_HEALTH_MAX_BYTES:-4194304}"   # 4 MiB
KEEP_LINES="${WDTT_HEALTH_KEEP_LINES:-12000}"   # ~8 суток по минуте

env_get() { # env_get <KEY> [default] — построчно, без source
  local key=${1-} def=${2-} line val
  line=$(grep -m1 -E "^${key}=" "$ETC/.env" 2>/dev/null || true)
  [ -n "$line" ] || { printf '%s' "$def"; return 0; }
  val=${line#*=}
  case "$val" in
    \'*\') val=${val:1:${#val}-2} ;;
    \"*\") val=${val:1:${#val}-2} ;;
  esac
  printf '%s' "$val"
}

cidr=$(env_get WG_SUBNET "10.66.66.0/24")
cidr=${cidr:-10.66.66.0/24}
ip_part=${cidr%/*}
plen=${cidr#*/}
[[ $plen =~ ^[0-9]+$ ]] || plen=24
IFS=. read -r -a octets <<<"$ip_part"
o0=${octets[0]:-10}; o1=${octets[1]:-66}; o2=${octets[2]:-66}; o3=${octets[3]:-0}
sess_pat="\["
peer_prefix=""
if (( plen >= 32 )); then
  sess_pat="\[$o0\.$o1\.$o2\.$o3:"
  peer_prefix="$o0.$o1.$o2.$o3"
elif (( plen >= 24 )); then
  sess_pat="\[$o0\.$o1\.$o2\."
  peer_prefix="$o0.$o1.$o2."
elif (( plen >= 16 )); then
  sess_pat="\[$o0\.$o1\."
  peer_prefix="$o0.$o1."
elif (( plen >= 8 )); then
  sess_pat="\[$o0\."
  peer_prefix="$o0."
fi

rx=0; tx=0; rxp=0; txp=0
if [ -r "$NET_DEV" ]; then
  read -r rx tx rxp txp < <(
    awk -v ifc="$IFACE:" '
      $1 == ifc { print $2, $10, $3, $11; found=1 }
      END { if (!found) print "0 0 0 0" }
    ' "$NET_DEV"
  )
fi
state=absent
if [ -r "/sys/class/net/$IFACE/operstate" ]; then
  state=$(cat "/sys/class/net/$IFACE/operstate" 2>/dev/null || printf 'unknown')
fi

prev_rx=""; prev_tx=""
if [ -s "$LOG" ]; then
  last=$(tail -n 1 "$LOG" 2>/dev/null || true)
  prev_rx=$(printf '%s\n' "$last" | sed -n 's/.* rx_b=\([0-9][0-9]*\).*/\1/p')
  prev_tx=$(printf '%s\n' "$last" | sed -n 's/.* tx_b=\([0-9][0-9]*\).*/\1/p')
fi
rx_d=0; tx_d=0; reset=0
if [ -n "$prev_rx" ]; then
  if [ "$rx" -ge "$prev_rx" ]; then rx_d=$((rx - prev_rx)); else reset=1; fi
fi
if [ -n "$prev_tx" ]; then
  if [ "$tx" -ge "$prev_tx" ]; then tx_d=$((tx - prev_tx)); else reset=1; fi
fi

tgws=down; sess=0; peers=0; err_2m=0
if command -v docker >/dev/null 2>&1; then
  running=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)
  if [ "$running" = "true" ]; then
    tgws=up
    logs=$(docker logs --since 2m --tail 5000 "$CONTAINER" 2>&1 || true)
    if [ -n "$logs" ]; then
      sess=$(printf '%s\n' "$logs" | grep -cE "$sess_pat" || true)
      if [ -n "$peer_prefix" ]; then
        peers=$(printf '%s\n' "$logs" \
          | grep -oE '\[[0-9]{1,3}(\.[0-9]{1,3}){3}:' \
          | sed -e 's/^\[//' -e 's/:$//' \
          | awk -v p="$peer_prefix" 'index($0, p) == 1' \
          | sort -u | wc -l)
      fi
      err_2m=$(printf '%s\n' "$logs" | grep -cE 'CF Worker timed out|ERROR' || true)
    fi
  fi
fi
peers=${peers//[[:space:]]/}

wdtt=unknown
if command -v systemctl >/dev/null 2>&1; then
  wdtt=$(systemctl is-active wdtt 2>/dev/null || true)
  [ -n "$wdtt" ] || wdtt=unknown
fi

log_dir=$(dirname "$LOG")
[ -d "$log_dir" ] || mkdir -p "$log_dir" 2>/dev/null || true
if [ ! -e "$LOG" ]; then
  ( umask 027; : >> "$LOG" ) 2>/dev/null || true
  chmod 0640 "$LOG" 2>/dev/null || true
  chown root:routeweave "$LOG" 2>/dev/null || true
fi
if [ -f "$LOG" ]; then
  size=$(stat -c %s "$LOG" 2>/dev/null || printf '0')
  if [ "$size" -gt "$MAX_BYTES" ]; then
    tmp="$LOG.tmp.$$"
    if tail -n "$KEEP_LINES" "$LOG" > "$tmp" 2>/dev/null; then
      chmod 0640 "$tmp" 2>/dev/null || true
      chown root:routeweave "$tmp" 2>/dev/null || true
      mv -f "$tmp" "$LOG" 2>/dev/null || rm -f "$tmp" || true
    else
      rm -f "$tmp" 2>/dev/null || true
    fi
  fi
fi

printf '%s iface=%s state=%s rx_b=%s tx_b=%s rx_p=%s tx_p=%s rx_d=%s tx_d=%s reset=%s wdtt=%s tgws=%s sess_2m=%s peers_2m=%s err_2m=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$IFACE" "$state" "$rx" "$tx" "$rxp" "$txp" \
  "$rx_d" "$tx_d" "$reset" "$wdtt" "$tgws" "$sess" "$peers" "$err_2m" >> "$LOG"
