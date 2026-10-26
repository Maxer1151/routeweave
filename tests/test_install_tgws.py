"""Тесты tg-ws-proxy в install.sh: порядок апстримов (`--pinned-upstream`).

Проверяются инварианты:
- чистая функция tgws_pinned_upstream: env имеет приоритет, иначе при
  непустом домене CF Worker ставится `cfworker,ws,tcp`, иначе пин пуст;
- невалидный список тиров отвергается (rc=2) и не попадает в .env;
- установщик не пишет TG_CF_IP и не пишет пустой TG_PINNED_UPSTREAM;
- документация и пример .env описывают ключ.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"
SECURITY = REPO_ROOT / "SECURITY.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
CF_WORKER_DOC = REPO_ROOT / "docs" / "cf-worker-tg.md"
COMPOSE = REPO_ROOT / "docker-compose.yml"


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


def run_function(script, func_name, *args, env=None):
    """Выполнить извлечённую функцию в bash и вернуть завершённый процесс."""
    script_text = extract_function(script, func_name) + f'\n{func_name} "$@"\n'
    base = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    if env:
        base.update(env)
    return subprocess.run(
        ["bash", "-c", script_text, "bash", *map(str, args)],
        check=False,
        text=True,
        capture_output=True,
        env=base,
    )


@pytest.mark.parametrize(
    ("cf_domain", "pin", "expected"),
    [
        # Домен задан, env не задан — Worker первым: direct WS режется на SYN.
        ("example.workers.dev", None, "cfworker,ws,tcp"),
        # Домена нет — пин не задаём, порядок остаётся дефолтным у прокси.
        ("", None, ""),
        # Env имеет приоритет над доменом.
        ("example.workers.dev", "ws,cfworker,tcp", "ws,cfworker,tcp"),
        ("example.workers.dev", "ws", "ws"),
        ("", "cfworker", "cfworker"),
        ("", "ws,cfworker,cfproxy,mtproto,tcp", "ws,cfworker,cfproxy,mtproto,tcp"),
    ],
)
def test_tgws_pinned_upstream_values(cf_domain, pin, expected):
    env = {} if pin is None else {"TG_PINNED_UPSTREAM": pin}
    proc = run_function(INSTALL, "tgws_pinned_upstream", cf_domain, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected


@pytest.mark.parametrize(
    "pin",
    [
        "cfworker;ws",
        "CFWORKER",
        "ws, cfworker",
        " ws",
        "worker",
        "ws,",
        "",
    ],
)
def test_tgws_pinned_upstream_rejects_invalid(pin):
    # Пустая строка в env эквивалентна «не задано» — это не ошибка, а дефолт.
    proc = run_function(
        INSTALL,
        "tgws_pinned_upstream",
        "example.workers.dev",
        env={"TG_PINNED_UPSTREAM": pin},
    )
    if pin == "":
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "cfworker,ws,tcp"
        return
    assert proc.returncode == 2, proc.stdout
    assert proc.stdout == ""


def test_install_calls_function_with_die():
    text = INSTALL.read_text(encoding="utf-8")
    assert re.search(
        r'TG_PINNED_UPSTREAM=\$\(tgws_pinned_upstream\s+"\$TG_CF_WORKER_DOMAIN"\)'
        r"\s*\\\n\s*\|\|\s*die",
        text,
    ), "нет вызова tgws_pinned_upstream с понятной ошибкой"


GUARDED_WRITE = (
    r'if \[ -n "\$TG_PINNED_UPSTREAM" \]; then(?:\s*\n\s*echo[^\n]*)?\s*\n'
    r'\s*env_line TG_PINNED_UPSTREAM "\$TG_PINNED_UPSTREAM"'
)


def test_pinned_upstream_written_only_when_not_empty():
    text = INSTALL.read_text(encoding="utf-8")
    # Шаблон свежего .env и .env.tgws: запись только под условием -n.
    assert re.search(GUARDED_WRITE, text), (
        "в .env/.env.tgws ключ должен писаться только при непустом значении"
    )
    assert text.count("env_line TG_PINNED_UPSTREAM") >= 3

    # Блоки: свежий .env (до chown) и .env.tgws.
    start = text.index('if [ ! -f "$ETC/.env" ]; then')
    end = text.index('chown root:routeweave "$ETC/.env"')
    fresh_env = text[start:end]
    assert "env_line TG_PINNED_UPSTREAM" in fresh_env
    assert re.search(GUARDED_WRITE, fresh_env)

    tgws_start = text.index('env_line TG_SECRET "$TG_SECRET"')
    tgws_block = text[tgws_start:text.index('} > "$WORK_DIR/env.tgws"', tgws_start)]
    assert "env_line TG_CF_WORKER_DOMAIN" in tgws_block
    assert re.search(GUARDED_WRITE, tgws_block)


def test_existing_env_branch_updates_and_removes_key():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index('warn "$ETC/.env уже существует')
    end = text.index('chown root:routeweave "$ETC/.env"')
    branch = text[start:end]

    assert re.search(r"sed -i .*\^TG_PINNED_UPSTREAM=\.\*", branch), (
        "нет идемпотентного обновления строки"
    )
    assert re.search(r"sed -i '/\^TG_PINNED_UPSTREAM=/d'", branch), (
        "нет удаления устаревшего пина"
    )
    assert re.search(r'if \[ "\$INSTALL_TGWS" = "yes" \]', branch)


def test_install_does_not_write_tg_cf_ip():
    text = INSTALL.read_text(encoding="utf-8")
    assert "TG_CF_IP" not in text, (
        "TG_CF_IP задавать не нужно: валидные edge дают тот же кластер"
    )


def test_help_mentions_pinned_upstream():
    proc = subprocess.run(
        ["bash", str(INSTALL), "--help"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TG_PINNED_UPSTREAM" in proc.stdout
    assert "cfworker,ws,tcp" in proc.stdout


def test_docs_describe_pinned_upstream():
    doc = CF_WORKER_DOC.read_text(encoding="utf-8")
    assert "TG_PINNED_UPSTREAM" in doc
    assert "cfworker,ws,tcp" in doc
    assert "--pinned-upstream" in doc

    readme = README.read_text(encoding="utf-8")
    assert re.search(r"^\|\s*`TG_PINNED_UPSTREAM`\s*\|", readme, re.MULTILINE)
    assert "docs/cf-worker-tg.md" in readme

    changelog = CHANGELOG.read_text(encoding="utf-8")
    unreleased = changelog.index("[Unreleased]")
    tail = changelog[unreleased:]
    assert "TG_PINNED_UPSTREAM" in tail
    assert "cfworker,ws,tcp" in tail

    security = SECURITY.read_text(encoding="utf-8")
    assert ".env.tgws" in security
    assert "TG_PINNED_UPSTREAM" in security
    assert "TG_CF_WORKER_DOMAIN" in security

    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "TG_PINNED_UPSTREAM" in example
    # В примере ключ закомментирован: пустое значение писать нельзя.
    for line in example.splitlines():
        assert not re.match(r"\s*TG_PINNED_UPSTREAM\s*=", line), line

    compose = COMPOSE.read_text(encoding="utf-8")
    assert "TG_PINNED_UPSTREAM" in compose
