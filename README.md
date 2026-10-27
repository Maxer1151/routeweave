# Routeweave

Домашний шлюз с раздельной маршрутизацией: mihomo + zapret + списки доменов + веб-панель.

Routeweave — прозрачный шлюз на Ubuntu/Debian, который ставится на отдельную
машину за домашним роутером. Клиенты получают его как шлюз и DNS, а mihomo
разводит трафик по доменам: часть доменов проходит через DPI-десинк zapret,
гео-блокировки уходят через внешний SOCKS/HTTP-прокси, российские сервисы и всё
остальное идут напрямую. Необязательные компоненты — Telegram-прокси
(tg-ws-proxy), WDTT-Plus для входа с телефона и Tailscale (exit node).

Ставится одной командой на чистую Ubuntu/Debian: установщик сам скачивает
mihomo и zapret, генерирует конфиг, поднимает firewall, systemd-юниты, таймер
обновления списков и веб-панель. Настраивать mihomo вручную не нужно.

```text
                       ┌────────── Routeweave: шлюз (Ubuntu/Debian, 1 NIC) ───────────┐
  LAN ────────────────►│ firewall ─► mihomo: TUN, fake-ip DNS :53, правила            │──► DIRECT: RU / GEOIP,RU
  WDTT (опц.) ────────►│ wdtt0 ────► та же политика, что у LAN                        │──► DIRECT + zapret: dpi.txt
  Tailscale (опц.) ───►│ tailscale0 ──► та же политика, что у LAN                     │
                       │ zapret (nfqws) · панель :8080 · zashboard :9090/ui           │──► SOCKS/HTTP: geo.txt
                       │ tg-ws-proxy :1443 (опц.)                                     │──► WS/TLS: Telegram (опц.)
                       └──────────────────────────────────────────────────────────────┘
```

Документация для тех, кто работает с кодом репозитория (архитектура,
инварианты, тесты, стиль), — в [`AGENTS.md`](AGENTS.md).

## Возможности

- Маршрутизация по доменам: отдельные списки для geo (SOCKS), dpi (zapret) и
  RU-направления.
- fake-ip DNS на порту 53 и правила mihomo на файловых rule-provider'ах.
- Firewall: отдельные цепочки `ROUTEWEAVE-IN` (вход на шлюз) и
  `ROUTEWEAVE-FWD` (forwarding для LAN, VPN и tailnet), опциональный drop QUIC.
- Tailscale как третий вход: сервер работает как exit node, `tailscale0`
  получает ту же политику, что LAN и WDTT.
- Автообновление списков по systemd-таймеру.
- Веб-панель: статусы сервисов, пробы путей, редактор списков, перезапуски.
- zashboard для соединений и трафика.
- Опциональные tg-ws-proxy, WDTT-Plus и Tailscale.
- Опциональный доступ к опубликованным портам из WAN (`WAN_INGRESS`).
- Android-приложение «VPN Авто»: правила по доверенным Wi-Fi, автопереключение
  Tailscale и WDTT-Plus, управление exit node.

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

## Быстрая установка

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

Установка идемпотентна: повторный запуск не создаёт дублей правил firewall и не
перезаписывает чужие ключи `.env`. Если на шлюзе активен ufw, установщик
открывает доступ из LAN (только при приватном `LAN_CIDR`: правила
`ufw route allow in on LAN_IFACE` и `ufw allow from LAN_CIDR`) и `WG_SUBNET` к
`53/tcp+udp`, `7890/tcp`, `9090/tcp`, `PANEL_PORT/tcp` и `1443/tcp` (при
`INSTALL_TGWS=yes`), а также разрешает forwarding для LAN-интерфейса и
`wdtt0`. При публичном или пустом `LAN_CIDR` LAN-правила не добавляются —
сервисы остаются доступны через tailscale/WDTT/SSH-туннель, установщик
предупреждает. При `INSTALL_TAILSCALE=yes` или уже установленном Tailscale
добавляются правила для `tailscale0`, `100.64.0.0/10` и `41641/udp`, а
forwarding разрешён для tailnet. Независимо от ufw вход на сам шлюз ограничивает
цепочка `ROUTEWEAVE-IN` (прыжок в `INPUT 1`): политика `INPUT` не меняется,
но панель, API mihomo, DNS и прокси открыты только из доверенных зон.

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

## Параметры `.env`

Файл `/etc/routeweave/.env` создаётся с правами `0640 root:routeweave`:
читать его могут только root и сервисный пользователь панели. Значения
записываются в одинарных кавычках (`KEY='value'`), апостроф внутри значения —
`'\''`. Файл — данные, а не shell-скрипт: `install.sh`, апдейтер списков и
панель разбирают его построчно (`env_get`/shlex-подобный парсер), `source` и
`eval` в установщике и апдейтере не используются. Секреты генерирует
установщик; в консоль они не печатаются, кроме сгенерированного `PANEL_PASS`
в итоговой сводке — и только когда stdout является терминалом (TTY). При
`| tee`, редиректе в лог или запуске из CI пароль не печатается: в сводке
будет ссылка на `/etc/routeweave/.env` → `PANEL_PASS`. `MIHOMO_SECRET`,
`TG_SECRET` и `SOCKS_URL` остаются только в файле.

Повторная установка делает бэкап `$ETC/.env.bak.YYYYmmdd-HHMMSS`
(`0600 root:root`) и обновляет (upsert) все управляемые ключи значениями из
ответов и окружения; незнакомые ключи и комментарии сохраняются.

| Ключ | Назначение |
|---|---|
| `LAN_IFACE` | LAN-интерфейс сервера (единственная NIC) |
| `LAN_CIDR` | Приватная подсеть(и) LAN через пробел/запятую; пусто = автоопределение из `LAN_IFACE`. Доверяются только RFC1918 (`10/8`, `172.16/12`, `192.168/16`), публичный или нулевой CIDR игнорируется с предупреждением. Обязательное второе условие LAN-правил firewall |
| `SERVER_IP` | LAN-IP сервера, статичный или зарезервированный в DHCP |
| `SOCKS_URL` | Внешний прокси для geo: `socks5h://` (имя резолвит прокси), `socks5://`, `socks://`, `http://`; `https://` отклоняется — TLS к прокси не поддерживается; пусто = гео напрямую |
| `PANEL_BIND` | Адрес прослушивания панели; по умолчанию `SERVER_IP`, если он пуст — `127.0.0.1` |
| `PANEL_PORT` | Порт веб-панели, по умолчанию `8080` |
| `PANEL_ALLOWED_HOSTS` | Доп. hostname/IP через запятую для проверки заголовка `Host`. Пусто **не** означает «любые»: всегда разрешены `SERVER_IP`, `localhost`, `127.0.0.1`, `::1`; неизвестный `Host` получает `421` |
| `PANEL_RATE_LIMIT` | Лимит запросов `N/M` (N за M секунд), по умолчанию `120/60` |
| `PANEL_AUTH_RATE_LIMIT` | Отдельный лимит на неуспешный Basic-auth, по умолчанию `10/60`; в `.env` пишется вручную |
| `PANEL_TLS` | `yes`, если панель отдаётся по https через reverse-proxy: меняет только ожидаемую схему в сравнении `Origin`/`Referer` при CSRF-проверке. Встроенного TLS нет |
| `PANEL_USER` | Логин панели, по умолчанию `admin` |
| `PANEL_PASS` | Пароль панели; генерируется установщиком. Печатается только в терминал (stdout — TTY) и только если сгенерирован; в логах, пайпах и CI его нет — смотрите `/etc/routeweave/.env` |
| `MIHOMO_API` | Адрес API mihomo для панели и апдейтера; по умолчанию `http://MIHOMO_BIND:9090` (пустой `MIHOMO_BIND` = `SERVER_IP`, `0.0.0.0` заменяется на `127.0.0.1`); при смене `MIHOMO_BIND` обновите `MIHOMO_API` вручную — установщик не пересчитывает уже записанное значение |
| `MIHOMO_BIND` | Адрес external-controller и `bind-address` mihomo, по умолчанию `SERVER_IP`; `0.0.0.0` допустим только осознанно (установщик предупреждает) |
| `MIHOMO_ALLOW_LAN` | `no` по умолчанию: mixed-port `7890` не доступен из LAN; `yes` включает `allow-lan` |
| `MIHOMO_AUTO_REDIRECT` | `yes` по умолчанию: auto-redirect mihomo перехватывает локальный TCP (nft redirect в mixed-port). `no` — только для отладки: ломает локальный TCP/DNS (трафик шлюза зацикливается), WAN-доступу не нужен |
| `MIHOMO_TUN_MTU` | MTU TUN mihomo, по умолчанию `1280`; допустимо 576..9000 |
| `DNS_BIND` | Адрес DNS-листенера mihomo (порт 53), по умолчанию `0.0.0.0:53`: DNS нужен LAN, wdtt0 и Tailscale MagicDNS (форвардит на `127.0.0.1:53`). Явный IP ограничивает листенер и может сломать loopback-резолв; доступ из WAN закрывает firewall |
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

## Сторож маршрутизации и снимок состояния

`routeweave-guard.sh` запускается таймером `routeweave-guard.timer` раз в
60 секунд от root и проверяет инварианты, без которых LAN-клиенты теряют
интернет при живых сервисах:

- включён `net.ipv4.ip_forward`;
- есть nft-таблица `inet mihomo` с цепочками `prerouting` и `redirect`;
- в таблице маршрутизации `2022` есть `default` (TUN mihomo);
- в `mangle POSTROUTING` есть правило `NFQUEUE` на очередь `QNUM` из
  `/etc/routeweave/zapret.conf`;
- `nfqws` действительно читает эту очередь (по
  `/proc/net/netfilter/nfnetlink_queue`, когда `zapret-nfqws` активен);
- при `WAN_INGRESS=yes` на месте `ip rule` priority 8993.

Структурных проверок мало: 27.10 все они были зелёными, а новые
TCP-соединения 80/443 четыре минуты не открывались. Поэтому следом сторож
делает функциональные пробы (только когда `zapret-nfqws` активен):

- ICMP `ping -c1 -W2` до `1.1.1.1` — жива ли внешняя сеть вообще;
- `curl --max-time 8` до `https://ya.ru/` — открывается ли обычный сайт
  через очередь (успех — код 2xx/3xx);
- `curl --max-time 8` до `https://discord.com/` — работает ли десинк: домен
  есть в `dpi.txt` и без nfqws в РФ не открывается.

Сторож сам ходит в сеть с самого шлюза (в снимке состояния это видно как
раздел «Сторож: … check»). Адреса и таймауты меняются в необязательном файле
`/etc/routeweave/guard.env`, он читается юнитом через `EnvironmentFile` и
установщиком не создаётся (создавайте с правами `0640 root:root`):

```bash
# /etc/routeweave/guard.env
GUARD_PROBE=no                                  # выключить пробы совсем
GUARD_PROBE_PING_HOST=1.1.1.1
GUARD_PROBE_PLAIN_URL=https://ya.ru/
GUARD_PROBE_DPI_URL=https://discord.com/
GUARD_STUCK_TTL=21600                         # пауза после неэффективного рестарта, с
```

Поведение по результатам проб:

| ICMP | обычный сайт | десинк | что делает сторож |
|---|---|---|---|
| ок | ок | ок | отчёт «трафик через очередь и десинк проходят» |
| ок | ок | отказ | перезапускает `zapret-nfqws` — но только со второй неудачи подряд (счётчик `/run/routeweave/guard/probe-dpi.fail`, реакция ~2 мин) |
| ок | отказ | — | сразу перезапускает `zapret-nfqws`: сломан путь TCP 80/443 через очередь |
| отказ | отказ | — | ничего не делает: это недоступность внешней сети, а не шлюза; пишет `warn` и не увеличивает счётчик проблем |

Пробы пропускаются с пометкой «пропуск: сначала структурные проверки», если
структура сломана: тогда причина отсутствия трафика уже известна. Пробы
подчиняются тем же кулдаунам (`probe-plain`, `probe-dpi`) и предохранителю
четырёх действий в час. В режиме `check` сторож ничего не пишет и не читает
в `/run/routeweave/guard` — только отчёт.

Пропавшее сторож возвращает: включает `ip_forward`, перезапускает mihomo,
поднимает правило NFQUEUE через `fw-nfq.sh start`, перезапускает
`zapret-nfqws`, возвращает правило WAN через `fw-wan.sh start`. Упавший
`zapret-nfqws` (состояние `failed`, например после исчерпания лимита
перезапусков) сторож тоже перезапускает, а выключенный вручную (`inactive`)
не трогает: пропуск виден в журнале строкой «пропуск». Починка
ограничена: кулдаун 600 секунд на каждый вид действия и не больше четырёх
действий за час — при исчерпанном предохранителе сторож только пишет в
журнал, что пропало и что починка остановлена (иначе неустранимая поломка
дала бы шторм рестартов). Если рестарт `zapret-nfqws` не восстановил трафик,
значит причина не в nfqws: повторный рестарт того же вида откладывается на
6 часов (`GUARD_STUCK_TTL`, маркеры `/run/routeweave/guard/probe-*.stuck`),
чтобы не рвать соединения клиентам и не засорять журнал каждые 10 минут.
Успешная проба снимает паузу досрочно. Состояние сторожа — в
`/run/routeweave/guard`.

Проверка без изменений, журнал и отключение:

```bash
sudo /usr/local/sbin/routeweave-guard.sh check
journalctl -u routeweave-guard -n 50
sudo systemctl disable --now routeweave-guard.timer
```

Снимок состояния снимается из панели кнопкой «Снять дамп состояния»,
последний снимок открывает «Показать последний». Файлы лежат в
`/var/log/routeweave`: `dump-ДАТА-ВРЕМЯ.txt` и `last-dump.txt` (обновляется
при каждом снятии), права `0640 root:routeweave` — пишет root-helper, читает
панель как группа `routeweave`. В снимок попадают маршруты, правила
firewall, счётчики и журнал за 10 минут, а также вывод
`routeweave-guard.sh check` (с пробами трафика) и последние 30 строк журнала
сторожа; `config.yaml` и `.env` в него НЕ включаются, секретов там нет.
Ручной запуск:

```bash
sudo /usr/local/sbin/routeweave-diag-dump.sh
```

Сторож панели не нужен и в sudo-allowlist не входит: его запускает только
systemd. Снимок, наоборот, вызывается панелью через `sudo -n` по allowlist
без wildcard.

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

Если WDTT-клиенты используют DNS шлюза, менять `DNS_BIND` не нужно: по
умолчанию DNS слушает `0.0.0.0:53` и покрывает `wdtt0`. `10.66.66.1` (адрес
шлюза в `WG_SUBNET`) задавайте, только если хотите ограничить листенер
этим адресом.

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
Порты те же, что у LAN.

**Сначала о bind-адресах.** `DNS_BIND` по умолчанию `0.0.0.0:53`, поэтому DNS
по `100.x.y.z` уже отвечает (доступ ограничивает `ROUTEWEAVE-IN`); менять его
для tailnet не нужно. А `PANEL_BIND` и `MIHOMO_BIND` по умолчанию равны
`SERVER_IP` (LAN-адресу), поэтому по `100.x.y.z` панель и API **не** отвечают,
даже когда firewall их разрешает. Чтобы доступ по tailscale-адресу заработал,
задайте в `/etc/routeweave/.env`:

- `PANEL_BIND`, `MIHOMO_BIND` — tailscale-адрес шлюза
  (`100.x.y.z`, безопаснее: сервис не слушает другие интерфейсы) либо
  `0.0.0.0` (осознанный opt-in: все интерфейсы, границей остаётся firewall);
- `PANEL_ALLOWED_HOSTS` — добавьте tailscale-IP и/или MagicDNS-имя, иначе
  панель ответит `421` на незнакомый `Host`;
- `MIHOMO_API` — при смене `MIHOMO_BIND` на tailscale-адрес укажите
  `http://100.x.y.z:9090`: установщик уже записанное значение не пересчитывает;
- `MIHOMO_ALLOW_LAN=yes` — без него mixed-port `7890` слушает localhost и
  по tailscale-адресу недоступен.

Вход на шлюз при этом ограничивает цепочка `ROUTEWEAVE-IN`:
порты открыты из `tailscale0`, `100.64.0.0/10`, `LAN_IFACE` с приватным
`LAN_CIDR`, `wdtt0` и `WG_SUBNET`. Если `LAN_CIDR` пуст или публичный,
LAN-правила не добавляются, и сервисы остаются доступны только через
tailscale, WDTT или SSH-туннель (`doctor` даёт WARN, а не FAIL). При активном
ufw `install.sh` дополнительно добавляет правила для `tailscale0`,
`100.64.0.0/10` и `41641/udp`, если Tailscale установлен, в том числе только
что. Если Tailscale поставлен позже, повторите `sudo bash install.sh` —
правила добавятся; повторный запуск идемпотентен.

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

## Опционально: Android-приложение

В каталоге [`android/`](android/) — приложение «VPN Авто» для телефона. Правила:
доверенная Wi-Fi → VPN выключен, незнакомая Wi-Fi → Tailscale + exit node,
мобильная сеть → WDTT-Plus. Есть ручные команды для обоих VPN, список доверенных
сетей и watchdog exit node. Готовый APK — в
[Releases](https://github.com/Maxer1151/routeweave/releases), сборка и
настройка — в [`android/README.md`](android/README.md).

## Порты

| Порт | Протокол | Назначение | Откуда доступен |
|---|---|---|---|
| 53 | TCP + UDP | DNS mihomo (fake-ip); слушает все интерфейсы (`DNS_BIND`, по умолчанию `0.0.0.0:53`), доступ ограничивает firewall | LAN (`LAN_IFACE`+`LAN_CIDR`), tailnet, WDTT |
| 8080 | TCP | Веб-панель (`PANEL_PORT`, слушает `PANEL_BIND`) | LAN (`LAN_IFACE`+`LAN_CIDR`), tailnet, WDTT |
| 9090 | TCP | mihomo API и zashboard (`MIHOMO_BIND`) | LAN (`LAN_IFACE`+`LAN_CIDR`), tailnet, WDTT |
| 7890 | TCP | mixed-proxy mihomo; из LAN только при `MIHOMO_ALLOW_LAN=yes` | LAN/tailnet/WDTT при `MIHOMO_ALLOW_LAN=yes` |
| 1443 | TCP | tg-ws-proxy (опц.), публикуется на `SERVER_IP` | LAN/tailnet/WDTT; периметр — Docker/ufw |
| 56000 | UDP | WDTT-Plus DTLS (опц.) | WAN (проброс на роутере), клиенты WDTT |
| 41641 | UDP | Tailscale, прямые соединения (опц.) | WAN/tailnet |

Порты `53`, `8080`, `9090`, `7890` и `1443` — только LAN/tailnet/WDTT, наружу
их пробрасывать нельзя. LAN-разрешение — не «весь интерфейс», а правило с
двумя условиями: `-i LAN_IFACE` **и** `-s LAN_CIDR`, где `LAN_CIDR` — только
RFC1918. Поэтому на VPS с публичным NIC или при пробросе порта на роутере
вход из WAN закрыт: публичный/нулевой `LAN_CIDR` игнорируется, LAN-правила не
добавляются, а сервисы остаются доступны через tailscale/WDTT или
SSH-туннель (`doctor` даёт WARN). Вход на `53`, `8080`, `9090` и `7890`
дополнительно ограничен цепочкой `ROUTEWEAVE-IN`; `1443` публикует Docker на
`SERVER_IP`, и его периметр держат правила Docker/ufw, а не `ROUTEWEAVE-IN`
(см. раздел про tg-ws-proxy). `56000/udp` при использовании WDTT-Plus,
наоборот, пробрасывается снаружи, иначе телефон не подключится.
`41641/udp` нужен только для прямых соединений Tailscale: если шлюз за
роутером, пробросьте порт на роутере, иначе соединения пойдут через
DERP-релей.

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
а не от `SERVER_IP`) уходит не в TUN, а также mihomo: предупреждает, если в
`config.yaml` выключен `auto-redirect` или если при заданном `SOCKS_URL`
селектор `GEO` стоит на `DIRECT` — выбор группы хранится в `cache.db`
(`profile.store-selected: true`), а не в конфиге, и `geo.txt` в этом случае
идёт напрямую мимо апстрима. Проверка селектора выполняется и при
`WAN_INGRESS=no`.

`auto-redirect` включён штатно и WAN-доступу не мешает: nft-таблица mihomo
исключает из redirect адреса из `inet4_local_address_set` (локальные и
LAN-адреса), поэтому входящий на `SERVER_IP` трафик не перехватывается, а
ответы помеченных пакетов уходят в `main` по `ip rule` 8993. Выключение
`auto-redirect` ломает локальный TCP (трафик шлюза заворачивается в TUN и
зацикливается) и DNS на loopback. Для отладки это можно сделать через
`MIHOMO_AUTO_REDIRECT=no`, но на рабочей установке не нужно. `config.yaml`
генерируется установщиком из `.env`, поэтому поведение меняется через `.env`,
а не ручной правкой файла.

Доступ из WAN не отменяет DNAT/docker-правила и не заменяет TLS или
reverse-proxy: открывайте только те порты, которые действительно должны быть
видны снаружи, и не публикуйте панель, API mihomo и прокси.

## Безопасность

- Панель рассчитана на доверенную LAN. Не выставляйте `8080`, `9090`, `1443`,
  `7890` и `53` в интернет; для удалённого доступа — VPN (в том числе tailnet)
  или reverse-proxy с TLS (Caddy/nginx) на отдельном хосте.
- `/etc/routeweave/.env` — файл с секретами. Он создаётся с правами
  `0640 root:routeweave`; при повторной установке рядом появляется бэкап
  `.env.bak.YYYYmmdd-HHMMSS` с `0600 root:root`. Не публикуйте файл, логи и
  дампы и не коммитьте их.
- Панель работает от пользователя `routeweave`; root-доступ — только через
  `sudo -n` по allowlist без wildcard.
- Изменяющие запросы защищены fail-closed (CSRF), неизвестный `Host` получает
  `421`, превышение лимита — `429`, список больше 200 КБ — `413`.
- Вход на шлюз ограничивает цепочка `ROUTEWEAVE-IN`, а forwarding —
  `ROUTEWEAVE-FWD`; политика `INPUT` не меняется, `QUIC_DROP` стоит выше
  разрешающих правил.
- LAN-доступ открывается только при приватном `LAN_CIDR`. Публичный или пустой
  CIDR — LAN-правила не добавляются (сервисы остаются в tailnet/WDTT/через
  SSH-туннель).
- IPv6 не фильтруется: проект IPv4-only, `ip6tables` не трогается. IPv6 в LAN
  нужно отключать в роутере.
- Версии mihomo, zashboard, WDTT и их sha256 зашиты в git; zapret ставится из
  закреплённого коммита, образ tgws — по digest. `--allow-unverified` — только
  для отладки.
- Установщик печатает `PANEL_PASS` только один раз и только в терминал (TTY);
  `MIHOMO_SECRET`, `TG_SECRET` и `SOCKS_URL` не печатаются, панель `.env` не
  отдаёт.
- tg-ws-proxy запускается с `read_only`, `cap_drop: ALL`, `no-new-privileges` и
  лимитами; правила Docker в `DOCKER-USER` Routeweave не управляет.

Полная модель угроз, инварианты и порядок ответственного раскрытия —
[`SECURITY.md`](SECURITY.md). Правила для разработчиков — [`AGENTS.md`](AGENTS.md).

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

## FAQ

**Нужен ли отдельный роутер или машина?**
Нужна отдельная машина (или VM) с Ubuntu 22.04+/Debian 12+ и одной NIC за
домашним роутером: она становится шлюзом и DNS для клиентов. Сам роутер
остаётся на месте, в нём настраивается DHCP с выдачей `SERVER_IP`.

**Почему YouTube не работает?**
Проверьте, что в `/etc/routeweave/zapret.conf` в `EXTRA` есть
`--dpi-desync-fwmark=0x80000`. Без этой метки фейковые пакеты nfqws уходят в
TUN mihomo и съедаются. Если метка на месте, подберите стратегию через
`sudo /opt/zapret/blockcheck.sh` и перезапустите `zapret-nfqws`.

**Почему панель не открывается из LAN?**
Три частые причины: `LAN_CIDR` пуст или публичный (LAN-правила не
добавляются — смотрите `fw-base.sh doctor`), `PANEL_BIND` указывает не на тот
адрес (по умолчанию `SERVER_IP`), либо запрос не проходит проверки самой
панели: `421` — неизвестный `Host` (добавьте его в `PANEL_ALLOWED_HOSTS`),
`429` — сработал лимит, `403` — не пройдена CSRF-проверка.

**Работает ли IPv6?**
Нет. Firewall фильтрует только IPv4, `ip6tables` не трогается. IPv6 в LAN
нужно отключать в роутере, иначе устройства уйдут мимо шлюза.

**Что будет с секретами при удалении?**
По умолчанию `uninstall.sh` сохраняет `/etc/routeweave` с `.env`: в
интерактивном режиме спросит подтверждение, в неинтерактивном — напомнит о
ротации секретов. `--purge` удаляет каталог целиком. Если секреты нужно
сохранить — сделайте копию `.env` заранее.

**Как обновить Routeweave?**
`git pull`, затем `sudo bash install.sh`. Установщик обновит управляемые ключи
`.env` (с бэкапом), перегенерирует `config.yaml`, обновит юниты и перезапустит
ядро; списки и `zapret.conf` сохранятся.

**Можно ли поставить на VPS?**
Технически да, но шлюз рассчитан на LAN за роутером. На VPS `SERVER_IP` —
публичный адрес, поэтому LAN-правила не добавляются: панель, DNS и прокси
останутся доступны только через tailscale/WDTT/SSH-туннель. Для WAN-доступа к
опубликованным портам есть `WAN_INGRESS`, но панель, API mihomo и прокси
публиковать нельзя.

**Чем отличается `TS_EXIT_MODE=nat` от `policy`?**
`policy` (по умолчанию) проводит трафик tailnet через TUN mihomo: работают
geo/SOCKS, DPI и fake-ip DNS. `nat` делает прямой MASQUERADE в WAN — быстрее и
без userspace-стека, но без geo/SOCKS/DPI-политики, источник в LAN
маскарадится, а DNS через fake-ip не применяется.

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

## Для разработчиков

Документация для контрибьюторов — [`AGENTS.md`](AGENTS.md): карта
репозитория, устройство шлюза, инварианты безопасности, тесты, CI и стиль.
Модель угроз — [`SECURITY.md`](SECURITY.md).

```bash
make lint    # bash -n, shellcheck, ruff, compileall
make test    # pytest tests -v
make all     # lint + test
```

Зависимости ставьте в venv: `pip install -r panel/requirements.txt pytest
pyyaml`. Привилегированные netns-тесты помечены `privileged` и без root
корректно пропускаются; полный прогон с ними требует root (см. `AGENTS.md`).

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
