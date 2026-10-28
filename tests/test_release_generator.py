"""Exercise image releases without Docker, AWS credentials, or network access."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "123456789012.dkr.ecr.us-east-1.amazonaws.com/the-bot-radio-generator"
IMAGE_DIGEST = "sha256:" + "b" * 64
MOCK_EXECUTABLE = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

name = pathlib.Path(sys.argv[0]).name
arguments = sys.argv[1:]
with open(os.environ["RELEASE_TEST_LOG"], "a") as log_file:
    log_file.write(json.dumps([name, *arguments]) + "\n")
if os.environ.get("RELEASE_TEST_FAIL") == " ".join([name, *arguments[:1]]):
    raise SystemExit(17)
if name == "terraform":
    print(json.dumps({key: {"value": value} for key, value in {
        "generation_repository_url": os.environ["RELEASE_TEST_REPOSITORY_URL"],
        "aws_region": "us-east-1",
    }.items()}))
elif name == "git":
    print("a" * 40)
elif name == "aws" and arguments[:2] == ["ecr", "get-login-password"]:
    if os.environ.get("RELEASE_TEST_LOGIN_FAILURE"):
        raise SystemExit(18)
    print("test-password")
elif name == "aws" and arguments[:2] == ["ecr", "describe-images"]:
    print(os.environ.get("RELEASE_TEST_DIGEST", "sha256:" + "b" * 64))
elif name == "docker" and arguments[0] in {"login", "run"}:
    if "--password-stdin" in arguments or "--interactive" in arguments:
        sys.stdin.read()
"""


@pytest.fixture
def release_environment(tmp_path: Path) -> dict[str, str]:
    """Isolate the release script and record all external command invocations."""
    repository_path = tmp_path / "repository"
    deploy_path = repository_path / "deploy"
    deploy_path.mkdir(parents=True)
    for filename in ("release-generator.sh", "smoke-generator.py", "Dockerfile.generator"):
        shutil.copyfile(REPOSITORY_ROOT / "deploy" / filename, deploy_path / filename)
    data_path = repository_path / "src" / "the_bot_radio" / "resources"
    data_path.mkdir(parents=True)
    for filename in ("personas.json", "shared_catalog.jsonl"):
        (data_path / filename).write_text("fixture runtime input")
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    for executable_name in ("terraform", "aws", "docker", "git"):
        executable_path = bin_path / executable_name
        executable_path.write_text(MOCK_EXECUTABLE)
        executable_path.chmod(0o755)
    return {
        "PATH": f"{bin_path}:{os.environ['PATH']}",
        "RELEASE_TEST_SCRIPT": str(deploy_path / "release-generator.sh"),
        "RELEASE_TEST_LOG": str(tmp_path / "commands.jsonl"),
        "RELEASE_TEST_REPOSITORY_URL": REPOSITORY_URL,
    }


def run_release(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real Bash script with AWS and Docker replaced by local mocks."""
    return subprocess.run(
        ["bash", environment["RELEASE_TEST_SCRIPT"]],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def read_commands(environment: dict[str, str]) -> list[list[str]]:
    """Return recorded external commands in execution order."""
    return [
        json.loads(line) for line in Path(environment["RELEASE_TEST_LOG"]).read_text().splitlines()
    ]


def test_release_builds_and_smokes_before_pushing(release_environment: dict[str, str]) -> None:
    """Release the AMD64 image only after offline runtime and CLI checks succeed."""
    result = run_release(release_environment)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{REPOSITORY_URL}@{IMAGE_DIGEST}"
    commands = read_commands(release_environment)
    build_command = next(
        command for command in commands if command[:3] == ["docker", "buildx", "build"]
    )
    assert build_command[build_command.index("--platform") + 1] == "linux/amd64"
    assert "--load" in build_command
    image_uri = build_command[build_command.index("--tag") + 1]
    assert image_uri.startswith(f"{REPOSITORY_URL}:git-{'a' * 40}-")
    assert len(image_uri.rsplit("-", 1)[1]) == 32
    smoke_commands = [command for command in commands if command[:2] == ["docker", "run"]]
    assert len(smoke_commands) == 2
    assert smoke_commands[0][-2:] == [image_uri, "--help"]
    assert smoke_commands[1][-4:] == ["--entrypoint", "python", image_uri, "-"]
    assert all(command[command.index("--network") + 1] == "none" for command in smoke_commands)
    push_command = ["docker", "push", image_uri]
    assert commands.index(push_command) > commands.index(smoke_commands[-1])
    assert not any("apply" in command or "run-task" in command for command in commands)


def test_release_tags_are_unique(release_environment: dict[str, str]) -> None:
    """Two releases of one commit never reuse an immutable ECR tag."""
    assert run_release(release_environment).returncode == 0
    assert run_release(release_environment).returncode == 0
    pushed_images = [
        command[-1]
        for command in read_commands(release_environment)
        if command[:2] == ["docker", "push"]
    ]
    assert len(set(pushed_images)) == 2


@pytest.mark.parametrize("failed_command", ["docker buildx", "docker run", "docker push"])
def test_release_propagates_command_failure(
    release_environment: dict[str, str], failed_command: str
) -> None:
    """Failed builds, smoke tests, and pushes produce no usable digest output."""
    release_environment["RELEASE_TEST_FAIL"] = failed_command
    result = run_release(release_environment)
    assert result.returncode == 17
    assert result.stdout == ""
    if failed_command != "docker push":
        assert not any(
            command[:2] == ["docker", "push"] for command in read_commands(release_environment)
        )


def test_login_pipeline_propagates_aws_failure(release_environment: dict[str, str]) -> None:
    """An AWS login failure aborts even if Docker accepts empty input."""
    release_environment["RELEASE_TEST_LOGIN_FAILURE"] = "1"
    result = run_release(release_environment)
    assert result.returncode == 18
    assert not any(
        command[:2] == ["docker", "buildx"] for command in read_commands(release_environment)
    )


@pytest.mark.parametrize("invalid_digest", ["None", "sha256:bad", "image:latest"])
def test_release_rejects_invalid_digest(
    release_environment: dict[str, str], invalid_digest: str
) -> None:
    """Missing or malformed ECR results cannot become Terraform inputs."""
    release_environment["RELEASE_TEST_DIGEST"] = invalid_digest
    result = run_release(release_environment)
    assert result.returncode != 0
    assert "Invalid ECR digest" in result.stderr
    assert result.stdout == ""


def test_release_rejects_wrong_repository(release_environment: dict[str, str]) -> None:
    """Reject malformed destinations before attempting authentication."""
    release_environment["RELEASE_TEST_REPOSITORY_URL"] = "example.com/wrong"
    result = run_release(release_environment)
    assert result.returncode != 0
    assert not any(command[0] == "aws" for command in read_commands(release_environment))


@pytest.mark.parametrize("invalid_kind", ["missing", "empty", "symlink"])
def test_release_rejects_unavailable_runtime_inputs_before_aws(
    release_environment: dict[str, str], invalid_kind: str
) -> None:
    """Missing or linked runtime inputs cannot produce a published broken image."""
    repository = Path(release_environment["RELEASE_TEST_SCRIPT"]).parents[1]
    catalog_path = repository / "src" / "the_bot_radio" / "resources" / "shared_catalog.jsonl"
    if invalid_kind == "missing":
        catalog_path.unlink()
    elif invalid_kind == "empty":
        catalog_path.write_text("")
    else:
        catalog_path.unlink()
        catalog_path.symlink_to(catalog_path.with_name("personas.json"))
    result = run_release(release_environment)
    assert result.returncode != 0
    assert "shared_catalog.jsonl" in result.stderr
    assert not Path(release_environment["RELEASE_TEST_LOG"]).exists()


def test_image_smoke_validates_runtime_snapshots(tmp_path: Path) -> None:
    """The real snapshot validation accepts approved inputs and rejects extra state."""
    specification = importlib.util.spec_from_file_location(
        "generator_smoke", REPOSITORY_ROOT / "deploy" / "smoke-generator.py"
    )
    assert specification is not None and specification.loader is not None
    smoke_module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(smoke_module)
    write_smoke_inputs(tmp_path)
    smoke_module.validate_packaged_resources(tmp_path)
    (tmp_path / "days").mkdir()
    with pytest.raises(AssertionError):
        smoke_module.validate_packaged_resources(tmp_path)


def test_image_smoke_rejects_empty_catalog(tmp_path: Path) -> None:
    """An empty catalog still fails the image's readiness check."""
    specification = importlib.util.spec_from_file_location(
        "generator_smoke", REPOSITORY_ROOT / "deploy" / "smoke-generator.py"
    )
    assert specification is not None and specification.loader is not None
    smoke_module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(smoke_module)
    write_smoke_inputs(tmp_path)
    (tmp_path / "shared_catalog.jsonl").write_text("")
    with pytest.raises(AssertionError):
        smoke_module.validate_packaged_resources(tmp_path)


def write_smoke_inputs(data_directory: Path) -> None:
    """Build an offline catalog independent of the packaged catalog contents."""
    for filename in ("personas.json",):
        shutil.copyfile(
            REPOSITORY_ROOT / "src" / "the_bot_radio" / "resources" / filename,
            data_directory / filename,
        )
    records = [
        {
            "spotify_id": f"{index:022d}",
            "title": f"Track {index}",
            "artists": [f"Artist {index}"],
            "album": "Test album",
            "duration_ms": 180000,
            "spotify_url": f"https://open.spotify.com/track/{index:022d}",
        }
        for index in range(5)
    ]
    (data_directory / "shared_catalog.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
