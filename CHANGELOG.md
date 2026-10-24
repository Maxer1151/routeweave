# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Версии и даты не выдумываются: изменения копятся в `Unreleased` и получают
номер при выпуске.

## [Unreleased]

### Исправлено

- Firewall: правила перенесены в собственную цепочку `ROUTEWEAVE-FWD` с одним
  прыжком из `FORWARD`; `DROP UDP/443` (`QUIC_DROP=yes`) теперь стоит выше
  `ACCEPT`, поэтому QUIC режется и у LAN, и у VPN-клиентов.
- Firewall: VPN-интерфейсы определяются по именам (`wdtt0`, `tailscale0`,
  существующие `wg*`) вместо нерабочего шаблона `wg+`, который не матчил
  `wdtt0`; `WG_SUBNET` (по умолчанию `10.66.66.0/24`) получает `ip rule`
  priority 8990.
- Установщик больше не затирает `/etc/routeweave/zapret.conf`: обновляется
  только строка `WAN_IFACE=`, при изменении делается бэкап
  `*.bak-<timestamp>`. `config.yaml` регенерируется, но с бэкапом.
- Установщик: DNS настраивается через бэкап исходного `resolv.conf` в
  `/etc/routeweave/backup/` и атомарную запись; повторный запуск не плодит
  дубликаты правил ufw.
- Панель: `innerHTML` заменён на безопасные DOM-операции, статика вынесена в
  `app.css`/`app.js`; списки пишутся атомарно; добавлена валидация доменов и
  лимит размера загрузки (200 КБ); парсинг `.env` понимает кавычки и
  экранирование.
- Панель: для `/ui` (zashboard) отдаётся отдельная CSP с `connect-src` на
  `http:`/`https:`/`ws:`/`wss:` и inline-стилями, иначе SPA не может ходить
  в API mihomo на другом порту; `script-src` остаётся `'self'`.
- `render_mihomo.py`: корректная обработка схем `socks5`, `socks5h`, `socks`,
  `http`, `https`, IPv6 и percent-encoding; невалидный непустой `SOCKS_URL`
  даёт понятную ошибку и `exit 1`; файл `.env` имеет приоритет над
  окружением; `FAKEIP_EXTRA` нормализуется.
- Тесты: зарегистрирован маркер `privileged`, добавлен `tests/conftest.py`,
  исправлены замечания ruff (PLW1510) и shellcheck (SC1090).
- CI: шаг firewall-тестов выполняется под `bash` (в container-job по
  умолчанию `sh`/dash, где падает `set -o pipefail`); замечания shellcheck
  0.9 (SC2317) закрыты директивами `SC2329,SC2317` и переписыванием
  конструкций `A && B || C` на явные `if`.

### Безопасность

- Панель работает от системного пользователя `routeweave`; root-операции
  выполняются только через `sudo -n` по allowlist
  `/etc/sudoers.d/routeweave-panel` (конкретные юниты и helper-скрипты, без
  wildcard).
- CSRF-защита изменяющих запросов (`Origin`, `Referer`, `Sec-Fetch-Site`),
  CSP без inline-скриптов и стилей, security-заголовки ответов.
- `.env` создаётся с правами `0640 root:routeweave`; `.env.tgws` —
  `0640 root:root` и содержит только три переменные для контейнера
  tg-ws-proxy.
- Сравнение логина и пароля панели — constant-time (`secrets.compare_digest`).
- Установщик проверяет скачанные mihomo и zashboard по sha256, если апстрим
  публикует контрольную сумму; иначе громко предупреждает.
- Установщик валидирует `LAN_IFACE`, `SERVER_IP`, `PANEL_PORT`, `PANEL_USER`,
  `SOCKS_URL` и `WG_SUBNET`; sudoers проверяется через `visudo -cf`.
- Временные файлы установщика создаются через `mktemp -d` с `trap` на
  очистку.

### Добавлено

- `feat(firewall): доступ из WAN (WAN_INGRESS)`:
  - цепочка `ROUTEWEAVE-WANMARK` помечает входящие NEW-соединения на
    `WAN_PORTS` и восстанавливает connmark на ответах (`restore-mark` первым,
    иначе ответ контейнера с приватным src теряет метку);
  - `ip rule fwmark 0x40000/0x40000 lookup main priority 8993` уводит
    помеченные пакеты в `main` до правил mihomo `9001`/`9002`;
  - `fw-wan.sh start|stop|status|doctor`, идемпотентные запуск и остановка,
    диагностика маршрута, приоритетов и `auto-redirect`;
  - `mihomo.service` переутверждает правило через `ExecStartPost` после
    пересоздания policy-routing, `routeweave-firewall.service` получил второй
    `ExecStart`/`ExecStop`.
- `pytest.ini`, `tests/conftest.py`, `Makefile` (`lint`, `test`, `all`,
  `clean`, `help`) и CI на GitHub Actions: линт, тесты и отдельный job с
  netns/iptables для firewall-тестов.
- `SECURITY.md` с моделью угроз и политикой ответственного раскрытия.
- Установщик: `--help`, проверка `preflight` обязательных файлов, фиксация
  коммита zapret в `/etc/routeweave/zapret.commit`, переменные
  `MIHOMO_SHA256`, `ZASHBOARD_SHA256`, `ZAPRET_REF`.
- ufw: открываются `53/tcp+udp`, `7890/tcp`, `9090/tcp`, `PANEL_PORT/tcp`,
  `1443/tcp` (при tgws) для LAN и `WG_SUBNET`.
- Панель: ссылка `tg://proxy?...` на основе `TG_LINK_IP` (пусто =
  `SERVER_IP`), асинхронное применение списков и `GET /api/jobs/apply`.
- `render_mihomo.py`: поддержка `socks5h://` (резолв на стороне прокси).

### Изменено

- `zapret-nfqws.service` запускает `/etc/routeweave/nfqws-launch.sh` с
  whitelist `EXTRA` вместо `sh -c`; `EnvironmentFile` убран.
- `fw-nfq.sh`: читаемый массив правил, атомарная запись hostlist, проверка
  `--qnum` у запущенного nfqws и идемпотентный `stop`.
- README синхронизирован с фактическим поведением: сохранение `zapret.conf`,
  порты ufw, `wdtt0`/`WG_SUBNET`, `TG_LINK_IP`, права `.env`, новые
  переменные и раздел про разработку.
