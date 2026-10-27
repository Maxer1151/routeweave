"""Тесты диагностического снимка Routeweave: helper-скрипт и API панели.

Скрипт проверяется с заглушками утилит в PATH: без root, сети и обращений
к живой системе. Панель загружается отдельным модулем с ROUTEWEAVE_ETC и
подменённым run_cmd — как в tests/test_panel.py, но без импорта того файла.
"""

import importlib.util
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

# Тестовый клиент Starlette использует httpx и предупреждает об этом.
warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated",
)

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "templates" / "routeweave-diag-dump.sh"
APP_PATH = ROOT / "panel" / "app.py"
AUTH = ("admin", "secret")

BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/tmp",
}

FAKE_TOOLS = {
    "ip": """#!/bin/sh
echo "2: eth0    inet 192.0.2.10/24 scope global eth0"
echo "1: lo      inet 127.0.0.1/8 scope host lo"
""",
    "nft": """#!/bin/sh
case "$*" in
  "list tables") printf 'table inet mihomo\\ntable ip routeweave\\n' ;;
  "list table inet mihomo") printf 'table inet mihomo {\\n\\tchain outbound {\\n\\t}\\n}\\n' ;;
  *) exit 1 ;;
esac
""",
    "iptables": """#!/bin/sh
printf '%s\\n' "-A POSTROUTING -j ROUTEWEAVE-INGRESS" "-A PREROUTING -j ROUTEWEAVE-DNS"
""",
    "ss": """#!/bin/sh
printf 'Netid State  Local Address:Port\\nudp   UNCONN 0.0.0.0:53\\ntcp   LISTEN 0.0.0.0:7890\\n'
""",
    "sysctl": """#!/bin/sh
for key in "$@"; do
  case "$key" in
    *.ip_forward) echo "net.ipv4.ip_forward = 1" ;;
    *.nf_conntrack_count) echo "net.netfilter.nf_conntrack_count = 7" ;;
    *.nf_conntrack_max) echo "net.netfilter.nf_conntrack_max = 65536" ;;
    *) echo "$key = unknown" ;;
  esac
done
""",
    "systemctl": """#!/bin/sh
case "$1" in
  is-active) echo active ;;
  show) printf 'NRestarts=0\\nActiveEnterTimestamp=Tue 2026-01-01 00:00:00 UTC\\n' ;;
  *) exit 1 ;;
esac
""",
    "conntrack": """#!/bin/sh
echo "cpu=0 found=7 invalid=0 ignore=0 insert=0 insert_failed=0"
""",
}

# Заглушка «команды нет»: реальные /usr/bin/nft и /usr/bin/conntrack не должны
# влиять на тест отсутствия утилит.
BLOCKER = "#!/bin/sh\nexit 127\n"

JOURNAL_FEW = """#!/bin/sh
i=0
while [ "$i" -lt 20 ]; do
  echo "2026-01-01T00:00:00+0000 host mihomo[$i]: fake journal line"
  i=$((i + 1))
done
"""

JOURNAL_MANY = """#!/bin/sh
i=0
while [ "$i" -lt 4000 ]; do
  echo "2026-01-01T00:00:00+0000 host mihomo[$i]: fake journal line for truncation"
  i=$((i + 1))
done
"""


def install_tools(tmp_path, *, verbose=False, skip=()):
    """Создаёт bin/ с заглушками; PATH указывает на него первым."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tools = dict(FAKE_TOOLS)
    tools["journalctl"] = JOURNAL_MANY if verbose else JOURNAL_FEW
    for name, text in tools.items():
        path = bin_dir / name
        if name in skip:
            text = BLOCKER
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def run_dump(tmp_path, *, verbose=False, skip=(), env_extra=None):
    bin_dir = install_tools(tmp_path, verbose=verbose, skip=skip)
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    log_dir = tmp_path / "log"
    # Сторож в снимке: по умолчанию — заглушка «команды нет», чтобы тесты не
    # запускали настоящий /usr/local/sbin/routeweave-guard.sh с живой системы.
    guard = bin_dir / "routeweave-guard-absent.sh"
    guard.write_text(BLOCKER, encoding="utf-8")
    guard.chmod(0o755)
    env = dict(BASE_ENV)
    env["PATH"] = str(bin_dir) + ":/usr/bin:/bin"
    env["ROUTEWEAVE_ETC"] = str(etc)
    env["DIAG_LOG_DIR"] = str(log_dir)
    env["ROUTEWEAVE_GUARD"] = str(guard)
    env.update(env_extra or {})
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )
    return proc, log_dir


def dump_file(log_dir: Path) -> Path:
    files = sorted(log_dir.glob("dump-*.txt"))
    assert len(files) == 1, files
    return files[0]


# ---------------- Скрипт ----------------

def test_script_syntax():
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)], check=False, text=True, capture_output=True
    )
    assert proc.returncode == 0, proc.stderr


def test_dump_creates_file_last_dump_and_report(tmp_path):
    proc, log_dir = run_dump(tmp_path)

    assert proc.returncode == 0, proc.stderr
    dump = dump_file(log_dir)
    assert stat.S_IMODE(dump.stat().st_mode) == 0o640

    last = log_dir / "last-dump.txt"
    assert last.is_file()
    assert stat.S_IMODE(last.stat().st_mode) == 0o640
    assert last.read_bytes() == dump.read_bytes()

    lines = proc.stdout.splitlines()
    assert len(lines) == 2, proc.stdout
    assert lines[0] == str(dump)
    assert lines[1].startswith("размер: ")
    assert lines[1].endswith(" байт")


def test_dump_has_all_key_sections(tmp_path):
    proc, log_dir = run_dump(tmp_path)
    assert proc.returncode == 0, proc.stderr
    text = dump_file(log_dir).read_text(encoding="utf-8", errors="replace")

    for token in (
        "ip -br addr",
        "ip -4 rule",
        "table 2022",
        "nft list tables",
        "mangle",
        "nfnetlink_queue",
        "ss -lntu",
        "ip_forward",
        "conntrack",
        "NRestarts",
        "journalctl",
        "Сторож:",
        "journalctl -u routeweave-guard -n 30",
    ):
        assert token in text, token
    assert "===== " in text


def test_dump_includes_guard_check_output(tmp_path):
    guard = tmp_path / "guard.sh"
    guard.write_text(
        "#!/bin/sh\n"
        "echo 'ok: инварианты маршрутизации в порядке'\n"
        "echo 'ok: трафик через очередь и десинк проходят'\n",
        encoding="utf-8",
    )
    guard.chmod(0o755)

    proc, log_dir = run_dump(tmp_path, env_extra={"ROUTEWEAVE_GUARD": str(guard)})

    assert proc.returncode == 0, proc.stderr
    text = dump_file(log_dir).read_text(encoding="utf-8", errors="replace")
    assert "Сторож:" in text
    assert "трафик через очередь и десинк проходят" in text


def test_dump_survives_guard_failure(tmp_path):
    # Сторож недоступен или упал: снимок должен собраться, а не оборваться.
    proc, log_dir = run_dump(tmp_path)

    assert proc.returncode == 0, proc.stderr
    text = dump_file(log_dir).read_text(encoding="utf-8", errors="replace")
    assert "rc=127" in text


def test_missing_nft_and_conntrack_do_not_break_dump(tmp_path):
    proc, log_dir = run_dump(tmp_path, skip=("nft", "conntrack"))

    assert proc.returncode == 0, proc.stderr
    text = dump_file(log_dir).read_text(encoding="utf-8", errors="replace")
    assert "rc=127" in text or "не установлен" in text
    assert "таблицы inet mihomo нет" in text or "nft не установлен" in text


def test_dump_does_not_include_env_or_config(tmp_path):
    marker = "SECRET-MARKER-DIAG"
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / ".env").write_text(f"PANEL_PASS={marker}\n", encoding="utf-8")
    (etc / "config.yaml").write_text(f"secret: {marker}\n", encoding="utf-8")

    proc, log_dir = run_dump(tmp_path)

    assert proc.returncode == 0, proc.stderr
    text = dump_file(log_dir).read_text(encoding="utf-8", errors="replace")
    assert marker not in text
    assert marker not in proc.stdout
    assert marker not in proc.stderr


def test_dump_truncates_to_limit(tmp_path):
    proc, log_dir = run_dump(
        tmp_path, verbose=True, env_extra={"DIAG_MAX_BYTES": "1500"}
    )

    assert proc.returncode == 0, proc.stderr
    dump = dump_file(log_dir)
    assert dump.stat().st_size <= 1500
    assert "снимок обрезан" in proc.stdout
    assert "1500" in proc.stdout
    last = log_dir / "last-dump.txt"
    assert last.read_bytes() == dump.read_bytes()


# ---------------- Панель ----------------

ENV_CONTENT = """\
PANEL_USER=admin
PANEL_PASS=secret
MIHOMO_SECRET=x
SERVER_IP=192.0.2.10
"""

FIXED_ENV = {
    "PANEL_ALLOWED_HOSTS": "testserver",
    "PANEL_RATE_LIMIT": "100000",
    "PANEL_AUTH_RATE_LIMIT": "100000",
}

MUTATING = ("POST", "PUT", "DELETE", "PATCH")
DUMP_NAME = "dump-20260101-120000.txt"


class PanelClient(TestClient):
    """TestClient, который по умолчанию шлёт CSRF-заголовок для мутаций."""

    inject_csrf = True

    def request(self, method, url, **kwargs):
        if self.inject_csrf and method.upper() in MUTATING:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-Requested-With", "Routeweave")
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """Загружает panel/app.py отдельным модулем и подменяет каталог снимков."""
    etc = tmp_path / "etc"
    (etc / "lists").mkdir(parents=True)
    (etc / ".env").write_text(ENV_CONTENT, encoding="utf-8")
    monkeypatch.setenv("ROUTEWEAVE_ETC", str(etc))
    for name, value in FIXED_ENV.items():
        monkeypatch.setenv(name, value)

    spec = importlib.util.spec_from_file_location("rw_panel_diag_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()
    monkeypatch.setattr(module, "DIAG_DIR", diag_dir)
    return SimpleNamespace(module=module, client=PanelClient(module.app), diag_dir=diag_dir)


def write_dump(diag_dir: Path, text: str = "===== test =====\nhello\n") -> Path:
    path = diag_dir / DUMP_NAME
    path.write_text(text, encoding="utf-8")
    (diag_dir / "last-dump.txt").write_text(text, encoding="utf-8")
    return path


def test_dump_post_returns_text_and_size(panel, monkeypatch):
    path = write_dump(panel.diag_dir, "===== test =====\nтекст снимка\n")
    calls = []

    def fake_run_cmd(args, timeout=60):
        calls.append((list(args), timeout))
        return subprocess.CompletedProcess(args, 0, str(path) + "\n", "")

    monkeypatch.setattr(panel.module, "run_cmd", fake_run_cmd)

    response = panel.client.post("/api/diagnostics/dump", auth=AUTH)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    assert data["path"] == str(path)
    assert data["size"] == path.stat().st_size
    assert data["text"] == path.read_text(encoding="utf-8")
    assert calls == [(["sudo", "-n", panel.module.DIAG_DUMP], 60)]


def test_dump_post_accepts_truncation_warning(panel, monkeypatch):
    """Скрипт при обрезке печатает warn: перед путём — это не путь."""
    path = write_dump(panel.diag_dir)

    def fake_run_cmd(args, timeout=60):
        stdout = f"warn: снимок обрезан до 1500 байт\n{path}\nразмер: 42 байт\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(panel.module, "run_cmd", fake_run_cmd)

    response = panel.client.post("/api/diagnostics/dump", auth=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["path"] == str(path)
    assert response.json()["text"] == path.read_text(encoding="utf-8")


def test_dump_post_without_csrf_is_403(panel, monkeypatch):
    path = write_dump(panel.diag_dir)
    calls = []

    def fake_run_cmd(args, timeout=60):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, str(path) + "\n", "")

    monkeypatch.setattr(panel.module, "run_cmd", fake_run_cmd)
    panel.client.inject_csrf = False

    response = panel.client.post("/api/diagnostics/dump", auth=AUTH)

    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]
    assert calls == []


def test_dump_last_returns_text(panel):
    path = write_dump(panel.diag_dir, "===== last =====\nпоследний\n")

    response = panel.client.get("/api/diagnostics/dump/last", auth=AUTH)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    assert data["path"] == str(path)
    assert data["text"] == path.read_text(encoding="utf-8")


def test_dump_last_symlink_is_rejected_without_leak(panel, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET-LEAK\n", encoding="utf-8")
    (panel.diag_dir / "last-dump.txt").symlink_to(outside)

    response = panel.client.get("/api/diagnostics/dump/last", auth=AUTH)

    assert response.status_code in (400, 404)
    assert "SECRET-LEAK" not in response.text
    assert str(outside) not in response.text
    assert str(tmp_path) not in response.text


def test_dump_last_missing_is_404(panel):
    response = panel.client.get("/api/diagnostics/dump/last", auth=AUTH)

    assert response.status_code == 404
    assert response.json()["detail"] == "Снимков ещё нет"


def test_dump_last_non_regular_file_is_400(panel):
    (panel.diag_dir / "last-dump.txt").mkdir()

    response = panel.client.get("/api/diagnostics/dump/last", auth=AUTH)

    assert response.status_code == 400
    assert response.json()["detail"] == "Снимок недоступен"
