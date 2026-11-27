"""Settings that must be safe by default, or refuse to start when a real
deployment sets them unsafely."""

import pytest
from pydantic import ValidationError

from app.config import Settings

URLS = {"database_url": "postgresql+asyncpg://x@localhost/x", "target_database_url": "x"}


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **URLS, **values)  # pyright: ignore[reportCallIssue]


def test_the_console_cookie_is_secure_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_CONSOLE_COOKIE_HTTPS_ONLY", raising=False)

    assert _settings().console_cookie_https_only is True


def test_a_public_server_with_an_insecure_cookie_refuses_to_start() -> None:
    with pytest.raises(ValidationError, match="set APP_CONSOLE_COOKIE_HTTPS_ONLY=true"):
        _settings(mcp_allowed_hosts="localhost, mcp.example.com", console_cookie_https_only=False)


def test_a_local_server_may_turn_the_secure_cookie_off() -> None:
    settings = _settings(mcp_allowed_hosts="localhost", console_cookie_https_only=False)

    assert settings.console_cookie_https_only is False
