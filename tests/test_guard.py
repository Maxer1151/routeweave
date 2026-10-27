"""Тесты templates/routeweave-guard.sh и его systemd-юнитов.

Сторож проверяет инварианты маршрутизации и чинит пропавшее. Тесты не требуют
root, сети и живой системы: все внешние команды (sysctl, nft, ip, iptables,
systemctl) подменяются фейками в <tmp>/bin, состояние «что сломано» лежит в
файлах, а вызовы пишутся в calls.log. timeout не подменяется — он настоящий.
"""

import re
import shutil
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "templates" / "routeweave-guard.sh"
SERVICE = REPO / "templates" / "routeweave-guard.service"
TIMER = REPO / "templates" / "routeweave-guard.timer"
BASH = shutil.which("bash")

QNUM = "200"

# Диагностика systemd-analyze в формате «/путь/к/юниту:строка: текст».
_UNIT_DIAGNOSTIC = re.compile(
    r"^(?P<path>[^:\s]+\.(?:service|socket|slice|timer|target|mount|automount"
    r"|path|swap|device|scope)):\d+:"
)

FAKES = {
    "sysctl": """
        #!/bin/sh
        printf 'sysctl %s\\n' "$*" >> "$FAKE_CALLS"
        case "$1" in
          -n) cat "$FAKE_STATE/ip_forward" ;;
          -w)
            if [ "$(cat "$FAKE_STATE/fix_ip_forward")" = 1 ]; then
              echo 1 > "$FAKE_STATE/ip_forward"
            fi
            printf '%s\\n' "$2"
            ;;
          *) exit 1 ;;
        esac
    """,
    "nft": """
        #!/bin/sh
        printf 'nft %s\\n' "$*" >> "$FAKE_CALLS"
        case "$*" in
          'list tables')
            if [ "$(cat "$FAKE_STATE/mihomo_table")" = 1 ]; then
              echo 'table inet mihomo'
            fi
            ;;
          'list table inet mihomo')
            if [ "$(cat "$FAKE_STATE/mihomo_table")" != 1 ]; then
              echo 'Error: No such file or directory' >&2
              exit 1
            fi
            echo 'table inet mihomo {'
            echo '	chain prerouting {'
            if [ "$(cat "$FAKE_STATE/mihomo_redirect")" = 1 ]; then
              echo '		redirect to :53'
            fi
            echo '	}'
            echo '}'
            ;;
          *) exit 1 ;;
        esac
    """,
    "ip": """
        #!/bin/sh
        printf 'ip %s\\n' "$*" >> "$FAKE_CALLS"
        case "$*" in
          '-4 route show table 2022')
            if [ "$(cat "$FAKE_STATE/route2022")" = 1 ]; then
              echo 'default via 198.18.0.2 dev Meta'
            fi
            ;;
          '-4 rule')
            if [ "$(cat "$FAKE_STATE/rule8993")" = 1 ]; then
              printf '8993:\tfrom all fwmark 0x200000/0x200000 lookup %s\n' \\
                "$(cat "$FAKE_STATE/rule8993_lookup")"
            fi
            echo '9000:	from all lookup 2022'
            ;;
          *) exit 1 ;;
        esac
    """,
    "iptables": """
        #!/bin/sh
        printf 'iptables %s\\n' "$*" >> "$FAKE_CALLS"
        case "$*" in
          '-w 5 -t mangle -S POSTROUTING')
            if [ "$(cat "$FAKE_STATE/nfq_rule")" = 1 ]; then
              echo '-A POSTROUTING -o eth0 -p tcp -m multiport' \\
                   '--dports 80,443 -j NFQUEUE --queue-num 200 --queue-bypass'
            fi
            ;;
          *) exit 1 ;;
        esac
    """,
    "systemctl": """
        #!/bin/sh
        printf 'systemctl %s\\n' "$*" >> "$FAKE_CALLS"
        case "$1" in
          is-active)
            state="$(cat "$FAKE_STATE/nfqws_active")"
            printf '%s\\n' "$state"
            [ "$state" = active ] && exit 0
            exit 3
            ;;
          restart)
            case "$2" in
              mihomo)
                if [ "$(cat "$FAKE_STATE/fix_mihomo")" = 1 ]; then
                  echo 1 > "$FAKE_STATE/mihomo_table"
                  echo 1 > "$FAKE_STATE/mihomo_redirect"
                  echo 1 > "$FAKE_STATE/route2022"
                  echo 1 > "$FAKE_STATE/rule8993"
                fi
                ;;
              zapret-nfqws)
                if [ "$(cat "$FAKE_STATE/fix_nfqws")" = 1 ]; then
                  printf '  200  4242 00000000 00000000\\n' > "$FAKE_QUEUE"
                  printf 'active\\n' > "$FAKE_STATE/nfqws_active"
                  if [ "$(cat "$FAKE_STATE/fix_probe")" = 1 ]; then
                    printf '200' > "$FAKE_STATE/curl_plain"
                    printf '200' > "$FAKE_STATE/curl_dpi"
                  fi
                fi
                ;;
            esac
            ;;
          *) exit 1 ;;
        esac
    """,
    "ping": """
        #!/bin/sh
        printf 'ping %s\\n' "$*" >> "$FAKE_CALLS"
        [ "$(cat "$FAKE_STATE/ping_ok")" = 1 ] && exit 0
        exit 1
    """,
    "curl": """
        #!/bin/sh
        printf 'curl %s\\n' "$*" >> "$FAKE_CALLS"
        url=""
        for arg in "$@"; do url="$arg"; done
        case "$url" in
          *discord*) state="$FAKE_STATE/curl_dpi" ;;
          *) state="$FAKE_STATE/curl_plain" ;;
        esac
        code="$(cat "$state")"
        [ "$code" = timeout ] && exit 28
        printf '%s' "$code"
    """,
}

FW_NFQ = """
    #!/bin/sh
    printf 'fw-nfq %s\\n' "$*" >> "$FAKE_CALLS"
    if [ "$1" = start ] && [ "$(cat "$FAKE_STATE/fix_nfq")" = 1 ]; then
      echo 1 > "$FAKE_STATE/nfq_rule"
    fi
    exit 0
"""

FW_WAN = """
    #!/bin/sh
    printf 'fw-wan %s\\n' "$*" >> "$FAKE_CALLS"
    case "$1" in
      status) printf 'WAN_INGRESS: %s\\n' "$(cat "$FAKE_STATE/wan_ingress")" ;;
      start)
        if [ "$(cat "$FAKE_STATE/fix_wan")" = 1 ]; then
          echo 1 > "$FAKE_STATE/rule8993"
        fi
        ;;
      *) exit 1 ;;
    esac
"""

DEFAULTS = {
    "ip_forward": "1",
    "fix_ip_forward": "1",
    "mihomo_table": "1",
    "mihomo_redirect": "1",
    "route2022": "1",
    "nfq_rule": "1",
    "nfqws_active": "active",
    "wan_ingress": "no",
    "rule8993": "0",
    "rule8993_lookup": "main",
    "fix_mihomo": "1",
    "fix_nfq": "1",
    "fix_nfqws": "1",
    "fix_wan": "1",
    "fix_probe": "1",
    "ping_ok": "1",
    "curl_plain": "200",
    "curl_dpi": "200",
}


class Sandbox:
    """Временное окружение сторожа с фейковыми командами."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        for name, value in DEFAULTS.items():
            (self.state / name).write_text(value + "\n", encoding="utf-8")
        self.calls = root / "calls.log"
        self.etc = root / "etc"
        self.etc.mkdir()
        (self.etc / "zapret.conf").write_text(f"QNUM={QNUM}\n", encoding="utf-8")
        self.queue = root / "nfq_queue"
        self.queue.write_text("  200  4242 00000000 00000000\n", encoding="utf-8")
        self.guard_dir = root / "guard"
        self.fw_nfq = root / "fw-nfq.sh"
        self.fw_wan = root / "fw-wan.sh"
        self._fake(self.fw_nfq, FW_NFQ)
        self._fake(self.fw_wan, FW_WAN)
        for name, body in FAKES.items():
            self._fake(self.bin / name, body)

    @staticmethod
    def _fake(path: Path, body: str):
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def set(self, name: str, value: str):
        (self.state / name).write_text(value + "\n", encoding="utf-8")

    def run(self, mode: str | None = None, **env_extra: str):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "LC_ALL": "C",
            "ROUTEWEAVE_ETC": str(self.etc),
            "GUARD_STATE_DIR": str(self.guard_dir),
            "NFQ_SCRIPT": str(self.fw_nfq),
            "WAN_SCRIPT": str(self.fw_wan),
            "NFQ_QUEUE_FILE": str(self.queue),
            "GUARD_WAIT_TRIES": "3",
            "GUARD_WAIT_SLEEP": "0",
            "FAKE_STATE": str(self.state),
            "FAKE_CALLS": str(self.calls),
            "FAKE_QUEUE": str(self.queue),
        }
        env.update(env_extra)
        args = [BASH, str(SCRIPT)] if mode is None else [BASH, str(SCRIPT), mode]
        return subprocess.run(
            args, check=False, env=env, capture_output=True, text=True, timeout=120
        )

    def call_lines(self) -> list[str]:
        if not self.calls.exists():
            return []
        return self.calls.read_text(encoding="utf-8").splitlines()

    def mutating_calls(self) -> list[str]:
        return [
            line
            for line in self.call_lines()
            if " restart " in line
            or line.startswith(("sysctl -w", "fw-nfq start", "fw-wan start"))
        ]


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


pytestmark = pytest.mark.skipif(BASH is None, reason="нет bash")


# --------------------------------------------------------------------------
# статические проверки скрипта
# --------------------------------------------------------------------------

def test_script_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "скрипт должен быть исполняемым"


def test_bash_syntax_ok():
    res = subprocess.run(
        [BASH, "-n", str(SCRIPT)], check=False, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


def test_no_duplicate_directives_in_units():
    for unit in (SERVICE, TIMER):
        names = directive_names(unit.read_text(encoding="utf-8"))
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, f"{unit.name}: директивы заданы дважды: {duplicates}"


def directive_names(text: str) -> list[str]:
    """Имена директив вне комментариев и заголовков секций."""
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        if "=" in stripped:
            names.append(stripped.split("=", 1)[0])
    return names


# --------------------------------------------------------------------------
# check: только чтение
# --------------------------------------------------------------------------

def test_check_healthy(sandbox: Sandbox):
    res = sandbox.run("check")

    assert res.returncode == 0, res.stderr
    assert "ok: ip_forward=1" in res.stdout
    assert "ok: инварианты маршрутизации в порядке" in res.stdout
    assert not sandbox.guard_dir.exists(), "check не должен создавать состояние"
    assert sandbox.mutating_calls() == []


def test_check_reports_problem_without_fixing(sandbox: Sandbox):
    sandbox.set("ip_forward", "0")

    res = sandbox.run("check")

    assert res.returncode == 1
    assert "warn: net.ipv4.ip_forward=0" in res.stderr
    assert "error: проблем: 1" in res.stderr
    assert not sandbox.guard_dir.exists()
    assert sandbox.mutating_calls() == []


def test_check_skips_nfqws_when_inactive(sandbox: Sandbox):
    sandbox.set("nfqws_active", "inactive")

    res = sandbox.run("check")

    assert res.returncode == 0, res.stderr
    assert "пропуск: zapret-nfqws.service не active" in res.stdout


def test_check_reports_failed_nfqws(sandbox: Sandbox):
    # failed — упавший юнит, а не выключенный руками: очередь не читается,
    # DPI-обход молча не работает, поэтому это поломка, а не пропуск.
    sandbox.set("nfqws_active", "failed")

    res = sandbox.run("check")

    assert res.returncode == 1
    assert "zapret-nfqws.service в состоянии failed" in res.stderr
    assert sandbox.mutating_calls() == []


def test_repair_restarts_failed_nfqws(sandbox: Sandbox):
    sandbox.set("nfqws_active", "failed")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert "systemctl restart zapret-nfqws" in sandbox.mutating_calls()
    assert "guard: zapret-nfqws.service в состоянии failed" in res.stdout


def test_tool_rc127_is_problem_not_crash(sandbox: Sandbox):
    # Фейк, который ведёт себя как отсутствующая команда: timeout отдаёт 127.
    (sandbox.bin / "nft").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    (sandbox.bin / "nft").chmod(0o755)

    checked = sandbox.run("check")
    repaired = sandbox.run("repair")

    assert checked.returncode == 1
    assert "nft list tables: команда недоступна (rc=127)" in checked.stderr
    assert repaired.returncode == 1
    # без nft рестарт mihomo вслепую не делаем
    assert "systemctl restart mihomo" not in sandbox.mutating_calls()


def test_unknown_argument_is_usage_error(sandbox: Sandbox):
    res = sandbox.run("frobnicate")

    assert res.returncode == 2
    assert "usage:" in res.stderr


# --------------------------------------------------------------------------
# repair: каждое действие по своему инварианту
# --------------------------------------------------------------------------

def test_repair_starts_fw_nfq_when_rule_missing(sandbox: Sandbox):
    sandbox.set("nfq_rule", "0")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    guard_lines = [ln for ln in res.stdout.splitlines() if ln.startswith("guard: ")]
    expected = (
        f"guard: нет правила NFQUEUE в mangle POSTROUTING (queue {QNUM})"
        f" → {sandbox.fw_nfq} start"
    )
    assert guard_lines == [expected]
    assert "fw-nfq start" in sandbox.mutating_calls()


def test_repair_restarts_nfqws_when_queue_not_bound(sandbox: Sandbox):
    sandbox.queue.write_text("  200  0 00000000 00000000\n", encoding="utf-8")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert "systemctl restart zapret-nfqws" in sandbox.mutating_calls()
    assert "guard: nfqws active, но очередь 200 не привязана" in res.stdout


def test_repair_enables_ip_forward(sandbox: Sandbox):
    sandbox.set("ip_forward", "0")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert "sysctl -w net.ipv4.ip_forward=1" in sandbox.mutating_calls()
    assert not sandbox.guard_dir.joinpath("mihomo.last").exists()


def test_repair_restarts_mihomo_when_table_missing(sandbox: Sandbox):
    sandbox.set("mihomo_table", "0")
    sandbox.set("route2022", "0")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert "systemctl restart mihomo" in sandbox.mutating_calls()
    assert (
        "guard: нет nft-таблицы 'table inet mihomo' → systemctl restart mihomo"
        in res.stdout
    )


def test_repair_starts_fw_wan_when_8993_missing(sandbox: Sandbox):
    sandbox.set("wan_ingress", "yes")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert "fw-wan start" in sandbox.mutating_calls()
    assert "guard: WAN_INGRESS=yes, но нет ip rule 8993 с lookup main" in res.stdout


def test_repair_does_nothing_when_healthy(sandbox: Sandbox):
    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert sandbox.mutating_calls() == []
    assert "guard: " not in res.stdout


# --------------------------------------------------------------------------
# защита от шторма
# --------------------------------------------------------------------------

def test_cooldown_blocks_repeated_action(sandbox: Sandbox):
    # sysctl -w «не помогает»: состояние остаётся сломанным.
    sandbox.set("ip_forward", "0")
    sandbox.set("fix_ip_forward", "0")

    first = sandbox.run("repair")
    second = sandbox.run("repair")

    assert first.returncode == 1
    assert second.returncode == 1
    assert sandbox.call_lines().count("sysctl -w net.ipv4.ip_forward=1") == 1
    assert "кулдаун" in second.stderr
    assert not sandbox.guard_dir.joinpath("nfq.last").exists()


def test_fuse_stops_all_actions(sandbox: Sandbox):
    sandbox.set("ip_forward", "0")
    sandbox.guard_dir.mkdir()
    # Четыре свежих записи — предохранитель уже исчерпан.
    now = int(time.time())
    actions = sandbox.guard_dir / "actions"
    actions.write_text("\n".join([str(now)] * 4) + "\n", encoding="utf-8")

    res = sandbox.run("repair")

    assert res.returncode == 1
    assert sandbox.mutating_calls() == []
    assert "предохранитель" in res.stderr


# --------------------------------------------------------------------------
# юниты
# --------------------------------------------------------------------------

def test_service_unit_contract():
    text = SERVICE.read_text(encoding="utf-8")

    assert "ExecStart=/usr/local/sbin/routeweave-guard.sh repair" in text
    assert "Type=oneshot" in text
    assert "ProtectSystem=strict" in text
    assert "NoNewPrivileges=yes" in text
    assert "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW" in text
    assert "ReadWritePaths=/run/routeweave " in text
    assert "RuntimeDirectoryPreserve=yes" in text
    # Необязательный файл настроек: пробы можно выключить, не правя юнит.
    assert "EnvironmentFile=-/etc/routeweave/guard.env" in text
    # Пробы ходят в сеть и ждут восстановления: 90-секундного дефолта мало.
    assert "TimeoutStartSec=300" in text


def test_timer_unit_contract():
    text = TIMER.read_text(encoding="utf-8")

    assert "OnActiveSec=60" in text
    assert "OnUnitActiveSec=60" in text
    assert "Persistent=no" in text
    assert "WantedBy=timers.target" in text


def is_foreign_unit_diagnostic(line: str, unit: str) -> bool:
    match = _UNIT_DIAGNOSTIC.match(line.strip())
    return bool(match) and Path(match.group("path")).name != unit


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze недоступен"
)
@pytest.mark.parametrize("unit", [SERVICE, TIMER], ids=["service", "timer"])
def test_systemd_analyze_verify(unit: Path):
    proc = subprocess.run(
        ["systemd-analyze", "verify", str(unit)],
        check=False,
        text=True,
        capture_output=True,
    )
    problems = [
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if line.strip()
        and "is not executable" not in line
        and not is_foreign_unit_diagnostic(line, unit.name)
    ]
    assert not problems, problems


# --------------------------------------------------------------------------
# функциональные пробы: структура цела, а трафик через очередь — нет
# --------------------------------------------------------------------------

def probe_calls(sandbox: Sandbox) -> list[str]:
    return [
        line
        for line in sandbox.call_lines()
        if line.startswith(("ping ", "curl "))
    ]


def test_check_probes_ok(sandbox: Sandbox):
    res = sandbox.run("check")

    assert res.returncode == 0, res.stderr
    assert "трафик через очередь и десинк проходят" in res.stdout
    calls = probe_calls(sandbox)
    assert sum(1 for c in calls if c.startswith("ping ")) == 1
    assert sum(1 for c in calls if c.startswith("curl ")) == 2
    assert sandbox.mutating_calls() == []


def test_check_probe_plain_fail_with_ping_is_problem(sandbox: Sandbox):
    sandbox.set("curl_plain", "timeout")

    res = sandbox.run("check")

    assert res.returncode == 1
    assert "TCP 80/443 через очередь не открывается" in res.stderr
    assert sandbox.mutating_calls() == []
    # check ничего не пишет в состояние.
    assert not sandbox.guard_dir.exists()


def test_check_probe_external_down_is_not_problem(sandbox: Sandbox):
    sandbox.set("curl_plain", "timeout")
    sandbox.set("ping_ok", "0")

    res = sandbox.run("check")

    assert res.returncode == 0, res.stderr
    assert "внешняя сеть недоступна" in res.stderr
    assert "проблем:" not in res.stderr
    assert sandbox.mutating_calls() == []
    assert not sandbox.guard_dir.exists()


def test_repair_probe_plain_fail_restarts_nfqws(sandbox: Sandbox):
    sandbox.set("curl_plain", "timeout")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert "systemctl restart zapret-nfqws" in sandbox.mutating_calls()
    assert "guard: TCP 80/443 через очередь не открывается" in res.stdout
    assert (sandbox.guard_dir / "probe-plain.last").is_file()


def test_repair_probe_external_down_does_nothing(sandbox: Sandbox):
    sandbox.set("curl_plain", "timeout")
    sandbox.set("ping_ok", "0")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert sandbox.mutating_calls() == []
    assert "внешняя сеть недоступна" in res.stderr


def test_repair_probe_dpi_fail_once_does_not_restart(sandbox: Sandbox):
    sandbox.set("curl_dpi", "timeout")

    res = sandbox.run("repair")

    assert "systemctl restart zapret-nfqws" not in sandbox.mutating_calls()
    assert (sandbox.guard_dir / "probe-dpi.fail").read_text().strip() == "1"
    # Десинк действительно не работает — это проблема, хотя действия ещё нет.
    assert res.returncode == 1
    assert "десинк не работает" in res.stderr


def test_repair_probe_dpi_fail_twice_restarts(sandbox: Sandbox):
    sandbox.set("curl_dpi", "timeout")

    sandbox.run("repair")
    second = sandbox.run("repair")

    assert "systemctl restart zapret-nfqws" in sandbox.mutating_calls()
    assert second.returncode == 0, second.stderr
    assert "десинк работает" in second.stdout
    assert (sandbox.guard_dir / "probe-dpi.last").is_file()
    assert not (sandbox.guard_dir / "probe-dpi.fail").exists()


def test_repair_probe_success_resets_dpi_counter(sandbox: Sandbox):
    sandbox.guard_dir.mkdir(parents=True)
    (sandbox.guard_dir / "probe-dpi.fail").write_text("3\n", encoding="utf-8")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert not (sandbox.guard_dir / "probe-dpi.fail").exists()


def test_check_dpi_fail_writes_no_state(sandbox: Sandbox):
    sandbox.set("curl_dpi", "timeout")

    res = sandbox.run("check")

    assert res.returncode == 1
    assert "десинк не работает" in res.stderr
    assert not sandbox.guard_dir.exists()


def test_probes_disabled_by_env(sandbox: Sandbox):
    res = sandbox.run("check", GUARD_PROBE="no")

    assert res.returncode == 0, res.stderr
    assert "пробы трафика выключены" in res.stdout
    assert probe_calls(sandbox) == []


def test_probes_skipped_when_nfqws_inactive(sandbox: Sandbox):
    sandbox.set("nfqws_active", "inactive")

    res = sandbox.run("check")

    assert res.returncode == 0, res.stderr
    assert "пропуск: zapret-nfqws.service не active" in res.stdout
    assert probe_calls(sandbox) == []


def test_probes_skipped_when_structure_broken(sandbox: Sandbox):
    sandbox.set("ip_forward", "0")

    res = sandbox.run("check")

    assert res.returncode == 1
    assert "пропуск: сначала структурные проверки" in res.stdout
    assert probe_calls(sandbox) == []


def test_non_https_probe_url_falls_back_to_default(sandbox: Sandbox):
    res = sandbox.run("check", GUARD_PROBE_PLAIN_URL="-XPOST")

    assert res.returncode == 0, res.stderr
    assert "не https-URL" in res.stderr
    curls = [c for c in probe_calls(sandbox) if c.startswith("curl ")]
    assert any("https://ya.ru/" in c for c in curls)
    assert not any("-XPOST" in c for c in curls)


def test_bad_ping_host_falls_back_to_default(sandbox: Sandbox):
    res = sandbox.run("check", GUARD_PROBE_PING_HOST="-f")

    assert res.returncode == 0, res.stderr
    assert "не похож на хост" in res.stderr
    pings = [c for c in probe_calls(sandbox) if c.startswith("ping ")]
    assert pings and "1.1.1.1" in pings[0]


# --------------------------------------------------------------------------
# пауза после неэффективного рестарта
# --------------------------------------------------------------------------

def restart_calls(sandbox: Sandbox) -> list[str]:
    return [
        line
        for line in sandbox.mutating_calls()
        if line == "systemctl restart zapret-nfqws"
    ]


def test_plain_stuck_blocks_repeat_restart(sandbox: Sandbox):
    # Рестарт не лечит: причина не в nfqws. Второй раз рвать очередь незачем.
    sandbox.set("curl_plain", "timeout")
    sandbox.set("fix_probe", "0")

    first = sandbox.run("repair")

    assert first.returncode == 1
    assert len(restart_calls(sandbox)) == 1
    marker = sandbox.guard_dir / "probe-plain.stuck"
    assert marker.is_file()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    second = sandbox.run("repair")

    assert second.returncode == 1
    assert len(restart_calls(sandbox)) == 1, "повторного рестарта быть не должно"
    assert "пауза" in second.stderr
    assert "probe-plain.stuck" in second.stderr


def test_stale_plain_stuck_marker_allows_restart(sandbox: Sandbox):
    sandbox.set("curl_plain", "timeout")
    sandbox.set("fix_probe", "0")
    sandbox.guard_dir.mkdir(parents=True)
    old = int(time.time()) - 400
    (sandbox.guard_dir / "probe-plain.stuck").write_text(f"{old}\n", encoding="utf-8")

    res = sandbox.run("repair", GUARD_STUCK_TTL="300")

    assert len(restart_calls(sandbox)) == 1
    assert res.returncode == 1
    marker = sandbox.guard_dir / "probe-plain.stuck"
    assert int(marker.read_text(encoding="utf-8").strip()) > old


def test_dpi_stuck_blocks_repeat_restart(sandbox: Sandbox):
    sandbox.set("curl_dpi", "timeout")
    sandbox.set("fix_probe", "0")

    sandbox.run("repair")  # первая неудача — только счётчик
    second = sandbox.run("repair")  # вторая — рестарт, он не помогает

    assert second.returncode == 1
    assert len(restart_calls(sandbox)) == 1
    assert (sandbox.guard_dir / "probe-dpi.stuck").is_file()

    third = sandbox.run("repair")

    assert third.returncode == 1
    assert len(restart_calls(sandbox)) == 1, "пока маркер свежий, рестартов нет"
    assert "пауза" in third.stderr


def test_healthy_probes_clear_stuck_markers(sandbox: Sandbox):
    sandbox.guard_dir.mkdir(parents=True)
    now = int(time.time())
    for key in ("probe-plain", "probe-dpi"):
        (sandbox.guard_dir / f"{key}.stuck").write_text(f"{now}\n", encoding="utf-8")

    res = sandbox.run("repair")

    assert res.returncode == 0, res.stderr
    assert not (sandbox.guard_dir / "probe-plain.stuck").exists()
    assert not (sandbox.guard_dir / "probe-dpi.stuck").exists()


def test_bad_stuck_ttl_warns_and_keeps_default(sandbox: Sandbox):
    sandbox.set("curl_plain", "timeout")
    sandbox.set("fix_probe", "0")
    sandbox.guard_dir.mkdir(parents=True)
    (sandbox.guard_dir / "probe-plain.stuck").write_text(
        f"{int(time.time())}\n", encoding="utf-8"
    )

    res = sandbox.run("repair", GUARD_STUCK_TTL="abc")

    assert "GUARD_STUCK_TTL='abc'" in res.stderr
    assert "пауза" in res.stderr
    assert restart_calls(sandbox) == []


# --------------------------------------------------------------------------
# ip rule 8993: приоритет и таблица, а не только номер
# --------------------------------------------------------------------------

def test_wan_rule_with_other_table_is_problem(sandbox: Sandbox):
    sandbox.set("wan_ingress", "yes")
    sandbox.set("rule8993", "1")
    sandbox.set("rule8993_lookup", "2022")

    res = sandbox.run("check")

    assert res.returncode == 1
    assert "нет ip rule 8993 с lookup main" in res.stderr
