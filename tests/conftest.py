"""Общие фикстуры тестов Routeweave.

Тесты самодостаточны: каждый сам готовит временные файлы и окружение и не
требует этих фикстур. ``repo_root`` — удобный путь к корню репозитория для
будущих общих тестов; авто-скипов здесь намеренно нет.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Корень репозитория (каталог с install.sh, panel/ и templates/)."""
    return ROOT
