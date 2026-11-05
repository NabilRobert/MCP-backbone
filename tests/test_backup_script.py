"""deploy/backup_bookkeeping.sh, run for real under bash with stand-ins for
pg_dump, age, rclone and curl on PATH, in a throwaway HOME. Checks what
matters if it goes wrong: which key the backup is encrypted to, and that no
plaintext dump is ever left on disk."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "backup_bookkeeping.sh"
ENV_KEY = "age1testkeyfromenvfile"

# Each stand-in logs its arguments, so a test can see what the script did.
# pg_dump and age write a file where the real ones would, and rclone logs
# which backup files exist while it uploads. FAIL_<TOOL> makes that tool
# fail instead.
STUBS = {
    "pg_dump": 'for a in "$@"; do case "$a" in --file=*) echo dump > "${a#--file=}";; esac; done',
    "age": 'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done; echo enc > "$out"',
    "rclone": 'echo "files at upload:" $(ls "$HOME/backups") >> "$HOME/calls.log"',
    "curl": "true",
}


def _write_stubs(bin_dir: Path, log: Path) -> None:
    bin_dir.mkdir()
    for tool, body in STUBS.items():
        stub = bin_dir / tool
        upper = tool.upper()
        stub.write_text(
            f'#!/usr/bin/env bash\necho "{tool} $*" >> "{log}"\n'
            f'[ -n "${{FAIL_{upper}:-}}" ] && exit 1\n{body}\n'
        )
        stub.chmod(0o755)


def _env_file(home: Path, *, with_age_key: bool = True) -> None:
    lines = [
        "APP_DATABASE_URL=postgresql+asyncpg://u:p@localhost/db",
        "AWS_ACCESS_KEY_ID=id",
        "AWS_SECRET_ACCESS_KEY=secret",
        "S3_BUCKET=bucket",
        "S3_REGION=region",
    ]
    if with_age_key:
        lines.append(f"AGE_PUBLIC_KEY={ENV_KEY}")
    (home / "MCP-backbone").mkdir()
    (home / "MCP-backbone" / ".env").write_text("\n".join(lines) + "\n")


def _run_backup(home: Path, **fail: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "PATH": f"{home / 'bin'}:{os.environ['PATH']}", **fail}
    # The tests check the exit code themselves, so a failure is a result, not an error.
    return subprocess.run(
        ["bash", str(SCRIPT)], check=False, env=env, capture_output=True, text=True
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "backups").mkdir()
    _write_stubs(tmp_path / "bin", tmp_path / "calls.log")
    return tmp_path


def _backup_files(home: Path) -> list[str]:
    return sorted(path.name for path in (home / "backups").iterdir())


def _calls(home: Path) -> str:
    log = home / "calls.log"
    return log.read_text() if log.exists() else ""


def test_a_backup_keeps_only_the_encrypted_dump(home: Path) -> None:
    _env_file(home)
    result = _run_backup(home)

    assert result.returncode == 0, result.stderr
    files = _backup_files(home)
    assert len(files) == 1
    assert files[0].endswith(".dump.age")


def test_a_backup_is_encrypted_to_the_key_in_env(home: Path) -> None:
    _env_file(home)
    _run_backup(home)

    assert f"age -r {ENV_KEY} " in _calls(home)


def test_the_plaintext_is_deleted_before_the_upload(home: Path) -> None:
    _env_file(home)
    _run_backup(home)

    upload_lines = [
        line for line in _calls(home).splitlines() if line.startswith("files at upload:")
    ]
    assert upload_lines
    for line in upload_lines:
        assert not any(name.endswith(".dump") for name in line.split())


def test_plaintext_left_by_a_killed_run_is_deleted(home: Path) -> None:
    _env_file(home)
    leftover = home / "backups" / "jarvis_mcp_bookkeeping-20260101-031700.dump"
    leftover.write_text("plaintext")
    result = _run_backup(home)

    assert result.returncode == 0, result.stderr
    assert not leftover.exists()


def test_a_missing_age_key_stops_before_anything_is_dumped(home: Path) -> None:
    _env_file(home, with_age_key=False)
    result = _run_backup(home)

    assert result.returncode != 0
    assert "AGE_PUBLIC_KEY is missing" in result.stderr
    assert "pg_dump" not in _calls(home)


@pytest.mark.parametrize("failing_tool", ["FAIL_AGE", "FAIL_RCLONE"])
def test_a_failed_backup_leaves_no_plaintext_dump(home: Path, failing_tool: str) -> None:
    _env_file(home)
    result = _run_backup(home, **{failing_tool: "1"})

    assert result.returncode != 0
    assert [name for name in _backup_files(home) if not name.endswith(".dump.age")] == []
