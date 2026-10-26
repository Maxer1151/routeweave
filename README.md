# Routeweave

Домашний шлюз с раздельной маршрутизацией: mihomo + zapret + списки доменов + веб-панель.

## Что это

Routeweave — прозрачный шлюз на Ubuntu/Debian, который ставится на отдельную машину за домашним роутером. Клиенты получают его как шлюз и DNS, а mihomo разводит трафик по доменам: часть доменов проходит через DPI-десинк zapret, гео-блокировки уходят через внешний SOCKS/HTTP-прокси, российские сервисы и всё остальное идут напрямую. Необязательные компоненты — Telegram-прокси (tg-ws-proxy), WDTT-Plus для входа с телефона и Tailscale (exit node).

## Возможности

- Маршрутизация по доменам: отдельные списки для geo (SOCKS), dpi (zapret) и RU-направления.
- fake-ip DNS на порту 53 и правила mihomo на файловых rule-provider'ах.
- Firewall: отдельная цепочка `ROUTEWEAVE-FWD`, forwarding для LAN и VPN,
  опциональный drop QUIC выше разрешающих правил.
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
  перезапуски; Basic-авторизация.
- **Firewall** — `fw-base.sh` / `fw-wan.sh` / `fw-nfq.sh` / `fw-tailscale.sh`,
  systemd-юниты, policy-routing для WDTT и Tailscale.

Опционально:

- **tg-ws-proxy-rs** — Telegram MTProto-прокси в Docker, порт `1443`.
- **WDTT-Plus** — сервер WDTT в systemd (`wdtt`), только amd64: DTLS
  `56000/udp`, внутренний WireGuard `56001/udp`, интерфейс `wdtt0`.
- **Tailscale** — опциональный exit node: ставится установщиком при
  `INSTALL_TAILSCALE=yes` (или вручную), интерфейс `tailscale0`.

## Требования

- Ubuntu 20.04+ или Debian 12+, архитектура amd64/arm64, ≥1 ГБ RAM, ≥3 ГБ диска.
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
выключены. Установщик
не перезаписывает существующие `.env` и live-списки. Повторный запуск обновляет
mihomo до свежего релиза (если версия отличается), юниты и конфиги, но сохраняет
`.env` и списки.

Если на шлюзе активен ufw, установщик открывает доступ из LAN и `WG_SUBNET`
к `53/tcp+udp`, `7890/tcp`, `9090/tcp`, `PANEL_PORT/tcp` и `1443/tcp` (при
`INSTALL_TGWS=yes`), а также разрешает forwarding для LAN-интерфейса и
`wdtt0`. При `INSTALL_TAILSCALE=yes` или уже установленном Tailscale
добавляются правила для `tailscale0`, `100.64.0.0/10` и `41641/udp`, а
forwarding разрешён для tailnet. Правила добавляются идемпотентно, повторный
запуск дублей не создаёт.

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
| `TG_PINNED_UPSTREAM` | Порядок апстримов tg-ws-proxy (`--pinned-upstream`), тиры через запятую: `ws`, `cfworker`, `cfproxy`, `mtproto`, `tcp`. Пишет установщик: по умолчанию `cfworker,ws,tcp`, если задан домен CF Worker; переопределяется одноимённой переменной окружения при установке (`TG_PINNED_UPSTREAM=... ./install.sh`); пусто = порядок прокси по умолчанию |
| `TG_LINK_IP` | IP/хост для `tg://`-ссылки в панели; пусто = `SERVER_IP` |
| `WG_SUBNET` | Подсеть клиентов WDTT (`wdtt0`), по умолчанию `10.66.66.0/24`; policy-routing — `ip rule` priority 8990 |
| `TS_IFACE` | Интерфейс Tailscale, по умолчанию `tailscale0`; установщик не пишет, можно добавить вручную |
| `TS_SUBNET` | Подсеть tailnet, по умолчанию `100.64.0.0/10`; установщик не пишет, можно добавить вручную |
| `WAN_INGRESS` | `yes` — включить доступ из WAN к опубликованным портам; по умолчанию `no` |
| `WAN_PORTS` | Порты для WAN-доступа: `8081`, `8081/tcp`, `51820/udp`, диапазоны `8000:8100/tcp` / `8000-8100/tcp`; голое число = tcp |
| `WAN_INGRESS_MARK` | Метка connmark для WAN-доступа, по умолчанию `0x40000`; не должна пересекаться с метками mihomo/docker; при Tailscale задайте `0x200000` |
| `WAN_INGRESS_RULE_PRIO` | Приоритет `ip rule` для помеченных пакетов, по умолчанию `8993`; должен быть меньше 9000 |
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
| `INSTALL_WDTT` | `yes`/`no` — ставить WDTT-Plus; по умолчанию `no`, `--defaults` — `no` |
| `INSTALL_TGWS` | `yes`/`no` — ставить tg-ws-proxy; по умолчанию `no`, `--defaults` — `no` |
| `INSTALL_TAILSCALE` | `yes`/`no` — поставить Tailscale и настроить exit node; по умолчанию `no`, `--defaults` — `no` |
| `TS_AUTHKEY_FILE` | файл с ключом авторизации Tailscale на время установки; предпочтительнее `TS_AUTHKEY`; в `.env` не пишется |
| `ZAPRET_REF` | Ветка/тег zapret при клонировании и обновлении, по умолчанию `master`; фактический коммит пишется в `/etc/routeweave/zapret.commit` |
| `MIHOMO_SHA256` | Ожидаемая sha256 архива mihomo, если апстрим не публикует `<url>.sha256` |
| `ZASHBOARD_SHA256` | То же для `dist.zip` zashboard |

`INSTALL_*` читаются из окружения или запрашиваются интерактивно; при
`--defaults` вопросы не задаются. Ключ Tailscale (`TS_AUTHKEY_FILE` или
`TS_AUTHKEY`) действует только на время установки и не сохраняется в
`/etc/routeweave/.env`.

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
6. **Tailscale (если ставили).** Одобрите шлюз как exit node в admin console
   (**Machines** → **Edit exit node** → **Use as exit node**) и настройте DNS
   tailnet: **Use with exit node** и nameserver `100.x.y.z` (см. раздел
   «Опционально: Tailscale»).

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
по `http://SERVER_IP:9090/ui/` и работает под отдельной CSP, разрешающей
запросы к API mihomo на другом порту.

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
обычно. Сервер работает как exit node, а `tailscale0` — третий вход шлюза:
трафик клиентов уходит в TUN mihomo и получает ту же политику, что LAN и WDTT
(geo → SOCKS, dpi → zapret, RU → direct). Прямого NAT-режима нет.

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
mihomo и `WAN_INGRESS_MARK` (тоже `0x40000`).

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

Сервисы по tailscale-адресу: панель `http://100.x.y.z:PANEL_PORT`, zashboard
`http://100.x.y.z:9090/ui/`, DNS `100.x.y.z:53`, mixed-proxy
`100.x.y.z:7890`. Порты те же, что у LAN; при активном ufw их открывает
`install.sh`: правила для `tailscale0`, `100.64.0.0/10` и `41641/udp`
добавляются, если Tailscale установлен, в том числе только что. Если
Tailscale поставлен позже, повторите `sudo bash install.sh` — правила ufw
добавятся; повторный запуск идемпотентен.

После обновления Routeweave достаточно повторить `sudo bash install.sh` и
`sudo systemctl restart routeweave-firewall`: пакет не переустанавливается,
состояние Tailscale сохраняется.

Диагностика:

```bash
sudo /etc/routeweave/fw-tailscale.sh doctor
tailscale status
journalctl -u tailscaled -n 50
```

Ограничения: policy-режим через userspace-стек mihomo (ниже пропускная
способность, выше нагрузка на CPU); IPv6 не поддерживается; `QUIC_DROP=yes`
режет UDP/443 и у tailnet-клиентов; fake-ip `198.18.0.0/16` может
конфликтовать с другим прокси на клиенте. При `WAN_INGRESS=yes` метка по
умолчанию `0x40000` совпадает с меткой Tailscale — задайте
`WAN_INGRESS_MARK="0x200000"`.

Подробности, DNS и ограничения — в
[`docs/tailscale-exit-node.md`](docs/tailscale-exit-node.md).

## Порты

| Порт | Протокол | Назначение |
|---|---|---|
| 53 | TCP + UDP | DNS mihomo (fake-ip) |
| 8080 | TCP | Веб-панель (`PANEL_PORT`) |
| 9090 | TCP | mihomo API и zashboard |
| 7890 | TCP | mixed-proxy mihomo (`allow-lan`) |
| 1443 | TCP | tg-ws-proxy (опц.) |
| 56000 | UDP | WDTT-Plus DTLS (опц.) |
| 41641 | UDP | Tailscale, прямые соединения (опц.) |

Порты `53`, `8080`, `9090`, `7890` и `1443` — только LAN/WG/tailnet, наружу их
пробрасывать нельзя. `56000/udp` при использовании WDTT-Plus, наоборот,
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
указанные порты получает connmark, на всех пакетах соединения (включая ответы
сервиса) метка восстанавливается, а `ip rule` с приоритетом `8993` уводит
помеченные пакеты в `main` раньше правил mihomo `9001`/`9002`.

Включение:

```bash
sudo sed -i 's/^WAN_INGRESS=.*/WAN_INGRESS="yes"/' /etc/routeweave/.env
sudo sed -i 's/^WAN_PORTS=.*/WAN_PORTS="8081 51820\/udp"/' /etc/routeweave/.env
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

Выключение — `WAN_INGRESS="no"` и `sudo systemctl restart routeweave-firewall`:
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
`2022`, что `ip route get 1.1.1.1 mark 0x40000` уходит не в TUN, а также
предупреждает о `redirect` в nftables и `auto-redirect: true` в
`config.yaml`. При `auto-redirect` mihomo перехватывает входящий TCP ещё до
маркировки; если WAN-доступ не работает, поставьте `auto-redirect: false` и
перезапустите mihomo.

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
- `.env` создаётся с правами `0640 root:routeweave`. Контейнер tg-ws-proxy
  получает только переменные из своего `.env.tgws` и не видит секретов
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
- Tailnet — отдельная граница доверия: доступ к панели и API mihomo по
  tailscale-адресу регулируется ACL/grants и сроком действия ключей. Это не
  публикация в интернет.
- CGNAT-спуфинг с `100.64.0.0/10` не с `tailscale0` дропается.
- `41641/udp` открыт осознанно: только для прямых соединений Tailscale.
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
  панель не стартует.
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

Повторный `sudo bash install.sh` обновляет mihomo при новой версии, юниты и
конфиги; `.env` и списки сохраняются. `zapret.conf` **сохраняется**: при
изменении `LAN_IFACE` обновляется только строка `WAN_IFACE=`, а перед
правкой делается бэкап `zapret.conf.bak-<timestamp>`. `config.yaml`
регенерируется из `.env`, но прежняя версия также сохраняется в бэкап.

`sudo bash uninstall.sh` останавливает и удаляет юниты, панель, zapret,
mihomo, таймер и апдейтер списков; контейнер tg-ws-proxy также останавливается
и удаляется. Каталог `/etc/routeweave` остаётся. WDTT-Plus не затрагивается.
`uninstall.sh` не удаляет пакет Tailscale и состояние `/var/lib/tailscale`;
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
