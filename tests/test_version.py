import subprocess
from pathlib import Path

import pytest

from app import version
from app.mcp.server import mcp

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_build_version_is_the_checkout_commit() -> None:
    """The version a client sees is the actual commit running, not a
    placeholder: compared against git directly, so the test fails if the
    module stops reading the checkout."""
    expected = subprocess.run(
        ["git", "describe", "--always", "--dirty", "--abbrev=12"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert version._from_git() == expected  # pyright: ignore[reportPrivateUsage]
    assert expected  # a real value, not an empty string


def test_mcp_handshake_reports_the_build_version() -> None:
    """MCP clients read serverInfo.version at initialize; it was ''."""
    assert mcp.version == version.BUILD_VERSION != ""


def test_no_git_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A build with no .git directory and no APP_BUILD_VERSION says
    "unknown" rather than crashing or claiming a commit."""
    monkeypatch.setattr(version, "_REPO_ROOT", tmp_path)
    assert version._from_git() is None  # pyright: ignore[reportPrivateUsage]
