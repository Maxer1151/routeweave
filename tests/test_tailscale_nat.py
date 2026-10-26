"""Тесты NAT-режима exit node Tailscale (`TS_EXIT_MODE=nat`).

Первая группа — статические проверки (без root): разбор режима, имена
цепочек/метки/приоритета, fail-safe и документация. Вторая — привилегированные
тесты в отдельном netns `rwnat` по образцу `test_tailscale_exit.py`.

Запуск только статики:
    pytest tests/test_tailscale_nat.py -v -m "not privileged"

Полный прогон (root, netns и iptables):
    pytest tests/test_tailscale_nat.py -v -rs

Если root нет, iptables не установлен, netns недоступен или в netns нет таблиц
nat/mangle — привилегированные тесты помечаются skip, а не падают.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FW_BASE = ROOT / "templates" / "fw-base.sh"
FW_TS = ROOT / "templates" / "fw-tailscale.sh"
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
DOCS = ROOT / "docs" / "tailscale-exit-node.md"
SECURITY = ROOT / "SECURITY.md"
TODO = ROOT / "TODO.md"

MARK = "0x100000"
PRIO = "8992"
MARK_CHAIN = "ROUTEWEAVE-TSMARK"
NAT_CHAIN = "ROUTEWEAVE-TSNAT"


def read(path):
    return path.read_text(encoding="utf-8")


def code_lines(text):
    """Строки без комментариев; продолжения строк склеены.

    В шапках MASQUERADE/-I упоминаются словами, а правила iptables разбиты
    переносом, поэтому проверять нужно логические команды.
    """
    joined = text.replace("\\\n", " ")
    return [
        line for line in joined.splitlines()
        if not line.lstrip().startswith("#")
    ]


# --- Статические проверки -------------------------------------------------


def test_fw_base_parses_mode_and_constants():
    text = read(FW_BASE)
    assert 'TS_EXIT_MODE="${TS_EXIT_MODE:-policy}"' in text
    assert 'case "$TS_EXIT_MODE" in' in text
    assert "policy | nat" in text
    assert f'TS_NAT_MARK="{MARK}"' in text
    assert f"TS_NAT_RULE_PRIO={PRIO}" in text
    assert f'TS_MARK_CHAIN="{MARK_CHAIN}"' in text
    assert f'TS_NAT_CHAIN="{NAT_CHAIN}"' in text


def test_fw_base_cleanup_is_called_and_idempotent():
    text = read(FW_BASE)
    assert "ts_nat_cleanup() {" in text
    # Определение + вызовы в start и stop.
    assert text.count("ts_nat_cleanup") >= 3
    # Прыжки/цепочки снимаются циклами с || true: повторный вызов не падает.
    assert '-D PREROUTING -j "$TS_MARK_CHAIN"' in text
    assert '-D POSTROUTING -j "$TS_NAT_CHAIN"' in text
    assert 'ip rule del priority "$TS_NAT_RULE_PRIO"' in text


def test_fw_base_creates_nat_artifacts():
    text = read(FW_BASE)
    assert 'iptables -t mangle -N "$TS_MARK_CHAIN"' in text
    assert '--set-xmark "$TS_NAT_MARK/$TS_NAT_MARK"' in text
    assert 'iptables -t mangle -I PREROUTING 1 -j "$TS_MARK_CHAIN"' in text
    assert 'iptables -t nat -N "$TS_NAT_CHAIN"' in text
    assert 'iptables -t nat -I POSTROUTING 1 -j "$TS_NAT_CHAIN"' in text
    assert 'ip rule add fwmark "$TS_NAT_MARK/$TS_NAT_MARK" lookup main priority "$TS_NAT_RULE_PRIO"' in text
    assert "TS_MODE=$TS_EXIT_MODE" in text


def test_fw_base_masquerade_always_has_source():
    for line in code_lines(read(FW_BASE)):
        if "-j MASQUERADE" not in line:
            continue
        assert '-s "$TS_SUBNET"' in line, f"MASQUERADE без -s: {line.strip()}"
        assert '-o "$LAN_IFACE"' in line, f"MASQUERADE без -o: {line.strip()}"
        assert '--mark "$TS_NAT_MARK/$TS_NAT_MARK"' in line, f"MASQUERADE без метки: {line.strip()}"


def test_fw_base_falls_back_to_policy():
    text = read(FW_BASE)
    # Мусор/пустые интерфейсы -> warn + policy, а не падение.
    assert "не policy/nat, считаю policy" in text
    assert "работаю как policy" in text
    assert 'TS_EXIT_MODE="policy"' in text
    # start не должен завершаться ошибкой из-за режима.
    assert "exit 1" not in text.split("start()")[1].split("stop()")[0]


def test_fw_tailscale_branches_and_is_read_only():
    text = read(FW_TS)
    assert 'TS_EXIT_MODE="${TS_EXIT_MODE:-policy}"' in text
    assert f'TS_NAT_MARK="{MARK}"' in text
    assert f"TS_NAT_RULE_PRIO={PRIO}" in text
    assert f'TS_MARK_CHAIN="{MARK_CHAIN}"' in text
    assert f'TS_NAT_CHAIN="{NAT_CHAIN}"' in text
    # Ветвление есть и в status, и в doctor.
    assert text.count('[ "$TS_EXIT_MODE" = "nat" ]') >= 2
    assert "TS_EXIT_MODE: $TS_EXIT_MODE" in text
    assert "MASQUERADE" in text
    assert 'ip route get 1.1.1.1 mark "$TS_NAT_MARK"' in text
    assert 'from "$ts_ip" iif "$TS_IFACE" mark "$TS_NAT_MARK"' in text

    # Скрипт обязан оставаться читающим: никаких изменений iptables/ip rule.
    for line in code_lines(text):
        for forbidden in ("-N ", " -A ", " -I ", "iptables -F", "ip rule add", "ip rule del"):
            assert forbidden not in line, f"doctor/status меняет состояние: {line.strip()}"


def test_env_example_has_default_mode():
    assert 'TS_EXIT_MODE="policy"' in read(ENV_EXAMPLE)


def test_docs_mention_both_modes():
    readme = read(README)
    assert "TS_EXIT_MODE" in readme
    assert "`nat`" in readme
    assert "docs/tailscale-exit-node.md" in readme

    changelog = read(CHANGELOG)
    assert "TS_EXIT_MODE=nat" in changelog

    docs = read(DOCS)
    assert "TS_EXIT_MODE" in docs
    assert "Режим exit node: policy и nat" in docs
    assert "8992" in docs
    assert MARK in docs
    assert "Прямого NAT-режима нет" not in docs

    security = read(SECURITY)
    assert "TS_EXIT_MODE=nat" in security

    todo = read(TODO)
    assert "IPv6" in todo
    assert "прямой NAT-режим для exit node" not in todo


# --- Привилегированные проверки в netns ------------------------------------

NS = "rwnat"
TUN_IFACE = "Meta"
TS_IFACE = "tailscale0"
TS_SUBNET = "100.64.0.0/10"
LAN_IFACE = "lan0"
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


def run_fw_ts(etc, action):
    return _run(
        "ip", "netns", "exec", NS,
        "env", f"ROUTEWEAVE_ETC={etc}",
        "bash", str(FW_TS), action,
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


def rules_with_prio(prio):
    return [line for line in rule_lines() if line.startswith(f"{prio}:")]


def jumps(table, hook, chain):
    out = ns_ok("iptables", "-t", table, "-S", hook)
    return [line for line in out.splitlines() if f"-j {chain}" in line]


def write_env(path, mode):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".env").write_text(
        f"LAN_IFACE={LAN_IFACE}\n"
        f"TUN_IFACE={TUN_IFACE}\n"
        f"TS_IFACE={TS_IFACE}\n"
        f"TS_SUBNET={TS_SUBNET}\n"
        f"TS_EXIT_MODE={mode}\n"
        "QUIC_DROP=yes\n"
        "WG_SUBNET=10.77.0.0/24\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def started(tmp_path_factory):
    etc = write_env(tmp_path_factory.mktemp("etc-nat"), "nat")

    _run("ip", "netns", "del", NS)  # мусор от упавшего прогона
    res = _run("ip", "netns", "add", NS)
    if res.returncode != 0:
        pytest.skip(f"ip netns add недоступен: {res.stderr.strip()}")

    try:
        for iface in (LAN_IFACE, TUN_IFACE, TS_IFACE):
            ns_ok("ip", "link", "add", iface, "type", "dummy")
            ns_ok("ip", "link", "set", iface, "up")
        ns_ok("ip", "link", "set", "lo", "up")

        # WAN в main и tailnet-маршрут для правила 8991.
        ns_ok("ip", "route", "add", "default", "dev", LAN_IFACE)
        ns_ok("ip", "route", "add", TS_SUBNET, "dev", TS_IFACE)
        # Имитация mihomo TUN: default в table 2022 + правило 9002.
        ns_ok("ip", "route", "add", "default", "dev", TUN_IFACE, "table", "2022")
        ns_ok("ip", "rule", "add", "lookup", "2022", "priority", POLICY_PRIO)

        if ns("iptables", "-S").returncode != 0:
            pytest.skip("iptables внутри netns не работает")
        # Проверяем именно то, что нужно fw-base: создать цепочку в mangle/nat
        # и добавить ip rule. Если таблица недоступна — skip, а не падение.
        for table in ("mangle", "nat"):
            probe = ns("iptables", "-t", table, "-N", "RWNATPROBE")
            if probe.returncode != 0:
                pytest.skip(
                    f"iptables -t {table} в netns недоступна: {probe.stderr.strip()}"
                )
            ns("iptables", "-t", table, "-X", "RWNATPROBE")
        probe = ns("ip", "rule", "add", "priority", "65534", "lookup", "main")
        if probe.returncode != 0:
            pytest.skip(f"ip rule в netns недоступен: {probe.stderr.strip()}")
        ns("ip", "rule", "del", "priority", "65534")

        res = run_fw(etc, "start")
        assert res.returncode == 0, res.stderr
        yield etc
    finally:
        _run("ip", "netns", "del", NS)


@pytest.mark.privileged
@pytest.mark.skipif(os.geteuid() != 0, reason="нужен root: netns и iptables")
@pytest.mark.skipif(shutil.which("ip") is None, reason="нет ip из iproute2")
@pytest.mark.skipif(shutil.which("iptables") is None, reason="нет iptables")
class TestNatNetns:
    def test_start_is_idempotent(self, started):
        assert run_fw(started, "start").returncode == 0
        assert len(jumps("mangle", "PREROUTING", MARK_CHAIN)) == 1
        assert len(jumps("nat", "POSTROUTING", NAT_CHAIN)) == 1
        assert len(rules_with_prio(PRIO)) == 1

    def test_rule_8992_and_8991(self, started):
        ours = [line for line in rules_with_prio(PRIO)
                if f"fwmark {MARK}/{MARK}" in line and "lookup main" in line]
        assert len(ours) == 1, f"ожидалось одно правило {PRIO}:\n" + "\n".join(rule_lines())
        # 8991 сохраняется в NAT-режиме: ответы tailnet-клиентам идут в main.
        assert any(f"to {TS_SUBNET}" in line and "lookup main" in line
                   for line in rules_with_prio("8991"))

    def test_mangle_marks_tailscale0(self, started):
        rules = ns_ok("iptables", "-t", "mangle", "-S", MARK_CHAIN)
        assert f"-i {TS_IFACE}" in rules
        assert f"--set-xmark {MARK}/{MARK}" in rules
        assert len(jumps("mangle", "PREROUTING", MARK_CHAIN)) == 1

    def test_nat_masquerades_to_lan(self, started):
        rules = ns_ok("iptables", "-t", "nat", "-S", NAT_CHAIN)
        assert f"-s {TS_SUBNET}" in rules
        assert f"-o {LAN_IFACE}" in rules
        assert f"--mark {MARK}/{MARK}" in rules
        assert "MASQUERADE" in rules
        assert len(jumps("nat", "POSTROUTING", NAT_CHAIN)) == 1

    def test_marked_route_goes_to_wan(self, started):
        assert route_dev("1.1.1.1") == TUN_IFACE
        assert route_dev("1.1.1.1", "mark", MARK) == LAN_IFACE

    def test_marked_forwarded_route_goes_to_wan(self, started):
        dev = route_dev(
            "1.1.1.1", "from", "100.64.0.5", "iif", TS_IFACE, "mark", MARK,
        )
        assert dev == LAN_IFACE

    def test_tailnet_route_stays_in_main(self, started):
        assert route_dev("100.64.0.1") == TS_IFACE

    def test_status_reports_nat(self, started):
        res = run_fw_ts(started, "status")
        assert res.returncode == 0, res.stderr
        assert "TS_EXIT_MODE: nat" in res.stdout
        assert PRIO in res.stdout
        assert NAT_CHAIN in res.stdout
        assert MARK_CHAIN in res.stdout

    def test_switch_back_to_policy(self, started):
        write_env(started, "policy")
        res = run_fw(started, "start")
        assert res.returncode == 0, res.stderr

        assert not rules_with_prio(PRIO)
        assert ns("iptables", "-t", "mangle", "-S", MARK_CHAIN).returncode != 0
        assert ns("iptables", "-t", "nat", "-S", NAT_CHAIN).returncode != 0
        assert route_dev("1.1.1.1", "mark", MARK) == TUN_IFACE

    def test_stop_removes_nat_artifacts(self, started):
        # Возвращаем nat, чтобы stop снимал именно артефакты NAT-режима.
        write_env(started, "nat")
        assert run_fw(started, "start").returncode == 0
        assert rules_with_prio(PRIO)

        res = run_fw(started, "stop")
        assert res.returncode == 0, res.stderr

        assert not rules_with_prio(PRIO)
        assert ns("iptables", "-t", "mangle", "-S", MARK_CHAIN).returncode != 0
        assert ns("iptables", "-t", "nat", "-S", NAT_CHAIN).returncode != 0

        # повторный stop — успех, а не "chain does not exist"
        again = run_fw(started, "stop")
        assert again.returncode == 0, again.stderr
