#!/usr/bin/env python3
"""Routeweave веб-панель: статусы сервисов, пробы путей, редактор списков, рестарты.

Приложение работает от непривилегированного пользователя ``routeweave``.
Все root-операции выполняются только через ``sudo -n`` по allowlist из
``/etc/sudoers.d/routeweave-panel``: ``systemctl restart`` для mihomo,
zapret-nfqws и самой панели, запуск routeweave-lists-update.service и
helper-скрипты в ``/usr/local/sbin``. Прямых обращений к Docker-сокету и
запусков systemctl/docker без sudo в коде нет.

Каталог конфигурации задаётся переменной окружения ``ROUTEWEAVE_ETC``
(по умолчанию системный каталог routeweave). Доступ только по
Basic-авторизации (PANEL_USER / PANEL_PASS).
"""

import base64
import copy
import json
import logging
import os
import re
import secrets
import socket
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("routeweave")

# ---------------- Пути и константы ----------------

ETC = Path(os.environ.get("ROUTEWEAVE_ETC", "/etc/routeweave"))
ENV_FILE = ETC / ".env"
LISTS_DIR = ETC / "lists"
STATIC_DIR = Path(__file__).resolve().parent / "static"

LISTS = {
    "geo-custom": "geo-custom.txt",
    "dpi": "dpi.txt",
    "ru-custom": "ru-direct-custom.txt",
}

UPDATER = "/usr/local/sbin/routeweave-update-lists.sh"
TGWS_RESTART = "/usr/local/sbin/routeweave-tgws-restart.sh"
TGWS_STATE = "/usr/local/sbin/routeweave-tgws-state.sh"
TAILSCALE_STATE = "/usr/local/sbin/routeweave-tailscale-state.sh"

MIXED_PORT = 7890
TGWS_PORT = 1443

MAX_LIST_BYTES = 200_000
DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
DOMAIN_TLD_RE = re.compile(r"[a-z]{2,63}")
DOMAIN_PREFIXES = ("+.", "*.")
SOCKS_SCHEMES = {"socks5", "socks5h", "socks", "http", "https"}

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

# CSP панели: строгий, без inline. frame-ancestors/object-src продублированы
# явно: X-Frame-Options покрывает не все браузеры, а object-src в default-src
# не всегда наследуется плагинами.
PANEL_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)

# Состояние в памяти процесса: юнит запускает ровно один воркер
# (uvicorn без --workers), поэтому JOB, кэш статуса и PROBES_LAST общие для
# всех запросов. Multi-worker не поддерживается: у каждого процесса были бы
# свои JOB и кэш, и /api/jobs/apply отвечал бы случайным состоянием.
PROBES_LOCK = threading.Lock()
PROBES_LAST: dict = {"ts": None, "results": {}}

JOB_LOCK = threading.Lock()
JOB: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "detail": None,
}

# TTL-кэш /api/status: 3 секунды. STATUS_LOCK реализует single-flight —
# параллельные запросы ждут результат первого, а не запускают свои пробы.
STATUS_TTL = 3.0
STATUS_LOCK = threading.Lock()
STATUS_CACHE: dict = {"at": None, "payload": None}

# Ограничение одновременных тяжёлых запросов. Переполнение — 429, а не
# бесконечная очередь: каждый /api/status запускает sudo-helper'ы, /api/probe
# ходит в сеть.
STATUS_SEM = threading.BoundedSemaphore(2)
PROBE_SEM = threading.BoundedSemaphore(1)

# Простой rate limit по IP (X-Forwarded-For не доверяем: панель не стоит за
# прокси, который бы его подчищал). Скользящее окно.
RATE_LOCK = threading.Lock()
RATE_BUCKETS: dict[str, deque] = {}
AUTH_RATE_BUCKETS: dict[str, deque] = {}
RATE_MAX_KEYS = 1024

# Оценка скорости Tailscale: счётчики и время предыдущего опроса /api/status.
# Подмена _tailscale_now в тестах позволяет задать время без sleep.
TAILSCALE_RATE_LOCK = threading.Lock()
TAILSCALE_RATE_STATE: dict = {"rx": None, "tx": None, "at": None}
_tailscale_now = time.monotonic

TAILSCALE_DAEMON_STATES = {"active", "inactive", "failed", "activating", "absent", "unknown"}
TAILSCALE_TYPES = {
    "installed": bool,
    "backend": str,
    "self_ip": str,
    "advertised": bool,
    "approved": bool,
    "iface": str,
    "error": str,
}


# ---------------- Конфигурация ----------------

def _unescape_double(raw: str) -> str:
    """Раскрывает только ``\\"`` → ``"`` и ``\\\\`` → ``\\`` за один проход.

    Остальные последовательности (``\\n``, ``\\t`` и т.п.) остаются как есть:
    .env — не язык программирования, переводы строк в значениях не нужны.
    """
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            nxt = raw[index + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _parse_env_value(value: str, line_number: int) -> str:
    """Разбирает значение .env как одно shell-слово, но без подстановок.

    * ``'...'`` — содержимое дословно, как в install.sh; записанный
      установщиком ``'\\''`` (закрыть, экранированный апостроф, открыть
      заново) снова даёт ``'``;
    * ``"..."`` — кавычки снимаются, раскрываются только ``\\"`` и ``\\\\``;
    * сегменты склеиваются: ``'a'"b"`` → ``ab``;
    * вне кавычек ``\\`` экранирует следующий символ, одиночный ``\\``
      в конце остаётся как есть;
    * незакрытая кавычка — значение как есть плюс предупреждение в лог.
    """
    out: list[str] = []
    index = 0
    segment_start = 0
    state = "plain"
    while index < len(value):
        char = value[index]
        if state == "plain":
            if char in ('"', "'"):
                out.append(value[segment_start:index])
                state = "single" if char == "'" else "double"
                index += 1
                segment_start = index
            elif char == "\\":
                out.append(value[segment_start:index])
                if index + 1 < len(value):
                    out.append(value[index + 1])
                    index += 2
                else:
                    out.append("\\")
                    index += 1
                segment_start = index
            else:
                index += 1
        elif state == "single":
            if char == "'":
                out.append(value[segment_start:index])
                state = "plain"
                index += 1
                segment_start = index
            else:
                index += 1
        else:  # double
            # Экранированный символ не может закрыть кавычку; раскрытие
            # делает _unescape_double уже над готовым сегментом.
            if char == "\\" and index + 1 < len(value):
                index += 2
            elif char == '"':
                out.append(_unescape_double(value[segment_start:index]))
                state = "plain"
                index += 1
                segment_start = index
            else:
                index += 1
    if state != "plain":
        logger.warning("Незакрытая кавычка в %s, строка %s", ENV_FILE, line_number)
        return value
    out.append(value[segment_start:])
    return "".join(out)


def load_env() -> dict:
    """Разбирает .env построчно: KEY=VALUE, строки с # — комментарии.

    Значения в кавычках сохраняются дословно (значимые пробелы и # внутри
    кавычек не теряются), незакавыченные — обрезаются по краям.
    """
    env: dict = {}
    if ENV_FILE.exists():
        for line_number, raw_line in enumerate(ENV_FILE.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = _parse_env_value(value.strip(), line_number)
    return env


def env_value(name: str, default: str = "") -> str:
    """Значение из .env без повторного strip: закавыченные важны дословно."""
    return ENV.get(name) or default


def env_setting(name: str, default: str = "") -> str:
    """Настройка процесса: окружение (systemd/тест) важнее .env."""
    return os.environ.get(name) or env_value(name) or default


ENV = load_env()
PANEL_USER = env_value("PANEL_USER", "admin") or "admin"
PANEL_PASS = env_value("PANEL_PASS")
SERVER_IP = env_value("SERVER_IP")
TG_LINK_IP = env_value("TG_LINK_IP")
TG_SECRET = env_value("TG_SECRET")
MIHOMO_SECRET = env_value("MIHOMO_SECRET")

# PANEL_TLS=yes означает, что панель отдаётся по https (например, через
# локальный reverse-proxy с сертификатом). Реализации TLS здесь нет — флаг
# нужен только для корректного сравнения Origin/Referer в CSRF-проверке.
PANEL_TLS = env_setting("PANEL_TLS").strip().lower() in ("yes", "1", "true", "on")
# Адрес прослушивания: SERVER_IP (панель в домашней сети), иначе loopback.
PANEL_BIND = env_setting("PANEL_BIND") or SERVER_IP or "127.0.0.1"
# Адрес API mihomo. Явная переменная окружения важнее вычисленного значения;
# при заданном SERVER_IP слушаем контроллер на нём, иначе — loopback.
MIHOMO_API = os.environ.get("MIHOMO_API") or (
    f"http://{SERVER_IP}:9090" if SERVER_IP else "http://127.0.0.1:9090"
)


def _parse_rate_limit(raw: str, default_limit: int, default_window: int) -> tuple[int, int]:
    """Разбирает ``N/M`` (N запросов за M секунд) или просто ``N``.

    Формат ``N/M`` пишет install.sh (PANEL_RATE_LIMIT); голое число удобно
    для ручных запусков и тестов. Мусор не роняет панель — берём дефолт.
    """
    value = (raw or "").strip()
    if not value:
        return default_limit, default_window
    limit_part, sep, window_part = value.partition("/")
    try:
        limit = int(limit_part)
        window = int(window_part) if sep else default_window
    except ValueError:
        return default_limit, default_window
    if limit <= 0 or window <= 0:
        return default_limit, default_window
    return limit, window


PANEL_RATE_LIMIT, PANEL_RATE_WINDOW = _parse_rate_limit(
    env_setting("PANEL_RATE_LIMIT"), 120, 60
)
PANEL_AUTH_RATE_LIMIT, PANEL_AUTH_RATE_WINDOW = _parse_rate_limit(
    env_setting("PANEL_AUTH_RATE_LIMIT"), 10, PANEL_RATE_WINDOW
)

# Host/Origin допускаются только из allowlist. Порт любой: панель может
# слушать не 8080, а Origin всё равно должен совпадать по hostname.
HOST_HEADER_RE = re.compile(
    r"^(?:[a-z0-9._-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?$", re.IGNORECASE
)


def normalize_hostname(value: str) -> str | None:
    """hostname из Host/allowlist: нижний регистр, без порта и скобок IPv6."""
    candidate = (value or "").strip()
    if not candidate or not HOST_HEADER_RE.fullmatch(candidate):
        return None
    try:
        hostname = urlsplit("//" + candidate).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


def _allowed_hostnames() -> set[str]:
    allowed = {"localhost", "127.0.0.1", "::1"}
    for raw in (SERVER_IP, *env_setting("PANEL_ALLOWED_HOSTS").split(",")):
        name = normalize_hostname(raw)
        if name is None and ":" in raw:
            # Голый IPv6 из .env: в заголовке Host он приходит в скобках.
            name = normalize_hostname("[" + raw.strip() + "]")
        if name:
            allowed.add(name)
    return allowed


ALLOWED_HOSTS = _allowed_hostnames()

if not PANEL_PASS:
    raise SystemExit(
        f"PANEL_PASS не задан в {ENV_FILE} — панель не запускается. "
        "Задайте пароль и перезапустите routeweave-panel."
    )

if not MIHOMO_SECRET:
    logger.warning("MIHOMO_SECRET пуст: API mihomo без авторизации недоступен")


def _valid_socks_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return parts.scheme in SOCKS_SCHEMES and bool(parts.hostname) and port is not None


if env_value("SOCKS_URL") and not _valid_socks_url(env_value("SOCKS_URL")):
    logger.warning(
        "SOCKS_URL задан, но не разбирается (нужен scheme://host:port) — "
        "гео-домены могут пойти напрямую"
    )


def _ensure_lists_dir() -> None:
    """Создаёт каталог списков; отсутствие прав — предупреждение, не падение."""
    try:
        LISTS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(LISTS_DIR, 0o755)
    except OSError as exc:
        logger.warning("Не удалось подготовить каталог списков %s: %s", LISTS_DIR, exc)


_ensure_lists_dir()


# ---------------- Внешние команды ----------------

def run_cmd(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Единственная точка запуска внешних команд (тесты её подменяют)."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        logger.warning("Команда не найдена: %s (%s)", args[0] if args else "?", exc)
        return subprocess.CompletedProcess(args, 127, "", f"{type(exc).__name__}: {exc}")
    except subprocess.TimeoutExpired:
        logger.warning("Команда не уложилась в %s с: %s", timeout, args)
        return subprocess.CompletedProcess(args, 124, "", "timeout")


def sudo_run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Привилегированная операция строго через sudo -n (allowlist в sudoers)."""
    return run_cmd(["sudo", "-n", *args], timeout=timeout)


def _sudo_error(result: subprocess.CompletedProcess, action: str) -> HTTPException:
    message = (result.stderr or result.stdout or "").strip()[:500]
    if not message:
        message = f"код возврата {result.returncode}"
    return HTTPException(502, f"{action}: {message}")


def svc_active(unit: str) -> str:
    result = run_cmd(["systemctl", "is-active", unit], timeout=5)
    state = (result.stdout or "").strip()
    return state if state in ("active", "inactive", "failed", "activating") else "unknown"


TGWS_STATES = {
    "running",
    "exited",
    "absent",
    "created",
    "restarting",
    "paused",
    "dead",
}
# "Включён" — контейнер существует и не остановлен окончательно. absent/dead/
# exited означают, что прокси не работает и карточку показывать незачем.
TGWS_ENABLED_STATES = {"running", "created", "restarting", "paused"}


def tgws_state() -> str:
    """Статус контейнера tg-ws-proxy через helper (без доступа к docker.sock)."""
    result = sudo_run([TGWS_STATE], timeout=15)
    if result.returncode != 0:
        logger.warning(
            "routeweave-tgws-state.sh вернул код %s: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip()[:200],
        )
        return "unknown"
    state = (result.stdout or "").strip().lower()
    return state if state in TGWS_STATES else "unknown"


def tgws_enabled(state: str | None = None) -> bool:
    """Включён ли tg-ws-proxy; готовое состояние можно передать, не вызывая helper."""
    return (tgws_state() if state is None else state) in TGWS_ENABLED_STATES


def _tailscale_default(error: str = "") -> dict:
    """Безопасный статус Tailscale полной формы (helper недоступен/сломан)."""
    return {
        "installed": False,
        "daemon": "unknown",
        "backend": "",
        "self_ip": "",
        "advertised": False,
        "approved": False,
        "peers": 0,
        "iface": "",
        "rx_bytes": 0,
        "tx_bytes": 0,
        "error": error,
    }


def _tailscale_parse(payload: object) -> dict | None:
    """Строгая валидация ответа helper: поля неверного типа → значения по умолчанию."""
    if not isinstance(payload, dict):
        return None
    state = _tailscale_default()
    wrong: list[str] = []

    for name, expected in TAILSCALE_TYPES.items():
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, expected):
            state[name] = value
        else:
            wrong.append(name)

    daemon = payload.get("daemon")
    if isinstance(daemon, str) and daemon in TAILSCALE_DAEMON_STATES:
        state["daemon"] = daemon
    elif daemon is not None:
        wrong.append("daemon")

    for name in ("peers", "rx_bytes", "tx_bytes"):
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            state[name] = value
        else:
            wrong.append(name)

    if wrong:
        logger.warning("Helper Tailscale вернул поля неверного типа: %s", ", ".join(sorted(wrong)))
    return state


def tailscale_state() -> dict:
    """Статус Tailscale через root-helper: сокет tailscaled панели недоступен."""
    # 25 с: helper делает до трёх внешних вызовов с timeout 5 каждый.
    result = sudo_run([TAILSCALE_STATE], timeout=25)
    if result.returncode != 0:
        # Вывод helper'а не логируем: в нём могут быть имена устройств и
        # адреса tailnet, а лог панели читают не только root.
        logger.warning("routeweave-tailscale-state.sh вернул код %s", result.returncode)
        return _tailscale_default(f"helper вернул код {result.returncode}")
    try:
        payload = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError:
        logger.warning("routeweave-tailscale-state.sh вернул не JSON")
        return _tailscale_default("некорректный ответ helper")
    state = _tailscale_parse(payload)
    if state is None:
        logger.warning("routeweave-tailscale-state.sh вернул JSON не-объект")
        return _tailscale_default("некорректный ответ helper")
    return state


def _tailscale_rates(rx: int, tx: int) -> tuple[int, int]:
    """ОЦЕНКА скорости (байт/с) по дельте счётчиков между опросами панели.

    Это не мгновенная скорость интерфейса: дельта делится на время между
    двумя последовательными вызовами ``/api/status``, поэтому интервал равен
    периоду опроса панели. Первый вызов, сброс счётчиков (новое значение
    меньше предыдущего) и неположительный интервал дают ``(0, 0)``.
    """
    now = _tailscale_now()
    with TAILSCALE_RATE_LOCK:
        prev_rx = TAILSCALE_RATE_STATE["rx"]
        prev_tx = TAILSCALE_RATE_STATE["tx"]
        prev_at = TAILSCALE_RATE_STATE["at"]
        TAILSCALE_RATE_STATE.update({"rx": rx, "tx": tx, "at": now})
    if prev_rx is None or prev_tx is None or prev_at is None:
        return 0, 0
    elapsed = now - prev_at
    if elapsed <= 0 or rx < prev_rx or tx < prev_tx:
        return 0, 0
    return int((rx - prev_rx) / elapsed), int((tx - prev_tx) / elapsed)


def mihomo_api(path: str, method: str = "GET") -> tuple:
    headers = {}
    if MIHOMO_SECRET:
        headers["Authorization"] = "Bearer " + MIHOMO_SECRET
    try:
        with httpx.Client(timeout=4) as client:
            response = client.request(method, MIHOMO_API + path, headers=headers)
            return response.status_code, response.text[:500]
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("mihomo API недоступен (%s): %s", path, exc)
        return 0, f"{type(exc).__name__}: {exc}"


# ---------------- Пробы ----------------

def probe_socks_chatgpt() -> dict:
    socks_url = env_value("SOCKS_URL")
    if not socks_url:
        return {"ok": True, "detail": "SOCKS_URL не задан — гео-домены идут напрямую", "skipped": True}
    try:
        started = time.monotonic()
        with httpx.Client(proxy=f"socks5://127.0.0.1:{MIXED_PORT}", timeout=8, follow_redirects=False) as client:
            response = client.get("https://chatgpt.com")
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": response.status_code < 500, "detail": f"HTTP {response.status_code}, {elapsed} мс (через mihomo→socks)"}
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def probe_socks_exit_ip() -> dict:
    if not env_value("SOCKS_URL"):
        return {"ok": True, "detail": "нет socks — пропущено", "skipped": True}
    try:
        with httpx.Client(proxy=f"socks5://127.0.0.1:{MIXED_PORT}", timeout=8) as client:
            response = client.get("https://api.ipify.org")
        return {"ok": response.status_code == 200, "detail": "exit IP: " + response.text[:64]}
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def probe_youtube_direct() -> dict:
    try:
        started = time.monotonic()
        with httpx.Client(timeout=10) as client:  # без прокси: прямой путь + zapret
            response = client.get("https://www.youtube.com/generate_204", follow_redirects=False)
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "ok": response.status_code in (204, 200, 301, 302),
            "detail": f"HTTP {response.status_code}, {elapsed} мс (direct+zapret)",
        }
    except (httpx.HTTPError, OSError) as exc:
        return {
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc} — zapret не спасает или домен ушёл из списка",
        }


def probe_tg_local() -> dict:
    if not tgws_enabled():
        return {"ok": True, "detail": "tg-ws-proxy не установлен (опциональный компонент)", "skipped": True}
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", TGWS_PORT), timeout=4):
            pass
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": True, "detail": f"tg-ws-proxy слушает :{TGWS_PORT}, TCP ok, {elapsed} мс"}
    except OSError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def probe_worker() -> dict:
    domain = env_value("BASE_DOMAIN")
    if not domain:
        return {"ok": True, "detail": "BASE_DOMAIN не задан — CF Worker не используется", "skipped": True}
    try:
        with httpx.Client(timeout=8) as client:
            response = client.get(f"https://{domain}/", follow_redirects=False)
        return {"ok": response.status_code < 500, "detail": f"HTTP {response.status_code} от https://{domain}/"}
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


PROBES = {
    "chatgpt_via_socks": probe_socks_chatgpt,
    "socks_exit_ip": probe_socks_exit_ip,
    "youtube_direct": probe_youtube_direct,
    "tg_ws_proxy": probe_tg_local,
    "cf_worker": probe_worker,
}


# ---------------- Аутентификация, CSRF, заголовки ----------------

def _check_auth(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, sep, password = raw.partition(":")
    if not sep or not user or not password:
        return False
    user_ok = secrets.compare_digest(user.encode(), PANEL_USER.encode())
    password_ok = secrets.compare_digest(password.encode(), PANEL_PASS.encode())
    return user_ok and password_ok


def _check_csrf(request: Request) -> str | None:
    """Fail-closed проверка источника изменяющего запроса.

    Мутирующие методы (POST/PUT/DELETE/PATCH) допускаются, если:
      * ``Origin`` (или ``Referer``, если Origin нет) совпадает с ожидаемым
        origin панели по (scheme, hostname, port);
      * либо ``Sec-Fetch-Site`` равен ``same-origin``/``none``;
      * либо ни одного из этих заголовков нет, но клиент прислал
        ``X-Requested-With: Routeweave`` (запасной путь для curl и старых
        браузеров; это же значение шлёт panel/static/app.js).
    Если заголовок есть, но не совпадает — 403, без «тихого» пропуска.
    Схема ожидаемого origin — http, пока оператор явно не задал PANEL_TLS=yes;
    X-Forwarded-Proto не доверяем (его подделывает клиент).
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()

    if origin:
        return None if _same_origin(request, origin) else "CSRF: чужой Origin"
    if referer:
        return None if _same_origin(request, referer) else "CSRF: чужой Referer"
    if fetch_site:
        if fetch_site in ("same-origin", "none"):
            return None
        return "CSRF: запрос с чужого сайта"
    if request.headers.get("x-requested-with") == "Routeweave":
        return None
    return "CSRF: нет подтверждения источника запроса"


def _expected_origin(request: Request) -> tuple[str, str, int] | None:
    """(scheme, hostname, port) из проверенного Host и PANEL_TLS."""
    scheme = "https" if PANEL_TLS else "http"
    host = request.headers.get("host") or ""
    try:
        parts = urlsplit("//" + host.strip())
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname.lower(), port


def _parse_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(value.strip())
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if scheme not in ("http", "https") or not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname.lower(), port


def _same_origin(request: Request, value: str) -> bool:
    expected = _expected_origin(request)
    parsed = _parse_origin(value)
    return expected is not None and parsed is not None and expected == parsed


def _host_allowed(host_header: str) -> bool:
    name = normalize_hostname(host_header)
    return name is not None and name in ALLOWED_HOSTS


def _client_ip(request: Request) -> str:
    """IP клиента для rate limit. X-Forwarded-For не доверяем."""
    return request.client.host if request.client and request.client.host else "unknown"


def _evict_oldest_bucket(buckets: dict) -> None:
    """Удаляет окно с самой старой последней активностью (``deque[-1]``)."""
    if not buckets:
        return
    oldest = min(
        buckets,
        key=lambda item: buckets[item][-1] if buckets[item] else float("-inf"),
    )
    buckets.pop(oldest, None)


def _prune_buckets(buckets: dict, now: float, window: int) -> None:
    """Удаляет истёкшие окна и при переполнении вытесняет самые старые.

    Полная очистка запрещена: поток новых ключей не должен обнулять лимиты
    уже отслеживаемых клиентов.
    """
    stale = [key for key, bucket in buckets.items() if not bucket or now - bucket[-1] >= window]
    for key in stale:
        buckets.pop(key, None)
    while len(buckets) > RATE_MAX_KEYS:
        _evict_oldest_bucket(buckets)


def _rate_limited(buckets: dict, key: str, limit: int, window: int) -> bool:
    """True, если лимит запросов key за скользящее окно уже исчерпан."""
    now = time.monotonic()
    with RATE_LOCK:
        if len(buckets) > RATE_MAX_KEYS or (key not in buckets and len(buckets) >= RATE_MAX_KEYS):
            _prune_buckets(buckets, now, window)
        # Новому ключу освобождаем место вытеснением, а не очисткой всех окон:
        # существующие ключи сохраняют свою историю запросов.
        if key not in buckets:
            while buckets and len(buckets) >= RATE_MAX_KEYS:
                _evict_oldest_bucket(buckets)
        bucket = buckets.setdefault(key, deque())
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


app = FastAPI(title="Routeweave panel", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # Host allowlist — до аутентификации: запрос с подменённым Host не должен
    # доходить ни до Basic-auth, ни до CSRF-сравнения. 421 без деталей.
    if not _host_allowed(request.headers.get("host") or ""):
        response = JSONResponse({"detail": "Неизвестный Host"}, status_code=421)
    else:
        authenticated = _check_auth(request)
        ip = _client_ip(request)
        if not authenticated:
            if _rate_limited(AUTH_RATE_BUCKETS, ip, PANEL_AUTH_RATE_LIMIT, PANEL_AUTH_RATE_WINDOW):
                response = JSONResponse({"detail": "Слишком много запросов"}, status_code=429)
            else:
                response = JSONResponse(
                    {"detail": "Требуется авторизация"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Routeweave"'},
                )
        elif _rate_limited(RATE_BUCKETS, ip, PANEL_RATE_LIMIT, PANEL_RATE_WINDOW):
            response = JSONResponse({"detail": "Слишком много запросов"}, status_code=429)
        else:
            csrf_error = _check_csrf(request)
            if csrf_error:
                response = JSONResponse({"detail": csrf_error}, status_code=403)
            else:
                try:
                    response = await call_next(request)
                except Exception:
                    logger.exception(
                        "Необработанная ошибка при обработке %s %s",
                        request.method,
                        request.url.path,
                    )
                    response = JSONResponse({"detail": "Внутренняя ошибка панели"}, status_code=500)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    response.headers["Content-Security-Policy"] = PANEL_CSP
    return response


# ---------------- Статус и пробы API ----------------

def _tg_link(tg_link_ip: str) -> str | None:
    """tg://-ссылка с percent-encoding секрета (a&b#c не ломает query)."""
    if not TG_SECRET or not tg_link_ip:
        return None
    return (
        f"tg://proxy?server={quote(tg_link_ip, safe=':')}"
        f"&port={TGWS_PORT}&secret={quote(TG_SECRET, safe='')}"
    )


def _status_payload() -> dict:
    """Тяжёлая часть /api/status: вызывается один раз на TTL (single-flight)."""
    status_code, version = mihomo_api("/version")
    tgws = tgws_state()
    tailscale = tailscale_state()
    rx_rate, tx_rate = _tailscale_rates(tailscale["rx_bytes"], tailscale["tx_bytes"])
    tailscale["rx_rate"] = rx_rate
    tailscale["tx_rate"] = tx_rate
    services = {
        "mihomo": svc_active("mihomo"),
        "zapret-nfqws": svc_active("zapret-nfqws"),
        "routeweave-firewall": svc_active("routeweave-firewall"),
        "routeweave-panel": svc_active("routeweave-panel"),
        "routeweave-tgwsproxy": tgws,
        "tailscaled": tailscale["daemon"],
    }
    socks_set = bool(env_value("SOCKS_URL"))
    tg_link_ip = TG_LINK_IP or SERVER_IP
    tg_link = _tg_link(tg_link_ip)
    mihomo_state = "ok" if status_code == 200 else f"недоступен ({version[:80]})"
    with PROBES_LOCK:
        probes = copy.deepcopy(PROBES_LAST)
    return {
        "services": services,
        "mihomo_api": mihomo_state,
        "config": {
            "server_ip": SERVER_IP,
            "socks": "задан" if socks_set else "не задан (гео → напрямую)",
            "base_domain": env_value("BASE_DOMAIN") or "—",
            "wg_subnet": env_value("WG_SUBNET") or "—",
            "quic_drop": env_value("QUIC_DROP", "yes"),
            "tgws_enabled": tgws_enabled(tgws),
            "tg_link_ip": tg_link_ip,
            "tg_link": tg_link,
            "mihomo_api": mihomo_state,
        },
        "probes": probes,
        "tailscale": tailscale,
    }


@app.get("/api/status")
def api_status():
    if not STATUS_SEM.acquire(blocking=False):
        raise HTTPException(429, "Слишком много запросов: статус уже запрашивается")
    try:
        now = time.monotonic()
        cached = STATUS_CACHE["payload"]
        if cached is not None and STATUS_CACHE["at"] is not None and now - STATUS_CACHE["at"] < STATUS_TTL:
            return copy.deepcopy(cached)
        # Single-flight: пока первый запрос считает, остальные ждут на локе и
        # затем попадают в свежий кэш, а не запускают свои sudo-helper'ы.
        with STATUS_LOCK:
            now = time.monotonic()
            cached = STATUS_CACHE["payload"]
            if cached is not None and STATUS_CACHE["at"] is not None and now - STATUS_CACHE["at"] < STATUS_TTL:
                return copy.deepcopy(cached)
            payload = _status_payload()
            STATUS_CACHE["payload"] = payload
            STATUS_CACHE["at"] = time.monotonic()
            return copy.deepcopy(payload)
    finally:
        STATUS_SEM.release()


@app.post("/api/probe")
def api_probe():
    if not PROBE_SEM.acquire(blocking=False):
        raise HTTPException(429, "Слишком много запросов: пробы уже идут")
    try:
        results = {}
        for name, probe in PROBES.items():
            try:
                results[name] = probe()
            except Exception as exc:
                logger.exception("Проба %s упала", name)
                results[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        with PROBES_LOCK:
            PROBES_LAST["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            PROBES_LAST["results"] = results
            ts = PROBES_LAST["ts"]
        return {"ts": ts, "results": copy.deepcopy(results)}
    finally:
        PROBE_SEM.release()


# ---------------- Списки ----------------

def _list_path(name: str) -> Path:
    if name not in LISTS:
        raise HTTPException(404, "Неизвестный список")
    return LISTS_DIR / LISTS[name]


def _valid_domain(line: str) -> bool:
    """Строгая проверка домена: каждая метка — буквы/цифры/дефис.

    Метка не может начинаться или заканчиваться дефисом (``a-.com``,
    ``-a.com`` отклоняются), длина метки — до 63 символов. Префиксы ``+.``
    (mihomo) и ``*.`` (wildcard) по-прежнему допустимы.
    """
    candidate = line.strip().lower()
    for prefix in DOMAIN_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    if not candidate or len(candidate) > 253:
        return False
    labels = candidate.split(".")
    if len(labels) < 2:
        return False
    if not all(DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return False
    return DOMAIN_TLD_RE.fullmatch(labels[-1]) is not None


def validate_list_text(text: str) -> list[tuple[int, str]]:
    bad: list[tuple[int, str]] = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not _valid_domain(line):
            bad.append((number, line))
    return bad


def _atomic_write(path: Path, text: str) -> None:
    """Пишет файл через временный в том же каталоге: fsync, chmod 0644, os.replace."""
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(LISTS_DIR), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Не удалось удалить временный файл %s: %s", tmp_name, exc)
        raise


def _read_list_file(path: Path) -> str:
    """Читает файл списка, не следуя симлинкам и не превышая MAX_LIST_BYTES.

    Ошибки открытия/типа файла отдаются нейтральным 400: цель ссылки и
    реальный путь наружу не раскрываются. Отсутствующий файл — пустой список.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("Не удалось открыть список %s: %s", path, exc)
        raise HTTPException(400, "Список недоступен") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise HTTPException(400, "Список недоступен")
        if info.st_size > MAX_LIST_BYTES:
            raise HTTPException(413, "Список больше 200 КБ")
        chunks: list[bytes] = []
        total = 0
        # Файл мог вырасти между fstat и чтением: читаем не больше лимита + 1.
        while total <= MAX_LIST_BYTES:
            chunk = os.read(fd, min(65536, MAX_LIST_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_LIST_BYTES:
            raise HTTPException(413, "Список больше 200 КБ")
        data = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Список должен быть в кодировке UTF-8") from exc


@app.get("/api/lists/{name}")
def api_list_get(name: str):
    path = _list_path(name)
    return {"name": name, "content": _read_list_file(path)}


@app.put("/api/lists/{name}")
async def api_list_put(name: str, request: Request):
    path = _list_path(name)

    # Content-Length — только подсказка: доверяем ей лишь как короткому числу.
    # При chunked-кодировке заголовка нет, поэтому лимит всё равно проверяется
    # по фактически прочитанным байтам ниже.
    declared = (request.headers.get("content-length") or "").strip()
    if declared and len(declared) <= 12:
        try:
            if int(declared) > MAX_LIST_BYTES:
                raise HTTPException(413, "Список больше 200 КБ")
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_LIST_BYTES:
            # Не дочитываем тело: сразу 413, соединение закроет сервер.
            raise HTTPException(413, "Список больше 200 КБ")
        chunks.append(chunk)
    body = b"".join(chunks)

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Список должен быть в кодировке UTF-8") from exc

    bad = validate_list_text(text)
    if bad:
        shown = ", ".join(f"{number}: {line!r}" for number, line in bad[:3])
        raise HTTPException(400, f"Некорректные строки (первые 3): {shown}")

    if text and not text.endswith("\n"):
        text += "\n"
    try:
        _atomic_write(path, text)
    except OSError as exc:
        logger.exception("Не удалось сохранить список %s", path)
        raise HTTPException(500, f"Не удалось сохранить список: {exc}") from exc
    return {"ok": True, "bytes": len(text.encode("utf-8")), "hint": "Не забудьте применить изменения"}


# ---------------- Применение списков (фоновая задача) ----------------

def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _job_update(**fields) -> None:
    with JOB_LOCK:
        JOB.update(fields)


def _job_snapshot() -> dict:
    with JOB_LOCK:
        return dict(JOB)


def _tail(text: str, limit: int = 1000) -> str:
    return (text or "").strip()[-limit:]


def _run_apply(name: str) -> None:
    """Обновляет списки и перезапускает нужный компонент; статус — в JOB."""
    try:
        _job_update(running=True, started_at=_utcnow(), finished_at=None, ok=None, detail="Применение...")
        result = sudo_run([UPDATER], timeout=180)
        detail = _tail((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))
        ok = result.returncode == 0
        if ok and name == "dpi":
            restart = sudo_run(["systemctl", "restart", "zapret-nfqws"], timeout=60)
            if restart.returncode != 0:
                ok = False
                detail = _tail(detail + "\nzapret-nfqws: " + (restart.stderr or restart.stdout))
            else:
                detail = _tail(detail + "\nzapret-nfqws перезапущен")
        elif ok:
            detail = _tail(detail + "\nmihomo перечитал списки")
        _job_update(ok=ok, detail=detail or ("Готово" if ok else "Обновление списков не удалось"))
    except Exception as exc:
        logger.exception("Ошибка применения списков %s", name)
        _job_update(ok=False, detail=f"{type(exc).__name__}: {exc}"[:1000])
    finally:
        _job_update(running=False, finished_at=_utcnow())


@app.post("/api/lists/{name}/apply")
def api_list_apply(name: str, background: BackgroundTasks):
    _list_path(name)
    try:
        with JOB_LOCK:
            # Состояние живёт в памяти процесса и после рестарта всегда False:
            # залипнуть может только «running», выставленный в этом процессе.
            if JOB["running"]:
                raise HTTPException(409, "Применение уже идёт")
            JOB["running"] = True
        background.add_task(_run_apply, name)
    except BaseException:
        # add_task может упасть (например, при сломанном BackgroundTasks) —
        # не оставляем JOB навсегда в running.
        with JOB_LOCK:
            JOB["running"] = False
        raise
    return {"started": True}


@app.get("/api/jobs/apply")
def api_job_apply():
    return _job_snapshot()


# ---------------- Рестарты ----------------

@app.post("/api/apply/{target}")
def api_apply(target: str):
    units = {"mihomo": "mihomo", "zapret": "zapret-nfqws"}
    if target not in units:
        raise HTTPException(404, "Неизвестная цель применения")
    result = sudo_run(["systemctl", "restart", units[target]], timeout=30)
    if result.returncode != 0:
        raise _sudo_error(result, f"Не удалось перезапустить {units[target]}")
    return {"ok": True, "detail": (result.stdout or "").strip()[:300]}


@app.post("/api/restart/{name}")
def api_restart(name: str):
    units = {"mihomo": "mihomo", "zapret": "zapret-nfqws", "panel": "routeweave-panel"}
    if name in units:
        result = sudo_run(["systemctl", "restart", units[name]], timeout=30)
        if result.returncode != 0:
            raise _sudo_error(result, f"Не удалось перезапустить {units[name]}")
        return {"ok": True, "detail": (result.stdout or "").strip()[:300]}
    if name == "tgwsproxy":
        # 70 с: helper сам ограничивает docker restart 60 секундами и успевает
        # напечатать понятную диагностику раньше, чем сработает этот таймаут.
        result = sudo_run([TGWS_RESTART], timeout=70)
        if result.returncode != 0:
            raise _sudo_error(result, "Не удалось перезапустить tg-ws-proxy")
        return {"ok": True, "detail": (result.stdout or "").strip()[:300]}
    raise HTTPException(404, "Неизвестный сервис")


# ---------------- Статика ----------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(env_setting("PANEL_PORT", "8080") or "8080")
    # PANEL_BIND: SERVER_IP (панель в домашней сети), по умолчанию — loopback.
    # Zashboard монтируется не здесь: его отдаёт mihomo на :9090/ui, это
    # отдельный origin, панель чужие файлы не проксирует.
    logger.info("Панель слушает %s:%s (один воркер)", PANEL_BIND, port)
    uvicorn.run(app, host=PANEL_BIND, port=port, log_level="info")


if __name__ == "__main__":
    main()
