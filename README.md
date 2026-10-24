# Routeweave

Домашний шлюз с раздельной маршрутизацией: mihomo + zapret + списки доменов + веб-панель.

## Что это

Routeweave — прозрачный шлюз на Ubuntu/Debian, который ставится на отдельную машину за домашним роутером. Клиенты получают его как шлюз и DNS, а mihomo разводит трафик по доменам: часть доменов проходит через DPI-десинк zapret, гео-блокировки уходят через внешний SOCKS/HTTP-прокси, российские сервисы и всё остальное идут напрямую. Необязательные компоненты — Telegram-прокси (tg-ws-proxy) и WDTT-Plus для входа с телефона.

## Возможности

- Маршрутизация по доменам: отдельные списки для geo (SOCKS), dpi (zapret) и RU-направления.
- fake-ip DNS на порту 53 и правила mihomo на файловых rule-provider'ах.
- Firewall: отдельная цепочка `ROUTEWEAVE-FWD`, forwarding для LAN и VPN,
  опциональный drop QUIC выше разрешающих правил.
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
- **Списки доменов** — `/etc/routeweave/lists/`: geo, dpi, ru-direct и custom-файлы.
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

Если на шлюзе активен ufw, установщик открывает доступ из LAN и `WG_SUBNET`
к `53/tcp+udp`, `7890/tcp`, `9090/tcp`, `PANEL_PORT/tcp` и `1443/tcp` (при
`INSTALL_TGWS=yes`), а также разрешает forwarding для LAN-интерфейса и
`wdtt0`. Правила добавляются идемпотентно, повторный запуск дублей не создаёт.

## Параметры `.env`

Файл `/etc/routeweave/.env` создаётся с правами `0640 root:routeweave`:
читать его могут только root и сервисный пользователь панели. Секреты
генерирует установщик, в консоль они не печатаются.

| Ключ | Назначение |
|---|---|
| `LAN_IFACE` | LAN-интерфейс сервера (единственная NIC) |
| `SERVER_IP` | LAN-IP сервера, статичный или зарезервированный в DHCP |
| `SOCKS_URL` | Внешний прокси для geo: `socks5h://` (имя резолвит прокси), `socks5://`, `socks://`, `http://`, `https://`; пусто = гео напрямую |
| `BASE_DOMAIN` | Домен CF Worker для tg-ws-proxy |
| `TG_CF_WORKER_DOMAIN` | Домен CF Worker (обычно совпадает с `BASE_DOMAIN`) |
| `TG_LINK_IP` | IP/хост для `tg://`-ссылки в панели; пусто = `SERVER_IP` |
| `WG_SUBNET` | Подсеть клиентов WDTT (`wdtt0`), по умолчанию `10.66.66.0/24`; policy-routing — `ip rule` priority 8990 |
| `PANEL_PORT` | Порт веб-панели, по умолчанию `8080` |
| `PANEL_USER` | Логин панели, по умолчанию `admin` |
| `PANEL_PASS` | Пароль панели; генерируется установщиком |
| `MIHOMO_SECRET` | Секрет API mihomo; генерируется установщиком |
| `TG_SECRET` | Секрет MTProto-прокси; только при `INSTALL_TGWS=yes` |
| `QUIC_DROP` | Резать UDP/443: `yes`/`no` |
| `FAKEIP_EXTRA` | Доп. записи в fake-ip-filter через запятую |

Переменные времени установки читаются из окружения, а не из `.env`:

| Переменная | Назначение |
|---|---|
| `ZAPRET_REF` | Ветка/тег zapret при клонировании и обновлении, по умолчанию `master`; фактический коммит пишется в `/etc/routeweave/zapret.commit` |
| `MIHOMO_SHA256` | Ожидаемая sha256 архива mihomo, если апстрим не публикует `<url>.sha256` |
| `ZASHBOARD_SHA256` | То же для `dist.zip` zashboard |

Если контрольная сумма недоступна ни у апстрима, ни в переменной, установщик
предупреждает, что целостность загрузки не проверена.

## После установки

1. **DHCP.** В роутере выдайте клиентам шлюз и DNS = `SERVER_IP`.
   IPv6 в LAN отключите — иначе устройства уйдут мимо шлюза.
2. **Панель.** Откройте `http://SERVER_IP:PANEL_PORT`, логин `PANEL_USER`,
   пароль — `PANEL_PASS` из `/etc/routeweave/.env`.
3. **Zashboard.** Откройте `http://SERVER_IP:9090/ui/`, укажите backend
   `http://SERVER_IP:9090` и секрет `MIHOMO_SECRET` из `.env`.
4. **Списки.** Проверьте `/etc/routeweave/lists/` и при необходимости
   добавьте домены в `geo-custom.txt`, `dpi.txt` или `ru-direct-custom.txt`
   (см. раздел «Панель»).
5. **Стратегия zapret.** Запустите `sudo /opt/zapret/blockcheck.sh`, перенесите
   рабочую стратегию в `/etc/routeweave/zapret.conf` и выполните
   `sudo systemctl restart zapret-nfqws`.

## Автообновление списков

Апдейтер: `/usr/local/sbin/routeweave-update-lists.sh`. Юниты:
`routeweave-lists-update.service` и `routeweave-lists-update.timer`.
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
sudo systemctl start routeweave-lists-update.service
journalctl -u routeweave-lists-update -n 50
```

Важно: `geo.txt` и `ru-direct.txt` — генерируемые файлы. Правьте
`geo-custom.txt`, `ru-direct-custom.txt` и `dpi.txt`.

## Панель

Панель закрыта Basic-авторизацией (`PANEL_USER` / `PANEL_PASS`). Она работает
от системного пользователя `routeweave`, а привилегированные действия
(перезапуск mihomo/zapret/панели, обновление списков, управление tgws)
выполняются через `sudo -n` строго по allowlist из
`/etc/sudoers.d/routeweave-panel` — без wildcard. Изменяющие запросы защищены
от CSRF, ответы отдаются с CSP и security-заголовками. Вкладки:

- `geo-custom (→ SOCKS)` — пишет `geo-custom.txt`;
- `dpi (→ zapret)` — пишет `dpi.txt`;
- `ru-custom (→ direct)` — пишет `ru-direct-custom.txt`.

Кнопка «Сохранить» только пишет файл. Кнопка «Сохранить, обновить списки и
mihomo» запускает апдейтер (для `dpi` — перезапуск `zapret-nfqws`); применение
выполняется в фоне, прогресс отдаёт `GET /api/jobs/apply`. Подсказка в панели
напоминает, что `geo.txt` и `ru-direct.txt` генерируются таймером, поэтому
править нужно custom-списки и `dpi.txt`. Zashboard открывается
по `http://SERVER_IP:9090/ui/`.

При заданном `TG_SECRET` панель показывает готовую ссылку
`tg://proxy?server=<TG_LINK_IP|SERVER_IP>&port=1443&secret=<TG_SECRET>`.

## DPI и fwmark

Критично: в `EXTRA` в `/etc/routeweave/zapret.conf` обязателен
`--dpi-desync-fwmark=0x80000`. Без метки фейковые пакеты nfqws уходят в TUN
mihomo и съедаются — YouTube не пробивается. Маркированные пакеты должны
уходить в `main`, а не в TUN.

На amd64 установщик качает `mihomo-linux-amd64-compatible` (без AVX), на
arm64 — `mihomo-linux-arm64`. Стратегия по умолчанию — `fake,multidisorder`
и `md5sig`; это стартовая стратегия, её подбирают через `blockcheck.sh`.

## Опционально: Telegram

Включается `INSTALL_TGWS=yes`. Нужны Docker Engine и compose v2, порт `1443`.
Секрет прокси для клиентов смотрите в `docker logs routeweave-tgwsproxy`.
CF Worker описан в `docs/cf-worker-tg.md`; ключи — `BASE_DOMAIN`,
`TG_CF_WORKER_DOMAIN`, `TG_LINK_IP`. Без этого компонента ядро работает как
обычно.

## Опционально: WDTT-Plus и Tailscale

WDTT-Plus включается `INSTALL_WDTT=yes`, только amd64. DTLS `56000/udp`,
внутренний WireGuard `56001/udp`, интерфейс `wdtt0`, начальный конфиг
`/root/wdtt-initial.json`. VK-хеш создаётся вручную. Управление:
`/opt/wdtt-installer/install.sh status|doctor|monitor`. Подсеть `WG_SUBNET`
(по умолчанию `10.66.66.0/24`) получает ту же политику, что LAN; для неё
добавляется `ip rule` priority 8990. Firewall учитывает `wdtt0`,
`tailscale0` и существующие `wg*`.

Tailscale настраивается отдельно, Routeweave его не трогает. При проблемах
смотрите `tailscale status`.

## Порты

| Порт | Протокол | Назначение |
|---|---|---|
| 53 | TCP + UDP | DNS mihomo (fake-ip) |
| 8080 | TCP | Веб-панель (`PANEL_PORT`) |
| 9090 | TCP | mihomo API и zashboard |
| 7890 | TCP | mixed-proxy mihomo (`allow-lan`) |
| 1443 | TCP | tg-ws-proxy (опц.) |
| 56000 | UDP | WDTT-Plus DTLS (опц.) |

Порты `53`, `8080`, `9090`, `7890` и `1443` — только LAN/WG, наружу их
пробрасывать нельзя. `56000/udp` при использовании WDTT-Plus, наоборот,
пробрасывается снаружи, иначе телефон не подключится.

## Безопасность

- Панель рассчитана на доверенную LAN. Не выставляйте `8080`, `9090`, `1443`,
  `7890` и `53` в интернет; при необходимости удалённого доступа — VPN или
  reverse-proxy с TLS (Caddy/nginx) на отдельном хосте.
- `.env` создаётся с правами `0640 root:routeweave`. Контейнер tg-ws-proxy
  получает только свои три переменные (`.env.tgws`) и не видит секретов
  панели и mihomo.
- Панель работает от пользователя `routeweave`; root-доступ — только через
  `sudo -n` по allowlist `/etc/sudoers.d/routeweave-panel`, без wildcard.
- `NoNewPrivileges=no` в юните панели стоит осознанно: иначе setuid-`sudo`
  не работает. Компенсация — пустой `CapabilityBoundingSet`,
  `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  `RestrictAddressFamilies` и узкий sudo-allowlist.
- Изменяющие запросы защищены от CSRF (`Origin`, `Referer`,
  `Sec-Fetch-Site`), CSP запрещает inline-скрипты и стили, ответы получают
  security-заголовки.
- Firewall держит правила в отдельной цепочке `ROUTEWEAVE-FWD`; `QUIC_DROP`
  стоит выше `ACCEPT`.
- Установщик не печатает секреты в консоль и не отдаёт `.env` через панель.
- SOCKS-креды лежат в `.env`; не публикуйте файл, логи и дампы.
- Не коммитьте `.env`, логи и дампы: в логах могут быть креды.

Подробности модели угроз и порядок ответственного раскрытия — в
[`SECURITY.md`](SECURITY.md).

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
  `mihomo -d /etc/routeweave -t`.
- **`mihomo v3` / `unsupported instruction`.** Нужен `-compatible` бинарник.
  Установщик уже ставит его на amd64; при ручной замене перекачайте
  `mihomo-linux-amd64-compatible`.
- **YouTube FAIL.** Проверьте `--dpi-desync-fwmark=0x80000` в
  `/etc/routeweave/zapret.conf`.
- **Таймер затёр ручные правки.** Правьте `geo-custom.txt`,
  `ru-direct-custom.txt` и `dpi.txt`, а не генерируемые `geo.txt` /
  `ru-direct.txt`.
- **Панель не пускает.** Проверьте `PANEL_PASS` в `.env`; пустой пароль —
  панель не стартует.
- **tgws не подключается.** Проверьте Docker, `docker logs routeweave-tgwsproxy`
  и совпадение секрета в клиенте.
- **WDTT.** `systemctl status wdtt`, `journalctl -u wdtt -n 50`,
  `ss -lunp | grep 56000`.

## Обновление и удаление

Повторный `sudo bash install.sh` обновляет mihomo при новой версии, юниты и
конфиги; `.env` и списки сохраняются. `zapret.conf` **сохраняется**: при
изменении `LAN_IFACE` обновляется только строка `WAN_IFACE=`, а перед
правкой делается бэкап `zapret.conf.bak-<timestamp>`. `config.yaml`
регенерируется из `.env`, но прежняя версия также сохраняется в бэкап.

`sudo bash uninstall.sh` останавливает и удаляет юниты, панель, zapret,
mihomo, таймер и апдейтер списков; контейнер tg-ws-proxy также останавливается
и удаляется. Каталог `/etc/routeweave` остаётся. WDTT-Plus не затрагивается.

## Разработка

Зависимости для тестов ставьте в venv:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r panel/requirements.txt pytest pyyaml
```

Основные команды:

```bash
make lint    # bash -n, shellcheck, ruff, compileall
make test    # pytest tests -v
make all     # lint + test (цель по умолчанию)
make help    # список целей
```

`make lint` требует `shellcheck` и `ruff` в PATH; пути можно задать
переменными (`make lint SHELLCHECK=... RUFF=...`). Firewall-тесты помечены
`privileged` и без root/netns корректно пропускаются. CI (GitHub Actions)
прогоняет линт, тесты и отдельный job с `NET_ADMIN`/`SYS_ADMIN` для
firewall-тестов.

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
