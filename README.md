# Routeweave

Домашний шлюз с раздельной маршрутизацией: mihomo + zapret + списки доменов + веб-панель.

## Что это

Routeweave — прозрачный шлюз на Ubuntu/Debian, который ставится на отдельную машину за домашним роутером. Клиенты получают его как шлюз и DNS, а mihomo разводит трафик по доменам: часть доменов проходит через DPI-десинк zapret, гео-блокировки уходят через внешний SOCKS/HTTP-прокси, российские сервисы и всё остальное идут напрямую. Необязательные компоненты — Telegram-прокси (tg-ws-proxy) и WDTT-Plus для входа с телефона.

## Возможности

- Маршрутизация по доменам: отдельные списки для geo (SOCKS), dpi (zapret) и RU-направления.
- fake-ip DNS на порту 53 и правила mihomo на файловых rule-provider'ах.
- Firewall: `ip_forward`, FORWARD-правила для LAN и WDTT, опциональный drop QUIC.
- Автообновление списков по systemd-таймеру.
- Веб-панель: статусы сервисов, пробы путей, редактор списков, перезапуски.
- zashboard для соединений и трафика.
- Опциональные tg-ws-proxy и WDTT-Plus.

## Архитектура

```text
                 ┌────────── Routeweave: шлюз (Ubuntu/Debian, 1 NIC) ───────────┐
  LAN ──────────►│ firewall ─► mihomo: TUN, fake-ip DNS :53, правила            │──► DIRECT: RU / GEOIP,RU
  WDTT (опц.) ──►│ wdtt0 ────► та же политика, что у LAN                        │──► DIRECT + zapret: dpi.txt
                 │ zapret (nfqws) · панель :8080 · zashboard :9090/ui           │──► SOCKS/HTTP: geo.txt
                 │ tg-ws-proxy :1443 (опц.)                                     │──► WS/TLS: Telegram (опц.)
                 └──────────────────────────────────────────────────────────────┘
```

## Состав

- **mihomo** — прозрачный шлюз: TUN, auto-route/auto-redirect, fake-ip DNS на `:53`,
  правила на файловых rule-provider'ах, mixed-proxy `:7890`, external-controller
  `:9090` (секрет в `.env`), zashboard на `/ui/`.
- **zapret (nfqws)** — NFQUEUE на исходящих TCP 80/443, десинк только доменов
  из `dpi.txt`.
- **Списки доменов** — `/etc/homegateway/lists/`: geo, dpi, ru-direct и custom-файлы.
- **Панель (FastAPI)** — статусы сервисов, пробы путей, редактор списков,
  перезапуски; Basic-авторизация.
- **Firewall** — `fw-base.sh` / `fw-nfq.sh`, systemd-юниты, policy-routing для WDTT.

Опционально:

- **tg-ws-proxy-rs** — Telegram MTProto-прокси в Docker, порт `1443`.
- **WDTT-Plus** — сервер WDTT в systemd (`wdtt`), только amd64: DTLS
  `56000/udp`, внутренний WireGuard `56001/udp`, интерфейс `wdtt0`.

## Требования

- Ubuntu 20.04+ или Debian 12+, архитектура amd64/arm64, ≥1 ГБ RAM, ≥3 ГБ диска.
- Одна NIC за роутером, статичный или зарезервированный в DHCP LAN-IP.
- Доступ к github.com (mihomo, zashboard, zapret, WDTT-Plus).
- Для Telegram-прокси: заранее установленные Docker Engine и compose v2.
  Установщик Docker **не ставит**.
- Для WDTT-Plus: amd64 и внешний UDP-порт (по умолчанию `56000`) для проброса.

## Быстрый старт

```bash
git clone https://github.com/Maxer1151/routeweave.git
cd routeweave
sudo bash install.sh                  # интерактивно
sudo bash install.sh --defaults       # только ядро, без вопросов
sudo env INSTALL_TGWS=yes bash install.sh   # + Telegram-прокси (нужен Docker)
sudo env INSTALL_WDTT=yes bash install.sh   # + WDTT-Plus (amd64)
```

`--defaults` ставит только ядро; tgws и WDTT по умолчанию выключены. Установщик
не перезаписывает существующие `.env` и live-списки. Повторный запуск обновляет
mihomo до свежего релиза (если версия отличается), юниты и конфиги, но сохраняет
`.env` и списки.

## Параметры `.env`

Файл `/etc/homegateway/.env` создаётся с `chmod 600`. Секреты генерирует
установщик, в консоль они не печатаются.

| Ключ | Назначение |
|---|---|
| `LAN_IFACE` | LAN-интерфейс сервера (единственная NIC) |
| `SERVER_IP` | LAN-IP сервера, статичный или зарезервированный в DHCP |
| `SOCKS_URL` | Внешний SOCKS/HTTP-прокси для geo; пусто = гео напрямую |
| `BASE_DOMAIN` | Домен CF Worker для tg-ws-proxy |
| `TG_CF_WORKER_DOMAIN` | Домен CF Worker (обычно совпадает с `BASE_DOMAIN`) |
| `TG_LINK_IP` | IP/хост для tg://-ссылки; пусто = `SERVER_IP` |
| `WG_SUBNET` | Подсеть клиентов WDTT, по умолчанию `10.66.66.0/24` |
| `PANEL_PORT` | Порт веб-панели, по умолчанию `8080` |
| `PANEL_USER` | Логин панели, по умолчанию `admin` |
| `PANEL_PASS` | Пароль панели; генерируется установщиком |
| `MIHOMO_SECRET` | Секрет API mihomo; генерируется установщиком |
| `TG_SECRET` | Секрет MTProto-прокси; только при `INSTALL_TGWS=yes` |
| `QUIC_DROP` | Резать UDP/443: `yes`/`no` |
| `FAKEIP_EXTRA` | Доп. записи в fake-ip-filter через запятую |

## После установки

1. **DHCP.** В роутере выдайте клиентам шлюз и DNS = `SERVER_IP`.
   IPv6 в LAN отключите — иначе устройства уйдут мимо шлюза.
2. **Панель.** Откройте `http://SERVER_IP:PANEL_PORT`, логин `PANEL_USER`,
   пароль — `PANEL_PASS` из `/etc/homegateway/.env`.
3. **Zashboard.** Откройте `http://SERVER_IP:9090/ui/`, укажите backend
   `http://SERVER_IP:9090` и секрет `MIHOMO_SECRET` из `.env`.
4. **Списки.** Проверьте `/etc/homegateway/lists/` и при необходимости
   добавьте домены в `geo-custom.txt`, `dpi.txt` или `ru-direct-custom.txt`
   (см. раздел «Панель»).
5. **Стратегия zapret.** Запустите `sudo /opt/zapret/blockcheck.sh`, перенесите
   рабочую стратегию в `/etc/homegateway/zapret.conf` и выполните
   `sudo systemctl restart zapret-nfqws`.

## Автообновление списков

Апдейтер: `/usr/local/sbin/homegw-update-lists.sh`. Юниты:
`homegw-lists-update.service` и `homegw-lists-update.timer`.
Таймер: `OnCalendar=04:15`, `RandomizedDelaySec=300`, `Persistent=true`.

Что делает апдейтер:

- тянет Amnezia JSON (RU-домены) и `geoblock.lst` (гео-домены);
- нормализует записи, подмешивает custom-списки;
- исключает dpi-домены из geo;
- пишет `geo.txt` и `ru-direct.txt`;
- перезагружает mihomo через API (`PUT /providers/rules/...`);
- при ошибке API перезапускает mihomo.

Ручной прогон и лог:

```bash
sudo systemctl start homegw-lists-update.service
journalctl -u homegw-lists-update -n 50
```

Важно: `geo.txt` и `ru-direct.txt` — генерируемые файлы. Правьте
`geo-custom.txt`, `ru-direct-custom.txt` и `dpi.txt`.

## Панель

Панель закрыта Basic-авторизацией (`PANEL_USER` / `PANEL_PASS`). Вкладки:

- `geo-custom (→ SOCKS)` — пишет `geo-custom.txt`;
- `dpi (→ zapret)` — пишет `dpi.txt`;
- `ru-custom (→ direct)` — пишет `ru-direct-custom.txt`.

Кнопка «Сохранить» только пишет файл. Кнопка «Сохранить, обновить списки и
mihomo» запускает апдейтер (для `dpi` — перезапуск `zapret-nfqws`).
Подсказка в панели напоминает, что `geo.txt` и `ru-direct.txt` генерируются
таймером, поэтому править нужно custom-списки и `dpi.txt`. Zashboard открывается
по `http://SERVER_IP:9090/ui/`.

## DPI и fwmark

Критично: в `EXTRA` в `/etc/homegateway/zapret.conf` обязателен
`--dpi-desync-fwmark=0x80000`. Без метки фейковые пакеты nfqws уходят в TUN
mihomo и съедаются — YouTube не пробивается. Маркированные пакеты должны
уходить в `main`, а не в TUN.

На amd64 установщик качает `mihomo-linux-amd64-compatible` (без AVX), на
arm64 — `mihomo-linux-arm64`. Стратегия по умолчанию — `fake,multidisorder`
и `md5sig`; это стартовая стратегия, её подбирают через `blockcheck.sh`.

## Опционально: Telegram

Включается `INSTALL_TGWS=yes`. Нужны Docker Engine и compose v2, порт `1443`.
Секрет прокси для клиентов смотрите в `docker logs homegw-tgwsproxy`.
CF Worker описан в `docs/cf-worker-tg.md`; ключи — `BASE_DOMAIN`,
`TG_CF_WORKER_DOMAIN`, `TG_LINK_IP`. Без этого компонента ядро работает как
обычно.

## Опционально: WDTT-Plus и Tailscale

WDTT-Plus включается `INSTALL_WDTT=yes`, только amd64. DTLS `56000/udp`,
внутренний WireGuard `56001/udp`, начальный конфиг
`/root/wdtt-initial.json`. VK-хеш создаётся вручную. Управление:
`/opt/wdtt-installer/install.sh status|doctor|monitor`. Подсеть `WG_SUBNET`
получает ту же политику, что LAN.

Tailscale настраивается отдельно, Routeweave его не трогает. При проблемах
смотрите `tailscale status`.

## Порты

| Порт | Протокол | Назначение |
|---|---|---|
| 53 | TCP + UDP | DNS mihomo (fake-ip) |
| 8080 | TCP | Веб-панель |
| 9090 | TCP | mihomo API и zashboard |
| 7890 | TCP | mixed-proxy mihomo (`allow-lan`) |
| 1443 | TCP | tg-ws-proxy (опц.) |
| 56000 | UDP | WDTT-Plus DTLS (опц.) |

Порты `53`, `8080`, `9090`, `7890` и `1443` — только LAN/WG, наружу их
пробрасывать нельзя. `56000/udp` при использовании WDTT-Plus, наоборот,
пробрасывается снаружи, иначе телефон не подключится.

## Безопасность

- `.env` создаётся с правами `0600`.
- Установщик не печатает пароли и секреты в консоль.
- Панель не отдаёт `.env`.
- Не выставляйте `8080`, `9090`, `1443` и `53` в интернет.
- SOCKS-креды лежат в `.env`; не публикуйте файл.
- Не коммитьте `.env`, логи и дампы: в логах могут быть креды.

## Ограничения

- UDP из LAN идёт мимо политики: auto-redirect в mihomo работает только для TCP.
- QUIC режется при `QUIC_DROP=yes`; приложения откатываются на TCP.
- Discord-голос через SOCKS не спасти: только текст и картинки.
- fake-ip: домены из `FAKEIP_EXTRA` получают реальный IP; добавляйте туда
  чувствительные к подмене приложения.
- IPv6 в LAN обходит шлюз — отключайте его в роутере.
- Двойной NAT (LAN → шлюз → роутер → провайдер) — нормально.

## Если что-то сломалось

- **mihomo не стартует.** `systemctl status mihomo`, `journalctl -u mihomo -n 50`.
  Проверьте, что порт `:53` свободен, есть `/dev/net/tun`, а конфиг валиден:
  `mihomo -d /etc/homegateway -t`.
- **`mihomo v3` / `unsupported instruction`.** Нужен `-compatible` бинарник.
  Установщик уже ставит его на amd64; при ручной замене перекачайте
  `mihomo-linux-amd64-compatible`.
- **YouTube FAIL.** Проверьте `--dpi-desync-fwmark=0x80000` в
  `/etc/homegateway/zapret.conf`.
- **Таймер затёр ручные правки.** Правьте `geo-custom.txt`,
  `ru-direct-custom.txt` и `dpi.txt`, а не генерируемые `geo.txt` /
  `ru-direct.txt`.
- **Панель не пускает.** Проверьте `PANEL_PASS` в `.env`; пустой пароль —
  панель не стартует.
- **tgws не подключается.** Проверьте Docker, `docker logs homegw-tgwsproxy`
  и совпадение секрета в клиенте.
- **WDTT.** `systemctl status wdtt`, `journalctl -u wdtt -n 50`,
  `ss -lunp | grep 56000`.

## Обновление и удаление

Повторный `sudo bash install.sh` обновляет mihomo при новой версии, юниты и
конфиги; `.env` и списки сохраняются. `config.yaml` и `zapret.conf` создаются
заново (рендер из `.env` и шаблона), поэтому ручные правки `zapret.conf` лучше
сохранить заранее.

`sudo bash uninstall.sh` останавливает и удаляет юниты, панель, zapret,
mihomo, таймер и апдейтер списков; контейнер tg-ws-proxy также останавливается
и удаляется. Каталог `/etc/homegateway` остаётся. WDTT-Plus не затрагивается.

## Лицензия

MIT, см. `LICENSE`.

## Credits

- mihomo — MetaCubeX
- zapret — bol-van
- tg-ws-proxy-rs — valnesfjord
- zashboard — Zephyruso
- WDTT-Plus — Ivan4537
- списки Amnezia — lib4u/amnezia-tunneling-ru
- geoblock — Internet-Helper/Unblock-for-Russia

## Примечание об историческом префиксе

Внутренние имена `homegw`, `/etc/homegateway`, `/opt/homegateway-panel` и
юниты `homegw-*` сохранены для совместимости с уже развёрнутыми шлюзами. Это
не пользовательские данные и не название проекта.
