# AGENTS.md — разработка Routeweave

Для людей и агентов, работающих с репозиторием; пользовательская документация — [`README.md`](README.md).

## Карта репозитория

| Путь | Что это |
|---|---|
| `install.sh` | Установка и идемпотентный апдейт: пакеты, юниты, `.env`, firewall, опциональные компоненты. Запиненные версии mihomo/zashboard/WDTT и их sha256, коммит zapret. |
| `uninstall.sh` | Удаление с `--purge`/`--dry-run`; удаляет только свои файлы (маркер владения `owned` и бэкапы). |
| `templates/` | `mihomo.service`, `routeweave-panel.service`, `routeweave-firewall.service`, `zapret-nfqws.service`, `routeweave-lists-update.{service,timer}`; `fw-base.sh`, `fw-wan.sh`, `fw-nfq.sh`, `fw-tailscale.sh`, `nfqws-launch.sh`, `render_mihomo.py`, `routeweave-update-lists.sh`, `routeweave-tailscale-state.sh`, `routeweave-tgws-state.sh`, `routeweave-tgws-restart.sh`, `zapret.conf.tpl`. |
| `panel/` | FastAPI-панель: `app.py`, `static/` (HTML/CSS/JS без inline), `requirements.txt` с точными пинами. Один воркер, состояние в памяти. |
| `lists/` | Исходные custom-списки; `geo.txt`/`ru-direct.txt` — генерируемые, руками не правятся. |
| `tests/` | pytest: статические проверки, юнит-тесты и netns-тесты firewall под root. |
| `tools/check_no_skips.py` | Превращает любой `SKIPPED` в отчёте pytest в падение job'а. |
| `docs/` | `tailscale-exit-node.md`, `cf-worker-tg.md` — подробные пользовательские руководства. |
| `docker-compose.yml` | tg-ws-proxy: образ по digest, `read_only`, `cap_drop: ALL`, лимиты. |
| `Makefile`, `pytest.ini` | Цели `lint`/`test`/`all`; объявление маркера `privileged`. |
| `.github/workflows/ci.yml` | Job'ы `lint`, `test`, `netns`, `compose`. |

## Как устроен шлюз

Пакет из LAN (`LAN_IFACE`), с `wdtt0` или `tailscale0` попадает в
`ROUTEWEAVE-FWD` и дальше в TUN mihomo. DNS обслуживает fake-ip листенер
mihomo на `:53`: клиент получает `198.18.x.x`, запрос уходит в mihomo, а
соединение маршрутизируется по доменному правилу:

- `direct` и `GEOIP,RU` — напрямую;
- `geo.txt` — через внешний SOCKS/HTTP из `SOCKS_URL`;
- `dpi.txt` — через zapret: nfqws метит пакеты `0x80000`, и они уходят в
  `main`, а не обратно в TUN.

Policy-routing (`ip rule`, приоритеты по возрастанию):

| Приоритет | Назначение |
|---|---|
| 8990 | `to WG_SUBNET lookup main` — ответы клиентам WDTT |
| 8991 | `to TS_SUBNET lookup main` — ответы tailnet-клиентам |
| 8992 | `fwmark 0x100000` → `main` — NAT-режим exit node Tailscale |
| 8993 | `fwmark WAN_INGRESS_MARK` → `main` — ответы сервисов, опубликованных в WAN |
| 9000/9001/9002 | правила mihomo (`auto-route`): таблица `2022`, `suppress_prefixlength 0`, `iif Meta` |

Правила 8990–8993 нужны, чтобы ответы доверенных зон и опубликованных
сервисов не заворачивались обратно в TUN: маршрут `default dev Meta` в таблице
`2022` иначе перехватывает их.

Конфиг mihomo не пишется руками: `.env` → `templates/render_mihomo.py` →
`/etc/routeweave/config.yaml`. Меняете поведение — правьте генератор и `.env`,
а не сгенерированный файл.

## Инварианты безопасности (не ломать)

1. **Списки пишутся только через root-only staging.** Апдейтер создаёт
   `.staging` с `0700 root:root`, пишет через `mktemp` и заменяет файл
   атомарным `mv`; симлинки отклоняются. Проверка:
   `tests/test_update_lists.py`.
2. **`.env` — данные, а не shell.** Построчный парсер, никакого
   `source`/`eval` в `install.sh`, апдейтере и панели. Права
   `0640 root:routeweave`, бэкап `.env.bak.*` — `0600 root:root`. Проверки:
   `tests/test_env_safety.py`, `tests/test_install_*.py`.
3. **CSRF fail-closed.** Нужен совпадающий `Origin`/`Referer` либо
   `Sec-Fetch-Site: same-origin|none`; без браузерных заголовков —
   `X-Requested-With: Routeweave`. Неизвестный `Host` → `421`, превышение
   лимита → `429`, тело >200 КБ → `413`. Проверки: `tests/test_panel.py`,
   `tests/test_panel_unit.py`.
4. **Панель читает списки через `O_NOFOLLOW`** и отказывается от симлинков.
   Проверка: `tests/test_panel*.py`.
5. **Firewall.** `ROUTEWEAVE-IN` вставляется прыжком в `INPUT 1`, политика
   `INPUT` не меняется; forwarding — `ROUTEWEAVE-FWD`. LAN-разрешение — это
   `-i LAN_IFACE` **и** `-s LAN_CIDR`, только RFC1918; публичный или пустой
   `LAN_CIDR` — WARN и ни одного LAN-правила. Проверки:
   `tests/test_firewall.py`, `tests/test_input_policy.py`,
   `tests/test_install_perimeter.py`.
6. **QUIC_DROP выше ACCEPT.** Помеченный `WAN_INGRESS` трафик из QUIC-drop
   исключён. `WAN_INGRESS_MARK=0x200000` не пересекается с `0x40000`,
   `0x80000`, `0x100000`; `WAN_INGRESS_RULE_PRIO=8993`; `0x40000` и
   приоритеты 8990–8992/9000–9002 запрещены. Проверки:
   `tests/test_wan_ingress.py`, `tests/test_tailscale_nat.py`.
7. **Supply chain.** Версии mihomo/zashboard/WDTT и sha256 — в git; zapret из
   закреплённого коммита, образ tgws по digest. `--allow-unverified` — только
   для отладки и не отключает проверку коммита zapret. Проверка:
   `tests/test_supply_chain.py`.
8. **Юнит панели.** `NoNewPrivileges=no` осознанно (нужен setuid-`sudo`) при
   минимальном `CapabilityBoundingSet`; `sudo -n` по allowlist без wildcard.
   Секреты не печатаются: `PANEL_PASS` выводится только когда stdout — TTY.
   IPv6 не фильтруется осознанно. Проверки: `tests/test_unit_hardening.py`,
   `tests/test_panel_unit.py`.

## Тесты

```bash
python -m pytest tests -q -rs          # обычный прогон
python -m pytest tests -m "not privileged" -q   # быстрый локальный
sudo env "PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH" \
  python -m pytest tests -m privileged -q -rs   # netns под root
python tools/check_no_skips.py <лог>   # падение при любом SKIPPED
```

Маркер `privileged` объявлен в `pytest.ini`; им помечены тесты, которым нужны
root, `ip`, `iptables` и netns. Без root они корректно скипаются через
`skipif`, а не падают. Netns-модули: `test_firewall.py`,
`test_wan_ingress.py`, `test_tailscale_exit.py`, `test_tailscale_nat.py`,
`test_input_policy.py`, `test_fw_nfq.py`.

Зависимости ставьте в venv, не в систему:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r panel/requirements.txt pytest pyyaml
```

Поддерживаемые версии Python — 3.12 и 3.14.

## Линтеры и Makefile

`make lint` = `bash -n`, `bash -O extglob -n`, `shellcheck -x install.sh
uninstall.sh templates/*.sh`, `ruff check panel templates tests`,
`python -m compileall -q panel templates tests`. `make test` = `pytest tests -v`.
`make all` (цель по умолчанию) = lint + test.

Пути к инструментам переопределяются: `make lint SHELLCHECK=... RUFF=...
PYTHON=...`. В CI ruff закреплён версией **0.17.2** — держите локально ту же,
иначе новые правила дадут расхождение. Каталог `tools/` в Makefile не входит и
проверяется в CI отдельно (`ruff check tools`, `compileall`).

## CI

| Job | Что проверяет |
|---|---|
| `lint` | `make lint`; отдельно `ruff check tools` и `compileall` по `tools/` |
| `test` | `pytest tests -v -rs` с покрытием `panel` и порогом 72 %; `compileall` |
| `netns` | `-m privileged` под sudo с sbin в `PATH`, `check_no_skips.py`; отдельно проверяет, что известные netns-модули дали privileged-тесты |
| `compose` | `docker compose config` (пустой stderr — обязательное условие) и `pytest tests/test_compose.py` |

Job `netns` нужен потому, что в job `test` netns-тесты скипаются без root и
ничего не проверяют: там же доказывается, что сгенерированные правила реально
применяются ядром, а не только парсятся.

## Стиль кода

- **Bash.** `set -euo pipefail` там, где уже есть; всегда цитируйте
  переменные; используйте хелперы `warn`/`fail`/`ok`; идемпотентность —
  `iptables -C` перед `-A`; никакого `source` для `.env`; учтите, что
  `bash -O extglob` входит в линт.
- **Python.** `from __future__ import annotations`, типизация, чистота под
  `ruff`; новые зависимости — только при реальной необходимости и с точным
  пином.
- **Тесты.** Только stdlib + `pytest` + `pyyaml`; временные каталоги через
  `tmp_path`; никаких сетевых вызовов и обращений к живой системе.

## Стиль коммитов

`<область>: <суть>` — `fix(firewall): ...`, `docs: ...`, `test: ...`,
`ci: ...`. Сообщение по-русски, одна логическая правка на коммит, без личных
данных и путей. В теле — что и почему. Пользовательские изменения
отражаются в `CHANGELOG.md` в разделе `[Unreleased]`.

## Правила публичного репозитория

Репозиторий **публичный**. В отслеживаемые файлы нельзя: личные пути к
домашним и временным каталогам, к точкам монтирования дисков Windows; имена и
адреса своих машин; SSH-логины и порты; личные IP; секреты и пароли.
Локальные заметки — в `*.local.md` (шаблон уже в `.gitignore`).

Перед коммитом прогоняйте поиск по изменённым и по всем трекаемым файлам.
Шаблон ниже намеренно записан так, чтобы сам этот файл не содержал личных
путей: символ пути дан классом, а не готовой строкой.

```bash
git grep -nE 'D:[\\]|/ho[m]e/|/tm[p]/|/mn[t]/[a-z]/|192[.]168[.]|10[.][0-9]+[.]|172[.](1[6-9]|2[0-9]|3[01])[.]'
```

Совпадение — стоп: переносите в локальный файл, а не «почищу потом». Не
коммитьте `.env`, логи и дампы.

## Как вносить изменения

- **Новый тест.** Статические и юнит-тесты — обычный модуль в `tests/` без
  маркера. Тест, которому нужны root/netns/iptables, помечайте
  `@pytest.mark.privileged` и ставьте `skipif` на отсутствие условий, а не
  module-level `skip`. Проверяйте то, что действительно может сломаться:
  правило, код возврата, отсутствие секрета в выводе.
- **Firewall.** Правьте `templates/fw-base.sh`/`fw-wan.sh`/`fw-tailscale.sh`,
  сохраняйте идемпотентность и `doctor`, добавляйте проверку в
  `tests/test_firewall.py`/`test_wan_ingress.py`/`test_input_policy.py` и
  прогоняйте под root. Порядок прыжков и приоритеты `ip rule` — часть
  инвариантов.
- **Установщик и панель.** Правьте `install.sh`/`panel/app.py`, обновляйте
  `tests/test_install_*.py`/`test_panel*.py` и синхронизируйте `README.md`,
  `.env.example`, `SECURITY.md`. Панель — один воркер, состояние в памяти
  процесса; multi-worker не поддерживается.

## Отложенное и известные ограничения

Смотрите `TODO.md`: разделы «Отложено (сознательно)», «Не проверено на живом
стенде», «Известные мелочи».

Главное: IPv6 не фильтруется; UDP идёт мимо политики (auto-redirect только
для TCP); fake-ip DNS работает только в `TS_EXIT_MODE=policy`;
`DOCKER-USER` Routeweave не управляет; `--require-hashes` для
`panel/requirements.txt` не включён; mypy не настроен.

## Ссылки

- [`README.md`](README.md) — пользовательская документация
- [`SECURITY.md`](SECURITY.md) — модель угроз
- [`TODO.md`](TODO.md) — отложенное и ограничения
- [`CHANGELOG.md`](CHANGELOG.md) — история изменений
- [`docs/tailscale-exit-node.md`](docs/tailscale-exit-node.md) — Tailscale exit node
- [`docs/cf-worker-tg.md`](docs/cf-worker-tg.md) — CF Worker для Telegram
- [`LICENSE`](LICENSE) — MIT
