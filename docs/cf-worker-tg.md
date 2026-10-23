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

Панель/конфиг: в `/etc/routeweave/.env` задайте
```
BASE_DOMAIN=myhome.dpdns.org
```
и `sudo systemctl restart mihomo routeweave-panel` (домен уедет в fake-ip-filter и
в правило DIRECT, чтобы туннель не заворачивался сам в себя).

Дальше — по документации tg-ws-proxy-rs (docs/Deployment.md в репозитории
valnesfjord/tg-ws-proxy-rs): прокси сам пробует прямой WS к Telegram DC, а при
недоступности переключается на CF-апстрим/Worker. Проверить путь можно в
панели — проба «CF Worker» и проба «tg-ws-proxy» должны быть OK.

## Лимиты бесплатного плана

- Workers Free: ~100 000 запросов/сутки — для семьи достаточно.
- Задержка выше, чем у прямого WS (запрос идёт через CF-кластер).
- Если CF Worker недоступен, tg-ws-proxy автоматически падает обратно
  на прямой WS/TCP — Telegram продолжает работать.
