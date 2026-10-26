"""Политика входа на шлюз (ROUTEWEAVE-IN) и проверки конфигурации firewall.

Первая часть — статические проверки (без root и без iptables): структура
цепочек fw-base.sh, ограничения fw-wan.sh, валидация .env. Вторая —
привилегированный тест в двух network namespace (`rwin` — шлюз, `rwpeer` —
клиент), который реально проверяет, что сервисные порты дропаются с
«чужого» адреса и разрешены из LAN/lo/по SSH.

Только статика:
    pytest tests/test_input_policy.py -v -m "not privileged"

Полный прогон (root, netns и iptables):
    pytest tests/test_input_policy.py -v -rs
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FW_BASE = ROOT / "templates" / "fw-base.sh"
FW_WAN = ROOT / "templates" / "fw-wan.sh"

# SSH_PORT/PANEL_PORT/... не должны протечь из окружения CI.
CLEAN_ENV = {
    "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


def read(path):
    return path.read_text(encoding="utf-8")


def code_lines(text):
    """Логические строки без комментариев (продолжения склеены)."""
    joined = text.replace("\\\n", " ")
    return [line for line in joined.splitlines() if not line.lstrip().startswith("#")]


def strip_strings(line):
    """Убрать содержимое двойных кавычек: в сообщениях тоже есть слово iptables."""
    return re.sub(r'"[^"]*"', '""', line)


def find_line(text, needle):
    """Номер первой логической строки с подстрокой; -1, если нет."""
    for index, line in enumerate(code_lines(text)):
        if needle in line:
            return index
    return -1


def write_env(path, extra):
    path.mkdir(parents=True, exist_ok=True)
    # Значения экранируются: .env читается через `source`, и строка вида
    # SSH_PORT=22;rm -rf / иначе выполнилась бы как команда.
    (path / ".env").write_text(
        "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in extra.items()),
        encoding="utf-8",
    )
    return path


def run_script(script, etc, action):
    env = dict(CLEAN_ENV)
    env["ROUTEWEAVE_ETC"] = str(etc)
    return subprocess.run(
        ["bash", str(script), action],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


# --- fw-base.sh: структура ROUTEWEAVE-IN ----------------------------------


def test_fw_base_has_input_chain():
    text = read(FW_BASE)
    assert 'IN_CHAIN="ROUTEWEAVE-IN"' in text
    assert 'iptables -w 5 -I INPUT 1 -j "$IN_CHAIN"' in text
    # Идемпотентность: старые прыжки снимаются, цепочка чистится.
    assert 'while iptables -w 5 -D INPUT -j "$IN_CHAIN"' in text
    assert 'iptables -w 5 -F "$IN_CHAIN"' in text


def test_fw_base_input_rules_order_and_content():
    text = read(FW_BASE)
    lo = find_line(text, '-A "$IN_CHAIN" -i lo -j ACCEPT')
    established = find_line(
        text, '-A "$IN_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT'
    )
    icmp = find_line(text, '-A "$IN_CHAIN" -p icmp -j ACCEPT')
    ssh = find_line(text, '-A "$IN_CHAIN" -p tcp --dport "$SSH_PORT" -j ACCEPT')
    invalid = find_line(text, '-A "$IN_CHAIN" -m conntrack --ctstate INVALID -j DROP')
    cgnat = find_line(text, '! -i "$TS_IFACE" -s "$TS_SUBNET" -j DROP')
    ret = find_line(text, '-A "$IN_CHAIN" -j RETURN')

    for name, index in (
        ("lo", lo), ("established", established), ("icmp", icmp), ("ssh", ssh),
        ("invalid", invalid), ("cgnat", cgnat), ("return", ret),
    ):
        assert index >= 0, f"в ROUTEWEAVE-IN нет правила {name}"
    assert lo < established < icmp < ssh < invalid < cgnat < ret
    # Политику INPUT не меняем: финальный RETURN, а не DROP.
    assert "iptables -w 5 -P INPUT" not in text


def test_fw_base_guard_ports_and_zones():
    text = read(FW_BASE)
    # Сервисные порты: DNS (tcp+udp), панель, API mihomo, mixed-proxy.
    for snippet in (
        "add_in_tcp 53",
        'add_in_tcp "$PANEL_PORT"',
        "add_in_tcp 9090",
        "add_in_tcp 7890",
        "IN_UDP_PORTS=(53)",
        'PANEL_PORT="${PANEL_PORT:-8080}"',
        'SSH_PORT="${SSH_PORT:-22}"',
    ):
        assert snippet in text, f"нет {snippet!r}"

    # Доверенные зоны: LAN, tailscale0, wdtt0 и подсети.
    for snippet in (
        'add_in_iface "$LAN_IFACE"',
        'add_in_iface "$TS_IFACE"',
        'add_in_iface "wdtt0"',
        'add_in_net "$WG_SUBNET"',
        'add_in_net "$TS_SUBNET"',
        'add_in_net "100.64.0.0/10"',
    ):
        assert snippet in text, f"нет {snippet!r}"

    # Для каждого порта — разрешения по зонам и DROP из всех остальных.
    assert '-A "$IN_CHAIN" -i "$iface" -p tcp --dport "$port" -j ACCEPT' in text
    assert '-A "$IN_CHAIN" -s "$net" -p tcp --dport "$port" -j ACCEPT' in text
    assert '-A "$IN_CHAIN" -i "$iface" -p udp --dport "$port" -j ACCEPT' in text
    assert '-A "$IN_CHAIN" -p tcp --dport "$port" -j DROP' in text
    assert '-A "$IN_CHAIN" -p udp --dport "$port" -j DROP' in text

    # Пустой LAN_IFACE не должен давать -i "" (правило не добавится).
    assert "[ -n \"$iface\" ] || return 0" in text


def test_fw_base_stop_removes_input_chain():
    text = read(FW_BASE)
    stop = text.split("stop()")[1].split("doctor()")[0]
    assert 'while iptables -w 5 -D INPUT -j "$IN_CHAIN"' in stop
    assert 'iptables -w 5 -X "$IN_CHAIN"' in stop


def test_fw_base_quic_drop_skips_wan_ingress_mark():
    text = read(FW_BASE)
    assert 'WAN_INGRESS_MARK="${WAN_INGRESS_MARK:-0x200000}"' in text
    mark_return = find_line(
        text, '-m mark --mark "$WAN_INGRESS_MARK/$WAN_INGRESS_MARK" -j RETURN'
    )
    quic_drop = find_line(text, '-p udp --dport 443 -j DROP')
    assert mark_return >= 0, "нет RETURN по метке WAN_INGRESS перед DROP UDP/443"
    assert quic_drop >= 0, "нет DROP UDP/443"
    assert mark_return < quic_drop, "RETURN по метке должен стоять выше DROP UDP/443"


def test_fw_base_forward_invalid_drop_before_accepts():
    text = read(FW_BASE)
    invalid = find_line(text, '-A "$FWD_CHAIN" -m conntrack --ctstate INVALID -j DROP')
    lan_tun = find_line(text, '-A "$FWD_CHAIN" -i "$LAN_IFACE" -o "$TUN_IFACE" -j ACCEPT')
    assert invalid >= 0, "в ROUTEWEAVE-FWD нет DROP для INVALID"
    assert lan_tun >= 0
    assert invalid < lan_tun, "DROP INVALID должен стоять выше ACCEPT"


def test_fw_base_forward_jump_position_is_checked():
    text = read(FW_BASE)
    assert "fwd_jump_on_top() {" in text
    assert 'ufw-before-forward' in text and "DOCKER-USER" in text
    assert "прыжок $FWD_CHAIN не первый в FORWARD" in text


def test_fw_base_doctor_is_read_only_and_warns_about_ipv6():
    text = read(FW_BASE)
    doctor = text.split("doctor() {")[1].split('case "${1:-}"')[0]
    assert "IPv6" in doctor
    assert "не фильтруется" in doctor
    for line in code_lines(doctor):
        assert not re.search(r"iptables -w 5 -(A|I|D|F|N|X)\b", line), (
            f"doctor меняет iptables: {line.strip()}"
        )
    # doctor должен проверять позицию прыжка и порты.
    assert "fwd_jump_on_top" in doctor
    assert '--dport $port' in doctor


def test_fw_base_validates_ports():
    for value in ("0", "65536", "abc", "-1", "22;rm -rf /"):
        etc = Path(f"/tmp/rw-unused-etc-{os.getpid()}")
        write_env(etc, {"SSH_PORT": value})
        res = run_script(FW_BASE, etc, "start")
        assert res.returncode == 1, (value, res.stdout, res.stderr)
        assert "SSH_PORT" in res.stderr, res.stderr


def test_fw_base_rejects_bad_panel_port():
    etc = Path(f"/tmp/rw-unused-etc-panel-{os.getpid()}")
    write_env(etc, {"PANEL_PORT": "70000"})
    res = run_script(FW_BASE, etc, "start")
    assert res.returncode == 1
    assert "PANEL_PORT" in res.stderr


# --- fw-wan.sh: метки, приоритеты, doctor ---------------------------------


def test_fw_wan_marks_new_with_mark_and_connmark():
    text = read(FW_BASE) + read(FW_WAN)
    assert 'WAN_INGRESS_MARK="${WAN_INGRESS_MARK:-0x200000}"' in text
    # mark_new: MARK на пакет и CONNMARK на соединение, MARK раньше.
    assert "-j MARK --set-xmark" in text
    assert "-j CONNMARK --set-mark" in text
    mark = find_line(text, "-j MARK --set-xmark")
    conn = find_line(text, "-j CONNMARK --set-mark")
    assert 0 <= mark < conn


def test_fw_wan_rejects_reserved_marks_and_prios():
    err = "должен быть целым числом"
    assert 'RESERVED_MARKS="0x40000 0x80000 0x100000"' in read(FW_WAN)
    assert 'RESERVED_PRIOS="8990 8991 8992 9000 9001 9002"' in read(FW_WAN)

    for mark in ("0x0", "0x40000", "0x80000", "0x100000", "0x240000"):
        etc = Path(f"/tmp/rw-wan-mark-{os.getpid()}")
        write_env(etc, {"WAN_INGRESS": "yes", "WAN_INGRESS_MARK": mark})
        res = run_script(FW_WAN, etc, "doctor")
        assert res.returncode == 1, (mark, res.stdout, res.stderr)
        assert "WAN_INGRESS_MARK" in res.stderr, res.stderr

    for prio in ("8990", "8991", "8992", "9000", "9001", "9002"):
        etc = Path(f"/tmp/rw-wan-prio-{os.getpid()}")
        write_env(etc, {"WAN_INGRESS_RULE_PRIO": prio})
        res = run_script(FW_WAN, etc, "status")
        assert res.returncode == 1, (prio, res.stdout, res.stderr)
        assert "зарезервирован" in res.stderr, res.stderr
        assert err not in res.stderr


def test_fw_wan_remove_ip_rule_is_exact():
    text = read(FW_WAN)
    joined = " ".join(" ".join(code_lines(text)).split())
    assert (
        'ip rule del fwmark "$WAN_INGRESS_MARK/$WAN_INGRESS_MARK" '
        'lookup main priority "$WAN_INGRESS_RULE_PRIO"' in joined
    ), "remove_ip_rule должен удалять только точное совпадение правила"
    # Удаления «всего по priority» быть не должно.
    assert 'ip rule del priority "$WAN_INGRESS_RULE_PRIO"' not in text


def test_fw_wan_doctor_checks_forwarded_path():
    text = read(FW_WAN)
    doctor = text.split("doctor() {")[1].split('case "${1:-}"')[0]
    assert 'iif "$LAN_IFACE" from "$client"' in doctor
    doctor_code = "\n".join(code_lines(doctor))
    assert 'from "$SERVER_IP"' not in doctor_code, (
        "from SERVER_IP — это локальный путь, а не forwarded"
    )
    assert "локальный путь, не forwarded" in doctor
    # При отсутствии правила 8993 doctor обязан провалиться.
    assert "FAIL: нет ip rule priority" in doctor


def test_fw_wan_warns_about_quic_port():
    text = read(FW_WAN)
    assert "ports_include_udp_443" in text
    assert "443/udp" in text
    start = text.split("start() {")[1].split("stop() {")[0]
    assert "ports_include_udp_443" in start


# --- Все вызовы iptables ждут xtables-lock --------------------------------


def test_every_iptables_call_uses_wait():
    for path in (FW_BASE, FW_WAN):
        for line in code_lines(read(path)):
            if "iptables" not in line:
                continue
            cleaned = strip_strings(line)
            for chunk in cleaned.split("iptables")[1:]:
                assert chunk.startswith(" -w 5"), (
                    f"{path.name}: iptables без -w 5: {line.strip()}"
                )


# --- Привилегированные тесты: реальные пакеты в netns ---------------------

NS = "rwin"
PEER = "rwpeer"
WAN_IP = "198.51.100.1"
PEER_WAN_IP = "198.51.100.5"
LAN_IP = "192.168.50.1"
PEER_LAN_IP = "192.168.50.5"
SSH_PORT = "2222"
PANEL_PORT = "8080"
GUARD_TCP = ("8080", "9090", "7890", "53")

CONNECT = """
import socket
import sys
sock = socket.socket()
sock.settimeout(0.6)
try:
    sock.connect((sys.argv[1], int(sys.argv[2])))
    print("ok")
except OSError:
    print("fail")
finally:
    sock.close()
"""

SEND_UDP = """
import socket
import sys
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.4)
try:
    sock.sendto(b"probe", (sys.argv[1], int(sys.argv[2])))
    sock.recvfrom(64)
except OSError:
    pass
finally:
    sock.close()
"""

LISTENER = """
import socket
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 8080))
srv.listen(16)
while True:
    conn, _ = srv.accept()
    try:
        conn.recv(64)
    finally:
        conn.close()
"""


def _run(*args):
    return subprocess.run(args, check=False, text=True, capture_output=True)


def ns_run(namespace, *args):
    return _run("ip", "netns", "exec", namespace, *args)


def ns_ok(namespace, *args):
    res = ns_run(namespace, *args)
    assert res.returncode == 0, f"{' '.join(args)}: {res.stderr.strip()}"
    return res.stdout


def run_fw(etc, action):
    return _run(
        "ip", "netns", "exec", NS,
        "env", f"ROUTEWEAVE_ETC={etc}",
        "bash", str(FW_BASE), action,
    )


def connect_from(namespace, host, port):
    return ns_run(namespace, sys.executable, "-c", CONNECT, host, str(port))


def zero_counters():
    ns_ok(NS, "iptables", "-Z", "ROUTEWEAVE-IN")


def rule_counters():
    """[(pkts, tokens)] по правилам ROUTEWEAVE-IN."""
    out = ns_ok(NS, "iptables", "-vnx", "-L", "ROUTEWEAVE-IN")
    result = []
    for line in out.splitlines():
        tokens = line.split()
        if len(tokens) < 4 or not tokens[0].isdigit():
            continue
        result.append((int(tokens[0]), tokens))
    return result


def counter(target, dpt=None, proto=None, iface=None, ctstate=None):
    total = 0
    for pkts, tokens in rule_counters():
        # tokens: pkts bytes target prot opt in out src dst [match...]
        if tokens[2] != target:
            continue
        if proto and not any(tok == proto for tok in tokens[3:5]):
            continue
        if dpt and f"dpt:{dpt}" not in tokens:
            continue
        if iface and iface not in tokens:
            continue
        if ctstate and not any(ctstate in tok for tok in tokens):
            continue
        total += pkts
    return total


def wait_listener():
    for _ in range(40):
        res = connect_from(NS, "127.0.0.1", PANEL_PORT)
        if "ok" in res.stdout:
            return
        time.sleep(0.05)
    raise AssertionError("тестовый listener на :8080 не поднялся")


@pytest.fixture(scope="module")
def input_ns(tmp_path_factory):
    if os.geteuid() != 0:
        pytest.skip("нужен root: netns и iptables")
    if shutil.which("iptables") is None:
        pytest.skip("нет iptables")

    etc = write_env(
        tmp_path_factory.mktemp("etc-in"),
        {
            "LAN_IFACE": "lan0",
            "TUN_IFACE": "Meta",
            "SERVER_IP": LAN_IP,
            "PANEL_PORT": PANEL_PORT,
            "SSH_PORT": SSH_PORT,
            "QUIC_DROP": "yes",
            "WG_SUBNET": "10.77.0.0/24",
            "TS_SUBNET": "100.64.0.0/10",
            "WAN_INGRESS": "no",
        },
    )

    for name in (NS, PEER):
        _run("ip", "netns", "del", name)  # мусор от упавшего прогона
    if _run("ip", "netns", "add", NS).returncode != 0:
        pytest.skip("ip netns add недоступен")
    if _run("ip", "netns", "add", PEER).returncode != 0:
        _run("ip", "netns", "del", NS)
        pytest.skip("ip netns add для peer недоступен")

    listener = None
    try:
        # lan0 <-> lan1 (доверенная LAN) и wan0 <-> wan1 (WAN) между ns.
        ns_ok(NS, "ip", "link", "add", "lan0", "type", "veth", "peer", "name", "lan1")
        ns_ok(NS, "ip", "link", "add", "wan0", "type", "veth", "peer", "name", "wan1")
        ns_ok(NS, "ip", "link", "set", "lan1", "netns", PEER)
        ns_ok(NS, "ip", "link", "set", "wan1", "netns", PEER)

        ns_ok(NS, "ip", "addr", "add", f"{LAN_IP}/24", "dev", "lan0")
        ns_ok(NS, "ip", "addr", "add", f"{WAN_IP}/24", "dev", "wan0")
        for iface in ("lan0", "wan0", "lo"):
            ns_ok(NS, "ip", "link", "set", iface, "up")

        ns_ok(PEER, "ip", "addr", "add", f"{PEER_LAN_IP}/24", "dev", "lan1")
        ns_ok(PEER, "ip", "addr", "add", f"{PEER_WAN_IP}/24", "dev", "wan1")
        for iface in ("lan1", "wan1", "lo"):
            ns_ok(PEER, "ip", "link", "set", iface, "up")

        if ns_run(NS, "iptables", "-S").returncode != 0:
            pytest.skip("iptables внутри netns не работает")

        res = run_fw(etc, "start")
        assert res.returncode == 0, res.stderr

        listener = subprocess.Popen(
            ["ip", "netns", "exec", NS, sys.executable, "-c", LISTENER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_listener()
        yield etc
    finally:
        if listener is not None:
            listener.terminate()
            try:
                listener.wait(timeout=5)
            except subprocess.TimeoutExpired:
                listener.kill()
        for name in (NS, PEER):
            _run("ip", "netns", "del", name)


@pytest.mark.privileged
@pytest.mark.skipif(os.geteuid() != 0, reason="нужен root: netns и iptables")
@pytest.mark.skipif(shutil.which("ip") is None, reason="нет ip из iproute2")
@pytest.mark.skipif(shutil.which("iptables") is None, reason="нет iptables")
class TestInputPolicy:
    def test_jump_is_first_in_input(self, input_ns):
        rules = [
            line for line in ns_ok(NS, "iptables", "-S", "INPUT").splitlines()
            if line.startswith("-A ")
        ]
        assert rules, "INPUT пуст"
        assert rules[0].endswith("-j ROUTEWEAVE-IN"), rules
        assert ns_run(NS, "iptables", "-S", "ROUTEWEAVE-IN").returncode == 0

    @pytest.mark.parametrize("port", GUARD_TCP)
    def test_untrusted_tcp_is_dropped(self, input_ns, port):
        zero_counters()
        connect_from(PEER, WAN_IP, port)
        assert counter("DROP", dpt=port, proto="tcp") > 0, (
            f"tcp/{port} с адреса {PEER_WAN_IP} (iif wan0) не попал в DROP"
        )

    def test_untrusted_udp_dns_is_dropped(self, input_ns):
        zero_counters()
        ns_run(PEER, sys.executable, "-c", SEND_UDP, WAN_IP, "53")
        assert counter("DROP", dpt="53", proto="udp") > 0, (
            "udp/53 с адреса WAN не попал в DROP"
        )

    def test_lan_is_allowed(self, input_ns):
        zero_counters()
        res = connect_from(PEER, LAN_IP, PANEL_PORT)
        assert "ok" in res.stdout, res.stdout + res.stderr
        assert counter("ACCEPT", dpt=PANEL_PORT, iface="lan0") > 0
        assert counter("DROP", dpt=PANEL_PORT) == 0

    def test_loopback_is_allowed(self, input_ns):
        zero_counters()
        res = connect_from(NS, "127.0.0.1", PANEL_PORT)
        assert "ok" in res.stdout, res.stdout + res.stderr
        assert counter("ACCEPT", dpt=PANEL_PORT, iface="lo") > 0

    def test_ssh_is_allowed_from_wan(self, input_ns):
        zero_counters()
        connect_from(PEER, WAN_IP, SSH_PORT)
        assert counter("ACCEPT", dpt=SSH_PORT) > 0, (
            "SSH с адреса WAN не попал в ACCEPT"
        )

    def test_established_is_allowed(self, input_ns):
        zero_counters()
        res = connect_from(PEER, LAN_IP, PANEL_PORT)
        assert "ok" in res.stdout, res.stdout + res.stderr
        assert counter("ACCEPT", ctstate="ESTABLISHED") > 0, (
            "ответный/установленный трафик не попал в ACCEPT ESTABLISHED,RELATED"
        )

    def test_doctor_passes(self, input_ns):
        res = run_fw(input_ns, "doctor")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "FAIL" not in res.stdout, res.stdout
        # IPv6 осознанно не фильтруется — doctor обязан это упомянуть.
        assert "IPv6" in res.stdout

    def test_stop_removes_input_chain(self, input_ns):
        assert run_fw(input_ns, "stop").returncode == 0
        input_rules = ns_ok(NS, "iptables", "-S", "INPUT")
        assert "ROUTEWEAVE-IN" not in input_rules
        assert ns_run(NS, "iptables", "-S", "ROUTEWEAVE-IN").returncode != 0
        # Возвращаем состояние: fixture живёт до конца модуля.
        assert run_fw(input_ns, "start").returncode == 0
