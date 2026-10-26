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
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

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

MIHOMO_API = "http://127.0.0.1:9090"
MIXED_PORT = 7890
TGWS_PORT = 1443

MAX_LIST_BYTES = 200_000
DOMAIN_RE = re.compile(r"(\+\.)?[a-z0-9]([a-z0-9-]{0,62}\.)+[a-z]{2,63}", re.IGNORECASE)
SOCKS_SCHEMES = {"socks5", "socks5h", "socks", "http", "https"}

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# CSP панели: строгий, без inline.
PANEL_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'"
)
# CSP zashboard: третья сторона, ходит в API mihomo на другой порт и в WS.
# script-src остаётся строгим ('self'): inline-скрипты по-прежнему запрещены.
UI_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self' http: https: ws: wss:; "
    "base-uri 'none'; form-action 'self'"
)

PROBES_LAST: dict = {"ts": None, "results": {}}

JOB_LOCK = threading.Lock()
JOB: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "detail": None,
}

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
    """Снимает кавычки по shell-семантике установщика.

    * ``"..."`` — кавычки снимаются, раскрываются только ``\\"`` и ``\\\\``;
    * ``'...'`` — кавычки снимаются без раскрытия escape;
    * без кавычек — значение как есть (обратная совместимость);
    * незакрытая кавычка — значение как есть плюс предупреждение в лог.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        if value[0] == '"':
            return _unescape_double(value[1:-1])
        return value[1:-1]
    if value[:1] in ('"', "'"):
        logger.warning("Незакрытая кавычка в %s, строка %s", ENV_FILE, line_number)
    return value


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


ENV = load_env()
PANEL_USER = env_value("PANEL_USER", "admin") or "admin"
PANEL_PASS = env_value("PANEL_PASS")
SERVER_IP = env_value("SERVER_IP")
TG_LINK_IP = env_value("TG_LINK_IP")
TG_SECRET = env_value("TG_SECRET")
MIHOMO_SECRET = env_value("MIHOMO_SECRET")

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
    return state if state in ("running", "exited", "absent") else "unknown"


def tgws_enabled() -> bool:
    return tgws_state() in ("running", "exited") or bool(TG_SECRET)


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
    result = sudo_run([TAILSCALE_STATE], timeout=10)
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
    """Проверяет источник изменяющего запроса; без заголовков — пропускает (curl)."""
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    host = (request.headers.get("host") or "").lower()
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin and urlsplit(origin).netloc.lower() != host:
        return "CSRF: чужой Origin"
    if not origin and referer and urlsplit(referer).netloc.lower() != host:
        return "CSRF: чужой Referer"
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return "CSRF: запрос с чужого сайта"
    return None


app = FastAPI(title="Routeweave panel", docs_url=None, redoc_url=None, openapi_url=None)


def _is_ui_path(path: str) -> bool:
    """True для /ui и /ui/..., но не для /uixyz."""
    return path == "/ui" or path.startswith("/ui/")


@app.middleware("http")
async def guard(request: Request, call_next):
    if not _check_auth(request):
        response = JSONResponse(
            {"detail": "Требуется авторизация"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Routeweave"'},
        )
    else:
        csrf_error = _check_csrf(request)
        if csrf_error:
            response = JSONResponse({"detail": csrf_error}, status_code=403)
        else:
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("Необработанная ошибка при обработке %s %s", request.method, request.url.path)
                response = JSONResponse({"detail": "Внутренняя ошибка панели"}, status_code=500)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    response.headers["Content-Security-Policy"] = (
        UI_CSP if _is_ui_path(request.url.path) else PANEL_CSP
    )
    return response


# ---------------- Статус и пробы API ----------------

@app.get("/api/status")
def api_status():
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
    tg_link = (
        f"tg://proxy?server={tg_link_ip}&port={TGWS_PORT}&secret={TG_SECRET}"
        if TG_SECRET and tg_link_ip
        else None
    )
    mihomo_state = "ok" if status_code == 200 else f"недоступен ({version[:80]})"
    return {
        "services": services,
        "mihomo_api": mihomo_state,
        "config": {
            "server_ip": SERVER_IP,
            "socks": "задан" if socks_set else "не задан (гео → напрямую)",
            "base_domain": env_value("BASE_DOMAIN") or "—",
            "wg_subnet": env_value("WG_SUBNET") or "—",
            "quic_drop": env_value("QUIC_DROP", "yes"),
            "tgws_enabled": tgws in ("running", "exited") or bool(TG_SECRET),
            "tg_link_ip": tg_link_ip,
            "tg_link": tg_link,
            "mihomo_api": mihomo_state,
        },
        "probes": PROBES_LAST,
        "tailscale": tailscale,
    }


@app.post("/api/probe")
def api_probe():
    results = {}
    for name, probe in PROBES.items():
        try:
            results[name] = probe()
        except Exception as exc:
            logger.exception("Проба %s упала", name)
            results[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    PROBES_LAST["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    PROBES_LAST["results"] = results
    return {"ts": PROBES_LAST["ts"], "results": results}


# ---------------- Списки ----------------

def _list_path(name: str) -> Path:
    if name not in LISTS:
        raise HTTPException(404, "Неизвестный список")
    return LISTS_DIR / LISTS[name]


def _valid_domain(line: str) -> bool:
    """Проверяет домен по regex и дополнительно запрещает пустые метки.

    Сам regex из контракта допускает ``bad..dom`` (метка из 0 символов),
    поэтому пустые метки отсекаем отдельно.
    """
    if not DOMAIN_RE.fullmatch(line):
        return False
    candidate = line.removeprefix("+.")
    return "" not in candidate.split(".")


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


@app.get("/api/lists/{name}")
def api_list_get(name: str):
    path = _list_path(name)
    try:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        logger.exception("Не удалось прочитать список %s", path)
        raise HTTPException(500, f"Не удалось прочитать список: {exc}") from exc
    return {"name": name, "content": content}


@app.put("/api/lists/{name}")
async def api_list_put(name: str, request: Request):
    path = _list_path(name)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_LIST_BYTES:
        raise HTTPException(413, "Список больше 200 КБ")

    body = await request.body()
    if len(body) > MAX_LIST_BYTES:
        raise HTTPException(413, "Список больше 200 КБ")

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
    with JOB_LOCK:
        if JOB["running"]:
            raise HTTPException(409, "Применение уже идёт")
        JOB["running"] = True
    background.add_task(_run_apply, name)
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
        result = sudo_run([TGWS_RESTART], timeout=60)
        if result.returncode != 0:
            raise _sudo_error(result, "Не удалось перезапустить tg-ws-proxy")
        return {"ok": True, "detail": (result.stdout or "").strip()[:300]}
    raise HTTPException(404, "Неизвестный сервис")


# ---------------- Статика ----------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

ZASHBOARD_DIR = ETC / "zashboard"
if ZASHBOARD_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(ZASHBOARD_DIR), html=True), name="ui")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(env_value("PANEL_PORT", "8080") or "8080")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
