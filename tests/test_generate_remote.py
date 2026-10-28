"""Verify one-off Fargate execution without AWS requests or generator API calls."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TASK_ARN = "arn:aws:ecs:us-east-1:123456789012:task/generator/task-id"


@pytest.fixture
def remote_generator() -> ModuleType:
    """Load the standalone operator script without starting its command."""
    specification = importlib.util.spec_from_file_location(
        "generate_remote", REPOSITORY_ROOT / "deploy" / "generate-remote.py"
    )
    assert specification is not None and specification.loader is not None
    remote_generator_module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(remote_generator_module)
    return remote_generator_module


@pytest.fixture
def generation_outputs() -> dict[str, Any]:
    """Provide resource identifiers from a staged Terraform deployment."""
    return {
        "aws_region": "us-east-1",
        "generation_cluster_arn": "cluster-arn",
        "generation_task_definition_arn": "task-definition-arn:3",
        "generation_subnet_ids": ["subnet-a", "subnet-b"],
        "generation_security_group_id": "sg-generator",
        "generation_log_group_name": "/ecs/generator",
    }


@pytest.fixture
def configured_generator(
    remote_generator: ModuleType,
    generation_outputs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    """Use local configuration and remove polling delays from mocked runs."""
    monkeypatch.setattr(remote_generator, "terraform_outputs", lambda: generation_outputs)
    monkeypatch.setattr(remote_generator.time, "sleep", lambda _: None)
    return remote_generator


def install_successful_aws(
    remote_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int = 0,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Record requests and return a normally stopped Fargate task."""
    requests: list[tuple[str, str, dict[str, Any]]] = []

    def fake_aws_request(
        region: str, service: str, operation: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        assert region == "us-east-1"
        requests.append((service, operation, request))
        if operation == "run-task":
            return {"tasks": [{"taskArn": TASK_ARN}], "failures": []}
        if operation == "describe-tasks":
            return {
                "tasks": [
                    {
                        "taskArn": TASK_ARN,
                        "lastStatus": "STOPPED",
                        "stoppedReason": "Essential container in task exited",
                        "containers": [{"name": "generator", "exitCode": exit_code}],
                    }
                ],
                "failures": [],
            }
        if operation == "get-log-events":
            return {
                "events": []
                if "nextToken" in request
                else [{"message": "remote generator output"}],
                "nextForwardToken": "end",
            }
        raise AssertionError(f"Unexpected operation: {operation}")

    monkeypatch.setattr(remote_generator, "aws_request", fake_aws_request)
    return requests


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--date", "2026-10-27"],
        [
            "--date",
            "2026-10-28",
            "--data-dir",
            "/work/data with spaces",
            "--model",
            "literal;$(not-a-command)",
            "--max-turns",
            "7",
            "--budget-usd",
            "2.5",
            "--deadline-seconds",
            "120",
            "--verbose",
        ],
        ["--unknown-option"],
    ],
)
def test_forwards_arguments_unchanged(
    configured_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    """Preserve the generator interface and literal argument boundaries remotely."""
    requests = install_successful_aws(configured_generator, monkeypatch)
    assert configured_generator.main(arguments) == 0
    launch_request = next(request for _, operation, request in requests if operation == "run-task")
    assert launch_request["overrides"]["containerOverrides"] == [
        {"name": "generator", "command": arguments}
    ]
    assert launch_request["taskDefinition"] == "task-definition-arn:3"
    assert launch_request["launchType"] == "FARGATE"
    assert launch_request["platformVersion"] == "1.4.0"
    assert launch_request["count"] == 1
    assert launch_request["networkConfiguration"]["awsvpcConfiguration"] == {
        "subnets": ["subnet-a", "subnet-b"],
        "securityGroups": ["sg-generator"],
        "assignPublicIp": "ENABLED",
    }


def test_empty_arguments_do_not_inherit_verbose_container_default(
    configured_generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Match the local generator's quiet default when invoked without arguments."""
    requests = install_successful_aws(configured_generator, monkeypatch)
    assert configured_generator.main([]) == 0
    launch_request = next(request for _, operation, request in requests if operation == "run-task")
    assert launch_request["overrides"]["containerOverrides"][0]["command"] == ["--"]


@pytest.mark.parametrize("exit_code", [0, 1, 2, 42])
def test_returns_remote_exit_code_and_prints_logs(
    configured_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_code: int,
) -> None:
    """Expose application failures to callers and print its CloudWatch output."""
    install_successful_aws(configured_generator, monkeypatch, exit_code=exit_code)
    assert configured_generator.main(["--help"]) == exit_code
    captured = capsys.readouterr()
    assert "remote generator output" in captured.out
    assert TASK_ARN in captured.err


def test_reports_launch_failure(
    configured_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RunTask failures cannot be mistaken for a successful one-off job."""
    monkeypatch.setattr(
        configured_generator,
        "aws_request",
        lambda *args: {"tasks": [], "failures": [{"reason": "capacity unavailable"}]},
    )
    assert configured_generator.main(["--help"]) == 1
    assert "capacity unavailable" in capsys.readouterr().err


def test_rejects_foundation_without_task_definition(
    configured_generator: ModuleType,
    generation_outputs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered generator must fail before requesting a remote launch."""
    generation_outputs["generation_task_definition_arn"] = None

    def unexpected_aws_request(*args: Any) -> dict[str, Any]:
        raise AssertionError("AWS must not be called before a task definition exists")

    monkeypatch.setattr(configured_generator, "aws_request", unexpected_aws_request)
    assert configured_generator.main(["--help"]) == 1


def test_reports_initialization_failure_without_exit_code(
    configured_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Image or secret initialization errors include the ECS stopped reason."""

    def fake_aws_request(
        region: str, service: str, operation: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        if operation == "run-task":
            return {"tasks": [{"taskArn": TASK_ARN}], "failures": []}
        if operation == "describe-tasks":
            return {
                "tasks": [
                    {
                        "lastStatus": "STOPPED",
                        "stoppedReason": "ResourceInitializationError: missing secret",
                        "containers": [{"name": "generator"}],
                    }
                ],
                "failures": [],
            }
        if operation == "get-log-events":
            return {"events": [], "nextForwardToken": "end"}
        raise AssertionError(operation)

    monkeypatch.setattr(configured_generator, "aws_request", fake_aws_request)
    assert configured_generator.main(["--help"]) == 1
    assert "missing secret" in capsys.readouterr().err


def test_interrupt_stops_launched_task(
    configured_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control-C requests a remote stop instead of abandoning the running job."""
    requests: list[tuple[str, dict[str, Any]]] = []

    def fake_aws_request(
        region: str, service: str, operation: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        requests.append((operation, request))
        if operation == "run-task":
            return {"tasks": [{"taskArn": TASK_ARN}], "failures": []}
        if operation == "describe-tasks":
            raise KeyboardInterrupt
        if operation == "get-log-events":
            return {"events": [], "nextForwardToken": "end"}
        if operation == "stop-task":
            return {"task": {"taskArn": TASK_ARN}}
        raise AssertionError(operation)

    monkeypatch.setattr(configured_generator, "aws_request", fake_aws_request)
    assert configured_generator.main(["--date", "2026-10-28"]) == 130
    stop_requests = [request for operation, request in requests if operation == "stop-task"]
    assert len(stop_requests) == 1
    assert stop_requests[0]["task"] == TASK_ARN
    assert stop_requests[0]["cluster"] == "cluster-arn"


def test_drains_log_pages_and_tolerates_stream_creation_delay(
    configured_generator: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read all CloudWatch pages without duplicating logs or failing before startup."""
    log_requests = 0

    def fake_aws_request(
        region: str, service: str, operation: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal log_requests
        if operation == "run-task":
            return {"tasks": [{"taskArn": TASK_ARN}], "failures": []}
        if operation == "describe-tasks":
            return {
                "tasks": [
                    {
                        "lastStatus": "STOPPED",
                        "containers": [{"name": "generator", "exitCode": 0}],
                    }
                ],
                "failures": [],
            }
        if operation == "get-log-events":
            log_requests += 1
            if log_requests == 1:
                raise configured_generator.AwsCommandError(
                    "ResourceNotFoundException: no stream yet"
                )
            assert request["logGroupName"] == "/ecs/generator"
            assert request["logStreamName"] == "generator/generator/task-id"
            token = request.get("nextToken")
            if token is None:
                return {"events": [{"message": "first page"}], "nextForwardToken": "page-1"}
            if token == "page-1":
                return {"events": [{"message": "second page"}], "nextForwardToken": "page-2"}
            assert token == "page-2"
            return {"events": [], "nextForwardToken": "page-2"}
        raise AssertionError(operation)

    monkeypatch.setattr(configured_generator, "aws_request", fake_aws_request)
    assert configured_generator.main(["--help"]) == 0
    captured = capsys.readouterr()
    assert captured.out.count("first page") == 1
    assert captured.out.count("second page") == 1


def test_aws_request_preserves_json_and_uses_no_shell(
    remote_generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep special characters literal and let the script handle API pagination."""
    request = {"overrides": {"command": ["--model", "literal;$(no shell)"]}}
    observed_calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_subprocess_run(
        arguments: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        observed_calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout='{"tasks": []}', stderr="")

    monkeypatch.setattr(remote_generator.subprocess, "run", fake_subprocess_run)
    assert remote_generator.aws_request("us-east-1", "ecs", "run-task", request) == {"tasks": []}
    assert len(observed_calls) == 1
    arguments, options = observed_calls[0]
    assert arguments[:3] == ["aws", "ecs", "run-task"]
    assert json.loads(arguments[arguments.index("--cli-input-json") + 1]) == request
    assert arguments[arguments.index("--region") + 1] == "us-east-1"
    assert "--no-paginate" in arguments and "--no-cli-pager" in arguments
    assert not options.get("shell", False)
    assert options["capture_output"] and options["text"]


def test_retries_eventually_consistent_task_lookup(
    configured_generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporarily missing newly launched task does not fail or launch a duplicate."""
    original_requests = install_successful_aws(configured_generator, monkeypatch)
    successful_aws_request = configured_generator.aws_request
    missing_lookups = 0

    def eventually_consistent_aws_request(
        region: str, service: str, operation: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal missing_lookups
        if operation == "describe-tasks" and missing_lookups < 2:
            missing_lookups += 1
            return {"tasks": [], "failures": [{"reason": "MISSING"}]}
        return successful_aws_request(region, service, operation, request)

    monkeypatch.setattr(configured_generator, "aws_request", eventually_consistent_aws_request)
    assert configured_generator.main(["--help"]) == 0
    assert missing_lookups == 2
    assert sum(operation == "run-task" for _, operation, _ in original_requests) == 1
