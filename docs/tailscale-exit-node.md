# Tailscale как exit node

> **Опционально:** установщик Routeweave может поставить Tailscale
> (`INSTALL_TAILSCALE=yes`), либо вы вручную. Без него шлюз работает как
> обычно.

Сервер Routeweave может быть exit node для tailnet: клиенты отправляют весь
интернет-трафик на шлюз, а он разводит его по тем же правилам, что LAN и WDTT.
Режимов два: `policy` (по умолчанию) — трафик проходит через mihomo TUN и
получает geo/DPI-политику; `nat` — прямой MASQUERADE в WAN без mihomo
(быстрее, но без политики).

## 1. Как это работает

- Клиент Tailscale выбирает шлюз как exit node и отправляет трафик в tailnet.
  Пакеты приходят на интерфейс `tailscale0`.
- `ROUTEWEAVE-FWD` разрешает forwarding `tailscale0 → Meta` (TUN mihomo).
  Дальше работает обычная политика: geo → SOCKS, dpi → zapret, RU → direct.
- Ответный трафик из `Meta` в `tailscale0` разрешён правилом
  `-i Meta -o tailscale0` для `RELATED,ESTABLISHED`.
- Адреса tailnet `100.64.0.0/10` уходят в таблицу `main` по правилу `ip rule`
  priority 8991. Иначе policy-routing mihomo (таблица 2022, приоритеты
  9001/9002) завернул бы их в TUN. Это нужно и для доступа к панели, DNS и
  прокси по tailscale-адресу, и для работы самого `tailscaled`.
- В `ROUTEWEAVE-FWD` также стоят симметричные ACCEPT `tailscale0 → tailscale0`
  и `LAN → tailscale0`, поэтому трафик между tailnet-клиентами и из LAN в
  tailnet не режется.
- Пакеты с источником из `100.64.0.0/10`, пришедшие не с `tailscale0`,
  дропаются в `ROUTEWEAVE-FWD`: это CGNAT-спуфинг.

Порядок приоритетов `ip rule`: 8990 — WDTT, 8991 — tailnet, 8992 — NAT-режим
tailnet, 8993 — WAN, 9001/9002 — mihomo.

### Режим exit node: policy и nat

Режим задаётся `TS_EXIT_MODE` в `/etc/routeweave/.env`; по умолчанию `policy`.
Оба режима сохраняют `ip rule` 8991 и CGNAT-спуф-гард, а правила policy-режима
в NAT остаются на месте: они безвредны.

`policy` — описан выше: `tailscale0 → Meta`, таблица 2022, политика mihomo.

`nat` — трафик из `tailscale0`:

- метится в mangle `PREROUTING` цепочкой `ROUTEWEAVE-TSMARK`:
  `-i tailscale0 -j MARK --set-xmark 0x100000/0x100000`;
- по правилу `ip rule ... fwmark 0x100000/0x100000 lookup main priority 8992`
  уходит в `main`, то есть в WAN, минуя таблицу 2022;
- маскарадится в nat `POSTROUTING` цепочкой `ROUTEWEAVE-TSNAT`:
  `-s 100.64.0.0/10 -o LAN_IFACE -m mark --mark 0x100000/0x100000 -j MASQUERADE`.

Что это значит:

- geo/SOCKS/DPI-политика mihomo для tailnet не применяется: трафик идёт
  напрямую. zapret на WAN-egress продолжает работать — он стоит на исходящем
  интерфейсе, а не в TUN;
- tailnet → LAN тоже маскарадится: LAN видит адрес шлюза, а не `100.x`
  клиента. Это осознанно — обратный маршрут к CGNAT из LAN всё равно был бы
  битым;
- нужен корректный `LAN_IFACE`: если `LAN_IFACE`, `TS_IFACE` или `TS_SUBNET`
  пусты, firewall не падает, пишет `warn` и работает как `policy`.

Почему такие метка и приоритет: `0x100000` свободна (`0x40000` — tailscale и
`WAN_INGRESS`, `0x80000` — zapret, `0x200000` рекомендуют для `WAN_INGRESS` при
Tailscale), `8992` свободен (`8990` — WDTT, `8991` — tailnet, `8993` — WAN,
`9001/9002` — mihomo).

## 2. Требования и что не нужно делать

Нужно:

- Tailscale установлен и узел авторизован: `tailscale status` показывает сервер
  и его адрес `100.x.y.z`. Пакет можно поставить установщиком
  (`INSTALL_TAILSCALE=yes`) или вручную.
- Для установки установщиком: Ubuntu/Debian, доступ к официальному
  apt-репозиторию Tailscale и интернет.
- Версия Tailscale поддерживает exit node (с 1.20) и команду `tailscale set`;
  на старых версиях используйте `tailscale up` с теми же флагами.
- `net.ipv4.ip_forward=1`. Routeweave выставляет его при старте firewall;
  проверка — `sysctl net.ipv4.ip_forward`.

Не нужно:

- Переустанавливать Tailscale и заново логиниться. Состояние узла лежит в
  `/var/lib/tailscale` и переживает обновление Routeweave. Установщик не
  переустанавливает уже установленный пакет и не логинит авторизованный узел
  (`BackendState=Running`) — только применяет `tailscale set`.
- Добавлять ключ авторизации в `/etc/routeweave/.env`: он там не хранится.
  `TS_IFACE` и `TS_SUBNET` по умолчанию установщик в `.env` не пишет, но при
  необходимости их можно добавить вручную: `tailscale0` и `100.64.0.0/10`.
- Запускать `tailscale logout` или менять аккаунт.
- Настраивать NAT/MASQUERADE для `tailscale0` вручную.
- Хранить ключ авторизации: для `tailscale set --advertise-exit-node` и
  `--netfilter-mode=nodivert` он не нужен — узел уже авторизован. При
  повторном входе ключ передаётся разово в `sudo tailscale up --auth-key=...`
  и не хранится в файлах проекта, включая `/etc/routeweave/.env`.

Что делает установщик при `INSTALL_TAILSCALE=yes`:

- ставит Tailscale из официального apt-репозитория Ubuntu/Debian, включает
  `tailscaled` и применяет
  `--advertise-exit-node --netfilter-mode=nodivert --accept-dns=false`;
- если пакет уже установлен, не переустанавливает его; если узел авторизован,
  применяет `tailscale set` с теми же флагами;
- ключ авторизации (`TS_AUTHKEY_FILE` предпочтительно, либо `TS_AUTHKEY`)
  использует только на время установки: временный файл `0600` удаляется, в
  `/etc/routeweave/.env` ключ не пишется;
- если узел не авторизован и ключа нет, не зависает: включает `tailscaled` и
  печатает команду `sudo tailscale up ...` для ручного входа.

Если Tailscale уже установлен, установщик добавляет firewall-правила, а при
активном ufw — разрешения для tailnet, независимо от `INSTALL_TAILSCALE`.

## 3. Настройка сервера

### Вариант 1 — установщик

```bash
sudo env INSTALL_TAILSCALE=yes bash install.sh
# ключ авторизации на время установки:
sudo env INSTALL_TAILSCALE=yes TS_AUTHKEY_FILE=/root/ts-authkey bash install.sh
```

Установщик ставит пакет из официального apt-репозитория, включает
`tailscaled` и применяет флаги ниже. Если узел не авторизован и ключа нет, он
печатает команду для ручного `tailscale up`. `--defaults` соответствует
`INSTALL_TAILSCALE=no`.

### Вариант 2 — уже авторизованный узел

```bash
sudo tailscale set --advertise-exit-node --netfilter-mode=nodivert
sudo tailscale set --accept-dns=false
```

### Вариант 3 — новый узел, который ещё не авторизован

```bash
sudo tailscale up --advertise-exit-node --netfilter-mode=nodivert --accept-dns=false
```

`--accept-dns=false` — рекомендация для самого шлюза: его DNS обслуживает
mihomo, и Tailscale не должен переписывать `/etc/resolv.conf`. Клиентские
устройства, наоборот, должны принимать DNS из tailnet (см. раздел 6).

Проверка:

```bash
tailscale status
tailscale ip -4
sysctl net.ipv4.ip_forward
```

### Почему nodivert

По умолчанию Tailscale работает с `--netfilter-mode=on`: создаёт цепочки
`ts-input`, `ts-forward`, `ts-postrouting` и сам вставляет в них прыжки. В
`ts-postrouting` есть MASQUERADE, а свои пакеты Tailscale помечает меткой
`0x40000`. На шлюзе это конфликтует с двумя вещами:

- policy-routing mihomo: трафик должен уходить в TUN и получать политику, а не
  маскарадиться и идти напрямую;
- `WAN_INGRESS_MARK`, у которого метка по умолчанию тоже `0x40000`.

С `--netfilter-mode=nodivert` Tailscale создаёт свои цепочки, но не вызывает
их: `ROUTEWEAVE-FWD` остаётся единственным хозяином forwarding. Поэтому
Routeweave сам разрешает forwarding в TUN и обратный путь и закрывает
CGNAT-спуфинг, а установщик открывает INPUT для нужных портов (если ufw
активен).

Проверить, что прыжков нет:

```bash
sudo iptables-save | grep -E -- '-j (ts-input|ts-forward|ts-postrouting)' || echo "прыжков нет"
```

Если в выводе есть `-j ts-...`, Tailscale всё ещё в режиме `on`: повторите
`--netfilter-mode=nodivert` и перезапустите `tailscaled`:

```bash
sudo systemctl restart tailscaled
```

Подробнее о режимах — в документации Tailscale:
[Netfilter modes](https://tailscale.com/docs/reference/netfilter-modes).

### Переключение режима exit node

`TS_EXIT_MODE` установщик в `.env` не пишет — как `TS_IFACE` и `TS_SUBNET`,
значение задаётся вручную. Включить прямой NAT:

```bash
sudo sed -i 's/^TS_EXIT_MODE=.*/TS_EXIT_MODE="nat"/' /etc/routeweave/.env
# если строки ещё нет:
echo 'TS_EXIT_MODE="nat"' | sudo tee -a /etc/routeweave/.env
sudo systemctl restart routeweave-firewall
sudo /etc/routeweave/fw-tailscale.sh doctor
sudo /etc/routeweave/fw-tailscale.sh status
```

`start` идемпотентен: он сам снимает артефакты прежнего nat-режима (прыжки,
цепочки `ROUTEWEAVE-TSMARK`/`ROUTEWEAVE-TSNAT`, правило 8992), поэтому
переключение в обе стороны не оставляет дублей. Откат — вернуть `policy` и
перезапустить юнит (см. раздел 11).

Если `LAN_IFACE` (или `TS_IFACE`/`TS_SUBNET`) пуст, режим `nat` безопасно
откатывается в `policy`: в логах будет `warn`, firewall продолжит работать.

## 4. Одобрение exit node и ACL

Одного `--advertise-exit-node` мало: узел должен быть одобрен в admin console.

1. Откройте **Machines** в
   [admin console](https://console.tailscale.com/admin/machines).
2. Найдите сервер, у которого в списке есть значок **Exit Node**.
3. Меню узла → **Edit exit node** → **Use as exit node**.

Пока exit node не одобрен, клиенты его не видят.

Проверить одобрение на сервере (после шага 3 должно быть `True`):

```bash
sudo tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["ExitNodeOption"])'
```

По умолчанию в tailnet разрешено всё, и отдельный grant не нужен. Если ACL
кастомизированы, добавьте grant на `autogroup:internet` — именно он разрешает
использовать узел как интернет-шлюз:

```json
{
  "grants": [
    {
      "src": ["autogroup:member"],
      "dst": ["autogroup:internet"],
      "ip": ["*"]
    }
  ]
}
```

Grant, который разрешает подключения к самому узлу (SSH и другие сервисы), не
даёт права использовать его как интернет-шлюз: для этого нужен именно
`autogroup:internet`.

Проверить, что узел анонсирует exit node, можно в `tailscale status` на
сервере и в списке exit node на клиенте:

```bash
tailscale exit-node list
```

Внешние доки: [Exit nodes](https://tailscale.com/kb/1103/exit-nodes),
[Grants](https://tailscale.com/docs/features/access-control/grants).

## 5. Клиент

Linux:

```bash
sudo tailscale set --exit-node=100.x.y.z --exit-node-allow-lan-access=true
```

`100.x.y.z` — tailscale-адрес шлюза (`tailscale ip -4` на сервере).
`--exit-node-allow-lan-access=true` оставляет клиенту доступ к его локальной
сети: без него при включённом exit node локальные устройства пропадут.

Проверка:

```bash
tailscale status
```

В строке шлюза должно быть указано, что он используется как exit node;
соединение может быть `direct` или `relay` — это нормально.

Windows и Android: откройте приложение Tailscale → **Exit node** → выберите
сервер; там же включите **Allow LAN access**. На Android тумблер **Use
Tailscale DNS settings** должен быть включён (раздел 6).

Выключить exit node:

```bash
sudo tailscale set --exit-node=
```

## 6. DNS и fake-ip

Это самый важный шаг: без него exit node работает, но доменные правила
geo/dpi/ru не применяются.

Почему так. mihomo принимает решения по домену, а не по IP. Если клиент
резолвит домен сам (через роутер, провайдера или DoH в браузере), шлюз видит
только IP и не может сопоставить его с `geo.txt`/`dpi.txt`. Поэтому DNS
tailnet-клиентов должен идти на `100.x.y.z:53` — в mihomo. Тот отвечает
fake-ip из `198.18.0.0/16`, запоминает соответствие и применяет правила к
соединению.

Настройка в admin console:

1. **DNS** → **Nameservers** → **Add nameserver** → **Custom**.
2. Укажите `100.x.y.z` (tailscale-адрес шлюза).
3. Включите **Use with exit node** — сразу в диалоге **Add nameserver** или
   позже: три точки рядом с nameserver'ом → **Edit nameserver**. Без этого
   тумблера при активном exit node Tailscale игнорирует кастомные
   nameserver'ы и резолвит все домены через сам exit node, поэтому запросы
   могут не попасть в mihomo и fake-ip.
4. Выберите режим и сохраните:
   - **Override local DNS** — все клиенты tailnet используют этот сервер для
     всех доменов. Проще всего и даёт единую политику.
   - **Split DNS** — сервер применяется только к выбранным доменам. Если нужны
     все правила geo/dpi/ru, добавляйте домены, которые должны идти через
     шлюз; остальные продолжат резолвиться локально.

В приложении Tailscale на Android и Windows включите **Use Tailscale DNS
settings**. Этот тумблер необходим, но сам по себе ничего не даёт: без
nameserver в админке запросы не попадут в mihomo. На Linux-клиенте не
отключайте `--accept-dns` (по умолчанию он включён).

Проверка на клиенте:

```bash
nslookup youtube.com
```

Ожидаемый ответ — адрес из `198.18.0.0/16`, например `198.18.0.7`. Если
вернулся реальный IP, клиент не использует DNS шлюза. На Windows при split DNS
`nslookup` может игнорировать правила Tailscale — используйте
`Resolve-DnsName youtube.com` в PowerShell.

DoH в браузерах (Chrome, Edge, Firefox) и **Private DNS** на Android
отключите. Иначе приложение резолвит домен само, получает реальный IP и идёт
мимо политики, даже если системный DNS настроен правильно.

Если шлюз недоступен, при **Override local DNS** ломается DNS всего tailnet:
клиенты не смогут резолвить имена, пока шлюз не вернётся. Держите запасной
доступ (LAN/SSH) и помните об этом; при split DNS проблема ограничена
выбранными доменами.

Внешняя документация: [DNS in Tailscale](https://tailscale.com/kb/1054/dns).

## 7. Доступ к сервисам по tailscale-адресу

С tailnet-клиента доступны те же сервисы, что из LAN, но по адресу
`100.x.y.z`:

- панель — `http://100.x.y.z:PANEL_PORT`;
- zashboard — `http://100.x.y.z:9090/ui/`, backend `http://100.x.y.z:9090`,
  секрет `MIHOMO_SECRET` из `/etc/routeweave/.env`;
- DNS — `100.x.y.z:53` (его же указываем в admin console);
- mixed-proxy — `100.x.y.z:7890`.

Порты те же, что у LAN. При активном ufw `install.sh` добавляет:

- `ufw route allow in on tailscale0` и `ufw route allow out on tailscale0`;
- `ufw allow 41641/udp` — прямые соединения Tailscale;
- `ufw insert 1 deny in on LAN_IFACE from 100.64.0.0/10` — защита от
  CGNAT-спуфинга из LAN;
- разрешения с `100.64.0.0/10` на `53/tcp+udp`, `7890`, `PANEL_PORT`, `9090`
  (и `1443` при `INSTALL_TGWS=yes`) — тот же набор, что у LAN/WDTT.

Если Tailscale установлен после Routeweave, повторите установщик:
`sudo env INSTALL_TAILSCALE=yes bash install.sh` (поставит и настроит) или
`sudo bash install.sh` (добавит ufw-правила, если `tailscale` уже есть).
Повторный запуск идемпотентен.

CGNAT-спуфинг закрыт и на уровне iptables: пакет с источником из
`100.64.0.0/10`, пришедший не с `tailscale0`, дропается в `ROUTEWEAVE-FWD`.
Поэтому LAN-клиент не может выдать себя за tailnet-узел.

Доступ к панели по tailscale-адресу — это доступ внутри tailnet, а не
публикация в интернет. Права регулируются ACL/grants; Basic-авторизация панели
при этом остаётся.

## 8. Карточка в панели

Если Tailscale установлен, панель показывает карточку **Tailscale**. Карточки
нет, если пакет не установлен или панель не нашла helper
`/usr/local/sbin/routeweave-tailscale-state.sh`: в этом случае
`tailscale.installed` в `/api/status` равен `false`, и карточка скрыта.

Что показывает карточка:

- **tailscaled** — `active`, `inactive` или `failed`; значение `absent`
  helper возвращает, когда пакета Tailscale нет (тогда карточка скрыта),
  `activating`/`unknown` — пока демон стартует или `systemctl` недоступен;
- **tailnet-адрес** — IPv4 из `Self.TailscaleIPs` (то же, что `tailscale ip -4`);
- **Exit node** — `анонсирован и одобрен`, `ждёт одобрения` (анонсирован, но
  не одобрен) или `не анонсирован`;
- **Трафик** — суммарные счётчики `↓`/`↑` по интерфейсу;
- **Скорость** — оценка `↓`/`↑` за интервал между опросами `/api/status`
  (панель опрашивает его раз в 15 с), а не мгновенная скорость интерфейса;
- **Пиры** — число пиров; строка показывается, только если оно больше нуля;
- **Ошибка** — короткий текст, если helper не смог прочитать состояние.

Данные готовит helper `/usr/local/sbin/routeweave-tailscale-state.sh`. Он
только читает:

- `systemctl is-active tailscaled`;
- `tailscale status --json` — через `sudo`, поэтому доступен сокет
  `tailscaled`, закрытый для непривилегированной панели;
- `tailscale debug prefs` — только при отсутствии одобрения и только поле
  `AdvertiseRoutes`; полный вывод содержит служебные данные, поэтому не
  сохраняется и не логируется;
- счётчики `/sys/class/net/<TS_IFACE>/statistics/rx_bytes` и `tx_bytes`.

Наружу helper отдаёт одну строку JSON с агрегатами: статус демона, tailnet-адрес,
флаги exit node, счётчики трафика и число пиров. Ключей авторизации, имён
устройств и списка пиров в ответе нет; вывод `tailscale status` и stderr в ответ
и логи не попадают.

`TS_IFACE` helper берёт из `/etc/routeweave/.env` (по умолчанию `tailscale0`);
сам он в `.env` не пишет. Невалидное имя интерфейса заменяется на `tailscale0`.

Одобрение exit node карточка не заменяет: оно по-прежнему делается только в
admin console (**Machines** → **Edit exit node** → **Use as exit node**, см.
раздел 4).

Флаги считаются так:

- `approved` — `Self.ExitNodeOption` из `tailscale status --json`. Tailscale
  выставляет это поле, только когда узел анонсировал exit node **и** одобрен
  (`offered && approved`), а сборка умеет использовать exit node. Это
  авторитетный признак, отдельного поля `Prefs` в JSON нет.
- `advertised` — либо то же одобрение, либо наличие `0.0.0.0/0` в
  `AdvertiseRoutes` из `tailscale debug prefs` (запрашивается только когда
  одобрения нет). Так `ждёт одобрения` (анонсирован, но не одобрен)
  отличается от `не анонсирован`. Если команда недоступна или вернула не
  JSON, считается `advertised=false`.

`Self.PrimaryRoutes` здесь не используется: в JSON 1.104.x такого поля у
своего узла нет, а само поле описывает subnet-маршруты, для которых узел
сейчас primary, и к exit node отношения не имеет. `Self.AllowedIPs` тоже не
разбирается: `ExitNodeOption` уже учитывает и `AllowedIPs`, и buildfeature.

`install.sh` ставит helper в `/usr/local/sbin` и включает его в sudo-allowlist
панели (`/etc/sudoers.d/routeweave-panel`, без wildcard). `uninstall.sh`
удаляет helper, но не трогает пакет Tailscale и состояние `/var/lib/tailscale`.

## 9. Диагностика

Основное:

```bash
sudo /etc/routeweave/fw-tailscale.sh doctor
sudo /etc/routeweave/fw-tailscale.sh status
```

Оба режима только читают состояние и ничего не меняют. `doctor` проверяет:

- запущен ли `tailscaled`;
- `net.ipv4.ip_forward=1`;
- есть ли `ip rule` priority 8991 на `100.64.0.0/10` → `main`;
- правила `ROUTEWEAVE-FWD` для `tailscale0`;
- нет ли прыжков в `ts-input`, `ts-forward`, `ts-postrouting`;
- анонсирован и одобрен ли exit node;
- маршруты до tailnet;
- правила ufw;
- не конфликтует ли метка `0x40000` с `WAN_INGRESS_MARK`;
- в режиме `nat` — правило 8992, цепочки `ROUTEWEAVE-TSMARK` и
  `ROUTEWEAVE-TSNAT`, MASQUERADE и маршрут помеченного пакета в WAN (а не в
  TUN). Правила `tailscale0 ↔ Meta` в этом режиме не требуются.

Дополнительно:

```bash
tailscale status
tailscale status --json
tailscale netcheck
ip rule show | grep -E '8991|8992'
sudo iptables -S ROUTEWEAVE-FWD
sudo iptables -t mangle -S ROUTEWEAVE-TSMARK
sudo iptables -t nat -S ROUTEWEAVE-TSNAT
sudo iptables-save | grep -E -- '-j (ts-input|ts-forward|ts-postrouting)'
journalctl -u tailscaled -n 50
```

Что смотреть:

- `tailscale status` — узел `active`, соединение `direct` или `relay`;
- `ip rule show` — строка `8991: from all to 100.64.0.0/10 lookup main`;
- `iptables -S ROUTEWEAVE-FWD` — ACCEPT для `tailscale0` и DROP для
  `100.64.0.0/10` не с `tailscale0`;
- `tailscale status --json` — `Self.ExitNodeOption` (`true` = анонсирован и
  одобрен) и `Self.AllowedIPs`, куда control-plane кладёт `0.0.0.0/0` и
  `::/0` только для одобренного exit node (формат JSON может меняться между
  версиями);
- `journalctl` — ошибки `tailscaled`, конфликты netfilter, проблемы с
  маршрутами.

Если exit node не работает, проверьте по порядку: одобрен ли он в admin
console, выбран ли на клиенте, `--netfilter-mode=nodivert`, правило 8991,
правила `ROUTEWEAVE-FWD` и ufw.

## 10. Ограничения

- В режиме `policy` трафик tailnet-клиентов идёт через userspace-стек mihomo.
  Пропускная способность ниже, чем при прямом NAT, и выше нагрузка на CPU.
  Для гигабита используйте `TS_EXIT_MODE=nat`.
- В режиме `nat` нет geo/SOCKS/DPI-политики mihomo: гео-блокировки и
  DPI-фильтрация для tailnet не обходятся. zapret на WAN-egress продолжает
  работать, но доменные правила mihomo не применяются.
- В режиме `nat` источник в LAN маскарадится: LAN-устройства видят адрес
  шлюза, а не `100.x` клиента, и логов с адресом клиента на них не будет.
- Режиму `nat` нужен корректный `LAN_IFACE`: при пустом значении (как и при
  пустых `TS_IFACE`/`TS_SUBNET`) firewall пишет `warn` и работает как `policy`.
- `QUIC_DROP=yes` режет UDP/443 и у tailnet-клиентов: браузеры откатятся на
  TCP и попадут в политику. Если QUIC нужен, придётся выключить `QUIC_DROP`,
  но тогда часть трафика пойдёт мимо правил.
- IPv6 не поддерживается: проект IPv4-only. Используйте tailscale-адрес
  `100.x.y.z`; IPv6 в LAN и на клиентах лучше отключить.
- `auto-redirect: true` в `/etc/routeweave/config.yaml` может перехватывать
  входящий TCP раньше forwarding. Если exit node не работает, поставьте
  `auto-redirect: false` и перезапустите mihomo — то же советует
  `fw-wan.sh doctor`. Файл генерируется установщиком, ручная правка живёт до
  следующего запуска `install.sh`.
- fake-ip `198.18.0.0/16` может конфликтовать с другим прокси на клиенте,
  который использует тот же диапазон (например, второй mihomo/Clash).
  Оставьте один перехватчик или смените диапазон у второго.
- При `WAN_INGRESS=yes` метка по умолчанию `0x40000` совпадает с меткой
  Tailscale. Поставьте в `/etc/routeweave/.env`
  `WAN_INGRESS_MARK="0x200000"` и выполните
  `sudo systemctl restart routeweave-firewall`.

## 11. Откат

Перестать быть exit node:

```bash
sudo tailscale set --advertise-exit-node=false
```

Клиенты больше не смогут выбрать шлюз как exit node; остальной tailnet и
доступ к сервисам по `100.x.y.z` продолжат работать.

Вернуть `policy` из `nat`:

```bash
sudo sed -i 's/^TS_EXIT_MODE=.*/TS_EXIT_MODE="policy"/' /etc/routeweave/.env
sudo systemctl restart routeweave-firewall
```

`start` снимет прыжки и цепочки `ROUTEWEAVE-TSMARK`/`ROUTEWEAVE-TSNAT` и правило
8992, и трафик tailnet снова пойдёт через TUN. `fw-tailscale.sh status` должен
показать `TS_EXIT_MODE: policy`.

Полный откат firewall-правил:

```bash
sudo /etc/routeweave/fw-base.sh stop
```

`stop` снимает `ROUTEWEAVE-FWD`, правило 8991 и артефакты nat-режима (цепочки
`ROUTEWEAVE-TSMARK`/`ROUTEWEAVE-TSNAT`, правило 8992). Это выключает и остальные
правила шлюза, поэтому в обычном сценарии достаточно первой команды;
`fw-base.sh stop` нужен, только если выводите шлюз из работы или хотите убрать
правила полностью. Чтобы вернуть базовый firewall, выполните
`sudo systemctl restart routeweave-firewall`: правило 8991 вернётся вместе с
базовым набором, но без `tailscale0` оно ни на что не влияет.

`uninstall.sh` снимает свои ufw-правила (включая `tailscale0`, `41641/udp` и
правила для `100.64.0.0/10`) и удаляет `/etc/routeweave/fw-tailscale.sh`, но
не удаляет пакет Tailscale и состояние `/var/lib/tailscale`. Если Tailscale
больше не нужен, удалите пакет вручную:

```bash
sudo apt remove tailscale
```

Состояние в `/var/lib/tailscale` при отключении exit node сохраняется; для
полной очистки удалите каталог вручную.
