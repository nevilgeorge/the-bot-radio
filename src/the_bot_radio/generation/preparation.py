"""Prepare a fixed evidence workspace and trusted references for one daily agent run."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from pydantic import TypeAdapter

from the_bot_radio.catalog import load_catalog
from the_bot_radio.generation.models import (
    DailyRecord,
    PlaylistMood,
    PlaylistProposal,
    ValidationContext,
    normalize_artist,
)
from the_bot_radio.generation.runtime import update_diagnostics
from the_bot_radio.generation.storage import DailyRecordStore
from the_bot_radio.services.inputs import SERVICES, runner
from the_bot_radio.services.inputs.base import TZ, Reading, RunContext
from the_bot_radio.services.inputs.dj_roll import DjRollService
from the_bot_radio.web.models import Persona

if TYPE_CHECKING:
    from the_bot_radio.services.claude_service import ClaudeService


@dataclass(frozen=True)
class PreparedRun:
    """Agent-visible evidence plus host-owned context and selected persona."""

    workspace: Path
    artifact_dir: Path
    persona: Persona
    context: ValidationContext
    prompt: str
    generated_at: datetime


class DiagnosticsSink(Protocol):
    """Accept synchronous diagnostic updates from preparation."""

    def __call__(self, **updates: Any) -> None:
        """Merge diagnostic keyword updates into the current run."""
        ...


@dataclass(frozen=True)
class HistorySnapshot:
    """A validated published record together with its original stored bytes."""

    record: DailyRecord
    content: bytes


def _write_json(path: Path, payload: object) -> None:
    """Write a readable JSON artifact inside a newly prepared run directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def _load_history(
    daily_store: DailyRecordStore, run_date: date
) -> tuple[list[HistorySnapshot], dict[str, object]]:
    """Read the preceding 30 dates with bounded concurrency and strict validation."""
    read_semaphore = asyncio.Semaphore(5)

    async def read_day(playlist_date: date) -> tuple[date, HistorySnapshot | None, bool]:
        """Fetch and parse one day once, retaining its unmodified payload."""
        async with read_semaphore:
            content = await daily_store.read(playlist_date)
            if content is None:
                return playlist_date, None, False
            payload = json.loads(content)
            filename = f"{playlist_date.isoformat()}.json"
            if not isinstance(payload, dict):
                raise ValueError(f"invalid history record: {filename}")
            if payload.get("publication_status") != "published":
                return playlist_date, None, True
            record = DailyRecord.model_validate(payload)
            if record.date != playlist_date:
                raise ValueError(f"history date does not match filename: {filename}")
            return playlist_date, HistorySnapshot(record, content), False

    read_tasks = [
        asyncio.create_task(read_day(run_date - timedelta(days=days_before)))
        for days_before in range(30, 0, -1)
    ]
    try:
        results = await asyncio.gather(*read_tasks)
    except BaseException:
        for read_task in read_tasks:
            read_task.cancel()
        await asyncio.gather(*read_tasks, return_exceptions=True)
        raise

    snapshots: list[HistorySnapshot] = []
    missing_dates: list[str] = []
    excluded_dates: list[str] = []
    for playlist_date, snapshot, excluded in results:
        if excluded:
            excluded_dates.append(playlist_date.isoformat())
        elif snapshot is None:
            missing_dates.append(playlist_date.isoformat())
        else:
            snapshots.append(snapshot)
    return snapshots, {
        "window_start": (run_date - timedelta(days=30)).isoformat(),
        "window_end_exclusive": run_date.isoformat(),
        "included_dates": [snapshot.record.date.isoformat() for snapshot in snapshots],
        "missing_dates": missing_dates,
        "excluded_dates": excluded_dates,
        "incomplete": bool(missing_dates or excluded_dates),
    }


def _select_dj(run_context: RunContext, history: list[DailyRecord]) -> tuple[Persona, Reading]:
    """Load residents, roll against published history, and validate the selected persona."""
    personas = TypeAdapter(list[Persona]).validate_json(
        (run_context.resources_dir / "personas.json").read_text(encoding="utf-8")
    )
    if len({persona.id for persona in personas}) != len(personas):
        raise ValueError("persona IDs must be unique")
    yesterday = run_context.run_date - timedelta(days=1)
    previous_dj_id = next((record.dj for record in history if record.date == yesterday), None)
    dj_reading = DjRollService().roll(run_context, personas, previous_dj_id)
    persona = next(persona for persona in personas if persona.id == dj_reading.data["persona_id"])
    for field_name in ("name", "bio", "agent_instructions", "speaking_voice"):
        field_value = getattr(persona, field_name).strip()
        if not field_value or field_value.startswith("["):
            raise ValueError(f"selected persona needs a real {field_name}")
    return persona, dj_reading


async def prepare_run(
    artifact_dir: Path,
    run_date: date,
    now: datetime,
    http_client: httpx.AsyncClient,
    claude_service: ClaudeService,
    *,
    resources_dir: Path,
    daily_store: DailyRecordStore,
    diagnostics_sink: DiagnosticsSink | None = None,
) -> PreparedRun:
    """Freeze inputs, roll the DJ from published history, and compute the fixed mood."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("run timestamp must be timezone-aware")
    now = now.astimezone(TZ)
    catalog_source = resources_dir / "shared_catalog.jsonl"
    if not catalog_source.is_file():
        raise ValueError("shared catalog is missing or empty")
    workspace = artifact_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    catalog_snapshot = workspace / "catalog.jsonl"
    shutil.copyfile(catalog_source, catalog_snapshot)
    catalog = load_catalog(catalog_snapshot)
    if not catalog:
        raise ValueError("shared catalog is missing or empty")
    history_snapshots, coverage = await _load_history(daily_store, run_date)
    history = [snapshot.record for snapshot in history_snapshots]
    diagnostic_callback = (
        diagnostics_sink
        if diagnostics_sink is not None
        else partial(update_diagnostics, artifact_dir)
    )
    recent_track_ids = frozenset(track.spotify_id for record in history for track in record.tracks)
    eligible_artists = {
        normalize_artist(track.artists[0])
        for track in catalog.values()
        if track.spotify_id not in recent_track_ids
        and track.artists
        and track.artists[0].strip()
        and track.duration_ms > 0
    }
    if len(eligible_artists) < 5:
        raise ValueError("catalog needs five eligible tracks with distinct primary artists")

    run_context = RunContext(
        run_date=run_date, now=now, http=http_client, resources_dir=resources_dir
    )
    persona, dj_reading = _select_dj(run_context, history)

    other_readings = await runner.read_all(
        run_context, services=[service for service in SERVICES if service.key != "dj_roll"]
    )
    audit_readings = [dj_reading, *other_readings]
    diagnostic_callback(
        inputs=[reading.model_dump(mode="json") for reading in audit_readings],
    )
    sanitized_dj = dj_reading.model_copy(update={"data": {"persona_id": persona.id}})
    readings: tuple[Reading, ...] = (sanitized_dj, *other_readings)
    scored_mood = await claude_service.score_mood(readings)
    diagnostic_callback(mood_usage=getattr(claude_service, "last_mood_usage", {}))
    mood = PlaylistMood.model_validate(scored_mood.model_dump())
    context = ValidationContext(
        run_date=run_date,
        dj_id=persona.id,
        catalog=catalog,
        recent_track_ids=recent_track_ids,
        readings=readings,
        mood=mood,
    )

    workspace_history = workspace / "history"
    workspace_history.mkdir(parents=True, exist_ok=True)
    for snapshot in history_snapshots:
        (workspace_history / f"{snapshot.record.date.isoformat()}.json").write_bytes(
            snapshot.content
        )
    _write_json(
        workspace / "context.json",
        {
            "date": run_date.isoformat(),
            "now": now.isoformat(),
            "timezone": "America/New_York",
            "dj": persona.id,
            "mood": mood.model_dump(),
            "history": coverage,
            "readings": [
                reading.model_dump(mode="json", exclude={"raw", "error"}) for reading in readings
            ],
        },
    )
    _write_json(
        workspace / "schemas/playlist_proposal.schema.json", PlaylistProposal.model_json_schema()
    )
    prompt = json.dumps(
        {"persona": persona.model_dump()},
        indent=2,
        ensure_ascii=False,
    )
    return PreparedRun(workspace, artifact_dir, persona, context, prompt, now)
