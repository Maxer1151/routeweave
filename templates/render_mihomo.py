#!/usr/bin/env python3
"""Генератор /etc/homegateway/config.yaml для mihomo.

Читает переменные из /etc/homegateway/.env и из окружения
(окружение имеет приоритет — так установщик прокидывает дефолты).
Запуск: render_mihomo.py <путь-к-.env> <выходной-config.yaml>
"""
import os
import sys

import yaml


def load_env(path: str) -> dict:
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def parse_socks(url: str):
    """socks5://user:pass@host:port | http://... -> dict для mihomo."""
    if "://" not in url:
        return None
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme not in ("socks5", "socks", "http", "https"):
        return None
    userinfo, _, hostport = rest.rpartition("@")
    user = password = ""
    if userinfo:
        user, _, password = userinfo.partition(":")
    host, _, port = hostport.partition(":")
    if not host or not port.isdigit():
        return None
    ptype = "socks5" if scheme.startswith("socks") else "http"
    proxy = {
        "name": "socks-out",
        "type": ptype,
        "server": host,
        "port": int(port),
        "udp": ptype == "socks5",
    }
    if user:
        proxy["username"] = user
        proxy["password"] = password
    return proxy


def main() -> int:
    env_file = sys.argv[1] if len(sys.argv) > 1 else "/etc/homegateway/.env"
    out_file = sys.argv[2] if len(sys.argv) > 2 else "/etc/homegateway/config.yaml"

    cfg_env = load_env(env_file)
    data = {**cfg_env, **os.environ}  # окружение важнее файла

    socks_url = (data.get("SOCKS_URL") or "").strip()
    base_domain = (data.get("BASE_DOMAIN") or "").strip()
    fakeip_extra = [s.strip() for s in (data.get("FAKEIP_EXTRA") or "").split(",") if s.strip()]

    fake_ip_filter = [
        "+.lan",
        "+.local",
        "+.msftconnecttest.com",
        "+.msftncsi.com",
        "+.ts.net",          # Tailscale MagicDNS
        "+.workgroup",       # SMB/local
    ] + fakeip_extra
    if base_domain:
        fake_ip_filter.append("+." + base_domain)

    proxies = []
    geo_target = "DIRECT"
    if socks_url:
        proxy = parse_socks(socks_url)
        if proxy:
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
                "type": "file", "behavior": "domain", "format": "text",
                "path": "/etc/homegateway/lists/geo.txt", "interval": 300,
            },
            "dpi-domains": {
                "type": "file", "behavior": "domain", "format": "text",
                "path": "/etc/homegateway/lists/dpi.txt", "interval": 300,
            },
            "ru-domains": {
                "type": "file", "behavior": "domain", "format": "text",
                "path": "/etc/homegateway/lists/ru-direct.txt", "interval": 300,
            },
        },
        "rules": [
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",   # Tailscale CGNAT
            "IP-CIDR,fe80::/10,DIRECT,no-resolve",
        ],
    }

    if base_domain:
        config["rules"].append(f"DOMAIN-SUFFIX,{base_domain},DIRECT")

    config["rules"] += [
        "RULE-SET,geo-domains,GEO",
        "RULE-SET,dpi-domains,DIRECT",
        "RULE-SET,ru-domains,DIRECT",
        "GEOIP,RU,DIRECT",
        "MATCH,DIRECT",
    ]

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("# Сгенерировано render_mihomo.py — правки вносить в /etc/homegateway/.env\n")
        f.write("# или через веб-панель (списки). Ручные правки будут потеряны.\n")
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)

    print(f"OK: {out_file} (geo -> {geo_target})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
