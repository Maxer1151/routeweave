"""Инварианты docker-compose.yml (опциональный tg-ws-proxy) без Docker.

Проверяется то, что нельзя увидеть в остальных тестах: пины образа по digest,
публикация порта только на LAN-адрес, hardening контейнера, изоляция секретов
через отдельный env_file и совместимость имён с helper-скриптами панели.
Сам Docker здесь не нужен — только разбор YAML.
"""

import re
from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def compose_text():
    return COMPOSE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose(compose_text):
    data = yaml.safe_load(compose_text)
    assert isinstance(data, dict), "docker-compose.yml: ожидался YAML-объект"
    return data


@pytest.fixture(scope="module")
def service(compose):
    assert "tg-ws-proxy" in compose["services"], "сервис tg-ws-proxy пропал"
    return compose["services"]["tg-ws-proxy"]


def test_image_pinned_by_digest(service):
    """Образ задан как тег@sha256:<64 hex>, тег не latest (Q-10)."""
    image = service["image"]
    name, sep, digest = image.partition("@sha256:")
    assert sep, f"образ должен быть запинен по digest: {image}"
    assert DIGEST_RE.fullmatch(digest), f"digest не 64 hex: {digest}"
    assert ":" in name, f"в образе нет тега: {name}"
    tag = name.rsplit(":", 1)[1]
    assert tag != "latest", "mutable-тег latest запрещён"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", tag), f"тег не версия образа: {tag}"


def test_no_latest_without_digest_in_file(compose_text):
    """Статическая проверка: ни одна строка image: не задаёт :latest."""
    for line in compose_text.splitlines():
        match = re.match(r"\s*image:\s*(\S+)", line)
        if match:
            assert ":latest" not in match.group(1), line.strip()


def test_port_published_on_server_ip_only(service):
    """Порт 1443 публикуется на ${SERVER_IP}, а не на 0.0.0.0 (B-05/B-06)."""
    ports = service["ports"]
    assert isinstance(ports, list) and len(ports) == 1, ports
    mapping = str(ports[0])
    assert "${SERVER_IP" in mapping, mapping
    assert not mapping.startswith(("1443:", "0.0.0.0", ":")), mapping
    assert mapping.endswith(":1443:1443"), mapping


def test_read_only_and_no_capabilities(service):
    """Корневая ФС только для чтения, capabilities сброшены, эскалация закрыта."""
    assert service.get("read_only") is True
    assert service.get("cap_drop") == ["ALL"]
    # Порт 1443 > 1024: NET_BIND_SERVICE не нужен, добавлять его нельзя.
    assert "NET_BIND_SERVICE" not in str(service.get("cap_add", []))
    assert service.get("security_opt") == ["no-new-privileges:true"]


def test_tmpfs_mount_is_restricted(service):
    """Единственный tmpfs — /tmp, без exec и suid, с ограничением размера."""
    tmpfs = service.get("tmpfs")
    assert tmpfs == ["/tmp:rw,noexec,nosuid,size=16m"], tmpfs


def test_resource_limits(service):
    """Лимиты заданы ключами non-swarm, без смешивания с deploy.resources."""
    assert "deploy" not in service, "deploy.resources не применяется вне swarm"
    assert service.get("mem_limit") == "256m"
    assert service.get("cpus") == 1.0
    assert service.get("pids_limit") == 128


def test_logging_rotation(service):
    """Логи ограничены по размеру и количеству файлов (Q-16)."""
    logging = service["logging"]
    assert logging["driver"] == "json-file"
    assert logging["options"]["max-size"] == "10m"
    assert logging["options"]["max-file"] == "3"


def test_restart_and_stop_grace(service):
    assert service.get("restart") == "unless-stopped"
    assert service.get("stop_grace_period") == "10s"


def test_pull_policy_left_default(service):
    """pull_policy не задаём: digest-пин и так фиксирует образ."""
    assert "pull_policy" not in service


def test_env_file_is_tgws_only(service):
    """Секреты tg-ws-proxy лежат в отдельном файле, а не в общем .env."""
    env_file = service["env_file"]
    paths = [entry["path"] if isinstance(entry, dict) else entry for entry in env_file]
    assert paths == ["/etc/routeweave/.env.tgws"], paths
    assert "/etc/routeweave/.env" not in paths, "общий .env не должен попадать в контейнер"


def test_project_and_container_names(compose, service):
    """Имена завязаны на helper-скрипты панели и uninstall.sh."""
    assert compose.get("name") == "routeweave"
    assert service.get("container_name") == "routeweave-tgwsproxy"


def test_healthcheck_or_documented_absence(service, compose_text):
    """Проверка живости либо задана, либо осознанно описана в комментарии."""
    healthcheck = service.get("healthcheck")
    if healthcheck is None:
        # В образе нет sh/curl/wget/nc: проверить порт изнутри нечем.
        assert "nc -z" in compose_text, "нет healthcheck и нет описания внешней проверки"
        return
    assert healthcheck.get("test"), "healthcheck без test"
    assert healthcheck.get("start_period"), "healthcheck без start_period"
