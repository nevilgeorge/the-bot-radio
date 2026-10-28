"""Offline daily-command acceptance, persistence, and opt-in live qualification."""

import asyncio
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
from the_bot_radio.generation.runtime import AgentLimits, RunRecorder, recover_run_artifacts
from the_bot_radio.generation.storage import (
    LocalDailyRecordStore,
    LocalRunArtifactStore,
    RecordExists,
)
from the_bot_radio.generation.tools import PlaylistTools
from the_bot_radio.generation.validation import PlaylistValidator, assemble_daily_record
from the_bot_radio.services.inputs import runner
from the_bot_radio.services.inputs.base import TZ, Reading
from the_bot_radio.services.spotify_service import SpotifyTrack
from the_bot_radio.web.models import Mood

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 6, tzinfo=TZ)


@pytest.fixture(autouse=True)
def local_cli_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI tests independent of ambient AWS storage configuration."""
    for variable_name in ("BOT_RADIO_S3_BUCKET", "BOT_RADIO_S3_PREFIX", "BOT_RADIO_RUN_DIR"):
        monkeypatch.delenv(variable_name, raising=False)


@pytest.fixture
def generation_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Provide deterministic catalog/persona evidence and real tool adapters with fake APIs."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(daily, "RESOURCE_DIR", data_dir)
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


def local_dependencies(data_dir: Path) -> dict[str, Any]:
    """Select explicit local generation dependencies for fixture evidence."""
    return {
        "daily_store": LocalDailyRecordStore(data_dir),
        "artifact_store": LocalRunArtifactStore(data_dir),
        "local_work_root": data_dir / ".run-staging",
        "resources_dir": data_dir,
    }


async def generate(services: SimpleNamespace) -> Path:
    """Invoke the real local job with injected provider boundaries."""
    return Path(
        await daily.generate_day(
            RUN_DATE,
            **local_dependencies(services.data_dir),
            now=NOW,
            claude_service=services.claude,
            spotify_service=services.spotify,
            playlist_agent=services.agent,
        )
    )


@pytest.mark.parametrize(
    "dependency_name", ["daily_store", "artifact_store", "local_work_root", "resources_dir"]
)
@pytest.mark.parametrize("explicit_none", [False, True], ids=["omitted", "none"])
async def test_dependencies_are_required_before_any_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency_name: str,
    explicit_none: bool,
) -> None:
    """Reject absent dependencies before storage, filesystem, or provider activity."""
    daily_store = Mock()
    artifact_store = Mock()
    dependencies: dict[str, Any] = {
        "daily_store": daily_store,
        "artifact_store": artifact_store,
        "local_work_root": tmp_path / "staging",
        "resources_dir": tmp_path / "resources",
    }
    if explicit_none:
        dependencies[dependency_name] = None
    else:
        del dependencies[dependency_name]
    boundaries = [
        (daily.uuid, "uuid4"),
        (daily, "RunRecorder"),
        (daily, "make_http_client"),
        (daily, "ClaudeService"),
        (daily, "SpotifyService"),
        (daily, "PlaylistAgent"),
        (daily, "prepare_run"),
    ]
    boundary_mocks = []
    for owner, attribute in boundaries:
        boundary_mock = Mock(side_effect=AssertionError(f"unexpected {attribute} call"))
        monkeypatch.setattr(owner, attribute, boundary_mock)
        boundary_mocks.append(boundary_mock)

    with pytest.raises(ValueError if explicit_none else TypeError, match=dependency_name):
        await daily.generate_day(RUN_DATE, **dependencies)

    assert daily_store.mock_calls == []
    assert artifact_store.mock_calls == []
    for boundary_mock in boundary_mocks:
        boundary_mock.assert_not_called()
    assert list(tmp_path.iterdir()) == []


async def test_local_generation_uses_separate_dependency_directories(
    generation_services: SimpleNamespace, tmp_path: Path
) -> None:
    """Keep resources, canonical output, archived evidence, and staging independent."""
    services = generation_services
    daily_root = tmp_path / "daily-output"
    artifact_root = tmp_path / "artifact-output"
    local_work_root = tmp_path / "staging"
    resources_before = {path.name: path.read_bytes() for path in services.data_dir.iterdir()}

    output_path = Path(
        await daily.generate_day(
            RUN_DATE,
            daily_store=LocalDailyRecordStore(daily_root),
            artifact_store=LocalRunArtifactStore(artifact_root),
            local_work_root=local_work_root,
            resources_dir=services.data_dir,
            now=NOW,
            claude_service=services.claude,
            spotify_service=services.spotify,
            playlist_agent=services.agent,
        )
    )

    assert output_path == daily_root / "days" / f"{RUN_DATE}.json"
    assert DailyRecord.model_validate_json(output_path.read_bytes()).date == RUN_DATE
    [artifact_directory] = (artifact_root / "runs" / str(RUN_DATE)).iterdir()
    staging_directory = local_work_root / str(RUN_DATE) / artifact_directory.name
    assert (staging_directory / "workspace" / "history").is_dir()
    assert (artifact_directory / "diagnostics.json").is_file()
    assert (artifact_directory / "workspace" / "catalog.jsonl").read_bytes() == (
        services.data_dir / "shared_catalog.jsonl"
    ).read_bytes()
    assert {
        path.name: path.read_bytes() for path in services.data_dir.iterdir()
    } == resources_before
    assert not (daily_root / "runs").exists()
    assert not (artifact_root / "days").exists()
    assert not (local_work_root / "days").exists()


async def test_offline_end_to_end_and_idempotent_rerun(generation_services, monkeypatch):
    services = generation_services
    output_path = await generate(services)
    record = DailyRecord.model_validate_json(Path(output_path).read_bytes())
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
        "schemas",
    }
    staging_workspace = (
        services.data_dir / ".run-staging" / str(RUN_DATE) / artifacts[0].parent.name / "workspace"
    )
    assert (staging_workspace / "history").is_dir()
    assert not list(workspace.rglob("diagnostics.json"))
    original_bytes = output_path.read_bytes()

    def fail_if_constructed(*args: Any, **kwargs: Any) -> None:
        """Ensure successful reruns do not need credentials or provider clients."""
        raise AssertionError("provider construction on a no-op rerun")

    monkeypatch.setattr(daily, "ClaudeService", fail_if_constructed)
    monkeypatch.setattr(daily, "SpotifyService", fail_if_constructed)
    monkeypatch.setattr(daily, "PlaylistAgent", fail_if_constructed)
    assert await daily.generate_day(
        RUN_DATE, **local_dependencies(services.data_dir), now=NOW
    ) == str(output_path)
    rerun_artifacts = list((services.data_dir / "runs/2026-10-27").glob("*/diagnostics.json"))
    assert len(rerun_artifacts) == 2
    outcomes = [json.loads(path.read_text()) for path in rerun_artifacts]
    assert {outcome["status"] for outcome in outcomes} == {"accepted", "skipped"}
    assert (
        next(outcome for outcome in outcomes if outcome["status"] == "skipped")["reason"]
        == "existing_daily_record"
    )
    assert len({path.parent.name for path in rerun_artifacts}) == 2
    assert len(list((services.data_dir / ".run-staging/2026-10-27").iterdir())) == 2
    assert output_path.read_bytes() == original_bytes
    assert services.agent.run.await_count == 1


async def test_verbose_generation_streams_transcript_to_stderr(generation_services, capsys):
    services = generation_services
    await daily.generate_day(
        RUN_DATE,
        **local_dependencies(services.data_dir),
        now=NOW,
        claude_service=services.claude,
        spotify_service=services.spotify,
        playlist_agent=services.agent,
        verbose=True,
    )
    console = capsys.readouterr()
    [transcript_path] = (services.data_dir / "runs/2026-10-27").glob("*/transcript.jsonl")
    assert console.out == ""
    assert console.err == transcript_path.read_text()
    assert json.loads(console.err.splitlines()[-1])["status"] == "accepted"


async def test_existing_malformed_day_is_preserved(generation_services):
    services = generation_services
    output_path = services.data_dir / "days/2026-10-27.json"
    output_path.parent.mkdir()
    output_path.write_text("malformed fixture")
    with pytest.raises(ValueError):
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
        RUN_DATE,
        **local_dependencies(services.data_dir),
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
@pytest.mark.parametrize("verbose", [True, False])
def test_cli_date_precedence_and_limits(tmp_path, monkeypatch, capsys, use_cli_date, verbose):
    captured: dict[str, Any] = {}

    async def fake_generate(run_date: date, **kwargs: Any) -> str:
        """Capture resolved CLI configuration without invoking providers."""
        captured.update(run_date=run_date, **kwargs)
        return kwargs["daily_store"].storage_location(run_date)

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
    if verbose:
        arguments += ["--verbose"]
    assert daily.main(arguments) == 0
    assert captured["run_date"] == date(2026, 10, 26 if use_cli_date else 25)
    assert captured["model"] == "test-model"
    assert captured["limits"] == AgentLimits(12, 2.5, 75)
    assert captured["now"].tzinfo == TZ
    assert captured["verbose"] is verbose
    assert captured["daily_store"].data_dir == tmp_path
    assert captured["artifact_store"].data_dir == tmp_path
    assert captured["local_work_root"] == tmp_path / ".run-staging"
    assert captured["resources_dir"] == daily.RESOURCE_DIR
    assert "data_dir" not in captured
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
    catalog_path = daily.RESOURCE_DIR / "shared_catalog.jsonl"
    if not catalog_path.exists():
        pytest.skip("populate the shared catalog before live qualification")
    data_dir = tmp_path / "live-data"
    data_dir.mkdir()
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
            now.date(),
            **{**local_dependencies(data_dir), "resources_dir": daily.RESOURCE_DIR},
            now=now,
            spotify_service=search_only,
        )
    record = DailyRecord.model_validate_json(Path(output_path).read_bytes())
    assert record.publication_status == "pending"
    assert record.spotify_playlist_id is None
    assert len(record.tracks) == 10
    assert sum(track.wildcard for track in record.tracks) == 5
    assert Path(output_path).is_relative_to(tmp_path)
    assert list((data_dir / "runs").glob("*/*/diagnostics.json"))

    catalog = load_catalog(catalog_path)
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


class MemoryDailyStore:
    """Keep canonical bytes and scripted write outcomes entirely outside the filesystem."""

    def __init__(self, *, create_mode: str = "success") -> None:
        self.records: dict[date, bytes] = {}
        self.read_dates: list[date] = []
        self.create_calls = 0
        self.create_mode = create_mode
        self.creation_error = OSError("canonical write interrupted")
        self.read_error: Exception | None = None

    def storage_location(self, run_date: date) -> str:
        """Return a URI without consulting stored content."""
        return f"memory://days/{run_date.isoformat()}.json"

    async def read(self, run_date: date) -> bytes | None:
        """Track reads and propagate configured storage failures."""
        self.read_dates.append(run_date)
        if self.read_error is not None:
            raise self.read_error
        return self.records.get(run_date)

    async def create(self, run_date: date, content: bytes) -> str:
        """Model conflicts, failed writes, and lost acknowledgements without retries."""
        self.create_calls += 1
        if self.create_mode in {"success", "ambiguous_commit", "conflict_valid", "cancel_commit"}:
            self.records[run_date] = content
        elif self.create_mode == "conflict_invalid":
            self.records[run_date] = b"malformed"
        if self.create_mode.startswith("conflict"):
            raise RecordExists(self.storage_location(run_date))
        if self.create_mode.startswith("ambiguous"):
            raise self.creation_error
        if self.create_mode.startswith("cancel"):
            raise asyncio.CancelledError()
        return self.storage_location(run_date)


class MemoryArtifactStore:
    """Capture replaceable snapshots and fail selected lifecycle boundaries."""

    def __init__(self, fail_at: str | None = None) -> None:
        self.artifacts: dict[tuple[str, str], bytes] = {}
        self.fail_at = fail_at
        self.failed_writes = 0

    async def write(self, run_date: date, run_id: str, relative_name: str, content: bytes) -> str:
        """Persist snapshots except at the configured stage."""
        should_fail = (
            self.fail_at == "initial"
            or self.fail_at == "precommit"
            and relative_name == "proposal.json"
            or self.fail_at == "postcommit"
            and relative_name == "diagnostics.json"
            and json.loads(content).get("status") == "accepted"
        )
        if should_fail:
            self.failed_writes += 1
            raise OSError("artifact unavailable")
        self.artifacts[run_id, relative_name] = content
        return f"memory://runs/{run_date}/{run_id}/{relative_name}"


def staged_diagnostics(services: SimpleNamespace) -> tuple[Path, dict[str, Any]]:
    """Read the sole retained run's local terminal outcome."""
    [diagnostics_path] = (services.data_dir / ".run-staging" / str(RUN_DATE)).glob(
        "*/diagnostics.json"
    )
    return diagnostics_path.parent, json.loads(diagnostics_path.read_bytes())


async def generate_in_memory(
    services: SimpleNamespace,
    daily_store: MemoryDailyStore,
    artifact_store: MemoryArtifactStore,
    **kwargs: Any,
) -> str:
    """Exercise orchestration while canonical records and artifacts use URI storage."""
    return await daily.generate_day(
        RUN_DATE,
        local_work_root=kwargs.pop("local_work_root", services.data_dir / ".run-staging"),
        resources_dir=services.data_dir,
        now=NOW,
        claude_service=services.claude,
        spotify_service=services.spotify,
        playlist_agent=services.agent,
        daily_store=daily_store,
        artifact_store=artifact_store,
        **kwargs,
    )


async def test_injected_uri_stores_and_audited_skip(generation_services):
    services = generation_services
    daily_store = MemoryDailyStore()
    artifact_store = MemoryArtifactStore()
    location = await generate_in_memory(services, daily_store, artifact_store)
    assert location == daily_store.storage_location(RUN_DATE)
    assert isinstance(location, str)
    assert DailyRecord.model_validate_json(daily_store.records[RUN_DATE]).date == RUN_DATE
    assert not (services.data_dir / "days").exists()
    assert len(daily_store.read_dates) == 31
    assert await generate_in_memory(services, daily_store, artifact_store) == location
    assert daily_store.create_calls == 1
    services.agent.run.assert_awaited_once()
    outcomes = [
        json.loads(content)
        for (_, name), content in artifact_store.artifacts.items()
        if name == "diagnostics.json"
    ]
    assert {outcome["status"] for outcome in outcomes} == {"accepted", "skipped"}
    assert len({outcome["run_id"] for outcome in outcomes}) == 2


@pytest.mark.parametrize("create_mode", ["conflict_valid", "conflict_invalid", "conflict_missing"])
async def test_create_conflict_validates_reread(generation_services, create_mode):
    daily_store = MemoryDailyStore(create_mode=create_mode)
    artifact_store = MemoryArtifactStore()
    if create_mode == "conflict_valid":
        assert await generate_in_memory(generation_services, daily_store, artifact_store) == (
            daily_store.storage_location(RUN_DATE)
        )
        _, outcome = staged_diagnostics(generation_services)
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "daily_record_created_elsewhere"
    else:
        with pytest.raises((ValueError, RecordExists)):
            await generate_in_memory(generation_services, daily_store, artifact_store)
        assert staged_diagnostics(generation_services)[1]["status"] == "failed"
    assert daily_store.create_calls == 1
    assert daily_store.read_dates.count(RUN_DATE) == 2


@pytest.mark.parametrize("committed", [True, False])
async def test_ambiguous_creation_reconciles_once(generation_services, committed):
    daily_store = MemoryDailyStore(
        create_mode="ambiguous_commit" if committed else "ambiguous_lost"
    )
    if committed:
        assert await generate_in_memory(
            generation_services, daily_store, MemoryArtifactStore()
        ) == (daily_store.storage_location(RUN_DATE))
        assert staged_diagnostics(generation_services)[1]["status"] == "accepted"
    else:
        with pytest.raises(OSError) as caught:
            await generate_in_memory(generation_services, daily_store, MemoryArtifactStore())
        assert caught.value is daily_store.creation_error
        assert any("reconcil" in note.lower() for note in caught.value.__notes__)
        assert staged_diagnostics(generation_services)[1]["status"] == "failed"
    assert daily_store.create_calls == 1
    assert daily_store.read_dates.count(RUN_DATE) == 2


async def test_initial_read_failure_is_audited_without_providers(generation_services):
    daily_store = MemoryDailyStore()
    daily_store.read_error = OSError("read unavailable")
    with pytest.raises(OSError, match="read unavailable"):
        await generate_in_memory(generation_services, daily_store, MemoryArtifactStore())
    generation_services.claude.score_mood.assert_not_awaited()
    generation_services.agent.run.assert_not_awaited()
    assert daily_store.create_calls == 0
    assert staged_diagnostics(generation_services)[1]["status"] == "failed"


@pytest.mark.parametrize("fail_at", ["initial", "precommit", "postcommit"])
async def test_artifact_failure_boundaries_and_original_run_recovery(
    generation_services, monkeypatch, fail_at
):
    monkeypatch.setattr(RunRecorder, "retry_delay", 0)
    daily_store = MemoryDailyStore()
    artifact_store = MemoryArtifactStore(fail_at)
    with pytest.raises(Exception) as caught:
        await generate_in_memory(generation_services, daily_store, artifact_store)
    staging_dir, outcome = staged_diagnostics(generation_services)
    assert outcome["status"] == ("accepted" if fail_at == "postcommit" else "failed")
    assert (RUN_DATE in daily_store.records) is (fail_at == "postcommit")
    assert daily_store.create_calls == int(fail_at == "postcommit")
    assert any(str(staging_dir) in note for note in caught.value.__notes__)
    if fail_at == "initial":
        generation_services.claude.score_mood.assert_not_awaited()
        generation_services.agent.run.assert_not_awaited()
    if fail_at == "postcommit":
        assert any(
            daily_store.storage_location(RUN_DATE) in note for note in caught.value.__notes__
        )
    original_run_id = outcome["run_id"]
    artifact_store.fail_at = None
    await recover_run_artifacts(artifact_store, RUN_DATE, original_run_id, staging_dir)
    assert (
        json.loads(artifact_store.artifacts[original_run_id, "diagnostics.json"])["status"]
        == (outcome["status"])
    )
    assert {run_id for run_id, _ in artifact_store.artifacts} == {original_run_id}
    assert staging_dir.exists()


@pytest.mark.parametrize("committed", [True, False])
async def test_cancellation_during_creation_reconciles_and_propagates(
    generation_services, committed
):
    daily_store = MemoryDailyStore(create_mode="cancel_commit" if committed else "cancel_lost")
    with pytest.raises(asyncio.CancelledError):
        await generate_in_memory(generation_services, daily_store, MemoryArtifactStore())
    _, outcome = staged_diagnostics(generation_services)
    assert outcome["status"] == ("accepted" if committed else "failed")
    assert daily_store.read_dates.count(RUN_DATE) == 2
    assert daily_store.create_calls == 1


async def test_setup_failure_archives_partial_workspace(generation_services):
    services = generation_services
    services.claude.score_mood.side_effect = RuntimeError("mood setup failed")
    artifact_store = MemoryArtifactStore()
    with pytest.raises(RuntimeError, match="mood setup failed"):
        await generate_in_memory(services, MemoryDailyStore(), artifact_store)
    staging_dir, outcome = staged_diagnostics(services)
    assert outcome["status"] == "failed"
    assert (outcome["run_id"], "workspace/catalog.jsonl") in artifact_store.artifacts
    assert (staging_dir / "workspace/catalog.jsonl").is_file()
    services.agent.run.assert_not_awaited()


async def test_agent_failure_archives_evidence_and_preserves_primary_error(
    generation_services, monkeypatch
):
    services = generation_services
    primary_error = RuntimeError("agent failed")

    async def fail_after_search(**kwargs: Any) -> None:
        """Leave a proposal and canonical evidence before a primary provider failure."""
        await services.run_agent(**kwargs)
        raise primary_error

    services.agent.run.side_effect = fail_after_search
    monkeypatch.setattr(RunRecorder, "retry_delay", 0)
    artifact_store = MemoryArtifactStore("precommit")
    with pytest.raises(RuntimeError) as caught:
        await generate_in_memory(services, MemoryDailyStore(), artifact_store)
    assert caught.value is primary_error
    staging_dir, outcome = staged_diagnostics(services)
    assert outcome["status"] == "failed"
    assert json.loads((staging_dir / "search_results.json").read_bytes())
    assert (staging_dir / "proposal.json").is_file()
    assert any(
        "final" in note.lower() or "artifact" in note.lower() for note in caught.value.__notes__
    )


async def test_naive_timestamp_fails_before_staging(generation_services):
    with pytest.raises(ValueError, match="timezone-aware"):
        await daily.generate_day(
            RUN_DATE,
            **local_dependencies(generation_services.data_dir),
            now=datetime(2026, 10, 27),
        )
    assert not (generation_services.data_dir / ".run-staging").exists()


@pytest.mark.parametrize("reconciliation_result", ["different", "read_error", "timeout"])
async def test_ambiguous_creation_preserves_original_error_when_reconciliation_fails(
    generation_services, monkeypatch, reconciliation_result
):
    daily_store = MemoryDailyStore(create_mode="ambiguous_lost")
    original_read = daily_store.read

    async def reconcile_read(run_date: date) -> bytes | None:
        """Change only the reconciliation read, leaving preparation reads intact."""
        if daily_store.create_calls and run_date == RUN_DATE:
            if reconciliation_result == "different":
                return b"different bytes"
            if reconciliation_result == "read_error":
                raise ConnectionError("read payload must stay out of diagnostics")
            await asyncio.sleep(10)
        return await original_read(run_date)

    monkeypatch.setattr(daily_store, "read", reconcile_read)
    with pytest.raises(OSError) as caught:
        await generate_in_memory(generation_services, daily_store, MemoryArtifactStore())
    assert caught.value is daily_store.creation_error
    assert daily_store.create_calls == 1
    outcome = staged_diagnostics(generation_services)[1]
    assert outcome["status"] == "failed"
    expected = {
        "different": "different_content",
        "read_error": "read_ConnectionError",
        "timeout": "read_TimeoutError",
    }
    assert outcome["reconciliation"] == expected[reconciliation_result]
    assert "read payload" not in json.dumps(outcome)


async def test_existing_record_with_wrong_date_fails_without_generation(generation_services):
    services = generation_services
    source_path = await generate(services)
    payload = json.loads(source_path.read_bytes())
    payload["date"] = "2026-10-26"
    daily_store = MemoryDailyStore()
    daily_store.records[RUN_DATE] = json.dumps(payload).encode()
    services.claude.score_mood.reset_mock()
    services.agent.run.reset_mock()
    with pytest.raises(ValueError, match="existing daily file is invalid"):
        await generate_in_memory(services, daily_store, MemoryArtifactStore())
    services.claude.score_mood.assert_not_awaited()
    services.agent.run.assert_not_awaited()
    assert daily_store.create_calls == 0


async def test_custom_local_work_root_is_retained(generation_services, tmp_path):
    local_work_root = tmp_path / "retained-work"
    artifacts = MemoryArtifactStore()
    await generate_in_memory(
        generation_services, MemoryDailyStore(), artifacts, local_work_root=local_work_root
    )
    [staging_dir] = (local_work_root / str(RUN_DATE)).iterdir()
    assert (staging_dir / "proposal.json").is_file()
    assert json.loads((staging_dir / "diagnostics.json").read_bytes())["status"] == "accepted"
    assert not (generation_services.data_dir / ".run-staging").exists()


async def test_cancellation_before_commit_archives_agent_evidence(generation_services):
    services = generation_services

    async def cancel_after_proposal(**kwargs: Any) -> None:
        """Retain successfully gathered evidence when generation gets cancelled."""
        await services.run_agent(**kwargs)
        raise asyncio.CancelledError()

    services.agent.run.side_effect = cancel_after_proposal
    daily_store = MemoryDailyStore()
    artifact_store = MemoryArtifactStore()
    with pytest.raises(asyncio.CancelledError):
        await generate_in_memory(services, daily_store, artifact_store)
    staging_dir, outcome = staged_diagnostics(services)
    assert outcome["status"] == "failed"
    assert outcome["error_type"] == "CancelledError"
    assert daily_store.create_calls == 0
    assert (outcome["run_id"], "proposal.json") in artifact_store.artifacts
    assert (staging_dir / "search_results.json").is_file()


def test_cli_failure_reports_controlled_recovery_context(monkeypatch, capsys):
    failure = RuntimeError("provider payload secret")
    failure.add_note(
        "Run recovery: run_id=original; staging_directory=/retained; committed_location=memory://day"
    )
    failure.add_note("arbitrary provider secret")
    monkeypatch.setattr(daily, "generate_day", AsyncMock(side_effect=failure))
    assert daily.main(["--date", str(RUN_DATE)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "committed_location=memory://day" in output.err
    assert "staging_directory=/retained" in output.err
    assert "secret" not in output.err


async def test_skip_artifact_failure_retains_local_failed_outcome(generation_services, monkeypatch):
    """An uncommitted invocation remains failed when its skip audit cannot upload."""
    daily_store = MemoryDailyStore()
    await generate_in_memory(generation_services, daily_store, MemoryArtifactStore())

    class FailedSkipStore(MemoryArtifactStore):
        """Reject the terminal skip snapshot while allowing startup evidence."""

        async def write(
            self, run_date: date, run_id: str, relative_name: str, content: bytes
        ) -> str:
            """Fail the skip audit after the existing day was validated."""
            if relative_name == "diagnostics.json" and json.loads(content)["status"] == "skipped":
                raise OSError("skip audit unavailable")
            return await super().write(run_date, run_id, relative_name, content)

    monkeypatch.setattr(RunRecorder, "retry_delay", 0)
    with pytest.raises(ExceptionGroup):
        await generate_in_memory(generation_services, daily_store, FailedSkipStore())
    diagnostics = [
        json.loads(path.read_bytes())
        for path in (generation_services.data_dir / ".run-staging" / str(RUN_DATE)).glob(
            "*/diagnostics.json"
        )
    ]
    assert {outcome["status"] for outcome in diagnostics} == {"accepted", "failed"}
    assert daily_store.create_calls == 1
    generation_services.agent.run.assert_awaited_once()
