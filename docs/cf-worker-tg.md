# CF Worker + dpdns.org для tg-ws-proxy

> **Опционально:** только при `INSTALL_TGWS=yes`. Ядро Routeweave работает без
> Telegram-прокси и CF Worker.

Цель: трафик Telegram идёт как обычный HTTPS к Cloudflare (Worker), который
туннелирует его до Telegram DC. TSPU видит WS/TLS к CF, а не MTProto.
Домен — бесплатный поддомен `*.dpdns.org` (DigitalPlat FreeDomain).

## 1. Поддомен dpdns.org → Cloudflare

1. Зарегистрируйте поддомен на https://dash.domain.digitalplat.org
   (пример: `myhome.dpdns.org`).
2. В панели DigitalPlat укажите NS-серверы вашего аккаунта Cloudflare
   (Cloudflare → любой домен → Cloudflare Nameservers, вида `xxx.ns.cloudflare.com`).
3. В Cloudflare: **Add a site → myhome.dpdns.org → Free plan**.
   Регистрация бесплатно, поддержка NS-делегирования у DigitalPlat есть.
4. Дождитесь активации зоны (обычно минуты).

## 2. CF Worker (вариант Flowseal / «CfProxy»)

1. В репозитории https://github.com/Flowseal/tg-ws-proxy откройте раздел
   **«Настройка Cloudflare Worker'а»** (CfProxy.md) — там актуальный код воркера.
2. Cloudflare → **Workers & Pages → Create Worker** → вставьте код → Deploy.
3. В настройках воркера: **Settings → Domains & Routes → Add → Custom domain**
   → укажите ваш `myhome.dpdns.org` (или поддомен, например `tg.myhome.dpdns.org`).
4. Проверка: `curl -sI https://myhome.dpdns.org/` должен вернуть HTTP-ответ
   (любой код, главное — не таймаут).

## 3. Подключение к tg-ws-proxy-rs

В `/etc/routeweave/.env` задайте домен в одинарных кавычках:
```
BASE_DOMAIN='myhome.dpdns.org'
```
`TG_CF_WORKER_DOMAIN` обычно совпадает с ним; если он пуст, установщик
подставит `BASE_DOMAIN`.

После смены домена перезапуска юнитов `mihomo` и `routeweave-panel`
**недостаточно**: `config.yaml` генерируется только установщиком, и в нём
останутся старые fake-ip-filter и правило `DIRECT`. Из каталога репозитория
выполните:
```
sudo INSTALL_TGWS=yes ./install.sh
```
`INSTALL_TGWS=yes` нужен, чтобы установщик перезаписал `/etc/routeweave/.env.tgws`
новым доменом; существующий `TG_SECRET` он возьмёт из `.env`. Установщик
перегенерирует `config.yaml` из `.env` и перезапустит юниты ядра. Проверка:
```
sudo mihomo -d /etc/routeweave -t
systemctl status mihomo routeweave-panel
sudo grep -n 'myhome.dpdns.org' /etc/routeweave/config.yaml
```
В `config.yaml` домен должен быть в `fake-ip-filter` и в правиле
`DOMAIN-SUFFIX,...,DIRECT`, чтобы туннель не заворачивался сам в себя.

Дальше — по документации tg-ws-proxy-rs (docs/Deployment.md в репозитории
valnesfjord/tg-ws-proxy-rs). Без явного пина прокси сначала пробует прямой
WebSocket к Telegram DC и только потом переходит к CF Worker; в сетях, где
такой WS недоступен, это стоит нескольких секунд на каждую новую сессию —
поэтому установщик ставит Worker первым (раздел 4).

## 4. CF Worker первым в цепочке апстримов

Почему. Прямой WebSocket к Telegram DC в таких сетях режется на SYN:
TCP-соединение вообще не устанавливается, mihomo отдаёт `i/o timeout`,
успешных direct-WS нет. Прокси тратит на быстрый probe 2 с (а в cooldown —
полный таймаут ~5 с), и только потом поднимает CF Worker за доли секунды.
В логах это видно как `WS DC2 timed out` и следующая строка
`failed → CF Worker pool hit`. То есть Worker работает, а пауза возникает
исключительно из-за порядка попыток.

Что делает установщик. При непустом `TG_CF_WORKER_DOMAIN` он пишет в
`/etc/routeweave/.env.tgws` (и дублирует в общий `.env`) строку, которую прокси
читает как `--pinned-upstream`:
```
TG_PINNED_UPSTREAM='cfworker,ws,tcp'
```
Порядок: CF Worker → прямой WS → TCP. Медиа-трафик наследует non-media пин.
Если домен CF Worker не задан, ключ не пишется вовсе: пустой список тиров для
прокси невалиден, а дефолтный порядок апстримов остаётся за прокси.

Как переопределить. Список тиров задаётся переменной окружения установщика
(она имеет приоритет над автоопределением):
```
sudo INSTALL_TGWS=yes TG_PINNED_UPSTREAM="ws,cfworker,tcp" ./install.sh
```
Допустимые тиры: `ws`, `cfworker`, `cfproxy`, `mtproto`, `tcp`; только через
запятую, без пробелов. Невалидный список установщик отвергает с ошибкой.

Как поправить уже установленный прокси без переустановки. Добавьте или измените
строку в `/etc/routeweave/.env.tgws` (на старых установках, где compose читает
общий `/etc/routeweave/.env`, — туда) и пересоздайте контейнер из каталога
репозитория:
```
sudo SERVER_IP=192.168.1.10 docker compose up -d --force-recreate
```
`SERVER_IP` — адрес шлюза из вашего `.env` (IPv4); он передаётся в окружении
процесса. `--env-file` не используется, чтобы compose не разбирал `.env` с
shell-экранированием (`'\''`): env-парсер compose не обязан понимать такую
запись и может упасть на значении с апострофом — например, в `SOCKS_URL`.
Сам контейнер получает переменные из `/etc/routeweave/.env.tgws` через
`env_file:`. Именно `--force-recreate`, а не `docker restart`: `env_file`
читается при создании контейнера, при рестарте новые значения не подхватятся.

Проверка. В логах сразу после старта должно быть `pinned → CF Worker connected`
и не должно быть `WS DC2 timed out`:
```
sudo docker logs routeweave-tgwsproxy
sudo docker exec routeweave-tgwsproxy tg-ws-proxy --check
```
`--check` проверяет Worker и отвечает за доли секунды.

`TG_CF_IP` задавать не нужно: все валидные edge-адреса Cloudflare ведут в один и
тот же ближайший кластер, выигрыша нет, а часть адресов вообще не туннелирует.
Установщик этот ключ не пишет.

Домен Worker в `lists/dpi.txt` нужен для TLS-десинка самого Worker; блокировку
прямого WS он не обходит — она происходит на SYN, до TLS и SNI.

На практике пиннинг убирает паузу 2–5 с на каждой новой сессии: холодный старт
end-to-end сокращается с ~10 с до ~0,5 с (по замерам).

## Лимиты бесплатного плана

- Workers Free: ~100 000 запросов/сутки — для семьи достаточно.
- Задержка выше, чем у прямого WS (запрос идёт через CF-кластер).
- Если CF Worker недоступен, tg-ws-proxy автоматически падает обратно
  на прямой WS/TCP — Telegram продолжает работать.
