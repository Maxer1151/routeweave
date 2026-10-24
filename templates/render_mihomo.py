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

Коды возврата: 0 — успех, 1 — ошибка разбора/записи, 2 — ошибка аргументов
(argparse).
"""

import argparse
import os
import sys
from urllib.parse import unquote, urlsplit

import yaml

# Схемы, которые принимаем в SOCKS_URL, и тип прокси в терминах mihomo.
#   * socks5h/socks -> socks5: mihomo для типа socks5 передаёт прокси имя
#     хоста, а не IP, то есть имя резолвится на стороне прокси — это ровно
#     семантика «h» в curl. Отдельного типа socks5h у mihomo нет.
#   * http/https -> http: отдельного https-типа у mihomo тоже нет, TLS к
#     самому прокси не поддерживается; https:// здесь означает «прокси,
#     которому нужен CONNECT», и трактуется как обычный http.
SCHEME_TO_TYPE = {
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks": "socks5",
    "http": "http",
    "https": "http",
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


class SocksUrlError(ValueError):
    """SOCKS_URL задан, но не разбирается."""


def load_env(path: str) -> dict:
    """Построчный разбор KEY=VALUE (комментарии и пустые строки пропускаем).

    Кавычки вокруг значения снимаем: install.sh делает `. .env`, то есть
    читает файл как shell-скрипт, где KEY="value" и KEY=value равнозначны.
    Без этого FAKEIP_EXTRA="a.com" дал бы домен с кавычкой внутри.
    """
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
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
    if scheme not in SCHEME_TO_TYPE:
        raise SocksUrlError(
            f"неподдерживаемая схема {scheme!r} "
            "(ожидается socks5, socks5h, socks, http или https)"
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

    config = {
        "mixed-port": 7890,
        "external-controller": "0.0.0.0:9090",
        "external-ui": "zashboard",
        "secret": data.get("MIHOMO_SECRET") or "",
        "allow-lan": True,
        "bind-address": "*",
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
            "auto-redirect": True,
            "auto-detect-interface": True,
            "strict-route": False,
            "mtu": 1500,
        },
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:53",
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
    except OSError as exc:
        print(f"ошибка: не удалось записать {args.out}: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {args.out} (geo -> {geo_target})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
