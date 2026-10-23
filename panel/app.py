#!/usr/bin/env python3
"""Routeweave веб-панель: статусы сервисов, пробы путей, редактор списков, рестарты.

Нативный сервис (systemd), читает /etc/routeweave/.env.
Доступ только по Basic-авторизации (PANEL_USER / PANEL_PASS).
"""
import base64
import hmac
import socket
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

CFG_DIR = Path("/etc/routeweave")
ENV_FILE = CFG_DIR / ".env"
LISTS_DIR = CFG_DIR / "lists"
LISTS = {"geo-custom": "geo-custom.txt", "dpi": "dpi.txt", "ru-custom": "ru-direct-custom.txt"}
UPDATER = "/usr/local/sbin/routeweave-update-lists.sh"
MIHOMO_API = "http://127.0.0.1:9090"
MIXED_PORT = 7890

PROBES_LAST: dict = {"ts": None, "results": {}}


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
PANEL_USER = ENV.get("PANEL_USER") or "admin"
PANEL_PASS = ENV.get("PANEL_PASS") or ""
if not PANEL_PASS:
    raise SystemExit("PANEL_PASS не задан в /etc/routeweave/.env — панель не запускается. Задайте пароль и перезапустите routeweave-panel.")


def run(cmd: list, timeout: int = 15) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def svc_active(unit: str) -> str:
    rc, out = run(["systemctl", "is-active", unit], timeout=5)
    return out if out in ("active", "inactive", "failed", "activating") else "unknown"


def docker_client() -> httpx.Client:
    transport = httpx.HTTPTransport(uds="/var/run/docker.sock")
    return httpx.Client(transport=transport, base_url="http://docker", timeout=5)


def docker_container_state(name: str) -> str:
    try:
        with docker_client() as c:
            r = c.get("/containers/json?all=true")
            for item in r.json():
                names = item.get("Names") or []
                if name in names or ("/" + name) in names:
                    return item.get("State", "unknown")
    except Exception:
        return "no-docker-socket"
    return "absent"


def tgws_enabled() -> bool:
    state = docker_container_state("routeweave-tgwsproxy")
    return state not in ("absent", "no-docker-socket") or bool((ENV.get("TG_SECRET") or "").strip())


def mihomo_api(path: str, method: str = "GET") -> tuple:
    try:
        with httpx.Client(timeout=4) as c:
            r = c.request(method, MIHOMO_API + path,
                          headers={"Authorization": "Bearer " + (ENV.get("MIHOMO_SECRET") or "")})
            return r.status_code, r.text[:500]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ---------------- Пробы ----------------

def probe_socks_chatgpt() -> dict:
    socks_url = (ENV.get("SOCKS_URL") or "").strip()
    if not socks_url:
        return {"ok": True, "detail": "SOCKS_URL не задан — гео-домены идут напрямую", "skipped": True}
    scheme = "socks5" if socks_url.startswith("socks") else "http"
    try:
        t0 = time.monotonic()
        with httpx.Client(proxy=f"{scheme}://127.0.0.1:{MIXED_PORT}", timeout=8, follow_redirects=False) as c:
            r = c.get("https://chatgpt.com")
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": r.status_code < 500, "detail": f"HTTP {r.status_code}, {ms} мс (через mihomo→socks)"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def probe_socks_exit_ip() -> dict:
    socks_url = (ENV.get("SOCKS_URL") or "").strip()
    if not socks_url:
        return {"ok": True, "detail": "нет socks — пропущено", "skipped": True}
    scheme = "socks5" if socks_url.startswith("socks") else "http"
    try:
        with httpx.Client(proxy=f"{scheme}://127.0.0.1:{MIXED_PORT}", timeout=8) as c:
            r = c.get("https://api.ipify.org")
        return {"ok": r.status_code == 200, "detail": "exit IP: " + r.text[:64]}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def probe_youtube_direct() -> dict:
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=10) as c:  # без прокси: прямой путь + zapret
            r = c.get("https://www.youtube.com/generate_204", follow_redirects=False)
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": r.status_code in (204, 200, 301, 302),
                "detail": f"HTTP {r.status_code}, {ms} мс (direct+zapret)"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e} — zapret не спасает или домен ушёл из списка"}


def probe_tg_local() -> dict:
    if not tgws_enabled():
        return {"ok": True, "detail": "tg-ws-proxy не установлен (опциональный компонент)", "skipped": True}
    try:
        t0 = time.monotonic()
        with socket.create_connection(("127.0.0.1", 1443), timeout=4):
            pass
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "detail": f"tg-ws-proxy слушает :1443, TCP ok, {ms} мс"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def probe_worker() -> dict:
    domain = (ENV.get("BASE_DOMAIN") or "").strip()
    if not domain:
        return {"ok": True, "detail": "BASE_DOMAIN не задан — CF Worker не используется", "skipped": True}
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(f"https://{domain}/", follow_redirects=False)
        return {"ok": r.status_code < 500, "detail": f"HTTP {r.status_code} от https://{domain}/"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


PROBES = {
    "chatgpt_via_socks": probe_socks_chatgpt,
    "socks_exit_ip": probe_socks_exit_ip,
    "youtube_direct": probe_youtube_direct,
    "tg_ws_proxy": probe_tg_local,
    "cf_worker": probe_worker,
}


# ---------------- API ----------------

app = FastAPI(title="Routeweave panel", docs_url=None, redoc_url=None, openapi_url=None)


class BasicAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        ok = False
        if auth.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(user, PANEL_USER) and hmac.compare_digest(pwd, PANEL_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Routeweave"'})
        return await call_next(request)


app.add_middleware(BasicAuth)


@app.get("/api/status")
def api_status():
    rc, ver = mihomo_api("/version")
    services = {
        "mihomo": svc_active("mihomo"),
        "zapret-nfqws": svc_active("zapret-nfqws"),
        "routeweave-firewall": svc_active("routeweave-firewall"),
        "routeweave-panel": svc_active("routeweave-panel"),
        "routeweave-tgwsproxy": docker_container_state("routeweave-tgwsproxy"),
    }
    socks_set = bool((ENV.get("SOCKS_URL") or "").strip())
    return {
        "services": services,
        "mihomo_api": "ok" if rc == 200 else f"недоступен ({ver[:80]})",
        "config": {
            "server_ip": ENV.get("SERVER_IP", ""),
            "socks": "задан" if socks_set else "не задан (гео → напрямую)",
            "base_domain": ENV.get("BASE_DOMAIN", "") or "—",
            "wg_subnet": ENV.get("WG_SUBNET", "") or "—",
            "quic_drop": ENV.get("QUIC_DROP", "yes"),
            "tgws_enabled": tgws_enabled(),
        },
        "probes": PROBES_LAST,
    }


@app.post("/api/probe")
def api_probe():
    results = {}
    for name, fn in PROBES.items():
        try:
            results[name] = fn()
        except Exception as e:  # страховка
            results[name] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    PROBES_LAST["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    PROBES_LAST["results"] = results
    return {"ts": PROBES_LAST["ts"], "results": results}


@app.get("/api/lists/{name}")
def api_list_get(name: str):
    if name not in LISTS:
        raise HTTPException(404)
    path = LISTS_DIR / LISTS[name]
    return {"name": name, "content": path.read_text(encoding="utf-8") if path.exists() else ""}


@app.put("/api/lists/{name}")
async def api_list_put(name: str, request: Request):
    if name not in LISTS:
        raise HTTPException(404)
    body = await request.body()
    if len(body) > 200_000:
        raise HTTPException(413, "список слишком большой")
    text = body.decode("utf-8", errors="replace")
    bad = [ln for ln in text.splitlines()
           if ln.strip() and not ln.strip().startswith(("#", "+.", ".")) and "+" not in ln
           and not all(ch.isalnum() or ch in "+.-_*" for ch in ln.strip())]
    if bad:
        raise HTTPException(400, f"непонятные строки: {bad[:3]}")
    path = LISTS_DIR / LISTS[name]
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return {"ok": True, "bytes": len(text), "hint": "не забудьте применить"}


@app.post("/api/lists/{name}/apply")
def api_list_apply(name: str):
    if name not in LISTS:
        raise HTTPException(404)
    rc, out = run([UPDATER], timeout=180)
    if rc != 0:
        return {"ok": False, "detail": (out or "updater завершился с ошибкой")[-1200:]}
    detail = out[-1200:]
    if name == "dpi":
        rc2, out2 = run(["systemctl", "restart", "zapret-nfqws"], timeout=30)
        if rc2 != 0:
            return {"ok": False, "detail": f"списки обновлены, но zapret-nfqws не перезапустился: {out2[:300]}"}
        detail = (detail + "\nzapret-nfqws перезапущен").strip()
    else:
        detail = (detail + "\nmihomo перезагрузил списки (updater)").strip()
    return {"ok": True, "detail": detail}


@app.post("/api/apply/{target}")
def api_apply(target: str):
    if target == "mihomo":
        rc, out = run(["systemctl", "restart", "mihomo"], timeout=30)
    elif target == "zapret":
        rc, out = run(["systemctl", "restart", "zapret-nfqws"], timeout=30)
    else:
        raise HTTPException(404)
    return {"ok": rc == 0, "detail": out[:300]}


@app.post("/api/restart/{name}")
def api_restart(name: str):
    units = {"mihomo": "mihomo", "zapret": "zapret-nfqws", "panel": "routeweave-panel"}
    if name in units:
        rc, out = run(["systemctl", "restart", units[name]], timeout=30)
        return {"ok": rc == 0, "detail": out[:300]}
    if name == "tgwsproxy":
        try:
            with docker_client() as c:
                r = c.post("/containers/routeweave-tgwsproxy/restart?t=10")
            return {"ok": r.status_code == 204, "detail": r.text[:200]}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    raise HTTPException(404)


# ---------------- Статика ----------------

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


if (CFG_DIR / "zashboard").is_dir():
    app.mount("/ui", StaticFiles(directory=str(CFG_DIR / "zashboard"), html=True), name="ui")
