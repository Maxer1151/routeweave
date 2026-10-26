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
