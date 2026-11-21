"""deploy/firewall.sh, run for real under bash with stand-ins for sudo,
ufw, systemd-run and docker on PATH, in a throwaway HOME. The server is
shared, so what matters is that it only ever adds this project's rules:
never a reset, never another project's rule rewritten."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "firewall.sh"

# ufw's status reads UFW_STATUS (a file the test writes), as the real one
# would print the live rules. Every stand-in logs its arguments.
STUBS = {
    "sudo": 'exec "$@"',
    "ufw": '[ "$1" = status ] && cat "$UFW_STATUS"; true',
    "systemd-run": "true",
    "systemctl": "true",
    "docker": '[ -z "${CADDY_CANT_REACH:-}" ]',
}
ACTIVE_WITH_ANOTHER_PROJECTS_RULES = """Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere                   # tracker
443/tcp                    ALLOW       Anywhere                   # tracker
5432/tcp                   ALLOW       10.0.0.0/8                 # tracker
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool, body in STUBS.items():
        stub = bin_dir / tool
        stub.write_text(
            f'#!/usr/bin/env bash\necho "{tool} $*" >> "{tmp_path}/calls.log"\n{body}\n'
        )
        stub.chmod(0o755)
    (tmp_path / "MCP-backbone").mkdir()
    (tmp_path / "MCP-backbone" / ".env").write_text("DEPLOY_CADDY_CONTAINER=caddy\n")
    return tmp_path


def _run(home: Path, ufw_status: str, **extra_env: str) -> list[str]:
    """Runs the script and returns the ufw commands it ran."""
    (home / "status.txt").write_text(ufw_status)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{home / 'bin'}:{os.environ['PATH']}",
        "UFW_STATUS": str(home / "status.txt"),
        **extra_env,
    }
    subprocess.run(["bash", str(SCRIPT)], check=False, env=env, capture_output=True, text=True)
    calls = (home / "calls.log").read_text().splitlines()
    return [call for call in calls if call.startswith("ufw ") and call != "ufw status verbose"]


def test_it_never_resets_the_firewall(home: Path) -> None:
    ufw_calls = _run(home, ACTIVE_WITH_ANOTHER_PROJECTS_RULES)

    assert not [call for call in ufw_calls if "reset" in call]


def test_on_an_active_firewall_it_only_adds_its_own_rules(home: Path) -> None:
    ufw_calls = _run(home, ACTIVE_WITH_ANOTHER_PROJECTS_RULES)

    changes = [call for call in ufw_calls if call != "ufw status"]
    assert changes == [
        "ufw allow 80/tcp comment mcp-backbone",
        "ufw allow 443/udp comment mcp-backbone",
        "ufw allow from 172.18.0.0/16 to any port 8443 proto tcp comment mcp-backbone",
    ]


def test_on_an_inactive_firewall_it_sets_defaults_and_enables(home: Path) -> None:
    ufw_calls = _run(home, "Status: inactive\n")

    assert "ufw default deny incoming" in ufw_calls
    assert "ufw --force enable" in ufw_calls


def test_a_failed_reach_check_leaves_an_already_active_firewall_on(home: Path) -> None:
    """Turning ufw off would drop every other project's protection too."""
    ufw_calls = _run(home, ACTIVE_WITH_ANOTHER_PROJECTS_RULES, CADDY_CANT_REACH="1")

    assert "ufw disable" not in ufw_calls
