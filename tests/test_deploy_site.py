"""Exercise SSH packaging and remote activation without network or privileged writes."""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MOCK_EXECUTABLE = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DEPLOY_TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "terraform":
    print(json.dumps({key: {"value": value} for key, value in {
        "static_ip_address": "192.0.2.10", "ssh_user": "ubuntu",
    }.items()}))
elif name == "ssh":
    command = " ".join(args)
    if "cloud-init" in command and os.environ.get("DEPLOY_TEST_INIT_FAILURE"):
        sys.exit(1)
    if "mktemp" in command:
        print("/tmp/the-bot-radio-test")
elif name == "scp":
    for argument in args[:-1]:
        source = pathlib.Path(argument)
        if source.is_file():
            shutil.copyfile(source, pathlib.Path(os.environ["DEPLOY_TEST_CAPTURE"]) / source.name)
"""


@pytest.fixture
def deploy_environment(tmp_path: Path) -> dict[str, str]:
    """Create an isolated repository, captured uploads, and executable mocks."""
    repository = tmp_path / "repository"
    (repository / "deploy").mkdir(parents=True)
    for script in ("deploy-site.sh", "remote-deploy.sh", "package.py"):
        source = REPOSITORY_ROOT / "deploy" / script
        if source.exists():
            shutil.copyfile(source, repository / "deploy" / script)
    for filename in ("pyproject.toml", "uv.lock", "README.md", ".python-version"):
        (repository / filename).write_text("test\n")
    source_directory = repository / "src" / "the_bot_radio"
    source_directory.mkdir(parents=True)
    (source_directory / "__init__.py").write_text('"""Test application."""\n')
    (source_directory / "__pycache__").mkdir()
    (source_directory / "__pycache__" / "private.pyc").write_text("private")
    for directory in (".git", ".venv", "data", "credentials"):
        (repository / directory).mkdir()
        (repository / directory / "private").write_text("secret")
    (repository / ".env").write_text("SECRET=hidden\n")
    (repository / "data" / "days").mkdir()
    for filename in ("personas.json", "inputs.json"):
        (repository / "data" / filename).write_text("[]")
    (repository / "data" / "days" / "2026-10-27.json").write_text("{}")
    (repository / "data" / "days" / "private.json").write_text("secret")
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for executable_name in ("terraform", "ssh", "scp"):
        executable = bin_directory / executable_name
        executable.write_text(MOCK_EXECUTABLE)
        executable.chmod(0o755)
    capture_directory = tmp_path / "uploads"
    capture_directory.mkdir()
    return {
        **{key: os.environ[key] for key in ("HOME", "TMPDIR") if key in os.environ},
        "PATH": f"{bin_directory}:{os.environ['PATH']}",
        "DEPLOY_TEST_REPOSITORY": str(repository),
        "DEPLOY_TEST_LOG": str(tmp_path / "commands.jsonl"),
        "DEPLOY_TEST_CAPTURE": str(capture_directory),
    }


def run_deploy(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    """Execute the actual deployment script with recorded SSH and transfer commands."""
    script = Path(environment["DEPLOY_TEST_REPOSITORY"]) / "deploy" / "deploy-site.sh"
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def read_commands(environment: dict[str, str]) -> list[list[str]]:
    """Load recorded subprocess calls in execution order."""
    return [
        json.loads(line) for line in Path(environment["DEPLOY_TEST_LOG"]).read_text().splitlines()
    ]


def archive_members(environment: dict[str, str], filename: str) -> set[str]:
    """Read the uploaded archive after the script cleans its temporary files."""
    with tarfile.open(Path(environment["DEPLOY_TEST_CAPTURE"]) / filename) as archive:
        return {
            member.name.removeprefix("./") for member in archive.getmembers() if member.isfile()
        }


def test_normal_deployment_excludes_credentials_and_data(
    deploy_environment: dict[str, str],
) -> None:
    """An explicit package includes build inputs without local state or data."""
    result = run_deploy(deploy_environment)
    assert result.returncode == 0, result.stderr
    members = archive_members(deploy_environment, "application.tar.gz")
    assert {"pyproject.toml", "uv.lock", "README.md", ".python-version"} <= members
    assert "src/the_bot_radio/__init__.py" in members
    assert not any(
        name.startswith(("data/", ".git/", ".venv/", "credentials/"))
        or "__pycache__" in name
        or name == ".env"
        for name in members
    )
    assert not (Path(deploy_environment["DEPLOY_TEST_CAPTURE"]) / "seed.tar.gz").exists()
    commands = read_commands(deploy_environment)
    assert any("cloud-init" in " ".join(command) for command in commands)
    assert any("remote-deploy.sh" in " ".join(command) for command in commands)
    assert not any(command[0] == "aws" for command in commands)


def test_seed_transfers_only_allowed_data(deploy_environment: dict[str, str]) -> None:
    """The seed archive omits unrelated records and private source material."""
    result = run_deploy(deploy_environment, "--seed-data")
    assert result.returncode == 0, result.stderr
    assert archive_members(deploy_environment, "seed.tar.gz") == {
        "personas.json",
        "inputs.json",
        "days/2026-10-27.json",
    }


def test_identity_file_is_passed_to_both_ssh_commands(deploy_environment: dict[str, str]) -> None:
    """Use the selected private key for commands and transfers."""
    identity_file = Path(deploy_environment["DEPLOY_TEST_REPOSITORY"]) / "operator key"
    identity_file.write_text("private")
    result = run_deploy(deploy_environment, "--identity-file", str(identity_file))
    assert result.returncode == 0, result.stderr
    commands = [
        command for command in read_commands(deploy_environment) if command[0] in {"ssh", "scp"}
    ]
    assert commands
    assert all(command[command.index("-i") + 1] == str(identity_file) for command in commands)


def test_initialization_failure_prevents_activation(deploy_environment: dict[str, str]) -> None:
    """Cloud-init errors stop deployment before the application is activated."""
    deploy_environment["DEPLOY_TEST_INIT_FAILURE"] = "1"
    result = run_deploy(deploy_environment)
    assert result.returncode != 0
    assert "Instance initialization failed" in result.stderr
    assert any(
        "cloud-init status --long" in " ".join(command)
        and "/var/log/cloud-init-output.log" in " ".join(command)
        for command in read_commands(deploy_environment)
    )
    assert not any(
        "sudo bash" in " ".join(command) for command in read_commands(deploy_environment)
    )


REMOTE_MOCK_EXECUTABLE = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DEPLOY_TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "flock" and os.environ.get("DEPLOY_TEST_LOCKED"):
    sys.exit(1)
elif name == "runuser" and os.environ.get("DEPLOY_TEST_INSTALL_FAILURE"):
    sys.exit(1)
elif name == "systemctl" and args[0] == "restart" and os.environ.get("DEPLOY_TEST_RESTART_FAILURE"):
    marker = pathlib.Path(os.environ["DEPLOY_TEST_CURL_COUNTER"] + ".restart")
    if not marker.exists():
        marker.write_text("failed")
        sys.exit(1)
elif name == "mv":
    source, destination = args[-2:]
    os.replace(source, destination)
elif name == "curl":
    counter_path = pathlib.Path(os.environ["DEPLOY_TEST_CURL_COUNTER"])
    count = int(counter_path.read_text()) if counter_path.exists() else 0
    counter_path.write_text(str(count + 1))
    if count < int(os.environ.get("DEPLOY_TEST_FAILED_HEALTH_CHECKS", "0")):
        sys.exit(22)
"""


@pytest.fixture
def remote_environment(tmp_path: Path) -> dict[str, str]:
    """Sandbox remote paths and replace privileged or network executables."""
    app_root = tmp_path / "app"
    state_root = tmp_path / "state"
    (app_root / "releases" / "previous").mkdir(parents=True)
    (app_root / "current").symlink_to(app_root / "releases" / "previous")
    (state_root / "data").mkdir(parents=True)
    staging_directory = tmp_path / "staging"
    staging_directory.mkdir()
    for archive_name, files in {
        "application.tar.gz": {"pyproject.toml": "test"},
        "seed.tar.gz": {"personas.json": "[]", "inputs.json": "[]"},
    }.items():
        source_directory = tmp_path / archive_name.removesuffix(".tar.gz")
        source_directory.mkdir()
        with tarfile.open(staging_directory / archive_name, "w:gz") as archive:
            for filename, content in files.items():
                source_path = source_directory / filename
                source_path.write_text(content)
                archive.add(source_path, arcname=filename)
    script_path = tmp_path / "remote-deploy.sh"
    script = (REPOSITORY_ROOT / "deploy" / "remote-deploy.sh").read_text()
    script = script.replace("/opt/the-bot-radio", str(app_root))
    script = script.replace("/var/lib/the-bot-radio", str(state_root))
    script_path.write_text(script)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for executable_name in ("flock", "runuser", "chown", "mv", "systemctl", "curl", "sleep"):
        executable = bin_directory / executable_name
        executable.write_text(REMOTE_MOCK_EXECUTABLE)
        executable.chmod(0o755)
    return {
        **{key: os.environ[key] for key in ("HOME", "TMPDIR") if key in os.environ},
        "PATH": f"{bin_directory}:{os.environ['PATH']}",
        "DEPLOY_TEST_SCRIPT": str(script_path),
        "DEPLOY_TEST_APP_ROOT": str(app_root),
        "DEPLOY_TEST_STATE_ROOT": str(state_root),
        "DEPLOY_TEST_STAGING": str(staging_directory),
        "DEPLOY_TEST_LOG": str(tmp_path / "commands.jsonl"),
        "DEPLOY_TEST_CURL_COUNTER": str(tmp_path / "curl-count"),
    }


def run_remote(
    environment: dict[str, str], *, seed: bool = False
) -> subprocess.CompletedProcess[str]:
    """Execute remote activation logic against isolated files and command mocks."""
    return subprocess.run(
        [
            "bash",
            environment["DEPLOY_TEST_SCRIPT"],
            environment["DEPLOY_TEST_STAGING"],
            "a" * 32,
            str(seed).lower(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_remote_deployment_preserves_data_and_installs_locked_dependencies(
    remote_environment: dict[str, str],
) -> None:
    """Release activation updates code while retaining the persistent dataset."""
    existing_record = Path(remote_environment["DEPLOY_TEST_STATE_ROOT"]) / "data" / "existing.json"
    existing_record.write_text("private existing data")
    result = run_remote(remote_environment)
    assert result.returncode == 0, result.stderr
    assert existing_record.read_text() == "private existing data"
    app_root = Path(remote_environment["DEPLOY_TEST_APP_ROOT"])
    assert (app_root / "current").resolve() == app_root / "releases" / ("a" * 32)
    install_command = next(
        command for command in read_commands(remote_environment) if command[0] == "runuser"
    )
    assert {"--locked", "--no-dev", "--no-editable"} <= set(install_command)


def test_remote_seed_is_activated_atomically(remote_environment: dict[str, str]) -> None:
    """The completed seed directory replaces the initial empty directory."""
    result = run_remote(remote_environment, seed=True)
    assert result.returncode == 0, result.stderr
    state_root = Path(remote_environment["DEPLOY_TEST_STATE_ROOT"])
    assert (state_root / "data" / "personas.json").read_text() == "[]"
    assert (state_root / "data" / "inputs.json").read_text() == "[]"
    assert not list(state_root.glob("seed.*"))
    moves = [command for command in read_commands(remote_environment) if command[0] == "mv"]
    assert any(command[1] == "-T" and command[-1] == str(state_root / "data") for command in moves)


def test_remote_seed_refuses_existing_dataset(remote_environment: dict[str, str]) -> None:
    """Seeding cannot overwrite even a partially populated persistent dataset."""
    existing_record = Path(remote_environment["DEPLOY_TEST_STATE_ROOT"]) / "data" / "existing.json"
    existing_record.write_text("preserved")
    result = run_remote(remote_environment, seed=True)
    assert result.returncode != 0
    assert "initialized dataset already exists" in result.stderr
    assert existing_record.read_text() == "preserved"
    assert not any(command[0] == "runuser" for command in read_commands(remote_environment))


def test_failed_dependency_install_does_not_activate_release(
    remote_environment: dict[str, str],
) -> None:
    """A failed production sync leaves the running release alone."""
    remote_environment["DEPLOY_TEST_INSTALL_FAILURE"] = "1"
    result = run_remote(remote_environment)
    assert result.returncode != 0
    app_root = Path(remote_environment["DEPLOY_TEST_APP_ROOT"])
    assert (app_root / "current").resolve() == app_root / "releases" / "previous"
    assert not any(command[0] == "systemctl" for command in read_commands(remote_environment))


def test_failed_health_check_restores_previous_release(remote_environment: dict[str, str]) -> None:
    """Exhausted health retries restore and restart the previous release."""
    remote_environment["DEPLOY_TEST_FAILED_HEALTH_CHECKS"] = "15"
    result = run_remote(remote_environment)
    assert result.returncode != 0
    assert "restoring previous release" in result.stderr
    app_root = Path(remote_environment["DEPLOY_TEST_APP_ROOT"])
    assert (app_root / "current").resolve() == app_root / "releases" / "previous"
    commands = read_commands(remote_environment)
    assert sum(command[0] == "curl" for command in commands) == 16
    assert sum(command[:2] == ["systemctl", "restart"] for command in commands) == 2


def test_failed_first_release_stops_service(remote_environment: dict[str, str]) -> None:
    """A failed first activation leaves no current symlink or unhealthy running service."""
    app_root = Path(remote_environment["DEPLOY_TEST_APP_ROOT"])
    (app_root / "current").unlink()
    remote_environment["DEPLOY_TEST_FAILED_HEALTH_CHECKS"] = "15"
    result = run_remote(remote_environment)
    assert result.returncode != 0
    assert not (app_root / "current").is_symlink()
    assert ["systemctl", "stop", "the-bot-radio.service"] in read_commands(remote_environment)


def test_concurrent_deployment_is_rejected(remote_environment: dict[str, str]) -> None:
    """An already-held nonblocking lock prevents all installation and activation."""
    remote_environment["DEPLOY_TEST_LOCKED"] = "1"
    result = run_remote(remote_environment)
    assert result.returncode != 0
    assert "Another deployment is in progress" in result.stderr
    assert read_commands(remote_environment) == [["flock", "-n", "9"]]


def test_seed_uses_explicit_data_directory(
    deploy_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    """An operator can seed a separate backup without including repository data."""
    backup_directory = tmp_path / "backup data"
    (backup_directory / "days").mkdir(parents=True)
    (backup_directory / "personas.json").write_text("[]")
    (backup_directory / "inputs.json").write_text("[]")
    (backup_directory / "days" / "2026-10-26.json").write_text("{}")
    result = run_deploy(deploy_environment, "--seed-data", "--data-dir", str(backup_directory))
    assert result.returncode == 0, result.stderr
    assert archive_members(deploy_environment, "seed.tar.gz") == {
        "personas.json",
        "inputs.json",
        "days/2026-10-26.json",
    }


def test_source_symlink_cannot_include_external_credentials(
    deploy_environment: dict[str, str],
) -> None:
    """Reject linked application files before any data is transferred."""
    repository = Path(deploy_environment["DEPLOY_TEST_REPOSITORY"])
    (repository / "src" / "the_bot_radio" / "secret.json").symlink_to(repository / ".env")
    result = run_deploy(deploy_environment)
    assert result.returncode != 0
    assert "Symlink in application source" in result.stderr
    assert not any(command[0] == "scp" for command in read_commands(deploy_environment))


def test_restart_failure_rolls_back_release(remote_environment: dict[str, str]) -> None:
    """A systemd restart failure restores the previous release and retries its restart."""
    remote_environment["DEPLOY_TEST_RESTART_FAILURE"] = "1"
    result = run_remote(remote_environment)
    assert result.returncode != 0
    app_root = Path(remote_environment["DEPLOY_TEST_APP_ROOT"])
    assert (app_root / "current").resolve() == app_root / "releases" / "previous"
    commands = read_commands(remote_environment)
    assert sum(command[:2] == ["systemctl", "restart"] for command in commands) == 2
    assert sum(command[0] == "curl" for command in commands) == 1


def test_bootstrap_explicitly_invokes_bash_under_sh(tmp_path: Path) -> None:
    """Lightsail's sh wrapper must hand the bootstrap body to Bash unchanged."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    bash_mock = bin_directory / "bash"
    bash_mock.write_text('#!/bin/sh\ncat > "$BOOTSTRAP_TEST_CAPTURE"\n')
    bash_mock.chmod(0o755)
    captured_script = tmp_path / "bootstrap-body.sh"
    result = subprocess.run(
        ["sh", str(REPOSITORY_ROOT / "terraform/templates/bootstrap.sh")],
        env={
            "PATH": f"{bin_directory}:{os.environ['PATH']}",
            "BOOTSTRAP_TEST_CAPTURE": str(captured_script),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert captured_script.read_text().startswith("set -euo pipefail\n")
    subprocess.run(["bash", "-n", str(captured_script)], check=True)
