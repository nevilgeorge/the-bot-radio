"""Verify credential transport and transactional systemd configuration without privileges."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ACCESS_KEY = "AKIA" + "A" * 16
SECRET_KEY = "S" * 40
MOCK_EXECUTABLE = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import subprocess
import sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CONFIGURE_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "terraform":
    print(json.dumps({key: {"value": value} for key, value in {
        "static_ip_address": "192.0.2.10", "ssh_user": "ubuntu",
    }.items()}))
elif name == "scp":
    for argument in args[:-1]:
        source = pathlib.Path(argument)
        if source.is_file():
            shutil.copy2(source, pathlib.Path(os.environ["CONFIGURE_CAPTURE"]) / source.name)
elif name == "install":
    cleaned = []
    index = 0
    while index < len(args):
        if args[index] in ("-o", "-g"):
            index += 2
        else:
            cleaned.append(args[index])
            index += 1
    sys.exit(subprocess.run(["/usr/bin/install", *cleaned]).returncode)
elif name == "systemctl" and args[0] == "restart":
    counter = pathlib.Path(os.environ["CONFIGURE_COUNTER"])
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    if count == 0 and os.environ.get("CONFIGURE_RESTART_FAILURE"):
        sys.exit(1)
elif name == "curl":
    counter = pathlib.Path(os.environ["CONFIGURE_CURL_COUNTER"])
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    if count < int(os.environ.get("CONFIGURE_FAILED_HEALTH_CHECKS", "0")):
        sys.exit(1)
elif name == "flock" and os.environ.get("CONFIGURE_LOCKED"):
    sys.exit(1)
"""


@pytest.fixture
def configuration_environment(tmp_path: Path) -> dict[str, str]:
    """Prepare scripts, credentials and command mocks in an isolated filesystem."""
    repository = tmp_path / "repository"
    (repository / "deploy").mkdir(parents=True)
    for filename in ("configure-web-s3.sh", "remote-configure-web-s3.sh"):
        shutil.copy2(REPOSITORY_ROOT / "deploy" / filename, repository / "deploy" / filename)
    configuration_root = tmp_path / "configuration"
    drop_in_directory = tmp_path / "drop-in"
    state_root = tmp_path / "state"
    state_root.mkdir()
    remote_script = repository / "deploy" / "remote-configure-web-s3.sh"
    remote_script.write_text(
        remote_script.read_text()
        .replace("/etc/the-bot-radio", str(configuration_root))
        .replace("/etc/systemd/system/the-bot-radio.service.d", str(drop_in_directory))
        .replace("/var/lib/the-bot-radio", str(state_root))
    )
    credentials_file = tmp_path / "credentials.json"
    credentials_file.write_text(
        json.dumps({"AccessKey": {"AccessKeyId": ACCESS_KEY, "SecretAccessKey": SECRET_KEY}})
    )
    credentials_file.chmod(0o600)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for executable_name in (
        "terraform",
        "ssh",
        "scp",
        "install",
        "chown",
        "systemctl",
        "curl",
        "sleep",
        "flock",
    ):
        executable = bin_directory / executable_name
        executable.write_text(MOCK_EXECUTABLE)
        executable.chmod(0o755)
    capture_directory = tmp_path / "uploads"
    capture_directory.mkdir()
    return {
        **{key: os.environ[key] for key in ("HOME", "TMPDIR") if key in os.environ},
        "PATH": f"{bin_directory}:{os.environ['PATH']}",
        "CONFIGURE_REPOSITORY": str(repository),
        "CONFIGURE_ROOT": str(configuration_root),
        "CONFIGURE_DROP_IN": str(drop_in_directory),
        "CONFIGURE_CREDENTIALS": str(credentials_file),
        "CONFIGURE_LOG": str(tmp_path / "commands.jsonl"),
        "CONFIGURE_COUNTER": str(tmp_path / "restart-count"),
        "CONFIGURE_CURL_COUNTER": str(tmp_path / "curl-count"),
        "CONFIGURE_CAPTURE": str(capture_directory),
    }


def run_configuration(
    environment: dict[str, str], *, remote: bool = False, extra_arguments: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Execute local transport or remote activation with only mock external commands."""
    deploy_directory = Path(environment["CONFIGURE_REPOSITORY"]) / "deploy"
    if remote:
        environment_source = Path(environment["CONFIGURE_CAPTURE"]) / "web.env"
        if not environment_source.exists():
            environment_source.write_text("AWS_ACCESS_KEY_ID=synthetic\n")
        arguments = [str(deploy_directory / "remote-configure-web-s3.sh"), str(environment_source)]
    else:
        arguments = [
            str(deploy_directory / "configure-web-s3.sh"),
            "--credentials-file",
            environment["CONFIGURE_CREDENTIALS"],
            *extra_arguments,
        ]
    return subprocess.run(
        ["bash", *arguments], env=environment, text=True, capture_output=True, check=False
    )


def test_private_credentials_are_transferred_without_secret_arguments(
    configuration_environment: dict[str, str],
) -> None:
    """The restricted upload contains credentials; all commands and output redact them."""
    result = run_configuration(
        configuration_environment, extra_arguments=("--identity-file", "/operator key")
    )
    assert result.returncode == 0, result.stderr
    uploaded = Path(configuration_environment["CONFIGURE_CAPTURE"]) / "web.env"
    assert uploaded.stat().st_mode & 0o777 == 0o600
    assert f"AWS_SECRET_ACCESS_KEY={SECRET_KEY}\n" in uploaded.read_text()
    assert "BOT_RADIO_S3_PREFIX=production\n" in uploaded.read_text()
    log_content = Path(configuration_environment["CONFIGURE_LOG"]).read_text()
    assert SECRET_KEY not in result.stdout + result.stderr + log_content
    assert ACCESS_KEY not in result.stdout + result.stderr + log_content
    commands = [json.loads(line) for line in log_content.splitlines()]
    assert all("/operator key" in command for command in commands if command[0] in {"ssh", "scp"})
    transfer = next(command for command in commands if command[0] == "scp")
    staged_environment = next(argument for argument in transfer if argument.endswith("/web.env"))
    assert not Path(staged_environment).exists()


@pytest.mark.parametrize("invalid_kind", ["public", "malformed", "injection", "symlink"])
def test_invalid_credentials_fail_without_disclosure(
    configuration_environment: dict[str, str], invalid_kind: str
) -> None:
    """Reject unsafe file modes, input syntax and environment injection before transfer."""
    credentials_file = Path(configuration_environment["CONFIGURE_CREDENTIALS"])
    if invalid_kind == "public":
        credentials_file.chmod(0o644)
    elif invalid_kind == "malformed":
        credentials_file.write_text(SECRET_KEY)
    elif invalid_kind == "injection":
        credentials_file.write_text(
            json.dumps(
                {
                    "AccessKey": {
                        "AccessKeyId": ACCESS_KEY,
                        "SecretAccessKey": SECRET_KEY + "\nOTHER=value",
                    }
                }
            )
        )
    else:
        linked_file = credentials_file.with_suffix(".link")
        linked_file.symlink_to(credentials_file)
        configuration_environment["CONFIGURE_CREDENTIALS"] = str(linked_file)
    result = run_configuration(configuration_environment)
    assert result.returncode != 0
    assert SECRET_KEY not in result.stdout + result.stderr
    assert "Invalid credentials file" in result.stderr
    assert not Path(configuration_environment["CONFIGURE_LOG"]).exists()


def test_activation_installs_private_environment_and_preserves_prior_configuration(
    configuration_environment: dict[str, str],
) -> None:
    """Repeat activation retains the preceding files and emits the systemd environment path."""
    first = run_configuration(configuration_environment, remote=True)
    assert first.returncode == 0, first.stderr
    configuration_root = Path(configuration_environment["CONFIGURE_ROOT"])
    environment_file = configuration_root / "web.env"
    drop_in = Path(configuration_environment["CONFIGURE_DROP_IN"]) / "20-web-s3.conf"
    assert environment_file.stat().st_mode & 0o777 == 0o600
    assert configuration_root.stat().st_mode & 0o777 == 0o700
    assert drop_in.read_text() == f"[Service]\nEnvironmentFile={environment_file}\n"
    original_environment = environment_file.read_text()
    original_drop_in = drop_in.read_text()
    staged = Path(configuration_environment["CONFIGURE_CAPTURE"]) / "web.env"
    staged.write_text("AWS_ACCESS_KEY_ID=replacement\n")
    second = run_configuration(configuration_environment, remote=True)
    assert second.returncode == 0, second.stderr
    assert environment_file.read_text() == staged.read_text()
    backups = list((configuration_root / "backups").iterdir())
    assert len(backups) == 2
    populated_backup = next(backup for backup in backups if (backup / "web.env").exists())
    assert (populated_backup / "web.env").read_text() == original_environment
    assert (populated_backup / "20-web-s3.conf").read_text() == original_drop_in
    commands = [
        json.loads(line)
        for line in Path(configuration_environment["CONFIGURE_LOG"]).read_text().splitlines()
    ]
    assert any(
        command[:7] == ["install", "-o", "root", "-g", "root", "-m", "600"] for command in commands
    )


@pytest.mark.parametrize("failure_kind", ["restart", "health"])
@pytest.mark.parametrize("existing_configuration", [True, False])
def test_activation_failure_restores_files_and_restarts_previous_configuration(
    configuration_environment: dict[str, str], failure_kind: str, existing_configuration: bool
) -> None:
    """Both startup and health failures roll back existing or initially absent configuration."""
    environment_file = Path(configuration_environment["CONFIGURE_ROOT"]) / "web.env"
    drop_in = Path(configuration_environment["CONFIGURE_DROP_IN"]) / "20-web-s3.conf"
    if existing_configuration:
        environment_file.parent.mkdir()
        environment_file.write_text("old environment")
        environment_file.chmod(0o600)
        drop_in.parent.mkdir()
        drop_in.write_text("old drop-in")
    if failure_kind == "restart":
        configuration_environment["CONFIGURE_RESTART_FAILURE"] = "1"
    else:
        configuration_environment["CONFIGURE_FAILED_HEALTH_CHECKS"] = "15"
    result = run_configuration(configuration_environment, remote=True)
    assert result.returncode != 0
    assert "restoring previous configuration" in result.stderr
    if existing_configuration:
        assert environment_file.read_text() == "old environment"
        assert drop_in.read_text() == "old drop-in"
        assert environment_file.stat().st_mode & 0o777 == 0o600
    else:
        assert not environment_file.exists()
        assert not drop_in.exists()
    assert Path(configuration_environment["CONFIGURE_COUNTER"]).read_text() == "2"


def test_configuration_shares_the_deployment_lock(
    configuration_environment: dict[str, str],
) -> None:
    """A running application deployment prevents credential configuration changes."""
    configuration_environment["CONFIGURE_LOCKED"] = "1"
    result = run_configuration(configuration_environment, remote=True)
    assert result.returncode != 0
    assert "Another deployment is in progress" in result.stderr
    assert not Path(configuration_environment["CONFIGURE_ROOT"]).exists()
