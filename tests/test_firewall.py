"""Динамические тесты templates/fw-base.sh в отдельном network namespace.

Запуск от root (netns + iptables):
    pytest tests/test_firewall.py -v -rs

Если root нет, iptables не установлен или netns недоступен — тесты
помечаются skip, а не падают: на обычной машине разработчика это норма.
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

NS = "rwtest"
REPO = Path(__file__).resolve().parent.parent
FW_BASE = REPO / "templates" / "fw-base.sh"


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


def chain_rules():
    return [ln for ln in ns_ok("iptables", "-S", "ROUTEWEAVE-FWD").splitlines()
            if ln.startswith("-A ")]


def forward_jumps():
    out = ns_ok("iptables", "-S", "FORWARD")
    return [ln for ln in out.splitlines() if "ROUTEWEAVE-FWD" in ln]


@pytest.fixture(scope="module")
def started(tmp_path_factory):
    etc = tmp_path_factory.mktemp("etc")
    (etc / ".env").write_text(
        "LAN_IFACE=lan0\n"
        "TUN_IFACE=Meta\n"
        "QUIC_DROP=yes\n"
        "WG_SUBNET=10.77.0.0/24\n",
        encoding="utf-8",
    )

    _run("ip", "netns", "del", NS)  # мусор от упавшего прогона
    res = _run("ip", "netns", "add", NS)
    if res.returncode != 0:
        pytest.skip(f"ip netns add недоступен: {res.stderr.strip()}")

    try:
        for iface in ("lan0", "wdtt0", "wg0", "tailscale0"):
            ns_ok("ip", "link", "add", iface, "type", "dummy")
            ns_ok("ip", "link", "set", iface, "up")
        ns_ok("ip", "link", "set", "lo", "up")

        if ns("iptables", "-S").returncode != 0:
            pytest.skip("iptables внутри netns не работает")

        res = run_fw(etc, "start")
        assert res.returncode == 0, res.stderr
        yield etc
    finally:
        _run("ip", "netns", "del", NS)


def test_quic_drop_above_accepts(started):
    rules = chain_rules()
    assert rules, "цепочка ROUTEWEAVE-FWD пуста"

    # первый шаг — ответный трафик из TUN Meta в LAN
    assert "RELATED,ESTABLISHED" in rules[0], rules[0]
    assert "-i Meta" in rules[0] and "-o lan0" in rules[0], rules[0]

    quic = [i for i, r in enumerate(rules)
            if "-p udp" in r and "443" in r and "-j DROP" in r]
    assert quic, "нет правила DROP для UDP/443"

    # первое разрешающее правило в сторону TUN Meta
    accepts = [i for i, r in enumerate(rules)
               if "-o Meta" in r and "-j ACCEPT" in r]
    assert accepts, "нет ACCEPT в TUN Meta"
    assert quic[0] < accepts[0], (
        f"QUIC DROP (строка {quic[0]}) ниже ACCEPT (строка {accepts[0]})"
    )


def test_vpn_ifaces(started):
    rules = chain_rules()
    assert any("-i wdtt0" in r for r in rules), "wdtt0 не попал в правила"
    assert any("-i wg0" in r for r in rules), "wg0 не попал в правила"
    # wg+ не матчит wdtt0, поэтому в правилах его быть не должно
    assert not any("wg+" in r for r in rules)


def test_tailscale_forwarding(started):
    rules = chain_rules()

    # exit-node: вход из tailnet в TUN и возврат из TUN в tailnet
    assert any("-i tailscale0" in r and "-o Meta" in r and "-j ACCEPT"
               for r in rules), "нет ACCEPT tailscale0 -> Meta"
    assert any("-i Meta" in r and "-o tailscale0" in r and "-j ACCEPT"
               for r in rules), "нет ACCEPT Meta -> tailscale0"

    # tailnet <-> tailnet и LAN -> tailnet
    assert any("-i tailscale0" in r and "-o tailscale0" in r and "-j ACCEPT"
               for r in rules), "нет ACCEPT tailscale0 -> tailscale0"
    assert any("-i lan0" in r and "-o tailscale0" in r and "-j ACCEPT"
               for r in rules), "нет ACCEPT lan0 -> tailscale0"

    # CGNAT-спуф с любого интерфейса, кроме tailscale0
    assert any("! -i tailscale0" in r and "-s 100.64.0.0/10" in r
               and "-j DROP" in r for r in rules), "нет DROP для CGNAT-спуфа"


def test_tailscale_ip_rule(started):
    lines = ns_ok("ip", "rule", "show").splitlines()
    rules = [ln for ln in lines if ln.startswith("8991:")]
    assert len(rules) == 1, lines
    assert "to 100.64.0.0/10" in rules[0], rules[0]
    assert "lookup main" in rules[0], rules[0]


def test_single_forward_jump(started):
    assert len(forward_jumps()) == 1, forward_jumps()


def test_second_start_does_not_duplicate(started):
    before = len(chain_rules())
    res = run_fw(started, "start")
    assert res.returncode == 0, res.stderr
    assert len(chain_rules()) == before
    assert len(forward_jumps()) == 1, forward_jumps()
    lines = ns_ok("ip", "rule", "show").splitlines()
    assert sum(1 for ln in lines if ln.startswith("8991:")) == 1, lines


def test_wdtt_ip_rule(started):
    out = ns_ok("ip", "rule")
    assert "10.77.0.0/24" in out
    assert sum(1 for ln in out.splitlines() if ln.startswith("8990:")) == 1, out


def test_stop_removes_everything(started):
    # start идемпотентен, поэтому тест не зависит от порядка запуска
    assert run_fw(started, "start").returncode == 0

    res = run_fw(started, "stop")
    assert res.returncode == 0, res.stderr

    assert "ROUTEWEAVE-FWD" not in ns_ok("iptables", "-S", "FORWARD")
    assert ns("iptables", "-S", "ROUTEWEAVE-FWD").returncode != 0, \
        "цепочка ROUTEWEAVE-FWD осталась после stop"
    rule_lines = ns_ok("ip", "rule", "show").splitlines()
    assert "10.77.0.0/24" not in "\n".join(rule_lines)
    assert not any(ln.startswith("8991:") for ln in rule_lines), rule_lines
    assert "100.64.0.0/10" not in "\n".join(rule_lines), rule_lines

    # повторный stop — успех, а не "chain does not exist"
    again = run_fw(started, "stop")
    assert again.returncode == 0, again.stderr
