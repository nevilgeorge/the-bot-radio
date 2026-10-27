"""Prepare a fixed evidence workspace and trusted references for one daily agent run."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

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


def _write_json(path: Path, payload: object) -> None:
    """Write a readable JSON artifact inside a newly prepared run directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_history(data_dir: Path, run_date: date) -> tuple[list[DailyRecord], dict[str, object]]:
    """Read strict explicitly published records, excluding pending and legacy fixtures."""
    records: list[DailyRecord] = []
    missing_dates: list[str] = []
    excluded_dates: list[str] = []
    for days_before in range(30, 0, -1):
        playlist_date = run_date - timedelta(days=days_before)
        path = data_dir / "days" / f"{playlist_date.isoformat()}.json"
        if not path.exists():
            missing_dates.append(playlist_date.isoformat())
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"invalid history record: {path.name}")
        if payload.get("publication_status") != "published":
            excluded_dates.append(playlist_date.isoformat())
            continue
        record = DailyRecord.model_validate(payload)
        if record.date != playlist_date:
            raise ValueError(f"history date does not match filename: {path.name}")
        records.append(record)
    return records, {
        "window_start": (run_date - timedelta(days=30)).isoformat(),
        "window_end_exclusive": run_date.isoformat(),
        "included_dates": [record.date.isoformat() for record in records],
        "missing_dates": missing_dates,
        "excluded_dates": excluded_dates,
        "incomplete": bool(missing_dates or excluded_dates),
    }


def _select_dj(run_context: RunContext, history: list[DailyRecord]) -> tuple[Persona, Reading]:
    """Load residents, roll against published history, and validate the selected persona."""
    personas = TypeAdapter(list[Persona]).validate_json(
        (run_context.data_dir / "personas.json").read_text(encoding="utf-8")
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
    data_dir: Path,
    artifact_dir: Path,
    run_date: date,
    now: datetime,
    http_client: httpx.AsyncClient,
    claude_service: ClaudeService,
) -> PreparedRun:
    """Freeze inputs, roll the DJ from published history, and compute the fixed mood."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("run timestamp must be timezone-aware")
    now = now.astimezone(TZ)
    catalog_source = data_dir / "shared_catalog.jsonl"
    if not catalog_source.is_file():
        raise ValueError("shared catalog is missing or empty")
    workspace = artifact_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    catalog_snapshot = workspace / "catalog.jsonl"
    shutil.copyfile(catalog_source, catalog_snapshot)
    catalog = load_catalog(catalog_snapshot)
    if not catalog:
        raise ValueError("shared catalog is missing or empty")
    history, coverage = _load_history(data_dir, run_date)
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

    run_context = RunContext(run_date=run_date, now=now, http=http_client, data_dir=data_dir)
    persona, dj_reading = _select_dj(run_context, history)

    other_readings = await runner.read_all(
        run_context, services=[service for service in SERVICES if service.key != "dj_roll"]
    )
    audit_readings = [dj_reading, *other_readings]
    update_diagnostics(
        artifact_dir,
        inputs=[reading.model_dump(mode="json") for reading in audit_readings],
    )
    sanitized_dj = dj_reading.model_copy(update={"data": {"persona_id": persona.id}})
    readings: tuple[Reading, ...] = (sanitized_dj, *other_readings)
    scored_mood = await claude_service.score_mood(readings)
    update_diagnostics(artifact_dir, mood_usage=getattr(claude_service, "last_mood_usage", {}))
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
    for record in history:
        _write_json(
            workspace / "history" / f"{record.date.isoformat()}.json",
            record.model_dump(mode="json"),
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
