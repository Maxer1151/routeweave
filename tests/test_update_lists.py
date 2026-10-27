"""Безопасность routeweave-update-lists.sh.

Скрипт работает от root, но каталог /etc/routeweave/lists принадлежит
routeweave. Тесты проверяют, что подмена файлов списков симлинками не даёт
писать по ссылке (ни через основной сценарий, ни через put()), а .env
разбирается без выполнения команд из значений.

Сеть не используется: сценарии с симлинками завершаются до загрузки,
put()/env_get() проверяются через ROUTEWEAVE_LIB_ONLY=1, а полный прогон
идёт с заглушками curl/jq из PATH.
"""

import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "templates" / "routeweave-update-lists.sh"

BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/tmp",
}


def run_script(etc: Path, **extra_env) -> subprocess.CompletedProcess:
    env = dict(BASE_ENV)
    env["ROUTEWEAVE_ETC"] = str(etc)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def run_lib(etc: Path, code: str, **extra_env) -> subprocess.CompletedProcess:
    """Source-нуть функции скрипта и выполнить code в том же bash."""
    env = dict(BASE_ENV)
    env["ROUTEWEAVE_ETC"] = str(etc)
    env["ROUTEWEAVE_LIB_ONLY"] = "1"
    env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"\n{code}\n'],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def make_lists(tmp_path: Path) -> tuple[Path, Path]:
    etc = tmp_path / "etc"
    lists = etc / "lists"
    lists.mkdir(parents=True)
    return etc, lists


def make_victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    victim.chmod(0o600)
    return victim


def assert_victim_intact(victim: Path) -> None:
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600


def test_ensure_custom_files_replaces_symlink_dest(tmp_path):
    etc, lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (lists / "geo-custom.txt").symlink_to(victim)
    (lists / "ru-direct-custom.txt").symlink_to(victim)

    proc = run_lib(etc, 'ensure_custom_files && echo "rc=0" || echo "rc=$?"')

    assert proc.returncode == 0, proc.stderr
    assert "rc=0" in proc.stdout
    assert_victim_intact(victim)
    for name in ("geo-custom.txt", "ru-direct-custom.txt"):
        dest = lists / name
        assert not dest.is_symlink(), name
        assert dest.is_file(), name
        assert dest.read_text(encoding="utf-8") == "", name
        assert stat.S_IMODE(dest.stat().st_mode) == 0o644, name
    assert list((etc / ".staging").iterdir()) == []


def test_ensure_custom_files_keeps_existing(tmp_path):
    etc, lists = make_lists(tmp_path)
    (lists / "geo-custom.txt").write_text("keep.example\n", encoding="utf-8")

    proc = run_lib(etc, 'ensure_custom_files && echo "rc=0"')

    assert proc.returncode == 0, proc.stderr
    assert (lists / "geo-custom.txt").read_text(encoding="utf-8") == "keep.example\n"
    assert (lists / "ru-direct-custom.txt").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("env_line", "expected"),
    [
        ("MIHOMO_API='http://10.0.0.5:9090'\n", "http://10.0.0.5:9090"),
        ("MIHOMO_API='http://10.0.0.5:9090/'\n", "http://10.0.0.5:9090"),
        ("MIHOMO_API=\"\"\n", "http://127.0.0.1:9090"),
        ("", "http://127.0.0.1:9090"),
    ],
)
def test_mihomo_api(tmp_path, env_line, expected):
    etc = tmp_path / "etc"
    etc.mkdir()
    if env_line:
        (etc / ".env").write_text(env_line, encoding="utf-8")

    proc = run_lib(etc, "mihomo_api; echo")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_geo_symlink_refused_before_any_write(tmp_path):
    etc, lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (lists / "geo.txt").symlink_to(victim)

    proc = run_script(etc)

    assert proc.returncode != 0, proc.stdout
    assert "симлинк" in proc.stdout
    assert_victim_intact(victim)
    # Проверки идут до создания custom: ничего в каталоге не появилось.
    assert not (lists / "geo-custom.txt").exists()
    assert not (lists / "ru-direct-custom.txt").exists()


def test_geo_custom_symlink_refused(tmp_path):
    etc, lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (lists / "geo.txt").write_text("+example.com\n", encoding="utf-8")
    (lists / "geo-custom.txt").symlink_to(victim)

    proc = run_script(etc)

    assert proc.returncode != 0, proc.stdout
    assert "симлинк" in proc.stdout
    assert_victim_intact(victim)


def test_dpi_symlink_refused(tmp_path):
    etc, lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (lists / "dpi.txt").symlink_to(victim)

    proc = run_script(etc)

    assert proc.returncode != 0, proc.stdout
    assert "симлинк" in proc.stdout
    assert_victim_intact(victim)


def test_ru_direct_symlink_refused(tmp_path):
    etc, lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (lists / "ru-direct.txt").symlink_to(victim)

    proc = run_script(etc)

    assert proc.returncode != 0, proc.stdout
    assert "симлинк" in proc.stdout
    assert_victim_intact(victim)


def test_lists_dir_symlink_refused(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    real = tmp_path / "real-lists"
    real.mkdir()
    victim = make_victim(tmp_path)
    (real / "geo.txt").symlink_to(victim)
    (etc / "lists").symlink_to(real)

    proc = run_script(etc)

    assert proc.returncode != 0, proc.stdout
    assert "симлинк" in proc.stdout
    assert_victim_intact(victim)


def test_put_replaces_symlink_without_following(tmp_path):
    etc, lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (lists / "geo.txt").symlink_to(victim)
    new = etc / "new.txt"
    new.write_text("+.new.example\n", encoding="utf-8")
    new.chmod(0o600)

    proc = run_lib(
        etc,
        f'put "{new}" "$L/geo.txt" geo && echo "rc=0" || echo "rc=$?"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "rc=0" in proc.stdout, proc.stdout
    assert_victim_intact(victim)
    dest = lists / "geo.txt"
    assert not dest.is_symlink()
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "+.new.example\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644
    # Временные файлы staging не остаются.
    assert list((etc / ".staging").iterdir()) == []


def test_put_same_content_keeps_file(tmp_path):
    etc, lists = make_lists(tmp_path)
    dest = lists / "geo.txt"
    dest.write_text("+.same.example\n", encoding="utf-8")
    new = etc / "new.txt"
    new.write_text("+.same.example\n", encoding="utf-8")

    proc = run_lib(
        etc,
        f'put "{new}" "$L/geo.txt" geo && echo "rc=0" || echo "rc=$?"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "rc=1" in proc.stdout, proc.stdout
    assert "SAME geo" in proc.stdout


def test_env_get_does_not_execute_command_substitution(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    marker = tmp_path / "id-marker"
    value = f"$(id > {marker})"
    (etc / ".env").write_text(f"SOCKS_URL='{value}'\n", encoding="utf-8")

    proc = run_lib(etc, "env_get SOCKS_URL; echo")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == value
    assert not marker.exists(), "значение .env выполнилось как команда"


def test_env_get_keeps_socks_literal(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / ".env").write_text(
        "SOCKS_URL='socks5://u:$(id)@h:1080'\n", encoding="utf-8"
    )

    proc = run_lib(etc, "env_get SOCKS_URL; echo")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "socks5://u:$(id)@h:1080"


def test_env_get_quotes(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / ".env").write_text(
        "A=\"double\"\n"
        "B='single'\n"
        "C='it'\\''s'\n"
        "D=plain\n",
        encoding="utf-8",
    )

    proc = run_lib(etc, "for k in A B C D; do printf '[%s]\\n' \"$(env_get $k)\"; done")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["[double]", "[single]", "[it's]", "[plain]"]


def test_env_get_missing_is_empty_and_not_exported(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / ".env").write_text("SOCKS_URL='socks5://h:1080'\n", encoding="utf-8")

    proc = run_lib(
        etc,
        'v=$(env_get SOCKS_URL); printf "v=%s\\n" "$v"; '
        'if [ -n "${SOCKS_URL+x}" ]; then echo leaked; else echo not-exported; fi; '
        'printf "miss=[%s]\\n" "$(env_get NO_SUCH_KEY)"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "v=socks5://h:1080" in proc.stdout
    assert "not-exported" in proc.stdout
    assert "miss=[]" in proc.stdout


def test_env_get_without_env_file_is_empty(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()

    proc = run_lib(etc, 'printf "[%s]\\n" "$(env_get SOCKS_URL)"')

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_staging_created_private(tmp_path):
    etc, _lists = make_lists(tmp_path)
    victim = make_victim(tmp_path)
    (_lists / "geo.txt").symlink_to(victim)

    run_script(etc)

    staging = etc / ".staging"
    assert staging.is_dir()
    assert not staging.is_symlink()
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700


def test_staging_symlink_refused(tmp_path):
    etc, _lists = make_lists(tmp_path)
    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir()
    victim_dir.chmod(0o755)
    (etc / ".staging").symlink_to(victim_dir)

    proc = run_script(etc)

    assert proc.returncode != 0, proc.stdout
    assert "симлинк" in proc.stdout
    assert stat.S_IMODE(victim_dir.stat().st_mode) == 0o755


FAKE_CURL = """#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) shift 2 ;;
    -K) shift 2 ;;
    --connect-timeout|--max-time|-X) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
cat >/dev/null
case "$url" in
  http://10.0.0.5:9090/providers/rules*) printf '204' ;;
  *providers/rules*) exit 22 ;;
  *amnezia*)
    [ "${FAIL_RU:-0}" = 1 ] && exit 22
    cat "$FIXTURES/amnezia.json" > "$out" ;;
  *geoblock*)
    [ "${FAIL_GEO:-0}" = 1 ] && exit 22
    cat "$FIXTURES/geoblock.lst" > "$out" ;;
  *empty*) : > "$out" ;;
  *) exit 22 ;;
esac
"""

FAKE_JQ = """#!/bin/sh
if [ "$1" = "-e" ]; then exit 0; fi
if [ "$1" = "-r" ]; then
  shift 2
  sed -n 's/.*"hostname":[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$1"
  exit 0
fi
exit 2
"""


def install_tools(tmp_path: Path) -> Path:
    """curl/jq/systemctl-заглушки в отдельном каталоге для PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (
        ("curl", FAKE_CURL),
        ("jq", FAKE_JQ),
        ("systemctl", "#!/bin/sh\nexit 0\n"),
    ):
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def make_fixtures(tmp_path: Path, ru_hosts, geo_hosts) -> Path:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fixtures.joinpath("amnezia.json").write_text(
        "[\n"
        + ",\n".join(f'{{"hostname": "{h}"}}' for h in ru_hosts)
        + "\n]\n",
        encoding="utf-8",
    )
    fixtures.joinpath("geoblock.lst").write_text(
        "\n".join(geo_hosts) + "\n", encoding="utf-8"
    )
    return fixtures


def expected_list(hosts) -> str:
    """Что norm() + префикс '+.' дают из подготовленных хостов."""
    return "".join(f"+.{h}\n" for h in sorted(set(hosts)))


def test_full_run_with_fake_network(tmp_path):
    """Основной сценарий целиком: custom, put, staging и reload — без сети."""
    etc, lists = make_lists(tmp_path)
    (lists / "dpi.txt").write_text("dpi.example\n", encoding="utf-8")
    (etc / ".env").write_text(
        "MIHOMO_API='http://10.0.0.5:9090/'\n", encoding="utf-8"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    hosts = [f"h{i}.site{i % 30}.com" for i in range(1200)]
    fixtures.joinpath("amnezia.json").write_text(
        "[\n"
        + ",\n".join(f'{{"hostname": "{h}"}}' for h in hosts)
        + "\n]\n",
        encoding="utf-8",
    )
    fixtures.joinpath("geoblock.lst").write_text(
        "\n".join(f"g{i}.blocked{i % 10}.net" for i in range(80)) + "\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (
        ("curl", FAKE_CURL),
        ("jq", FAKE_JQ),
        ("systemctl", "#!/bin/sh\nexit 0\n"),
    ):
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    proc = run_script(
        etc,
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        FIXTURES=str(fixtures),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    geo = lists / "geo.txt"
    ru = lists / "ru-direct.txt"
    assert geo.read_text(encoding="utf-8").splitlines() == sorted(
        f"+.g{i}.blocked{i % 10}.net" for i in range(80)
    )
    assert len(ru.read_text(encoding="utf-8").splitlines()) == 1200
    assert stat.S_IMODE(geo.stat().st_mode) == 0o644
    # custom — пользовательский: пустой, без копии geo.txt/ru-direct.txt.
    assert (lists / "geo-custom.txt").read_text(encoding="utf-8") == ""
    assert (lists / "ru-direct-custom.txt").read_text(encoding="utf-8") == ""
    assert list((etc / ".staging").iterdir()) == []
    assert "reload geo-domains -> 204" in proc.stdout


def test_no_secret_in_argv():
    text = SCRIPT.read_text(encoding="utf-8")
    # Секреты и прокси передаются только через stdin-конфиг curl.
    assert "-K -" in text
    assert '"--proxy"' not in text
    assert "--proxy \"$P\"" not in text
    assert '-H "Authorization' not in text


# --- Отказы сети не должны выглядеть как «Изменений нет» -------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/x",  # curl exit 22, файла нет
        "https://example.invalid/empty",  # curl exit 0, файл пустой
    ],
)
def test_fetch_returns_error_on_failed_or_empty_download(tmp_path, url):
    """Регрессия: при пустом SOCKS_URL fetch() раньше возвращал 0."""
    etc = tmp_path / "etc"
    etc.mkdir()
    bin_dir = install_tools(tmp_path)
    out = tmp_path / "out.txt"

    proc = run_lib(
        etc,
        f'set +e\nfetch "{url}" "{out}"\necho "rc=$?"\n',
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
    )

    assert proc.returncode == 0, proc.stderr
    assert "rc=1" in proc.stdout, proc.stdout
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_run_fails_when_both_sources_unreachable(tmp_path):
    """Полный отказ сети — ненулевой код, без «Изменений нет» и без reload."""
    etc, lists = make_lists(tmp_path)
    bin_dir = install_tools(tmp_path)
    fixtures = make_fixtures(
        tmp_path,
        ru_hosts=[f"h{i}.ru{i % 5}.example" for i in range(1200)],
        geo_hosts=[f"g{i}.blocked{i % 7}.example" for i in range(80)],
    )

    proc = run_script(
        etc,
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        FIXTURES=str(fixtures),
        FAIL_RU="1",
        FAIL_GEO="1",
    )

    assert proc.returncode != 0, proc.stdout
    assert "ОШИБКА" in proc.stdout, proc.stdout
    assert "Изменений нет" not in proc.stdout, proc.stdout
    assert "ОШИБКА: ru-direct: fetch/json" in proc.stdout
    assert "ОШИБКА: geo: fetch" in proc.stdout
    assert "reload geo-domains" not in proc.stdout
    assert not (lists / "ru-direct.txt").exists()
    assert not (lists / "geo.txt").exists()


def test_run_fails_but_reloads_successful_source(tmp_path):
    """ru-direct отказал, geo обновился: файл записан, reload есть, код 1."""
    etc, lists = make_lists(tmp_path)
    bin_dir = install_tools(tmp_path)
    geo_hosts = [f"g{i}.blocked{i % 7}.example" for i in range(80)]
    fixtures = make_fixtures(
        tmp_path,
        ru_hosts=[f"h{i}.ru{i % 5}.example" for i in range(1200)],
        geo_hosts=geo_hosts,
    )
    (etc / ".env").write_text(
        "MIHOMO_API='http://10.0.0.5:9090/'\n", encoding="utf-8"
    )

    proc = run_script(
        etc,
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        FIXTURES=str(fixtures),
        FAIL_RU="1",
    )

    assert proc.returncode != 0, proc.stdout
    assert "ОШИБКА: ru-direct: fetch/json" in proc.stdout
    assert "ОШИБКА: источники с ошибкой: ru-direct" in proc.stdout
    # Успешно обновлённый geo всё равно попал в файл и в mihomo.
    assert (lists / "geo.txt").read_text(encoding="utf-8") == expected_list(
        geo_hosts
    )
    assert "reload geo-domains -> 204" in proc.stdout
    assert not (lists / "ru-direct.txt").exists()


def test_run_reports_no_changes_when_both_same(tmp_path):
    """Оба источника SAME, ошибок нет: код 0 и «Изменений нет» без reload."""
    etc, lists = make_lists(tmp_path)
    bin_dir = install_tools(tmp_path)
    ru_hosts = [f"h{i}.ru{i % 5}.example" for i in range(1200)]
    geo_hosts = [f"g{i}.blocked{i % 7}.example" for i in range(80)]
    fixtures = make_fixtures(tmp_path, ru_hosts=ru_hosts, geo_hosts=geo_hosts)
    (lists / "dpi.txt").write_text("", encoding="utf-8")
    (lists / "ru-direct.txt").write_text(expected_list(ru_hosts), encoding="utf-8")
    (lists / "geo.txt").write_text(expected_list(geo_hosts), encoding="utf-8")

    proc = run_script(
        etc,
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        FIXTURES=str(fixtures),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Изменений нет" in proc.stdout
    assert "SAME ru-direct" in proc.stdout
    assert "SAME geo" in proc.stdout
    assert "reload geo-domains" not in proc.stdout
    assert (lists / "ru-direct.txt").read_text(encoding="utf-8") == expected_list(
        ru_hosts
    )


def test_run_fails_when_ru_below_min_count(tmp_path):
    """Мало хостов в ru-direct — отказ, но geo успевает обновиться."""
    etc, lists = make_lists(tmp_path)
    bin_dir = install_tools(tmp_path)
    geo_hosts = [f"g{i}.blocked{i % 7}.example" for i in range(80)]
    fixtures = make_fixtures(
        tmp_path,
        ru_hosts=["a.example", "b.example", "c.example"],
        geo_hosts=geo_hosts,
    )
    (etc / ".env").write_text(
        "MIHOMO_API='http://10.0.0.5:9090/'\n", encoding="utf-8"
    )

    proc = run_script(
        etc,
        PATH=f"{bin_dir}:{BASE_ENV['PATH']}",
        FIXTURES=str(fixtures),
    )

    assert proc.returncode != 0, proc.stdout
    assert "ОШИБКА: ru-direct: 3 < 1000" in proc.stdout
    assert "Изменений нет" not in proc.stdout
    assert (lists / "geo.txt").read_text(encoding="utf-8") == expected_list(
        geo_hosts
    )
    assert not (lists / "ru-direct.txt").exists()
