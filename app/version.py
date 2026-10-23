"""Which build of the server is running, so a client (the eval harness,
an uptime monitor, a person) can tell two deploys apart. Without it, a
change in behaviour after a deploy looks identical to a change in the
client, and a benchmark run can't say what it was run against."""

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _from_git() -> str | None:
    """The checkout's commit, plus "-dirty" when the working tree has
    uncommitted changes (so a hand-edited server can't pass itself off
    as a clean commit). None if git or the .git directory isn't there."""
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--abbrev=12"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


# APP_BUILD_VERSION wins, for builds without a .git directory (e.g. a
# container image stamped at build time); otherwise the git checkout.
# Worked out once at import: it can't change while the process runs.
BUILD_VERSION: str = os.environ.get("APP_BUILD_VERSION") or _from_git() or "unknown"
