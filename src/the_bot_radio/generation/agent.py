"""Run one tool-assisted playlist task and validate its final creative proposal."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
)
from pydantic import ValidationError

from the_bot_radio.generation.models import PlaylistProposal
from the_bot_radio.generation.runtime import AgentLimits, EventSink, filesystem_guard, json_value
from the_bot_radio.generation.tools import build_tool_server
from the_bot_radio.generation.validation import PlaylistValidator
from the_bot_radio.services.claude_service import MODEL
from the_bot_radio.services.reccobeats_service import ReccobeatsService
from the_bot_radio.services.spotify_service import SpotifyService

PLAYLIST_SYSTEM_PROMPT = """\
You are the selected resident DJ at The Bot Radio in Greenpoint, Brooklyn.
The Bot Radio is an AI-run spoof of Brooklyn's independent online radio, The Lot Radio.

Each morning, a bot DJ turns neighborhood signals such as weather, transit, and tides into a Spotify
playlist, with liner notes explaining every pick.

Read the supplied run context, catalog, recent published playlists, and output schemas.
The user message contains the selected persona under "persona" and run context under "inputs":
run date, timestamp, timezone,
selected DJ ID, input readings, mood based on input readings, and history coverage.
Your workspace contains:
- catalog.jsonl: the shared catalog with canonical track metadata and audio features.
- history/: published playlists from the preceding thirty days.
- schemas/playlist_proposal.schema.json: the required structure for your final proposal.
Read any of these files and use grep/glob. Do not write to them.

Use the MCP tools to develop and verify your set:
- mcp__playlist__get_recommendation: when you want to explore discoveries around recordings
  that suit the persona and today's mood. Choose suitable seeds and adjust song feature targets to
  explore the musical direction you want.
- mcp__playlist__get_audio_features: when additional musical evidence would help you compare
  candidates or plan transitions. Request features for the candidates under consideration.
- mcp__playlist__search_tracks: before selecting any discovery, including a recommendation.
  Search by title and artist, compare recordings and versions, and select only a result whose
  canonical metadata matches your intended recording.
- mcp__playlist__validate_playlist: once you have a complete draft. Fix reported violations
  and validate again before submitting your final proposal.

get_recommendation and get_audio_features are optional research aids.
If data is missing or a tool fails, continue using catalog evidence and Spotify search.

The selected persona's guidance and speaking voice are supplied in the user message.
Follow the selected persona's musical guidance and speaking voice.

Choose exactly ten tracks: five from the shared catalog and five discoveries absent from it.

Bounds:
    - Give the playlist a descriptive, creative title of two to four words, counted by whitespace.
      Do not copy any selected track's title, ignoring case and whitespace. Do not include a date;
      the host adds the run date to the daily title and Spotify playlist name.
    - No Spotify track ID may repeat from the previous thirty days.
    - Primary artists (the first credited artist) must be unique within the set;
      featured collaborators may recur.
    - Use the mood from context and the readings' actual dates and freshness. Skipped sources
      and stale readings without usable observations contribute no effect: use an empty effect and
      null weight. Provide exactly one effect entry for every reading key; DJ-roll weight is null.

Develop a coherent musical arc, explaining every selection with specific musical detail.
Return only the proposal's creative fields. The host supplies run and publication metadata
and writes the daily record + playlist.
"""


@dataclass(frozen=True)
class PlaylistAgentResult:
    """Accepted creative proposal and archived system prompt and initial user message."""

    proposal: PlaylistProposal
    prompt: str


class PlaylistAgent:
    """Own Agent SDK orchestration for one playlist task per invocation."""

    def __init__(
        self,
        spotify_service: SpotifyService,
        validator: PlaylistValidator,
        *,
        reccobeats_service: ReccobeatsService,
        agent_client_factory: Callable[..., Any] = ClaudeSDKClient,
    ) -> None:
        """Use Spotify evidence tools, a run validator, and an injectable SDK client factory."""
        self.reccobeats_service = reccobeats_service
        self.spotify_service = spotify_service
        self.validator = validator
        # Factory injection lets tests use a fake client without launching the Agent SDK.
        self.agent_client_factory = agent_client_factory

    async def run(
        self,
        *,
        workspace: Path,
        prompt: str,
        event_sink: EventSink,
        proposal_sink: Callable[[PlaylistProposal], None] | None = None,
        model: str = MODEL,
        limits: AgentLimits | None = None,
    ) -> PlaylistAgentResult:
        """Run one bounded task without a host follow-up if final validation fails."""
        limits = limits or AgentLimits()
        options = self._build_options(workspace, model, limits, event_sink)
        async with asyncio.timeout(limits.deadline_seconds):
            async with self.agent_client_factory(options=options) as agent_client:
                result = await self._receive_response(agent_client, prompt, limits, event_sink)
                proposal = self._process_response(result, limits, event_sink, proposal_sink)
        archived_prompt = json.dumps(
            {"system": PLAYLIST_SYSTEM_PROMPT, "messages": [{"role": "user", "content": prompt}]},
            ensure_ascii=False,
        )
        return PlaylistAgentResult(proposal=proposal, prompt=archived_prompt)

    def _build_options(
        self,
        workspace: Path,
        model: str,
        limits: AgentLimits,
        event_sink: EventSink,
    ) -> ClaudeAgentOptions:
        """Configure evidence tools, filesystem permissions, schema, and SDK limits."""
        return ClaudeAgentOptions(
            cwd=workspace,
            model=model,
            system_prompt=PLAYLIST_SYSTEM_PROMPT,
            tools=["Read", "Grep", "Glob"],
            allowed_tools=[
                "Read",
                "Grep",
                "Glob",
                "StructuredOutput",
                "mcp__playlist__search_tracks",
                "mcp__playlist__validate_playlist",
                "mcp__playlist__get_audio_features",
                "mcp__playlist__get_recommendation",
            ],
            permission_mode="dontAsk",
            setting_sources=[],
            strict_mcp_config=True,
            mcp_servers={
                "playlist": build_tool_server(
                    self.validator,
                    self.spotify_service,
                    event_sink,
                    reccobeats_service=self.reccobeats_service,
                )
            },
            hooks={"PreToolUse": [HookMatcher(hooks=[filesystem_guard(workspace)])]},
            output_format={"type": "json_schema", "schema": PlaylistProposal.model_json_schema()},
            max_turns=limits.max_turns,
            max_budget_usd=limits.budget_usd,
        )

    async def _receive_response(
        self,
        agent_client: Any,
        prompt: str,
        limits: AgentLimits,
        event_sink: EventSink,
    ) -> ResultMessage | None:
        """Send one query, log its stream, and interrupt excessive assistant turns."""
        await agent_client.query(prompt)
        result: ResultMessage | None = None
        response_turn_ids: set[str] = set()
        async for message in agent_client.receive_response():
            event_sink({"event": "sdk_message", "message": json_value(message)})
            if isinstance(message, AssistantMessage) and message.message_id:
                response_turn_ids.add(message.message_id)
                if len(response_turn_ids) > limits.max_turns:
                    await agent_client.interrupt()
                    raise ValueError("agent turn limit exceeded")
            if isinstance(message, ResultMessage):
                result = message
        return result

    def _process_response(
        self,
        result: ResultMessage | None,
        limits: AgentLimits,
        event_sink: EventSink,
        proposal_sink: Callable[[PlaylistProposal], None] | None,
    ) -> PlaylistProposal:
        """Capture successful schema-valid output before enforcing limits and playlist rules."""
        if result is None:
            raise ValueError("agent returned no result")
        cost_usd = result.total_cost_usd or 0.0
        event_sink({"event": "usage", "cost_usd": cost_usd, "turns": result.num_turns})
        if (
            result.subtype != "success"
            or result.is_error
            or result.structured_output is None
            or result.stop_reason == "refusal"
            or result.terminal_reason in {"api_error", "aborted"}
        ):
            raise ValueError(f"agent failed: {result.subtype}")
        try:
            proposal = PlaylistProposal.model_validate(result.structured_output)
        except ValidationError:
            proposal = None
        else:
            if proposal_sink is not None:
                proposal_sink(proposal)
        if cost_usd > limits.budget_usd or result.num_turns > limits.max_turns:
            raise ValueError("agent run budget or turn limit exceeded")
        validation = self.validator.validate_playlist(result.structured_output)
        event_sink({"event": "final_validation", **validation.model_dump()})
        if not validation.valid:
            raise ValueError(f"playlist validation failed: {validation.model_dump_json()}")
        assert proposal is not None
        return proposal
