#!/usr/bin/env bash
# Routeweave: ежедневное обновление geo/ru-direct для mihomo. DPI-домены исключаются из geo.
set -euo pipefail
L=/etc/routeweave/lists; E=/etc/routeweave/.env
# .env читается по фиксированному пути /etc/routeweave/.env; статически
# разобрать source через переменную анализатор не может.
# shellcheck disable=SC1090
[ -f "$E" ] && { set -a; . "$E"; set +a; }
log(){ echo "[$(date '+%F %T')] $*"; }
P=""; case "${SOCKS_URL:-}" in socks5://*) P="socks5h://${SOCKS_URL#socks5://}";; socks5h://*) P="$SOCKS_URL";; http://*|https://*) P="$SOCKS_URL";; esac
fetch(){ if curl -fsSL --connect-timeout 10 --max-time 90 "$1" -o "$2" && [ -s "$2" ]; then return 0; fi
  [ -n "$P" ] && curl -fsSL --connect-timeout 15 --max-time 120 --proxy "$P" "$1" -o "$2" && [ -s "$2" ]; }
norm(){ tr -d '\r' | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//;s/^\*\.//;s/^\+\.//;s/^\.//' | grep -E '^[A-Za-z0-9_.-]+\.[A-Za-z]{2,}$' | LC_ALL=C sort -u || true; }
put(){ local n=$1 d=$2 l=$3; if [ -f "$d" ] && cmp -s "$n" "$d"; then log "SAME $l ($(wc -l <"$n"))"; return 1; fi
  local t="$L/.$l.tmp.$$"; cp "$n" "$t"; chmod 644 "$t"; chown root:root "$t"; mv -f "$t" "$d"; log "UPD $l ($(wc -l <"$n") строк)"; return 0; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
if [ ! -f "$L/geo-custom.txt" ]; then
  if [ -f "$L/geo.txt" ]; then cp "$L/geo.txt" "$L/geo-custom.txt"; else : >"$L/geo-custom.txt"; fi
fi
if [ ! -f "$L/ru-direct-custom.txt" ]; then
  if [ -f "$L/ru-direct.txt" ]; then cp "$L/ru-direct.txt" "$L/ru-direct-custom.txt"; else : >"$L/ru-direct-custom.txt"; fi
fi
R=0
if fetch https://raw.githubusercontent.com/lib4u/amnezia-tunneling-ru/main/amnezia.json "$T/a.json" && jq -e . "$T/a.json" >/dev/null 2>&1; then
  jq -r '.[].hostname // empty' "$T/a.json" | norm > "$T/ru"; n=$(wc -l <"$T/ru")
  if [ "$n" -ge 1000 ]; then norm <"$L/ru-direct-custom.txt" > "$T/ruc" || true
    cat "$T/ru" "$T/ruc" | LC_ALL=C sort -u | sed 's/^/+./' > "$T/ruf"
    if put "$T/ruf" "$L/ru-direct.txt" ru-direct; then R=1; fi
  else log "SKIP ru-direct: $n < 1000"; fi
else log "SKIP ru-direct: fetch/json"; fi
G=0
if fetch https://raw.githubusercontent.com/Internet-Helper/Unblock-for-Russia/main/geoblock.lst "$T/g.lst"; then
  norm <"$T/g.lst" > "$T/g"; n=$(wc -l <"$T/g")
  if [ "$n" -ge 50 ]; then norm <"$L/dpi.txt" > "$T/dpi" || true; norm <"$L/geo-custom.txt" > "$T/gc" || true
    cat "$T/g" "$T/gc" | LC_ALL=C sort -u | awk -v f="$T/dpi" 'BEGIN{while((getline d < f)>0)D[d]=1}{g=$0;s=0;for(x in D)if(g==x||(length(g)>length(x)&&index(g,"."x)==length(g)-length(x))){s=1;break}if(!s)print g}' | sed 's/^/+./' > "$T/gf"
    if put "$T/gf" "$L/geo.txt" geo; then G=1; fi
  else log "SKIP geo: $n < 50"; fi
else log "SKIP geo: fetch"; fi
if [ "$R$G" = "00" ]; then log "Изменений нет"; exit 0; fi
S=$(sed -n 's/^MIHOMO_SECRET=//p' "$E" | tail -1); rc=0
for p in geo-domains ru-domains; do c=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $S" "http://127.0.0.1:9090/providers/rules/$p" || true); log "reload $p -> ${c:-ERR}"; [ "$c" = 204 ] || [ "$c" = 200 ] || rc=1; done
[ "$rc" = 0 ] || { log "API reload failed -> restart mihomo"; systemctl restart mihomo || true; }
log "Готово"
