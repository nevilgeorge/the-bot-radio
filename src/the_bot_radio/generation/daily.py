"""Generate a pending daily record through injected storage interfaces.

Each invocation retains evidence beneath the supplied local work root and
checkpoints snapshots through the supplied artifact store. Canonical
records are replaced through DailyRecordStore after archiving validated output.
One generation job at a time is assumed; no generation lock is used.
Recover failed uploads with runtime.recover_run_artifacts using the original
run identity and retained staging directory, without regenerating the day.
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from the_bot_radio.generation.agent import PlaylistAgent
from the_bot_radio.generation.config import GenerationStorageConfig, generation_stores
from the_bot_radio.generation.preparation import PreparedRun, prepare_run
from the_bot_radio.generation.runtime import (
    AgentLimits,
    RunRecorder,
    error_details,
    redact_secrets,
    update_diagnostics,
    write_json_atomic,
)
from the_bot_radio.generation.storage import (
    DailyRecordStore,
    RunArtifactStore,
    atomic_write_bytes,
)
from the_bot_radio.generation.validation import PlaylistValidator, assemble_daily_record
from the_bot_radio.resources import RESOURCE_DIR
from the_bot_radio.services.claude_service import MODEL, ClaudeService
from the_bot_radio.services.inputs.base import TZ, make_http_client
from the_bot_radio.services.spotify_service import SpotifyService
from the_bot_radio.web.config import DATA_DIR


async def generate_day(
    run_date: date,
    *,
    daily_store: DailyRecordStore,
    artifact_store: RunArtifactStore,
    local_work_root: Path,
    resources_dir: Path,
    now: datetime | None = None,
    model: str = MODEL,
    limits: AgentLimits | None = None,
    verbose: bool = False,
    # Provided by tests only.
    claude_service: ClaudeService | None = None,
    spotify_service: SpotifyService | None = None,
    playlist_agent: PlaylistAgent | None = None,
) -> str:
    """Generate fresh output, archive validated bytes, and replace the canonical day."""
    # Assert required dependencies are non-null
    for dependency_name, dependency in (
        ("daily_store", daily_store),
        ("artifact_store", artifact_store),
        ("local_work_root", local_work_root),
        ("resources_dir", resources_dir),
    ):
        if dependency is None:
            raise ValueError(f"{dependency_name} is required and must not be None")

    now = now or datetime.now(TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("run timestamp must be timezone-aware")
    now = now.astimezone(TZ)
    limits = limits or AgentLimits()
    run_id = uuid.uuid4().hex
    local_run_dir = (local_work_root / run_date.isoformat() / run_id).resolve()
    location = daily_store.storage_location(run_date)
    recorder = RunRecorder(
        artifact_store,
        run_date,
        run_id,
        local_run_dir,
        console=sys.stderr if verbose else None,
    )
    prepared: PreparedRun | None = None
    committed = False
    started = False
    status = "failed"
    outcome: dict[str, object] = {}
    primary_error: BaseException | None = None
    phase = "initialization"

    async def reconcile(content: bytes) -> bool:
        """Check an ambiguous write once without retrying canonical replacement."""
        try:
            async with asyncio.timeout(5):
                actual = await daily_store.read(run_date)
            result = (
                "exact_match"
                if actual == content
                else ("absent" if actual is None else "different_content")
            )
        except BaseException as error:
            result = "read_" + type(error).__name__
        outcome["reconciliation"] = result
        return result == "exact_match"

    try:
        await recorder.start(now=now, model=model, daily_file=location, run_id=run_id)
        started = True
        phase = "preparation"
        async with make_http_client() as http_client:
            claude_service = claude_service or ClaudeService()
            spotify_service = spotify_service or SpotifyService(http_client)
            prepared = await prepare_run(
                local_run_dir,
                run_date,
                now,
                http_client,
                claude_service,
                resources_dir=resources_dir,
                daily_store=daily_store,
                diagnostics_sink=recorder.update_diagnostics,
            )
            recorder.append({"event": "prepared", "mood": prepared.context.mood})
            await recorder.checkpoint()
            playlist_agent = playlist_agent or PlaylistAgent(
                spotify_service, PlaylistValidator(prepared.context)
            )
            phase = "agent"
            result = await playlist_agent.run(
                workspace=prepared.workspace,
                prompt=prepared.prompt,
                model=model,
                limits=limits,
                event_sink=recorder.append,
                proposal_sink=lambda proposal: write_json_atomic(
                    local_run_dir / "proposal.json", proposal
                ),
            )
            phase = "validation"
            record = assemble_daily_record(
                result.proposal,
                prepared.context,
                result.prompt,
                model,
                prepared.generated_at,
            )
            content = (record.model_dump_json(indent=2) + "\n").encode("utf-8")
            write_json_atomic(
                local_run_dir / "search_results.json", prepared.context.search_results
            )
            atomic_write_bytes(
                local_run_dir / f"{run_date.isoformat()}.json", content, replace=True
            )
            phase = "archive"
            await recorder.checkpoint()
            phase = "commit"
            try:
                location = await daily_store.write(run_date, content)
            except BaseException as error:
                # Even cancellation can arrive after a write became visible.
                reconciliation_task = asyncio.create_task(reconcile(content))
                cancellation_error: asyncio.CancelledError | None = None
                while True:
                    try:
                        committed = await asyncio.shield(reconciliation_task)
                        break
                    except asyncio.CancelledError as cancelled:
                        cancellation_error = cancelled
                if cancellation_error is not None:
                    raise cancellation_error from error
                if isinstance(error, asyncio.CancelledError) or not committed:
                    raise
            else:
                committed = True
            if committed:
                status = "accepted"
    except BaseException as error:
        primary_error = error
        outcome.update(error_details(error, phase))
    finally:
        # A partial workspace is included by finish even if preparation never returned.
        if prepared is not None:
            try:
                write_json_atomic(
                    local_run_dir / "search_results.json", prepared.context.search_results
                )
            except BaseException as error:
                if primary_error is None:
                    primary_error = error
                    outcome.update(error_details(error, "finalization"))
                else:
                    primary_error.add_note(
                        f"Search evidence archival failed: {type(error).__name__}"
                    )
        status = "accepted" if committed else ("failed" if primary_error else status)
        outcome["daily_file"] = location
        outcome["committed"] = committed
        try:
            async with asyncio.timeout(30):
                if started:
                    await recorder.finish(status, **outcome)
                else:
                    # Startup failure still leaves a local outcome for recovery.
                    update_diagnostics(local_run_dir, status=status, **outcome)
                    recorder.transcript.append({"event": "finished", "status": status, **outcome})
        except BaseException as error:
            if primary_error is None:
                primary_error = error
                if not committed:
                    # Keep the local snapshot recoverable even after finish closed.
                    try:
                        update_diagnostics(
                            local_run_dir, status="failed", **error_details(error, "finalization")
                        )
                        recorder.transcript.append(
                            {
                                "event": "finalization_failed",
                                "status": "failed",
                                **error_details(error, "finalization"),
                            }
                        )
                    except BaseException as local_error:
                        primary_error.add_note(
                            f"Local outcome update failed: {type(local_error).__name__}"
                        )
            else:
                primary_error.add_note(f"Run finalization failed: {type(error).__name__}")
        finally:
            try:
                await recorder.close()
            except BaseException as error:
                if primary_error is None:
                    primary_error = error
                else:
                    primary_error.add_note(f"Run cleanup failed: {type(error).__name__}")
    if primary_error is not None:
        context = f"Run recovery: run_id={run_id}; staging_directory={local_run_dir}"
        if committed:
            context += f"; committed_location={location}"
        if "reconciliation" in outcome:
            context += f"; reconciliation={outcome['reconciliation']}"
        primary_error.add_note(context)
        raise primary_error
    return location


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
    """Replace a pending daily file and print its location, returning nonzero on failure."""
    arguments = parse_args(argv)
    try:
        now = datetime.now(TZ)
        env_date = os.environ.get("RUN_DATE")
        run_date = arguments.run_date or (date.fromisoformat(env_date) if env_date else now.date())
        limits = AgentLimits(arguments.max_turns, arguments.budget_usd, arguments.deadline_seconds)
        storage_config = GenerationStorageConfig.from_environment(arguments.data_dir, os.environ)

        async def run_generation() -> str:
            """Own selected stores until generation has finalized its recorder."""
            async with generation_stores(storage_config) as stores:
                return await generate_day(
                    run_date,
                    now=now,
                    model=arguments.model,
                    limits=limits,
                    verbose=arguments.verbose,
                    daily_store=stores.daily_store,
                    artifact_store=stores.artifact_store,
                    local_work_root=stores.local_work_root,
                    resources_dir=RESOURCE_DIR,
                )

        path = asyncio.run(run_generation())
    except (Exception, KeyboardInterrupt) as error:
        message = redact_secrets(str(error))
        print(
            f"Generation failed ({type(error).__name__}): {message}; see run artifacts.",
            file=sys.stderr,
        )
        for note in getattr(error, "__notes__", ()):
            if note.startswith("Run recovery:"):
                print(redact_secrets(note), file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
