"""Профили zapret в install.sh/uninstall.sh и helper применения.

Проверяются инварианты milestone 1:
- установщик ставит шаблон zapret-nfqws@.service и helper
  routeweave-zapret-apply.sh, добавляет helper в sudoers;
- enable_zapret_profiles включает только валидные профили и не падает без
  каталога; restart-вариант перезапускает их после пересборки правил;
- helper применения перезапускает default и профили, ошибки суммирует в rc=1;
- uninstall снимает экземпляры и удаляет шаблон/helper.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
UNINSTALL = REPO / "uninstall.sh"
APPLY = REPO / "templates" / "routeweave-zapret-apply.sh"
PROFILE_SET = REPO / "templates" / "routeweave-zapret-profile-set.sh"
UNIT = REPO / "templates" / "zapret-nfqws@.service"

BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/tmp",
}


def extract_function(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{[^\n]*\n.*?^\}}\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"{path.name}: не найдена функция {name}"
    return match.group(0)


def fake_systemctl(bin_dir: Path) -> Path:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / "systemctl"
    path.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
        'exit "${FAKE_SYSTEMCTL_RC:-0}"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


# --------------------------------------------------------------- статические


def test_install_preflight_knows_profile_files():
    text = INSTALL.read_text(encoding="utf-8")
    section = text[text.index("preflight()"):text.index("# <<< pure-functions <<<")]
    assert "templates/zapret-nfqws@.service" in section
    assert "templates/routeweave-zapret-apply.sh" in section
    assert "templates/routeweave-zapret-profile-set.sh" in section


def test_install_installs_template_unit_and_helper():
    text = INSTALL.read_text(encoding="utf-8")
    assert "ZAPRET_APPLY=/usr/local/sbin/routeweave-zapret-apply.sh" in text
    assert (
        "ZAPRET_PROFILE_SET=/usr/local/sbin/routeweave-zapret-profile-set.sh"
        in text
    )
    assert (
        'install -m 0644 "$APP_DIR/templates/zapret-nfqws@.service"'
        in text
    )
    assert (
        'install -m 0755 "$APP_DIR/templates/routeweave-zapret-apply.sh"'
        in text
    )
    assert (
        'install -m 0755 "$APP_DIR/templates/routeweave-zapret-profile-set.sh"'
        in text
    )
    assert '"$ZAPRET_APPLY"' in text
    assert '"$ZAPRET_PROFILE_SET"' in text
    # Helpers доступны панели через sudo без wildcard.
    sudoers = text.split("write_panel_sudoers()", 1)[1].split("}", 1)[0]
    assert "$ZAPRET_APPLY" in sudoers
    assert "$ZAPRET_PROFILE_SET" in sudoers
    assert "routeweave ALL=(root) NOPASSWD:" in sudoers


def test_install_creates_profile_dir_and_enables_instances():
    text = INSTALL.read_text(encoding="utf-8")
    assert 'install -d -m 0755 -o routeweave -g routeweave "$ETC/zapret.d"' in text
    assert "enable_zapret_profiles" in text
    assert "restart_zapret_profiles" in text
    # Профили включаются до перезапуска default, перезапускаются — после:
    # default-юнит генерирует свежие hostlist'ы своим ExecStartPre.
    launch = text[text.index('info "Запуск сервисов..."'):]
    assert launch.index("enable_zapret_profiles") < launch.index(
        "systemctl restart mihomo zapret-nfqws"
    )
    assert launch.index("systemctl restart mihomo zapret-nfqws") < launch.index(
        "restart_zapret_profiles"
    )


def test_unit_template_contract():
    text = UNIT.read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ExecStart=/etc/routeweave/nfqws-launch.sh %i" in directives
    assert "Requires=zapret-nfqws.service" in directives
    assert "ExecStartPre" not in directives
    assert "ExecStopPost" not in directives
    assert "Restart=on-failure" in directives
    for line in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE",
    ):
        assert line in directives, line


# ------------------------------------------------------------ enable/restart


def profile_script(action: str) -> str:
    return (
        'ETC="$1"\n'
        "info() { printf 'INFO: %s\\n' \"$*\"; }\n"
        "ok() { printf 'OK: %s\\n' \"$*\"; }\n"
        "warn() { printf 'WARN: %s\\n' \"$*\" >&2; }\n"
        + extract_function(INSTALL, "zapret_profile_action")
        + extract_function(INSTALL, "enable_zapret_profiles")
        + extract_function(INSTALL, "restart_zapret_profiles")
        + f'\n{action}_zapret_profiles "$2"\n'
    )


def run_profile_action(action: str, etc: Path, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    fake_systemctl(bin_dir)
    log = tmp_path / "systemctl.log"
    env = dict(BASE_ENV)
    env["PATH"] = f"{bin_dir}:{BASE_ENV['PATH']}"
    env["SYSTEMCTL_LOG"] = str(log)
    proc = subprocess.run(
        ["bash", "-c", profile_script(action), "bash", str(etc), str(etc / "zapret.d")],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_enable_profiles_without_directory_is_not_an_error(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    proc, calls = run_profile_action("enable", etc, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert "каталога" in proc.stdout


def test_enable_profiles_only_valid_names(tmp_path):
    etc = tmp_path / "etc"
    profiles = etc / "zapret.d"
    profiles.mkdir(parents=True)
    for name in ("alpha", "split", "default", "Bad", "a" * 17):
        (profiles / f"{name}.conf").write_text("LIST=dpi.txt\nQNUM=201\n", encoding="utf-8")
    # Не *.conf — не профиль.
    (profiles / "notes.txt").write_text("ignore\n", encoding="utf-8")

    proc, calls = run_profile_action("enable", etc, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert calls == ["enable zapret-nfqws@alpha.service", "enable zapret-nfqws@split.service"]
    assert "недопустимое имя" in proc.stderr


def test_restart_profiles_uses_restart(tmp_path):
    etc = tmp_path / "etc"
    profiles = etc / "zapret.d"
    profiles.mkdir(parents=True)
    (profiles / "split.conf").write_text("LIST=dpi.txt\nQNUM=201\n", encoding="utf-8")

    proc, calls = run_profile_action("restart", etc, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert calls == ["restart zapret-nfqws@split.service"]


# ------------------------------------------------------------------- helper


class ApplySandbox:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.etc = tmp_path / "etc"
        self.etc.mkdir()
        self.profiles = self.etc / "zapret.d"
        self.profiles.mkdir()
        self.log = tmp_path / "calls.log"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake_systemctl(self.bin)
        self.updater = tmp_path / "update-lists.sh"
        self.updater.write_text(
            "#!/bin/sh\nprintf 'updater\\n' >> \"$CALL_LOG\"\nexit \"${FAKE_UPDATER_RC:-0}\"\n",
            encoding="utf-8",
        )
        self.updater.chmod(0o755)

    def conf(self, profile_dir: str | None = None) -> None:
        path = self.profiles if profile_dir is None else profile_dir
        (self.etc / "zapret.conf").write_text(
            f"PROFILE_DIR={path}\n", encoding="utf-8"
        )

    def profile(self, name: str = "split") -> None:
        (self.profiles / f"{name}.conf").write_text(
            "LIST=dpi-split.txt\nQNUM=201\nDESYNC=multisplit\n", encoding="utf-8"
        )

    def run(self, **env_extra: str):
        env = dict(BASE_ENV)
        env.update(
            {
                "PATH": f"{self.bin}:{BASE_ENV['PATH']}",
                "ROUTEWEAVE_ETC": str(self.etc),
                "ROUTEWEAVE_UPDATER": str(self.updater),
                "SYSTEMCTL": str(self.bin / "systemctl"),
                "SYSTEMCTL_LOG": str(self.log),
                "CALL_LOG": str(self.log),
            }
        )
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(APPLY)],
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )


@pytest.fixture
def apply_sandbox(tmp_path):
    return ApplySandbox(tmp_path)


def test_apply_restarts_default_and_profiles(apply_sandbox):
    apply_sandbox.conf()
    apply_sandbox.profile("split")
    apply_sandbox.profile("alpha")

    proc = apply_sandbox.run()

    assert proc.returncode == 0, proc.stderr
    calls = apply_sandbox.log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "updater"
    assert calls[1] == "restart zapret-nfqws"
    assert calls[2:] == [
        "restart zapret-nfqws@alpha.service",
        "restart zapret-nfqws@split.service",
    ]


def test_apply_without_profile_dir_is_ok(apply_sandbox):
    missing = apply_sandbox.root / "missing"
    apply_sandbox.conf(str(missing))

    proc = apply_sandbox.run()

    assert proc.returncode == 0, proc.stderr
    calls = apply_sandbox.log.read_text(encoding="utf-8").splitlines()
    assert calls == ["updater", "restart zapret-nfqws"]


def test_apply_propagates_updater_failure(apply_sandbox):
    apply_sandbox.conf()
    apply_sandbox.profile()

    proc = apply_sandbox.run(FAKE_UPDATER_RC="2")

    assert proc.returncode == 1
    assert "routeweave-update-lists.sh завершился ошибкой" in proc.stderr
    # Ошибка обновления списков не мешает перезапуску.
    calls = apply_sandbox.log.read_text(encoding="utf-8").splitlines()
    assert "restart zapret-nfqws" in calls


def test_apply_propagates_systemctl_failure(apply_sandbox):
    apply_sandbox.conf()
    apply_sandbox.profile()

    proc = apply_sandbox.run(FAKE_SYSTEMCTL_RC="1")

    assert proc.returncode == 1
    assert "не удалось перезапустить zapret-nfqws" in proc.stderr


def test_apply_ignores_invalid_profile_names(apply_sandbox):
    apply_sandbox.conf()
    apply_sandbox.profile("Bad")
    apply_sandbox.profile("split")

    proc = apply_sandbox.run()

    assert proc.returncode == 1
    assert "недопустимое имя" in proc.stderr
    calls = apply_sandbox.log.read_text(encoding="utf-8").splitlines()
    assert "restart zapret-nfqws@split.service" in calls
    assert "restart zapret-nfqws@Bad.service" not in calls


def test_apply_and_unit_bash_syntax():
    for path in (APPLY, PROFILE_SET):
        proc = subprocess.run(
            ["bash", "-n", str(path)],
            check=False,
            text=True,
            capture_output=True,
            env=BASE_ENV,
        )
        assert proc.returncode == 0, f"{path.name}: {proc.stderr}"


# ---------------------------------------------------------------- uninstall


def test_uninstall_removes_template_helper_and_instances():
    text = UNINSTALL.read_text(encoding="utf-8")

    units = re.search(r"^UNITS=\(\n(.*?)^\)\n", text, re.MULTILINE | re.DOTALL)
    assert units, "не найден массив UNITS"
    assert "zapret-nfqws@.service" in units.group(1).split()

    helpers = re.search(r"^HELPERS=\(\n(.*?)^\)\n", text, re.MULTILINE | re.DOTALL)
    assert helpers, "не найден массив HELPERS"
    assert "/usr/local/sbin/routeweave-zapret-apply.sh" in helpers.group(1).split()
    assert "/usr/local/sbin/routeweave-zapret-profile-set.sh" in helpers.group(1).split()

    assert "systemctl disable --now 'zapret-nfqws@*.service'" in text
    assert "DRY: systemctl disable --now zapret-nfqws@*.service" in text


def test_uninstall_dry_run_shows_profile_stop(tmp_path):
    etc = tmp_path / "etc"
    (etc / "lists").mkdir(parents=True)
    (etc / "backup").mkdir()
    (etc / ".env").write_text("LAN_IFACE='eth0'\n", encoding="utf-8")

    env = dict(BASE_ENV)
    env["ROUTEWEAVE_ETC"] = str(etc)
    env["ROUTEWEAVE_BACKUP_DIR"] = str(etc / "backup")
    proc = subprocess.run(
        ["bash", str(UNINSTALL), "--dry-run"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "DRY: systemctl disable --now zapret-nfqws@*.service" in proc.stdout
    # Без --purge сам /etc/routeweave (включая zapret.d) остаётся.
    assert f"DRY: rm -rf {etc}" not in proc.stdout.splitlines()


# ------------------------------------------------------- profile-set helper


def run_profile_set(etc: Path, *args: str):
    env = dict(BASE_ENV)
    env["ROUTEWEAVE_ETC"] = str(etc)
    return subprocess.run(
        ["bash", str(PROFILE_SET), *args],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def test_profile_set_default_upserts_only_strategy_keys(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    conf = etc / "zapret.conf"
    conf.write_text(
        "# комментарий владельца\n"
        "QNUM=200\n"
        "NFQWS_MARK=0x80000\n"
        "DESYNC=fake,multidisorder\n"
        "FOOLING=md5sig\n"
        'EXTRA="--dpi-desync-repeats=11"\n'
        "AUTOTTL_ARG=10-20\n"
        "TTL_ARG=64\n",
        encoding="utf-8",
    )

    proc = run_profile_set(
        etc,
        "default",
        "--desync",
        "multisplit",
        "--fooling",
        "none",
        "--extra",
        "--dpi-desync-split-pos=1 --dpi-desync-fwmark=$NFQWS_MARK",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""
    text = conf.read_text(encoding="utf-8")
    assert "# комментарий владельца" in text
    assert "QNUM=200" in text
    assert "NFQWS_MARK=0x80000" in text
    assert 'DESYNC="multisplit"' in text
    assert 'FOOLING="none"' in text
    assert '$NFQWS_MARK' in text
    # Неуказанные optional-ключи не стираются.
    assert "AUTOTTL_ARG=10-20" in text
    assert "TTL_ARG=64" in text


def test_profile_set_updates_profile_and_optional_keys(tmp_path):
    etc = tmp_path / "etc"
    profiles = etc / "zapret.d"
    profiles.mkdir(parents=True)
    conf = profiles / "split.conf"
    conf.write_text(
        "LIST=dpi-split.txt\nQNUM=201\nDESYNC=multisplit\n"
        "FOOLING=none\nAUTOTTL_ARG=\nTTL_ARG=\nEXTRA=\n",
        encoding="utf-8",
    )

    proc = run_profile_set(
        etc,
        "split",
        "--desync",
        "hostfakesplit",
        "--fooling",
        "md5sig",
        "--extra",
        "--dpi-desync-split-pos=1",
        "--autottl",
        "1-2",
        "--ttl",
        "5",
    )

    assert proc.returncode == 0, proc.stderr
    text = conf.read_text(encoding="utf-8")
    assert "LIST=dpi-split.txt" in text
    assert "QNUM=201" in text
    assert 'DESYNC="hostfakesplit"' in text
    assert 'FOOLING="md5sig"' in text
    assert 'AUTOTTL_ARG="1-2"' in text
    assert 'TTL_ARG="5"' in text


@pytest.mark.parametrize(
    "args",
    [
        ("default", "--desync", "evil", "--fooling", "none", "--extra", ""),
        ("default", "--desync", "multisplit", "--fooling", "evil", "--extra", ""),
        ("default", "--desync", "multisplit", "--fooling", "none", "--extra", "x;id"),
        ("default", "--desync", "multisplit", "--fooling", "none", "--extra", "x$(id)"),
        ("default", "--desync", "multisplit", "--fooling", "none", "--extra", "x`id`"),
        ("default", "--desync", "multisplit", "--fooling", "none"),
        ("../split", "--desync", "multisplit", "--fooling", "none", "--extra", ""),
    ],
)
def test_profile_set_rejects_invalid_values(tmp_path, args):
    etc = tmp_path / "etc"
    etc.mkdir()
    conf = etc / "zapret.conf"
    original = "DESYNC=fake,multidisorder\nFOOLING=md5sig\n"
    conf.write_text(original, encoding="utf-8")

    proc = run_profile_set(etc, *args)

    assert proc.returncode == 1, (args, proc.stdout, proc.stderr)
    assert proc.stderr.startswith("routeweave-zapret-profile-set:"), proc.stderr
    assert conf.read_text(encoding="utf-8") == original


def test_profile_set_requires_existing_profile(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "zapret.conf").write_text("DESYNC=fake,multidisorder\n", encoding="utf-8")
    (etc / "zapret.d").mkdir()

    proc = run_profile_set(
        etc, "split", "--desync", "multisplit", "--fooling", "none", "--extra", ""
    )

    assert proc.returncode == 1
    assert "не найден" in proc.stderr
