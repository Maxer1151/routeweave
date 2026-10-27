#!/usr/bin/env python3
"""Генератор /etc/routeweave/config.yaml для mihomo.

Запуск: render_mihomo.py [--env-wins] [ENV_FILE] [OUT]

Источники настроек: файл .env (по умолчанию /etc/routeweave/.env) и
переменные окружения процесса. По умолчанию **файл важнее окружения**:
установщик генерирует конфиг именно из /etc/routeweave/.env, и случайно
унаследованная переменная (например, SOCKS_URL из чужого профиля shell или
из systemd-юнита) не должна подменять сохранённую конфигурацию сервера.
Флаг --env-wins инвертирует приоритет — нужен только для отладки и разовых
экспериментов (проверить значение, не правя файл).

Адреса прослушивания и TUN:
  * MIHOMO_BIND — адрес external-controller и bind-address (по умолчанию
    SERVER_IP, если он пуст — 127.0.0.1). 0.0.0.0 разрешён, но с
    предупреждением: API окажется доступен из WAN.
  * DNS_BIND — адрес DNS-листенера (по умолчанию 0.0.0.0:53: DNS нужен и
    LAN, и `127.0.0.1` — через него резолвит Tailscale MagicDNS, и wdtt0).
  * MIHOMO_ALLOW_LAN=yes — включить allow-lan (по умолчанию false: mixed-port
    слушает только доверенные адреса, доступ из LAN не открывается сам).
  * MIHOMO_TUN_MTU — MTU TUN (по умолчанию 1280, допустимо 576..9000).
  * MIHOMO_AUTO_REDIRECT=no — выключить auto-redirect mihomo (по умолчанию
    включён). Отключение ломает локальный TCP: трафик шлюза уходит в TUN и
    зацикливается, а DNS на loopback перестаёт отвечать. Оставляйте `no`
    только для отладки: WAN-доступ от него не зависит (метка 0x200000 и
    `ip rule` 8993).

Коды возврата: 0 — успех, 1 — ошибка разбора/записи, 2 — ошибка аргументов
(argparse).
"""

import argparse
import ipaddress
import os
import sys
from urllib.parse import unquote, urlsplit

import yaml

# Схемы, которые принимаем в SOCKS_URL, и тип прокси в терминах mihomo.
#   * socks5h/socks -> socks5: mihomo для типа socks5 передаёт прокси имя
#     хоста, а не IP, то есть имя резолвится на стороне прокси — это ровно
#     семантика «h» в curl. Отдельного типа socks5h у mihomo нет.
#   * http -> http: отдельного https-типа у mihomo нет, а TLS к самому прокси
#     не поддерживается. https:// в SOCKS_URL — ошибка (см. parse_socks), а не
#     тихий маппинг на http: иначе пользователь думал бы, что канал шифруется.
SCHEME_TO_TYPE = {
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks": "socks5",
    "http": "http",
}

# Базовые домены fake-ip-filter: им нужен настоящий IP, а не 198.18.x.x.
BASE_FAKEIP_FILTER = [
    "+.lan",
    "+.local",
    "+.msftconnecttest.com",
    "+.msftncsi.com",
    "+.ts.net",  # Tailscale MagicDNS
    "+.workgroup",  # SMB/local
]

DEFAULT_ENV_FILE = "/etc/routeweave/.env"
DEFAULT_OUT_FILE = "/etc/routeweave/config.yaml"
# Адрес по умолчанию, если SERVER_IP не задан: слушаем только localhost,
# а не 0.0.0.0. Иначе панель/API видны из WAN при INPUT policy ACCEPT.
# (DNS — исключение, см. DEFAULT_DNS_BIND ниже.)
DEFAULT_BIND = "127.0.0.1"
# DNS по умолчанию слушает все адреса: его спрашивают LAN и wdtt0, а
# Tailscale MagicDNS форвардит на 127.0.0.1:53. Привязка к SERVER_IP ломала
# loopback-резолв и DNS шлюза. Доступ из WAN закрывает ROUTEWEAVE-IN.
DEFAULT_DNS_BIND = "0.0.0.0"
# MTU TUN: 1280 — минимум IPv6 и безопасное значение для инкапсуляции
# (WireGuard/Tailscale сверху не любят 1500 без PMTU).
DEFAULT_TUN_MTU = 1280
TUN_MTU_MIN = 576
TUN_MTU_MAX = 9000


class SocksUrlError(ValueError):
    """SOCKS_URL задан, но не разбирается."""


class EnvValueError(ValueError):
    """Значение в .env не годится для config.yaml."""


def _parse_env_value(raw: str) -> str:
    """Разобрать значение .env как одно shell-слово без подстановок.

    Установщик пишет ``KEY='value'`` и не исполняет файл как shell, поэтому
    значение разбираем сами, но по shell-семантике кавычек:

    * ``'...'`` — содержимое дословно до закрывающей кавычки;
    * ``'\\''`` — закрывающая кавычка, экранированный апостроф и новая
      кавычка, то есть один ``'`` внутри значения;
    * ``"..."`` — кавычки снимаются, раскрываются только ``\\"`` и ``\\\\``
      (как в panel/app.py; ``\\n`` и прочие последовательности остаются
      текстом);
    * вне кавычек ``\\`` экранирует следующий символ;
    * сегменты конкатенируются: ``'a'"b"`` → ``ab``.

    Подстановок ($VAR, `...`) нет — это данные, а не код. Незакрытая
    кавычка не ошибка: возвращаем строку как есть, чтобы ручная правка .env
    не валила рендер.
    """
    if not raw:
        return ""

    out = []
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]

        if char == "'":
            end = raw.find("'", index + 1)
            if end == -1:
                return raw
            out.append(raw[index + 1:end])
            index = end + 1
            continue

        if char == '"':
            index += 1
            chunk = []
            closed = False
            while index < length:
                char = raw[index]
                if char == "\\" and index + 1 < length:
                    nxt = raw[index + 1]
                    if nxt in ('"', "\\"):
                        chunk.append(nxt)
                    else:
                        chunk.append(char)
                        chunk.append(nxt)
                    index += 2
                    continue
                if char == '"':
                    closed = True
                    index += 1
                    break
                chunk.append(char)
                index += 1
            if not closed:
                return raw
            out.append("".join(chunk))
            continue

        if char == "\\":
            if index + 1 < length:
                out.append(raw[index + 1])
                index += 2
            else:
                out.append(char)
                index += 1
            continue

        # Вне кавычек: накапливаем до следующей кавычки/обратного слэша.
        start = index
        while index < length and raw[index] not in "'\"\\":
            index += 1
        out.append(raw[start:index])

    return "".join(out)


def load_env(path: str) -> dict:
    """Построчный разбор KEY=VALUE (комментарии и пустые строки пропускаем).

    Файл — данные: установщик и апдейтер не делают ``source``/``eval`` .env.
    Значение разбирается как одно shell-слово без подстановок: снимаются
    одинарные и двойные кавычки, ``'\\''`` даёт апостроф внутри значения,
    соседние сегменты конкатенируются. Поэтому
    ``MIHOMO_SECRET='a'\\''b'`` — это секрет ``a'b``, а не ``a'\\''b``.
    """
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = _parse_env_value(v.strip())
    except FileNotFoundError:
        pass
    return env


def parse_socks(url: str) -> dict:
    """Разобрать SOCKS_URL в запись proxies для mihomo.

    Бросает SocksUrlError с русской причиной; вызывающий обязан упасть,
    а не тихо переключаться на DIRECT (иначе гео-обход молча не работает).
    """
    if "://" not in url:
        raise SocksUrlError("нет схемы (пример: socks5://user:pass@host:1080)")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "https":
        # Отдельного https-типа у mihomo нет: TLS до прокси не поддерживается.
        # Молчаливый маппинг на http опасен — пользователь считал бы, что
        # канал до прокси шифруется.
        raise SocksUrlError(
            "TLS к SOCKS-прокси не поддерживается, используйте http:// или socks5h://"
        )
    if scheme not in SCHEME_TO_TYPE:
        raise SocksUrlError(
            f"неподдерживаемая схема {scheme!r} "
            "(ожидается socks5, socks5h, socks или http)"
        )

    # Опечатки в хвосте URL лучше не глотать: иначе mihomo получит не тот
    # прокси, а пользователь — молча неработающий обход.
    if parts.query or parts.fragment:
        raise SocksUrlError("query/fragment в URL не поддерживаются")
    if parts.path not in ("", "/"):
        raise SocksUrlError(f"лишний путь в URL: {parts.path!r}")

    if not parts.netloc:
        raise SocksUrlError("не указан хост")
    # hostname у urlsplit уже без квадратных скобок: [::1] -> "::1".
    host = parts.hostname
    if not host:
        raise SocksUrlError("не указан хост")

    try:
        port = parts.port
    except ValueError as exc:
        raise SocksUrlError(f"некорректный порт: {exc}") from exc
    if port is None:
        raise SocksUrlError("не указан порт")
    if not 1 <= port <= 65535:
        raise SocksUrlError(f"порт вне диапазона 1..65535: {port}")

    # urlsplit не декодирует percent-encoding в userinfo: p%40ss -> "p%40ss".
    username = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    if password and not username:
        raise SocksUrlError("пароль указан без имени пользователя")

    ptype = SCHEME_TO_TYPE[scheme]
    proxy = {
        "name": "socks-out",
        "type": ptype,
        "server": host,
        "port": port,
        # UDP есть только у socks5; для http-прокси mihomo UDP не умеет.
        "udp": ptype == "socks5",
    }
    if username:
        proxy["username"] = username
        proxy["password"] = password
    return proxy


def normalize_domain(value: str) -> str:
    """Домен без префикса +./*. и без хвостовой точки."""
    value = (value or "").strip().lstrip("+*").lstrip(".")
    return value.rstrip(".")


def build_fakeip_filter(extra_raw: str, base_domain: str) -> list:
    """Базовые домены + FAKEIP_EXTRA + BASE_DOMAIN, без дублей.

    FAKEIP_EXTRA — список доменов для fake-ip-filter (им нужен реальный IP).
    Значение без префикса +./*. дополняем до +.: в fake-ip-filter это
    wildcard-домен. Синтаксис geosite:/rule-set: здесь не поддерживается —
    такие строки не разворачиваются в наборы, поэтому вписывать их смысла нет.
    *.foo приводим к +.foo: для mihomo это одно и то же, зато дедуп с
    базовым списком работает (иначе +.lan и *.lan остались бы двумя строками).
    """
    result = list(BASE_FAKEIP_FILTER)
    seen = {item.lower() for item in result}

    for token in extra_raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("*."):
            token = "+." + token[2:].strip()
        elif not token.startswith("+."):
            token = "+." + token
        token = token.rstrip(".")
        if len(token) <= 2:  # остался голый "+."
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(token)

    base = normalize_domain(base_domain)
    if base and "+." + base.lower() not in seen:
        seen.add("+." + base.lower())
        result.append("+." + base)

    return result


def parse_flag(data: dict, key: str, *, default: bool) -> bool:
    """Разобрать yes/no из .env; пусто — default, мусор — default + warn.

    Флаги в .env пишут руками ("Yes", " true", опечатка), поэтому
    нормализуем так же, как firewall-скрипты, и не падаем из-за одной
    переменной: неверное значение не должно оставлять сервис полувключённым.
    """
    raw = (data.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("yes", "true", "1", "on"):
        return True
    if raw in ("no", "false", "0", "off"):
        return False
    print(
        f"warn: {key}={raw!r} не yes/no, считаю "
        f"{'yes' if default else 'no'}",
        file=sys.stderr,
    )
    return default


def resolve_bind(data: dict, key: str, what: str, default=None) -> str:
    """Адрес прослушивания: <key> -> default -> SERVER_IP -> 127.0.0.1.

    Допускаем IPv4 и localhost: 0.0.0.0 разрешён, но предупреждаем только
    когда его задали явно. Штатный wildcard-дефолт (DNS слушает все
    интерфейсы) не должен шуметь в stderr: иначе любой рендер на рабочей
    установке выглядел бы как ошибка. Имя хоста не принимаем: mihomo ждёт
    IP, а SERVER_IP в .env — адрес интерфейса, не DNS-имя.
    """
    server_ip = (data.get("SERVER_IP") or "").strip()
    raw_value = (data.get(key) or "").strip()
    explicit = bool(raw_value)
    fallback = default or server_ip or DEFAULT_BIND
    raw = raw_value or fallback
    if raw == "localhost":
        return raw
    try:
        addr = ipaddress.IPv4Address(raw)
    except ValueError:
        raise EnvValueError(
            f"{key}={raw!r} должен быть IPv4-адресом или "
            f"localhost (например {fallback})"
        ) from None
    if addr.is_unspecified and explicit:
        print(
            f"warn: {key}={raw} — {what} будет слушать все "
            "интерфейсы, включая WAN; ограничьте доступ firewall'ом "
            "или укажите SERVER_IP",
            file=sys.stderr,
        )
    return raw


def parse_tun_mtu(data: dict) -> int:
    """MIHOMO_TUN_MTU: целое 576..9000, по умолчанию 1280."""
    raw = (data.get("MIHOMO_TUN_MTU") or "").strip()
    if not raw:
        return DEFAULT_TUN_MTU
    try:
        mtu = int(raw, 10)
    except ValueError:
        raise EnvValueError(
            f"MIHOMO_TUN_MTU={raw!r} должен быть целым числом "
            f"{TUN_MTU_MIN}..{TUN_MTU_MAX}"
        ) from None
    if not TUN_MTU_MIN <= mtu <= TUN_MTU_MAX:
        raise EnvValueError(
            f"MIHOMO_TUN_MTU={mtu} вне диапазона {TUN_MTU_MIN}..{TUN_MTU_MAX}"
        )
    return mtu


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render_mihomo.py",
        description="Сгенерировать config.yaml mihomo из .env (без сети).",
        epilog=(
            "По умолчанию значения из ENV_FILE важнее переменных окружения; "
            "--env-wins меняет приоритет на обратный."
        ),
    )
    parser.add_argument(
        "--env-wins",
        action="store_true",
        help="переменные окружения важнее файла .env (только для отладки)",
    )
    parser.add_argument(
        "env_file",
        nargs="?",
        default=DEFAULT_ENV_FILE,
        metavar="ENV_FILE",
        help=f"путь к .env (по умолчанию {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "out",
        nargs="?",
        default=DEFAULT_OUT_FILE,
        metavar="OUT",
        help=f"куда писать config.yaml (по умолчанию {DEFAULT_OUT_FILE})",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    cfg_env = load_env(args.env_file)
    if args.env_wins:
        data = {**cfg_env, **os.environ}  # окружение важнее файла
    else:
        data = {**os.environ, **cfg_env}  # файл важнее окружения

    socks_url = (data.get("SOCKS_URL") or "").strip()
    base_domain = normalize_domain(data.get("BASE_DOMAIN") or "")
    fakeip_extra = data.get("FAKEIP_EXTRA") or ""
    fake_ip_filter = build_fakeip_filter(fakeip_extra, base_domain)

    proxies = []
    geo_target = "DIRECT"
    if socks_url:
        try:
            proxy = parse_socks(socks_url)
        except SocksUrlError as exc:
            print(f"ошибка: SOCKS_URL не разобран: {exc}", file=sys.stderr)
            return 1
        proxies.append(proxy)
        geo_target = "socks-out"

    # Значения, которые нельзя «угадать»: при ошибке лучше упасть, чем
    # сгенерировать конфиг, который mihomo не примет или который откроет
    # сервисы в WAN.
    try:
        mihomo_bind = resolve_bind(
            data, "MIHOMO_BIND", "external-controller/API mihomo"
        )
        dns_bind = resolve_bind(
            data, "DNS_BIND", "DNS mihomo", default=DEFAULT_DNS_BIND
        )
        tun_mtu = parse_tun_mtu(data)
    except EnvValueError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1

    # allow-lan по умолчанию выключен: mixed-port не должен быть доступен из
    # LAN без явного согласия (MIHOMO_ALLOW_LAN=yes). WAN_INGRESS auto-redirect
    # больше не выключает: nft-таблица mihomo исключает из redirect адреса из
    # inet4_local_address_set (локальные/LAN), поэтому входящий WAN-трафик к
    # SERVER_IP не перехватывается, а ответы помеченных пакетов уходят по
    # `ip rule` 8993. Отключение auto-redirect ломает локальный TCP и DNS:
    # трафик шлюза заворачивается в TUN и зацикливается.
    allow_lan = parse_flag(data, "MIHOMO_ALLOW_LAN", default=False)
    auto_redirect = parse_flag(data, "MIHOMO_AUTO_REDIRECT", default=True)

    secret = (data.get("MIHOMO_SECRET") or "").strip()
    if not secret:
        # Установщик генерирует секрет сам; пустой секрет здесь — признак
        # ручной правки .env. Не генерируем молча: иначе панель и mihomo
        # разойдутся в секретах.
        print(
            "warn: MIHOMO_SECRET пуст — API mihomo/zashboard без пароля; "
            "установщик генерирует секрет, проверьте /etc/routeweave/.env",
            file=sys.stderr,
        )

    config = {
        "mixed-port": 7890,
        "external-controller": f"{mihomo_bind}:9090",
        "external-ui": "zashboard",
        "secret": secret,
        "allow-lan": allow_lan,
        "bind-address": mihomo_bind,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "geodata-mode": False,
        "geo-auto-update": False,
        "geox-url": {
            "geoip": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.metadb",
            "geosite": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat",
            "mmdb": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/country.mmdb",
        },
        "profile": {"store-selected": True},
        "tun": {
            "enable": True,
            "stack": "mixed",
            "auto-route": True,
            "auto-redirect": auto_redirect,
            "auto-detect-interface": True,
            "strict-route": False,
            "mtu": tun_mtu,
        },
        "dns": {
            "enable": True,
            "listen": f"{dns_bind}:53",
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": fake_ip_filter,
            "nameserver": ["77.88.8.8", "77.88.8.1"],
            "default-nameserver": ["77.88.8.8"],
        },
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "GEO",
                "type": "select",
                "proxies": (["socks-out", "DIRECT"] if proxies else ["DIRECT"]),
            }
        ],
        "rule-providers": {
            "geo-domains": {
                "type": "file",
                "behavior": "domain",
                "format": "text",
                "path": "/etc/routeweave/lists/geo.txt",
                "interval": 300,
            },
            "dpi-domains": {
                "type": "file",
                "behavior": "domain",
                "format": "text",
                "path": "/etc/routeweave/lists/dpi.txt",
                "interval": 300,
            },
            "ru-domains": {
                "type": "file",
                "behavior": "domain",
                "format": "text",
                "path": "/etc/routeweave/lists/ru-direct.txt",
                "interval": 300,
            },
        },
        "rules": [
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",  # Tailscale CGNAT
            "IP-CIDR,fe80::/10,DIRECT,no-resolve",
        ],
    }

    # BASE_DOMAIN — служебный домен CF Worker: всегда напрямую, мимо гео-правил.
    if base_domain:
        config["rules"].append(f"DOMAIN-SUFFIX,{base_domain},DIRECT")

    config["rules"] += [
        "RULE-SET,geo-domains,GEO",
        "RULE-SET,dpi-domains,DIRECT",
        "RULE-SET,ru-domains,DIRECT",
        "GEOIP,RU,DIRECT",
        "MATCH,DIRECT",
    ]

    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(
                "# Сгенерировано render_mihomo.py — правки вносить в /etc/routeweave/.env\n"
            )
            f.write("# или через веб-панель (списки). Ручные правки будут потеряны.\n")
            yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
        # В конфиге лежит Bearer-секрет API mihomo: читать его должен только
        # root. Install.sh делает install -m 0600, но при ручном запуске
        # (отладка, регенерация) права надо выставить сами.
        os.chmod(args.out, 0o600)
    except OSError as exc:
        print(
            f"ошибка: не удалось записать {args.out} или выставить права 0600: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {args.out} (geo -> {geo_target})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
