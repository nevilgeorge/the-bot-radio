"""Offline daily-command acceptance, persistence, and opt-in live qualification."""

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from the_bot_radio.catalog import CatalogTrack, load_catalog
from the_bot_radio.generation import daily
from the_bot_radio.generation.agent import PlaylistAgentResult
from the_bot_radio.generation.models import DailyRecord, PlaylistProposal, normalize_artist
from the_bot_radio.generation.preparation import PreparedRun
from the_bot_radio.generation.runtime import AgentLimits
from the_bot_radio.generation.tools import PlaylistTools
from the_bot_radio.generation.validation import PlaylistValidator, assemble_daily_record
from the_bot_radio.services.inputs import runner
from the_bot_radio.services.inputs.base import TZ, Reading
from the_bot_radio.services.spotify_service import SpotifyTrack
from the_bot_radio.web.models import Mood

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 6, tzinfo=TZ)


@pytest.fixture
def generation_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Provide deterministic catalog/persona evidence and real tool adapters with fake APIs."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    tracks = [
        {
            "position": index,
            "spotify_id": f"{index:022d}",
            "spotify_url": f"https://open.spotify.com/track/{index:022d}",
            "title": f"Song {index}",
            "artists": [f"Artist {index}"],
            "duration_ms": 180000 + index,
            "reason": "Its texture fits the rain.",
            "wildcard": index > 5,
        }
        for index in range(1, 11)
    ]
    catalog_tracks = [
        CatalogTrack(
            **{
                key: track[key]
                for key in ("spotify_id", "spotify_url", "title", "artists", "duration_ms")
            },
            album="Fixture album",
        )
        for track in tracks[:5]
    ]
    (data_dir / "shared_catalog.jsonl").write_text(
        "".join(track.model_dump_json() + "\n" for track in catalog_tracks)
    )
    (data_dir / "personas.json").write_text(
        json.dumps(
            [
                {
                    "id": "resident",
                    "name": "Resident",
                    "bio": "An attentive DJ.",
                    "agent_instructions": "Choose textured songs.",
                    "speaking_voice": "Warm and concise.",
                }
            ]
        )
    )
    readings = [
        Reading(
            key="weather",
            label="Weather",
            value="Rain",
            data={"temp_f": 52},
            raw={"forecast": "rain"},
            fetched_at=NOW,
        )
    ]
    monkeypatch.setattr(runner, "read_all", AsyncMock(return_value=readings))
    spotify_tracks = [
        SpotifyTrack(
            id=track["spotify_id"],
            uri=f"spotify:track:{track['spotify_id']}",
            name=track["title"],
            artists=track["artists"],
            album="Fixture album",
            duration_ms=track["duration_ms"],
            explicit=False,
        )
        for track in tracks[5:]
    ]
    spotify_service = SimpleNamespace(search_tracks=AsyncMock(return_value=spotify_tracks))

    prepared_runs: list[PreparedRun] = []
    prepare_run = daily.prepare_run

    async def capture_prepared_run(*args: Any, **kwargs: Any) -> PreparedRun:
        """Retain trusted setup state for the injected agent's evidence tools."""
        prepared_run = await prepare_run(*args, **kwargs)
        prepared_runs.append(prepared_run)
        return prepared_run

    monkeypatch.setattr(daily, "prepare_run", capture_prepared_run)

    async def run_agent(**kwargs: Any) -> PlaylistAgentResult:
        """Exercise search retention and tool feedback without assembling a daily record."""
        context = prepared_runs[-1].context
        adapters = PlaylistTools(PlaylistValidator(context), spotify_service, kwargs["event_sink"])
        search_result = await adapters.search_tracks({"query": "fixture songs"})
        assert not search_result["isError"]
        proposal = PlaylistProposal.model_validate(
            {
                "title": "Rain on the avenue",
                "description": "Ten tracks for Greenpoint.",
                "liner_notes": ["A patient set for the rain."],
                "tracks": tracks,
                "effects": [
                    {"key": reading.key, "effect": "A warm selection", "weight": None}
                    for reading in context.readings
                ],
            }
        )
        kwargs["proposal_sink"](proposal)
        feedback = await adapters.validate_playlist({"proposal": proposal.model_dump()})
        assert json.loads(feedback["content"][0]["text"])["valid"]
        return PlaylistAgentResult(proposal=proposal, prompt=kwargs["prompt"])

    claude_service = SimpleNamespace(
        score_mood=AsyncMock(return_value=Mood(tempo_bpm=90, energy=4, brightness=3, obscurity=7)),
    )
    return SimpleNamespace(
        data_dir=data_dir,
        claude=claude_service,
        spotify=spotify_service,
        agent=SimpleNamespace(run=AsyncMock(side_effect=run_agent)),
        run_agent=run_agent,
        prepared_runs=prepared_runs,
    )


async def generate(services: SimpleNamespace) -> Path:
    """Invoke the real local job with injected provider boundaries."""
    return await daily.generate_day(
        services.data_dir,
        RUN_DATE,
        now=NOW,
        claude_service=services.claude,
        spotify_service=services.spotify,
        playlist_agent=services.agent,
    )


async def test_offline_end_to_end_and_idempotent_rerun(generation_services, monkeypatch):
    services = generation_services
    output_path = await generate(services)
    record = DailyRecord.model_validate_json(output_path.read_bytes())
    assert record.publication_status == "pending"
    assert record.spotify_playlist_id is None
    assert len(record.tracks) == 10
    assert sum(track.wildcard for track in record.tracks) == 5
    assert record.inputs[1].raw == {"forecast": "rain"}
    assert services.spotify.search_tracks.await_count == 1
    artifacts = list((services.data_dir / "runs/2026-10-27").glob("*/diagnostics.json"))
    assert len(artifacts) == 1
    diagnostics = json.loads(artifacts[0].read_text())
    assert diagnostics["status"] == "accepted"
    assert diagnostics["date"] == RUN_DATE.isoformat()
    assert diagnostics["inputs"][1]["raw"] == {"forecast": "rain"}
    assert "mood_usage" in diagnostics
    proposal_payload = json.loads(artifacts[0].with_name("proposal.json").read_text())
    saved_proposal = PlaylistProposal.model_validate(proposal_payload)
    assert saved_proposal.title == record.title
    assert [track.model_dump() for track in saved_proposal.tracks] == [
        track.model_dump(exclude={"artist"}) for track in record.tracks
    ]
    assert saved_proposal.effects[1].effect == record.inputs[1].effect
    assert set(proposal_payload) == {"title", "description", "liner_notes", "tracks", "effects"}
    events = [
        json.loads(line)
        for line in artifacts[0].with_name("transcript.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "spotify_search" for event in events)
    assert any(event["event"] == "playlist_validation" for event in events)
    workspace = artifacts[0].parent / "workspace"
    assert {path.name for path in workspace.iterdir()} == {
        "context.json",
        "catalog.jsonl",
        "history",
        "schemas",
    }
    assert not list(workspace.rglob("diagnostics.json"))
    original_bytes = output_path.read_bytes()

    def fail_if_constructed(*args: Any, **kwargs: Any) -> None:
        """Ensure successful reruns do not need credentials or provider clients."""
        raise AssertionError("provider construction on a no-op rerun")

    monkeypatch.setattr(daily, "ClaudeService", fail_if_constructed)
    monkeypatch.setattr(daily, "SpotifyService", fail_if_constructed)
    monkeypatch.setattr(daily, "PlaylistAgent", fail_if_constructed)
    assert await daily.generate_day(services.data_dir, RUN_DATE, now=NOW) == output_path
    assert output_path.read_bytes() == original_bytes
    assert services.agent.run.await_count == 1


def test_same_date_lock_rejects_concurrent_run_and_allows_other_date(tmp_path):
    with daily.generation_lock(tmp_path, RUN_DATE):
        with pytest.raises(ValueError, match="another generation"):
            with daily.generation_lock(tmp_path, RUN_DATE):
                pytest.fail("concurrent lock acquired")
        with daily.generation_lock(tmp_path, date(2026, 10, 28)):
            pass
    with daily.generation_lock(tmp_path, RUN_DATE):
        pass


async def test_save_is_atomic_and_never_replaces_existing_record(generation_services):
    source_path = await generate(generation_services)
    record = DailyRecord.model_validate_json(source_path.read_bytes())
    new_path = source_path.with_name("copy.json")
    daily.save_daily_record(new_path, record)
    original_bytes = new_path.read_bytes()
    record.title = "Replacement"
    with pytest.raises(FileExistsError):
        daily.save_daily_record(new_path, record)
    assert new_path.read_bytes() == original_bytes
    assert not list(new_path.parent.glob(".copy.json.*"))


async def test_link_failure_cleans_temporary_file(generation_services, monkeypatch):
    source_path = await generate(generation_services)
    record = DailyRecord.model_validate_json(source_path.read_bytes())
    destination = source_path.with_name("interrupted.json")

    def fail_link(*args: Any) -> None:
        """Simulate a filesystem failure before the accepted name becomes visible."""
        raise OSError("link failed")

    monkeypatch.setattr(daily.os, "link", fail_link)
    with pytest.raises(OSError, match="link failed"):
        daily.save_daily_record(destination, record)
    assert not destination.exists()
    assert not list(destination.parent.glob(".interrupted.json.*"))


async def test_existing_malformed_day_is_preserved(generation_services):
    services = generation_services
    output_path = services.data_dir / "days/2026-10-27.json"
    output_path.parent.mkdir()
    output_path.write_text("malformed fixture")
    with pytest.raises(ValueError, match="existing daily file is invalid"):
        await generate(services)
    assert output_path.read_text() == "malformed fixture"
    services.agent.run.assert_not_awaited()


async def test_provider_failure_records_outcome_without_a_daily_file(generation_services):
    services = generation_services
    services.agent.run.side_effect = RuntimeError("generation failed")
    with pytest.raises(RuntimeError, match="generation failed"):
        await generate(services)
    assert not (services.data_dir / "days/2026-10-27.json").exists()
    [diagnostics_path] = (services.data_dir / "runs/2026-10-27").glob("*/diagnostics.json")
    diagnostics = json.loads(diagnostics_path.read_text())
    assert diagnostics["status"] == "failed"
    assert diagnostics["error_type"] == "RuntimeError"
    assert diagnostics["date"] == RUN_DATE.isoformat()
    assert diagnostics["inputs"][1]["raw"] == {"forecast": "rain"}
    assert not diagnostics_path.with_name("proposal.json").exists()


async def test_malformed_structured_response_does_not_save_proposal(generation_services):
    services = generation_services
    services.agent.run.side_effect = ValueError("malformed structured output")
    with pytest.raises(ValueError, match="malformed structured output"):
        await generate(services)
    [diagnostics_path] = (services.data_dir / "runs/2026-10-27").glob("*/diagnostics.json")
    assert json.loads(diagnostics_path.read_text())["status"] == "failed"
    assert not diagnostics_path.with_name("proposal.json").exists()
    assert not (services.data_dir / "days/2026-10-27.json").exists()


async def test_rule_invalid_structured_proposal_is_retained_without_daily_file(generation_services):
    services = generation_services

    async def return_invalid_proposal(**kwargs: Any) -> PlaylistAgentResult:
        """Archive a structurally valid proposal before reporting its rule violations."""
        result = await services.run_agent(**kwargs)
        proposal = result.proposal
        proposal.tracks[0].wildcard = True
        kwargs["proposal_sink"](proposal)
        raise ValueError("playlist validation failed")

    services.agent.run.side_effect = return_invalid_proposal
    with pytest.raises(ValueError, match="playlist validation failed"):
        await generate(services)
    [diagnostics_path] = (services.data_dir / "runs/2026-10-27").glob("*/diagnostics.json")
    assert json.loads(diagnostics_path.read_text())["status"] == "failed"
    proposal = PlaylistProposal.model_validate_json(
        diagnostics_path.with_name("proposal.json").read_text()
    )
    assert sum(track.wildcard for track in proposal.tracks) == 6
    assert not (services.data_dir / "days/2026-10-27.json").exists()


async def test_final_validation_rejects_changed_track_metadata(generation_services):
    services = generation_services

    async def mutate_after_validation(**kwargs: Any) -> PlaylistAgentResult:
        """Change canonical duration after a successful validation tool call."""
        result = await services.run_agent(**kwargs)
        result.proposal.tracks[0].duration_ms = 1
        return result

    services.agent.run.side_effect = mutate_after_validation
    with pytest.raises(ValueError, match="canonical duration_ms"):
        await generate(services)
    assert not (services.data_dir / "days/2026-10-27.json").exists()


async def test_host_assembles_once_with_trusted_metadata(generation_services, monkeypatch):
    services = generation_services
    assemble_record = Mock(wraps=assemble_daily_record)
    monkeypatch.setattr(daily, "assemble_daily_record", assemble_record)

    output_path = await generate(services)

    assemble_record.assert_called_once()
    record = DailyRecord.model_validate_json(output_path.read_bytes())
    assert record.date == RUN_DATE
    assert record.dj == "resident"
    assert record.mood.model_dump() == services.claude.score_mood.return_value.model_dump()
    assert record.inputs[1].value == "Rain"
    assert record.inputs[1].data == {"temp_f": 52}
    assert record.inputs[1].raw == {"forecast": "rain"}
    assert record.model == daily.MODEL
    assert record.generated_at == NOW
    assert record.publication_status == "pending"
    assert record.spotify_playlist_id is None
    agent_arguments = services.agent.run.call_args.kwargs
    assert "context" not in agent_arguments
    assert "generated_at" not in agent_arguments
    assert "spotify_service" not in agent_arguments
    assert record.prompt == agent_arguments["prompt"]


async def test_default_agent_receives_validator_with_prepared_context(
    generation_services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind agent validation to the same evidence later used by host acceptance."""
    services = generation_services
    agent_factory = Mock(return_value=services.agent)
    monkeypatch.setattr(daily, "PlaylistAgent", agent_factory)

    await daily.generate_day(
        services.data_dir,
        RUN_DATE,
        now=NOW,
        claude_service=services.claude,
        spotify_service=services.spotify,
    )

    agent_factory.assert_called_once()
    spotify_service, validator = agent_factory.call_args.args
    assert spotify_service is services.spotify
    assert isinstance(validator, PlaylistValidator)
    assert validator.context is services.prepared_runs[-1].context


@pytest.mark.parametrize("use_cli_date", [True, False])
def test_cli_date_precedence_and_limits(tmp_path, monkeypatch, capsys, use_cli_date):
    captured: dict[str, Any] = {}

    async def fake_generate(data_dir: Path, run_date: date, **kwargs: Any) -> Path:
        """Capture resolved CLI configuration without invoking providers."""
        captured.update(data_dir=data_dir, run_date=run_date, **kwargs)
        return data_dir / "days" / f"{run_date.isoformat()}.json"

    monkeypatch.setattr(daily, "generate_day", fake_generate)
    monkeypatch.setenv("RUN_DATE", "2026-10-25")
    arguments = [
        "--data-dir",
        str(tmp_path),
        "--model",
        "test-model",
        "--max-turns",
        "12",
        "--budget-usd",
        "2.5",
        "--deadline-seconds",
        "75",
    ]
    if use_cli_date:
        arguments += ["--date", "2026-10-26"]
    assert daily.main(arguments) == 0
    assert captured["run_date"] == date(2026, 10, 26 if use_cli_date else 25)
    assert captured["model"] == "test-model"
    assert captured["limits"] == AgentLimits(12, 2.5, 75)
    assert captured["now"].tzinfo == TZ
    assert "days/2026-10-" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--budget-usd", "0"],
        ["--max-turns", "0"],
        ["--deadline-seconds", "nan"],
    ],
)
def test_cli_invalid_limits_fail_before_generation(monkeypatch, capsys, arguments):
    generate_mock = AsyncMock()
    monkeypatch.setattr(daily, "generate_day", generate_mock)
    assert daily.main(arguments) == 1
    generate_mock.assert_not_awaited()
    assert "Generation failed" in capsys.readouterr().err


@pytest.mark.live
async def test_live_generation_writes_only_a_pending_temporary_day(tmp_path):
    """Opt in with RUN_LIVE=1; calls Claude/readings/search but never creates a playlist."""
    repo_data = Path(__file__).resolve().parents[2] / "data"
    catalog_path = repo_data / "shared_catalog.jsonl"
    if not catalog_path.exists():
        pytest.skip("populate the shared catalog before live qualification")
    data_dir = tmp_path / "live-data"
    data_dir.mkdir()
    shutil.copyfile(catalog_path, data_dir / "shared_catalog.jsonl")
    shutil.copyfile(repo_data / "personas.json", data_dir / "personas.json")
    now = datetime.now(TZ)
    history_dir = data_dir / "days"
    history_dir.mkdir()
    for source_path in (repo_data / "days").glob("*.json"):
        payload = json.loads(source_path.read_text())
        if payload.get("publication_status") == "published":
            playlist_date = date.fromisoformat(source_path.stem)
            if now.date() - timedelta(days=30) <= playlist_date < now.date():
                shutil.copyfile(source_path, history_dir / source_path.name)
    async with httpx.AsyncClient() as http_client:
        # A search-only boundary makes publication impossible even if orchestration changes.
        from the_bot_radio.services.spotify_service import SpotifyService

        spotify_service = SpotifyService(http_client)
        search_only = SimpleNamespace(search_tracks=spotify_service.search_tracks)
        output_path = await daily.generate_day(
            data_dir,
            now.date(),
            now=now,
            spotify_service=search_only,
        )
    record = DailyRecord.model_validate_json(output_path.read_bytes())
    assert record.publication_status == "pending"
    assert record.spotify_playlist_id is None
    assert len(record.tracks) == 10
    assert sum(track.wildcard for track in record.tracks) == 5
    assert output_path.is_relative_to(tmp_path)
    assert list((data_dir / "runs").glob("*/*/diagnostics.json"))

    catalog = load_catalog(data_dir / "shared_catalog.jsonl")
    assert len({normalize_artist(track.artists[0]) for track in record.tracks}) == 10
    for track in record.tracks:
        assert track.wildcard == (track.spotify_id not in catalog)
        assert track.spotify_url == f"https://open.spotify.com/track/{track.spotify_id}"
    artifact_dir = next((data_dir / "runs").glob("*/*/diagnostics.json")).parent
    search_evidence = json.loads((artifact_dir / "search_results.json").read_text())
    assert all(track.spotify_id in search_evidence for track in record.tracks if track.wildcard)
    events = [
        json.loads(line) for line in (artifact_dir / "transcript.jsonl").read_text().splitlines()
    ]
    initialized = next(
        event["message"]["data"]
        for event in events
        if event.get("message", {}).get("subtype") == "init"
    )
    assert set(initialized["tools"]) == {
        "Read",
        "Grep",
        "Glob",
        "StructuredOutput",
        "mcp__playlist__search_tracks",
        "mcp__playlist__validate_playlist",
    }
