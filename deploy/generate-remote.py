#!/usr/bin/env python3
"""Run the generator CLI in the deployed Fargate task and follow its CloudWatch logs."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 2


class AwsCommandError(RuntimeError):
    """An AWS CLI request failed before returning a valid response."""


def terraform_outputs() -> dict[str, Any]:
    """Read deployed resource identifiers using the repository's configured backend."""
    response = subprocess.run(
        ["terraform", f"-chdir={REPOSITORY_ROOT / 'terraform'}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {name: output["value"] for name, output in json.loads(response.stdout).items()}


def aws_request(
    region: str, service: str, operation: str, request: dict[str, Any]
) -> dict[str, Any]:
    """Call AWS without shell interpretation or automatic CLI pagination."""
    response = subprocess.run(
        [
            "aws",
            service,
            operation,
            "--region",
            region,
            "--cli-input-json",
            json.dumps(request),
            "--output",
            "json",
            "--no-cli-pager",
            "--no-paginate",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "30",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if response.returncode:
        raise AwsCommandError(response.stderr.strip() or f"AWS {service} {operation} failed")
    return json.loads(response.stdout)


def read_logs(
    region: str, log_group: str, task_arn: str, token: str | None
) -> tuple[str | None, int]:
    """Print new events, tolerating a stream that has not been created yet."""
    event_count = 0
    # Bound each pass so a busy stream cannot prevent checking task status or cancellation.
    for _ in range(10):
        request: dict[str, Any] = {
            "logGroupName": log_group,
            "logStreamName": f"generator/generator/{task_arn.rsplit('/', 1)[-1]}",
            "startFromHead": True,
        }
        if token is not None:
            request["nextToken"] = token
        try:
            response = aws_request(region, "logs", "get-log-events", request)
        except AwsCommandError as error:
            if "ResourceNotFoundException" in str(error):
                return token, event_count
            raise
        for event in response.get("events", []):
            print(event["message"], flush=True)
            event_count += 1
        next_token = response["nextForwardToken"]
        if next_token == token:
            break
        token = next_token
    return token, event_count


def follow_task(region: str, cluster: str, log_group: str, task_arn: str) -> int:
    """Follow one task until stopped, then drain logs and return its container exit code."""
    token: str | None = None
    missing_count = 0
    quiet_stopped_polls = 0
    while True:
        response = aws_request(
            region,
            "ecs",
            "describe-tasks",
            {
                "cluster": cluster,
                "tasks": [task_arn],
            },
        )
        tasks = response.get("tasks", [])
        if not tasks:
            failures = response.get("failures", [])
            if any(failure.get("reason") != "MISSING" for failure in failures):
                raise RuntimeError(f"Cannot inspect task: {failures}")
            missing_count += 1
            if missing_count >= 30:
                raise RuntimeError(
                    "Task is still missing from DescribeTasks; inspect its ARN above."
                )
            time.sleep(POLL_SECONDS)
            continue
        missing_count = 0
        task = tasks[0]
        token, event_count = read_logs(region, log_group, task_arn, token)
        if task["lastStatus"] == "STOPPED":
            # CloudWatch ingestion may lag task completion. Drain until three quiet polls.
            quiet_stopped_polls = 0 if event_count else quiet_stopped_polls + 1
            if quiet_stopped_polls >= 3:
                container = next(
                    (item for item in task.get("containers", []) if item["name"] == "generator"),
                    {},
                )
                exit_code = container.get("exitCode")
                if exit_code != 0:
                    print(
                        f"Remote generator failed (exit code {exit_code}): "
                        f"{task.get('stoppedReason', '')} {container.get('reason', '')}".strip(),
                        file=sys.stderr,
                    )
                return exit_code if isinstance(exit_code, int) else 1
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    """Forward generator arguments verbatim and launch exactly one standalone task."""
    arguments = sys.argv[1:] if argv is None else argv
    task_arn: str | None = None
    region = cluster = ""
    try:
        outputs = terraform_outputs()
        required_outputs = (
            "aws_region",
            "generation_cluster_arn",
            "generation_task_definition_arn",
            "generation_subnet_ids",
            "generation_security_group_id",
            "generation_log_group_name",
        )
        for name in required_outputs:
            if not outputs.get(name):
                raise RuntimeError(f"Missing Terraform output {name}; deploy the generator first.")
        region = outputs["aws_region"]
        cluster = outputs["generation_cluster_arn"]
        response = aws_request(
            region,
            "ecs",
            "run-task",
            {
                "cluster": cluster,
                "taskDefinition": outputs["generation_task_definition_arn"],
                "launchType": "FARGATE",
                "platformVersion": "1.4.0",
                "count": 1,
                "clientToken": uuid.uuid4().hex,
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": outputs["generation_subnet_ids"],
                        "securityGroups": [outputs["generation_security_group_id"]],
                        "assignPublicIp": "ENABLED",
                    }
                },
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "generator",
                            # An empty override can preserve Docker CMD (--verbose). argparse's --
                            # means no arguments, preserving the local CLI's non-verbose default.
                            "command": arguments or ["--"],
                        }
                    ]
                },
            },
        )
        tasks = response.get("tasks", [])
        if not tasks:
            raise RuntimeError(f"Fargate launch failed: {response.get('failures', [])}")
        task_arn = tasks[0]["taskArn"]
        print(f"Remote task: {task_arn}", file=sys.stderr, flush=True)
        return follow_task(region, cluster, outputs["generation_log_group_name"], task_arn)
    except KeyboardInterrupt:
        if task_arn:
            try:
                aws_request(
                    region,
                    "ecs",
                    "stop-task",
                    {
                        "cluster": cluster,
                        "task": task_arn,
                        "reason": "Remote CLI interrupted",
                    },
                )
                print("Requested remote task stop.", file=sys.stderr)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Could not stop task {task_arn}: {error}", file=sys.stderr)
        else:
            print(
                "Interrupted during launch; inspect ECS for a task if launch was in progress.",
                file=sys.stderr,
            )
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Remote generator: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        if task_arn:
            print(
                f"Inspect remote task {task_arn}; monitoring errors do not stop it.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
