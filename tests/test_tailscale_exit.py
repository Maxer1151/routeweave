"""Динамические тесты Tailscale exit node в fw-base.sh (netns).

Запуск от root (netns + iptables):
    pytest tests/test_tailscale_exit.py -v -rs

Если root нет, iptables не установлен или netns недоступен — тесты
помечаются skip, а не падают: на обычной машине разработчика это норма.

Сценарий повторяет policy-routing mihomo: таблица 2022 с default dev Meta и
правило 9002 lookup 2022. Всё, что не ушло в main по более приоритетному
правилу, попадает в TUN. fw-base.sh добавляет priority 8991 для
100.64.0.0/10, поэтому:
  * адреса tailnet резолвятся через main (dev tailscale0);
  * forwarded-трафик из tailscale0 (exit node) уходит в meta-политику;
  * ответ из Meta клиенту tailnet возвращается на tailscale0.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.privileged,
    # skipif, а не module-level skip: иначе pytest не соберёт ни одного теста
    # и вернёт код 5; так тесты просто помечаются skipped.
    pytest.mark.skipif(os.geteuid() != 0, reason="нужен root: netns и iptables"),
    pytest.mark.skipif(shutil.which("ip") is None, reason="нет ip из iproute2"),
    pytest.mark.skipif(shutil.which("iptables") is None, reason="нет iptables"),
]

NS = "rwts"
REPO = Path(__file__).resolve().parent.parent
FW_BASE = REPO / "templates" / "fw-base.sh"
TUN_IFACE = "Meta"
TS_IFACE = "tailscale0"
TS_RULE_PRIO = "8991"
POLICY_PRIO = "9002"


def _run(*args):
    return subprocess.run(args, check=False, text=True, capture_output=True)


def ns(*args):
    """Команда внутри тестового namespace."""
    return _run("ip", "netns", "exec", NS, *args)


def ns_ok(*args):
    res = ns(*args)
    assert res.returncode == 0, f"{' '.join(args)}: {res.stderr.strip()}"
    return res.stdout


def run_fw(etc, action):
    """fw-base.sh внутри ns: все ip/iptables вызовы уходят в namespace."""
    return _run(
        "ip", "netns", "exec", NS,
        "env", f"ROUTEWEAVE_ETC={etc}",
        "bash", str(FW_BASE), action,
    )


def route_dev(*args):
    """Устройство из `ip route get ...`; пустая строка при ошибке."""
    res = ns("ip", "route", "get", *args)
    if res.returncode != 0:
        return ""
    fields = res.stdout.split()
    for i, field in enumerate(fields):
        if field == "dev" and i + 1 < len(fields):
            return fields[i + 1]
    return ""


def rule_lines():
    return ns_ok("ip", "rule", "show").splitlines()


def write_env(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".env").write_text(
        "LAN_IFACE=lan0\n"
        f"TUN_IFACE={TUN_IFACE}\n"
        "QUIC_DROP=yes\n"
        "WG_SUBNET=10.77.0.0/24\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def started(tmp_path_factory):
    etc = write_env(tmp_path_factory.mktemp("etc"))

    _run("ip", "netns", "del", NS)  # мусор от упавшего прогона
    res = _run("ip", "netns", "add", NS)
    if res.returncode != 0:
        pytest.skip(f"ip netns add недоступен: {res.stderr.strip()}")

    try:
        for iface in ("lan0", TUN_IFACE, TS_IFACE):
            ns_ok("ip", "link", "add", iface, "type", "dummy")
            ns_ok("ip", "link", "set", iface, "up")
        ns_ok("ip", "link", "set", "lo", "up")

        # Tailnet-маршрут лежит в main и должен выигрывать у policy-routing.
        ns_ok("ip", "route", "add", "100.64.0.0/10", "dev", TS_IFACE)
        # Имитация mihomo TUN: default в table 2022 + правило 9002.
        ns_ok("ip", "route", "add", "default", "dev", TUN_IFACE, "table", "2022")
        ns_ok("ip", "rule", "add", "lookup", "2022", "priority", POLICY_PRIO)

        if ns("iptables", "-S").returncode != 0:
            pytest.skip("iptables внутри netns не работает")

        res = run_fw(etc, "start")
        assert res.returncode == 0, res.stderr
        yield etc
    finally:
        _run("ip", "netns", "del", NS)


def test_tailnet_rule_above_policy(started):
    lines = rule_lines()
    ours = [i for i, ln in enumerate(lines)
            if ln.startswith(f"{TS_RULE_PRIO}:")
            and "to 100.64.0.0/10" in ln and "lookup main" in ln]
    policy = [i for i, ln in enumerate(lines) if ln.startswith(f"{POLICY_PRIO}:")]
    assert ours, f"нет правила {TS_RULE_PRIO} to 100.64.0.0/10 lookup main:\n" + "\n".join(lines)
    assert policy, f"нет правила {POLICY_PRIO} (имитация mihomo)"
    assert ours[0] < policy[0], (
        f"правило {TS_RULE_PRIO} (строка {ours[0]}) ниже {POLICY_PRIO} (строка {policy[0]})"
    )
    assert route_dev("100.64.0.1") == TS_IFACE

    # Без 8991 policy-routing заворачивает tailnet в TUN — это и лечит fw-base.
    assert run_fw(started, "stop").returncode == 0
    assert route_dev("100.64.0.1") == TUN_IFACE
    assert not any(ln.startswith(f"{TS_RULE_PRIO}:") for ln in rule_lines())

    assert run_fw(started, "start").returncode == 0
    assert route_dev("100.64.0.1") == TS_IFACE


def test_exit_node_traffic_goes_to_tun(started):
    # Адрес 100.64.0.5 — клиент tailnet, а не локальный адрес шлюза.
    assert route_dev("1.1.1.1", "from", "100.64.0.5", "iif", TS_IFACE) == TUN_IFACE


def test_return_path_goes_to_tailnet(started):
    assert route_dev("100.64.0.5", "from", "1.1.1.1", "iif", TUN_IFACE) == TS_IFACE


def test_stop_removes_rule(started):
    # start идемпотентен, поэтому тест не зависит от порядка запуска
    assert run_fw(started, "start").returncode == 0

    res = run_fw(started, "stop")
    assert res.returncode == 0, res.stderr

    assert not any(ln.startswith(f"{TS_RULE_PRIO}:") for ln in rule_lines())
    assert ns("iptables", "-S", "ROUTEWEAVE-FWD").returncode != 0, \
        "цепочка ROUTEWEAVE-FWD осталась после stop"
    # После снятия 8991 tailnet снова уходит в policy-routing mihomo.
    assert route_dev("100.64.0.1") == TUN_IFACE

    # повторный stop — успех, а не "chain does not exist"
    again = run_fw(started, "stop")
    assert again.returncode == 0, again.stderr
