"""Тесты панели Routeweave: авторизация, CSRF, заголовки, списки, фоновое применение.

Тесты не требуют root и сети: внешние команды всегда подменяются стабом
``run_cmd``, а API mihomo — заглушкой. Конфигурация готовится в tmp_path,
модуль ``panel/app.py`` загружается заново с ``ROUTEWEAVE_ETC`` из окружения.
"""

import base64
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

# Тестовый клиент Starlette ещё использует httpx и предупреждает об этом.
# Гасим предупреждение до импорта, чтобы строгий filterwarnings в pytest.ini
# (его ведёт другой исполнитель) не превратил его в ошибку.
warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated",
)

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "panel" / "app.py"
AUTH = ("admin", "secret")
MUTATING = ("POST", "PUT", "DELETE", "PATCH")

ENV_CONTENT = """\
PANEL_USER=admin
PANEL_PASS=secret
MIHOMO_SECRET=x
SERVER_IP=192.168.1.10
BASE_DOMAIN=
SOCKS_URL=
WG_SUBNET=10.66.66.0/24
TG_LINK_IP=
"""

# Компактный ответ helper'а Tailscale: CGNAT-адрес — заглушка из 100.64.0.0/10.
TAILSCALE_FIELDS = {
    "installed": True,
    "daemon": "active",
    "backend": "Running",
    "self_ip": "100.64.0.10",
    "advertised": True,
    "approved": True,
    "peers": 1,
    "iface": "tailscale0",
    "rx_bytes": 1000,
    "tx_bytes": 2000,
    "error": "",
}
TAILSCALE_JSON = json.dumps(TAILSCALE_FIELDS, separators=(",", ":"))


def default_handler(args, timeout=60):
    """Стаб внешних команд: сервисы активны, helper'ы отвечают успехом."""
    joined = " ".join(args)
    if "is-active" in args:
        return subprocess.CompletedProcess(args, 0, "active\n", "")
    if joined.endswith("routeweave-tgws-state.sh"):
        return subprocess.CompletedProcess(args, 0, "running\n", "")
    if joined.endswith("routeweave-tailscale-state.sh"):
        return subprocess.CompletedProcess(args, 0, TAILSCALE_JSON + "\n", "")
    if joined.endswith("routeweave-update-lists.sh"):
        return subprocess.CompletedProcess(args, 0, "списки обновлены\n", "")
    return subprocess.CompletedProcess(args, 0, "", "")


FIXED_RATE_LIMIT_ENV = {
    "PANEL_RATE_LIMIT": "100000",
    "PANEL_AUTH_RATE_LIMIT": "100000",
}


class PanelClient(TestClient):
    """TestClient, который по умолчанию шлёт X-Requested-With для мутаций.

    Так обычные тесты не обязаны помнить про CSRF-заголовок, а тесты самой
    CSRF-проверки выключают автоинъекцию (``inject_csrf = False``).
    """

    inject_csrf = True

    def request(self, method, url, **kwargs):
        if self.inject_csrf and method.upper() in MUTATING:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-Requested-With", "Routeweave")
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """Каталог конфигурации с .env; ROUTEWEAVE_ETC виден при импорте модуля."""
    etc_dir = tmp_path / "etc"
    (etc_dir / "lists").mkdir(parents=True)
    (etc_dir / ".env").write_text(ENV_CONTENT, encoding="utf-8")
    monkeypatch.setenv("ROUTEWEAVE_ETC", str(etc_dir))
    for name, value in FIXED_RATE_LIMIT_ENV.items():
        monkeypatch.setenv(name, value)
    return etc_dir


def make_client(monkeypatch, handler=default_handler):
    """Загружает panel/app.py как отдельный модуль и подменяет run_cmd."""
    # TestClient шлёт Host: testserver. Прод-код allowlist не ослабляем:
    # добавляем testserver только в тестовом окружении.
    hosts = [h.strip() for h in os.environ.get("PANEL_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if "testserver" not in hosts:
        hosts.append("testserver")
    monkeypatch.setenv("PANEL_ALLOWED_HOSTS", ",".join(hosts))

    spec = importlib.util.spec_from_file_location("rw_panel_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    calls = []

    def fake_run_cmd(args, timeout=60):
        calls.append(list(args))
        return handler(list(args), timeout)

    monkeypatch.setattr(module, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(module, "mihomo_api", lambda path, method="GET": (200, "test"))
    return module, PanelClient(module.app), calls


@pytest.fixture
def panel(etc, monkeypatch):
    module, client, calls = make_client(monkeypatch)
    return SimpleNamespace(module=module, client=client, calls=calls, etc=etc)


# ---------------- Авторизация ----------------

def test_auth_required_and_security_headers(panel):
    response = panel.client.get("/api/status")
    assert response.status_code == 401
    assert response.json()["detail"] == "Требуется авторизация"
    assert response.headers["www-authenticate"].startswith("Basic")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_auth_ok_and_wrong_password(panel):
    ok = panel.client.get("/api/status", auth=AUTH)
    assert ok.status_code == 200
    wrong = panel.client.get("/api/status", auth=("admin", "wrong"))
    assert wrong.status_code == 401


def test_non_ascii_password_is_401_not_500(panel):
    token = base64.b64encode("admin:пароль".encode()).decode("ascii")
    response = panel.client.get("/api/status", headers={"Authorization": "Basic " + token})
    assert response.status_code == 401


def test_empty_credentials_are_401(panel):
    response = panel.client.get("/api/status", auth=("", ""))
    assert response.status_code == 401


def test_empty_panel_pass_fails_fast(etc, monkeypatch):
    (etc / ".env").write_text("PANEL_USER=admin\nPANEL_PASS=\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        make_client(monkeypatch)


# ---------------- Разбор .env (кавычки и экранирование) ----------------

def write_env(etc, content):
    """Перезаписывает .env; модуль загружается заново уже с новым файлом."""
    (etc / ".env").write_text(content, encoding="utf-8")


def test_env_double_quoted_password_with_space(etc, monkeypatch):
    write_env(etc, 'PANEL_USER=admin\nPANEL_PASS="p@ss word"\n')
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "p@ss word"
    assert client.get("/api/status", auth=("admin", "p@ss word")).status_code == 200
    # литеральные кавычки в пароле — уже другое значение
    assert client.get("/api/status", auth=("admin", '"p@ss word"')).status_code == 401


def test_env_quoted_value_keeps_edges_and_spaces(etc, monkeypatch):
    write_env(etc, 'PANEL_USER=admin\nPANEL_PASS=" secret "\n')
    module, client, _ = make_client(monkeypatch)
    assert module.env_value("PANEL_PASS") == " secret "
    assert client.get("/api/status", auth=("admin", " secret ")).status_code == 200
    assert client.get("/api/status", auth=("admin", "secret")).status_code == 401


def test_env_hash_inside_quotes_not_truncated(etc, monkeypatch):
    write_env(etc, 'PANEL_USER=admin\nPANEL_PASS=secret\nBASE_DOMAIN="a#b"\n')
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["BASE_DOMAIN"] == "a#b"
    config = client.get("/api/status", auth=AUTH).json()["config"]
    assert config["base_domain"] == "a#b"


def test_env_escaped_quote_semantics(etc, monkeypatch):
    # В файле ровно: PANEL_PASS="a\\\"b" — символы: a, \, \, \, ", b.
    # Однопроходный разбор: \\ -> \, затем \" -> " ⇒ значение a\"b.
    write_env(etc, 'PANEL_USER=admin\n' + r'PANEL_PASS="a\\\"b"' + "\n")
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == r'a\"b'
    assert client.get("/api/status", auth=("admin", r'a\"b')).status_code == 200
    assert client.get("/api/status", auth=("admin", 'a"b')).status_code == 401


def test_env_unknown_escape_kept_as_is(etc, monkeypatch):
    write_env(etc, 'PANEL_USER=admin\nPANEL_PASS="a\\nb"\n')
    module, _, _ = make_client(monkeypatch)
    # \n не превращается в перевод строки — остаются два символа
    assert module.load_env()["PANEL_PASS"] == "a\\nb"


def test_env_single_quotes_no_escape(etc, monkeypatch):
    write_env(etc, "PANEL_USER=admin\n" + r"PANEL_PASS='a\\b'" + "\n")
    module, client, _ = make_client(monkeypatch)
    # shell-семантика одинарных кавычек: обратные слэши не раскрываются
    assert module.load_env()["PANEL_PASS"] == r"a\\b"
    assert client.get("/api/status", auth=("admin", r"a\\b")).status_code == 200


def test_env_installer_quote_escape(etc, monkeypatch):
    # install.sh пишет апостроф как '\'' — панель читает его обратно тем же.
    write_env(etc, "PANEL_USER=admin\n" + r"PANEL_PASS='a'\''b'" + "\n")
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "a'b"
    assert client.get("/api/status", auth=("admin", "a'b")).status_code == 200
    assert client.get("/api/status", auth=("admin", r"a'\''b")).status_code == 401


def test_env_multiple_escaped_apostrophes(etc, monkeypatch):
    write_env(etc, "PANEL_USER=admin\n" + r"PANEL_PASS='it'\''s'\''ok'" + "\n")
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "it's'ok"
    assert client.get("/api/status", auth=("admin", "it's'ok")).status_code == 200


def test_env_concatenated_quote_segments(etc, monkeypatch):
    # shell-семантика: соседние сегменты в разных кавычках склеиваются
    write_env(etc, "PANEL_USER=admin\nPANEL_PASS='a'" + '"b"' + "\n")
    module, _, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "ab"


def test_env_hash_inside_single_quotes_not_truncated(etc, monkeypatch):
    write_env(etc, "PANEL_USER=admin\nPANEL_PASS=secret\n" + r"BASE_DOMAIN='a#b'" + "\n")
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["BASE_DOMAIN"] == "a#b"
    config = client.get("/api/status", auth=AUTH).json()["config"]
    assert config["base_domain"] == "a#b"


def test_env_unquoted_backslash_escape(etc, monkeypatch):
    write_env(etc, "PANEL_USER=admin\n" + r"PANEL_PASS=a\ b" + "\n")
    module, _, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "a b"


def test_env_trailing_backslash_kept(etc, monkeypatch):
    write_env(etc, "PANEL_USER=admin\n" + "PANEL_PASS=x\\" + "\n")
    module, _, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "x\\"


def test_env_unquoted_legacy_hash(etc, monkeypatch):
    write_env(etc, "PANEL_USER=admin\nPANEL_PASS=old#pass\n")
    module, client, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == "old#pass"
    assert client.get("/api/status", auth=("admin", "old#pass")).status_code == 200


def test_env_unclosed_quote_does_not_break_import(etc, monkeypatch, caplog):
    write_env(etc, 'PANEL_USER=admin\nPANEL_PASS="unclosed\n')
    with caplog.at_level(logging.WARNING, logger="routeweave"):
        module, client, _ = make_client(monkeypatch)
    assert module.load_env()["PANEL_PASS"] == '"unclosed'
    assert client.get("/api/status", auth=("admin", '"unclosed')).status_code == 200
    assert any("Незакрытая кавычка" in record.getMessage() for record in caplog.records)


# ---------------- CSRF ----------------

def test_csrf_foreign_origin_403(panel):
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF: чужой Origin"


def test_csrf_same_origin_allowed(panel):
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_csrf_foreign_referer_403(panel):
    response = panel.client.post(
        "/api/restart/mihomo",
        auth=AUTH,
        headers={"Referer": "http://evil.example/page"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF: чужой Referer"


def test_csrf_cross_site_fetch_metadata_403(panel):
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    assert "Sec-Fetch-Site" in response.json()["detail"] or "CSRF" in response.json()["detail"]


def test_csrf_same_site_metadata_allowed(panel):
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert response.status_code == 200


def test_csrf_same_site_none_allowed(panel):
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Sec-Fetch-Site": "none"}
    )
    assert response.status_code == 200


def test_csrf_without_any_signal_403(panel):
    """Fail-closed: ни Origin, ни Referer, ни Sec-Fetch-Site, ни XHR — отказ."""
    panel.client.inject_csrf = False
    response = panel.client.post("/api/restart/mihomo", auth=AUTH)
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_csrf_x_requested_with_allows_curl_style(panel):
    panel.client.inject_csrf = False
    response = panel.client.post(
        "/api/lists/geo-custom/apply",
        auth=AUTH,
        headers={"X-Requested-With": "Routeweave"},
    )
    assert response.status_code == 200
    assert response.json() == {"started": True}


def test_csrf_broken_origin_is_403_not_500(panel):
    panel.client.inject_csrf = False
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Origin": "http://["}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF: чужой Origin"


def test_csrf_origin_port_mismatch_403(panel):
    response = panel.client.post(
        "/api/restart/mihomo", auth=AUTH, headers={"Origin": "http://testserver:8080"}
    )
    assert response.status_code == 403


def test_csrf_https_origin_rejected_without_panel_tls(panel):
    """X-Forwarded-Proto не доверяем: схема берётся из PANEL_TLS."""
    response = panel.client.post(
        "/api/restart/mihomo",
        auth=AUTH,
        headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 403


def test_csrf_get_requests_not_checked(panel):
    panel.client.inject_csrf = False
    response = panel.client.get("/api/jobs/apply", auth=AUTH)
    assert response.status_code == 200


def test_auth_checked_before_csrf(panel):
    response = panel.client.post(
        "/api/restart/mihomo", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 401


# ---------------- Host allowlist ----------------

def test_host_unknown_421(panel):
    response = panel.client.get("/api/status", auth=AUTH, headers={"Host": "evil.example"})
    assert response.status_code == 421
    assert "evil.example" not in response.text


def test_host_server_ip_allowed(panel):
    response = panel.client.get(
        "/api/jobs/apply", auth=AUTH, headers={"Host": "192.168.1.10:8080"}
    )
    assert response.status_code == 200


def test_host_localhost_and_ipv6_allowed(panel):
    for host in ("localhost:8080", "127.0.0.1:8080", "[::1]:8080"):
        response = panel.client.get("/api/jobs/apply", auth=AUTH, headers={"Host": host})
        assert response.status_code == 200, host


def test_host_panel_allowed_hosts_env(etc, monkeypatch):
    monkeypatch.setenv("PANEL_ALLOWED_HOSTS", "panel.home,extra.example")
    _module, client, _ = make_client(monkeypatch)
    assert client.get(
        "/api/jobs/apply", auth=AUTH, headers={"Host": "panel.home"}
    ).status_code == 200
    assert client.get(
        "/api/jobs/apply", auth=AUTH, headers={"Host": "extra.example:8080"}
    ).status_code == 200
    assert client.get(
        "/api/jobs/apply", auth=AUTH, headers={"Host": "not-listed.example"}
    ).status_code == 421


# ---------------- Заголовки безопасности ----------------

def test_security_headers_on_index_and_404(panel):
    index = panel.client.get("/", auth=AUTH)
    assert index.status_code == 200
    assert index.headers["content-security-policy"].startswith("default-src 'self'")
    assert index.headers["x-frame-options"] == "DENY"

    missing = panel.client.get("/api/lists/nope", auth=AUTH)
    assert missing.status_code == 404
    assert missing.headers["x-frame-options"] == "DENY"
    assert missing.headers["cache-control"] == "no-store"


def test_security_headers_full_set(panel):
    headers = panel.client.get("/", auth=AUTH).headers
    assert headers["permissions-policy"] == "geolocation=(), camera=(), microphone=()"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    csp = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_ui_is_not_mounted(panel):
    """Zashboard отдаёт mihomo на :9090/ui — панель не проксирует его."""
    response = panel.client.get("/ui/", auth=AUTH)
    assert response.status_code == 404
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "http:" not in csp


def test_panel_csp_stays_strict(panel):
    response = panel.client.get("/", auth=AUTH)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "http:" not in csp
    assert "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_uixyz_gets_panel_csp(panel):
    """Похожий на /ui путь не должен получать ослабленную CSP."""
    response = panel.client.get("/uixyz", auth=AUTH)
    assert response.status_code in (200, 404)
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "http:" not in csp
    assert "'unsafe-inline'" not in csp


def test_index_has_no_zashboard_link(panel):
    html = panel.client.get("/", auth=AUTH).text
    assert 'href="/ui' not in html
    assert "9090/ui" in html


def test_index_and_assets_have_no_inline_code(panel):
    html = panel.client.get("/", auth=AUTH).text
    assert "<style" not in html
    assert "<script>" not in html
    assert 'href="/static/app.css"' in html
    assert 'src="/static/app.js"' in html

    js = panel.client.get("/static/app.js", auth=AUTH)
    assert js.status_code == 200
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in js.text

    css = panel.client.get("/static/app.css", auth=AUTH)
    assert css.status_code == 200


def test_js_has_csrf_header_and_payload_snapshot(panel):
    js = panel.client.get("/static/app.js", auth=AUTH).text
    assert "X-Requested-With" in js
    assert "'Routeweave'" in js
    assert "const payload" in js
    assert "encodeURIComponent(secret)" in js
    assert "innerHTML" not in js


# ---------------- Списки: валидация и запись ----------------

def test_list_validation_rejects_bad_lines(panel):
    body = "# комментарий\n+.good.com\n\nbad..dom\n-x.com\nплохой.домен\nunder_score.com\n"
    response = panel.client.put("/api/lists/geo-custom", content=body, auth=AUTH)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("Некорректные строки (первые 3): ")
    assert "4: 'bad..dom'" in detail
    assert "5: '-x.com'" in detail
    assert "6: 'плохой.домен'" in detail
    assert "7:" not in detail


def test_list_put_valid_and_atomic(panel):
    body = "+.good.com\n#comment\n\nexample.COM\n"
    response = panel.client.put("/api/lists/geo-custom", content=body, auth=AUTH)
    assert response.status_code == 200
    assert response.json()["ok"] is True

    target = panel.etc / "lists" / "geo-custom.txt"
    assert target.read_text(encoding="utf-8") == body
    assert oct(target.stat().st_mode & 0o777) == "0o644"
    assert list((panel.etc / "lists").glob(".tmp-*")) == []


def test_list_put_too_large_413(panel):
    response = panel.client.put("/api/lists/geo-custom", content="a" * 200_001, auth=AUTH)
    assert response.status_code == 413
    assert response.json()["detail"] == "Список больше 200 КБ"
    assert not (panel.etc / "lists" / "geo-custom.txt").exists()


def test_list_put_chunked_too_large_413(panel):
    """Chunked-тело без Content-Length не обходит лимит: 413 и без записи."""
    def chunks():
        for _ in range(3):
            yield b"a" * 100_000

    response = panel.client.put("/api/lists/geo-custom", content=chunks(), auth=AUTH)
    assert response.status_code == 413
    assert response.json()["detail"] == "Список больше 200 КБ"
    assert not (panel.etc / "lists" / "geo-custom.txt").exists()


def test_list_get_too_large_413(panel):
    target = panel.etc / "lists" / "geo-custom.txt"
    target.write_text("#" + "a" * 200_001 + "\n", encoding="utf-8")
    response = panel.client.get("/api/lists/geo-custom", auth=AUTH)
    assert response.status_code == 413
    assert response.json()["detail"] == "Список больше 200 КБ"


@pytest.mark.parametrize(
    ("domain", "valid"),
    [
        ("a-.com", False),
        ("a.b-.com", False),
        ("-a.com", False),
        ("a..com", False),
        ("a_b.com", False),
        ("a.com", True),
        ("a-b.com", True),
        ("+.ok.com", True),
        ("*.ok.com", True),
        ("sub.a-b.example.com", True),
        ("1a.com", True),
    ],
)
def test_domain_label_validation(panel, domain, valid):
    assert panel.module._valid_domain(domain) is valid


def test_list_put_rejects_trailing_hyphen_label(panel):
    response = panel.client.put("/api/lists/geo-custom", content="a-.com\n", auth=AUTH)
    assert response.status_code == 400
    assert "1: 'a-.com'" in response.json()["detail"]
    assert not (panel.etc / "lists" / "geo-custom.txt").exists()


def test_list_get_unknown_404(panel):
    assert panel.client.get("/api/lists/nope", auth=AUTH).status_code == 404


def test_list_put_unknown_404(panel):
    assert panel.client.put("/api/lists/nope", content="a.com\n", auth=AUTH).status_code == 404


# ---------------- Применение списков ----------------

def test_apply_runs_in_background_and_reports_job(panel):
    response = panel.client.post("/api/lists/geo-custom/apply", auth=AUTH)
    assert response.status_code == 200
    assert response.json() == {"started": True}

    job = panel.client.get("/api/jobs/apply", auth=AUTH)
    assert job.status_code == 200
    data = job.json()
    assert data["running"] is False
    assert data["ok"] is True
    assert data["started_at"] is not None
    assert data["finished_at"] is not None
    assert data["detail"]
    assert ["sudo", "-n", "/usr/local/sbin/routeweave-update-lists.sh"] in panel.calls


def test_apply_dpi_restarts_zapret(panel):
    response = panel.client.post("/api/lists/dpi/apply", auth=AUTH)
    assert response.status_code == 200
    job = panel.client.get("/api/jobs/apply", auth=AUTH).json()
    assert job["ok"] is True
    assert ["sudo", "-n", "systemctl", "restart", "zapret-nfqws"] in panel.calls


def test_apply_conflict_when_running(panel):
    panel.module.JOB.update(running=True, started_at="2026-01-01T00:00:00+00:00")
    try:
        response = panel.client.post("/api/lists/geo-custom/apply", auth=AUTH)
    finally:
        panel.module.JOB.update(running=False)
    assert response.status_code == 409
    assert response.json()["detail"] == "Применение уже идёт"


def test_apply_failure_sets_job_error(etc, monkeypatch):
    def failing(args, timeout=60):
        return subprocess.CompletedProcess(args, 1, "", "updater: нет прав")

    _module, client, _ = make_client(monkeypatch, handler=failing)
    client.post("/api/lists/geo-custom/apply", auth=AUTH)
    job = client.get("/api/jobs/apply", auth=AUTH).json()
    assert job["running"] is False
    assert job["ok"] is False
    assert "updater" in job["detail"]


# ---------------- Статус и sudo allowlist ----------------

def _clear_status_cache(module):
    module.STATUS_CACHE.update({"at": None, "payload": None})


def test_status_reports_config_and_tgws_state(panel):
    response = panel.client.get("/api/status", auth=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["config"]["server_ip"] == "192.168.1.10"
    assert data["config"]["tg_link_ip"] == "192.168.1.10"
    assert data["config"]["tg_link"] is None
    assert data["config"]["wg_subnet"] == "10.66.66.0/24"
    assert data["services"]["routeweave-tgwsproxy"] == "running"


def test_status_tg_link_with_secret(etc, monkeypatch):
    content = ENV_CONTENT + "TG_SECRET=deadbeef\nTG_LINK_IP=203.0.113.5\n"
    (etc / ".env").write_text(content, encoding="utf-8")
    _module, client, _ = make_client(monkeypatch)
    data = client.get("/api/status", auth=AUTH).json()
    assert data["config"]["tg_link_ip"] == "203.0.113.5"
    assert data["config"]["tg_link"] == "tg://proxy?server=203.0.113.5&port=1443&secret=deadbeef"


def test_status_tg_link_secret_percent_encoded(etc, monkeypatch):
    content = ENV_CONTENT + "TG_SECRET=a&b#c\nTG_LINK_IP=203.0.113.5\n"
    (etc / ".env").write_text(content, encoding="utf-8")
    _module, client, _ = make_client(monkeypatch)
    link = client.get("/api/status", auth=AUTH).json()["config"]["tg_link"]
    assert "secret=a%26b%23c" in link
    assert "#" not in link
    assert "server=203.0.113.5" in link


def test_status_cached_and_single_flight(etc, monkeypatch):
    """Два параллельных запроса считают статус один раз; после TTL — снова."""
    module, client, _ = make_client(monkeypatch)
    counter = {"n": 0}
    counter_lock = threading.Lock()

    def heavy(path, method="GET"):
        with counter_lock:
            counter["n"] += 1
        time.sleep(0.2)
        return 200, "test"

    monkeypatch.setattr(module, "mihomo_api", heavy)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.get, "/api/status", auth=AUTH) for _ in range(2)]
        responses = [future.result(timeout=15) for future in futures]
    assert all(response.status_code == 200 for response in responses)
    assert counter["n"] == 1

    # В пределах TTL — из кэша, тяжёлая функция не вызывается.
    assert client.get("/api/status", auth=AUTH).status_code == 200
    assert counter["n"] == 1

    # TTL истёк (в тесте сдвигаем отметку) — статус считается заново.
    module.STATUS_CACHE["at"] = None
    assert client.get("/api/status", auth=AUTH).status_code == 200
    assert counter["n"] == 2


def test_status_cache_returns_copy(panel):
    data = panel.client.get("/api/status", auth=AUTH).json()
    data["services"]["mihomo"] = "изменено"
    data["config"]["server_ip"] = "0.0.0.0"
    fresh = panel.client.get("/api/status", auth=AUTH).json()
    assert fresh["services"]["mihomo"] == "active"
    assert fresh["config"]["server_ip"] == "192.168.1.10"


def test_status_concurrency_limited_429(panel):
    assert panel.module.STATUS_SEM.acquire(blocking=False)
    assert panel.module.STATUS_SEM.acquire(blocking=False)
    try:
        response = panel.client.get("/api/status", auth=AUTH)
    finally:
        panel.module.STATUS_SEM.release()
        panel.module.STATUS_SEM.release()
    assert response.status_code == 429


def test_probe_concurrency_limited_429(panel):
    assert panel.module.PROBE_SEM.acquire(blocking=False)
    try:
        response = panel.client.post("/api/probe", auth=AUTH)
    finally:
        panel.module.PROBE_SEM.release()
    assert response.status_code == 429


def test_probes_last_updated_under_lock(panel, monkeypatch):
    results = {"x": {"ok": True, "detail": "ok"}}
    monkeypatch.setattr(panel.module, "PROBES", {"x": lambda: results["x"]})
    response = panel.client.post("/api/probe", auth=AUTH)
    assert response.status_code == 200
    assert response.json()["results"]["x"] == results["x"]
    assert panel.module.PROBES_LAST["results"]["x"] == results["x"]


def test_rate_limit_per_ip_429(etc, monkeypatch):
    # install.sh пишет PANEL_RATE_LIMIT в формате N/M.
    monkeypatch.setenv("PANEL_RATE_LIMIT", "3/60")
    _module, client, _ = make_client(monkeypatch)
    for _ in range(3):
        assert client.get("/api/jobs/apply", auth=AUTH).status_code == 200
    response = client.get("/api/jobs/apply", auth=AUTH)
    assert response.status_code == 429
    assert response.json()["detail"] == "Слишком много запросов"


def test_rate_limit_plain_integer_and_garbage(etc, monkeypatch):
    monkeypatch.setenv("PANEL_RATE_LIMIT", "garbage")
    module, _client, _ = make_client(monkeypatch)
    assert (module.PANEL_RATE_LIMIT, module.PANEL_RATE_WINDOW) == (120, 60)
    assert module._parse_rate_limit("7/5", 120, 60) == (7, 5)
    assert module._parse_rate_limit("7", 120, 60) == (7, 60)
    assert module._parse_rate_limit("0/5", 120, 60) == (120, 60)


def test_auth_failure_rate_limit_429(etc, monkeypatch):
    monkeypatch.setenv("PANEL_AUTH_RATE_LIMIT", "2")
    _module, client, _ = make_client(monkeypatch)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status").status_code == 429


@pytest.mark.parametrize(
    "state",
    ["running", "exited", "absent", "created", "restarting", "paused", "dead"],
)
def test_tgws_state_whitelist(panel, monkeypatch, state):
    def fake_run_cmd(args, timeout=60):
        return subprocess.CompletedProcess(args, 0, state + "\n", "")

    monkeypatch.setattr(panel.module, "run_cmd", fake_run_cmd)
    assert panel.module.tgws_state() == state
    expected = state in {"running", "created", "restarting", "paused"}
    assert panel.module.tgws_enabled() is expected


def test_tgws_state_unknown_word(panel, monkeypatch):
    monkeypatch.setattr(
        panel.module, "run_cmd", lambda args, timeout=60: subprocess.CompletedProcess(args, 0, "weird\n", "")
    )
    assert panel.module.tgws_state() == "unknown"
    assert panel.module.tgws_enabled() is False


def test_apply_rollback_when_add_task_fails(panel, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("background сломан")

    monkeypatch.setattr(panel.module.BackgroundTasks, "add_task", boom)
    response = panel.client.post("/api/lists/geo-custom/apply", auth=AUTH)
    assert response.status_code == 500
    assert panel.module.JOB["running"] is False


# ---------------- Tailscale: статус, оценка скорости, отказ helper'а ----------------

def _tailscale_calls(calls):
    """Вызовы helper'а Tailscale (путь — отдельный элемент списка аргументов)."""
    return [
        call
        for call in calls
        if any("routeweave-tailscale-state.sh" in part for part in call)
    ]


def _safe_tailscale_handler(override):
    """Обёртка default_handler: helper Tailscale отвечает через override."""

    def handler(args, timeout=60):
        if " ".join(args).endswith("routeweave-tailscale-state.sh"):
            return override(args)
        return default_handler(args, timeout)

    return handler


def _assert_tailscale_unknown(data):
    tailscale = data["tailscale"]
    assert tailscale["installed"] is False
    assert tailscale["daemon"] == "unknown"
    assert tailscale["error"]
    assert data["services"]["tailscaled"] == "unknown"


def test_status_reports_tailscale(panel):
    response = panel.client.get("/api/status", auth=AUTH)
    assert response.status_code == 200
    data = response.json()

    tailscale = data["tailscale"]
    expected_types = {
        "installed": bool,
        "daemon": str,
        "backend": str,
        "self_ip": str,
        "advertised": bool,
        "approved": bool,
        "peers": int,
        "iface": str,
        "rx_bytes": int,
        "tx_bytes": int,
        "error": str,
        "rx_rate": int,
        "tx_rate": int,
    }
    for name, expected in expected_types.items():
        assert name in tailscale, name
        assert isinstance(tailscale[name], expected), name

    assert tailscale["installed"] is True
    assert tailscale["daemon"] == "active"
    assert tailscale["backend"] == "Running"
    assert tailscale["self_ip"] == "100.64.0.10"
    assert tailscale["advertised"] is True
    assert tailscale["approved"] is True
    assert tailscale["peers"] == 1
    assert tailscale["iface"] == "tailscale0"
    assert tailscale["rx_bytes"] == 1000
    assert tailscale["tx_bytes"] == 2000
    assert tailscale["error"] == ""
    assert data["services"]["tailscaled"] == "active"

    helper_calls = _tailscale_calls(panel.calls)
    assert len(helper_calls) == 1
    assert helper_calls[0][:2] == ["sudo", "-n"]

    # Первый опрос: предыдущих счётчиков нет — скорость неизвестна, но не ошибка.
    assert tailscale["rx_rate"] == 0
    assert tailscale["tx_rate"] == 0


def test_tailscale_rates_second_poll(etc, monkeypatch):
    counters = {"rx": 1000, "tx": 2000}

    def handler(args, timeout=60):
        joined = " ".join(args)
        if "is-active" in args:
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        if joined.endswith("routeweave-tgws-state.sh"):
            return subprocess.CompletedProcess(args, 0, "running\n", "")
        if joined.endswith("routeweave-tailscale-state.sh"):
            payload = dict(
                TAILSCALE_FIELDS,
                rx_bytes=counters["rx"],
                tx_bytes=counters["tx"],
            )
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    module, client, _ = make_client(monkeypatch, handler=handler)
    times = iter([100.0, 102.0, 106.0])
    monkeypatch.setattr(module, "_tailscale_now", lambda: next(times))

    first = client.get("/api/status", auth=AUTH).json()["tailscale"]
    assert (first["rx_rate"], first["tx_rate"]) == (0, 0)

    # +1000 Б за 2 с и +2000 Б за 2 с.
    counters["rx"] += 1000
    counters["tx"] += 2000
    _clear_status_cache(module)
    second = client.get("/api/status", auth=AUTH).json()["tailscale"]
    assert second["rx_bytes"] == 2000
    assert second["tx_bytes"] == 4000
    assert (second["rx_rate"], second["tx_rate"]) == (500, 1000)

    # Счётчики сброшены (интерфейс пересоздан) — деление на отрицательную
    # дельту запрещено, скорость остаётся нулевой.
    counters["rx"] = 500
    counters["tx"] = 600
    _clear_status_cache(module)
    third = client.get("/api/status", auth=AUTH).json()["tailscale"]
    assert (third["rx_rate"], third["tx_rate"]) == (0, 0)


def test_tailscale_helper_failure_is_safe(etc, monkeypatch):
    def failing(args):
        return subprocess.CompletedProcess(args, 1, "", "helper: ошибка")

    _module, client, _ = make_client(
        monkeypatch, handler=_safe_tailscale_handler(failing)
    )
    response = client.get("/api/status", auth=AUTH)
    assert response.status_code == 200
    _assert_tailscale_unknown(response.json())


def test_tailscale_helper_missing_127(etc, monkeypatch):
    def missing(args):
        return subprocess.CompletedProcess(args, 127, "", "FileNotFoundError")

    _module, client, _ = make_client(
        monkeypatch, handler=_safe_tailscale_handler(missing)
    )
    response = client.get("/api/status", auth=AUTH)
    assert response.status_code == 200
    _assert_tailscale_unknown(response.json())


def test_tailscale_helper_garbage_json(etc, monkeypatch):
    def garbage(args):
        return subprocess.CompletedProcess(args, 0, "not json", "")

    _module, client, _ = make_client(
        monkeypatch, handler=_safe_tailscale_handler(garbage)
    )
    response = client.get("/api/status", auth=AUTH)
    assert response.status_code == 200
    _assert_tailscale_unknown(response.json())


def test_tailscale_card_static_assets(panel):
    html = panel.client.get("/", auth=AUTH).text
    js = panel.client.get("/static/app.js", auth=AUTH).text

    for token in ("tailscaled", "renderTailscale", "ts-card", "i-globe"):
        assert token in js, token
    for token in ('id="ts-card"', 'id="ts-list"', "i-globe"):
        assert token in html, token

    # Карточка строится через DOM API: inline-HTML и document.write запрещены.
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in js


def test_privileged_calls_only_sudo(panel):
    panel.client.get("/api/status", auth=AUTH)
    panel.client.post("/api/restart/mihomo", auth=AUTH)
    panel.client.post("/api/restart/tgwsproxy", auth=AUTH)
    panel.client.post("/api/apply/zapret", auth=AUTH)
    for call in panel.calls:
        if call[:2] == ["systemctl", "is-active"]:
            continue
        assert call[:2] == ["sudo", "-n"], call


def test_sudo_failure_returns_502(etc, monkeypatch):
    def failing(args, timeout=60):
        if "is-active" in args:
            return subprocess.CompletedProcess(args, 0, "inactive", "")
        if " ".join(args).endswith("routeweave-tgws-state.sh"):
            return subprocess.CompletedProcess(args, 0, "absent", "")
        return subprocess.CompletedProcess(args, 1, "", "sudo: пароль не требуется")

    _module, client, _ = make_client(monkeypatch, handler=failing)
    response = client.post("/api/restart/mihomo", auth=AUTH)
    assert response.status_code == 502
    assert "пароль не требуется" in response.json()["detail"]
