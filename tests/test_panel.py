"""Тесты панели Routeweave: авторизация, CSRF, заголовки, списки, фоновое применение.

Тесты не требуют root и сети: внешние команды всегда подменяются стабом
``run_cmd``, а API mihomo — заглушкой. Конфигурация готовится в tmp_path,
модуль ``panel/app.py`` загружается заново с ``ROUTEWEAVE_ETC`` из окружения.
"""

import base64
import importlib.util
import logging
import subprocess
import sys
import warnings
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


def default_handler(args, timeout=60):
    """Стаб внешних команд: сервисы активны, helper'ы отвечают успехом."""
    joined = " ".join(args)
    if "is-active" in args:
        return subprocess.CompletedProcess(args, 0, "active\n", "")
    if joined.endswith("routeweave-tgws-state.sh"):
        return subprocess.CompletedProcess(args, 0, "running\n", "")
    if joined.endswith("routeweave-update-lists.sh"):
        return subprocess.CompletedProcess(args, 0, "списки обновлены\n", "")
    return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """Каталог конфигурации с .env; ROUTEWEAVE_ETC виден при импорте модуля."""
    etc_dir = tmp_path / "etc"
    (etc_dir / "lists").mkdir(parents=True)
    (etc_dir / ".env").write_text(ENV_CONTENT, encoding="utf-8")
    monkeypatch.setenv("ROUTEWEAVE_ETC", str(etc_dir))
    return etc_dir


def make_client(monkeypatch, handler=default_handler):
    """Загружает panel/app.py как отдельный модуль и подменяет run_cmd."""
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
    return module, TestClient(module.app), calls


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


def test_auth_checked_before_csrf(panel):
    response = panel.client.post(
        "/api/restart/mihomo", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 401


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


def test_ui_csp_allows_mihomo_api(panel):
    """zashboard на /ui ходит в API mihomo на другой порт и в WS."""
    response = panel.client.get("/ui/", auth=AUTH)
    assert response.status_code in (200, 404)
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self' http: https: ws: wss:" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert response.headers["x-frame-options"] == "DENY"


def test_panel_csp_stays_strict(panel):
    response = panel.client.get("/", auth=AUTH)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "http:" not in csp
    assert "'unsafe-inline'" not in csp


def test_uixyz_gets_panel_csp(panel):
    """Похожий на /ui путь не должен получать ослабленную CSP."""
    response = panel.client.get("/uixyz", auth=AUTH)
    assert response.status_code in (200, 404)
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self'" in csp
    assert "http:" not in csp
    assert "'unsafe-inline'" not in csp


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
