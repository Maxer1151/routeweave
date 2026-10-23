# Конфиг nfqws (zapret). Правится вручную, затем:
#   sudo systemctl restart zapret-nfqws
# Подбор стратегии под вашего провайдера: /opt/zapret/blockcheck.sh
# (результаты переносите сюда в DESYNC/FOOLING/AUTOTTL_ARG)

# Номер NFQUEUE (должен совпадать в fw-nfq.sh)
QNUM=200

# WAN-интерфейс (uplink к роутеру/интернету)
WAN_IFACE={{WAN_IFACE}}

# Hostlist (домены из списка десинким)
# Файл генерируется из /etc/routeweave/lists/dpi.txt при старте сервиса:
# формат mihomo (+.domain) преобразуется в голые хосты для nfqws.
HOSTLIST=/run/routeweave/dpi-zapret.txt

# Стартовая стратегия: fake + multidisorder, TLS-режим с подменой SNI.
# Подбирается blockcheck.sh под вашего провайдера.
# ВАЖНО: --dpi-desync-fwmark=0x80000 обязателен при активном TUN mihomo:
# без метки фейковые пакеты nfqws уходят в TUN mihomo и съедаются,
# из-за чего youtube.com не пробивается.
# Маркированные пакеты должны уходить в main, а не в TUN.
DESYNC=fake,multidisorder
FOOLING=md5sig
AUTOTTL_ARG=
TTL_ARG=
EXTRA="--dpi-desync-repeats=11 --dpi-desync-split-pos=1,midsld --dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com --dpi-desync-fwmark=0x80000"
