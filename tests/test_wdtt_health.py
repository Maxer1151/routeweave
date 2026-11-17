"""Лёгкий монитор WDTT (templates/routeweave-wdtt-health.sh) без root.

Проверяется то, что нельзя увидеть статически: формат строки журнала,
дельты счётчиков между запусками, подсчёт сессий/пиров/ошибок по логу
tg-ws-proxy и ротация файла по размеру. Скрипт запускается с подменённым
окружением (ROUTEWEAVE_ETC, WDTT_NET_DEV, WDTT_HEALTH_LOG, fake docker и
systemctl в PATH), поэтому chown/chmod молча игнорируются — тест проходит
от непривилегированного пользователя.
"""

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "templates" / "routeweave-wdtt-health.sh"
SERVICE = REPO_ROOT / "templates" / "routeweave-wdtt-health.service"
TIMER = REPO_ROOT / "templates" / "routeweave-wdtt-health.timer"
INSTALL = REPO_ROOT / "install.sh"
UNINSTALL = REPO_ROOT / "uninstall.sh"

TGWS_LOGS = (
    "2026-01-01T00:00:00Z client [10.66.66.5:50000] handshake ok\n"
    "2026-01-01T00:00:01Z client [10.66.66.6:50001] handshake ok\n"
    "2026-01-01T00:00:02Z ERROR CF Worker timed out\n"
)


def net_dev(rx_bytes: int, tx_bytes: int, rx_packets: int = 10, tx_packets: int = 20) -> str:
    """Содержимое /proc/net/dev с одной строкой wdtt0 (формат ядра)."""
    return (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets"
        " errs drop fifo colls carrier compressed\n"
        f"  wdtt0: {rx_bytes} {rx_packets} 0 0 0 0 0 0 {tx_bytes} {tx_packets} 0 0 0 0 0 0\n"
    )


def make_env(tmp_path: Path, counters: str) -> dict:
    """Каталоги, .env, fake /proc/net/dev, docker и systemctl."""
    etc = tmp_path / "etc"
    bin_dir = tmp_path / "bin"
    log_dir = tmp_path / "log"
    for d in (etc, bin_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)
    (etc / ".env").write_text("WG_SUBNET='10.66.66.0/24'\n", encoding="utf-8")
    net = tmp_path / "net_dev"
    net.write_text(counters, encoding="utf-8")
    tgws_log = tmp_path / "tgws.log"
    tgws_log.write_text(TGWS_LOGS, encoding="utf-8")

    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  inspect) printf 'true\\n' ;;\n"
        "  logs) cat \"$FAKE_TGWS_LOG\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  is-active) printf 'active\\n' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for f in (docker, systemctl):
        f.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(tmp_path),
        "ROUTEWEAVE_ETC": str(etc),
        "WDTT_NET_DEV": str(net),
        "WDTT_HEALTH_LOG": str(log_dir / "wdtt-health.log"),
        "WDTT_IFACE": "wdtt0",
        "TGWS_CONTAINER": "routeweave-tgwsproxy",
        "FAKE_TGWS_LOG": str(tgws_log),
    }
    return env


def run_monitor(env: dict, **overrides: str) -> subprocess.CompletedProcess:
    full_env = dict(env)
    full_env.update(overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=full_env,
    )


def parse_line(line: str) -> dict:
    fields = {}
    for token in line.split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
    return fields


def test_first_run_writes_counters_sessions_and_peers(tmp_path):
    env = make_env(tmp_path, net_dev(1000, 2000))
    proc = run_monitor(env)
    assert proc.returncode == 0, proc.stderr

    log = Path(env["WDTT_HEALTH_LOG"]).read_text(encoding="utf-8").splitlines()
    assert len(log) == 1, log
    row = parse_line(log[0])
    assert row["iface"] == "wdtt0"
    assert row["rx_b"] == "1000"
    assert row["tx_b"] == "2000"
    assert row["rx_p"] == "10"
    assert row["tx_p"] == "20"
    assert row["rx_d"] == "0"
    assert row["tx_d"] == "0"
    assert row["reset"] == "0"
    assert row["wdtt"] == "active"
    assert row["tgws"] == "up"
    assert row["sess_2m"] == "2"
    assert row["peers_2m"] == "2"
    assert row["err_2m"] == "1"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", log[0].split()[0])


def test_second_run_reports_minute_deltas(tmp_path):
    env = make_env(tmp_path, net_dev(1000, 2000))
    assert run_monitor(env).returncode == 0

    Path(env["WDTT_NET_DEV"]).write_text(net_dev(1600, 2600), encoding="utf-8")
    proc = run_monitor(env)
    assert proc.returncode == 0, proc.stderr

    log = Path(env["WDTT_HEALTH_LOG"]).read_text(encoding="utf-8").splitlines()
    assert len(log) == 2, log
    row = parse_line(log[-1])
    assert row["rx_b"] == "1600"
    assert row["tx_b"] == "2600"
    assert row["rx_d"] == "600"
    assert row["tx_d"] == "600"
    assert row["reset"] == "0"


def test_counter_reset_marks_reset(tmp_path):
    env = make_env(tmp_path, net_dev(1000, 2000))
    assert run_monitor(env).returncode == 0

    Path(env["WDTT_NET_DEV"]).write_text(net_dev(100, 200), encoding="utf-8")
    assert run_monitor(env).returncode == 0

    row = parse_line(Path(env["WDTT_HEALTH_LOG"]).read_text(encoding="utf-8").splitlines()[-1])
    assert row["rx_d"] == "0"
    assert row["tx_d"] == "0"
    assert row["reset"] == "1"


def test_rotation_keeps_tail_and_new_line(tmp_path):
    env = make_env(tmp_path, net_dev(1000, 2000))
    log_path = Path(env["WDTT_HEALTH_LOG"])
    log_path.write_text(
        "".join(f"2026-01-01T00:0{i}:00Z rx_b={i}0 tx_b={i}0\n" for i in range(5)),
        encoding="utf-8",
    )
    assert log_path.stat().st_size > 10

    proc = run_monitor(env, WDTT_HEALTH_MAX_BYTES="10", WDTT_HEALTH_KEEP_LINES="2")
    assert proc.returncode == 0, proc.stderr

    lines = log_path.read_text(encoding="utf-8").splitlines()
    # Две сохранённые строки (KEEP_LINES=2) плюс текущий замер.
    assert len(lines) == 3, lines
    assert "rx_b=30" in lines[0]
    assert "rx_b=40" in lines[1]
    assert parse_line(lines[2])["rx_b"] == "1000"


def test_service_and_timer_invariants():
    service = SERVICE.read_text(encoding="utf-8")
    for needle in (
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ReadWritePaths=/var/log/routeweave",
        "NoNewPrivileges=yes",
        "Environment=DOCKER_CONFIG=/tmp",
        "ExecStart=/usr/local/sbin/routeweave-wdtt-health.sh",
        "Type=oneshot",
    ):
        assert needle in service, f"в service нет {needle}"

    timer = TIMER.read_text(encoding="utf-8")
    for needle in ("OnActiveSec=60", "OnUnitActiveSec=60", "AccuracySec=5s", "Persistent=no",
                   "WantedBy=timers.target"):
        assert needle in timer, f"в timer нет {needle}"


def test_install_and_uninstall_wire_monitor():
    install = INSTALL.read_text(encoding="utf-8")
    assert 'templates/routeweave-wdtt-health.sh"       /usr/local/sbin/routeweave-wdtt-health.sh' in install
    assert "templates/routeweave-wdtt-health.service" in install
    assert "templates/routeweave-wdtt-health.timer" in install
    # Условное включение: helper проверяет wdtt0 или wdtt.service и не входит
    # в жёсткий for-цикл проверки обязательных юнитов.
    assert "wdtt_health_timer_enable" in install
    helper = re.search(r"^wdtt_health_timer_enable\(\) \{.*?^\}\n", install, re.MULTILINE | re.DOTALL)
    assert helper, "в install.sh нет wdtt_health_timer_enable"
    body = helper.group(0)
    assert 'ip -4 addr show dev "$WDTT_IFACE"' in body
    assert "/etc/systemd/system/wdtt.service" in body
    assert "systemctl enable --now routeweave-wdtt-health.timer" in body
    for_line = re.search(r"^for u in [^\n]*$", install, re.MULTILINE)
    assert for_line and "wdtt-health" not in for_line.group(0)

    uninstall = UNINSTALL.read_text(encoding="utf-8")
    assert "routeweave-wdtt-health.timer" in uninstall
    assert "routeweave-wdtt-health.service" in uninstall
    assert "/usr/local/sbin/routeweave-wdtt-health.sh" in uninstall


def test_bash_syntax():
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LANG": "C.UTF-8"},
    )
    assert proc.returncode == 0, proc.stderr
