"""Hardening systemd-юнитов Routeweave (L-09).

Проверки статические: в юнитах должны остаться ограничения песочницы и не
должно появиться директив, которые ломают TUN (mihomo), ambient capabilities
(zapret) или sysctl (firewall).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"

# Диагностика systemd-analyze в формате «/путь/к/юниту:строка: текст».
_UNIT_DIAGNOSTIC = re.compile(
    r"^(?P<path>[^:\s]+\.(?:service|socket|slice|timer|target|mount|automount"
    r"|path|swap|device|scope)):\d+:"
)


def service(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def directive(text, name):
    """Значение директивы (последнее вхождение) или None."""
    matches = re.findall(rf"^{re.escape(name)}=(.*)$", text, re.MULTILINE)
    assert len(matches) <= 1, f"{name} задан несколько раз"
    return matches[0].strip() if matches else None


def test_mihomo_hardening_keeps_tun_working():
    text = service("mihomo.service")
    assert directive(text, "ProtectSystem") == "strict"
    assert directive(text, "ReadWritePaths") == "/etc/routeweave /run"
    assert directive(text, "ProtectHome") == "yes"
    assert directive(text, "PrivateTmp") == "yes"
    assert "AF_NETLINK" in directive(text, "RestrictAddressFamilies")
    assert directive(text, "RestrictSUIDSGID") == "yes"
    assert directive(text, "LockPersonality") == "yes"
    # AmbientCapabilities несовместимы с NoNewPrivileges, а /dev/net/tun —
    # с PrivateDevices.
    assert directive(text, "NoNewPrivileges") is None
    assert directive(text, "PrivateDevices") is None
    assert directive(text, "AmbientCapabilities") == (
        "CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE"
    )
    assert "ExecStartPre=/usr/bin/test -c /dev/net/tun" in text


def test_zapret_hardening():
    text = service("zapret-nfqws.service")
    assert directive(text, "NoNewPrivileges") == "yes"
    assert directive(text, "ProtectSystem") == "strict"
    assert directive(text, "ReadWritePaths") == "/etc/routeweave /run"
    assert directive(text, "ProtectHome") == "yes"
    assert directive(text, "PrivateTmp") == "yes"
    assert "AF_NETLINK" in directive(text, "RestrictAddressFamilies")
    caps = "CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE"
    assert directive(text, "CapabilityBoundingSet") == caps
    assert directive(text, "AmbientCapabilities") == caps
    assert directive(text, "RestrictSUIDSGID") == "yes"
    assert directive(text, "LockPersonality") == "yes"


def test_firewall_hardening_allows_sysctl():
    text = service("routeweave-firewall.service")
    assert directive(text, "NoNewPrivileges") == "yes"
    assert directive(text, "ProtectSystem") == "full"
    assert directive(text, "ProtectHome") == "yes"
    assert directive(text, "PrivateTmp") == "yes"
    assert "AF_NETLINK" in directive(text, "RestrictAddressFamilies")
    caps = "CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE"
    assert directive(text, "CapabilityBoundingSet") == caps
    assert directive(text, "AmbientCapabilities") == caps
    assert directive(text, "RestrictSUIDSGID") == "yes"
    assert directive(text, "LockPersonality") == "yes"
    # fw-base.sh делает sysctl -w net.ipv4.ip_forward=1: ProtectKernelTunables
    # сломал бы форвардинг, поэтому директивы быть не должно.
    assert directive(text, "ProtectKernelTunables") is None
    assert "EnvironmentFile=/etc/routeweave/.env" in text


def test_lists_update_hardening():
    text = service("routeweave-lists-update.service")
    assert directive(text, "NoNewPrivileges") == "yes"
    assert directive(text, "ProtectSystem") == "strict"
    assert directive(text, "ReadWritePaths") == (
        "/etc/routeweave/lists /etc/routeweave/.staging"
    )
    assert directive(text, "ProtectHome") == "yes"
    assert directive(text, "PrivateTmp") == "yes"
    families = directive(text, "RestrictAddressFamilies")
    assert "AF_INET" in families and "AF_INET6" in families
    assert directive(text, "CapabilityBoundingSet") == (
        "CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER"
    )
    assert directive(text, "RestrictSUIDSGID") == "yes"
    assert directive(text, "LockPersonality") == "yes"


def test_panel_unit_keeps_its_exceptions():
    text = service("routeweave-panel.service")
    # Панель слушает PANEL_BIND из .env, а не 0.0.0.0, и работает ровно в один
    # воркер (состояние JOB/кэш живут в памяти процесса).
    assert "0.0.0.0" not in text
    assert "--workers" not in text
    assert "NoNewPrivileges=no" in text  # нужен setuid-root sudo
    assert "CapabilityBoundingSet=" in text
    assert "ProtectSystem=strict" in text


def is_foreign_unit_diagnostic(line, unit):
    """Относится ли строка диагностики к другому юниту хоста.

    systemd-analyze verify проверяет не только свой файл, но и зависимости,
    поэтому на живом хосте в вывод попадают предупреждения о чужих юнитах
    (например, «Support for option CPUAccounting= has been removed» из
    xfs_scrub_all.service на systemd 258+). К проверяемому юниту такие строки
    отношения не имеют.
    """
    match = _UNIT_DIAGNOSTIC.match(line.strip())
    return bool(match) and Path(match.group("path")).name != unit


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze недоступен"
)
@pytest.mark.parametrize(
    "unit",
    [
        "mihomo.service",
        "zapret-nfqws.service",
        "routeweave-firewall.service",
        "routeweave-lists-update.service",
        "routeweave-panel.service",
    ],
)
def test_systemd_analyze_verify_has_no_syntax_errors(unit):
    proc = subprocess.run(
        ["systemd-analyze", "verify", str(TEMPLATES / unit)],
        check=False,
        text=True,
        capture_output=True,
    )
    # Отсутствие бинарников (/usr/local/bin/mihomo и т.п.) — ожидаемо в
    # песочнице; синтаксических ошибок и неизвестных директив быть не должно.
    # Чужие юниты хоста (зависимости) не наша зона ответственности.
    problems = [
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if line.strip()
        and "is not executable" not in line
        and not is_foreign_unit_diagnostic(line, unit)
    ]
    assert not problems, problems


@pytest.mark.parametrize(
    "line, unit, foreign",
    [
        (
            (
                "/usr/lib/systemd/system/xfs_scrub_all.service:26: Support for option "
                "CPUAccounting= has been removed and it is ignored"
            ),
            "mihomo.service",
            True,
        ),
        (
            "/tmp/unit/mihomo.service:12: Unknown key name 'Foo'",
            "mihomo.service",
            False,
        ),
        ("mihomo.service:12: Unknown key name 'Foo'", "mihomo.service", False),
        (
            "Failed to prepare filename /etc/foo.service: Invalid argument",
            "mihomo.service",
            False,
        ),
        ("Unit mihomo.service has a bad unit file setting.", "mihomo.service", False),
    ],
)
def test_foreign_unit_diagnostic_filter(line, unit, foreign):
    # Фильтр обязан отбрасывать только чужие юниты и не прятать наши ошибки.
    assert is_foreign_unit_diagnostic(line, unit) is foreign
