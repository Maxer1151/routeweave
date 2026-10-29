# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Версии и даты не выдумываются: изменения копятся в `Unreleased` и получают
номер при выпуске.

## [Unreleased]

### Добавлено

- Сторож маршрутизации: `routeweave-guard.sh` и `routeweave-guard.timer` — раз
  в 60 с проверяют `ip_forward`, nft-таблицу `inet mihomo`, `default` в
  таблице 2022, правило NFQUEUE на `QNUM`, привязку очереди к `nfqws` и
  `ip rule` 8993 при `WAN_INGRESS=yes`; пропавшее чинится с кулдауном 600 с и
  предохранителем «не больше четырёх действий в час», упавший
  `zapret-nfqws` (`failed`) перезапускается, выключенный вручную — нет.
- Сторож: функциональные пробы трафика — ICMP до `1.1.1.1` и два `curl`-запроса
  (обычный сайт и домен из `dpi.txt`). Ломается не только структура: 27.10
  при зелёных проверках новые TCP-соединения 80/443 не открывались. Сломанный
  десинк лечится перезапуском `zapret-nfqws` со второй неудачи подряд,
  сломанный TCP — сразу; при недоступной внешней сети сторож бездействует.
  Если рестарт не помог, повторный откладывается на 6 часов
  (`GUARD_STUCK_TTL`, маркеры `probe-*.stuck`), чтобы не рвать очередь
  клиентам каждые 10 минут; успешная проба паузу снимает.
  Пробы выключаются через `/etc/routeweave/guard.env` (`GUARD_PROBE=no`).
- Панель: снимок состояния — кнопки «Снять дамп состояния» и «Показать
  последний», helper `routeweave-diag-dump.sh` в sudo-allowlist без wildcard,
  файлы в `/var/log/routeweave` (`0640 root:routeweave`); `.env` и
  `config.yaml` в снимок не попадают.
- Android-приложение «VPN Авто» (`android/`): правила по типу сети и доверенным
  Wi-Fi (доверенная — VPN выключен, незнакомая — Tailscale + exit node,
  мобильная — WDTT-Plus), ручные команды, список доверенных SSID и watchdog exit
  node. APK публикуется в релизах.
- Прямой NAT-режим exit node Tailscale (`TS_EXIT_MODE=nat`): трафик из
  `tailscale0` метится в mangle (`0x100000`), уходит в WAN по `ip rule`
  priority 8992 и маскарадится в `LAN_IFACE` — без userspace-стека mihomo.
  Режим `policy` остаётся дефолтом: в `nat` для tailnet не действуют
  geo/SOCKS/DPI-политика mihomo, а источник в LAN маскарадится.
- Панель: карточка Tailscale — статус `tailscaled`, tailnet-адрес,
  анонсирование/одобрение exit node, суммарный трафик и оценка скорости, число
  пиров; helper `routeweave-tailscale-state.sh` только читает состояние
  (sudo-allowlist, без wildcard) и возвращает агрегаты без ключей и имён
  устройств.
- Firewall: `tailscale0` как третий вход (forwarding в TUN, возвратный путь,
  CGNAT-спуф-гард, `ip rule` 8991).
- Диагностика: `fw-tailscale.sh status|doctor`.
- Установщик: ufw-правила для `100.64.0.0/10` и `tailscale0`, `41641/udp`.
- Установщик: `INSTALL_TAILSCALE=yes|no` (по умолчанию `no`, `--defaults` —
  `no`) — установка Tailscale из официального apt-репозитория Ubuntu/Debian,
  включение `tailscaled` и настройка
  `--advertise-exit-node --netfilter-mode=nodivert --accept-dns=false`.
  Повторный запуск идемпотентен: установленный пакет не переустанавливается,
  авторизованный узел (`BackendState=Running`) получает `tailscale set`, а
  состояние `/var/lib/tailscale` и логин сохраняются. Ключ
  `TS_AUTHKEY_FILE`/`TS_AUTHKEY` используется только на время установки и не
  попадает в `.env`; если узел не авторизован и ключа нет, установщик не
  зависает, а печатает команду `tailscale up`.
- `feat(firewall): доступ из WAN (WAN_INGRESS)`:
  - цепочка `ROUTEWEAVE-WANMARK` помечает входящие NEW-соединения на
    `WAN_PORTS` и восстанавливает connmark на ответах (`restore-mark` первым,
    иначе ответ контейнера с приватным src теряет метку);
  - `ip rule fwmark $WAN_INGRESS_MARK lookup main priority 8993` уводит
    помеченные пакеты в `main` до правил mihomo `9001`/`9002` (метка
    настраивается, по умолчанию `0x200000`);
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
  коммита zapret в `/etc/routeweave/zapret.commit`, переменная `ZAPRET_REF`;
  версии и sha256 компонентов закреплены в `install.sh`.
- ufw: открываются `53/tcp+udp`, `7890/tcp`, `9090/tcp`, `PANEL_PORT/tcp`,
  `1443/tcp` (при tgws) для LAN и `WG_SUBNET`.
- Панель: ссылка `tg://proxy?...` на основе `TG_LINK_IP` (пусто =
  `SERVER_IP`), асинхронное применение списков и `GET /api/jobs/apply`.
- `render_mihomo.py`: поддержка `socks5h://` (резолв на стороне прокси).
- Тесты: наборы `tests/test_env_safety.py` (формат и чтение `.env`),
  `tests/test_update_lists.py` (staging, симлинки, секреты не в argv),
  `tests/test_panel.py` и `tests/test_panel_unit.py` (CSRF, Host, лимиты),
  `tests/test_input_policy.py` (цепочка `ROUTEWEAVE-IN`),
  `tests/test_supply_chain.py` (пины версий и sha256),
  `tests/test_uninstall.py`, `tests/test_compose.py`; в CI добавлен job
  `compose` (`docker compose config`) и проверка, что netns-тесты не
  пропускаются.
- `fw-wan.sh doctor`: предупреждение, если при заданном `SOCKS_URL` селектор
  `GEO` в mihomo стоит на `DIRECT`. Выбор группы хранится в `cache.db`
  (`profile.store-selected: true`), поэтому при правильном `config.yaml` весь
  `geo.txt` может молча идти напрямую, мимо апстрима. Проверка идёт первой и
  не зависит от `WAN_INGRESS`; это WARN, код возврата `doctor` не меняется.

### Изменено

- Лицензирование: добавлены `NOTICE` и `lists/README.md`; область действия MIT
  ограничена кодом и документацией Routeweave, зафиксированы лицензии
  сторонних компонентов и провенанс списков: `lists/geo.txt` и
  `lists/ru-direct.txt` — производные источников без лицензии и MIT не
  покрываются.
- Документация: Tailscale теперь необязательный компонент и может ставиться
  установщиком (`INSTALL_TAILSCALE=yes`): идемпотентность, ключ авторизации без
  хранения в `.env`, ручной `tailscale up`, одобрение exit node, DNS/fake-ip с
  **Use with exit node**, доступ к сервисам по tailscale-адресу, ufw и откат.
- `zapret-nfqws.service` запускает `/etc/routeweave/nfqws-launch.sh` с
  whitelist `EXTRA` вместо `sh -c`; `EnvironmentFile` убран.
- `fw-nfq.sh`: читаемый массив правил, атомарная запись hostlist, проверка
  `--qnum` у запущенного nfqws и идемпотентный `stop`.
- README синхронизирован с фактическим поведением: сохранение `zapret.conf`,
  порты ufw, `wdtt0`/`WG_SUBNET`, `TG_LINK_IP`, права `.env`, новые
  переменные и раздел про разработку.
- `install.sh`: повторная установка обновляет все управляемые ключи `.env`
  (с бэкапом `*.bak-<timestamp>`) вместо частичного пропуска, перезапускает
  только активные юниты, пишет маркер владения `/etc/routeweave/owned`,
  сохраняет исходное `net.ipv4.ip_forward` в `ip_forward.orig`; требуется
  Ubuntu 22.04+/Debian 12+ (Python 3.10+), на Ubuntu 20.04 установка
  останавливается с объяснением.
- `uninstall.sh`: режимы `--purge` и `--dry-run`, восстановление
  `net.ipv4.ip_forward`, удаление `/opt/zapret`, бинарника mihomo, юнитов и
  drop-in только по маркеру владения (`routeweave`/`preexisting`), точечные
  scoped-правила ufw вместо сброса, код возврата 1 при ошибках (удаление при
  этом продолжается).
- Документация разделена: `README.md` — пошаговое руководство для пользователя
  (добавлен раздел FAQ), `AGENTS.md` — архитектура, инварианты безопасности,
  тесты, стиль для разработчиков и агентов.

### Удалено

- Переменные `MIHOMO_SHA256` и `ZASHBOARD_SHA256`: версии и контрольные суммы
  теперь константы `install.sh` и не переопределяются из окружения.
- Поддержка Ubuntu 20.04 (Python 3.8): требуется Python 3.10+.
- Монтирование `/ui` (zashboard) самой панелью: SPA отдаёт mihomo на `:9090/ui`.
- Схема `https://` в `SOCKS_URL`: TLS к SOCKS-прокси не поддерживается.

### Исправлено

- Списки: домен из `ru-direct-custom.txt` теперь вычитается из апстримной
  части `geo.txt` по границе метки (вместе с поддоменами), поэтому явный
  direct-выбор сильнее geo-листа: `4pda.to`, попавший в `geoblock.lst`,
  больше не уходит в SOCKS. `geo-custom.txt` остаётся сильнее и сохраняет
  домен в geo; вычитание работает и при отказе RU-источника.
- Дедлок `nfqws`: правило NFQUEUE не исключало поддельные пакеты, помеченные
  `--dpi-desync-fwmark=0x80000`. Они возвращались в ту же очередь 200, их skb
  держали `sk_wmem_alloc` raw-сокета, `sendto()` вставал навсегда, очередь не
  дренировалась — TCP 80/443 умирал на 3–4 минуты при живом ICMP. Теперь в
  правиле есть `-m mark ! --mark $NFQWS_MARK/$NFQWS_MARK`, а `start`/`stop`
  снимают legacy-правило без метки. Сторож считает правило без метки
  сломанным и чинит его.
- Панель: применение списка падало с `read-only file system` (EROFS, errno 30).
  `routeweave-panel.service` с `ProtectSystem=strict` не открывал на запись
  `/etc/routeweave/.staging`, хотя панель запускает
  `routeweave-update-lists.sh` через `sudo -n`, а root-helper наследует
  mount-namespace юнита. Каталог добавлен в `ReadWritePaths`; сам он остаётся
  `0700 root:root`, поэтому пользователь `routeweave` писать в него не может.

- Регрессия `919f6eb`: `auto-redirect` mihomo снова включён по умолчанию и не
  выключается при `WAN_INGRESS=yes`, а DNS по умолчанию слушает `0.0.0.0:53`.
  На шлюзе с `WAN_INGRESS=yes` локальный TCP уходил в TUN и зацикливался, DNS
  не отвечал на `127.0.0.1:53` (через него резолвит Tailscale MagicDNS), и
  шлюз с клиентами оставались без сети. Отключение auto-redirect теперь только
  явное — `MIHOMO_AUTO_REDIRECT=no` для отладки (ломает локальный TCP/DNS).
  `fw-wan.sh doctor` больше не советует выключать auto-redirect и
  предупреждает только о выключенном.
- Tailscale: статус exit node больше не берётся из `Self.PrimaryRoutes` — в
  `tailscale status --json` 1.104.x такого поля у своего узла нет, а само
  поле описывает subnet-маршруты. Одобрение теперь определяется по
  `Self.ExitNodeOption` (`offered && approved`), а `анонсирован, но не
  одобрен` — по `AdvertiseRoutes` из `tailscale debug prefs` (только при
  отсутствии одобрения). Карточка снова показывает «анонсирован и одобрен»,
  `fw-tailscale.sh doctor` не даёт ложный FAIL про неодобренный маршрут.
- Панель: счётчик пиров Tailscale берётся из объекта `Peer` в
  `tailscale status --json` (раньше читалось несуществующее поле `Peers`,
  поэтому строка «Пиры» всегда скрывалась). Имена и ключи пиров наружу
  по-прежнему не отдаются.
- tg-ws-proxy: при заданном CF Worker он теперь ставится первым в
  `--pinned-upstream` (`cfworker,ws,tcp`). Прямой WS к Telegram DC режется на
  SYN, поэтому каждая новая сессия тратила 2–5 с на заведомо неудачную
  попытку перед переходом на Worker; холодный старт сократился с ~10 с до
  ~0,5 с. Порядок переопределяется `TG_PINNED_UPSTREAM` при установке.
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
- Панель: CSP собирается одним набором для всех маршрутов (`connect-src
  'self'`, `script-src 'self'`); отдельная политика для `/ui` больше не
  нужна, потому что SPA отдаёт mihomo на `:9090/ui`.
- `render_mihomo.py`: корректная обработка схем `socks5`, `socks5h`, `socks`,
  `http`, `https`, IPv6 и percent-encoding; невалидный непустой `SOCKS_URL`
  даёт понятную ошибку и `exit 1`; файл `.env` имеет приоритет над
  окружением; `FAKEIP_EXTRA` нормализуется.
- Тесты: зарегистрирован маркер `privileged`, добавлен `tests/conftest.py`,
  исправлены замечания ruff (PLW1510) и shellcheck (SC1090).
- CI: шаг firewall-тестов выполняется под `bash` (в container-job по
  умолчанию `sh`/dash, где падает `set -o pipefail`); замечания shellcheck
  0.9 (SC2317) закрыты директивами `SC2329,SC2317` и переписыванием
  конструкций `A && B || C` на явные `if`. Для netns-тестов в контейнере
  отключён профиль AppArmor (он запрещает `mount`), а shebang-скриптам
  выставлен бит `+x` (ruff EXE001).
- Установщик: drop-in `systemd-resolved` помечается маркером, поэтому
  повторная установка не бэкапит собственную конфигурацию, а `uninstall.sh`
  удаляет только свой файл; при несогласованном DNS после удаления
  записывается рабочий статический `resolv.conf`.
- `routeweave-update-lists.sh`: Bearer-секрет mihomo берётся из уже
  прочитанного `.env` (`${MIHOMO_SECRET:-}`) без `sed`, который оставлял
  кавычки; обновление списков больше не получает 401 и не перезапускает
  mihomo вместо reload через API.
- Установщик: если порт 53 уже слушает mihomo, `systemd-resolved` не
  перезапускается, а `/etc/resolv.conf` не перезаписывается.
- `uninstall.sh` возвращает `/etc/resolv.conf` симлинком (как было до
  установки), если он был симлинком; цель записывается при установке в
  `backup/resolv.conf.orig.link`.
- Панель: юнит больше не ставит пустой `CapabilityBoundingSet` — с ним
  `sudo -n` не мог повысить привилегии (setuid-бинарник получает capability
  только из bounding set), из-за чего не работали кнопки рестартов/обновления
  списков; набор сведён к минимально нужному.
- Firewall/LAN: вход на сервисы из LAN разрешает правило с двумя условиями —
  `-i LAN_IFACE` **и** `-s LAN_CIDR`; принимаются только RFC1918-подсети,
  публичный или пустой `LAN_CIDR` игнорируется с WARN и LAN-правила не
  добавляются. Раньше хватало одного интерфейса, поэтому на VPS с публичным
  NIC сервисы открывались наружу; `doctor` предупреждает, если LAN закрыт.
- Firewall/WAN: `fw-wan.sh` снимает устаревшее правило `ip rule` priority 8993
  при выключенном `WAN_INGRESS`; установщик мигрирует легаси
  `WAN_INGRESS_MARK=0x40000` (конфликт с меткой subnet-route Tailscale) на
  `0x200000` и чистит оставшееся правило.
- Апдейтер списков: сбой загрузки, JSON или слишком малое число хостов теперь
  пишется в лог и даёт `exit 1`; при частичном успехе обновлённый список
  сохраняется, но код возврата всё равно ненулевой; `geo.txt` и
  `ru-direct.txt` пишутся через root-only staging без симлинков.
- Панель: при переполнении таблицы окна rate-limit больше не сбрасываются
  целиком — вытесняется самая старая запись (`RATE_MAX_KEYS`), поэтому лимит
  нельзя обойти потоком разных ключей; списки читаются с `O_NOFOLLOW`, симлинк
  отклоняется.
- Установщик: `ip_forward.orig` сохраняется до первого изменения
  `net.ipv4.ip_forward` (иначе `uninstall.sh` восстанавливал уже изменённое
  значение); при `PANEL_BIND=0.0.0.0` печатается предупреждение; сгенерированный
  `PANEL_PASS` показывается только когда stdout — терминал.
- Тесты: `systemd-analyze verify` больше не считается упавшим из-за диагностики
  чужих юнитов хоста (`CPUAccounting=` на systemd 258+); ошибки самого
  проверяемого юнита по-прежнему валят тест.

### Безопасность

- Панель работает от системного пользователя `routeweave`; root-операции
  выполняются только через `sudo -n` по allowlist
  `/etc/sudoers.d/routeweave-panel` (конкретные юниты и helper-скрипты, без
  wildcard).
- CSRF-защита изменяющих запросов fail-closed (`Origin`, `Referer`,
  `Sec-Fetch-Site`, запасной `X-Requested-With`), allowlist заголовка `Host`
  (421), потоковый лимит тела запроса (413), CSP без inline-скриптов и
  стилей, security-заголовки ответов.
- `.env` создаётся с правами `0640 root:routeweave`; `.env.tgws` —
  `0640 root:root` и содержит только три переменные для контейнера
  tg-ws-proxy.
- Сравнение логина и пароля панели — constant-time (`secrets.compare_digest`).
- Целостность загрузок fail-closed: версии и sha256 mihomo `v1.19.13`,
  zashboard `v3.29.1`, WDTT `v20` и коммит zapret закреплены в `install.sh`;
  принимается ровно 64 hex-символа, при расхождении или отсутствии
  закреплённой суммы архив отклоняется. Распакованные архивы проверяются
  на symlink/setuid, образ tg-ws-proxy фиксируется по digest. Обход —
  только явным `--allow-unverified`/`ALLOW_UNVERIFIED=yes` с громким
  предупреждением.
- Установщик валидирует `LAN_IFACE`, `SERVER_IP`, `PANEL_PORT`, `PANEL_USER`,
  `SOCKS_URL` и `WG_SUBNET`; sudoers проверяется через `visudo -cf`.
- Временные файлы установщика создаются через `mktemp -d` с `trap` на
  очистку.
- `.env` пишется в одинарных кавычках и читается как данные: `source`/`eval`
  в установщике и апдейтере убраны, значения не могут выполниться как
  shell-код. `SOCKS_URL` (без `https://`), `BASE_DOMAIN` и `TG_LINK_IP`
  проходят строгую валидацию до записи.
- Периметр: цепочка `ROUTEWEAVE-IN` вставляется в `INPUT 1`, панель, API
  mihomo и DNS слушают `SERVER_IP`, Docker публикует `${SERVER_IP}:1443`;
  `iptables -w 5` во всех правилах, чтобы параллельные запуски не теряли
  блокировку. QUIC-drop не затрагивает помеченный `WAN_INGRESS` трафик;
  `WAN_INGRESS_MARK=0x0` и метки/приоритеты других компонентов запрещены.
- Панель: TTL-кэш и single-flight для тяжёлых запросов статуса, rate limit
  (429), таймауты всех helper-вызовов, `MemoryMax`/`TasksMax` в юните.
- Апдейтер списков: staging-каталог `0700 root:root`, отказ от работы через
  симлинки, атомарный `mv`, секреты передаются через `curl -K -` (не в argv),
  адрес API берётся из `MIHOMO_API`.
