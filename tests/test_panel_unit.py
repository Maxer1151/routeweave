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
