"""Тесты генератора templates/render_mihomo.py.

Скрипт запускается подпроцессом с чистым окружением (без наследования
SOCKS_URL и прочих переменных хоста), поэтому тесты не зависят от
tests/conftest.py и от реального /etc/routeweave/.env.
"""

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# PyYAML нужен и скрипту, и тестам: без него честнее skip, чем ошибка сбора.
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "templates" / "render_mihomo.py"

# Минимальное окружение: ни одна переменная проекта не должна протечь.
BASE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}


def run_render(tmp_path, env_text="", extra_env=None, extra_args=()):
    """Записать .env, запустить скрипт, вернуть (процесс, путь к YAML)."""
    env_file = tmp_path / "routeweave.env"
    env_file.write_text(env_text, encoding="utf-8")
    out_file = tmp_path / "config.yaml"
    env = dict(BASE_ENV)
    env["HOME"] = str(tmp_path)
    env.update(extra_env or {})
    cmd = [sys.executable, str(SCRIPT), *extra_args, str(env_file), str(out_file)]
    proc = subprocess.run(
        cmd, check=False, capture_output=True, text=True, env=env, timeout=60
    )
    return proc, out_file


def load_ok(tmp_path, env_text="", extra_env=None, extra_args=()):
    proc, out_file = run_render(tmp_path, env_text, extra_env, extra_args)
    assert proc.returncode == 0, f"rc={proc.returncode}\nstderr={proc.stderr}"
    return yaml.safe_load(out_file.read_text(encoding="utf-8"))


def load_render_module():
    """Импортировать скрипт как модуль: нужны load_env/_parse_env_value."""
    spec = importlib.util.spec_from_file_location("render_mihomo_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "raw, expected",
    [
        # install.sh экранирует апостроф как '\'' — это один символ внутри.
        ("'a'\\''b'", "a'b"),
        # Соседние сегменты склеиваются в одно слово.
        ("'a'\"b\"", "ab"),
        ("plain", "plain"),
        ("", ""),
        # В двойных кавычках раскрываются только \" и \\.
        ('"a\\"b"', 'a"b'),
        ('"a\\\\b"', "a\\b"),
        ('"a\\nb"', "a\\nb"),
        # Вне кавычек обратный слэш экранирует любой следующий символ.
        ("a\\ b", "a b"),
        ("a\\'b", "a'b"),
        # Незакрытая кавычка — не падаем, возвращаем строку как есть.
        ("'unterminated", "'unterminated"),
        ('"unterminated', '"unterminated'),
    ],
)
def test_parse_env_value(raw, expected):
    assert load_render_module()._parse_env_value(raw) == expected


def test_env_single_quote_apostrophe_in_secret(tmp_path):
    # Файл содержит ровно то, что пишет install.sh: MIHOMO_SECRET='a'\''b'
    cfg = load_ok(tmp_path, "SOCKS_URL=\nMIHOMO_SECRET='a'\\''b'\n")
    assert cfg["secret"] == "a'b"


def test_env_single_quote_apostrophe_in_socks_url(tmp_path):
    cfg = load_ok(
        tmp_path,
        "SOCKS_URL='socks5h://u:p'\\''q@127.0.0.1:1080'\n",
    )
    proxy = cfg["proxies"][0]
    assert proxy["username"] == "u"
    # unquote не трогает апостроф: пароль должен дойти как p'q.
    assert proxy["password"] == "p'q"


def test_env_single_quoted_fakeip_extra(tmp_path):
    cfg = load_ok(tmp_path, "FAKEIP_EXTRA='+.a.example,+.b.example'\n")
    filt = cfg["dns"]["fake-ip-filter"]
    assert "+.a.example" in filt
    assert "+.b.example" in filt


def test_env_adjacent_segments_are_concatenated(tmp_path):
    # 'a'"b" — одно слово ab, а не два значения с кавычками.
    cfg = load_ok(tmp_path, "SOCKS_URL=\nMIHOMO_SECRET='a'\"b\"\n")
    assert cfg["secret"] == "ab"


def test_empty_socks_url_is_direct(tmp_path):
    cfg = load_ok(tmp_path, "SOCKS_URL=\nMIHOMO_SECRET=s3cret\n")
    assert cfg["proxies"] == []
    groups = {g["name"]: g for g in cfg["proxy-groups"]}
    assert groups["GEO"]["proxies"] == ["DIRECT"]
    # DIRECT — штатный режим, никаких предупреждений при заданном секрете.
    proc, _ = run_render(tmp_path, "SOCKS_URL=\nMIHOMO_SECRET=s3cret\n")
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "geo -> DIRECT" in proc.stdout


def test_empty_secret_warns_but_writes(tmp_path):
    # Секрет генерирует установщик; пустой — предупреждение, не ошибка.
    proc, out_file = run_render(tmp_path, "SOCKS_URL=\nMIHOMO_SECRET=\n")
    assert proc.returncode == 0, proc.stderr
    assert "MIHOMO_SECRET" in proc.stderr
    assert out_file.exists()


def test_socks5_with_auth(tmp_path):
    cfg = load_ok(tmp_path, "SOCKS_URL=socks5://user:pass@proxy.example:1080\n")
    assert cfg["proxies"] == [
        {
            "name": "socks-out",
            "type": "socks5",
            "server": "proxy.example",
            "port": 1080,
            "udp": True,
            "username": "user",
            "password": "pass",
        }
    ]
    groups = {g["name"]: g for g in cfg["proxy-groups"]}
    assert groups["GEO"]["proxies"] == ["socks-out", "DIRECT"]


def test_socks5h_maps_to_socks5(tmp_path):
    cfg = load_ok(tmp_path, "SOCKS_URL=socks5h://u:p@1.2.3.4:1080\n")
    assert cfg["proxies"][0]["type"] == "socks5"
    assert cfg["proxies"][0]["udp"] is True


@pytest.mark.parametrize(
    "url, expected_type, expected_udp",
    [
        ("socks5://h.example:1080", "socks5", True),
        ("socks://h.example:1080", "socks5", True),
        ("http://h.example:3128", "http", False),
    ],
)
def test_scheme_mapping(tmp_path, url, expected_type, expected_udp):
    cfg = load_ok(tmp_path, f"SOCKS_URL={url}\n")
    proxy = cfg["proxies"][0]
    assert proxy["type"] == expected_type
    assert proxy["udp"] is expected_udp
    assert proxy["server"] == "h.example"
    assert "username" not in proxy


def test_https_socks_url_fails(tmp_path):
    # TLS к самому прокси mihomo не умеет: молчаливый маппинг https -> http
    # создавал бы ложное впечатление шифрования канала.
    proc, out_file = run_render(tmp_path, "SOCKS_URL=https://h.example:8443\n")
    assert proc.returncode == 1, proc.stderr
    assert "TLS к SOCKS-прокси не поддерживается" in proc.stderr
    assert "socks5h://" in proc.stderr
    assert not out_file.exists(), "при ошибке конфиг писать нельзя"


def test_scheme_is_case_insensitive(tmp_path):
    cfg = load_ok(tmp_path, "SOCKS_URL=SOCKS5H://h.example:1080\n")
    assert cfg["proxies"][0]["type"] == "socks5"


def test_ipv6_host_without_brackets(tmp_path):
    proc, out_file = run_render(tmp_path, "SOCKS_URL=socks5://[::1]:1080\n")
    assert proc.returncode == 0, proc.stderr
    text = out_file.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    assert cfg["proxies"][0]["server"] == "::1"
    assert cfg["proxies"][0]["port"] == 1080
    assert "[::1]" not in text


def test_percent_encoded_credentials(tmp_path):
    cfg = load_ok(tmp_path, "SOCKS_URL=socks5://user:p%40ss%3A1@h.example:1080\n")
    proxy = cfg["proxies"][0]
    assert proxy["username"] == "user"
    assert proxy["password"] == "p@ss:1"


def test_raw_at_in_password(tmp_path):
    # urlsplit делит netloc по последнему @, поэтому «сырой» @ в пароле тоже
    # разбирается; надёжнее всё же percent-encoding (тест выше).
    cfg = load_ok(tmp_path, "SOCKS_URL=socks5://user:p@ss@h.example:1080\n")
    proxy = cfg["proxies"][0]
    assert proxy["username"] == "user"
    assert proxy["password"] == "p@ss"
    assert proxy["server"] == "h.example"


def test_empty_password_allowed(tmp_path):
    cfg = load_ok(tmp_path, "SOCKS_URL=socks5://user:@h.example:1080\n")
    assert cfg["proxies"][0]["username"] == "user"
    assert cfg["proxies"][0]["password"] == ""


@pytest.mark.parametrize(
    "url",
    [
        "socks5://host",  # нет порта
        "socks5://host:0",  # порт 0
        "socks5://host:70000",  # порт вне диапазона
        "ftp://h:1",  # схема не поддерживается
        "host:1080",  # нет схемы
        "socks5://:1080",  # нет хоста
        "socks5://h:1080/path",  # лишний путь
        "socks5://h:1080/?x=1",  # query
        "socks5://h:1080#frag",  # fragment
    ],
)
def test_invalid_socks_url_fails(tmp_path, url):
    proc, out_file = run_render(tmp_path, f"SOCKS_URL={url}\n")
    assert proc.returncode == 1, f"ожидался rc 1 для {url!r}, stderr={proc.stderr!r}"
    assert "SOCKS_URL" in proc.stderr
    assert "ошибка:" in proc.stderr
    assert not out_file.exists(), "при ошибке конфиг писать нельзя"


def test_env_file_wins_over_environment(tmp_path):
    cfg = load_ok(
        tmp_path,
        "BASE_DOMAIN=file.example\nMIHOMO_SECRET=file-secret\n",
        extra_env={"BASE_DOMAIN": "env.example", "MIHOMO_SECRET": "env-secret"},
    )
    assert "+.file.example" in cfg["dns"]["fake-ip-filter"]
    assert "+.env.example" not in cfg["dns"]["fake-ip-filter"]
    assert "DOMAIN-SUFFIX,file.example,DIRECT" in cfg["rules"]
    assert cfg["secret"] == "file-secret"


def test_env_wins_flag(tmp_path):
    cfg = load_ok(
        tmp_path,
        "BASE_DOMAIN=file.example\nMIHOMO_SECRET=file-secret\n",
        extra_env={"BASE_DOMAIN": "env.example", "MIHOMO_SECRET": "env-secret"},
        extra_args=["--env-wins"],
    )
    assert "+.env.example" in cfg["dns"]["fake-ip-filter"]
    assert "+.file.example" not in cfg["dns"]["fake-ip-filter"]
    assert "DOMAIN-SUFFIX,env.example,DIRECT" in cfg["rules"]
    assert cfg["secret"] == "env-secret"


def test_socks_url_from_environment_in_env_wins_mode(tmp_path):
    cfg = load_ok(
        tmp_path,
        "SOCKS_URL=\n",
        extra_env={"SOCKS_URL": "socks5://envproxy:1080"},
        extra_args=["--env-wins"],
    )
    assert cfg["proxies"][0]["server"] == "envproxy"


def test_fakeip_extra_normalized_and_deduped(tmp_path):
    # Значение в кавычках — как в обычном .env; install.sh читает файл как shell.
    cfg = load_ok(tmp_path, 'FAKEIP_EXTRA="a.com, b.com ,,a.com,+.lan"\n')
    filt = cfg["dns"]["fake-ip-filter"]
    # базовые — ровно по одному разу, extras в порядке появления, без пустых;
    # +.lan из FAKEIP_EXTRA не дублирует базовый
    assert filt == [
        "+.lan",
        "+.local",
        "+.msftconnecttest.com",
        "+.msftncsi.com",
        "+.ts.net",
        "+.workgroup",
        "+.a.com",
        "+.b.com",
    ]


def test_fakeip_extra_star_prefix_and_trailing_dot(tmp_path):
    cfg = load_ok(tmp_path, "FAKEIP_EXTRA=*.corp.example, bank.example.\n")
    filt = cfg["dns"]["fake-ip-filter"]
    assert "+.corp.example" in filt
    assert "+.bank.example" in filt
    assert "*.corp.example" not in filt


def test_full_config_structure(tmp_path):
    cfg = load_ok(
        tmp_path,
        "SOCKS_URL=socks5://user:pass@h.example:1080\n"
        "BASE_DOMAIN=cf.example\n"
        "MIHOMO_SECRET=topsecret\n",
    )
    assert cfg["tun"]["enable"] is True
    # DNS по умолчанию слушает все интерфейсы, API mihomo — только localhost.
    assert cfg["dns"]["listen"] == "0.0.0.0:53"
    assert cfg["external-controller"] == "127.0.0.1:9090"
    assert cfg["bind-address"] == "127.0.0.1"
    assert cfg["allow-lan"] is False
    assert cfg["tun"]["auto-redirect"] is True
    assert cfg["tun"]["mtu"] == 1280
    assert cfg["secret"] == "topsecret"
    assert cfg["mixed-port"] == 7890
    assert cfg["mode"] == "rule"
    assert cfg["dns"]["enhanced-mode"] == "fake-ip"

    rules = cfg["rules"]
    assert all(r.startswith("IP-CIDR,") for r in rules[:6])
    assert "DOMAIN-SUFFIX,cf.example,DIRECT" in rules
    assert (
        rules.index("RULE-SET,geo-domains,GEO")
        < rules.index("GEOIP,RU,DIRECT")
        < rules.index("MATCH,DIRECT")
    )
    assert rules[-1] == "MATCH,DIRECT"
    # BASE_DOMAIN идёт до RULE-SET, как и раньше
    assert rules.index("DOMAIN-SUFFIX,cf.example,DIRECT") < rules.index(
        "RULE-SET,geo-domains,GEO"
    )


def test_rule_providers_paths(tmp_path):
    cfg = load_ok(tmp_path)
    assert (
        cfg["rule-providers"]["geo-domains"]["path"] == "/etc/routeweave/lists/geo.txt"
    )
    assert (
        cfg["rule-providers"]["dpi-domains"]["path"] == "/etc/routeweave/lists/dpi.txt"
    )
    assert (
        cfg["rule-providers"]["ru-domains"]["path"]
        == "/etc/routeweave/lists/ru-direct.txt"
    )


def test_missing_env_file_is_not_fatal(tmp_path):
    out_file = tmp_path / "config.yaml"
    cmd = [sys.executable, str(SCRIPT), str(tmp_path / "nope.env"), str(out_file)]
    env = dict(BASE_ENV)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        cmd, check=False, capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    cfg = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert cfg["proxies"] == []


def test_bind_addresses_from_server_ip(tmp_path):
    cfg = load_ok(tmp_path, "SERVER_IP=192.168.1.10\nMIHOMO_SECRET=s\n")
    assert cfg["external-controller"] == "192.168.1.10:9090"
    assert cfg["bind-address"] == "192.168.1.10"
    # SERVER_IP больше не влияет на DNS: листенер слушает все интерфейсы.
    assert cfg["dns"]["listen"] == "0.0.0.0:53"


def test_dns_default_wildcard_does_not_warn(tmp_path):
    # 0.0.0.0 — штатный дефолт DNS, а не осознанный opt-in: предупреждения нет.
    proc, out_file = run_render(
        tmp_path, "SERVER_IP=192.168.1.10\nMIHOMO_SECRET=s\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    cfg = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert cfg["dns"]["listen"] == "0.0.0.0:53"


def test_dns_explicit_wildcard_warns(tmp_path):
    proc, out_file = run_render(
        tmp_path, "DNS_BIND=0.0.0.0\nMIHOMO_SECRET=s\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "DNS_BIND" in proc.stderr
    assert "0.0.0.0" in proc.stderr
    cfg = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert cfg["dns"]["listen"] == "0.0.0.0:53"


def test_bind_addresses_overridden_by_env(tmp_path):
    cfg = load_ok(
        tmp_path,
        "SERVER_IP=192.168.1.10\nMIHOMO_BIND=127.0.0.1\nDNS_BIND=192.168.1.11\n",
    )
    assert cfg["external-controller"] == "127.0.0.1:9090"
    assert cfg["bind-address"] == "127.0.0.1"
    assert cfg["dns"]["listen"] == "192.168.1.11:53"


def test_bind_localhost_allowed(tmp_path):
    cfg = load_ok(tmp_path, "MIHOMO_BIND=localhost\nDNS_BIND=localhost\n")
    assert cfg["external-controller"] == "localhost:9090"
    assert cfg["dns"]["listen"] == "localhost:53"


def test_bind_wildcard_warns(tmp_path):
    proc, out_file = run_render(tmp_path, "MIHOMO_BIND=0.0.0.0\n")
    assert proc.returncode == 0, proc.stderr
    assert "0.0.0.0" in proc.stderr
    assert "WAN" in proc.stderr
    cfg = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert cfg["external-controller"] == "0.0.0.0:9090"


@pytest.mark.parametrize("value", ["example.com", "192.168.1.300", "10.0.0.1:9090", "::1"])
def test_bind_invalid_fails(tmp_path, value):
    proc, out_file = run_render(tmp_path, f"MIHOMO_BIND={value}\n")
    assert proc.returncode == 1, proc.stderr
    assert "MIHOMO_BIND" in proc.stderr
    assert not out_file.exists()


def test_allow_lan_off_by_default(tmp_path):
    cfg = load_ok(tmp_path)
    assert cfg["allow-lan"] is False


def test_allow_lan_enabled_by_env(tmp_path):
    cfg = load_ok(tmp_path, "MIHOMO_ALLOW_LAN=yes\n")
    assert cfg["allow-lan"] is True


def test_allow_lan_garbage_is_off_and_warns(tmp_path):
    proc, out_file = run_render(tmp_path, "MIHOMO_ALLOW_LAN=maybe\n")
    assert proc.returncode == 0, proc.stderr
    assert "MIHOMO_ALLOW_LAN" in proc.stderr
    cfg = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert cfg["allow-lan"] is False


@pytest.mark.parametrize(
    "env_text, expected",
    [
        ("", True),
        ("WAN_INGRESS=yes\n", True),
        ("MIHOMO_AUTO_REDIRECT=no\n", False),
        ("MIHOMO_AUTO_REDIRECT=yes\n", True),
    ],
)
def test_auto_redirect_env(tmp_path, env_text, expected):
    # WAN_INGRESS больше не выключает auto-redirect: redirect не трогает
    # локальные адреса, а его отключение ломает локальный TCP и DNS.
    cfg = load_ok(tmp_path, env_text)
    assert cfg["tun"]["auto-redirect"] is expected


def test_auto_redirect_garbage_is_on_and_warns(tmp_path):
    proc, out_file = run_render(tmp_path, "MIHOMO_AUTO_REDIRECT=maybe\n")
    assert proc.returncode == 0, proc.stderr
    assert "MIHOMO_AUTO_REDIRECT" in proc.stderr
    cfg = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert cfg["tun"]["auto-redirect"] is True


def test_tun_mtu_default_and_custom(tmp_path):
    assert load_ok(tmp_path)["tun"]["mtu"] == 1280
    assert load_ok(tmp_path, "MIHOMO_TUN_MTU=1400\n")["tun"]["mtu"] == 1400


@pytest.mark.parametrize("value", ["100", "9001", "abc", "-1"])
def test_tun_mtu_invalid_fails(tmp_path, value):
    proc, out_file = run_render(tmp_path, f"MIHOMO_TUN_MTU={value}\n")
    assert proc.returncode == 1, proc.stderr
    assert "MIHOMO_TUN_MTU" in proc.stderr
    assert not out_file.exists()


def test_config_is_written_with_0600(tmp_path):
    proc, out_file = run_render(tmp_path, "MIHOMO_SECRET=s3cret\n")
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600


def test_help_and_bad_args(tmp_path):
    env = dict(BASE_ENV)
    env["HOME"] = str(tmp_path)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "--env-wins" in proc.stdout
    assert "ENV_FILE" in proc.stdout

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--nope"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 2
