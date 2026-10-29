"""Exercise single-task SDK orchestration, resource limits, and read-only runtime controls."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
from test_validation import make_inputs

from the_bot_radio.generation import tools
from the_bot_radio.generation.agent import (
    PLAYLIST_SYSTEM_PROMPT,
    PlaylistAgent,
    PlaylistAgentResult,
)
from the_bot_radio.generation.models import PlaylistProposal
from the_bot_radio.generation.runtime import AgentLimits, LogTranscript, filesystem_guard
from the_bot_radio.generation.validation import PlaylistValidator
from the_bot_radio.services.reccobeats_service import ReccobeatsService
from the_bot_radio.services.spotify_service import SpotifyService

pytestmark = pytest.mark.anyio


def sdk_result(proposal: dict[str, Any] | None, **overrides: Any) -> ResultMessage:
    """Build the actual SDK result type without launching a CLI process."""
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 5,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
        "total_cost_usd": 0.1,
        "structured_output": proposal,
    }
    fields.update(overrides)
    return ResultMessage(**fields)


class FakeAgentClient:
    """Maintain a fake conversation and provide predefined response streams."""

    def __init__(self, responses: list[list[Any]], *, stall: bool = False) -> None:
        """Keep streams separate to detect unexpected follow-up queries."""
        self.responses = responses
        self.stall = stall
        self.prompts: list[str] = []
        self.entered = False
        self.exited = False
        self.interrupt = AsyncMock()

    async def __aenter__(self) -> "FakeAgentClient":
        """Simulate opening the SDK session."""
        self.entered = True
        return self

    async def __aexit__(self, *exception_details: Any) -> None:
        """Record cleanup on both successful and failed runs."""
        self.exited = True

    async def query(self, prompt: str) -> None:
        """Record another message without resetting the conversation."""
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        """Yield the selected stream, optionally waiting for timeout cancellation."""
        if self.stall:
            await asyncio.Event().wait()
        for message in self.responses[len(self.prompts) - 1]:
            yield message


async def run_agent(
    workspace: Path,
    client: FakeAgentClient,
    *,
    limits: AgentLimits | None = None,
    proposal_sink: Callable[[PlaylistProposal], None] | None = None,
) -> tuple[PlaylistAgentResult, ClaudeAgentOptions, list[dict[str, Any]]]:
    """Invoke the production service with fake external clients and captured options."""
    _, context = make_inputs()
    captured: list[ClaudeAgentOptions] = []
    events: list[dict[str, Any]] = []

    def factory(*, options: ClaudeAgentOptions) -> FakeAgentClient:
        captured.append(options)
        return client

    agent = PlaylistAgent(
        AsyncMock(spec=SpotifyService),
        PlaylistValidator(context),
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
        agent_client_factory=factory,
    )
    result = await agent.run(
        workspace=workspace,
        prompt="Today's selected persona and evidence.",
        event_sink=events.append,
        limits=limits,
        proposal_sink=proposal_sink,
    )
    assert len(captured) == 1
    return result, captured[0], events


async def test_success_returns_proposal_and_prompt_with_exact_tools_and_schema(
    tmp_path: Path,
) -> None:
    proposal, _ = make_inputs()
    client = FakeAgentClient([[sdk_result(proposal)]])
    result, options, events = await run_agent(tmp_path, client)

    assert result.proposal == PlaylistProposal.model_validate(proposal)
    assert json.loads(result.prompt) == {
        "system": PLAYLIST_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": client.prompts[0]}],
    }
    with pytest.raises(FrozenInstanceError):
        result.prompt = "changed"
    assert options.system_prompt == PLAYLIST_SYSTEM_PROMPT
    assert client.entered and client.exited
    assert len(client.prompts) == 1
    assert options.tools == ["Read", "Grep", "Glob"]
    assert set(options.allowed_tools) == {
        "Read",
        "Grep",
        "Glob",
        "StructuredOutput",
        "mcp__playlist__search_tracks",
        "mcp__playlist__validate_playlist",
        "mcp__playlist__get_audio_features",
        "mcp__playlist__get_recommendation",
    }
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []
    assert options.strict_mcp_config
    assert set(options.mcp_servers) == {"playlist"}
    assert options.output_format["type"] == "json_schema"
    assert options.max_turns == 40
    assert options.max_budget_usd == 5.0
    assert options.cwd == tmp_path
    assert "PreToolUse" in options.hooks
    assert any(event["event"] == "sdk_message" for event in events)
    assert not list(tmp_path.iterdir()), "The service must not write a daily file itself."


async def test_final_changed_metadata_fails_after_exactly_one_query(tmp_path: Path) -> None:
    proposal, _ = make_inputs()
    proposal["tracks"][0]["title"] = "Invented title after validation"
    client = FakeAgentClient([[sdk_result(proposal)]])
    with pytest.raises(ValueError, match="metadata"):
        await run_agent(tmp_path, client)
    assert len(client.prompts) == 1
    assert client.exited


async def test_proposal_sink_receives_accepted_output(tmp_path: Path) -> None:
    proposal, _ = make_inputs()
    client = FakeAgentClient([[sdk_result(proposal)]])
    captured: list[PlaylistProposal] = []
    result, _, _ = await run_agent(tmp_path, client, proposal_sink=captured.append)
    assert captured == [result.proposal]


async def test_proposal_sink_retains_rule_invalid_output_after_failure(tmp_path: Path) -> None:
    proposal, _ = make_inputs()
    proposal["tracks"][0]["title"] = "Wrong title"
    client = FakeAgentClient([[sdk_result(proposal)]])
    captured: list[PlaylistProposal] = []
    with pytest.raises(ValueError, match="validation"):
        await run_agent(tmp_path, client, proposal_sink=captured.append)
    assert len(captured) == 1
    assert captured[0].tracks[0].title == "Wrong title"
    assert len(client.prompts) == 1


async def test_schema_invalid_output_does_not_call_proposal_sink(tmp_path: Path) -> None:
    client = FakeAgentClient([[sdk_result({})]])
    captured: list[PlaylistProposal] = []
    with pytest.raises(ValueError, match="validation"):
        await run_agent(tmp_path, client, proposal_sink=captured.append)
    assert captured == []
    assert len(client.prompts) == 1
    assert client.exited


@pytest.mark.parametrize(
    "failure_fields",
    [
        {"is_error": True},
        {"stop_reason": "refusal"},
        {"terminal_reason": "api_error"},
        {"terminal_reason": "aborted"},
        {"subtype": "error_during_execution"},
    ],
)
async def test_failed_sdk_response_does_not_capture_stale_proposal(
    tmp_path: Path, failure_fields: dict[str, Any]
) -> None:
    proposal, _ = make_inputs()
    client = FakeAgentClient([[sdk_result(proposal, **failure_fields)]])
    captured: list[PlaylistProposal] = []

    with pytest.raises(ValueError, match="agent failed"):
        await run_agent(tmp_path, client, proposal_sink=captured.append)

    assert captured == []


@pytest.mark.parametrize(
    "responses",
    [
        [[]],
        [[sdk_result(None)]],
        [[sdk_result({}, subtype="error_max_turns", is_error=True)]],
        [[sdk_result({}, is_error=True, stop_reason="refusal")]],
        [[sdk_result({}, is_error=True, terminal_reason="api_error")]],
    ],
)
async def test_sdk_failures_are_not_saved_or_repaired(
    tmp_path: Path, responses: list[list[Any]]
) -> None:
    client = FakeAgentClient(responses)
    with pytest.raises(ValueError):
        await run_agent(tmp_path, client)
    assert len(client.prompts) == 1
    assert client.exited
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("overrides", [{"num_turns": 41}, {"total_cost_usd": 5.1}])
async def test_run_limits_reject_even_valid_output(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    proposal, _ = make_inputs()
    client = FakeAgentClient([[sdk_result(proposal, **overrides)]])
    captured: list[PlaylistProposal] = []
    with pytest.raises(ValueError, match="limit"):
        await run_agent(tmp_path, client, proposal_sink=captured.append)
    assert captured == [PlaylistProposal.model_validate(proposal)]
    assert len(client.prompts) == 1
    assert client.exited


async def test_usage_records_single_result_cost_and_turn_count(tmp_path: Path) -> None:
    proposal, _ = make_inputs()
    client = FakeAgentClient([[sdk_result(proposal, total_cost_usd=4.0, num_turns=3)]])
    _, _, events = await run_agent(tmp_path, client)
    assert [event for event in events if event["event"] == "usage"] == [
        {"event": "usage", "cost_usd": 4.0, "turns": 3}
    ]
    assert not any(event["event"] == "attempt" for event in events)
    assert len(client.prompts) == 1


async def test_wall_deadline_cleans_up_sdk_session(tmp_path: Path) -> None:
    client = FakeAgentClient([[]], stall=True)
    with pytest.raises(TimeoutError):
        await run_agent(tmp_path, client, limits=AgentLimits(deadline_seconds=0.01))
    assert client.exited


async def test_assistant_frames_with_same_id_count_as_one_turn(tmp_path: Path) -> None:
    proposal, _ = make_inputs()
    frames = [
        AssistantMessage(
            content=[TextBlock(text="One frame")], model="test-model", message_id="same-turn"
        )
        for _ in range(3)
    ]
    client = FakeAgentClient([[*frames, sdk_result(proposal)]])
    result, _, _ = await run_agent(tmp_path, client, limits=AgentLimits(max_turns=1))
    assert result.proposal == PlaylistProposal.model_validate(proposal)
    client.interrupt.assert_not_awaited()


async def test_turn_limit_interrupts_stream_before_final_result(tmp_path: Path) -> None:
    proposal, _ = make_inputs()
    frames = [
        AssistantMessage(
            content=[TextBlock(text="Another step")], model="test-model", message_id=identifier
        )
        for identifier in ["turn-one", "turn-two", "turn-three"]
    ]
    client = FakeAgentClient([[*frames, sdk_result(proposal)]])
    with pytest.raises(ValueError, match="turn limit exceeded"):
        await run_agent(tmp_path, client, limits=AgentLimits(max_turns=2))
    assert len(client.prompts) == 1
    client.interrupt.assert_awaited_once()
    assert client.exited


async def test_repeated_runs_have_independent_streamed_turn_ids_and_usage(tmp_path: Path) -> None:
    proposal, context = make_inputs()
    clients = [
        FakeAgentClient(
            [
                [
                    AssistantMessage(
                        content=[TextBlock(text="Selecting")],
                        model="test-model",
                        message_id=identifier,
                    ),
                    sdk_result(proposal, total_cost_usd=4.0),
                ]
            ]
        )
        for identifier in ["first-run", "second-run"]
    ]
    pending_clients = iter(clients)
    agent = PlaylistAgent(
        AsyncMock(spec=SpotifyService),
        PlaylistValidator(context),
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
        agent_client_factory=lambda **kwargs: next(pending_clients),
    )
    events: list[dict[str, Any]] = []
    for prompt in ["First run", "Second run"]:
        result = await agent.run(
            workspace=tmp_path,
            prompt=prompt,
            event_sink=events.append,
            limits=AgentLimits(max_turns=1),
        )
        assert json.loads(result.prompt)["messages"] == [{"role": "user", "content": prompt}]
    assert [event["cost_usd"] for event in events if event["event"] == "usage"] == [4.0, 4.0]
    assert all(client.entered and client.exited for client in clients)
    assert all(len(client.prompts) == 1 for client in clients)


async def test_agent_can_validate_and_revise_within_single_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, _ = make_inputs()
    registered_tools: list[Any] = []

    def create_server(**arguments: Any) -> dict[str, Any]:
        registered_tools.extend(arguments["tools"])
        return {"type": "sdk", "name": arguments["name"], "instance": object()}

    monkeypatch.setattr(tools, "create_sdk_mcp_server", create_server)

    class ValidatingAgentClient(FakeAgentClient):
        """Exercise the registered validation handler before submitting a final answer."""

        async def receive_response(self) -> AsyncIterator[Any]:
            """Check an invalid draft and corrected proposal within one SDK response."""
            validation_tool = next(
                tool for tool in registered_tools if tool.name == "validate_playlist"
            )
            invalid_result = await validation_tool.handler({"proposal": {}})
            assert not json.loads(invalid_result["content"][0]["text"])["valid"]
            valid_result = await validation_tool.handler({"proposal": proposal})
            assert json.loads(valid_result["content"][0]["text"])["valid"]
            yield sdk_result(proposal)

    client = ValidatingAgentClient([[]])
    result, _, events = await run_agent(tmp_path, client)
    assert result.proposal == PlaylistProposal.model_validate(proposal)
    assert len(client.prompts) == 1
    assert [event["valid"] for event in events if event["event"] == "playlist_validation"] == [
        False,
        True,
    ]


@pytest.mark.parametrize(
    "terminal_fields",
    [{"stop_reason": "refusal"}, {"terminal_reason": "api_error"}],
)
async def test_explicit_terminal_failure_rejects_stale_structured_output(
    tmp_path: Path, terminal_fields: dict[str, Any]
) -> None:
    proposal, _ = make_inputs()
    client = FakeAgentClient([[sdk_result(proposal, is_error=False, **terminal_fields)]])
    with pytest.raises(ValueError, match="agent failed"):
        await run_agent(tmp_path, client)
    assert len(client.prompts) == 1


@pytest.mark.parametrize(
    "limits",
    [{"max_turns": 0}, {"budget_usd": 0}, {"budget_usd": float("inf")}, {"deadline_seconds": -1}],
)
def test_limits_reject_invalid_configuration(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AgentLimits(**limits)


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
async def test_guard_allows_inside_paths_and_denies_escape(tmp_path: Path, tool: str) -> None:
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    (workspace / "catalog.jsonl").write_text("{}\n")
    guard = filesystem_guard(workspace)
    path_key = "file_path" if tool == "Read" else "path"
    inside = {path_key: "catalog.jsonl" if tool == "Read" else ".", "pattern": "*"}
    assert await guard({"tool_name": tool, "tool_input": inside}, None, None) == {}
    for invalid_path in ["../outside", str(tmp_path / "outside"), ["invalid"]]:
        decision = await guard(
            {"tool_name": tool, "tool_input": {path_key: invalid_path}}, None, None
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_guard_rejects_symlink_traversal_and_unsafe_glob_patterns(tmp_path: Path) -> None:
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    target = workspace / "real.json"
    target.write_text("{}")
    (workspace / "inside-link").symlink_to(target)
    (workspace / "outside-link").symlink_to(tmp_path)
    guard = filesystem_guard(workspace)
    for tool, arguments in [
        ("Read", {"file_path": "inside-link"}),
        ("Read", {"file_path": "outside-link/secret"}),
        ("Glob", {"pattern": "../*"}),
        ("Glob", {"pattern": str(tmp_path / "*")}),
        ("Grep", {"pattern": "secret"}),
    ]:
        decision = await guard({"tool_name": tool, "tool_input": arguments}, None, None)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_guard_allows_internal_output_but_denies_mutating_tools(tmp_path: Path) -> None:
    guard = filesystem_guard(tmp_path)
    for tool in [
        "StructuredOutput",
        "mcp__playlist__search_tracks",
        "mcp__playlist__validate_playlist",
        "mcp__playlist__get_audio_features",
        "mcp__playlist__get_recommendation",
    ]:
        assert await guard({"tool_name": tool}, None, None) == {}
    for tool in ["Bash", "Write", "Edit", "Agent", "mcp__other__search", "mcp__playlist__delete"]:
        decision = await guard({"tool_name": tool}, None, None)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (await guard({"tool_name": "Read", "tool_input": []}, None, None))["hookSpecificOutput"][
        "permissionDecision"
    ] == "deny"


def test_transcript_redacts_environment_credentials_and_normalizes_sdk_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = 'credential-with-"quotes"-and-newline\nvalue'
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", secret)
    path = tmp_path / "artifacts" / "transcript.jsonl"
    console = StringIO()
    transcript = LogTranscript(path, console=console)
    transcript.append({"event": "diagnostic", "message": secret})
    transcript.append({"event": "sdk_message", "message": sdk_result(None)})

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["message"] == "[REDACTED]"
    assert lines[1]["message"]["session_id"] == "test-session"
    assert secret not in path.read_text()
    assert console.getvalue() == path.read_text()


async def test_recommendations_need_search_evidence_and_feature_failure_allows_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Research tools propagate injected dependencies without changing discovery acceptance."""
    proposal, context = make_inputs()
    discovery_id = proposal["tracks"][5]["spotify_id"]
    canonical_track = context.search_results.pop(discovery_id)
    spotify_service = AsyncMock(spec=SpotifyService)
    spotify_service.search_tracks.return_value = [canonical_track]
    reccobeats_service = AsyncMock(spec=ReccobeatsService)
    reccobeats_service.get_recommendation.return_value = {
        "content": [
            {
                "id": "reccobeats-id",
                "href": f"https://open.spotify.com/track/{discovery_id}",
                "trackTitle": canonical_track.name,
            }
        ]
    }
    reccobeats_service.get_audio_features.side_effect = RuntimeError("private provider URL")
    registered_tools: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    def create_server(**arguments: Any) -> dict[str, Any]:
        """Keep the actual registered adapters available to the fake SDK flow."""
        registered_tools.update({tool.name: tool for tool in arguments["tools"]})
        return {"type": "sdk", "name": arguments["name"], "instance": object()}

    monkeypatch.setattr(tools, "create_sdk_mcp_server", create_server)

    class ResearchingAgentClient(FakeAgentClient):
        """Try optional research, then obtain authoritative Spotify evidence."""

        async def receive_response(self) -> AsyncIterator[Any]:
            """Reject a recommendation until search verifies its canonical recording."""
            recommendation = await registered_tools["get_recommendation"].handler(
                {"seeds": [proposal["tracks"][0]["spotify_id"]]}
            )
            assert not recommendation["isError"]
            assert discovery_id not in context.search_results
            features = await registered_tools["get_audio_features"].handler({"ids": [discovery_id]})
            assert features["isError"]
            assert "private provider URL" not in json.dumps(features)
            invalid_result = await registered_tools["validate_playlist"].handler(
                {"proposal": proposal}
            )
            invalid_payload = json.loads(invalid_result["content"][0]["text"])
            assert not invalid_payload["valid"]
            assert {violation["code"] for violation in invalid_payload["violations"]} == {
                "unverified_wildcard"
            }
            await registered_tools["search_tracks"].handler({"query": "Song 6 Artist 6"})
            valid_result = await registered_tools["validate_playlist"].handler(
                {"proposal": proposal}
            )
            assert json.loads(valid_result["content"][0]["text"])["valid"]
            yield sdk_result(proposal)

    client = ResearchingAgentClient([[]])
    agent = PlaylistAgent(
        spotify_service,
        PlaylistValidator(context),
        reccobeats_service=reccobeats_service,
        agent_client_factory=lambda **kwargs: client,
    )
    result = await agent.run(workspace=tmp_path, prompt="Select tracks", event_sink=events.append)
    assert result.proposal == PlaylistProposal.model_validate(proposal)
    reccobeats_service.get_recommendation.assert_awaited_once()
    recommendation_request = reccobeats_service.get_recommendation.await_args.args[0]
    assert recommendation_request.seeds == (proposal["tracks"][0]["spotify_id"],)
    assert recommendation_request.size == 10
    reccobeats_service.get_audio_features.assert_awaited_once()
    spotify_service.search_tracks.assert_awaited_once_with("Song 6 Artist 6", limit=10, offset=0)
    assert [
        event["status"] for event in events if event["event"] == "reccobeats_audio_features"
    ] == ["error"]
    assert [event["valid"] for event in events if event["event"] == "playlist_validation"] == [
        False,
        True,
    ]
    assert any(event["event"] == "final_validation" and event["valid"] for event in events)
    assert len(client.prompts) == 1
