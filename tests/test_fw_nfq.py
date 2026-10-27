"""Тесты templates/fw-nfq.sh: NFQUEUE-правило для nfqws.

Две группы:

* статические (всегда): каждый вызов iptables ждёт xtables-lock (`-w 5`), а
  ошибка вставки правила приводит к `exit 1`, а не к «успеху без правила»;
* динамические под root: скрипт запускается в отдельном network namespace и
  проверяется, что правило появляется, не дублируется и снимается.

Без root, ip или iptables динамические тесты помечаются skip — на машине
разработчика это норма, статические обязаны проходить всегда.
"""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
FW_NFQ = REPO / "templates" / "fw-nfq.sh"

NS = "rw-nfqtest"
QNUM = "2211"

# iptables-опция ожидания: -w 5 или --wait 5/--wait=5.
WAIT_RE = re.compile(r"(?:^|\s)(?:-w\s+5|--wait(?:=|\s+)5)(?:\s|$)")


def _code_lines():
    """Строки без комментариев: (номер, текст)."""
    lines = FW_NFQ.read_text(encoding="utf-8").splitlines()
    return [
        (num, line)
        for num, line in enumerate(lines, start=1)
        if not line.lstrip().startswith("#")
    ]


def _iptables_lines():
    return [(num, line) for num, line in _code_lines() if "iptables" in line]


def _logical_lines(text: str) -> list[str]:
    """Склеивает строки с продолжением `\\` в одну команду."""
    result: list[str] = []
    buf = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1].rstrip() + " "
            continue
        buf += stripped
        if buf:
            result.append(buf)
        buf = ""
    if buf:
        result.append(buf)
    return result


# --------------------------------------------------------------------------
# статические проверки
# --------------------------------------------------------------------------

def test_script_exists():
    assert FW_NFQ.is_file()


def test_every_iptables_call_waits_for_lock():
    calls = _iptables_lines()
    assert calls, "в fw-nfq.sh не найдено вызовов iptables"
    without_wait = [(num, line.strip()) for num, line in calls if not WAIT_RE.search(line)]
    assert not without_wait, "iptables без -w 5 (гонка с Docker/ufw): " + "; ".join(
        f"строка {num}: {line}" for num, line in without_wait
    )


def test_wait_timeout_is_five_seconds():
    for num, line in _iptables_lines():
        if "-w" in line:
            assert "-w 5" in line, f"строка {num}: ожидался таймаут -w 5, получено: {line.strip()}"
        else:
            assert "--wait" in line, f"строка {num}: нет -w/--wait: {line.strip()}"


def test_insert_failure_exits_nonzero():
    text = FW_NFQ.read_text(encoding="utf-8")
    start_body = text.split("start()", 1)[1].split("stop()", 1)[0]
    assert "-I POSTROUTING" in start_body, "start() не вставляет правило"
    assert "exit 1" in start_body, "ошибка вставки правила не приводит к exit 1"
    assert re.search(
        r"-I POSTROUTING.*\|\|\s*\{[^}]*exit 1", start_body, re.DOTALL
    ), "нет обработки неудачной вставки правила (|| { ...; exit 1; })"


def test_optional_deletion_still_tolerates_absence():
    text = FW_NFQ.read_text(encoding="utf-8")
    stop_body = text.split("stop()", 1)[1].split("case ", 1)[0]
    delete_lines = [ln.strip() for ln in stop_body.splitlines() if "-D POSTROUTING" in ln]
    assert delete_lines, "stop() не удаляет правило"
    assert any("|| true" in ln for ln in delete_lines), "stop() должен переживать отсутствие правила"


def test_bash_syntax_ok():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("нет bash")
    res = subprocess.run([bash, "-n", str(FW_NFQ)], check=False, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_rule_body_unchanged():
    """Правило осталось прежним: multiport 80/443, connbytes, NFQUEUE, queue-bypass."""
    text = FW_NFQ.read_text(encoding="utf-8")
    for needle in (
        '--dports "80,443"',
        "--connbytes 1:10",
        "-j NFQUEUE",
        "--queue-bypass",
        '-m mark ! --mark "$NFQWS_MARK/$NFQWS_MARK"',
    ):
        assert needle in text, f"из правила пропало: {needle}"


def test_rule_excludes_nfqws_mark():
    """Фейки nfqws не должны возвращаться в ту же очередь (дедлок sendto)."""
    text = FW_NFQ.read_text(encoding="utf-8")
    assert "-m mark ! --mark" in text
    assert "$NFQWS_MARK/$NFQWS_MARK" in text


def test_legacy_rule_without_mark_is_deleted():
    """start и stop снимают правило прежних версий — без исключения метки."""
    text = FW_NFQ.read_text(encoding="utf-8")
    legacy = [
        cmd
        for cmd in _logical_lines(text)
        if "-D POSTROUTING" in cmd and "-j NFQUEUE" in cmd
    ]
    assert legacy, "нет удаления legacy-правила без метки"
    for cmd in legacy:
        assert "--queue-bypass" in cmd, cmd
        assert "--mark" not in cmd, f"legacy-правило не должно содержать метку: {cmd}"

    start_body = text.split("start()", 1)[1].split("stop()", 1)[0]
    stop_body = text.split("stop()", 1)[1].split("case ", 1)[0]
    assert "delete_legacy_rule" in start_body, "start() не удаляет legacy-правило"
    assert "delete_legacy_rule" in stop_body, "stop() не удаляет legacy-правило"


# --------------------------------------------------------------------------
# сборка правила без root: фейковый iptables записывает argv
# --------------------------------------------------------------------------

class TestRuleBuild:
    """Проверяет текст правила и миграцию без netns: iptables — фейк."""

    @pytest.fixture
    def sandbox(self, tmp_path):
        etc = tmp_path / "etc"
        etc.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "iptables.log"

        fake_iptables = bin_dir / "iptables"
        fake_iptables.write_text(
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$*" >> "$FAKE_IPT_LOG"
                case " $* " in
                  *" -C "*) exit 1 ;;
                  *) exit 0 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        fake_iptables.chmod(0o755)
        (bin_dir / "pgrep").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (bin_dir / "pgrep").chmod(0o755)

        def write_conf(extra_line: str = "") -> None:
            (etc / "zapret.conf").write_text(
                "WAN_IFACE=dummy0\n"
                f"QNUM={QNUM}\n"
                f"HOSTLIST={tmp_path / 'run' / 'dpi-zapret.txt'}\n"
                f"SRC_HOSTLIST={tmp_path / 'missing-dpi.txt'}\n"
                f"{extra_line}\n",
                encoding="utf-8",
            )

        write_conf()

        def run(action: str = "start"):
            env = {
                "PATH": f"{bin_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LC_ALL": "C",
                "ROUTEWEAVE_ETC": str(etc),
                "FAKE_IPT_LOG": str(log),
            }
            return subprocess.run(
                ["bash", str(FW_NFQ), action],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )

        return SimpleNamespace(
            etc=etc, log=log, write_conf=write_conf, run=run
        )

    @staticmethod
    def calls(sandbox) -> list[str]:
        if not sandbox.log.exists():
            return []
        return sandbox.log.read_text(encoding="utf-8").splitlines()

    def test_start_adds_mark_and_deletes_legacy(self, sandbox):
        res = sandbox.run("start")
        assert res.returncode == 0, res.stderr

        calls = self.calls(sandbox)
        inserted = [c for c in calls if "-I POSTROUTING" in c]
        assert len(inserted) == 1, calls
        assert "-m mark ! --mark 0x80000/0x80000" in inserted[0], inserted[0]

        legacy = [c for c in calls if "-D POSTROUTING" in c and "-j NFQUEUE" in c]
        assert legacy, calls
        assert all("--queue-bypass" in c and "--mark" not in c for c in legacy), legacy

    def test_mark_from_extra_for_old_install(self, sandbox):
        sandbox.write_conf('EXTRA="--dpi-desync-fwmark=0x90000"')
        res = sandbox.run("start")
        assert res.returncode == 0, res.stderr
        inserted = [c for c in self.calls(sandbox) if "-I POSTROUTING" in c]
        assert len(inserted) == 1, self.calls(sandbox)
        assert "--mark 0x90000/0x90000" in inserted[0], inserted[0]

    def test_nfqws_mark_beats_extra(self, sandbox):
        sandbox.write_conf(
            'NFQWS_MARK=0xA0000\nEXTRA="--dpi-desync-fwmark=0x90000"'
        )
        res = sandbox.run("start")
        assert res.returncode == 0, res.stderr
        inserted = [c for c in self.calls(sandbox) if "-I POSTROUTING" in c]
        assert len(inserted) == 1, self.calls(sandbox)
        assert "--mark 0xA0000/0xA0000" in inserted[0], inserted[0]

    def test_invalid_mark_warns_and_falls_back(self, sandbox):
        sandbox.write_conf('NFQWS_MARK="abc"')
        res = sandbox.run("start")
        assert res.returncode == 0, res.stderr
        assert "не похож на число" in res.stderr
        inserted = [c for c in self.calls(sandbox) if "-I POSTROUTING" in c]
        assert len(inserted) == 1, self.calls(sandbox)
        assert "--mark 0x80000/0x80000" in inserted[0], inserted[0]


# --------------------------------------------------------------------------
# динамические проверки в network namespace
# --------------------------------------------------------------------------

def _run(*args):
    return subprocess.run(args, check=False, text=True, capture_output=True)


def ns(*args):
    return _run("ip", "netns", "exec", NS, *args)


def ns_ok(*args):
    res = ns(*args)
    assert res.returncode == 0, f"{' '.join(args)}: {res.stderr.strip()}"
    return res.stdout


def run_fw(etc, action):
    """fw-nfq.sh внутри ns с чистым окружением (конфиг — только из файла)."""
    return _run(
        "ip", "netns", "exec", NS,
        "env", "-i",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"ROUTEWEAVE_ETC={etc}",
        "bash", str(FW_NFQ), action,
    )


def nfq_rules():
    out = ns_ok("iptables", "-w", "5", "-t", "mangle", "-S", "POSTROUTING")
    return [ln for ln in out.splitlines() if "NFQUEUE" in ln]


def _probe_nfqueue():
    """Умеет ли ядро NFQUEUE+connbytes — до запуска скрипта.

    Skip только если точно такое же правило не ставится напрямую через
    iptables: значит, в окружении нет модулей xt_NFQUEUE/xt_connbytes, и это
    не ошибка fw-nfq.sh. Любая другая проблема проявится как падение теста.
    """
    ns_ok("iptables", "-w", "5", "-t", "mangle", "-N", "RW-NFQ-PROBE")
    try:
        res = ns(
            "iptables", "-w", "5", "-t", "mangle", "-A", "RW-NFQ-PROBE",
            "-p", "tcp",
            "-m", "multiport", "--dports", "80,443",
            "-m", "connbytes", "--connbytes", "1:10",
            "--connbytes-mode", "packets", "--connbytes-dir", "original",
            "-m", "mark", "!", "--mark", "0x80000/0x80000",
            "-j", "NFQUEUE", "--queue-num", QNUM, "--queue-bypass",
        )
    finally:
        ns("iptables", "-w", "5", "-t", "mangle", "-F", "RW-NFQ-PROBE")
        ns("iptables", "-w", "5", "-t", "mangle", "-X", "RW-NFQ-PROBE")
    if res.returncode != 0:
        pytest.skip(
            "NFQUEUE/connbytes недоступны в netns (нет модулей ядра): "
            f"{res.stderr.strip()}"
        )


@pytest.mark.privileged
@pytest.mark.skipif(os.geteuid() != 0, reason="нужен root: netns и iptables")
@pytest.mark.skipif(shutil.which("ip") is None, reason="нет ip из iproute2")
@pytest.mark.skipif(shutil.which("iptables") is None, reason="нет iptables")
class TestNetns:
    @pytest.fixture(scope="class")
    def started(self, tmp_path_factory):
        work = tmp_path_factory.mktemp("fw-nfq")
        etc = work / "etc"
        etc.mkdir()
        hosts = work / "dpi.txt"
        hosts.write_text(
            "# комментарий\n"
            "+.example.com\n"
            ".example.org\n"
            "\n"
            "  plain.net  \n",
            encoding="utf-8",
        )
        generated = work / "run" / "dpi-zapret.txt"
        (etc / "zapret.conf").write_text(
            "WAN_IFACE=dummy0\n"
            f"QNUM={QNUM}\n"
            f"SRC_HOSTLIST={hosts}\n"
            f"HOSTLIST={generated}\n",
            encoding="utf-8",
        )

        _run("ip", "netns", "del", NS)  # мусор от упавшего прогона
        res = _run("ip", "netns", "add", NS)
        if res.returncode != 0:
            pytest.skip(f"ip netns add недоступен: {res.stderr.strip()}")
        try:
            ns_ok("ip", "link", "set", "lo", "up")
            ns_ok("ip", "link", "add", "dummy0", "type", "dummy")
            ns_ok("ip", "link", "set", "dummy0", "up")
            if ns("iptables", "-t", "mangle", "-S").returncode != 0:
                pytest.skip("iptables внутри netns не работает")
            _probe_nfqueue()

            start = run_fw(etc, "start")
            assert start.returncode == 0, start.stderr
            yield SimpleNamespace(work=work, etc=etc, hosts=hosts, generated=generated)
        finally:
            _run("ip", "netns", "del", NS)

    def test_start_creates_rule(self, started):
        rules = nfq_rules()
        assert len(rules) == 1, rules
        rule = rules[0]
        assert "-o dummy0" in rule, rule
        assert "80,443" in rule, rule
        assert "-m mark ! --mark 0x80000/0x80000" in rule, rule
        assert "NFQUEUE" in rule and QNUM in rule, rule

    def test_hostlist_is_generated(self, started):
        assert started.generated.is_file(), "hostlist не создан"
        assert started.generated.read_text(encoding="utf-8").splitlines() == [
            "example.com",
            "example.org",
            "plain.net",
        ]

    def test_second_start_does_not_duplicate(self, started):
        before = len(nfq_rules())
        res = run_fw(started.etc, "start")
        assert res.returncode == 0, res.stderr
        assert len(nfq_rules()) == before == 1, nfq_rules()

    def test_start_removes_legacy_rule_without_mark(self, started):
        """Правило прежних версий не должно остаться рядом с новым."""
        legacy = [
            "-p", "tcp", "-m", "multiport", "--dports", "80,443",
            "-m", "connbytes", "--connbytes", "1:10",
            "--connbytes-mode", "packets", "--connbytes-dir", "original",
            "-j", "NFQUEUE", "--queue-num", QNUM, "--queue-bypass",
        ]
        ns_ok(
            "iptables", "-w", "5", "-t", "mangle", "-I", "POSTROUTING", "1",
            "-o", "dummy0", *legacy,
        )
        assert len(nfq_rules()) == 2, nfq_rules()

        res = run_fw(started.etc, "start")
        assert res.returncode == 0, res.stderr

        rules = nfq_rules()
        assert len(rules) == 1, rules
        assert "-m mark ! --mark 0x80000/0x80000" in rules[0], rules[0]

    def test_mark_from_extra(self, started):
        """Старая установка без NFQWS_MARK: метка берётся из EXTRA."""
        conf = started.etc / "zapret.conf"
        original = conf.read_text(encoding="utf-8")
        assert run_fw(started.etc, "stop").returncode == 0
        conf.write_text(
            "WAN_IFACE=dummy0\n"
            f"QNUM={QNUM}\n"
            f"SRC_HOSTLIST={started.hosts}\n"
            f"HOSTLIST={started.generated}\n"
            'EXTRA="--dpi-desync-fwmark=0x90000"\n',
            encoding="utf-8",
        )
        try:
            res = run_fw(started.etc, "start")
            assert res.returncode == 0, res.stderr
            rules = nfq_rules()
            assert len(rules) == 1, rules
            assert "-m mark ! --mark 0x90000/0x90000" in rules[0], rules[0]
        finally:
            assert run_fw(started.etc, "stop").returncode == 0
            conf.write_text(original, encoding="utf-8")
            assert run_fw(started.etc, "start").returncode == 0

    def test_stop_removes_rule(self, started):
        assert run_fw(started.etc, "start").returncode == 0

        res = run_fw(started.etc, "stop")
        assert res.returncode == 0, res.stderr
        assert nfq_rules() == [], nfq_rules()

        # повторный stop не падает на отсутствующем правиле
        again = run_fw(started.etc, "stop")
        assert again.returncode == 0, again.stderr

    def test_start_without_wan_iface_fails(self, started, tmp_path):
        """Пустой WAN_IFACE — ошибка с понятным сообщением, а не «тихий успех»."""
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "zapret.conf").write_text(
            f"WAN_IFACE=\nQNUM={QNUM}\n"
            f"HOSTLIST={tmp_path / 'dpi-zapret.txt'}\n",
            encoding="utf-8",
        )
        # пустой WAN_IFACE заменяется автодетектом по маршруту; в netns без
        # маршрута он остаётся пустым
        res = run_fw(etc, "start")
        assert res.returncode != 0
        assert "WAN_IFACE" in res.stderr, res.stderr
