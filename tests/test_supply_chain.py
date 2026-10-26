"""Supply-chain инварианты install.sh: закреплённые версии и fail-closed sha256.

Проверяется без сети и без root:
- ``verify_sha`` принимает только ровно 64 hex-символа в нижнем регистре и
  падает при пустой/битой/несовпавшей сумме; ``ALLOW_UNVERIFIED=1`` — явный
  тестовый обход с предупреждением;
- в install.sh нет mutable-источников (последний релиз GitHub, GitHub API),
  версии mihomo/WDTT/zashboard закреплены константами, а их суммы — 64 hex;
- zashboard качается по URL с ``$ZASHBOARD_VER`` и распаковывается только
  после проверки суммы, с отказом от абсолютных путей, ``..``, симлинков и
  setuid-файлов;
- zapret собирается с закреплённого коммита: HEAD сверяется с ``ZAPRET_REF``;
- ``--allow-unverified`` описан в ``--help`` и разобран в аргументах.

Функция и константы извлекаются из install.sh как текст: тесты не запускают
установщик целиком (он требует root и меняет систему).
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "install.sh"
TEXT = INSTALL.read_text(encoding="utf-8")

# Ожидаемые значения: менять только вместе с install.sh, осознанно.
EXPECTED_CONSTANTS = {
    "MIHOMO_PINNED_VER": "v1.19.13",
    "MIHOMO_SHA256_AMD64_COMPAT": (
        "e7a2bb3680cfbc58f9c382e46ca1863eb6093743da2ba86a81588bb552b8002a"
    ),
    "MIHOMO_SHA256_AMD64": (
        "c85a912c1374c88f602df6b5d8a9b7ce0771828fb66e5bd0bca9179518db90d8"
    ),
    "MIHOMO_SHA256_ARM64": (
        "7aa8651738202741467d83b9ce51638ab7f01eef352472d2fba17e71dd5cce9a"
    ),
    "ZASHBOARD_VER": "v3.29.1",
    "ZASHBOARD_SHA256_PINNED": (
        "4f16cce229e223bd07b5a3daccfbfb8bf349d7335572d412959284ab6c9bab50"
    ),
    "WDTT_PINNED_TAG": "v20",
    "WDTT_PINNED_NAME": (
        "WDTT-Plus-server-v20-installer-0.20.3-linux-amd64.tar.gz"
    ),
    "WDTT_SHA256": (
        "466aa573e9b97160964d06ccfb04a3f99a19f5f0b760b6491b0fe0ab97e040e8"
    ),
    "ZAPRET_PINNED_COMMIT": "c4d0b1990129dce2b28095277173ee0d7702d304",
}

SHA_CONSTANTS = (
    "MIHOMO_SHA256_AMD64_COMPAT",
    "MIHOMO_SHA256_AMD64",
    "MIHOMO_SHA256_ARM64",
    "ZASHBOARD_SHA256_PINNED",
    "WDTT_SHA256",
)

VERIFY_TEMPLATE = """set -u
info() {{ printf 'info %s\\n' "$*"; }}
ok()   {{ printf 'ok %s\\n' "$*"; }}
warn() {{ printf 'warn %s\\n' "$*" >&2; }}
die()  {{ printf 'die %s\\n' "$*" >&2; exit 1; }}
ALLOW_UNVERIFIED=${{ALLOW_UNVERIFIED:-0}}
{verify}
verify_sha "$1" "$2" "test-label"
"""


def find_constant(name):
    """Вернуть значение константы ``NAME="..."`` или None."""
    match = re.search(
        r'^' + re.escape(name) + r'="([^"]*)"\s*$', TEXT, re.MULTILINE
    )
    return match.group(1) if match else None


def extract_function(name):
    """Вырезать функцию ``name() {{ ... }}`` из install.sh как текст."""
    match = re.search(
        r"^" + re.escape(name) + r"\(\) \{[^\n]*\n.*?^\}\n",
        TEXT,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"install.sh: не найдена функция {name}"
    return match.group(0)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_verify_sha(workdir, path, sum_value, allow_unverified=None):
    """Выполнить извлечённый verify_sha в bash; вернуть CompletedProcess."""
    script = workdir / "verify_sha.sh"
    script.write_text(
        VERIFY_TEMPLATE.format(verify=extract_function("verify_sha")),
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workdir),
    }
    if allow_unverified is not None:
        env["ALLOW_UNVERIFIED"] = str(allow_unverified)
    return subprocess.run(
        ["bash", str(script), str(path), sum_value],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        cwd=workdir,
    )


@pytest.fixture()
def payload(tmp_path):
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"routeweave supply-chain test payload\n")
    return path


# --------------------------- verify_sha (fail-closed) ---------------------------


def test_verify_sha_accepts_exact_64_hex(payload, tmp_path):
    proc = run_verify_sha(tmp_path, payload, sha256_of(payload))
    assert proc.returncode == 0, proc.stderr
    assert "sha256 подтверждена" in proc.stdout


@pytest.mark.parametrize("length", [60, 32])
def test_verify_sha_rejects_truncated_sum(payload, tmp_path, length):
    proc = run_verify_sha(tmp_path, payload, sha256_of(payload)[:length])
    assert proc.returncode != 0
    assert "не задана/некорректна" in proc.stderr


def test_verify_sha_rejects_uppercase_sum(payload, tmp_path):
    # Суммы в константах — всегда нижний регистр; uppercase — битая сумма.
    proc = run_verify_sha(tmp_path, payload, sha256_of(payload).upper())
    assert proc.returncode != 0
    assert "не задана/некорректна" in proc.stderr


@pytest.mark.parametrize(
    "bad_sum",
    [
        "",  # пусто
        "z" * 64,  # не hex
        "g" + "0" * 63,  # не hex в первом символе
        "0" * 63 + "\n",  # перевод строки в «сумме»
        " " + "0" * 63,  # пробел
    ],
)
def test_verify_sha_rejects_malformed_sum(payload, tmp_path, bad_sum):
    proc = run_verify_sha(tmp_path, payload, bad_sum)
    assert proc.returncode != 0, proc.stdout
    assert "не задана/некорректна" in proc.stderr


def test_verify_sha_rejects_substituted_file(payload, tmp_path):
    expected = sha256_of(payload)
    payload.write_bytes(b"substituted content\n")
    proc = run_verify_sha(tmp_path, payload, expected)
    assert proc.returncode != 0
    assert "не совпала" in proc.stderr


def test_verify_sha_rejects_missing_file(tmp_path):
    proc = run_verify_sha(tmp_path, tmp_path / "absent.bin", "0" * 64)
    assert proc.returncode != 0
    assert "не совпала" in proc.stderr


def test_verify_sha_allow_unverified_warns_and_continues(payload, tmp_path):
    proc = run_verify_sha(tmp_path, payload, "", allow_unverified=1)
    assert proc.returncode == 0, proc.stderr
    assert "warn" in proc.stderr
    assert "НЕ проверена" in proc.stderr


def test_verify_sha_requires_full_length_and_strict_equality():
    body = extract_function("verify_sha")
    # Ровно 64 hex в нижнем регистре, а не «префикс не короче 32».
    assert r"^[0-9a-f]{64}$" in body
    # Строгое равенство: никаких case/prefix-сравнений.
    assert '[ "$actual" = "$sum" ]' in body
    assert '"$sum"*' not in body
    assert not re.search(r'case "\$actual" in', body)
    # actual приводится к нижнему регистру.
    assert 'actual=${actual,,}' in body


# ------------------------------- Mutable-источники -------------------------------


def test_no_mutable_release_sources():
    # Никаких «последний релиз»/GitHub API: версии и суммы только из констант.
    assert "releases/latest" not in TEXT
    assert "api.github.com" not in TEXT
    assert "/latest/download" not in TEXT


def test_pinned_constants_are_lowercase_sha256():
    for name in SHA_CONSTANTS:
        value = find_constant(name)
        assert value is not None, f"нет константы {name}"
        assert re.fullmatch(r"[0-9a-f]{64}", value), f"{name}={value}"


def test_pinned_versions_and_sums_match_expected():
    for name, expected in EXPECTED_CONSTANTS.items():
        assert find_constant(name) == expected, f"{name} изменена без обновления теста"


def test_zapret_pinned_commit_is_full_sha():
    value = find_constant("ZAPRET_PINNED_COMMIT")
    assert re.fullmatch(r"[0-9a-f]{40}", value)


# ----------------------------------- mihomo ------------------------------------


def test_mihomo_version_is_pinned_not_latest():
    assert 'VER="$MIHOMO_PINNED_VER"' in TEXT
    # Выбор суммы по имени ассета и отказ для неизвестной архитектуры.
    assert "MIHOMO_SHA256_PIN" in TEXT
    assert 'amd64-compatible) MIHOMO_SHA256_PIN="$MIHOMO_SHA256_AMD64_COMPAT"' in TEXT
    assert 'amd64)            MIHOMO_SHA256_PIN="$MIHOMO_SHA256_AMD64"' in TEXT
    assert 'arm64)            MIHOMO_SHA256_PIN="$MIHOMO_SHA256_ARM64"' in TEXT
    assert "неизвестная архитектура" in TEXT
    # Старый env-override и скачивание .sha256 убраны.
    assert "${MIHOMO_SHA256:-" not in TEXT
    assert "fetch_sha" not in TEXT


# ---------------------------------- zashboard ----------------------------------


def test_zashboard_url_uses_pinned_version():
    assert (
        'ZASH_URL="https://github.com/Zephyruso/zashboard/releases/download/'
        '$ZASHBOARD_VER/dist.zip"' in TEXT
    )


def test_zashboard_archive_is_checked_before_unpacking():
    block = TEXT.split('info "Загрузка zashboard', 1)[1]
    verify_at = block.index("verify_sha")
    unzip_at = block.index("unzip -q")
    assert verify_at < unzip_at, "архив распаковывается до проверки суммы"
    # Path traversal, симлинки и setuid отклоняются.
    assert "unzip -Z1" in block
    assert "(^|/)\\.\\.(/|$)" in block
    assert '-type l' in block
    assert "-perm -4000" in block
    # Копирование без прав из архива и атомарная замена каталога.
    assert "--no-preserve=mode,ownership,timestamps,links" in block
    assert ".zashboard.new" in block


def test_zashboard_has_no_env_sum_override():
    assert "${ZASHBOARD_SHA256:-" not in TEXT


# ------------------------------------ WDTT -------------------------------------


def test_wdtt_uses_pinned_asset_and_sum():
    assert 'tag="$WDTT_PINNED_TAG"' in TEXT
    assert 'name="$WDTT_PINNED_NAME"' in TEXT
    assert '"$actual" != "$WDTT_SHA256"' in TEXT
    # Ветка «API недоступен → fallback» больше не нужна.
    assert "WDTT_FALLBACK" not in TEXT


# ----------------------------------- zapret ------------------------------------


def test_zapret_ref_defaults_to_pinned_commit_and_head_is_verified():
    assert 'ZAPRET_REF="${ZAPRET_REF:-$ZAPRET_PINNED_COMMIT}"' in TEXT
    assert 'git -C "$ZAPRET_DIR" rev-parse HEAD' in TEXT
    assert '[ "$ZAPRET_ACTUAL" = "$ZAPRET_REF" ]' in TEXT
    assert "не совпадает с закреплённым коммитом" in TEXT
    # allow-unverified не должен фигурировать в проверке коммита.
    zapret_block = TEXT.split("# ---------- 7. zapret", 1)[1].split(
        "if [ -x \"$ZAPRET_DIR/bin/nfqws\" ]", 1
    )[0]
    assert "ALLOW_UNVERIFIED" not in zapret_block


# ------------------------------ --allow-unverified ------------------------------


def test_allow_unverified_documented_and_parsed():
    usage = re.search(
        r"^usage\(\) \{\n.*?^\}\n", TEXT, re.MULTILINE | re.DOTALL
    )
    assert usage, "не найдена usage()"
    assert "--allow-unverified" in usage.group(0)
    assert "--allow-unverified) ALLOW_UNVERIFIED=1" in TEXT
    assert "ALLOW_UNVERIFIED" in TEXT


# ------------------------------- синтаксис/линт --------------------------------


def test_bash_syntax_ok():
    proc = subprocess.run(
        ["bash", "-n", str(INSTALL)], check=False, text=True, capture_output=True
    )
    assert proc.returncode == 0, proc.stderr


def test_shellcheck_clean():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck не установлен")
    proc = subprocess.run(
        [shellcheck, "-x", str(INSTALL)],
        check=False,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
