"""Регресс-тест юнита панели: bounding set не должен быть пустым.

Пустой ``CapabilityBoundingSet`` выглядит безопаснее, но ломает sudo:
``/usr/bin/sudo`` — setuid-бинарник, и при exec ядро поднимает его
capabilities только в пределах bounding set. С пустым набором ``sudo -n``
падает («unable to set groups» / «could not restore to saved user id»,
особенно в sudo-rs), и все кнопки панели — рестарты сервисов, обновление
списков, состояние tg-ws-proxy — перестают работать. Тест фиксирует
непустой минимальный набор и остальные обязательные параметры юнита.
"""

import configparser
from pathlib import Path

UNIT = Path(__file__).resolve().parent.parent / "templates" / "routeweave-panel.service"


def load_unit():
    """Разбирает unit-файл как INI: секции и ключи без интерполяции."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(UNIT, encoding="utf-8")
    return parser


def service_value(key):
    """Возвращает значение ключа из ``[Service]`` без окружающих пробелов."""
    return load_unit()["Service"][key].strip()


def capabilities():
    """Множество capability из ``CapabilityBoundingSet``."""
    return set(service_value("CapabilityBoundingSet").split())


def test_panel_runs_as_unprivileged_user():
    """Панель работает от routeweave, а не от root."""
    assert service_value("User") == "routeweave"
    assert service_value("Group") == "routeweave"


def test_capability_bounding_set_is_not_empty():
    """Пустой набор — регресс: sudo не сможет повысить привилегии."""
    caps = capabilities()
    assert caps, "CapabilityBoundingSet пуст: setuid sudo не получит capability"
    assert "CAP_SETUID" in caps
    assert "CAP_SETGID" in caps


def test_capability_bounding_set_has_no_broad_caps():
    """Широкие capability панели не нужны и расширяют ущерб при RCE."""
    caps = capabilities()
    assert "CAP_SYS_ADMIN" not in caps
    assert "CAP_NET_ADMIN" not in caps


def test_sudo_and_filesystem_restrictions():
    """NoNewPrivileges=no нужен sudo, ProtectSystem=strict — остальная защита."""
    assert service_value("NoNewPrivileges") == "no"
    assert service_value("ProtectSystem") == "strict"


def test_lists_directory_is_writable():
    """Панель правит списки, поэтому каталог должен быть в ReadWritePaths."""
    paths = service_value("ReadWritePaths").split()
    assert "/etc/routeweave/lists" in paths


def test_diagnostics_directory_is_writable():
    """Снимок пишет root-helper через sudo в namespace панели.

    Без /var/log/routeweave в ReadWritePaths ProtectSystem=strict не даст
    записать снимок. Префикс "-" допустим: на старых установках каталога
    может не быть, и юнит не должен из-за этого не стартовать.
    """
    paths = service_value("ReadWritePaths").split()
    assert "/var/log/routeweave" in paths or "-/var/log/routeweave" in paths


def test_exec_start_has_no_wildcard_host_and_single_worker():
    """0.0.0.0 в ExecStart — регресс: адрес задаёт PANEL_BIND.

    --workers не поддерживается: JOB и кэш /api/status живут в памяти
    процесса, второй воркер отвечал бы чужим состоянием.
    """
    exec_start = service_value("ExecStart")
    assert "0.0.0.0" not in exec_start
    assert "--host" not in exec_start
    assert "--workers" not in exec_start
    assert "app.py" in exec_start
    assert "uvicorn" not in exec_start


def test_service_resource_limits():
    """MemoryMax/TasksMax ограничивают утечки памяти и потоки."""
    assert service_value("MemoryMax") == "256M"
    assert service_value("TasksMax") == "64"


def test_memory_and_tasks_limits_anywhere_in_unit():
    text = UNIT.read_text(encoding="utf-8")
    for line in text.splitlines():
        assert "--workers" not in line
        assert "0.0.0.0" not in line
