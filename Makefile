# Routeweave — линт, тесты и обслуживание репозитория.
#
# Зависимости для `make test` (ставьте в venv, чтобы не трогать систему):
#   python3 -m venv .venv && . .venv/bin/activate
#   pip install -r panel/requirements.txt pytest pyyaml
# Для `make lint` нужны shellcheck и ruff: они автоматически ищутся в PATH,
# затем в репозиторном .venv/bin. Если инструмента нет нигде, lint падает с
# подсказкой. Ручное переопределение путей:
#   make lint SHELLCHECK=/path/to/shellcheck RUFF=/path/to/ruff

SHELLCHECK ?= $(or $(shell command -v shellcheck 2>/dev/null),$(wildcard .venv/bin/shellcheck))
RUFF ?= $(or $(shell command -v ruff 2>/dev/null),$(wildcard .venv/bin/ruff))
PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest

SHELL_SCRIPTS := install.sh uninstall.sh $(wildcard templates/*.sh)

.DEFAULT_GOAL := all
.PHONY: all lint test clean help

all: lint test

lint:
	@if [ -z "$(SHELLCHECK)" ] || ! command -v "$(SHELLCHECK)" >/dev/null 2>&1; then \
		echo "make lint: shellcheck не найден ни в PATH, ни в .venv/bin — установите его или задайте SHELLCHECK=/путь/к/shellcheck" >&2; \
		exit 1; \
	fi
	@if [ -z "$(RUFF)" ] || ! command -v "$(RUFF)" >/dev/null 2>&1; then \
		echo "make lint: ruff не найден ни в PATH, ни в .venv/bin — установите его или задайте RUFF=/путь/к/ruff" >&2; \
		exit 1; \
	fi
	bash -n $(SHELL_SCRIPTS)
	bash -O extglob -n $(SHELL_SCRIPTS)
	$(SHELLCHECK) -x install.sh uninstall.sh templates/*.sh
	$(RUFF) check panel templates tests
	$(PYTHON) -m compileall -q panel templates tests

test:
	$(PYTEST) tests -v

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache

help:
	@echo "Routeweave: доступные цели"
	@echo "  make lint   — bash -n, shellcheck, ruff, compileall"
	@echo "  make test   — pytest tests -v"
	@echo "  make all    — lint + test (цель по умолчанию)"
	@echo "  make clean  — удалить __pycache__, .pytest_cache, .ruff_cache"
