# Daily playlist agent loop

The agent loop produces a daily JSON file describing an ordered, 10-track playlist based on
inputs from Greenpoint, Brooklyn, guided by the selected resident DJ's taste. Deterministic
code outside the agent loop reads this file and creates the Spotify playlist.

## Expected inputs

### Run context

- Run date and timezone-aware run timestamp, using `America/New_York`.
- Selected DJ ID from the date-seeded DJ roll, plus the persona's name, bio, taste prompt,
  and the authored `agent_instructions` and `speaking_voice`. Musical guidance remains
  agent judgment; no additional machine-enforced genre restrictions are defined.

### Neighborhood readings

Readings come from `services.inputs.runner.read_all()`, in display order:

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
  and danceability. Missing descriptors remain missing. Fetching new descriptors belongs
  to offline catalog preparation, outside the daily run.
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
at `data/shared_catalog.jsonl`, with source playlists in `data/days/`. The run-context file and proposal schema
are prepared by the implementation. Prepare a fixed snapshot of the inputs before starting the
loop so it works from consistent evidence throughout selection and revision.

| File | Purpose |
| --- | --- |
| `catalog.jsonl` | One song per line, with Spotify metadata and cached audio descriptors. The pool for the five shared-catalog selections. |
| `history/YYYY-MM-DD.json` for the preceding 30 days | Published playlists across all DJs, used to exclude recent tracks and understand recent programming. Titles, sequencing, and notes can also help avoid repetitive sets. |
| `context.json` | Run date, timezone-aware timestamp, selected DJ ID, and today's normalized neighborhood readings with status and fetch time. Include any precomputed mood scores if mood scoring remains a separate step. |
| `schemas/playlist_proposal.schema.json` | The required creative proposal structure, generated from the canonical Python proposal model. The host assembles the full daily record. |

The history window is `run_date - 30 days <= playlist date < run_date`, using New York dates.
Only successfully published playlists count toward the repeat rule; pending generations
and placeholder fixture records do not. Missing days are allowed, but the run context should
record which dates were found and whether history is known to be incomplete.

Deterministic setup reads `data/personas.json` and passes only the selected DJ's name, bio,
`agent_instructions`, and `speaking_voice` directly in the agent's prompt. The agent does not receive
the full personas file. The selected DJ ID remains in the run context for traceability.

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
| Playlist validation | Accept a proposed daily playlist and check exactly 10 tracks in positions 1–10; five catalog selections and five verified wildcards with consistent flags; no duplicate tracks or primary artists; no tracks played in the preceding 30 days; catalog membership; wildcard IDs and metadata against retained search results; required fields, Spotify URLs, canonical metadata, and mood ranges. Return `valid` and violations with stable codes, affected positions where applicable, and actionable messages. |

`PlaylistValidator(context).validate_playlist(proposal)` wraps the reusable validation function
and uses the run's fixed catalog, published history, and retained Spotify search results; the agent cannot override those reference inputs through tool arguments. The agent
revises invalid selections and validates again before submitting its final output. Subjective
musical fit remains the agent's responsibility.

Keep validation logic in a reusable Python module. Deterministic code runs the same validation
again before accepting the final daily file. Spotify playlist creation and publishing remain
outside the agent loop and are not exposed as agent tools.

## Expected output

### Daily playlist file

We'll publish the daily playlist JSON schema at `schemas/daily_playlist.schema.json` in the
agent's filesystem so it can consult the required output structure while generating the file.

The agent loop's output is `data/days/YYYY-MM-DD.json`, written before any Spotify playlist
is created. It contains everything required to publish the playlist and display the day:

- Playlist name (`title`), Spotify description, and liner notes as a list of paragraphs.
- The selected DJ ID and run date.
- Exactly 10 ordered tracks, each with a verified Spotify ID and URL, title, artist, duration,
  one-line reason, and `wildcard` flag. Positions run from 1 through 10.
- Playlist mood: `tempo_bpm` (60–160), `energy`, `brightness`, and `obscurity` (each 0–10).
- An effect line and optional weight for each input, grounded in its reading and status.
  Skipped inputs must not be presented as having influenced the selections.

The file uses the existing `Day` fields from `src/the_bot_radio/web/models.py` as its starting
point, with publication state and description added. The strict generation schema supports a
generated day without a Spotify playlist ID; legacy website models remain compatible:

| Field | Content |
| --- | --- |
| `date`, `dj` | Run date and selected persona ID |
| `title` | Name to use when creating the Spotify playlist |
| `description` | Description to use when creating the Spotify playlist |
| `liner_notes` | Liner-note paragraphs for the website |
| `inputs` | Display readings with effect, weight, status, and raw payload |
| `mood` | Tempo, energy, brightness, and obscurity scores |
| `tracks` | The 10 verified tracks in their intended playback order; Spotify URIs can be derived from their IDs |
| `publication_status` | `pending` when generation completes; managed by deterministic publishing code afterward |
| `spotify_playlist_id` | `null` until deterministic publishing code records the ID returned by Spotify |
| `prompt`, `model` | Generation prompt and model identifier |
| `generated_at` | Timezone-aware generation timestamp |

Python validates the file against the selection rules and canonical track metadata before
it is accepted as the loop's completed output. Track count, mood ranges, and musical rules
must be enforced explicitly; the current `Day` schema does not enforce them.

### Deterministic publishing outside the loop

A separate publisher reads the validated daily file, creates the Spotify playlist using
`title` and `description`, and adds the tracks in position order. Visibility and other
publishing settings come from deterministic job configuration.

The publisher records the returned Spotify playlist ID and publication status in the daily
file. The website should display only published days, so a pending file is not mistaken for
a playable playlist. The website filters pending days while keeping legacy fixture rendering compatible.

Play history is updated only after successful publication. The agent transcript and verified
wildcard promotions are supporting artifacts; their formats, enrichment, and publication
recovery behavior remain to be specified.

## Implementation Details

Use the Python `claude-agent-sdk` package to run one agent task per day. Keep SDK
orchestration and the playlist system prompt in `generation/agent.py` (`PlaylistAgent`).
`ClaudeService` owns standalone Anthropic requests such as mood scoring.

1. **Prepare:** deterministic setup collects readings, selects the DJ, loads published
   history, and creates the fixed input snapshot and schema file. Put only the selected
   persona in the prompt. Initially keep `score_mood()` as a separate call and include its
   result in the run context.
2. **Configure:** use one `ClaudeSDKClient` session and one query for the daily task,
   retaining the same evidence and tool state throughout it. Set the working directory, model, prompt,
   maximum turns, and cost budget. Explicitly select `Read`, `Grep`, and `Glob` as built-in
   tools and register the two custom tools. `allowed_tools` controls automatic approval,
   not tool availability; configure permissions and file access explicitly because `cwd`
   alone does not isolate the filesystem.
3. **Wire tools:** define Spotify search and playlist validation with `@tool` and register
   them through `create_sdk_mcp_server()`. This is in-process MCP, with no hosted endpoint
   required. Search wraps `SpotifyService`; both tools share run state containing the fixed
   reference inputs and canonical search results.
4. **Run:** the agent reads evidence, chooses tracks, verifies wildcards, and calls validation.
   It uses returned violations to revise and revalidate its proposal within the run limits.
5. **Accept:** pass the generated schema through SDK `output_format` and obtain
   `ResultMessage.structured_output`. Treat missing structured output or an unsuccessful
   result as failure before capturing any proposal. Capture schema-valid output in
   `proposal.json`, then enforce resource limits and playlist rules. Return the accepted
   proposal with the archived system prompt and initial user message. The daily job supplies
   trusted run metadata once and validates the assembled record using the same Python validator
   exposed by the tool.
   Schema validation does not replace checks against catalog and history.
6. **Save:** only after validation passes, Python atomically writes the daily JSON with
   publication status `pending`. If final validation fails, fail without a follow-up query or
   a daily file. Retain schema-valid rejected proposals for inspection, record validation failures in the
   transcript, and mark the run as failed in diagnostics.
   Spotify publication remains a separate deterministic step.

Use the validation tool for feedback during generation and the mandatory Python check for
enforcement before saving. A `PreToolUse` hook could reject an agent `Write` operation, but
cannot intercept the host Python write. Since the agent returns structured output and has
no file-writing tool, a write-validation hook is unnecessary. The final check also catches
changes made after a successful validation tool call.

Proposed module responsibilities:

| Module | Responsibility |
| --- | --- |
| `generation/daily.py` | Prepare the run, invoke the agent, assemble and validate the record once with trusted metadata, and save it |
| `generation/agent.py` | `PlaylistAgent`, playlist system prompt, SDK options and streaming, single-result usage and validation, and frozen `PlaylistAgentResult` carrying the proposal and archived prompt |
| `generation/models.py` | Pydantic proposal/output models and generated JSON schema |
| `generation/validation.py` | Reusable deterministic checks, `PlaylistValidator` binding those checks to a run context, and mandatory validation during host record assembly |
| `generation/tools.py` | SDK tool adapters and shared run state |
| `services/claude_service.py` | Standalone Anthropic requests and their prompts, including mood scoring |
| `services/spotify_service.py` | Spotify API requests used by search and the separate publisher |

Paths are relative to `src/the_bot_radio/`. Test validation independently, tool adapters with
fake dependencies, and orchestration with simulated SDK results before running live generation.

Construct `PlaylistValidator` with the prepared validation context, then inject it through
`PlaylistAgent(spotify_service, validator)`. The agent uses the same validator for tool feedback
and final proposal checks; `run()` no longer takes a context argument. Spotify search retains
evidence in the validator's context. The daily job accepts a separate injectable `playlist_agent`
and retains mandatory host-side validation in `assemble_daily_record()`. Tests may also inject
an `agent_client_factory`. Keep `run()` responsible for options, the deadline, client lifetime,
and returning the result. `_build_options()` configures
tools, permissions, the filesystem guard, structured output, and SDK limits.
`_receive_response()` sends the initial prompt, logs messages, enforces streamed turn limits,
and collects the terminal result. `_process_response()` records usage, checks SDK success,
captures schema-valid proposals, and enforces limits and selection rules. Counters and streamed
message IDs stay local to each invocation. Timeout cleanup follows the SDK client context;
no conversation-state class, follow-up prompt, or cross-response accounting is needed.

SDK references: [Python API](https://code.claude.com/docs/en/agent-sdk/python),
[custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools),
[structured output](https://code.claude.com/docs/en/agent-sdk/structured-outputs),
[permissions](https://code.claude.com/docs/en/agent-sdk/permissions), and
[hooks](https://code.claude.com/docs/en/agent-sdk/hooks).

## Development Plan

Build incrementally. Each phase must pass its validation gate before dependent phases begin.
Spotify publishing and deployment remain separate work.

| Phase | Build | Validate before completion | Dependencies / parallel work |
| --- | --- | --- | --- |
| **1. Output contract** | Define Pydantic proposal and daily-record models; generate the filesystem schema. Support track URLs and pending publication. Update website loading to exclude pending days while retaining existing fixture compatibility. | Round-trip a valid 10-track record; reject invalid structure and mood ranges; confirm pending days stay off the website and existing published pages still render. | First phase; establishes interfaces for subsequent work. |
| **2A. Run preparation** | Assemble a fixed catalog/history snapshot, neighborhood context, separate mood score, and selected-persona prompt. Record history coverage and preserve reading freshness. | With fixtures, verify the 30-day boundaries, published-only history, skipped/stale inputs, and exposure of only the selected persona. | After Phase 1; parallel with **2B and 2C**. |
| **2B. Deterministic validator** | Implement reusable checks against catalog, history, and verified search results, returning structured violations. | Cover valid output, incorrect count/split, duplicate tracks/primary artists, recent repeats, unknown IDs, metadata mismatches, and missing fields. | After Phase 1; parallel with **2A and 2C**. |
| **2C. Spotify search adapter** | Wrap the existing service, return canonical metadata and URLs, and retain results in per-run state. | Mock matches, alternate versions, empty results, API failures, and isolation between runs. Confirm search makes no playlist changes. | After Phase 1; parallel with **2A and 2B**. |
| **3. SDK harness** | Add the Claude Agent SDK; register search and validation through in-process MCP. Configure read/search tools, permissions, structured output, transcript capture, turn limits, and budget. | Exercise adapters with fake dependencies and orchestration with simulated SDK events. Confirm validation feedback reaches the agent, required tools are exposed, and writes/publishing are unavailable. | Requires **2A–2C**. |
| **4. Final acceptance and saving** | Parse and capture schema-valid output from a successful SDK result, enforce limits and playlist rules, attach trusted metadata once, rerun validation, and atomically save pending daily JSON. Invalid final output fails after one query. | Cover changed output after successful tool validation, rejected proposal retention, malformed or missing output, exhausted limits, SDK errors, timeout cleanup, independent repeated runs, and interrupted saving. Failures must not replace an accepted daily file. | After Phase 3. |
| **5. End-to-end qualification** | Add a local generation command and run the complete harness against a real catalog and published history. | Run offline integration tests first, then an opt-in live generation. Verify 10 tracks, the 5/5 split, wildcard search evidence, exclusion rules, metadata, transcript, and schema conformance. Confirm no Spotify playlist is created. | After Phase 4. |

Phases **2A–2C** should agree on shared interfaces from Phase 1 before parallel implementation.
Merge their work and run the combined checks before starting Phase 3.

Use anyio for async tests, respx for mocked HTTP, and fake SDK dependencies for offline tests.
Run relevant tests and Ruff at each phase; run the full offline suite before live qualification.
Real personas and a populated catalog are prerequisites for assessing musical quality.


### Implementation status and qualification

Phases 1–5 are implemented, with offline integration coverage and an opt-in live
qualification test. Live qualification passed on 2026-10-27: the real catalog and selected
resident produced a pending 10-track record after Spotify search and validation feedback.
The agent used 15 turns and an estimated $0.45, excluding separate mood scoring; no Spotify
playlist was created. Initial history was incomplete and recorded as such. Generation uses strict proposal and daily-record models, separate generated
schemas, and a host-owned atomic write. The validator checks primary artists only and requires
wildcards to be outside the catalog. Explicitly published strict daily records form repeat history;
legacy fixtures remain visible on the website but are excluded from generation history.

The CLI is `uv run the-bot-radio-generate`. Defaults are 40 agent turns, a $5 estimated agent
budget, and a 600-second agent-session deadline for a single generation task. Mood scoring
is separate; its model/token usage is recorded independently. Record the single SDK result's
cost and turn count directly; streamed assistant message IDs are deduplicated per invocation
for turn-limit enforcement. The agent may revise after tool feedback during its task, but
the host never sends a repair prompt after final validation fails.
Inputs and schemas are snapshotted under `data/runs/<date>/<run-id>/workspace/`; host-only `proposal.json`,
`transcript.jsonl`, `diagnostics.json`, and `search_results.json` remain outside that readable
workspace. Diagnostics combines raw inputs, mood usage, and the run outcome. The workspace
contains `context.json`, `catalog.jsonl`, `history/`, and `schemas/`; there is no setup directory
or copied personas file. The annotated full layout is documented at the top of
`src/the_bot_radio/generation/daily.py`.

The agent workspace contains only the proposal schema. The full daily schema remains
a host-side contract and is not supplied to the agent.
Python supplies date, DJ, mood, readings, prompt, model, timestamp, and publication state.
The file-access hook restricts reads/searches to the snapshot; playlist validation remains a tool
plus a mandatory host check. No playlist-writing or Spotify-publishing tool is exposed.
