"""Тесты templates/nfqws-launch.sh: whitelist символов и сборка argv без шелла.

Скрипт — ExecStart юнита zapret-nfqws.service. Он должен передавать значения
из /etc/routeweave/zapret.conf в nfqws массивом argv, не отдавая их шеллу:
раньше `;`, `$(...)`, обратные кавычки и `|` из конфига исполнялись от root.

Тесты не требуют root, iptables, сети и настоящего nfqws: вместо бинарника в
NFQWS_BIN подставляется фейковый `#!/bin/sh`, который только записывает свой
argv (NUL-разделённый) в файл и выходит с кодом 0.

Запуск:
    pytest tests/test_nfqws_launch.py -v
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCH = REPO / "templates" / "nfqws-launch.sh"
BASH = shutil.which("bash")

# Путь к hostlist только участвует в argv/warn — файл создавать не нужно.
# Значение заведомо проходит whitelist (A-Za-z0-9 _.,:=/+@%-).
HOSTLIST = "/tmp/routeweave-test-nfqws/dpi-zapret.txt"

pytestmark = pytest.mark.skipif(
    BASH is None or not LAUNCH.is_file(),
    reason="нет bash или templates/nfqws-launch.sh",
)


class Sandbox:
    """Временный ROUTEWEAVE_ETC с zapret.conf и фейковый nfqws."""

    def __init__(self, root: Path):
        self.root = root
        self.etc = root / "etc"
        self.etc.mkdir()
        self.argv_log = root / "argv.bin"
        self.fake = root / "nfqws"
        self.fake.write_text(
            "#!/bin/sh\n"
            "# фейковый nfqws: фиксирует argv и выходит успешно\n"
            'printf \'%s\\0\' "$@" > "$ROUTEWEAVE_TEST_ARGV"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)

    def conf(self, **override: str) -> str:
        """Собрать zapret.conf в формате, который читает лаунчер."""
        values = {
            "QNUM": "200",
            "DESYNC": "fake,multidisorder",
            "FOOLING": "md5sig",
            "AUTOTTL_ARG": "",
            "TTL_ARG": "",
            "EXTRA": "--dpi-desync-repeats=11 --dpi-desync-split-pos=1,midsld",
            "HOSTLIST": HOSTLIST,
            "HOSTLIST_DIR": str(self.root / "run"),
        }
        values.update(override)
        lines = ["# тестовый zapret.conf"]
        lines += [f"{key}={value}" for key, value in values.items()]
        return "\n".join(lines) + "\n"

    def profile(self, name: str = "split", **override: str) -> Path:
        """Записать профиль /etc/routeweave/zapret.d/<name>.conf."""
        profile_dir = self.etc / "zapret.d"
        profile_dir.mkdir(exist_ok=True)
        values = {
            "LIST": "dpi-split.txt",
            "QNUM": "201",
            "DESYNC": "multisplit",
            "FOOLING": "none",
            "AUTOTTL_ARG": "",
            "TTL_ARG": "",
            "EXTRA": "--dpi-desync-split-pos=1",
        }
        values.update(override)
        path = profile_dir / f"{name}.conf"
        path.write_text(
            "# профиль\n" + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
            encoding="utf-8",
        )
        return path

    def run(self, *profile_args: str):
        """Запустить лаунчер. Возвращает (CompletedProcess, argv или None)."""
        self.argv_log.unlink(missing_ok=True)
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(self.root),
            "LC_ALL": "C",
            "ROUTEWEAVE_ETC": str(self.etc),
            "NFQWS_BIN": str(self.fake),
            "ROUTEWEAVE_TEST_ARGV": str(self.argv_log),
        }
        res = subprocess.run(
            [BASH, str(LAUNCH), *profile_args],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        argv = None
        if self.argv_log.exists():
            raw = self.argv_log.read_bytes()
            assert raw.endswith(b"\0"), "фейковый nfqws записал argv без NUL-разделителя"
            argv = raw[:-1].decode("utf-8", "surrogateescape").split("\0")
        return res, argv


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_valid_conf_passes_expected_argv(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert argv == [
        "--qnum=200",
        "--dpi-desync=fake,multidisorder",
        "--dpi-desync-fooling=md5sig",
        "--dpi-desync-repeats=11",
        "--dpi-desync-split-pos=1,midsld",
        f"--hostlist={HOSTLIST}",
    ]


def test_extra_mark_placeholder_is_expanded(sandbox: Sandbox):
    """Шаблонный EXTRA с $NFQWS_MARK превращается в число без участия шелла."""
    sandbox.etc.joinpath("zapret.conf").write_text(
        sandbox.conf(
            NFQWS_MARK="0x90000",
            EXTRA="--dpi-desync-fwmark=$NFQWS_MARK",
        ),
        encoding="utf-8",
    )
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert "--dpi-desync-fwmark=0x90000" in argv
    assert not any("$" in word or "NFQWS_MARK" in word for word in argv), argv


def test_extra_literal_mark_still_works(sandbox: Sandbox):
    """Старый конфиг без NFQWS_MARK и с литералом в EXTRA не ломается."""
    sandbox.etc.joinpath("zapret.conf").write_text(
        sandbox.conf(EXTRA="--dpi-desync-fwmark=0x90000"),
        encoding="utf-8",
    )
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert "--dpi-desync-fwmark=0x90000" in argv


def test_bad_nfqws_mark_falls_back_to_default(sandbox: Sandbox):
    """Нечисловая метка не попадает в argv: берём 0x80000 и предупреждаем."""
    sandbox.etc.joinpath("zapret.conf").write_text(
        sandbox.conf(
            NFQWS_MARK="0x90000;id",
            EXTRA="--dpi-desync-fwmark=$NFQWS_MARK",
        ),
        encoding="utf-8",
    )
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert "не похож на число" in res.stderr
    assert "--dpi-desync-fwmark=0x80000" in argv


def test_argv_has_no_shell_artifacts(sandbox: Sandbox):
    """Ровно один exec, без `sh -c`, без кавычек и метасимволов в словах."""
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert argv
    assert "sh" not in argv and "-c" not in argv
    for word in argv:
        assert not set(word) & set(";|&$`<>\"'\\"), word
        assert "\n" not in word


def test_optional_words_are_split_into_separate_argv(sandbox: Sandbox):
    """AUTOTTL_ARG/TTL_ARG/EXTRA — набор слов, а не один аргумент."""
    sandbox.etc.joinpath("zapret.conf").write_text(
        sandbox.conf(
            AUTOTTL_ARG="--dpi-desync-autottl=2 --dpi-desync-autottl-ttl=8",
            TTL_ARG="--dpi-desync-ttl=5",
            EXTRA="",
        ),
        encoding="utf-8",
    )
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert "--dpi-desync-autottl=2" in argv
    assert "--dpi-desync-autottl-ttl=8" in argv
    assert "--dpi-desync-ttl=5" in argv
    # пустой EXTRA не должен превращаться в пустой аргумент
    assert "" not in argv


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("semicolon", "--a=1; id"),
        ("cmd_substitution", "--a=$(id)"),
        ("backtick", "--a=`id`"),
        ("pipe", "--a=1 | id"),
        ("ampersand", "--a=1 && id"),
        ("redirect", "--a=1 > /tmp/routeweave-pwned"),
        # перевод строки в значении: многострочное значение в кавычках
        ("newline", '"--a=1\n--b=2"'),
    ],
)
def test_extra_with_forbidden_chars_refuses_to_start(sandbox: Sandbox, name: str, payload: str):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(EXTRA=payload), encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode != 0, f"{name}: лаунчер не должен запускать nfqws"
    assert argv is None, f"{name}: fake nfqws всё-таки запущен"
    assert "nfqws-launch:" in res.stderr, res.stderr


@pytest.mark.parametrize(
    ("key", "payload"),
    [
        ("QNUM", "200;id"),
        ("QNUM", "200$(id)"),
        ("DESYNC", "fake`id`"),
        ("HOSTLIST", f"{HOSTLIST} | id"),
    ],
)
def test_forbidden_chars_in_single_token_keys(sandbox: Sandbox, key: str, payload: str):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(**{key: payload}), encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode != 0, f"{key}={payload!r}: запуск должен быть отклонён"
    assert argv is None
    assert "недопустим" in res.stderr, res.stderr


def test_missing_conf(sandbox: Sandbox):
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None
    assert "не найден" in res.stderr, res.stderr


def test_empty_conf(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text("", encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None
    assert "QNUM пустой" in res.stderr, res.stderr


@pytest.mark.parametrize("key", ["QNUM", "DESYNC", "HOSTLIST"])
def test_empty_required_key(sandbox: Sandbox, key: str):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(**{key: ""}), encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None
    assert f"{key} пустой" in res.stderr, res.stderr


def test_non_numeric_qnum(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(QNUM="nfqueue"), encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None
    assert "QNUM должен быть числом" in res.stderr, res.stderr


def test_unreadable_config_directory(sandbox: Sandbox):
    """zapret.conf-каталог: чтение падает, но nfqws не запускается."""
    sandbox.etc.joinpath("zapret.conf").mkdir()
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None


def test_extra_line_in_conf_is_ignored_with_warning(sandbox: Sandbox):
    """Строка не в формате KEY=VALUE отбрасывается, а не исполняется.

    Проверка на перевод строки выше показывает отказ; здесь — что «хвост»
    многострочной попытки не попадает ни в argv, ни в шелл.
    """
    text = sandbox.conf() + "$(id)\n"
    sandbox.etc.joinpath("zapret.conf").write_text(text, encoding="utf-8")
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert argv == [
        "--qnum=200",
        "--dpi-desync=fake,multidisorder",
        "--dpi-desync-fooling=md5sig",
        "--dpi-desync-repeats=11",
        "--dpi-desync-split-pos=1,midsld",
        f"--hostlist={HOSTLIST}",
    ]
    assert "не похожа на KEY=VALUE" in res.stderr, res.stderr


def test_missing_nfqws_binary(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.fake.chmod(0o644)  # не исполняемый
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None
    assert "не исполняемый" in res.stderr, res.stderr


def test_nonexistent_nfqws_binary(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.fake.unlink()
    res, argv = sandbox.run()

    assert res.returncode != 0
    assert argv is None
    assert "не найден" in res.stderr, res.stderr


def test_conf_with_crlf_and_quotes(sandbox: Sandbox):
    """CRLF и одна пара обрамляющих кавычек снимаются (как EnvironmentFile)."""
    text = (
        "QNUM=200\r\n"
        'DESYNC="fake,multidisorder"\r\n'
        "FOOLING=md5sig\r\n"
        f"HOSTLIST={HOSTLIST}\r\n"
    )
    sandbox.etc.joinpath("zapret.conf").write_bytes(text.encode("utf-8"))
    res, argv = sandbox.run()

    assert res.returncode == 0, res.stderr
    assert argv[:3] == [
        "--qnum=200",
        "--dpi-desync=fake,multidisorder",
        "--dpi-desync-fooling=md5sig",
    ]
    assert argv[-1] == f"--hostlist={HOSTLIST}"


# --------------------------------------------------------------------------
# профили: свой QNUM, свой hostlist, никакого наследования default
# --------------------------------------------------------------------------

def test_profile_uses_own_qnum_strategy_and_hostlist(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.profile(
        "split",
        QNUM="201",
        DESYNC="multisplit",
        FOOLING="none",
        EXTRA="--dpi-desync-split-pos=1 --dpi-desync-fwmark=0x80000",
    )

    res, argv = sandbox.run("split")

    assert res.returncode == 0, res.stderr
    assert argv == [
        "--qnum=201",
        "--dpi-desync=multisplit",
        "--dpi-desync-fooling=none",
        "--dpi-desync-split-pos=1",
        "--dpi-desync-fwmark=0x80000",
        f"--hostlist={sandbox.root / 'run' / 'dpi-zapret-split.txt'}",
    ]


def test_profile_does_not_inherit_default_strategy(sandbox: Sandbox):
    """Забытый в профиле DESYNC — ошибка, а не тихое наследование default."""
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.profile("split", DESYNC="")

    res, argv = sandbox.run("split")

    assert res.returncode != 0
    assert argv is None
    assert "DESYNC пустой" in res.stderr, res.stderr


def test_unknown_profile_is_rejected(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")

    res, argv = sandbox.run("missing")

    assert res.returncode != 0
    assert argv is None
    assert "не найден" in res.stderr, res.stderr


def test_profile_qnum_collision_with_default_is_rejected(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.profile("split", QNUM="200")

    res, argv = sandbox.run("split")

    assert res.returncode != 0
    assert argv is None
    assert "совпадает с очередью default" in res.stderr, res.stderr


@pytest.mark.parametrize("name", ["../evil", "split;id", "Split", "-bad", "a" * 17])
def test_invalid_profile_name_is_rejected(sandbox: Sandbox, name: str):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")

    res, argv = sandbox.run(name)

    assert res.returncode != 0, name
    assert argv is None, name
    assert "недопустимое имя профиля" in res.stderr, res.stderr


def test_extra_profile_argument_is_rejected(sandbox: Sandbox):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.profile()

    res, argv = sandbox.run("split", "extra")

    assert res.returncode != 0
    assert argv is None
    assert "лишние аргументы" in res.stderr, res.stderr


@pytest.mark.parametrize("list_name", ["../dpi.txt", "sub/dpi.txt", "..", "a" * 129])
def test_profile_list_must_be_plain_filename(sandbox: Sandbox, list_name: str):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.profile("split", LIST=list_name)

    res, argv = sandbox.run("split")

    assert res.returncode != 0, list_name
    assert argv is None, list_name
    assert "недопустимое имя списка" in res.stderr, res.stderr


@pytest.mark.parametrize(
    ("key", "payload"),
    [
        ("QNUM", "201;id"),
        ("DESYNC", "multisplit$(id)"),
        ("EXTRA", "--dpi-desync-split-pos=1 && id"),
    ],
)
def test_profile_rejects_shell_injection(sandbox: Sandbox, key: str, payload: str):
    sandbox.etc.joinpath("zapret.conf").write_text(sandbox.conf(), encoding="utf-8")
    sandbox.profile("split", **{key: payload})

    res, argv = sandbox.run("split")

    assert res.returncode != 0, f"{key}={payload}"
    assert argv is None
    assert "nfqws-launch:" in res.stderr, res.stderr
