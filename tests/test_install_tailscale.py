"""Тесты опциональной установки Tailscale в install.sh (без root и сети).

Проверяются инварианты:
- INSTALL_TAILSCALE спрашивается как INSTALL_TGWS и по умолчанию равен no;
- URL официального apt-репозитория строятся только для ubuntu/debian;
- шаг Tailscale стоит после tg-ws-proxy и до ufw, поэтому command -v tailscale
  в ufw-блоке видит свежепоставленный пакет;
- auth key не попадает в /etc/routeweave/.env;
- uninstall не удаляет пакет Tailscale и его состояние.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install.sh"
UNINSTALL = REPO_ROOT / "uninstall.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
FW_TAILSCALE = REPO_ROOT / "templates" / "fw-tailscale.sh"


def run_install(*args):
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        check=False,
        text=True,
        capture_output=True,
    )


def extract_function(script, func_name):
    """Собрать одну функцию из скрипта по имени."""
    text = script.read_text(encoding="utf-8")
    # После "{" может быть комментарий (например, у normalize_yn).
    func = re.search(
        rf"^{re.escape(func_name)}\(\) \{{[^\n]*\n.*?^\}}\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert func, f"{script.name}: не найдена функция {func_name}"
    return func.group(0)


def run_function(script, func_name, *args):
    """Выполнить извлечённую функцию в bash и вернуть завершённый процесс."""
    script_text = extract_function(script, func_name) + f'\n{func_name} "$@"\n'
    return subprocess.run(
        ["bash", "-c", script_text, "bash", *map(str, args)],
        check=False,
        text=True,
        capture_output=True,
    )


def test_help_mentions_tailscale():
    proc = run_install("--help")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "INSTALL_TAILSCALE=yes|no" in out
    assert re.search(r"--defaults[\s\S]{0,600}INSTALL_TAILSCALE=no", out), out
    assert "TS_AUTHKEY_FILE" in out
    assert "TS_AUTHKEY" in out


def test_tailscale_prompt_defaults_to_no():
    text = INSTALL.read_text(encoding="utf-8")
    assert re.search(
        r'INSTALL_TAILSCALE="\$\{INSTALL_TAILSCALE:-no\}"', text
    ), "нет значения по умолчанию no"
    assert re.search(
        r'ask\s+INSTALL_TAILSCALE\b[^\n]*"\$\{INSTALL_TAILSCALE:-no\}"', text
    ), "нет интерактивного вопроса с дефолтом no"
    assert re.search(
        r'INSTALL_TAILSCALE=\$\(normalize_yn\s+"\$INSTALL_TAILSCALE"\)', text
    ), "ответ не проходит через normalize_yn"


def test_ask_uses_default_in_noninteractive_mode():
    """ask при DEFAULTS=1 не читает stdin и возвращает дефолт."""
    func = extract_function(INSTALL, "ask")
    script = "DEFAULTS=1\n" + func + '\nask RESULT "q" "no"\nprintf "%s" "$RESULT"\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        check=False,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "no"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("yes", "yes"),
        ("Y", "yes"),
        ("да", "yes"),
        ("1", "yes"),
        ("true", "yes"),
        ("no", "no"),
        ("N", "no"),
        ("нет", "no"),
        ("0", "no"),
        ("false", "no"),
    ],
)
def test_normalize_yn_accepts_known_values(value, expected):
    proc = run_function(INSTALL, "normalize_yn", value)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected


def test_normalize_yn_rejects_garbage():
    proc = run_function(INSTALL, "normalize_yn", "maybe")
    assert proc.returncode == 1
    assert proc.stdout == ""


@pytest.mark.parametrize(
    ("os_id", "codename", "expected"),
    [
        (
            "ubuntu",
            "noble",
            [
                "https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg",
                "https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list",
            ],
        ),
        (
            "debian",
            "bookworm",
            [
                "https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg",
                "https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list",
            ],
        ),
    ],
)
def test_tailscale_repo_urls_supported(os_id, codename, expected):
    proc = run_function(INSTALL, "tailscale_repo_urls", os_id, codename)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == expected


@pytest.mark.parametrize(
    ("os_id", "codename"),
    [
        ("arch", ""),
        ("fedora", "40"),
        ("ubuntu", ""),
        ("debian", ""),
        ("ubuntu", "../etc"),
        ("ubuntu", "noble/../../x"),
        ("ubuntu", "Noble"),
        ("", "noble"),
    ],
)
def test_tailscale_repo_urls_rejected(os_id, codename):
    proc = run_function(INSTALL, "tailscale_repo_urls", os_id, codename)
    assert proc.returncode == 1, proc.stdout
    assert proc.stdout == ""


def test_tailscale_step_before_ufw():
    text = INSTALL.read_text(encoding="utf-8")
    ts_step = re.search(r"^# -+ \d+\. Tailscale\b", text, re.MULTILINE)
    ufw_step = re.search(r"^# -+ \d+\. Firewall\b", text, re.MULTILINE)
    assert ts_step, "не найден шаг Tailscale"
    assert ufw_step, "не найден шаг Firewall"
    assert ts_step.start() < ufw_step.start(), (
        "шаг Tailscale должен быть раньше ufw-блока"
    )

    call = text.find("install_tailscale || FAIL=1")
    assert call != -1, "install_tailscale не вызывается из main"
    assert call < ufw_step.start()

    # В ufw-блоке (до следующего шага) должна быть проверка наличия команды.
    next_step = re.search(
        r"^# -+ \d+\. ", text[ufw_step.end():], re.MULTILINE
    )
    end = ufw_step.end() + next_step.start() if next_step else len(text)
    ufw_block = text[ufw_step.start():end]
    assert "command -v tailscale" in ufw_block
    assert "tailscale0" in ufw_block


def test_step_numbers_renumbered():
    text = INSTALL.read_text(encoding="utf-8")
    assert re.search(r"^# -+ 11\. Tailscale\b", text, re.MULTILINE)
    assert re.search(r"^# -+ 12\. Firewall\b", text, re.MULTILINE)
    assert re.search(r"^# -+ 13\. Запуск\b", text, re.MULTILINE)
    assert re.search(r"^# -+ 14\. WDTT-Plus\b", text, re.MULTILINE)


def test_install_tailscale_flags_and_package():
    func = extract_function(INSTALL, "install_tailscale")
    text = INSTALL.read_text(encoding="utf-8")

    assert "--advertise-exit-node" in func
    assert "--netfilter-mode=nodivert" in func
    assert "--accept-dns=false" in func
    assert "--timeout=60s" in func

    assert re.search(
        r"tailscale set\s+--advertise-exit-node\s+"
        r"--netfilter-mode=nodivert\s+--accept-dns=false",
        func,
    ), "для Running нужен tailscale set с флагами"
    assert re.search(
        r"tailscale up.*?--timeout=60s", func, re.DOTALL
    ), "для авторизации нужен tailscale up с таймаутом"

    assert "apt-get install -y -qq tailscale tailscale-archive-keyring" in func
    assert "systemctl enable --now tailscaled" in func
    assert "command -v tailscale" in func

    assert "/usr/share/keyrings/tailscale-archive-keyring.gpg" in text
    assert "/etc/apt/sources.list.d/tailscale.list" in text
    assert func.count("install -m 0644") >= 2


def test_backend_state_uses_python_and_retries():
    state_func = extract_function(INSTALL, "ts_backend_state")
    wait_func = extract_function(INSTALL, "ts_wait_state")

    assert "BackendState" in state_func
    assert "python3 -c" in state_func
    assert "jq" not in state_func
    assert "sleep 1" in wait_func
    assert "Running" in wait_func


def test_auth_key_handling():
    func = extract_function(INSTALL, "install_tailscale")

    assert "--auth-key=file:" in func
    assert "TS_AUTHKEY_FILE" in func
    assert "TS_AUTHKEY" in func
    assert "TS_AUTHKEY_FILE должен быть абсолютным" in func
    assert re.search(r'\[ ! -r "\$TS_AUTHKEY_FILE" \]', func)
    assert "umask 077" in func
    assert "WORK_DIR/ts-authkey" in func
    assert 'rm -f "$auth_tmp"' in func
    assert "unset TS_AUTHKEY" in func
    assert "требуется авторизация" in func


def test_status_strings_present():
    text = INSTALL.read_text(encoding="utf-8")
    for status in (
        "не устанавливался",
        "пропущен",
        "установлен",
        "настроен",
        "требуется авторизация",
        "ошибка",
    ):
        assert status in text, f"нет статуса {status!r}"


def test_summary_and_tail_mention_exit_node():
    text = INSTALL.read_text(encoding="utf-8")
    assert "Use with exit node" in text
    assert "одобрите" in text
    assert "INSTALL_TAILSCALE=yes" in text


def test_install_header_lists_tailscale():
    header = "\n".join(
        INSTALL.read_text(encoding="utf-8").splitlines()[:15]
    )
    assert "Tailscale" in header


def test_env_example_has_tailscale_settings_without_secrets():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    lines = text.splitlines()
    # Шапка — всё до первой активной строки. Install-time переменные там
    # перечислены комментарием, активных INSTALL_* строк быть не должно.
    header_end = next(
        i for i, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("#")
    )
    header = "\n".join(lines[:header_end])
    assert re.search(r"^#.*\bINSTALL_TAILSCALE\b", header, re.MULTILINE)
    assert not re.search(r"^\s*INSTALL_TAILSCALE\s*=", text, re.MULTILINE)

    # Формат файла — KEY='value': значения именно в одинарных кавычках.
    assert "TS_IFACE='tailscale0'" in text
    assert "TS_SUBNET='100.64.0.0/10'" in text

    for line in lines:
        assert not re.match(r"\s*TS_AUTHKEY(_FILE)?\s*=", line), line
    # Комментарий о том, что ключи здесь не хранятся, должен остаться.
    assert "TS_AUTHKEY" in text


def test_install_does_not_write_tailscale_authkey_to_env():
    text = INSTALL.read_text(encoding="utf-8")
    assert not re.search(r"env_line\s+TS_AUTHKEY", text)
    assert not re.search(r"env_line\s+TS_AUTHKEY_FILE", text)
    assert not re.search(r"TS_AUTHKEY\b[^\n]*\$ETC/\.env", text)

    start = text.index('if [ ! -f "$ETC/.env" ]; then')
    end = text.index('chown root:routeweave "$ETC/.env"')
    env_block = text[start:end]
    assert "TS_AUTHKEY" not in env_block
    assert "TS_AUTHKEY_FILE" not in env_block


def test_uninstall_keeps_tailscale_package():
    text = UNINSTALL.read_text(encoding="utf-8")
    assert not re.search(r"apt(-get)?\s+(remove|purge)[^\n]*tailscale", text)
    assert not re.search(r"rm\s+-rf\s+/var/lib/tailscale", text)
    assert "не удаляет пакет Tailscale" in text
    assert "/var/lib/tailscale" in text


def test_fw_tailscale_header_says_installer_may_install():
    text = FW_TAILSCALE.read_text(encoding="utf-8")
    header = "\n".join(text.splitlines()[:20])
    assert "INSTALL_TAILSCALE=yes" in header
    assert "только читает состояние" in header
    assert re.search(r"не\s+устанавливает", header)
