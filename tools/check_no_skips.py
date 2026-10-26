#!/usr/bin/env python3
"""Запрет «тихих» skip в выводе pytest.

Скрипт читает лог pytest, запущенного с ``-rs`` (короткий отчёт о причинах
skip), и ищет строки вида::

    SKIPPED [1] tests/test_firewall.py:21: нужен root: netns и iptables

В привилегированном CI-job такие тесты обязаны выполняться, поэтому любой
найденный пропуск считается ошибкой. Исключения задаются явно и по одной
причине за раз через ``--allow`` (по умолчанию список пуст).

Коды возврата:
  0 — пропусков нет либо все они покрыты ``--allow``;
  1 — найден хотя бы один недопустимый пропуск;
  2 — ошибка аргументов или чтения лога.

Примеры::

    python tools/check_no_skips.py pytest-netns.log
    python -m pytest tests -q -rs | python tools/check_no_skips.py -
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

# Строка короткого отчёта pytest: "SKIPPED [N] <файл>:<строка>: <причина>".
# Причина может содержать двоеточия, поэтому её не разбираем, а храним целиком.
SKIP_RE = re.compile(r"^\s*SKIPPED\s*\[\d+\]\s*(?P<detail>.*)$")

EXAMPLES = """\
примеры:
  python tools/check_no_skips.py pytest-netns.log
  python -m pytest tests -q -rs | python tools/check_no_skips.py -
  python tools/check_no_skips.py pytest-netns.log --allow "нет ip из iproute2"
"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_no_skips.py",
        description=(
            "Падает (код 1), если в выводе pytest -rs есть строки 'SKIPPED [' — "
            "то есть тесты не выполнились, а были пропущены."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "log",
        nargs="?",
        default="-",
        metavar="LOG",
        help="файл с выводом pytest -rs; '-' (по умолчанию) — читать stdin",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="SUBSTRING",
        help=(
            "подстрока причины, при которой пропуск допустим; можно указывать "
            "несколько раз. По умолчанию не разрешено ничего"
        ),
    )
    return parser.parse_args(argv)


def read_log(path: str) -> list[str]:
    """Вернуть строки лога; код 2, если файл не читается."""
    if path == "-":
        return sys.stdin.read().splitlines()
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError as exc:
        print(f"check_no_skips: не читается {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)


def find_skips(lines: Sequence[str]) -> list[str]:
    """Выбрать из лога строки отчёта о пропусках (без ведущих пробелов)."""
    return [line.strip() for line in lines if SKIP_RE.match(line)]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lines = read_log(args.log)
    skips = find_skips(lines)

    allowed = [skip for skip in skips if any(p in skip for p in args.allow)]
    blocked = [skip for skip in skips if skip not in allowed]

    if allowed:
        print(f"check_no_skips: разрешено через --allow: {len(allowed)}")
        for skip in allowed:
            print(f"  allow: {skip}")
        sys.stdout.flush()

    if blocked:
        print(
            f"check_no_skips: недопустимых пропусков: {len(blocked)}",
            file=sys.stderr,
        )
        for skip in blocked:
            print(f"  {skip}", file=sys.stderr)
        print(
            "check_no_skips: тесты пропущены вместо выполнения — job падает",
            file=sys.stderr,
        )
        return 1

    print(f"check_no_skips: пропусков нет (строк в логе: {len(lines)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
