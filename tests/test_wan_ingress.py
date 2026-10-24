"""Динамические тесты templates/fw-wan.sh в отдельном network namespace.

Запуск от root (netns + iptables):
    pytest tests/test_wan_ingress.py -v -rs

Если root нет, iptables не установлен или netns недоступен — тесты
помечаются skip, а не падают: на обычной машине разработчика это норма.

Сценарий повторяет policy-routing mihomo: таблица 2022 с default dev Meta и
правило 9002 iif lo lookup 2022. Без маркировки пакет уходит в TUN, с меткой
0x40000 — в main через правило 8993.
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

NS = "rwwan"
REPO = Path(__file__).resolve().parent.parent
FW_WAN = REPO / "templates" / "fw-wan.sh"
MARK = "0x40000"
PRIO = "8993"


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
    """fw-wan.sh внутри ns: все ip/iptables вызовы уходят в namespace."""
    return _run(
        "ip", "netns", "exec", NS,
        "env", f"ROUTEWEAVE_ETC={etc}",
        "bash", str(FW_WAN), action,
    )


def route_dev(*extra):
    out = ns_ok("ip", "route", "get", "1.1.1.1", *extra)
    for line in out.splitlines():
        fields = line.split()
        if "dev" in fields:
            return fields[fields.index("dev") + 1]
    return ""


def chain_rules(chain):
    return [ln for ln in ns_ok("iptables", "-t", "mangle", "-S", chain).splitlines()
            if ln.startswith("-A ")]


def hook_jumps(hook, chain):
    out = ns_ok("iptables", "-t", "mangle", "-S", hook)
    return [ln for ln in out.splitlines() if f"-j {chain}" in ln]


def rule_lines():
    return ns_ok("ip", "rule", "show").splitlines()


def write_env(path, wan_ingress="yes"):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".env").write_text(
        "LAN_IFACE=lan0\n"
        "TUN_IFACE=Meta\n"
        "SERVER_IP=192.168.1.10\n"
        f"WAN_INGRESS={wan_ingress}\n"
        'WAN_PORTS="8081/tcp 51820/udp"\n'
        f"WAN_INGRESS_MARK={MARK}\n"
        f"WAN_INGRESS_RULE_PRIO={PRIO}\n",
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
        for iface in ("lan0", "Meta"):
            ns_ok("ip", "link", "add", iface, "type", "dummy")
            ns_ok("ip", "link", "set", iface, "up")
        ns_ok("ip", "link", "set", "lo", "up")

        ns_ok("ip", "addr", "add", "192.168.1.10/24", "dev", "lan0")
        ns_ok("ip", "route", "add", "default", "via", "192.168.1.1", "dev", "lan0")
        # Имитация mihomo: TUN-таблица 2022 и правило для локального трафика.
        ns_ok("ip", "route", "add", "default", "dev", "Meta", "table", "2022")
        ns_ok("ip", "rule", "add", "iif", "lo", "lookup", "2022", "priority", "9002")

        if ns("iptables", "-S").returncode != 0:
            pytest.skip("iptables внутри netns не работает")

        res = run_fw(etc, "start")
        assert res.returncode == 0, res.stderr
        yield etc
    finally:
        _run("ip", "netns", "del", NS)


def test_mark_changes_route(started):
    # До start обе проверки уходят в TUN: правило 9002 заворачивает всё в 2022.
    res = run_fw(started, "stop")
    assert res.returncode == 0, res.stderr
    assert route_dev() == "Meta"
    assert route_dev("mark", MARK) == "Meta"

    # После start немаркированный трафик по-прежнему в TUN, помеченный — в main.
    res = run_fw(started, "start")
    assert res.returncode == 0, res.stderr
    assert route_dev() == "Meta"
    assert route_dev("mark", MARK) == "lan0"


def test_rule_8993_above_2022(started):
    lines = rule_lines()
    marked = [i for i, ln in enumerate(lines)
              if ln.startswith(f"{PRIO}:") and f"fwmark {MARK}/{MARK}" in ln
              and "lookup main" in ln]
    tun = [i for i, ln in enumerate(lines) if ln.startswith("9002:")]
    assert marked, f"нет правила {PRIO} с fwmark {MARK}/{MARK} lookup main:\n" + "\n".join(lines)
    assert tun, "нет правила 9002 (имитация mihomo)"
    assert marked[0] < tun[0], (
        f"правило {PRIO} (строка {marked[0]}) ниже 9002 (строка {tun[0]})"
    )
    assert sum(1 for ln in lines if ln.startswith(f"{PRIO}:")) == 1, lines


def test_prerouting_chain_order(started):
    rules = chain_rules("ROUTEWEAVE-WANMARK")
    assert rules, "цепочка ROUTEWEAVE-WANMARK пуста"

    # restore-mark первым: ответ контейнера с приватным src должен получить
    # метку раньше, чем сработает RETURN для приватных сетей.
    first = rules[0]
    assert "--restore-mark" in first, first
    assert "ESTABLISHED" in first and "RELATED" in first, first

    tun_return = [i for i, r in enumerate(rules) if "-i Meta" in r and "-j RETURN" in r]
    set_marks = [i for i, r in enumerate(rules)
                 if "--set-mark" in r or "--set-xmark" in r]
    assert tun_return, "нет RETURN для TUN_IFACE=Meta"
    assert set_marks, "нет правил set-mark"
    assert tun_return[0] < set_marks[0], "RETURN для Meta ниже set-mark"

    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"):
        assert any(f"-s {net}" in r and "-j RETURN" in r for r in rules), \
            f"нет RETURN для приватной сети {net}"

    assert any("-p tcp" in r and "--dport 8081" in r
               and ("--set-mark" in r or "--set-xmark" in r)
               for r in rules), "нет set-mark для tcp/8081"
    assert any("-p udp" in r and "--dport 51820" in r
               and ("--set-mark" in r or "--set-xmark" in r)
               for r in rules), "нет set-mark для udp/51820"


def test_output_chain_restore_only(started):
    rules = chain_rules("ROUTEWEAVE-WANMARK-OUT")
    assert len(rules) == 1, rules
    assert "--restore-mark" in rules[0], rules[0]
    assert "--set-mark" not in rules[0] and "--set-xmark" not in rules[0], rules[0]

    jumps = hook_jumps("OUTPUT", "ROUTEWEAVE-WANMARK-OUT")
    assert len(jumps) == 1, jumps


def test_second_start_does_not_duplicate(started):
    before_rules = chain_rules("ROUTEWEAVE-WANMARK")
    before_out = chain_rules("ROUTEWEAVE-WANMARK-OUT")
    before_pre = hook_jumps("PREROUTING", "ROUTEWEAVE-WANMARK")
    before_out_jumps = hook_jumps("OUTPUT", "ROUTEWEAVE-WANMARK-OUT")

    res = run_fw(started, "start")
    assert res.returncode == 0, res.stderr

    assert chain_rules("ROUTEWEAVE-WANMARK") == before_rules
    assert chain_rules("ROUTEWEAVE-WANMARK-OUT") == before_out
    assert hook_jumps("PREROUTING", "ROUTEWEAVE-WANMARK") == before_pre
    assert hook_jumps("OUTPUT", "ROUTEWEAVE-WANMARK-OUT") == before_out_jumps
    assert sum(1 for ln in rule_lines() if ln.startswith(f"{PRIO}:")) == 1


def test_doctor_ok(started):
    res = run_fw(started, "doctor")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout
    assert "FAIL" not in res.stdout


def test_status(started):
    res = run_fw(started, "status")
    assert res.returncode == 0, res.stderr
    assert "WAN_INGRESS" in res.stdout
    assert "8081" in res.stdout


def test_wan_ingress_no_disables(started, tmp_path):
    assert run_fw(started, "stop").returncode == 0

    etc_no = write_env(tmp_path / "etc_no", wan_ingress="no")
    res = run_fw(etc_no, "start")
    assert res.returncode == 0, res.stderr
    assert "WAN_INGRESS=no" in res.stdout

    assert ns("iptables", "-t", "mangle", "-S", "ROUTEWEAVE-WANMARK").returncode != 0
    assert ns("iptables", "-t", "mangle", "-S", "ROUTEWEAVE-WANMARK-OUT").returncode != 0
    assert not hook_jumps("PREROUTING", "ROUTEWEAVE-WANMARK")
    assert not hook_jumps("OUTPUT", "ROUTEWEAVE-WANMARK-OUT")
    assert not any(ln.startswith(f"{PRIO}:") for ln in rule_lines())


def test_stop_removes_everything(started):
    # start идемпотентен, поэтому тест не зависит от порядка запуска
    assert run_fw(started, "start").returncode == 0

    res = run_fw(started, "stop")
    assert res.returncode == 0, res.stderr

    assert not hook_jumps("PREROUTING", "ROUTEWEAVE-WANMARK")
    assert not hook_jumps("OUTPUT", "ROUTEWEAVE-WANMARK-OUT")
    assert ns("iptables", "-t", "mangle", "-S", "ROUTEWEAVE-WANMARK").returncode != 0, \
        "цепочка ROUTEWEAVE-WANMARK осталась после stop"
    assert ns("iptables", "-t", "mangle", "-S", "ROUTEWEAVE-WANMARK-OUT").returncode != 0, \
        "цепочка ROUTEWEAVE-WANMARK-OUT осталась после stop"
    assert not any(ln.startswith(f"{PRIO}:") for ln in rule_lines())

    # повторный stop — успех, а не "chain does not exist"
    again = run_fw(started, "stop")
    assert again.returncode == 0, again.stderr
