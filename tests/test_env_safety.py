"""Безопасность записи и чтения /etc/routeweave/.env (H-01).

Раньше .env писался в двойных кавычках с экранированием только ``\\`` и
``"``, а установщик исполнял его как shell от root: значение вида
``socks5://u:$(id)@h:1080`` давало root RCE. Теперь:

- ``env_line`` пишет ``KEY='value'`` и экранирует ``'`` как ``'\\''``;
- ``env_get`` читает .env построчно, без ``source``/``eval``, и разворачивает
  только кавычки;
- ``valid_socks``/``valid_base_domain``/``valid_tg_link_host`` пропускают
  лишь allowlist символов, так что shell-метасимволы до файла не доходят;
- в install.sh нет ``set -a`` и нет исполнения .env как shell-скрипта.

Тесты не требуют root и сети: функции извлекаются из install.sh и
выполняются в bash во временном каталоге.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install.sh"

# Минимальное окружение: тесты не должны зависеть от профиля пользователя.
BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
}

# env_line вызывает die при недопустимом значении; в песочнице подменяем.
DIE_STUB = 'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'


def extract_function(func_name, script=INSTALL):
    """Собрать одну функцию из скрипта по имени (как в tests/test_install_dns.py)."""
    text = script.read_text(encoding="utf-8")
    # После "{" может быть комментарий (например, у valid_socks).
    func = re.search(
        rf"^{re.escape(func_name)}\(\) \{{[^\n]*\n.*?^\}}\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert func, f"{script.name}: не найдена функция {func_name}"
    return func.group(0)


def run_bash(script, *args, env=None):
    """Выполнить bash-скрипт с аргументами; вернуть завершённый процесс."""
    full_env = dict(BASE_ENV)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script, "bash", *map(str, args)],
        check=False,
        text=True,
        capture_output=True,
        env=full_env,
    )


def run_function(func_name, *args, env=None, prelude=""):
    """Выполнить извлечённую функцию в bash и вернуть процесс."""
    script = prelude + extract_function(func_name) + f'\n{func_name} "$@"\n'
    return run_bash(script, *args, env=env)


def write_with_env_line(tmp_path, key, value):
    """Записать одну строку через env_line и вернуть (процесс, файл)."""
    out = tmp_path / ".env"
    script = (
        DIE_STUB
        + extract_function("env_line")
        + '\nenv_line "$1" "$2" > "$3"\n'
    )
    proc = run_bash(script, key, value, out)
    return proc, out


# ---------------------------------------------------------------- env_line


def test_env_line_does_not_execute_command_substitution(tmp_path):
    """$(touch ...) в значении остаётся текстом: маркер не создаётся."""
    marker = tmp_path / "pwned"
    value = f"socks5://u:$(touch {marker})@h:1080"

    proc, out = write_with_env_line(tmp_path, "SOCKS_URL", value)
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == f"SOCKS_URL='{value}'\n"

    src = run_bash('. "$1"; printf %s "$SOCKS_URL"', out)
    assert src.returncode == 0, src.stderr
    assert src.stdout == value, "значение из .env изменилось при source"
    assert not marker.exists(), "код из значения .env был исполнен"


def test_env_line_uses_single_quotes(tmp_path):
    """Формат строки — KEY='value', без двойных кавычек."""
    proc, out = write_with_env_line(tmp_path, "LAN_IFACE", "eth0")
    assert proc.returncode == 0, proc.stderr
    raw = out.read_text(encoding="utf-8")
    assert raw == "LAN_IFACE='eth0'\n"
    assert "=\"eth0\"" not in raw

    proc, out = write_with_env_line(tmp_path, "SOCKS_URL", "")
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == "SOCKS_URL=''\n"


@pytest.mark.parametrize(
    "value",
    [
        "pa'ss",
        "a'b'c",
        "quote' end and 'another",
        "x'$(touch /tmp/routeweave-pwned)'y",
        "a$HOME `id` \"q\" \\ end",
        "100% 'литерал'",
    ],
)
def test_env_line_quote_roundtrip(tmp_path, value):
    """Одинарная кавычка экранируется как '\\'' — source даёт исходник."""
    proc, out = write_with_env_line(tmp_path, "PANEL_PASS", value)
    assert proc.returncode == 0, proc.stderr

    raw = out.read_text(encoding="utf-8")
    assert raw.startswith("PANEL_PASS='")
    assert raw.endswith("'\n")
    # Внутри значения не остаётся неэкранированной кавычки: каждая одиночная
    # кавычка исходника превратилась в четыре символа  ' \ ' '.
    assert "'\\''" in raw or "'" not in value

    src = run_bash('. "$1"; printf %s "$PANEL_PASS"', out)
    assert src.returncode == 0, src.stderr
    assert src.stdout == value


@pytest.mark.parametrize("value", ["line1\nline2", "a\rb", "\n", "trailing\n"])
def test_env_line_rejects_newline(tmp_path, value):
    """Перевод строки/CR в значении — ошибка: одна запись = одна строка."""
    proc, out = write_with_env_line(tmp_path, "SOCKS_URL", value)
    assert proc.returncode != 0
    assert "перевод строки" in proc.stderr
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_env_line_does_not_use_esc():
    """env_line не опирается на старый esc() с двойными кавычками."""
    func = extract_function("env_line")
    assert "esc " not in func
    assert 'printf "%s=\'%s\'\\n"' in func


# ---------------------------------------------------------------- env_get


ENV_FIXTURE = (
    "# комментарий\n"
    "SOCKS_URL='socks5://u:p@h:1080'\n"
    'BASE_DOMAIN="example.com"\n'
    "PANEL_PASS='pa'\\''ss'\n"
    "EMPTY=''\n"
    "PLAIN=value\n"
    "EXEC='$(touch {marker})'\n"
    "DOUBLE=\"a\\\"b\\\\c\"\n"
)


@pytest.fixture()
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        ENV_FIXTURE.format(marker=tmp_path / "pwned-env-get"), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("SOCKS_URL", "socks5://u:p@h:1080"),
        ("BASE_DOMAIN", "example.com"),
        ("PANEL_PASS", "pa'ss"),
        ("EMPTY", ""),
        ("PLAIN", "value"),
        ("DOUBLE", 'a"b\\c'),
    ],
)
def test_env_get_reads_values(env_file, key, expected):
    """env_get снимает кавычки и разворачивает экранирование."""
    proc = run_function(
        "env_get", key, "", env={"ENV_FILE": str(env_file)}
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected


def test_env_get_does_not_execute_command_substitution(env_file):
    """$(touch ...) в .env возвращается текстом и не исполняется."""
    marker = env_file.parent / "pwned-env-get"
    proc = run_function(
        "env_get", "EXEC", "", env={"ENV_FILE": str(env_file)}
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"$(touch {marker})"
    assert not marker.exists(), "env_get исполнил код из значения .env"


def test_env_get_default_for_absent_key(env_file):
    proc = run_function(
        "env_get", "MISSING", "fallback", env={"ENV_FILE": str(env_file)}
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "fallback"


def test_env_get_default_for_absent_file(tmp_path):
    proc = run_function(
        "env_get", "MISSING", "fallback", env={"ENV_FILE": str(tmp_path / "nope")}
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "fallback"


def test_env_get_does_not_use_eval_or_source():
    func = extract_function("env_get")
    assert not re.search(r"\beval\b", func)
    assert not re.search(r"\bsource\b", func)
    assert not re.search(r"^\s*\.\s", func, re.MULTILINE)


# ---------------------------------------------------------------- valid_socks


@pytest.mark.parametrize(
    "url",
    [
        # https молча стал бы http: TLS к прокси не поддерживается.
        "https://h:1080",
        # shell-метасимволы и подстановки команд.
        "socks5://u:$(id)@h:1080",
        "socks5://h:1080`id`",
        "socks5://h:1080$(id)",
        "socks5://h:1080;id",
        "socks5://h:1080|id",
        "socks5://h:1080&id",
        "socks5://h:1080<id",
        "socks5://h:1080>id",
        "socks5://h:1080(1)",
        "socks5://h:1080{1}",
        # пробелы, кавычки, обратный слэш, перевод строки.
        " socks5://h:1080",
        "socks5://h:1080 x",
        "socks5://h:1080'",
        'socks5://h:1080"',
        "socks5://h:1080\\",
        "socks5://h:1080\n",
        # структурно неверные URL.
        "socks5://h",
        "socks5://h:0",
        "socks5://h:70000",
        "example.com:1080",
    ],
)
def test_valid_socks_rejects(url):
    proc = run_function("valid_socks", url)
    assert proc.returncode != 0, f"valid_socks принял небезопасный URL: {url!r}"


def test_valid_socks_explains_https_rejection():
    proc = run_function("valid_socks", "https://h:1080")
    assert proc.returncode != 0
    assert "https:// не поддерживается" in proc.stderr


@pytest.mark.parametrize(
    "url",
    [
        "",
        "socks5h://u:p@h:1080",
        "socks5://u:p@1.2.3.4:1080",
        "socks://h:1080",
        "http://h:8080",
        "socks5://user:p%40ss@proxy.example:1080",
    ],
)
def test_valid_socks_accepts(url):
    proc = run_function("valid_socks", url)
    assert proc.returncode == 0, proc.stderr


# ------------------------------------------------------------ valid_base_domain


@pytest.mark.parametrize(
    "domain",
    ["", "example.com", "a-b.c.d", "a.com", "sub.domain.example.org", "localhost"],
)
def test_valid_base_domain_accepts(domain):
    proc = run_function("valid_base_domain", domain)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "domain",
    [
        "A.com",
        "-a.com",
        "a-.com",
        "a..com",
        ".com",
        "a.com.",
        "a_b.com",
        "x$(id).com",
        "x`id`.com",
        "a b.com",
        "a\\.com",
        # метка длиннее 63 символов.
        ("a" * 64) + ".com",
        # имя длиннее 253 символов.
        ("a." * 127) + "com",
    ],
)
def test_valid_base_domain_rejects(domain):
    proc = run_function("valid_base_domain", domain)
    assert proc.returncode != 0, f"valid_base_domain принял: {domain!r}"


# ----------------------------------------------------------- valid_tg_link_host


def tg_link_host_prelude():
    """valid_tg_link_host использует valid_ipv4 — подкладываем её в песочницу."""
    return extract_function("valid_ipv4") + "\n"


def run_tg_link_host(host):
    return run_function("valid_tg_link_host", host, prelude=tg_link_host_prelude())


@pytest.mark.parametrize(
    "host",
    ["", "192.168.1.10", "tg.example.com", "example", "a-b.c"],
)
def test_valid_tg_link_host_accepts(host):
    proc = run_tg_link_host(host)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "host",
    ["$(id)", "`id`", "a b", "a;b", "999.1.1.1", "a..b", "-a.com", "a-.com"],
)
def test_valid_tg_link_host_rejects(host):
    proc = run_tg_link_host(host)
    assert proc.returncode != 0, f"valid_tg_link_host принял: {host!r}"


# ---------------------------------------------------------------- статика


def test_install_never_executes_env():
    """Регрессия H-01: .env не исполняется как shell (set -a/. /source)."""
    text = INSTALL.read_text(encoding="utf-8")
    assert "set -a" not in text, "в install.sh остался set -a"
    assert not re.search(
        r'^\s*(?:\.|source)\s+"?\$(?:\{?ETC\}?|ENV_FILE)', text, re.MULTILINE
    ), "install.sh исполняет .env как shell"
    assert not re.search(
        r'^\s*(?:\.|source)\s+[^\n]*\.env', text, re.MULTILINE
    ), "install.sh исполняет .env как shell"


def test_install_reads_env_only_through_env_get():
    """Новые ключи читаются через env_get, а не через source."""
    text = INSTALL.read_text(encoding="utf-8")
    for key in (
        "LAN_IFACE",
        "SERVER_IP",
        "SOCKS_URL",
        "BASE_DOMAIN",
        "PANEL_PASS",
        "PANEL_BIND",
        "MIHOMO_API",
        "SSH_PORT",
        "WAN_INGRESS",
        "WAN_PORTS",
    ):
        assert re.search(rf'^{key}=\$\(env_get {key}\b', text, re.MULTILINE), (
            f"{key} не читается через env_get"
        )


def test_managed_keys_are_upserted_on_reinstall():
    """Повторная установка обновляет все управляемые ключи (а не только 2)."""
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index('warn "$ETC/.env уже существует')
    end = text.index('chown root:routeweave "$ETC/.env"')
    branch = text[start:end]
    for key in (
        "LAN_IFACE",
        "SERVER_IP",
        "SOCKS_URL",
        "BASE_DOMAIN",
        "WG_SUBNET",
        "PANEL_PORT",
        "PANEL_USER",
        "PANEL_PASS",
        "MIHOMO_SECRET",
        "TG_SECRET",
        "QUIC_DROP",
        "PANEL_BIND",
        "MIHOMO_API",
        "SSH_PORT",
    ):
        assert re.search(rf"\b{key}\b", branch), f"{key} не обновляется при переустановке"
    assert re.search(r"env_upsert\s+\"\$ETC/\.env\"", branch)
    # Бэкап .env — один на прогон и до изменений.
    assert branch.index("env_backup_once") < branch.index("env_upsert")
