"""Тесты helper'а состояния Tailscale и его установки (без root и сети).

Helper ``templates/routeweave-tailscale-state.sh`` только читает состояние:
``tailscale status --json``, ``tailscale debug prefs`` (лишь
``AdvertiseRoutes``), ``systemctl is-active tailscaled`` и счётчики sysfs.
Тесты подменяют PATH на временный каталог со стабами, поэтому настоящий
``tailscale``/``systemctl`` на хосте не вызывается, а пути к .env и sysfs
задаются через ``ROUTEWEAVE_TS_ENV`` / ``ROUTEWEAVE_TS_SYSFS``.

Дополнительно статически проверяется, что установщик копирует helper и
выдаёт панели точечное право sudo без wildcard, а uninstall удаляет файл.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "templates" / "routeweave-tailscale-state.sh"
FW_TAILSCALE = ROOT / "templates" / "fw-tailscale.sh"
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"

# Ответ tailscale status --json реального вида 1.104.x: CGNAT-адрес — заглушка
# из 100.64.0.0/10, имя пира фиктивное. Поля PrimaryRoutes у Self нет, Prefs
# верхнего уровня в ответе отсутствуют: одобрение видно только по
# ExitNodeOption, анонс — по `tailscale debug prefs`.
STATUS_JSON = json.dumps(
    {
        "BackendState": "Running",
        "Self": {
            "TailscaleIPs": ["100.64.0.10", "fd7a:115c::1"],
            "AllowedIPs": ["100.64.0.10/32", "fd7a:115c::1/128", "0.0.0.0/0", "::/0"],
            "ExitNodeOption": True,
        },
        "Peer": {"peer-a": {"HostName": "peer-a"}},
    },
    separators=(",", ":"),
)


def require_tool(name):
    """Путь к реальной утилите или skip, если её нет в системе."""
    path = shutil.which(name)
    if not path:
        pytest.skip(f"в системе нет {name}")
    return path


def write_stub(bin_dir, name, body):
    """Исполняемый bash-стаб с shebang и фиксированными правами."""
    path = bin_dir / name
    path.write_text(
        "#!/usr/bin/env bash\n" + textwrap.dedent(body).lstrip("\n"),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def make_path(
    tmp_path,
    with_tailscale=True,
    status_json=STATUS_JSON,
    prefs_json="",
    systemctl_out="active\n",
    tailscale_rc=0,
    env_content='TS_IFACE="tailscale0"\n',
    with_python=True,
):
    """Готовит PATH со стабами, .env и sysfs; возвращает окружение запуска."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # PATH содержит только этот каталог: настоящий tailscale не найдётся.
    # bash нужен и для shebang стабов, sed/head/python3 — самому helper'у.
    for name in ("bash", "sed", "head"):
        (bin_dir / name).symlink_to(require_tool(name))
    if with_python:
        (bin_dir / "python3").symlink_to(require_tool("python3"))

    write_stub(bin_dir, "systemctl", 'printf "%s" "$SYSTEMCTL_OUT"\n')
    if with_tailscale:
        write_stub(
            bin_dir,
            "tailscale",
            """\
            rc="${TAILSCALE_RC:-0}"
            if [ "$rc" != "0" ]; then
              exit "$rc"
            fi
            if [ "$1" = "status" ]; then
              printf '%s' "$TS_STUB_JSON"
            elif [ "$1" = "debug" ] && [ "$2" = "prefs" ]; then
              printf '%s' "${TS_STUB_PREFS_JSON:-}"
            fi
            exit 0
            """,
        )

    sys_dir = tmp_path / "sys"
    stats = sys_dir / "tailscale0" / "statistics"
    stats.mkdir(parents=True)
    (stats / "rx_bytes").write_text("111\n", encoding="utf-8")
    (stats / "tx_bytes").write_text("222\n", encoding="utf-8")

    env_file = tmp_path / ".env"
    env_file.write_text(env_content, encoding="utf-8")

    env = {
        "PATH": str(bin_dir),
        "ROUTEWEAVE_TS_ENV": str(env_file),
        "ROUTEWEAVE_TS_SYSFS": str(sys_dir),
        "TS_STUB_JSON": status_json,
        "TS_STUB_PREFS_JSON": prefs_json,
        "SYSTEMCTL_OUT": systemctl_out,
        "TAILSCALE_RC": str(tailscale_rc),
    }
    return {
        "env": env,
        "bin": bin_dir,
        "sys": sys_dir,
        "env_file": env_file,
        "stats": stats,
    }


def run_helper(paths):
    """Запуск helper'а через bash (shebang может указывать на другой bash)."""
    return subprocess.run(
        ["bash", str(HELPER)],
        env=paths["env"],
        text=True,
        capture_output=True,
        check=False,
    )


def parse_state(proc):
    """Одна непустая строка stdout → dict; иначе падение с диагностикой."""
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, proc.stdout
    return json.loads(lines[0])


# ---------------- Helper ----------------

def test_helper_is_read_only_and_executable():
    assert os.access(HELPER, os.X_OK)
    text = HELPER.read_text(encoding="utf-8")
    # Никаких ключей, имён устройств и попыток что-то изменить в системе.
    for forbidden in ("tskey-", "--auth-key", "HostName", "tee ", "mktemp", "rm ", "install "):
        assert forbidden not in text, f"helper не должен содержать {forbidden!r}"

    for args in (
        ["bash", "-n", str(HELPER)],
        ["bash", "-O", "extglob", "-n", str(HELPER)],
    ):
        proc = subprocess.run(args, text=True, capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr


def test_absent_tailscale_returns_valid_json(tmp_path):
    paths = make_path(tmp_path, with_tailscale=False)
    state = parse_state(run_helper(paths))

    assert state["installed"] is False
    assert state["daemon"] == "absent"
    assert state["error"] == ""
    assert isinstance(state["rx_bytes"], int)
    assert isinstance(state["tx_bytes"], int)


def test_running_tailscale_aggregates(tmp_path):
    proc = run_helper(make_path(tmp_path))
    state = parse_state(proc)

    assert state["installed"] is True
    assert state["daemon"] == "active"
    assert state["backend"] == "Running"
    assert state["self_ip"] == "100.64.0.10"  # IPv4, а не fd7a:...
    assert state["advertised"] is True
    assert state["approved"] is True
    assert state["peers"] == 1
    assert state["iface"] == "tailscale0"
    assert state["rx_bytes"] == 111
    assert state["tx_bytes"] == 222
    assert state["error"] == ""

    # Имена пиров и ключи ответа в вывод не попадают — только агрегаты.
    assert "peer-a" not in proc.stdout
    assert "HostName" not in proc.stdout


def test_peers_counted_from_peer_map(tmp_path):
    # Пиры считаются по числу ключей объекта Peer; поля Peers в 1.104.x нет.
    status_json = json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["100.64.0.14"],
                "ExitNodeOption": False,
            },
            "Peer": {"a": {}, "b": {}},
        },
        separators=(",", ":"),
    )
    proc = run_helper(make_path(tmp_path, status_json=status_json))
    state = parse_state(proc)

    assert state["peers"] == 2
    assert state["approved"] is False
    assert "HostName" not in proc.stdout


def test_status_failure_keeps_installed(tmp_path):
    paths = make_path(tmp_path, tailscale_rc=1)
    state = parse_state(run_helper(paths))

    assert state["installed"] is True
    assert state["backend"] == ""
    assert state["error"]
    assert state["self_ip"] == ""
    assert state["advertised"] is False
    assert state["approved"] is False
    assert state["peers"] == 0


def test_broken_json_sets_error(tmp_path):
    paths = make_path(tmp_path, status_json="not json")
    state = parse_state(run_helper(paths))

    assert state["installed"] is True
    assert state["backend"] == ""
    assert state["error"]


def test_invalid_iface_falls_back(tmp_path):
    paths = make_path(tmp_path, env_content="TS_IFACE=../../etc\n")
    state = parse_state(run_helper(paths))
    assert state["iface"] == "tailscale0"

    # Кавычки в .env снимаются shell-семантикой, а не попадают в имя.
    paths["env_file"].write_text('TS_IFACE="tailscale0"\n', encoding="utf-8")
    quoted = parse_state(run_helper(paths))
    assert quoted["iface"] == "tailscale0"


def test_counters_read_from_sysfs(tmp_path):
    paths = make_path(tmp_path)
    assert parse_state(run_helper(paths))["rx_bytes"] == 111

    (paths["stats"] / "rx_bytes").unlink()
    (paths["stats"] / "tx_bytes").unlink()
    state = parse_state(run_helper(paths))
    assert state["rx_bytes"] == 0
    assert state["tx_bytes"] == 0


def _status_without_exit_routes(ip="100.64.0.11"):
    """Running-узел без одобрения и без exit-маршрутов в AllowedIPs."""
    return json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": [ip, "fd7a:115c::2"],
                "AllowedIPs": [f"{ip}/32", "fd7a:115c::2/128"],
                "ExitNodeOption": False,
            },
            "Peer": {},
        },
        separators=(",", ":"),
    )


def test_advertised_pending_approval_via_debug_prefs(tmp_path):
    # Prefs верхнего уровня в status --json больше не поддерживаем: анонс
    # берём из `tailscale debug prefs` (AdvertiseRoutes), одобрения нет.
    state = parse_state(
        run_helper(
            make_path(
                tmp_path,
                status_json=_status_without_exit_routes(),
                prefs_json='{"AdvertiseRoutes":["0.0.0.0/0","::/0"]}',
            )
        )
    )

    assert state["advertised"] is True
    assert state["approved"] is False
    assert state["peers"] == 0


def test_approved_without_primary_routes(tmp_path):
    # В tailscale status --json 1.104.x у Self нет PrimaryRoutes: одобрение
    # определяется только по ExitNodeOption.
    status_json = json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["100.64.0.12"],
                "ExitNodeOption": True,
            },
        },
        separators=(",", ":"),
    )
    state = parse_state(run_helper(make_path(tmp_path, status_json=status_json)))

    assert state["approved"] is True
    assert state["advertised"] is True


def test_not_advertised_when_prefs_unavailable(tmp_path):
    # `tailscale debug prefs` недоступен (стаб печатает пустоту) — деградация
    # до advertised=false, без ошибки в ответе.
    state = parse_state(
        run_helper(
            make_path(
                tmp_path,
                status_json=_status_without_exit_routes("100.64.0.13"),
                prefs_json="",
            )
        )
    )

    assert state["advertised"] is False
    assert state["approved"] is False
    assert state["error"] == ""


def test_no_python3_fallback(tmp_path):
    paths = make_path(tmp_path, with_python=False)
    proc = run_helper(paths)
    state = parse_state(proc)

    assert state["installed"] is True
    assert state["error"] == "python3 недоступен"
    # Fallback не читает sysfs и печатает нулевые счётчики.
    assert state["rx_bytes"] == 0
    assert state["tx_bytes"] == 0


# ---------------- fw-tailscale.sh (статически) ----------------

def test_fw_tailscale_uses_exitnodeoption():
    text = FW_TAILSCALE.read_text(encoding="utf-8")

    # PrimaryRoutes — это subnet-маршруты, у Self в 1.104.x поля нет.
    assert 'self_.get("PrimaryRoutes")' not in text
    assert 'self_.get("ExitNodeOption")' in text
    # Анонс без одобрения виден только в локальных prefs.
    assert '["tailscale", "debug", "prefs"]' in text
    # doctor не должен выдавать ложный FAIL про «маршрут не одобрен».
    assert "(Self.ExitNodeOption)" in text


# ---------------- Установщик (статически) ----------------

def test_installer_preflight_and_sudoers():
    text = INSTALL.read_text(encoding="utf-8")

    start = text.find("preflight() {")
    end = text.find('\npreflight "$APP_DIR"', start)
    assert start != -1 and end != -1, "не найден preflight в install.sh"
    assert "templates/routeweave-tailscale-state.sh" in text[start:end]

    assert "TAILSCALE_STATE=/usr/local/sbin/routeweave-tailscale-state.sh" in text

    sudo_line = next(
        line
        for line in text.splitlines()
        if "/usr/local/sbin/routeweave-tailscale-state.sh" in line
        and "NOPASSWD" in line
    )
    assert "/usr/local/sbin/routeweave-tailscale-state.sh" in sudo_line
    assert "*" not in sudo_line, "sudoers панели не должен содержать wildcard"

    install_line = next(
        line
        for line in text.splitlines()
        if 'install -m 0755 "$APP_DIR/templates/routeweave-tailscale-state.sh"' in line
    )
    assert '"$TAILSCALE_STATE"' in install_line


def test_uninstall_removes_helper():
    text = UNINSTALL.read_text(encoding="utf-8")

    start = text.find("HELPERS=(")
    end = text.find(")", start) + 1
    assert start != -1 and end > start, "не найден массив HELPERS"
    assert "/usr/local/sbin/routeweave-tailscale-state.sh" in text[start:end]

    assert "включая routeweave-tailscale-state.sh;" in text


def test_no_authkey_in_helper():
    text = HELPER.read_text(encoding="utf-8")
    for forbidden in ("tskey-", "--auth-key", "TS_AUTHKEY"):
        assert forbidden not in text, f"helper не должен содержать {forbidden!r}"
