# Daily playlist agent harness

This document describes the current implementation in `src/the_bot_radio/generation/`.
The agent returns a structured creative proposal for an ordered, 10-track playlist based on
Greenpoint, Brooklyn inputs and the selected resident DJ's taste. The same generation job
validates the proposal, publishes and verifies a public Spotify playlist outside the agent
session, archives the published daily record, and replaces the canonical daily record.
Optional ReccoBeats tools support candidate research without changing playlist acceptance rules.

## Expected inputs

### Run context

- Run date and timezone-aware run timestamp, using `America/New_York`.
- Selected DJ ID from the date-seeded DJ roll, plus the persona's name, bio,
  and the authored `agent_instructions` and `speaking_voice`. Musical guidance remains
  agent judgment; no additional machine-enforced genre restrictions are defined.

### Neighborhood readings

Preparation rolls the DJ against published history, then collects the remaining readings
through `services.inputs.runner.read_all()`. The combined readings are in display order;
the table describes audit data (the agent prompt receives only the selected DJ ID for DJ roll):

| Reading | Expected information |
| --- | --- |
| DJ roll | Selected persona ID, candidates, excluded previous DJ, and seed |
| Day of week | Date, weekday, weekend flag, and observed federal holiday |
| Weather | Temperature, conditions, precipitation chance, wind, and daily high/low |
| G train | Current service status and active alerts |
| Sunset | Sunrise, sunset, civil dusk, and day length |
| Tide | Today's high/low events and next high/low at Hunters Point, Newtown Creek |
| Citi Bike | Available bikes and capacity across ZIP 11222, with station coverage |
| 311 | Latest complete complaint counts for ZIP 11222, including noise complaints |

Each reading includes a display value, status (`ok`, `stale`, or `skipped`), normalized
`data`, trimmed source `raw`, fetch timestamp, and an optional error. Give the agent normalized
data and status; retain raw payloads and errors for auditing. Skip failed sources and reduce
the influence of stale data. A stale reading with no usable data contributes no musical signal.

311 may report D−2 instead of yesterday because of publication lag; use its reported date.
Weather, G train, and Citi Bike are live snapshots, so a historical run date does not recreate
historical conditions.

### Music candidates and history

- Shared catalog tracks: verified Spotify IDs, titles, artists, durations, and cached
  descriptors where available. The DJ's taste guides selection from this shared pool;
  separate persona crate files are not required for this version.
- Compact descriptors on their source scales, such as ReccoBeats tempo, energy, valence,
  and danceability. Optional live feature lookups can supplement research during the daily
  run; persisting descriptors remains offline catalog preparation. Missing descriptors
  remain missing.
- Play history covering the preceding 30 days, across all DJs, to exclude recently played
  tracks. This history must contain real published tracks rather than the website fixtures.
- Spotify search results for proposed open-catalog wildcards, including canonical metadata
  and IDs. Search verifies proposals before they can enter the final playlist.

### Selection rules

- Exactly **10 tracks**, in playback order.
- Select 5 songs from the shared catalog and 5 verified Spotify wildcards whose IDs are
  outside the fixed catalog snapshot.
- No Spotify track ID repeated within 30 days and no primary artist appearing twice in the
  playlist. Primary artist means the first canonical credit; featured artists may recur.
- Respect the persona's restrictions and explain each selection.
- Playlist duration is derived from the selected tracks; a duration target is not yet defined.

## Files available to the agent

Paths below are relative to the run's agent-readable `workspace/`. The source catalog stays
at `src/the_bot_radio/resources/shared_catalog.jsonl`; published history comes from the
configured `DailyRecordStore` (local `data/days/` or S3 `<prefix>/days/`).
The run context and selected persona are supplied in the initial user prompt as JSON under `inputs` and `persona`. The context includes
the run date, timezone-aware timestamp, timezone, selected DJ ID, mood, history coverage, and
normalized readings with status and fetch time; raw payloads and errors are excluded from
that prompt. Preparation freezes the catalog/history and writes the proposal schema before
starting the agent. Archived published history files retain their full original payloads.

| File | Purpose |
| --- | --- |
| `catalog.jsonl` | One song per line, with Spotify metadata and cached audio descriptors. The pool for the five shared-catalog selections. |
| `history/YYYY-MM-DD.json` for the preceding 30 days | Published playlists across all DJs, used to exclude recent tracks and understand recent programming. Titles, sequencing, and notes can also help avoid repetitive sets. |
| `schemas/playlist_proposal.schema.json` | The required creative proposal structure, generated from the canonical Python proposal model. The host assembles the full daily record. |

The history window is `run_date - 30 days <= playlist date < run_date`, using New York dates.
Only successfully published playlists count toward the repeat rule; pending generations
and placeholder fixture records do not. Missing days are allowed; the run context records included, missing, and excluded dates
and whether history is incomplete.

Deterministic setup reads `personas.json` from the resources directory and passes the selected
`Persona` model (ID, name, bio, `agent_instructions`, and `speaking_voice`) in the prompt.
The agent does not receive the full personas file. The selected DJ ID remains in the run context for traceability.

The run context is prepared by deterministic code from the input services and DJ roll.
It preserves the actual date represented by each reading, including stale 311 data. Keep
trimmed raw payloads and collection errors in the run snapshot for auditing, without requiring
the agent to interpret them as musical evidence.

Selection rules and operating instructions belong in the agent's prompt; they do not need
another mutable data file. The output schema describes structure, while Python still checks
cross-record constraints such as recent repeats, primary-artist uniqueness, and the 5/5 split.

The website's `data/inputs.json` is display copy rather than today's evidence, so it is not
a required agent input. A separate play-history summary is also unnecessary initially because
the daily files already contain the relevant track history.

## Tools available to the agent

| Tool | Purpose |
| --- | --- |
| File reading and search (`Read`, `Grep`, `Glob`) | Use the Claude Agent SDK's capabilities to inspect the run's fixed input snapshot. |
| Spotify search | Accept a query, including title and artist filters, and call `SpotifyService.search_tracks()`. Return canonical Spotify IDs, URLs, titles, artists, albums, and durations so the agent can distinguish recordings and versions. Retain results in run state for validation and include each selected track's URL in the daily output. A nonempty result list alone does not establish a match. |
| ReccoBeats audio features | Optionally look up 1–40 Spotify or ReccoBeats track IDs to evaluate candidates and musical transitions. Return provider feature rows and metadata unchanged; partial or missing coverage is successful. |
| ReccoBeats recommendations | Optionally explore recordings around 1–5 suitable seeds, with feature targets and weighting controls. Return provider track metadata unchanged, defaulting to 10 results with a 20-result agent cap. Verify recommended discoveries through Spotify search before selecting them. |
| Playlist validation | Accept a proposed daily playlist and check exactly 10 tracks in positions 1–10; five catalog selections and five verified wildcards with consistent flags; no duplicate tracks or primary artists; no tracks played in the preceding 30 days; catalog membership; wildcard IDs and metadata against retained search results; required fields, Spotify URLs, canonical metadata, creative-title constraints, and input effects. Return `valid` and violations with stable codes, affected positions where applicable, and actionable messages. |

`PlaylistValidator(context).validate_playlist(proposal)` wraps the reusable validation function
and uses the run's fixed catalog, published history, and retained Spotify search results; the agent cannot override those reference inputs through tool arguments. The agent
revises invalid selections and validates again before submitting its final output. Subjective
musical fit remains the agent's responsibility.

The reusable validator runs during final proposal acceptance and again during host record
assembly. Mood ranges are checked by `PlaylistMood` during preparation; mood is not supplied
by the proposal. Spotify playlist creation and publishing remain outside the agent loop and
are not exposed as agent tools.

## Proposal and daily record contracts

The agent receives only `schemas/playlist_proposal.schema.json` and returns structured output;
it does not write a daily file. `PlaylistProposal` contains only:

- A creative title of 2–4 whitespace-separated words, without a date. The validator rejects
  titles matching any selected track title after case and whitespace normalization.
- Spotify description (at most 300 characters) and a nonempty list of liner-note paragraphs.
- Exactly 10 tracks with positions, Spotify IDs/URLs, titles, canonical artist lists,
  positive durations, reasons, and wildcard flags.
- Exactly one effect entry per reading key, with an effect string and weight in `[0, 1]` or null.

The system prompt requests empty effects and null weights for skipped or unusable stale inputs.
The validator permits null or zero weights for those inputs, but requires empty effects.
DJ-roll weight must always be null. Other subjective freshness weighting and musical fit remain
agent judgment.

`assemble_daily_record()` reruns the shared validator and attaches trusted date, DJ, mood,
readings, archived prompt, model, generation timestamp, and initial pending publication state.
The host prefixes the title as `YYYY-MM-DD — <creative title>` and derives the display artist
from the canonical artist list. The agent cannot change the separately scored mood.

`DailyRecord` is the strict generation contract in `generation/models.py`; the website's
`Day` model remains more permissive for legacy records. The strict model enforces mood
ranges, track count/order, the 5/5 flag split, unique IDs and primary artists, and consistent
track URLs/display credits. Context-dependent checks against catalog, history, and Spotify
search evidence remain in `generation/validation.py`.

`write_schemas()` can export both proposal and daily schemas, but preparation writes only
the proposal schema into the agent workspace. The pending record exists in memory between
assembly and publication; normal generation does not persist a pending canonical daily file.

## Execution and SDK configuration

`PlaylistAgent` owns one `ClaudeSDKClient` session and one `query()` per invocation.
The agent may revise after tool feedback within that task. The host never sends a repair
prompt after final output fails validation.

1. **Prepare:** copy and validate the catalog, load the previous 30 days, require at least
   five eligible catalog primary artists, roll the DJ, collect readings, and score mood with
   `ClaudeService`. Archive full readings in diagnostics. The prompt excludes reading raw
   payloads/errors and reduces the DJ-roll data to the selected persona ID; other residents
   and roll candidates are not exposed in that prompt.
2. **Configure:** `_build_options()` sets the workspace, model, system prompt, proposal JSON
   schema, turn limit, and budget. Built-in tools are exactly `Read`, `Grep`, and `Glob`.
   The in-process MCP server registers Spotify search, ReccoBeats audio features and
   recommendations, and playlist validation. `StructuredOutput` is also permitted for the
   SDK's structured response.
3. **Constrain access:** options use `permission_mode="dontAsk"`, `setting_sources=[]`, and
   `strict_mcp_config=True`. A `PreToolUse` filesystem guard allows only the intended tools,
   restricts read/search paths to the workspace, and rejects symlinks and escaping glob
   patterns. `cwd` alone is not the boundary. There are no shell, write, or publishing tools.
4. **Stream:** `_receive_response()` sends the prompt and logs SDK messages. It deduplicates
   assistant message IDs within the invocation and interrupts if their count exceeds the
   turn limit. The session, response processing, and client cleanup are inside an
   `asyncio.timeout()` deadline.
5. **Accept:** `_process_response()` records the terminal result's usage and requires a
   successful, non-error result with structured output. Refusals and explicit API-error or
   aborted terminal reasons fail. Schema-valid output is captured as `proposal.json` before
   checking final budget/turn usage and playlist rules, so rule-invalid or over-budget
   proposals can be retained. Failed SDK results and schema-invalid output are not captured.
6. **Validate again:** the agent uses its injected `PlaylistValidator` for tool feedback and
   final acceptance. The daily job calls `assemble_daily_record()` for an independent
   mandatory check before publication. A frozen `PlaylistAgentResult` carries the accepted
   proposal and archived system/user prompt.

Spotify search accepts a trimmed query of 1–500 characters, a limit of 1–10 (default 10),
and an offset of 0–1000. Requests are serialized with a per-tool-instance lock. Results include
album metadata as well as IDs, URLs, titles, artist lists, and durations; successful canonical
results are retained by ID in `validator.context.search_results`. Final proposals do not have
an album field. Tool failures return sanitized errors and emit outcome events.

Defaults are 40 agent turns, a $5 estimated agent budget, and a 600-second agent-session
deadline. These limits exclude preparation, standalone mood scoring, and publication. Mood
usage is recorded separately. Tests inject a fake Anthropic client for mood scoring and an
`agent_client_factory` for SDK orchestration; the daily job can also receive a test agent.

## Publication and canonical saving

`generate_day()` automatically calls `publish_playlist()` after shared validation, in both
local and S3 storage modes. The CLI is `uv run the-bot-radio-generate`; its default mode
publishes a public Spotify playlist.

`--dry-run` instead uses `preview_day()` to run the shared preparation, agent invocation,
and host validation stages, then prints the complete unpublished `DailyRecord` as indented
JSON to stderr. Stdout is empty. The record remains `publication_status="pending"` with
publication metadata unset; Spotify publication and canonical daily-record writes are skipped.
History comes only from local `--data-dir/days/`, so recent-play exclusions and DJ selection
use that local history. Missing history retains the normal incomplete-coverage behavior.
The preview ignores S3 bucket, prefix, and staging environment settings and constructs no
AWS client. Its workspace, transcript, diagnostics, and any recorder snapshots live under a
temporary directory that is removed on success, failure, or cancellation; errors do not
advertise retained artifacts or recovery locations. `--verbose` also streams redacted events
to stderr before the final JSON. Live input services, Claude, Spotify search, and optional
ReccoBeats requests still run with normal API costs and the configured generation limits.

The deterministic publisher:

1. Records the requested name, description, public visibility, and ordered IDs in
   `publication.json`, then checkpoints artifacts before creation.
2. Creates the playlist and checkpoints its returned ID/URL before adding tracks.
3. Inserts all ten tracks in order, then reads the playlist back and checks identity, name,
   public visibility, exact ordered IDs, item count when supplied, and a nonempty snapshot ID.
4. Returns a published `DailyRecord` with playlist ID/URL, snapshot ID, public flag, and
   timezone-aware publication timestamp.

The host serializes that published record once, writes it into local staging, and checkpoints
its identical bytes beneath `runs/<date>/<run-id>/<date>.json` before replacing the canonical
`days/<date>.json`. Local writes are atomic; S3 uses the corresponding bucket/prefix keys.
Every invocation generates afresh, even if that date already exists. One generation job at a
time is assumed; there is no generation lock.

Publication failures preserve previous canonical daily bytes and retain any created Spotify
playlist. Mutations are not automatically retried, resumed, deleted, or rolled back. Known
identity and progress remain in publication artifacts/diagnostics. A later archive failure can
also leave a published Spotify playlist without a new canonical daily record.

If canonical replacement raises ambiguously, the job reads once with a five-second timeout
and compares exact bytes; it does not retry replacement. Diagnostics record whether output
committed. Cancellation still propagates, and finalization can report an error after a
successful commit; recovery notes distinguish these outcomes.

Only strict records explicitly marked published enter future repeat history. Missing and
excluded dates are recorded as incomplete coverage; malformed JSON or invalid published
history fails preparation rather than silently shrinking history. Original history bytes are
copied into the workspace. Website loading excludes pending records while retaining legacy
fixture compatibility.

## Storage and run artifacts

For normal generation, the CLI selects local storage by default. `BOT_RADIO_S3_BUCKET` enables S3 and
`BOT_RADIO_S3_PREFIX` scopes its keys. `BOT_RADIO_RUN_DIR` can override the S3 staging root;
otherwise staging defaults to `<data-dir>/.run-staging` in either mode. Resources remain
packaged in `src/the_bot_radio/resources/`, independent of output storage.

Each run uses `<local-work-root>/<date>/<run-id>/`:

```text
workspace/                          # agent-readable fixed snapshot
  catalog.jsonl
  history/YYYY-MM-DD.json
  schemas/playlist_proposal.schema.json
proposal.json                       # successful SDK result, schema-valid proposal
search_results.json                 # retained canonical search evidence
publication.json                    # publication progress and known Spotify identity
transcript.jsonl                    # SDK messages, tool events, usage, outcome
diagnostics.json                    # raw inputs, mood usage, outcome, recovery metadata
YYYY-MM-DD.json                     # validated published output, if reached
```

Artifacts are created as their stages are reached, so failed runs may have only a subset.
`RunRecorder` keeps local staging, redacts secrets, periodically checkpoints transcript and
diagnostics, and performs full checkpoints at lifecycle boundaries. Archived destinations are
`<data-dir>/runs/<date>/<run-id>/` locally or `<prefix>/runs/<date>/<run-id>/` in S3. Local staging
must be separate from the artifact destination. Host artifacts outside `workspace/` are not
agent-readable. The initial context is supplied in the prompt, not a separate workspace file.

`recover_run_artifacts()` reuploads retained evidence under the original run identity. It
never calls Spotify, regenerates a proposal, or replaces canonical daily data. Discoveries are
not automatically promoted into the shared catalog; catalog enrichment remains offline work.

## Module responsibilities

Paths are relative to `src/the_bot_radio/`.

| Module | Responsibility |
| --- | --- |
| `generation/daily.py` | CLI and orchestration: prepare, invoke, assemble, publish, archive, commit, and finalize |
| `generation/preparation.py` | Catalog/history snapshots, DJ selection, readings, fixed mood, prompt, and proposal schema |
| `generation/agent.py` | System prompt, SDK options/session, streamed response handling, usage/limits, and final proposal acceptance |
| `generation/models.py` | Strict proposal/daily contracts, validation context, and schema exports |
| `generation/validation.py` | Shared checks, bound `PlaylistValidator`, and host record assembly |
| `generation/tools.py` | In-process MCP search, ReccoBeats research, and validation tools; canonical Spotify search evidence |
| `generation/publication.py` | Checkpointed public Spotify creation, insertion, and verification |
| `generation/runtime.py` | Limits, filesystem guard, redacted diagnostics/transcript, recorder, and artifact recovery |
| `generation/config.py` | Storage selection and client/store lifetime |
| `generation/storage.py`, `generation/s3_storage.py` | Daily-byte and run-artifact store interfaces/backends |
| `services/claude_service.py` | Standalone Anthropic requests, including mood scoring |
| `services/spotify_service.py` | All Spotify Web API calls used by tools and publication |

## Validation coverage and qualification

The original harness phases are implemented. Offline tests in `tests/generation/` cover
preparation and history boundaries, proposal validation, tool evidence, fake SDK streams,
permissions, limits, rejected-output retention, publication checkpoints/readback, local/S3
archival and replacement, cancellation, and artifact recovery. Async tests use anyio; HTTP
mocks use respx; SDK tests inject fake clients.

The earlier qualification recorded on 2026-10-27 produced a pending ten-track record in
15 turns at approximately $0.45, excluding mood scoring, without creating a Spotify playlist.
That describes the earlier harness, not the current CLI publication behavior. The current
opt-in `test_live_generation_writes_a_temporary_day_with_mocked_publication` uses real
inputs/agent/search but mocks publication and asserts a published record; it does not create
a real Spotify playlist. Live tests require `RUN_LIVE=1`; S3 qualification is separately gated.

SDK references: [Python API](https://code.claude.com/docs/en/agent-sdk/python),
[custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools),
[structured output](https://code.claude.com/docs/en/agent-sdk/structured-outputs),
[permissions](https://code.claude.com/docs/en/agent-sdk/permissions), and
[hooks](https://code.claude.com/docs/en/agent-sdk/hooks).

## Reccobeats tools

Implemented service and collector portion:

- Added `services/reccobeats_service.py` with the injected `ReccobeatsService`, exposing
  `get_audio_features()` for `GET /v1/audio-features` and `get_recommendation()` for
  `GET /v1/track/recommendation`. The typed recommendation contract validates provider
  controls, rejects unknown fields, and uses an empty `negativeSeeds` list (omitted from
  the HTTP query). Responses
  preserve provider envelopes and rows; recommendations return track metadata.
- Replaced the script-local `ReccoBeatsClient` in `scripts/collect_song_features.py` with
  the shared service, retaining collector batches of 20, feature-row interpretation,
  request pacing, bounded retries, both `Retry-After` forms, and catalog/cache behavior.
  No API key is required; numeric provider rate limits are unpublished. Offline service
  and collector tests cover transport, validation, row isolation, caching, and recovery.

Implemented harness integration:

- Daily orchestration injects one service per run, using its shared HTTP client unless a
  test service is supplied. `PlaylistAgent`, `PlaylistTools`, and `build_tool_server` require
  this dependency. Both tools are registered in the `playlist` MCP server and allowed by
  SDK options and the filesystem guard. The adapters reuse service pacing, retries, and
  cooldown without an additional lock or retry loop.
- Tool definitions describe behavior, inputs, and outputs; `PLAYLIST_SYSTEM_PROMPT` explains
  when and how to use each tool toward a coherent, verified set. It guides optional feature
  research for candidates and transitions, and recommendations around suitable seeds. Recommendations
  supply track metadata; feature lookups supply audio features. Recommended discoveries
  still require canonical Spotify search evidence. The five-catalog/five-discovery split,
  thirty-day exclusions, unique primary artists, and mandatory tool and host validation
  remain unchanged.
- Partial or missing features, empty recommendations, and provider failures are recoverable
  outcomes: continue with catalog evidence and Spotify search. Research tools preserve
  provider envelopes and never modify catalog files, descriptors, or validator search
  evidence. Failures are sanitized; cancellation propagates.
- Offline tests cover adapter contracts, registration and schema limits, permissions,
  dependency propagation, failure recovery in a fake SDK session, and unchanged acceptance
  rules. Live qualification remains opt-in and must mock Spotify publication.

`get_audio_features` accepts only `ids`, containing 1–40 nonempty track IDs without commas.
`get_recommendation` reuses `RecommendationRequest`, including all seed and feature-control
validation and the `featureWeight`/`feature_weight` aliases, with a strict integer `size`
of 1–20 (default 10). Unknown fields and negative seeds are unavailable. The shared service
retains its 100-result capability for other callers. MCP input schemas are generated from
the same argument models used for validation before delegation.

Invalid arguments return MCP errors `invalid_audio_features_arguments` or
`invalid_recommendation_arguments`; provider and unexpected failures return
`audio_features_failed` or `recommendation_failed` without exception details, URLs, or
response bodies. Each invocation records a `reccobeats_audio_features` or
`reccobeats_recommendation` transcript outcome with status `invalid_arguments`, `error`,
`empty`, or `ok`; successful calls include the returned row count. No new credentials,
environment settings, cache, deployment, or publication behavior is introduced.
