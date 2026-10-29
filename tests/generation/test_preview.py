"""Offline preview isolation, validation, and CLI output contracts."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from test_daily import NOW, RUN_DATE, generate
from test_daily import generation_services as generation_services

from the_bot_radio.generation import config, daily
from the_bot_radio.generation.agent import PlaylistAgentResult
from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.runtime import AgentLimits
from the_bot_radio.generation.storage import LocalDailyRecordStore

pytestmark = pytest.mark.anyio


def data_snapshot(data_dir: Path) -> dict[str, bytes | None]:
    """Capture files and directories to detect every persistent preview side effect."""
    return {
        str(path.relative_to(data_dir)): path.read_bytes() if path.is_file() else None
        for path in data_dir.rglob("*")
    }


async def preview(services: SimpleNamespace, **overrides: Any) -> DailyRecord:
    """Run preview with fake provider boundaries and actual preparation and validation."""
    arguments = {
        "data_dir": services.data_dir,
        "resources_dir": services.data_dir,
        "now": NOW,
        "claude_service": services.claude,
        "spotify_service": services.spotify,
        "reccobeats_service": services.reccobeats,
        "playlist_agent": services.agent,
    }
    arguments.update(overrides)
    return await daily.preview_day(RUN_DATE, **arguments)


@pytest.fixture
def temporary_run_directories(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Observe workspace roots without prescribing preview's temporary-directory layout."""
    directories: list[Path] = []
    prepare_run = daily.prepare_run

    async def capture_directory(*args: Any, **kwargs: Any) -> Any:
        """Capture the workspace parent even if preparation raises."""
        directories.append(Path(args[0]))
        return await prepare_run(*args, **kwargs)

    monkeypatch.setattr(daily, "prepare_run", capture_directory)
    return directories


@pytest.mark.parametrize("verbose", [False, True])
async def test_preview_returns_pending_record_without_persistent_writes(
    generation_services: SimpleNamespace,
    temporary_run_directories: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verbose: bool,
) -> None:
    """Preview validates the real record while keeping publication and storage read-only."""
    services = generation_services
    existing_day = services.data_dir / "days" / f"{RUN_DATE}.json"
    existing_day.parent.mkdir()
    existing_day.write_bytes(b"unchanged existing daily bytes")
    before = data_snapshot(services.data_dir)
    publish_playlist = AsyncMock(side_effect=AssertionError("preview must not publish"))
    daily_write = AsyncMock(side_effect=AssertionError("preview must not commit"))
    create_s3_client = Mock(side_effect=AssertionError("preview must not access S3"))
    monkeypatch.setattr(daily, "publish_playlist", publish_playlist)
    monkeypatch.setattr(LocalDailyRecordStore, "write", daily_write)
    monkeypatch.setattr(config, "create_s3_client", create_s3_client)
    monkeypatch.setenv("BOT_RADIO_S3_BUCKET", "ambient-production-bucket")
    monkeypatch.setenv("BOT_RADIO_S3_PREFIX", "production")
    monkeypatch.setenv("BOT_RADIO_RUN_DIR", str(services.data_dir / "forbidden-staging"))

    record = await preview(services, verbose=verbose)

    assert record.publication_status == "pending"
    assert record.spotify_playlist_id is None
    assert record.spotify_playlist_url is None
    assert record.spotify_snapshot_id is None
    assert record.published_at is None
    assert record.date == RUN_DATE
    assert record.generated_at == NOW
    assert len(record.tracks) == 10
    assert sum(track.wildcard for track in record.tracks) == 5
    assert data_snapshot(services.data_dir) == before
    assert temporary_run_directories
    assert all(not directory.exists() for directory in temporary_run_directories)
    publish_playlist.assert_not_awaited()
    daily_write.assert_not_awaited()
    create_s3_client.assert_not_called()
    services.spotify.create_playlist.assert_not_awaited()
    services.spotify.add_items_to_playlist.assert_not_awaited()
    services.spotify.get_playlist.assert_not_awaited()
    console = capsys.readouterr()
    assert console.out == ""
    if verbose:
        assert any(json.loads(line)["event"] == "prepared" for line in console.err.splitlines())
    else:
        assert console.err == ""


@pytest.mark.parametrize("failure_kind", ["provider", "validation", "cancellation"])
async def test_preview_cleans_temporary_files_after_failure(
    generation_services: SimpleNamespace,
    temporary_run_directories: list[Path],
    failure_kind: str,
) -> None:
    """Preview keeps host validation mandatory and cleans workspace files for every outcome."""
    services = generation_services
    before = data_snapshot(services.data_dir)
    if failure_kind == "provider":
        services.claude.score_mood.side_effect = RuntimeError("mood provider failed")
        exception_type = RuntimeError
    elif failure_kind == "cancellation":
        services.agent.run.side_effect = asyncio.CancelledError
        exception_type = asyncio.CancelledError
    else:

        async def return_invalid_proposal(**kwargs: Any) -> PlaylistAgentResult:
            """Return a schema-valid proposal that violates canonical metadata rules."""
            result = await services.run_agent(**kwargs)
            result.proposal.tracks[0].duration_ms = 1
            return result

        services.agent.run.side_effect = return_invalid_proposal
        exception_type = ValueError

    with pytest.raises(exception_type) as raised:
        await preview(services)

    assert data_snapshot(services.data_dir) == before
    assert temporary_run_directories
    assert all(not directory.exists() for directory in temporary_run_directories)
    assert not any("Run recovery:" in note for note in getattr(raised.value, "__notes__", []))
    services.spotify.create_playlist.assert_not_awaited()


async def test_preview_observes_local_published_history_exclusions(
    generation_services: SimpleNamespace,
    temporary_run_directories: list[Path],
) -> None:
    """A locally published set excludes catalog tracks even when S3 is configured elsewhere."""
    services = generation_services
    record = DailyRecord.model_validate_json((await generate(services)).read_bytes())
    previous_date = RUN_DATE - timedelta(days=1)
    history_record = record.model_copy(update={"date": previous_date})
    (services.data_dir / "days" / f"{previous_date}.json").write_text(
        history_record.model_dump_json()
    )
    before = data_snapshot(services.data_dir)
    previous_agent_calls = services.agent.run.await_count
    temporary_run_directories.clear()

    with pytest.raises(ValueError, match="five eligible tracks"):
        await preview(services)

    assert services.agent.run.await_count == previous_agent_calls
    assert data_snapshot(services.data_dir) == before
    assert temporary_run_directories
    assert all(not directory.exists() for directory in temporary_run_directories)


async def test_preview_uses_local_history_for_dj_selection(
    generation_services: SimpleNamespace,
) -> None:
    """Yesterday's local resident is excluded without excluding today's catalog choices."""
    services = generation_services
    record = DailyRecord.model_validate_json((await generate(services)).read_bytes())
    previous_date = RUN_DATE - timedelta(days=1)
    history_payload = record.model_dump(mode="json")
    history_payload["date"] = previous_date.isoformat()
    for position, track in enumerate(history_payload["tracks"], start=100):
        track["spotify_id"] = f"{position:022d}"
        track["spotify_url"] = f"https://open.spotify.com/track/{position:022d}"
    history_record = DailyRecord.model_validate(history_payload)
    (services.data_dir / "days" / f"{previous_date}.json").write_text(
        history_record.model_dump_json()
    )
    personas_path = services.data_dir / "personas.json"
    personas = json.loads(personas_path.read_text())
    personas.append({**personas[0], "id": "another-resident", "name": "Another resident"})
    personas_path.write_text(json.dumps(personas))
    before = data_snapshot(services.data_dir)

    preview_record = await preview(services)

    assert preview_record.dj == "another-resident"
    assert services.prepared_runs[-1].context.recent_track_ids == frozenset(
        track.spotify_id for track in history_record.tracks
    )
    assert data_snapshot(services.data_dir) == before


@pytest.mark.parametrize("verbose", [False, True])
def test_dry_run_cli_prints_record_to_stderr_and_ignores_s3_settings(
    generation_services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verbose: bool,
) -> None:
    """CLI routes directly to preview before resolving storage and preserves generation options."""
    services = generation_services
    real_preview = daily.preview_day

    async def fake_preview(run_date: Any, **kwargs: Any) -> DailyRecord:
        """Use actual preview with the fixture's provider dependencies."""
        assert run_date == RUN_DATE
        assert kwargs["data_dir"] == services.data_dir
        assert kwargs["resources_dir"] == services.data_dir
        assert kwargs["model"] == "preview-model"
        assert kwargs["limits"] == AgentLimits(12, 2.5, 75)
        assert kwargs["verbose"] is verbose
        return await real_preview(
            run_date,
            **kwargs,
            claude_service=services.claude,
            spotify_service=services.spotify,
            playlist_agent=services.agent,
            reccobeats_service=services.reccobeats,
        )

    monkeypatch.setattr(daily, "preview_day", fake_preview)
    monkeypatch.setattr(
        daily, "generate_day", AsyncMock(side_effect=AssertionError("publish path"))
    )
    monkeypatch.setattr(
        daily.GenerationStorageConfig,
        "from_environment",
        Mock(side_effect=AssertionError("must not resolve S3 settings")),
    )
    monkeypatch.setenv("BOT_RADIO_S3_PREFIX", "prefix-without-a-bucket")
    monkeypatch.delenv("BOT_RADIO_S3_BUCKET", raising=False)
    monkeypatch.setenv("BOT_RADIO_RUN_DIR", str(services.data_dir / "forbidden-staging"))
    monkeypatch.setenv("RUN_DATE", RUN_DATE.isoformat())
    arguments = [
        "--dry-run",
        "--data-dir",
        str(services.data_dir),
        "--model",
        "preview-model",
        "--max-turns",
        "12",
        "--budget-usd",
        "2.5",
        "--deadline-seconds",
        "75",
    ]
    if verbose:
        arguments.append("--verbose")

    assert daily.main(arguments) == 0

    console = capsys.readouterr()
    assert console.out == ""
    json_start = console.err.index("{\n")
    record = DailyRecord.model_validate_json(console.err[json_start:])
    assert record.publication_status == "pending"
    assert record.model == "preview-model"
    assert record.date == RUN_DATE
    if verbose:
        assert console.err[:json_start]
    else:
        assert json_start == 0
    assert not (services.data_dir / "days").exists()
    assert not (services.data_dir / "runs").exists()


def test_dry_run_cli_failure_does_not_advertise_retained_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failed preview prints a sanitized error and no output or recovery path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "preview-secret-key")
    monkeypatch.setattr(
        daily, "preview_day", AsyncMock(side_effect=RuntimeError("preview-secret-key failed"))
    )

    assert daily.main(["--dry-run"]) == 1

    console = capsys.readouterr()
    assert console.out == ""
    assert "preview-secret-key" not in console.err
    assert "[REDACTED]" in console.err
    assert "see run artifacts" not in console.err
    assert "Run recovery" not in console.err
