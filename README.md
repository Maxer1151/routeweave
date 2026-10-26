# Routeweave

Домашний шлюз с раздельной маршрутизацией: mihomo + zapret + списки доменов + веб-панель.

## Что это

Routeweave — прозрачный шлюз на Ubuntu/Debian, который ставится на отдельную машину за домашним роутером. Клиенты получают его как шлюз и DNS, а mihomo разводит трафик по доменам: часть доменов проходит через DPI-десинк zapret, гео-блокировки уходят через внешний SOCKS/HTTP-прокси, российские сервисы и всё остальное идут напрямую. Необязательные компоненты — Telegram-прокси (tg-ws-proxy), WDTT-Plus для входа с телефона и Tailscale (exit node).

## Возможности

- Маршрутизация по доменам: отдельные списки для geo (SOCKS), dpi (zapret) и RU-направления.
- fake-ip DNS на порту 53 и правила mihomo на файловых rule-provider'ах.
- Firewall: отдельные цепочки `ROUTEWEAVE-IN` (вход на шлюз) и
  `ROUTEWEAVE-FWD` (forwarding для LAN, VPN и tailnet), опциональный drop QUIC
  выше разрешающих правил.
- Tailscale как третий вход: сервер работает как exit node, `tailscale0`
  получает ту же политику, что LAN и WDTT.
- Автообновление списков по systemd-таймеру.
- Веб-панель: статусы сервисов, пробы путей, редактор списков, перезапуски.
- zashboard для соединений и трафика.
- Опциональные tg-ws-proxy, WDTT-Plus и Tailscale.

## Архитектура

```text
                       ┌────────── Routeweave: шлюз (Ubuntu/Debian, 1 NIC) ───────────┐
  LAN ────────────────►│ firewall ─► mihomo: TUN, fake-ip DNS :53, правила            │──► DIRECT: RU / GEOIP,RU
  WDTT (опц.) ────────►│ wdtt0 ────► та же политика, что у LAN                        │──► DIRECT + zapret: dpi.txt
  Tailscale (опц.) ───►│ tailscale0 ──► та же политика, что у LAN                     │
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
  перезапуски; Basic-авторизация, один воркер, адрес прослушивания `PANEL_BIND`.
- **Firewall** — `fw-base.sh` / `fw-wan.sh` / `fw-nfq.sh` / `fw-tailscale.sh`,
  systemd-юниты, policy-routing для WDTT и Tailscale.

Опционально:

- **tg-ws-proxy-rs** — Telegram MTProto-прокси в Docker, порт `1443`.
- **WDTT-Plus** — сервер WDTT в systemd (`wdtt`), только amd64: DTLS
  `56000/udp`, внутренний WireGuard `56001/udp`, интерфейс `wdtt0`.
- **Tailscale** — опциональный exit node: ставится установщиком при
  `INSTALL_TAILSCALE=yes` (или вручную), интерфейс `tailscale0`.

## Требования

- Ubuntu 22.04+ или Debian 12+, Python 3.10+, архитектура amd64/arm64,
  ≥1 ГБ RAM, ≥3 ГБ диска. Более старые выпуски с Python 3.8 не
  поддерживаются: панели и генератору конфига нужен 3.10+.
- Одна NIC за роутером, статичный или зарезервированный в DHCP LAN-IP.
- Доступ к github.com (mihomo, zashboard, zapret, WDTT-Plus).
- Для Telegram-прокси: заранее установленные Docker Engine и compose v2.
  Установщик Docker **не ставит**.
- Для WDTT-Plus: amd64 и внешний UDP-порт (по умолчанию `56000`) для проброса.
- Для Tailscale: Ubuntu/Debian и доступ к официальному apt-репозиторию
  Tailscale (нужен интернет). Авторизация — ключ (`TS_AUTHKEY_FILE` или
  `TS_AUTHKEY`) либо ручной `tailscale up` после установки.

## Быстрый старт

```bash
git clone https://github.com/Maxer1151/routeweave.git
cd routeweave
sudo bash install.sh                  # интерактивно
sudo bash install.sh --defaults       # только ядро, без вопросов
sudo env INSTALL_TGWS=yes bash install.sh   # + Telegram-прокси (нужен Docker)
sudo env INSTALL_WDTT=yes bash install.sh   # + WDTT-Plus (amd64)
sudo env INSTALL_TAILSCALE=yes bash install.sh  # + Tailscale exit node
```

`--defaults` ставит только ядро; tgws, WDTT и Tailscale по умолчанию
выключены. Версии mihomo, zashboard, WDTT и zapret закреплены в `install.sh`
вместе с sha256/коммитом, а не берутся «последним релизом». Повторный запуск
после `git pull` обновляет управляемые ключи `/etc/routeweave/.env` (сначала
делает бэкап `$ETC/.env.bak.YYYYmmdd-HHMMSS`, `0600 root:root`), чужие ключи и
комментарии сохраняет, перегенерирует `config.yaml`, обновляет юниты и
перезапускает ядро: `routeweave-firewall`, `mihomo`, `zapret-nfqws` и
`routeweave-panel` (юниты перезапускаются, а не только перечитывают конфиг).
Поэтому исправление безопасности применяется без ручного
`systemctl restart`, но результат стоит проверить через `systemctl status`.
Списки и `zapret.conf` сохраняются.

Если на шлюзе активен ufw, установщик открывает доступ из LAN и `WG_SUBNET`
к `53/tcp+udp`, `7890/tcp`, `9090/tcp`, `PANEL_PORT/tcp` и `1443/tcp` (при
`INSTALL_TGWS=yes`), а также разрешает forwarding для LAN-интерфейса и
`wdtt0`. При `INSTALL_TAILSCALE=yes` или уже установленном Tailscale
добавляются правила для `tailscale0`, `100.64.0.0/10` и `41641/udp`, а
forwarding разрешён для tailnet. Правила добавляются идемпотентно, повторный
запуск дублей не создаёт. Независимо от ufw вход на сам шлюз ограничивает
цепочка `ROUTEWEAVE-IN` (прыжок в `INPUT 1`): политика `INPUT` не меняется,
но панель, API mihomo, DNS и прокси открыты только из доверенных зон.

## Параметры `.env`

Файл `/etc/routeweave/.env` создаётся с правами `0640 root:routeweave`:
читать его могут только root и сервисный пользователь панели. Значения
записываются в одинарных кавычках (`KEY='value'`), апостроф внутри значения —
`'\''`. Файл — данные, а не shell-скрипт: `install.sh`, апдейтер списков и
панель разбирают его построчно (`env_get`/shlex-подобный парсер), `source` и
`eval` в установщике и апдейтере не используются. Секреты генерирует
установщик; в консоль они не печатаются, кроме сгенерированного `PANEL_PASS`
в итоговой сводке — его нужно один раз передать пользователю. `MIHOMO_SECRET`,
`TG_SECRET` и `SOCKS_URL` остаются только в файле.

Повторная установка делает бэкап `$ETC/.env.bak.YYYYmmdd-HHMMSS`
(`0600 root:root`) и обновляет (upsert) все управляемые ключи значениями из
ответов и окружения; незнакомые ключи и комментарии сохраняются.

| Ключ | Назначение |
|---|---|
| `LAN_IFACE` | LAN-интерфейс сервера (единственная NIC) |
| `SERVER_IP` | LAN-IP сервера, статичный или зарезервированный в DHCP |
| `SOCKS_URL` | Внешний прокси для geo: `socks5h://` (имя резолвит прокси), `socks5://`, `socks://`, `http://`; `https://` отклоняется — TLS к прокси не поддерживается; пусто = гео напрямую |
| `PANEL_BIND` | Адрес прослушивания панели; по умолчанию `SERVER_IP`, если он пуст — `127.0.0.1` |
| `PANEL_PORT` | Порт веб-панели, по умолчанию `8080` |
| `PANEL_ALLOWED_HOSTS` | Доп. hostname/IP через запятую для проверки заголовка `Host`. Пусто **не** означает «любые»: всегда разрешены `SERVER_IP`, `localhost`, `127.0.0.1`, `::1`; неизвестный `Host` получает `421` |
| `PANEL_RATE_LIMIT` | Лимит запросов `N/M` (N за M секунд), по умолчанию `120/60` |
| `PANEL_AUTH_RATE_LIMIT` | Отдельный лимит на неуспешный Basic-auth, по умолчанию `10/60`; в `.env` пишется вручную |
| `PANEL_TLS` | `yes`, если панель отдаётся по https через reverse-proxy: меняет только ожидаемую схему в сравнении `Origin`/`Referer` при CSRF-проверке. Встроенного TLS нет |
| `PANEL_USER` | Логин панели, по умолчанию `admin` |
| `PANEL_PASS` | Пароль панели; генерируется установщиком, печатается один раз в итоговой сводке |
| `MIHOMO_API` | Адрес API mihomo для панели и апдейтера; по умолчанию `http://MIHOMO_BIND:9090` (пустой `MIHOMO_BIND` = `SERVER_IP`, `0.0.0.0` заменяется на `127.0.0.1`); при смене `MIHOMO_BIND` обновите `MIHOMO_API` вручную — установщик не пересчитывает уже записанное значение |
| `MIHOMO_BIND` | Адрес external-controller и `bind-address` mihomo, по умолчанию `SERVER_IP`; `0.0.0.0` допустим только осознанно (установщик предупреждает) |
| `MIHOMO_ALLOW_LAN` | `no` по умолчанию: mixed-port `7890` не доступен из LAN; `yes` включает `allow-lan` |
| `MIHOMO_TUN_MTU` | MTU TUN mihomo, по умолчанию `1280`; допустимо 576..9000 |
| `DNS_BIND` | Адрес DNS-листенера mihomo (порт 53), по умолчанию `SERVER_IP` |
| `SSH_PORT` | Порт SSH хоста, по умолчанию `22`; 1..65535. Используется правилом firewall и подсказками панели |
| `MIHOMO_SECRET` | Секрет API mihomo; генерируется установщиком |
| `BASE_DOMAIN` | Домен CF Worker для tg-ws-proxy |
| `TG_CF_WORKER_DOMAIN` | Домен CF Worker (обычно совпадает с `BASE_DOMAIN`) |
| `TG_PINNED_UPSTREAM` | Порядок апстримов tg-ws-proxy (`--pinned-upstream`), тиры через запятую: `ws`, `cfworker`, `cfproxy`, `mtproto`, `tcp`. Пишет установщик: по умолчанию `cfworker,ws,tcp`, если задан домен CF Worker; переопределяется одноимённой переменной окружения при установке (`TG_PINNED_UPSTREAM=... ./install.sh`); пусто = порядок прокси по умолчанию |
| `TG_LINK_IP` | IP/хост для `tg://`-ссылки в панели; пусто = `SERVER_IP` |
| `TG_SECRET` | Секрет MTProto-прокси; только при `INSTALL_TGWS=yes` |
| `WG_SUBNET` | Подсеть клиентов WDTT (`wdtt0`), по умолчанию `10.66.66.0/24`; policy-routing — `ip rule` priority 8990 |
| `TS_IFACE` | Интерфейс Tailscale, по умолчанию `tailscale0`; установщик не пишет, можно добавить вручную |
| `TS_SUBNET` | Подсеть tailnet, по умолчанию `100.64.0.0/10`; установщик не пишет, можно добавить вручную |
| `TS_EXIT_MODE` | Режим exit node: `policy` — трафик tailnet идёт через TUN mihomo (geo/DPI), `nat` — прямой NAT в WAN (быстрее, но без geo/DPI, источник маскарадится); по умолчанию `policy`, установщик не пишет |
| `WAN_INGRESS` | `yes` — включить доступ из WAN к опубликованным портам; по умолчанию `no` |
| `WAN_PORTS` | Порты для WAN-доступа: `8081`, `8081/tcp`, `51820/udp`, диапазоны `8000:8100/tcp` / `8000-8100/tcp`; голое число = tcp |
| `WAN_INGRESS_MARK` | Метка connmark для WAN-доступа, по умолчанию `0x200000`. `0x40000` (tailscale subnet-route), `0x80000` (zapret), `0x100000` (NAT exit node) и `0x0` запрещены |
| `WAN_INGRESS_RULE_PRIO` | Приоритет `ip rule` для помеченных пакетов, по умолчанию `8993`; `8990`, `8991`, `8992`, `9000`, `9001`, `9002` зарезервированы и отвергаются |
| `QUIC_DROP` | Резать UDP/443: `yes`/`no` |
| `FAKEIP_EXTRA` | Доп. записи в fake-ip-filter через запятую |

Переменные времени установки читаются из окружения, а не из `.env`:

| Переменная | Назначение |
|---|---|
| `INSTALL_WDTT` | `yes`/`no` — ставить WDTT-Plus; по умолчанию `no`, `--defaults` — `no` |
| `INSTALL_TGWS` | `yes`/`no` — ставить tg-ws-proxy; по умолчанию `no`, `--defaults` — `no` |
| `INSTALL_TAILSCALE` | `yes`/`no` — поставить Tailscale и настроить exit node; по умолчанию `no`, `--defaults` — `no` |
| `TS_AUTHKEY_FILE` | файл с ключом авторизации Tailscale на время установки; предпочтительнее `TS_AUTHKEY`; в `.env` не пишется |
| `TS_AUTHKEY` | ключ авторизации строкой; только на время установки, временный файл `0600` удаляется |
| `ZAPRET_REF` | ref zapret при клонировании и обновлении; по умолчанию закреплённый 40-hex commit. Ветка/тег — осознанный override с предупреждением, фактический HEAD сверяется и пишется в `/etc/routeweave/zapret.commit` |
| `ALLOW_UNVERIFIED` | `yes` — пропустить проверку sha256 скачанных архивов (эквивалент `--allow-unverified`); только для отладки, громко предупреждает. Проверку коммита zapret не отключает |

`INSTALL_*` читаются из окружения или запрашиваются интерактивно; при
`--defaults` вопросы не задаются. Ключ Tailscale (`TS_AUTHKEY_FILE` или
`TS_AUTHKEY`) действует только на время установки и не сохраняется в
`/etc/routeweave/.env`.

Скачанные mihomo, zashboard и WDTT сверяются с sha256, зашитыми в `install.sh`;
без совпадения установка останавливается. Архивы zashboard и WDTT
распаковываются в `mktemp`, симлинки, setuid и абсолютные пути/`..`
отклоняются, замена каталога атомарная. zapret собирается из закреплённого
коммита; `ZAPRET_REF` с веткой или тегом — осознанный override с
предупреждением, фактический HEAD всё равно сверяется и пишется в
`/etc/routeweave/zapret.commit`. `--allow-unverified`/`ALLOW_UNVERIFIED=yes`
отключает только проверку архивов и громко предупреждает; проверку коммита
zapret он не отключает.

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
6. **Tailscale (если ставили).** Одобрите шлюз как exit node в admin console
   (**Machines** → **Edit exit node** → **Use as exit node**) и настройте DNS
   tailnet: **Use with exit node** и nameserver `100.x.y.z` (см. раздел
   «Опционально: Tailscale»). DNS через fake-ip работает только при
   `TS_EXIT_MODE=policy`; в режиме `nat` запросы уходят в WAN напрямую.

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

Панель закрыта Basic-авторизацией (`PANEL_USER` / `PANEL_PASS`). Она слушает
`PANEL_BIND` (по умолчанию `SERVER_IP`, иначе `127.0.0.1`) и работает ровно в
одном воркере: состояние заданий и кэш `/api/status` живут в памяти процесса,
multi-worker не поддерживается. Юнит ограничен `MemoryMax=256M` и `TasksMax=64`.
Панель работает от системного пользователя `routeweave`, а привилегированные
действия (перезапуск mihomo/zapret/панели, обновление списков, управление tgws)
выполняются через `sudo -n` строго по allowlist из
`/etc/sudoers.d/routeweave-panel` — без wildcard.

Проверка `Host` идёт до аутентификации: разрешены только `SERVER_IP`,
`localhost`, `127.0.0.1`, `::1` и значения из `PANEL_ALLOWED_HOSTS`; запрос с
другим `Host` получает `421`. Лимит запросов задаёт `PANEL_RATE_LIMIT`
(`120/60` по умолчанию), неуспешный Basic-auth ограничен отдельно
`PANEL_AUTH_RATE_LIMIT` (`10/60`); превышение — `429`. Тело списка больше
200 КБ отклоняется с `413`, в том числе при chunked-передаче. `/api/status`
кэшируется на 3 секунды и считает статус один раз (single-flight); при
переполнении одновременных запросов — `429`.

Изменяющие запросы (`POST`/`PUT`/`DELETE`/`PATCH`) защищены fail-closed: нужен
совпадающий `Origin`/`Referer`, либо `Sec-Fetch-Site: same-origin|none`, либо —
если браузерных заголовков нет — `X-Requested-With: Routeweave`. Иначе `403`.
Для ручного вызова из curl добавьте этот заголовок:

```bash
curl -u admin:ПАРОЛЬ -X PUT \
  -H 'X-Requested-With: Routeweave' \
  --data-binary @dpi.txt \
  http://SERVER_IP:PANEL_PORT/api/lists/dpi
```

Ответы отдаются с CSP (`frame-ancestors 'none'`, `object-src 'none'`),
`Permissions-Policy`, COOP/CORP; inline-скрипты и стили запрещены. Домены в
списках проверяются по меткам: дефис в начале или конце метки (`a-.com`)
отклоняется.

Вкладки:

- `geo-custom (→ SOCKS)` — пишет `geo-custom.txt`;
- `dpi (→ zapret)` — пишет `dpi.txt`;
- `ru-custom (→ direct)` — пишет `ru-direct-custom.txt`.

Кнопка «Сохранить» только пишет файл. Кнопка «Сохранить, обновить списки и
mihomo» запускает апдейтер (для `dpi` — перезапуск `zapret-nfqws`); применение
выполняется в фоне, прогресс отдаёт `GET /api/jobs/apply`. Подсказка в панели
напоминает, что `geo.txt` и `ru-direct.txt` генерируются таймером, поэтому
править нужно custom-списки и `dpi.txt`. Zashboard панель не раздаёт: его
отдаёт сам mihomo по `http://SERVER_IP:9090/ui/`, это отдельный origin, и CSP
панели на него не распространяется.

При заданном `TG_SECRET` панель показывает готовую ссылку
`tg://proxy?server=<TG_LINK_IP|SERVER_IP>&port=1443&secret=<TG_SECRET>`.

Если Tailscale установлен, в панели есть карточка **Tailscale**: статус
`tailscaled`, tailnet-адрес, состояние exit node (анонсирован и одобрен / ждёт
одобрения / не анонсирован), суммарный трафик `↓`/`↑`, оценка скорости и число
пиров. Данные отдаёт helper `/usr/local/sbin/routeweave-tailscale-state.sh`: он
только читает состояние (`systemctl`, `tailscale status --json`, счётчики
sysfs), возвращает агрегаты без ключей и имён устройств и входит в
sudo-allowlist панели без wildcard. Карточки нет, если Tailscale не установлен;
одобрение exit node по-прежнему делается в admin console.

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
`TG_CF_WORKER_DOMAIN`, `TG_LINK_IP`. При заданном домене CF Worker установщик
ставит Worker первым в цепочке апстримов (`TG_PINNED_UPSTREAM`), иначе каждая
новая сессия платит 2–5 с за заведомо недоступный прямой WS — подробности в
[`docs/cf-worker-tg.md`](docs/cf-worker-tg.md). Без этого компонента ядро
работает как обычно.

Образ запинен по digest
(`valnesfjord/tg-ws-proxy-rs:v2.5.8@sha256:d10285e7…`), контейнер запускается
с `read_only`, `cap_drop: ALL`, `no-new-privileges`, лимитами памяти/pids/CPU
и ротацией логов. Порт публикуется как `${SERVER_IP}:1443:1443`, а не на
`0.0.0.0`. Docker при этом всё равно вставляет свои правила в `DOCKER-USER`,
которыми Routeweave не управляет: если `SERVER_IP` — публичный адрес или
роутер пробрасывает `1443`, прокси будет доступен извне. Не пробрасывайте
`1443` на роутере; полноценное ограничение `DOCKER-USER` отложено (см.
`TODO.md`).

## Опционально: WDTT-Plus

WDTT-Plus включается `INSTALL_WDTT=yes`, только amd64. DTLS `56000/udp`,
внутренний WireGuard `56001/udp`, интерфейс `wdtt0`, начальный конфиг
`/root/wdtt-initial.json`. VK-хеш создаётся вручную. Управление:
`/opt/wdtt-installer/install.sh status|doctor|monitor`. Подсеть `WG_SUBNET`
(по умолчанию `10.66.66.0/24`) получает ту же политику, что LAN; для неё
добавляется `ip rule` priority 8990. Firewall учитывает `wdtt0` и
существующие `wg*`; `tailscale0` — в разделе ниже.

## Опционально: Tailscale (exit node)

Tailscale ставится установщиком при `INSTALL_TAILSCALE=yes` (интерактивный
вопрос, по умолчанию `no`; `--defaults` — `no`). Без него шлюз работает как
обычно. Сервер работает как exit node, а `tailscale0` — третий вход шлюза.
Режим задаёт `TS_EXIT_MODE`:

- `policy` (по умолчанию) — трафик клиентов уходит в TUN mihomo и получает
  ту же политику, что LAN и WDTT (geo → SOCKS, dpi → zapret, RU → direct);
- `nat` — прямой MASQUERADE в WAN: быстрее (нет userspace-стека mihomo), но
  geo/SOCKS/DPI-политика для tailnet не применяется, а источник в LAN
  маскарадится (LAN видит адрес шлюза). Включать осознанно.

Подробнее — в [`docs/tailscale-exit-node.md`](docs/tailscale-exit-node.md).

Что делает установщик:

- добавляет официальный apt-репозиторий Tailscale для Ubuntu/Debian, ставит
  пакет `tailscale` и включает `tailscaled`;
- применяет `--advertise-exit-node --netfilter-mode=nodivert
  --accept-dns=false` (для уже авторизованного узла — через `tailscale set`);
- не переустанавливает уже установленный пакет и не логинит узел заново:
  состояние `/var/lib/tailscale` сохраняется, повторный `tailscale login` не
  нужен;
- если узел не авторизован и ключа нет, не зависает: включает `tailscaled` и
  печатает команду для ручного входа.

Ключ авторизации передаётся только на время установки:

```bash
sudo env INSTALL_TAILSCALE=yes TS_AUTHKEY_FILE=/root/ts-authkey bash install.sh
```

`TS_AUTHKEY_FILE` предпочтительнее; `TS_AUTHKEY` тоже поддерживается, но
установщик временно пишет его в файл `0600` и удаляет. В `/etc/routeweave/.env`
и в репозиторий ключ не пишется. Если узел ещё не авторизован и ключа нет,
после установки выполните напечатанную команду:

```bash
sudo tailscale up --advertise-exit-node --netfilter-mode=nodivert --accept-dns=false
```

`--accept-dns=false` рекомендуется, чтобы Tailscale не перехватывал DNS самого
шлюза: DNS клиентов обслуживает mihomo.

`nodivert` обязателен: иначе Tailscale вставляет свои цепочки `ts-*`, помечает
пакеты меткой `0x40000` и делает MASQUERADE, что конфликтует с policy-routing
mihomo. Метка `0x40000` зарезервирована за tailscale subnet-route, а
`WAN_INGRESS_MARK` по умолчанию `0x200000`, поэтому эти метки не пересекаются.

Одобрение: admin console → **Machines** → у сервера **Edit exit node** →
**Use as exit node**. При кастомных ACL нужен grant на `autogroup:internet`.
В панели есть карточка Tailscale: helper
`/usr/local/sbin/routeweave-tailscale-state.sh` только читает состояние и
показывает `tailscaled`, адрес, флаги exit node и трафик; одобрение — только в
admin console.

Клиент (Linux):

```bash
sudo tailscale set --exit-node=100.x.y.z --exit-node-allow-lan-access=true
```

На Windows и Android exit node выбирается в меню приложения; там же включите
**Allow LAN access**.

DNS. В admin console → **DNS** → **Nameservers** → **Add nameserver** →
**Custom** → `100.x.y.z` (tailscale-адрес шлюза) и включите
**Use with exit node**: без этого при активном exit node Tailscale резолвит
через сам exit node, игнорируя кастомный nameserver. **Override local DNS**
включает сервер для всего tailnet, **Split DNS** — только для выбранных
доменов. В приложениях Android/Windows тумблер **Use Tailscale DNS settings**
должен быть включён: без него клиент не использует nameserver из админки,
но и одного тумблера без nameserver недостаточно — запросы не попадут в
mihomo. С nameserver клиент получает fake-ip `198.18.x.x`, и работают
доменные правила geo/dpi/ru. DoH в браузерах и Private DNS на Android
отключите: иначе приложения ходят мимо системного DNS.

**Важно:** эта схема (nameserver на `100.x.y.z:53` плюс fake-ip) работает
только при `TS_EXIT_MODE=policy`. В режиме `nat` трафик tailnet-клиентов идёт
напрямую в WAN, fake-ip DNS не применяется, и доменные правила geo/dpi/ru для
них не работают. Либо используйте `policy`, либо настраивайте DNS напрямую
(без расчёта на fake-ip). Это известное ограничение (NET-08).

Сервисы по tailscale-адресу: панель `http://100.x.y.z:PANEL_PORT`, zashboard
`http://100.x.y.z:9090/ui/`, DNS `100.x.y.z:53`, mixed-proxy
`100.x.y.z:7890` (последний доступен только при `MIHOMO_ALLOW_LAN=yes`).
Порты те же, что у LAN. Вход на шлюз ограничивает цепочка `ROUTEWEAVE-IN`:
порты открыты из `tailscale0`, `100.64.0.0/10`, `LAN_IFACE`, `wdtt0` и
`WG_SUBNET`. При активном ufw `install.sh` дополнительно добавляет правила для
`tailscale0`, `100.64.0.0/10` и `41641/udp`, если Tailscale установлен, в том
числе только что. Если Tailscale поставлен позже, повторите
`sudo bash install.sh` — правила добавятся; повторный запуск идемпотентен.

После обновления Routeweave достаточно повторить `sudo bash install.sh`: он
перегенерирует `config.yaml`, обновит правила и перезапустит юниты ядра;
пакет Tailscale не переустанавливается, состояние сохраняется. Проверьте
`systemctl status` и `fw-tailscale.sh doctor`.

Диагностика:

```bash
sudo /etc/routeweave/fw-tailscale.sh doctor
tailscale status
journalctl -u tailscaled -n 50
```

Ограничения: policy-режим идёт через userspace-стек mihomo (ниже пропускная
способность, выше нагрузка на CPU); в режиме `nat` нет geo/SOCKS/DPI-политики
mihomo для tailnet (zapret на WAN-egress продолжает работать), а источник в
LAN маскарадится; DNS на `100.x.y.z` с fake-ip имеет смысл только в `policy`;
IPv6 не поддерживается; `QUIC_DROP=yes` режет UDP/443 и у tailnet-клиентов;
fake-ip `198.18.0.0/16` может конфликтовать с другим прокси на клиенте.
`WAN_INGRESS_MARK` по умолчанию `0x200000` и с меткой Tailscale `0x40000` не
конфликтует; `0x40000` вручную задавать нельзя.

Подробности, DNS и ограничения — в
[`docs/tailscale-exit-node.md`](docs/tailscale-exit-node.md).

## Порты

| Порт | Протокол | Назначение |
|---|---|---|
| 53 | TCP + UDP | DNS mihomo (fake-ip), `DNS_BIND` |
| 8080 | TCP | Веб-панель (`PANEL_PORT`, слушает `PANEL_BIND`) |
| 9090 | TCP | mihomo API и zashboard (`MIHOMO_BIND`) |
| 7890 | TCP | mixed-proxy mihomo; из LAN только при `MIHOMO_ALLOW_LAN=yes` |
| 1443 | TCP | tg-ws-proxy (опц.), публикуется на `SERVER_IP` |
| 56000 | UDP | WDTT-Plus DTLS (опц.) |
| 41641 | UDP | Tailscale, прямые соединения (опц.) |

Порты `53`, `8080`, `9090`, `7890` и `1443` — только LAN/WG/tailnet, наружу
их пробрасывать нельзя. Вход на `53`, `8080`, `9090` и `7890` дополнительно
ограничен цепочкой `ROUTEWEAVE-IN`; `1443` публикует Docker на `SERVER_IP`, и
его периметр держат правила Docker/ufw, а не `ROUTEWEAVE-IN` (см. раздел про
tg-ws-proxy). `56000/udp` при использовании WDTT-Plus, наоборот,
пробрасывается снаружи, иначе телефон не подключится. `41641/udp` нужен только
для прямых соединений Tailscale: если шлюз за роутером, пробросьте порт на
роутере, иначе соединения пойдут через DERP-релей.

## Доступ извне (WAN)

Сервис, опубликованный на шлюзе (DNAT на docker-контейнер или локальный
процесс), из LAN открывается, а снаружи — нет. Причина не в DNAT, а в
policy-routing mihomo: TUN с `auto-route` создаёт таблицу `2022` с маршрутом
`default dev Meta` и правила `9001`/`9002`. Входящий пакет на опубликованный
порт доходит до сервиса, но ответ сервиса (src `172.21.x.x`, dst публичный
IP) попадает в правило `9002` и уходит в TUN, поэтому TCP-соединение не
устанавливается. Из LAN та же схема работает только потому, что правило
`9001` с `suppress_prefixlength 0` отдаёт connected-маршруты из `main`.

Для WAN используется маркировка соединений: входящее NEW-соединение на
указанные порты получает и метку пакета (`MARK`), и `CONNMARK`; на всех пакетах
соединения (включая ответы сервиса) метка восстанавливается, а `ip rule` с
приоритетом `8993` уводит помеченные пакеты в `main` раньше правил mihomo
`9001`/`9002`.

Включение:

```bash
sudo sed -i "s/^WAN_INGRESS=.*/WAN_INGRESS='yes'/" /etc/routeweave/.env
sudo sed -i "s|^WAN_PORTS=.*|WAN_PORTS='8081 51820/udp'|" /etc/routeweave/.env
sudo systemctl restart routeweave-firewall
sudo /etc/routeweave/fw-wan.sh status
```

`WAN_PORTS` принимает порты через пробел и/или запятую: `8081`,
`8081/tcp`, `51820/udp`, диапазоны `8000:8100/tcp` и `8000-8100/tcp`.
Голое число считается TCP. Пустой `WAN_PORTS` при `WAN_INGRESS=yes` означает
«все порты»: это помечает все входящие соединения с не-приватных адресов и
не рекомендуется.

`restore-mark` в цепочке `ROUTEWEAVE-WANMARK` стоит первым намеренно: ответ
сервиса приходит на docker-интерфейс с приватным src (`172.21.x.x`). Если бы
`RETURN` для приватных сетей срабатывал раньше, метка бы не восстановилась и
ответ снова ушёл бы в TUN.

Выключение — `WAN_INGRESS='no'` и `sudo systemctl restart routeweave-firewall`:
`fw-wan.sh start` при выключенном флаге сам снимает цепочки, прыжки и правило
`8993`. Отдельный `fw-wan.sh stop` делает то же самое.

Если после ручного `systemctl restart mihomo` доступ из WAN пропал, mihomo
пересоздал policy-routing и мог переставить правила. В юните `mihomo.service`
есть `ExecStartPost=-/etc/routeweave/fw-wan.sh start`, поэтому правило `8993`
переутверждается автоматически; на старых установках без `fw-wan.sh` юнит не
падает из-за префикса `-`. После ручного изменения `.env` всё равно нужен
`systemctl restart routeweave-firewall`.

Диагностика — `sudo /etc/routeweave/fw-wan.sh doctor`: проверяет цепочки и
прыжки, наличие правила `8993`, что его приоритет меньше правил таблицы
`2022`, что forwarded-путь помеченного пакета (от адреса клиента в `LAN_IFACE`,
а не от `SERVER_IP`) уходит не в TUN, а также предупреждает о `redirect` в
nftables и `auto-redirect: true` в `config.yaml`. При `auto-redirect` mihomo
перехватывает входящий TCP ещё до маркировки; если WAN-доступ не работает,
поставьте `auto-redirect: false` и перезапустите mihomo.

Оба WARN, про `redirect` и `auto-redirect`, — ожидаемая картина на рабочей
установке, а не признак поломки. Таблица `2022` и правила `9001`/`9002`,
которые создаёт `auto-route`, нужны для работы шлюза; правила `redirect` от
`auto-redirect: true` ограничены множеством локальных адресов
(`ip daddr @inet4_local_address_set ... return`), поэтому входящий на
LAN-адрес сервера трафик они не перехватывают. Если доступ из WAN
действительно не работает, а `doctor` при этом заканчивается строкой
`OK: проверки пройдены`, сначала поставьте `auto-redirect: false` в
`/etc/routeweave/config.yaml`, выполните `sudo systemctl restart mihomo` и
повторите проверку. `config.yaml` генерируется установщиком из `.env`, так
что ручная правка живёт до следующего запуска установщика; чтобы изменить
поведение навсегда, нужно править шаблон/генератор.

Доступ из WAN не отменяет DNAT/docker-правила и не заменяет TLS или
reverse-proxy: открывайте только те порты, которые действительно должны быть
видны снаружи, и не публикуйте панель, API mihomo и прокси.

## Безопасность

- Панель рассчитана на доверенную LAN. Не выставляйте `8080`, `9090`, `1443`,
  `7890` и `53` в интернет; при необходимости удалённого доступа — VPN (в том
  числе tailnet) или reverse-proxy с TLS (Caddy/nginx) на отдельном хосте.
- `.env` — данные, а не shell: значения в одинарных кавычках, `install.sh`,
  апдейтер и панель разбирают файл построчно и не исполняют его. Права
  `0640 root:routeweave`; при повторной установке создаётся бэкап
  `.env.bak.YYYYmmdd-HHMMSS` с `0600 root:root`. Контейнер tg-ws-proxy
  получает только переменные из своего `.env.tgws` и не видит секретов
  панели и mihomo.
- Панель работает от пользователя `routeweave`; root-доступ — только через
  `sudo -n` по allowlist `/etc/sudoers.d/routeweave-panel`, без wildcard.
- `NoNewPrivileges=no` в юните панели стоит осознанно: иначе setuid-`sudo`
  не работает. `CapabilityBoundingSet` при этом **не пустой**: в нём
  минимальный набор `CAP_SETUID CAP_SETGID CAP_CHOWN CAP_DAC_OVERRIDE
  CAP_DAC_READ_SEARCH CAP_FOWNER CAP_KILL CAP_AUDIT_WRITE CAP_SYS_CHROOT`,
  без которого setuid-`sudo` не может повысить привилегии. Широких
  capability (`CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `CAP_SYS_PTRACE`) нет;
  остальные ограничения — `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  `RestrictAddressFamilies` и узкий sudo-allowlist.
- Изменяющие запросы защищены fail-closed: нужен совпадающий
  `Origin`/`Referer`, либо `Sec-Fetch-Site: same-origin|none`, либо
  `X-Requested-With: Routeweave`. Неизвестный `Host` получает `421`, список
  больше 200 КБ — `413`, превышение лимита запросов или Basic-auth — `429`.
  CSP запрещает inline-скрипты и стили (`frame-ancestors 'none'`,
  `object-src 'none'`), ответы получают `Permissions-Policy` и COOP/CORP.
- Firewall держит правила в двух цепочках: `ROUTEWEAVE-IN` (прыжок в
  `INPUT 1`) и `ROUTEWEAVE-FWD`. Вход разрешён только из доверенных зон, а
  `QUIC_DROP` стоит выше `ACCEPT`; помеченный `WAN_INGRESS` трафик из
  QUIC-drop исключён. Политика `INPUT` не меняется.
- IPv6 осознанно не фильтруется: проект IPv4-only, `ip6tables` не трогается.
  Это известное ограничение — IPv6 в LAN нужно отключать.
- Tailnet — отдельная граница доверия: доступ к панели и API mihomo по
  tailscale-адресу регулируется ACL/grants и сроком действия ключей. Это не
  публикация в интернет.
- CGNAT-спуфинг с `100.64.0.0/10` не с `tailscale0` дропается.
- `41641/udp` открыт осознанно: только для прямых соединений Tailscale.
- tg-ws-proxy запускается из образа, запиненного по digest, с `read_only`,
  `cap_drop: ALL`, `no-new-privileges` и лимитами; порт публикуется на
  `SERVER_IP`, а не на `0.0.0.0`. Правила Docker в `DOCKER-USER` Routeweave не
  управляет: при публичном `SERVER_IP` или пробросе `1443` на роутере прокси
  будет доступен извне.
- Целостность: версии mihomo, zashboard и WDTT и их sha256 зашиты в git;
  несовпадение останавливает установку. Архивы распаковываются в `mktemp` с
  отказом от symlink/setuid и путей `..`, zapret собирается из закреплённого
  коммита. `--allow-unverified` — только для отладки и не отключает проверку
  коммита zapret.
- Установщик печатает только сгенерированный `PANEL_PASS` — один раз в
  итоговой сводке; `MIHOMO_SECRET`, `TG_SECRET` и `SOCKS_URL` в консоль не
  попадают, панель `.env` не отдаёт.
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
- IPv6 в LAN обходит шлюз — отключайте его в роутере. Firewall Routeweave
  фильтрует только IPv4; `ip6tables` не трогается.
- mixed-port `7890` по умолчанию слушает только localhost: из LAN он доступен
  лишь при `MIHOMO_ALLOW_LAN=yes`.
- Docker вставляет свои правила в `DOCKER-USER`; Routeweave ими не управляет.
  Порт `1443` опубликован на `SERVER_IP` — не пробрасывайте его на роутере.
- zashboard отдаёт сам mihomo на `:9090/ui/` — это отдельный origin, не часть
  панели.
- Двойной NAT (LAN → шлюз → роутер → провайдер) — нормально.

## Если что-то сломалось

- **mihomo не стартует.** `systemctl status mihomo`, `journalctl -u mihomo -n 50`.
  Проверьте, что порт `:53` свободен, есть `/dev/net/tun`, а конфиг валиден:
  `mihomo -d /etc/routeweave -t`.
- **DNS после удаления не работает.** Drop-in
  `/etc/systemd/resolved.conf.d/routeweave.conf` помечен строкой
  `# Generated by Routeweave installer`, и `uninstall.sh` удаляет только свой
  файл (чужой восстанавливает из бэкапа). Если после удаления
  `/etc/resolv.conf` указывает на `127.0.0.53`, а порт 53 никто не слушает,
  скрипт записывает рабочий статический `resolv.conf` с публичным DNS.
  Если до установки `/etc/resolv.conf` был симлинком (в Ubuntu 26.04 — на
  `../run/systemd/resolve/stub-resolv.conf`), `uninstall.sh` после рестарта
  `systemd-resolved` возвращает ссылку, а не обычный файл с её содержимым;
  цель сохраняется при установке в `backup/resolv.conf.orig.link`.
  Повторный `install.sh` не трогает `systemd-resolved`, если порт 53 уже
  слушает mihomo.
- **`mihomo v3` / `unsupported instruction`.** Нужен `-compatible` бинарник.
  Установщик уже ставит его на amd64; при ручной замене перекачайте
  `mihomo-linux-amd64-compatible`.
- **YouTube FAIL.** Проверьте `--dpi-desync-fwmark=0x80000` в
  `/etc/routeweave/zapret.conf`.
- **Таймер затёр ручные правки.** Правьте `geo-custom.txt`,
  `ru-direct-custom.txt` и `dpi.txt`, а не генерируемые `geo.txt` /
  `ru-direct.txt`.
- **Панель не пускает.** Проверьте `PANEL_PASS` в `.env`; пустой пароль —
  панель не стартует. `421` означает неизвестный `Host` (добавьте его в
  `PANEL_ALLOWED_HOSTS`), `429` — сработал `PANEL_RATE_LIMIT` или
  `PANEL_AUTH_RATE_LIMIT`, `413` — список больше 200 КБ, `403` — не пройдена
  CSRF-проверка (для curl нужен `X-Requested-With: Routeweave`).
- **Снаружи не открывается сервис на опубликованном порту.** Запустите
  `sudo /etc/routeweave/fw-wan.sh doctor`: он покажет, помечаются ли
  соединения, стоит ли `ip rule` 8993 выше правил mihomo и не уходит ли
  помеченный маршрут в TUN. Проверьте, что порт есть в `WAN_PORTS`, а
  `WAN_INGRESS=yes`.
- **tgws не подключается.** Проверьте Docker, `docker logs routeweave-tgwsproxy`
  и совпадение секрета в клиенте.
- **WDTT.** `systemctl status wdtt`, `journalctl -u wdtt -n 50`,
  `ss -lunp | grep 56000`.
- **Нет выхода через Tailscale exit node.** Запустите
  `sudo /etc/routeweave/fw-tailscale.sh doctor`: он проверяет tailscaled,
  `ip_forward`, правило `8991`, правила `ROUTEWEAVE-FWD` и отсутствие прыжков
  в `ts-*`. Убедитесь, что exit node одобрен в admin console, а Tailscale
  работает с `--netfilter-mode=nodivert`. Логи: `tailscale status`,
  `journalctl -u tailscaled -n 50`.

## Обновление и удаление

Повторный `sudo bash install.sh` сверяет закреплённые версии, обновляет
mihomo/zashboard при необходимости, юниты и конфиги, делает бэкап и upsert
управляемых ключей `.env`, перегенерирует `config.yaml` и перезапускает
юниты ядра. `zapret.conf` **сохраняется**: при изменении `LAN_IFACE`
обновляется только строка `WAN_IFACE=`, а перед правкой делается бэкап
`zapret.conf.bak-<timestamp>`. Прежний `config.yaml` тоже сохраняется в
бэкап. После обновления проверьте `systemctl status` и `fw-wan.sh doctor`.

`sudo bash uninstall.sh [--dry-run] [--purge]` останавливает и удаляет юниты,
панель, zapret, mihomo, таймер и апдейтер списков; контейнер tg-ws-proxy также
останавливается и удаляется. По умолчанию каталог `/etc/routeweave` с секретами
сохраняется: в интерактивном режиме скрипт спросит подтверждение, при
неинтерактивном — напечатает предупреждение о ротации секретов и команду с
`--purge`. `--purge` удаляет каталог, `--dry-run` только печатает план и не
требует root. Скрипт восстанавливает `net.ipv4.ip_forward` из
`ip_forward.orig`, удаляет `/opt/zapret`, `mihomo`, юниты и drop-in
`systemd-resolved` только при подтверждении владения (маркер `owned` и
бэкапы) — чужие файлы не трогает. WDTT-Plus остаётся, его `ip rule` 8990
сохраняется. Пакет Tailscale и состояние `/var/lib/tailscale` не удаляются;
пакет при необходимости удаляется вручную.

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

python -m pytest tests -q   # обычный прогон; netns-тесты без root пропускаются
```

`make lint` требует `shellcheck` и `ruff` в PATH; пути можно задать
переменными (`make lint SHELLCHECK=... RUFF=...`). Привилегированные
netns-тесты помечены `privileged` и без root корректно пропускаются. Отдельный
CI job запускает все тесты с маркером `privileged` (netns-модули:
`test_firewall.py`, `test_wan_ingress.py`, `test_tailscale_nat.py`,
`test_tailscale_exit.py`, `test_input_policy.py`, `test_fw_nfq.py`) под root и
падает, если в логе есть хоть один `SKIPPED`. Другие job'ы прогоняют
`docker compose config` и pytest с порогом покрытия `panel/`.

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
