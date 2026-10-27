"""Периметр установщика: маркер владения, WAN-метка и защитные проверки.

Тесты не требуют root и сети: функции извлекаются из ``install.sh`` и
выполняются в bash во временном каталоге, статические инварианты проверяются
по тексту скрипта (как в ``tests/test_env_safety.py``).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install.sh"
COMPOSE = REPO_ROOT / "docker-compose.yml"

BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
}


def extract_function(func_name, script=INSTALL):
    """Собрать одну функцию из скрипта по имени."""
    text = script.read_text(encoding="utf-8")
    func = re.search(
        rf"^{re.escape(func_name)}\(\) \{{[^\n]*\n.*?^\}}\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert func, f"{script.name}: не найдена функция {func_name}"
    return func.group(0)


def run_bash(script, *args):
    return subprocess.run(
        ["bash", "-c", script, "bash", *map(str, args)],
        check=False,
        text=True,
        capture_output=True,
        env=BASE_ENV,
    )


def marker_script(actions):
    """Скрипт с функциями маркера и заданными действиями."""
    return (
        'OWNED="$1"\n'
        + extract_function("owned_lookup")
        + extract_function("mark_owned")
        + extract_function("claim_path")
        + actions
    )


def read_marker(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------- маркер владения


def test_mark_owned_is_idempotent_and_first_wins(tmp_path):
    owned = tmp_path / "owned"
    proc = run_bash(
        marker_script(
            'mark_owned "/opt/zapret" routeweave\n'
            'mark_owned "/opt/zapret" routeweave\n'
            'mark_owned "/opt/zapret" preexisting\n'
        ),
        owned,
    )
    assert proc.returncode == 0, proc.stderr
    lines = read_marker(owned)
    assert lines == ["/opt/zapret\trouteweave"]


def test_mark_owned_does_not_downgrade_preexisting(tmp_path):
    owned = tmp_path / "owned"
    proc = run_bash(
        marker_script(
            'mark_owned "/usr/local/bin/mihomo" preexisting\n'
            'mark_owned "/usr/local/bin/mihomo" routeweave\n'
        ),
        owned,
    )
    assert proc.returncode == 0, proc.stderr
    assert read_marker(owned) == ["/usr/local/bin/mihomo\tpreexisting"]


def test_claim_path_absent_is_routeweave(tmp_path):
    owned = tmp_path / "owned"
    target = tmp_path / "new-binary"
    proc = run_bash(
        marker_script('printf "%s" "$(claim_path "$2")"\n'),
        owned,
        target,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "routeweave"
    assert read_marker(owned) == [f"{target}\trouteweave"]


def test_claim_path_existing_is_preexisting_and_sticky(tmp_path):
    owned = tmp_path / "owned"
    target = tmp_path / "existing"
    target.write_text("foreign\n", encoding="utf-8")
    proc = run_bash(
        marker_script(
            'printf "%s\\n" "$(claim_path "$2")"\n'
            'printf "%s\\n" "$(claim_path "$2")"\n'
        ),
        owned,
        target,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["preexisting", "preexisting"]
    # Ровно одна запись на путь, состояние не переписано.
    assert read_marker(owned) == [f"{target}\tpreexisting"]


def test_marker_ignores_comments_and_blank_lines(tmp_path):
    owned = tmp_path / "owned"
    owned.write_text(
        "# комментарий\n\n/opt/zapret\tpreexisting\n", encoding="utf-8"
    )
    proc = run_bash(
        marker_script('printf "%s" "$(claim_path "/opt/zapret")"\n'),
        owned,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "preexisting"
    assert read_marker(owned) == [
        "# комментарий",
        "/opt/zapret\tpreexisting",
    ]


def test_marker_contract_matches_uninstall(tmp_path):
    """install.sh пишет маркер так, как его читает uninstall.sh (owned_state)."""
    uninstall = REPO_ROOT / "uninstall.sh"
    owned = tmp_path / "owned"
    fresh = tmp_path / "fresh"
    foreign = tmp_path / "foreign"
    foreign.write_text("x\n", encoding="utf-8")

    # Маркер создаётся тем же кодом, что и в установщике.
    proc = run_bash(
        marker_script(
            'claim_path "$2" >/dev/null\n'
            'claim_path "$3" >/dev/null\n'
        ),
        owned,
        fresh,
        foreign,
    )
    assert proc.returncode == 0, proc.stderr
    text = owned.read_text(encoding="utf-8")
    assert f"{fresh}\trouteweave\n" in text
    assert f"{foreign}\tpreexisting\n" in text

    # uninstall.sh читает ровно этот формат.
    script = (
        f"OWNED_MARKER={owned}\n"
        + extract_function("owned_state", uninstall)
        + '\nprintf "%s|%s|%s\\n" "$(owned_state "$1")" '
        '"$(owned_state "$2")" "$(owned_state "$3")"\n'
    )
    proc = run_bash(script, fresh, foreign, tmp_path / "absent")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "routeweave|preexisting|unknown"


# ------------------------------------------------------------- WAN_INGRESS_MARK


@pytest.mark.parametrize(
    "mark",
    ["0x200000", "0x200001", "0x10000000", "0x20"],
)
def test_valid_wan_mark_accepts_free_bits(mark):
    script = extract_function("valid_wan_mark") + '\nvalid_wan_mark "$1"\n'
    proc = run_bash(script, mark)
    assert proc.returncode == 0, (mark, proc.stderr)


@pytest.mark.parametrize(
    "mark",
    [
        "0x0",  # нулевая метка
        "0x40000",  # tailscale subnet-route
        "0x80000",  # zapret
        "0x100000",  # NAT exit node
        "0x240000",  # пересечение с 0x40000
        "0x180000",  # пересечение с 0x80000 и 0x100000
        "0xffffffff",  # пересечение со всеми
        "0x",  # не hex
        "0xZZ",  # не hex
        "40000",  # нет префикса
        "",  # пусто
    ],
)
def test_valid_wan_mark_rejects_reserved(mark):
    script = extract_function("valid_wan_mark") + '\nvalid_wan_mark "$1"\n'
    proc = run_bash(script, mark)
    assert proc.returncode != 0, (mark, proc.stdout)


def test_wan_mark_reserved_message_and_env_check():
    text = INSTALL.read_text(encoding="utf-8")
    assert "пересекается с зарезервированной меткой" in text
    assert "0x40000 tailscale, 0x80000 zapret, 0x100000 NAT" in text
    assert "используйте 0x200000" in text
    # Проверка выполняется при валидации прочитанного .env, а не только при вводе.
    assert (
        'require_valid_optional WAN_INGRESS_MARK "$WAN_INGRESS_MARK" '
        "valid_wan_mark" in text
    )
    # Пример в комментарии записи .env больше не предлагает занятую метку.
    assert "Метка connmark для WAN_INGRESS (например 0x200000)" in text


def test_wan_mark_migrates_legacy_env_default():
    """Старый .env из main с 0x40000 не должен валить установку.

    Миграция допустима только для значения из файла: явный 0x40000 в
    окружении установщика остаётся осознанным override и отвергается
    valid_wan_mark (см. test_valid_wan_mark_rejects_reserved).
    """
    text = INSTALL.read_text(encoding="utf-8")
    env_mark = 'WAN_INGRESS_MARK_ENV="${WAN_INGRESS_MARK:-}"'
    assert env_mark in text
    # Источник запоминается до чтения .env, иначе различие потеряется.
    env_pos = text.find(env_mark)
    get_pos = text.find(
        'WAN_INGRESS_MARK=$(env_get WAN_INGRESS_MARK', env_pos
    )
    assert get_pos > env_pos
    assert '[ -z "$WAN_INGRESS_MARK_ENV" ]' in text
    assert '[ "${WAN_INGRESS_MARK,,}" = "0x40000" ]' in text
    assert "WAN_INGRESS_MARK=0x200000" in text
    assert "мигрирую на 0x200000" in text
    # Миграция не отменяет строгую валидацию остальных зарезервированных меток.
    assert (
        'require_valid_optional WAN_INGRESS_MARK "$WAN_INGRESS_MARK" '
        "valid_wan_mark" in text
    )


# ------------------------------------------------------------ MIHOMO_API


@pytest.mark.parametrize(
    ("bind", "server_ip", "expected"),
    [
        ("", "192.0.2.10", "http://192.0.2.10:9090"),
        ("127.0.0.1", "192.0.2.10", "http://127.0.0.1:9090"),
        ("0.0.0.0", "192.0.2.10", "http://127.0.0.1:9090"),
        ("localhost", "192.0.2.10", "http://localhost:9090"),
        ("192.0.2.20", "192.0.2.10", "http://192.0.2.20:9090"),
    ],
)
def test_default_mihomo_api_follows_bind(bind, server_ip, expected):
    script = extract_function("default_mihomo_api")
    script += '\ndefault_mihomo_api "$1" "$2"\n'
    proc = run_bash(script, bind, server_ip)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected


def valid_api_url_script():
    """Скрипт с настоящей valid_api_url: код возврата — вердикт валидатора."""
    return extract_function("valid_api_url") + '\nvalid_api_url "$1"\n'


@pytest.mark.parametrize(
    "url",
    [
        "http://198.51.100.10:9090",
        "http://192.0.2.10:9090",
        "https://panel.example.com:9090",
        "http://127.0.0.1:9090",
        "http://localhost:9090",
        "http://[::1]:9090",
        "http://host:9090/path?x=1",
    ],
)
def test_valid_api_url_accepts_host_port(url):
    # Валидатор выполняется в bash, а не проверяется по тексту скрипта:
    # сломанный POSIX-класс символов иначе остаётся незамеченным.
    proc = run_bash(valid_api_url_script(), url)
    assert proc.returncode == 0, f"valid_api_url отклонила {url!r}: {proc.stderr}"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "198.51.100.10:9090",
        "ftp://h:1",
        "http://",
        "http://h:90 90",
        "http://h:9090;rm -rf /",
        "http://h:9090|id",
        "http://h:9090$(id)",
        "http://h:9090&x",
    ],
)
def test_valid_api_url_rejects_garbage(url):
    # Мусор и попытки инъекции (;, |, $(), &) не должны проходить в .env.
    proc = run_bash(valid_api_url_script(), url)
    assert proc.returncode != 0, f"valid_api_url приняла {url!r}"


def test_valid_api_url_accepts_plain_host_port_regression():
    """Регрессия: класс символов нельзя закрывать на ``\\]``.

    В POSIX bracket expression ``\\]`` не экранирует ``]``, а завершает класс:
    остаток `` -]`` становился обязательным хвостом шаблона. В итоге не
    принимался ни один адрес, и установка «из коробки» падала на дефолтном
    ``http://192.0.2.10:9090``. При откате правки падает именно этот тест.
    """
    func = extract_function("valid_api_url")
    assert "\\]" not in func, "класс символов закрыт через экранированную ]"
    proc = run_bash(func + '\nvalid_api_url "$1"\n', "http://192.0.2.10:9090")
    assert proc.returncode == 0, proc.stderr


def test_explicit_mihomo_api_is_not_overridden():
    text = INSTALL.read_text(encoding="utf-8")
    # Дефолт вычисляется только при пустом MIHOMO_API, явное значение из .env
    # не перетирается.
    assert 'if [ -z "$MIHOMO_API" ]; then' in text
    assert 'MIHOMO_API=$(default_mihomo_api "$MIHOMO_BIND" "$SERVER_IP")' in text
    assert 'MIHOMO_API="${MIHOMO_API:-http://$SERVER_IP:9090}"' not in text


def test_mihomo_api_still_validated_and_written():
    text = INSTALL.read_text(encoding="utf-8")
    assert (
        'require_valid MIHOMO_API "$MIHOMO_API" valid_api_url' in text
    )
    assert 'env_line MIHOMO_API "$MIHOMO_API"' in text
    assert "PANEL_BIND MIHOMO_API SSH_PORT" in text


# ------------------------------------------------------------ статика install.sh


def test_compose_gets_server_ip_inline():
    text = INSTALL.read_text(encoding="utf-8")
    assert 'SERVER_IP="$SERVER_IP" docker compose up -d' in text
    # compose не должен разбирать .env с секретами как --env-file.
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "--env-file" not in line, line
    assert "docker inspect -f '{{.State.Running}}' routeweave-tgwsproxy" in text
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "${SERVER_IP:?SERVER_IP не задан}:1443:1443" in compose


def test_active_units_are_restarted_not_only_enabled():
    text = INSTALL.read_text(encoding="utf-8")
    assert "systemctl restart routeweave-firewall" in text
    assert "systemctl restart mihomo zapret-nfqws routeweave-panel" in text
    # enable --now для юнитов больше не используется (таймер — можно).
    assert (
        "systemctl enable --now routeweave-firewall mihomo zapret-nfqws "
        "routeweave-panel" not in text
    )
    assert "systemctl enable --now routeweave-lists-update.timer" in text


def test_healthcheck_uses_panel_port_and_accepts_401():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.find('PANEL_CHECK_URL="http://')
    end = text.find("MIHOMO_CHECK_HOST=", start)
    assert start != -1 and end > start, "не найден health-check панели"
    block = text[start:end]
    assert "${PANEL_PORT}" in block
    assert "200|401" in block
    assert "--max-time 2" in block
    assert "journalctl -u routeweave-panel -n 30" in block


def test_healthcheck_checks_mihomo_tcp_without_secret_in_argv():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.find("MIHOMO_CHECK_HOST=")
    end = text.find("if [ \"$PORT53_BLOCKED\"", start)
    assert start != -1 and end > start
    block = text[start:end]
    assert "/dev/tcp/$MIHOMO_CHECK_HOST/9090" in block
    assert "timeout 1" in block
    assert "MIHOMO_SECRET" not in block
    assert "Authorization: Bearer" not in text
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "MIHOMO_SECRET" in line and "curl" in line:
            pytest.fail(f"MIHOMO_SECRET попадает в argv curl: {line}")


def test_preflight_rejects_symlinks_and_checks_compose():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.find("preflight() {")
    end = text.find('\npreflight "$APP_DIR"', start)
    assert start != -1 and end != -1
    block = text[start:end]
    assert '[ -L "$root/$f" ]' in block
    assert "-type l" in block
    assert "docker-compose.yml" in block
    assert "${INSTALL_TGWS:-no}" in block
    assert "warn_world_writable" in block
    assert "checkout доступен на запись другим пользователям" in text


def test_install_wires_guard_and_diag():
    """Сторож и снимок диагностики: preflight, установка, таймер, sudoers."""
    text = INSTALL.read_text(encoding="utf-8")

    start = text.find("preflight() {")
    end = text.find('\npreflight "$APP_DIR"', start)
    assert start != -1 and end > start, "не найден блок preflight"
    preflight = text[start:end]
    for template in (
        "templates/routeweave-guard.sh",
        "templates/routeweave-guard.service",
        "templates/routeweave-guard.timer",
        "templates/routeweave-diag-dump.sh",
    ):
        assert template in preflight, f"preflight не проверяет {template}"

    # Helper'ы ставятся в /usr/local/sbin с правами 0755 — как update-lists.
    for name in ("routeweave-guard.sh", "routeweave-diag-dump.sh"):
        pattern = (
            rf'^install -m 0755 "\$APP_DIR/templates/{re.escape(name)}"\s+'
            rf"/usr/local/sbin/{re.escape(name)}$"
        )
        assert re.search(pattern, text, re.MULTILINE), name

    # Таймер включается идемпотентно; сам oneshot-сервис в health-check не
    # попадает (после запуска он inactive).
    assert "systemctl enable --now routeweave-guard.timer" in text

    # Каталог снимков: helper пишет от root, панель читает как группа.
    assert (
        "install -d -m 0750 -o root -g routeweave /var/log/routeweave" in text
    )

    # sudoers: helper снимка — без wildcard; сторож панели не нужен вовсе.
    sudoers = [line for line in text.splitlines() if "NOPASSWD" in line]
    diag = [line for line in sudoers if "routeweave-diag-dump.sh" in line]
    assert len(diag) == 1, sudoers
    assert "*" not in diag[0], diag[0]
    for line in sudoers:
        assert "routeweave-guard.sh" not in line, line


def test_install_claims_ownership_before_overwrite():
    text = INSTALL.read_text(encoding="utf-8")
    for path in (
        '"$ZAPRET_DIR"',
        '"$MIHOMO_BIN"',
    ):
        assert f"claim_path {path}" in text
    assert 'unit_path="/etc/systemd/system/$unit"' in text
    assert '"$BACKUP_DIR/$unit.orig"' in text
    # Ровно четыре пути, которые понимает uninstall (два каталога/бинарника и
    # два юнита), без лишних записей в маркер.
    marker_paths = re.findall(r'claim_path "(\$[A-Za-z_]+)"', text)
    assert marker_paths.count("$ZAPRET_DIR") == 1
    assert marker_paths.count("$MIHOMO_BIN") == 1
    assert "for unit in mihomo.service zapret-nfqws.service; do" in text


def test_port53_occupied_marks_failure():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.find("  other)")
    end = text.find("esac", start)
    assert start != -1 and end > start
    block = text[start:end]
    assert "FAIL=1" in block
    assert "PORT53_BLOCKED=1" in block
    assert "порт 53 занят неизвестным сервисом" in text


# ---------------------------------------------------------------- LAN_CIDR


def lan_cidr_script():
    """Настоящие helpers нормализации: код возврата и stdout — вердикт."""
    return (
        'warn() { printf "WARN:%s\\n" "$*" >&2; }\n'
        + extract_function("valid_cidr")
        + extract_function("canonical_cidr")
        + extract_function("is_private_cidr")
        + extract_function("normalize_lan_cidr")
        + '\nnormalize_lan_cidr "$1"\n'
    )


@pytest.mark.parametrize(
    ("cidr", "expected"),
    [
        ("10.0.0.0/8", True),
        ("10.20.30.0/24", True),
        ("172.16.0.0/12", True),
        ("172.31.255.0/24", True),
        ("192.168.0.0/16", True),
        ("192.168.1.0/24", True),
        ("172.32.0.0/16", False),  # вне 172.16/12
        ("192.169.0.0/16", False),  # вне 192.168/16
        ("203.0.113.0/24", False),  # публичный TEST-NET-3
        ("127.0.0.0/8", False),  # loopback не RFC1918
        ("169.254.0.0/16", False),  # link-local не RFC1918
        ("0.0.0.0/0", False),  # нулевая сеть
    ],
)
def test_is_private_cidr_only_rfc1918(cidr, expected):
    # is_private в ipaddress включает loopback/link-local, поэтому helper
    # обязан проверять именно три RFC1918-сети.
    script = extract_function("is_private_cidr") + '\nis_private_cidr "$1"\n'
    proc = run_bash(script, cidr)
    assert (proc.returncode == 0) is expected, (cidr, proc.returncode, proc.stderr)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.5/24", "192.168.1.0/24"),  # хост-биты сброшены
        ("10.1.2.3/8", "10.0.0.0/8"),
        ("172.20.5.5/16", "172.20.0.0/16"),
        ("192.168.1.5/24 10.1.2.3/8", "192.168.1.0/24 10.0.0.0/8"),
        ("10.1.2.3/8,192.168.1.5/24", "10.0.0.0/8 192.168.1.0/24"),
        ("10.0.0.0/8 10.0.0.0/8", "10.0.0.0/8"),  # дубликаты схлопнуты
    ],
)
def test_normalize_lan_cidr_canonicalizes_private_ranges(raw, expected):
    proc = run_bash(lan_cidr_script(), raw)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected


@pytest.mark.parametrize(
    "raw",
    [
        "203.0.113.0/24",  # публичный
        "8.8.8.8/32",  # публичный
        "0.0.0.0/0",  # нулевая сеть
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local
        "not-a-cidr",  # мусор
        "fe80::/10",  # IPv6 не поддерживается
    ],
)
def test_normalize_lan_cidr_drops_non_rfc1918(raw):
    proc = run_bash(lan_cidr_script(), raw)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "LAN_CIDR" in proc.stderr


def test_normalize_lan_cidr_keeps_private_part_of_mixed_list():
    proc = run_bash(lan_cidr_script(), "203.0.113.0/24,10.0.0.0/8")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "10.0.0.0/8"
    assert "203.0.113.0/24" in proc.stderr


def test_lan_cidr_read_derived_and_written():
    text = INSTALL.read_text(encoding="utf-8")
    # Читается из .env/окружения сразу после LAN_IFACE и нормализуется до
    # первого использования (ufw-блок идёт намного ниже).
    assert 'LAN_CIDR=$(env_get LAN_CIDR "${LAN_CIDR:-}")' in text
    assert 'LAN_CIDR=$(normalize_lan_cidr "$LAN_CIDR")' in text
    assert text.find('LAN_CIDR=$(env_get LAN_CIDR') < text.find(
        'lan_cidr_is_private "$LAN_CIDR"'
    )
    # Канонизация — через ipaddress (strict=False сбрасывает хост-биты).
    canon = extract_function("canonical_cidr")
    assert "ipaddress.IPv4Network(sys.argv[1], strict=False)" in canon
    private = extract_function("is_private_cidr")
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert net in private
    # Фолбэк из физического интерфейса: connected-маршрут и адрес NIC.
    assert 'ip -4 route show dev "$DETECTED_IFACE" scope link' in text
    assert 'ip -4 -o addr show dev "$DETECTED_IFACE" scope global' in text
    assert 'if is_private_cidr "$__lan_cidr"; then' in text
    # Публичный NIC: явное предупреждение про доступ только через туннели.
    assert "публичный интерфейс" in text
    assert "Tailscale/WDTT/SSH-туннель" in text
    # Запись в свежий .env и upsert существующего (в конце списка, чтобы не
    # ломать статическую проверку "PANEL_BIND MIHOMO_API SSH_PORT").
    assert 'env_line LAN_CIDR "$LAN_CIDR"' in text
    assert "PANEL_BIND MIHOMO_API SSH_PORT LAN_CIDR; do" in text


def test_ufw_lan_rules_are_guarded_by_private_check():
    text = INSTALL.read_text(encoding="utf-8")
    # Повторного вычисления LAN_CIDR из ip addr в ufw-блоке больше нет.
    assert 'LAN_CIDR=$(ip -4 addr show' not in text
    guard = 'if [ -n "$LAN_CIDR" ] && lan_cidr_is_private "$LAN_CIDR"; then'
    start = text.find(guard)
    assert start != -1, "ufw: нет guard-проверки приватности LAN_CIDR"
    # Разрешение LAN стоит ровно один раз и только внутри guard-блока.
    ufw_lan = 'ufw route allow in on "$LAN_IFACE"'
    assert text.count(ufw_lan) == 1
    else_pos = text.find("\n  else\n", start)
    assert else_pos > start, "guard-блок LAN_CIDR без else"
    guarded = text[start:else_pos]
    assert ufw_lan in guarded
    assert "LAN_OPEN=1" in guarded
    # Публичный/пустой список обнуляется до цикла открытия портов.
    assert 'LAN_CIDR=""' in text[else_pos : text.find("PORTS_TCP=", else_pos)]
    # LAN_CIDR раскрывается по словам: каждое правило — одна сеть.
    assert 'for src in "$WG_SUBNET" $LAN_CIDR "${TS_CIDR:-}"; do' in text
    assert '"$WG_SUBNET" "$LAN_CIDR"' not in text
    # Итоговая сводка не врёт про открытый LAN при пустом списке.
    assert 'LAN: не открыт' in text
    assert 'if [ "$LAN_OPEN" = 1 ]; then' in text


# ------------------------------------------------------- порядок ip_forward


def test_ip_forward_orig_captured_before_forwarding_enabled():
    """ip_forward.orig снимается до fw-base.sh/Tailscale/WDTT.

    Раньше захват стоял в ufw-разделе — после install_tailscale, чей
    `systemctl restart routeweave-firewall` уже включал ip_forward=1, и при
    переустановке в файл попадала единица вместо исходного значения.
    """
    text = INSTALL.read_text(encoding="utf-8")
    marker = 'if [ ! -e "$ETC/ip_forward.orig" ]'
    assert text.count(marker) == 1, "захват ip_forward.orig продублирован"
    assert text.count("tr -d '\\n' < /proc/sys/net/ipv4/ip_forward") == 1
    capture = text.find(marker)
    # Каталог $ETC уже создан, но параметры ещё не читались/не спрашивались.
    etc = text.find('install -d -m 0755 "$ETC"')
    assert 0 <= etc < capture

    for needle in (
        "install_tailscale()",  # определение: внутри перезапуск firewall
        "install_tailscale || FAIL=1",
        "install_wdtt || FAIL=1",
    ):
        idx = text.find(needle)
        assert idx != -1, f"не найден {needle}"
        assert capture < idx, f"{needle} раньше захвата ip_forward.orig"

    # Перезапуски внутри определений функций не выполняются при объявлении,
    # поэтому проверяем поток установки: строки верхнего уровня и вызов
    # внутри install_tailscale (именно он ломал переустановку).
    top_level = [
        m.start()
        for m in re.finditer(
            r"^systemctl restart routeweave-firewall", text, re.MULTILINE
        )
    ]
    assert top_level, "нет перезапуска routeweave-firewall в потоке установки"
    assert capture < min(top_level)
    ts_def = text.find("install_tailscale()")
    ts_restart = text.find("systemctl restart routeweave-firewall", ts_def)
    assert ts_restart != -1
    assert capture < ts_restart


# ------------------------------------------------------------ PANEL_BIND


def test_panel_bind_wildcard_warns_after_validation():
    text = INSTALL.read_text(encoding="utf-8")
    guard = 'if [ "$PANEL_BIND" = "0.0.0.0" ]; then'
    start = text.find(guard)
    assert start != -1, "нет предупреждения для PANEL_BIND=0.0.0.0"
    # Предупреждение стоит после require_valid PANEL_BIND.
    assert text.find('require_valid PANEL_BIND "$PANEL_BIND"') < start
    end = text.find("\nfi\n", start)
    assert end > start
    block = text[start:end]
    assert "слушает все интерфейсы" in block
    assert "ROUTEWEAVE-IN" in block
    assert "при его остановке порт открыт" in block
    assert "tailscale" in block


# --------------------------------------------------------------- пароль


def test_generated_panel_password_printed_only_in_tty():
    text = INSTALL.read_text(encoding="utf-8")
    guard = 'if [ -t 1 ] && [ "$PANEL_PASS_GENERATED" = 1 ]; then'
    pos = text.find(guard)
    assert pos != -1
    # Проверка TTY и признак генерации — в одной условной конструкции.
    assert "[ -t 1 ]" in text[pos : pos + 120]
    assert "PANEL_PASS_GENERATED" in text[pos : pos + 120]
    assert "см. $ETC/.env → PANEL_PASS" in text
    # --help больше не обещает пароль в логах/пайпах.
    assert "в пайпе/логе/CI его нет" in text
