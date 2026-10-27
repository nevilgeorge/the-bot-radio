"""Generate a daily playlist file; Spotify publication is a separate operation.

Filesystem layout (``data_dir`` defaults to the repository's ``data/`` directory)::

    data/
    ├── personas.json                   All resident definitions, read by setup code.
    ├── inputs.json                     Input descriptions used by the website.
    ├── shared_catalog.jsonl            Canonical shared song catalog.
    ├── days/
    │   └── YYYY-MM-DD.json              Validated daily record, initially pending publication.
    └── runs/
        └── YYYY-MM-DD/
            ├── generation.lock         Prevents concurrent generation for this date.
            └── <run-id>/               A distinct directory for every generation run.
                ├── proposal.json       Schema-valid final response before assembly.
                ├── transcript.jsonl    SDK messages, tools, usage, and validation events.
                ├── diagnostics.json    Status, raw readings, mood usage, and failure details.
                ├── search_results.json Canonical Spotify evidence for wildcard verification.
                └── workspace/          The only subtree the agent can read.
                    ├── context.json    Date, DJ ID, mood, readings, and history coverage.
                    ├── catalog.jsonl   Fixed copy of the shared catalog for this run.
                    ├── history/
                    │   └── YYYY-MM-DD.json  Published records from the preceding 30 days.
                    └── schemas/
                        └── playlist_proposal.schema.json  Required agent response fields.

Permanent inputs and accepted daily records live directly under ``data_dir``. Each run has
its own diagnostic artifacts and fixed evidence snapshot. Setup reads ``personas.json`` once;
only the selected persona is sent in the prompt, with no persona files copied into the run.

The host saves schema-valid final output from a successful SDK result to ``proposal.json``
before resource and playlist validation. The file can therefore contain an invalid selection
on a failed run; ``diagnostics.json`` records whether the run was accepted. Malformed responses
remain in the transcript. ``PlaylistAgent`` runs one task and returns the accepted proposal
and archived prompt; it never sends a host repair prompt after final validation fails. The daily
job adds trusted run metadata once, validates the assembled record, and saves
``days/<date>.json`` atomically only after acceptance. The agent cannot write any of these
files, read sibling run artifacts, or publish a Spotify playlist.

Diagnostics begins with status ``running`` and retains raw readings and mood usage as setup
progresses, then finishes with status ``accepted`` or ``failed``. Early failures may leave no
workspace, proposal, or search evidence. Separate run IDs preserve failed attempts; an existing
accepted daily record makes a rerun a no-op. Older run layouts remain untouched.
"""

import argparse
import asyncio
import fcntl
import os
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from the_bot_radio.generation.agent import PlaylistAgent
from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.preparation import PreparedRun, prepare_run
from the_bot_radio.generation.runtime import (
    AgentLimits,
    LogTranscript,
    update_diagnostics,
    write_json_atomic,
)
from the_bot_radio.generation.validation import PlaylistValidator, assemble_daily_record
from the_bot_radio.services.claude_service import MODEL, ClaudeService
from the_bot_radio.services.inputs.base import TZ, make_http_client
from the_bot_radio.services.spotify_service import SpotifyService
from the_bot_radio.web.config import DATA_DIR


@contextmanager
def generation_lock(data_dir: Path, run_date: date) -> Iterator[None]:
    """Hold a stable nonblocking per-day lock through final acceptance and saving."""
    lock_path = data_dir / "runs" / run_date.isoformat() / "generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another generation is running for this date") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def existing_record(path: Path, run_date: date) -> DailyRecord | None:
    """Return a valid accepted output, refusing to replace malformed existing data."""
    if not path.exists():
        return None
    try:
        record = DailyRecord.model_validate_json(path.read_bytes())
        if record.date != run_date:
            raise ValueError("record date differs from filename")
    except (ValueError, OSError) as error:
        raise ValueError(f"existing daily file is invalid: {path.name}") from error
    return record


def save_daily_record(path: Path, record: DailyRecord) -> None:
    """Publish one complete accepted file atomically without replacing an existing day."""
    # Reparse even typed records so mutations cannot bypass persistence constraints.
    accepted_record = DailyRecord.model_validate_json(record.model_dump_json())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output_file:
            temporary_path = Path(output_file.name)
            output_file.write(accepted_record.model_dump_json(indent=2) + "\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        # Linking is atomic and fails if another writer already created the destination.
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def generate_day(
    data_dir: Path,
    run_date: date,
    *,
    now: datetime | None = None,
    model: str = MODEL,
    limits: AgentLimits | None = None,
    verbose: bool = False,
    claude_service: ClaudeService | None = None,
    spotify_service: SpotifyService | None = None,
    playlist_agent: PlaylistAgent | None = None,
) -> Path:
    """Prepare, run, accept, and persist a day with injectable external services."""
    now = now or datetime.now(TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("run timestamp must be timezone-aware")
    limits = limits or AgentLimits()
    daily_path = data_dir / "days" / f"{run_date.isoformat()}.json"
    with generation_lock(data_dir, run_date):
        if existing_record(daily_path, run_date) is not None:
            return daily_path
        artifact_dir = data_dir / "runs" / run_date.isoformat() / uuid.uuid4().hex
        artifact_dir.mkdir(parents=True)
        transcript = LogTranscript(
            artifact_dir / "transcript.jsonl", console=sys.stderr if verbose else None
        )
        transcript.append({"event": "started", "date": run_date, "now": now, "model": model})
        update_diagnostics(artifact_dir, status="running", date=run_date, now=now, model=model)
        prepared: PreparedRun | None = None
        try:
            async with make_http_client() as http_client:
                claude_service = claude_service or ClaudeService()
                spotify_service = spotify_service or SpotifyService(http_client)
                prepared = await prepare_run(
                    data_dir, artifact_dir, run_date, now, http_client, claude_service
                )
                transcript.append({"event": "prepared", "mood": prepared.context.mood})
                playlist_agent = playlist_agent or PlaylistAgent(
                    spotify_service, PlaylistValidator(prepared.context)
                )
                result = await playlist_agent.run(
                    workspace=prepared.workspace,
                    prompt=prepared.prompt,
                    model=model,
                    limits=limits,
                    event_sink=transcript.append,
                    proposal_sink=lambda proposal: write_json_atomic(
                        artifact_dir / "proposal.json", proposal
                    ),
                )
                # Validate again at the host boundary and attach trusted run metadata once.
                record = assemble_daily_record(
                    result.proposal,
                    prepared.context,
                    result.prompt,
                    model,
                    prepared.generated_at,
                )
                save_daily_record(daily_path, record)
            outcome = {"status": "accepted", "daily_file": str(daily_path)}
        except BaseException as error:
            outcome = {"status": "failed", "error_type": type(error).__name__}
            update_diagnostics(artifact_dir, **outcome)
            transcript.append({"event": "finished", **outcome})
            raise
        finally:
            if prepared is not None:
                write_json_atomic(
                    artifact_dir / "search_results.json", prepared.context.search_results
                )
        update_diagnostics(artifact_dir, **outcome)
        transcript.append({"event": "finished", **outcome})
    return daily_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse generation options without changing the website's command."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, dest="run_date")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--deadline-seconds", type=float, default=600.0)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream redacted run events and agent messages to stderr.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Generate a pending daily file and print its location, returning nonzero on failure."""
    arguments = parse_args(argv)
    try:
        now = datetime.now(TZ)
        env_date = os.environ.get("RUN_DATE")
        run_date = arguments.run_date or (date.fromisoformat(env_date) if env_date else now.date())
        limits = AgentLimits(arguments.max_turns, arguments.budget_usd, arguments.deadline_seconds)
        path = asyncio.run(
            generate_day(
                arguments.data_dir,
                run_date,
                now=now,
                model=arguments.model,
                limits=limits,
                verbose=arguments.verbose,
            )
        )
    except (Exception, KeyboardInterrupt) as error:
        print(f"Generation failed ({type(error).__name__}); see run artifacts.", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
