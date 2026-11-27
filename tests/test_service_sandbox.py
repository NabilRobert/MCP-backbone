"""deploy/mcp-backbone.service keeps its sandbox. The service runs as a
user in the docker group, which is as good as root, so losing one of
these lines would quietly hand a bug in the app far more than it needs.
The server checks the file itself with systemd-analyze; this catches an
edit that drops a line before it gets there."""

import configparser
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "mcp-backbone.service"
REQUIRED = {
    "NoNewPrivileges": "true",
    "ProtectSystem": "strict",
    "ProtectHome": "tmpfs",
    "PrivateTmp": "true",
    "CapabilityBoundingSet": "",
    "RestrictAddressFamilies": "AF_INET AF_INET6 AF_UNIX AF_NETLINK",
}


def _service_section() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # pyright: ignore[reportAttributeAccessIssue] -- keep the keys' case
    parser.read(UNIT)
    return parser["Service"]


@pytest.mark.parametrize(("directive", "value"), REQUIRED.items())
def test_the_service_keeps_its_sandbox_directive(directive: str, value: str) -> None:
    assert _service_section().get(directive) == value


def test_the_service_has_a_memory_ceiling() -> None:
    assert _service_section().get("MemoryMax")


def test_the_service_hides_the_docker_socket() -> None:
    assert "/run/docker.sock" in (_service_section().get("InaccessiblePaths") or "")


def test_uvicorn_listens_only_on_the_docker_bridge() -> None:
    exec_start = _service_section().get("ExecStart") or ""
    assert "--host 172.18.0.1 " in exec_start
    assert "0.0.0.0" not in exec_start
