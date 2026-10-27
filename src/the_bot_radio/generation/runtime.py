"""SDK limits, read-only filesystem access, and append-only run diagnostics."""

import dataclasses
import json
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

EventSink = Callable[[dict[str, Any]], None]


@dataclasses.dataclass(frozen=True)
class AgentLimits:
    """Limits for one agent task, excluding input setup and standalone mood scoring."""

    max_turns: int = 40
    budget_usd: float = 5.0
    deadline_seconds: float = 600.0

    def __post_init__(self) -> None:
        """Reject nonpositive and nonfinite resource limits before starting a run."""
        import math

        if self.max_turns < 1 or not all(
            math.isfinite(value) and value > 0 for value in (self.budget_usd, self.deadline_seconds)
        ):
            raise ValueError("agent limits must be finite and positive")


def json_value(value: Any) -> Any:
    """Convert SDK dataclasses and application models to JSON-compatible values."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: json_value(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, (datetime, date, Path)):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return type(value).__name__


def write_json_atomic(path: Path, payload: Any) -> None:
    """Replace one run artifact with complete JSON, cleaning up on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output_file:
            temporary_path = Path(output_file.name)
            json.dump(json_value(payload), output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_diagnostics(artifact_dir: Path, **updates: Any) -> None:
    """Merge progress into the host-only diagnostics file without losing earlier evidence."""
    path = artifact_dir / "diagnostics.json"
    diagnostics = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    diagnostics.update(updates)
    write_json_atomic(path, diagnostics)


class LogTranscript:
    """Append JSONL events with environment credential values redacted."""

    def __init__(self, path: Path) -> None:
        """Create an artifact-only log, separate from the readable snapshot."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.secrets = [
            value
            for key, value in os.environ.items()
            if value and len(value) >= 6 and any(word in key for word in ("TOKEN", "KEY", "SECRET"))
        ]

    def append(self, event: dict[str, Any]) -> None:
        """Write one normalized event without serializing clients or process environments."""
        encoded = json.dumps(json_value(event), ensure_ascii=False)
        for secret in self.secrets:
            encoded = encoded.replace(json.dumps(secret)[1:-1], "[REDACTED]")
        with self.path.open("a", encoding="utf-8") as output_file:
            output_file.write(encoded + "\n")


def filesystem_guard(workspace: Path) -> Callable[..., Any]:
    """Build a PreToolUse guard enforcing the agent's workspace boundary.

    The SDK's cwd sets a working directory, not a filesystem access boundary. Read,
    Grep, and Glob could otherwise access files outside the snapshot, including .env
    credentials and sibling run artifacts. Restrict their paths to the workspace and
    reject symlink escapes so only the intended input snapshot is readable.
    """
    workspace_root = workspace.resolve()
    permitted_tools = {
        "Read",
        "Grep",
        "Glob",
        "StructuredOutput",
        "mcp__playlist__search_tracks",
        "mcp__playlist__validate_playlist",
    }

    async def guard(
        hook_input: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Allow intended tools and reject outside paths and symlink escapes."""
        tool_name = hook_input.get("tool_name", "")
        denied_reason: str | None = None
        if tool_name not in permitted_tools:
            denied_reason = "This tool is unavailable in playlist generation."
        elif tool_name in {"Read", "Grep", "Glob"}:
            arguments = hook_input.get("tool_input", {})
            if not isinstance(arguments, dict):
                arguments = {}
                denied_reason = "Tool arguments must be an object."
            path_value = arguments.get("file_path" if tool_name == "Read" else "path")
            if path_value is not None and not isinstance(path_value, str):
                denied_reason = "Snapshot paths must be strings."
            elif tool_name == "Read" and not path_value:
                denied_reason = "A snapshot file path is required."
            elif denied_reason is None:
                requested_path = Path(path_value or ".")
                absolute_path = (
                    requested_path
                    if requested_path.is_absolute()
                    else workspace_root / requested_path
                )
                resolved_path = absolute_path.resolve()
                if not resolved_path.is_relative_to(workspace_root):
                    denied_reason = "Read and search paths must stay inside the run snapshot."
                elif any(parent.is_symlink() for parent in [absolute_path, *absolute_path.parents]):
                    denied_reason = "Symlinks are unavailable in the run snapshot."
            if tool_name == "Glob":
                pattern = str(arguments.get("pattern", ""))
                if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    denied_reason = "Glob patterns must stay inside the run snapshot."
            # A search of a directory must not traverse links to outside data.
            if denied_reason is None and tool_name in {"Grep", "Glob"}:
                if any(path.is_symlink() for path in workspace_root.rglob("*")):
                    denied_reason = "Snapshot searches cannot traverse symlinks."
        if denied_reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": denied_reason,
                }
            }
        return {}

    return guard
