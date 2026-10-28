# Daily files in S3

Use S3 for durable daily records and run artifacts. Keep the agent workspace local.
Every invocation gets a fresh run prefix, including skipped and failed runs. Assume
one generation job at a time: remove `fcntl` locking and add no distributed lock.

## Layout and configuration

```text
s3://<bucket>/<prefix>/
  days/YYYY-MM-DD.json
  runs/YYYY-MM-DD/<run-id>/
    diagnostics.json
    transcript.jsonl
    proposal.json
    search_results.json
    workspace/
      context.json
      catalog.jsonl
      history/YYYY-MM-DD.json
      schemas/playlist_proposal.schema.json
```

Use a UUID per invocation. Resolve dates using `--date`, then `RUN_DATE`, then today
in `America/New_York`; keep timestamps timezone-aware. Initial diagnostics establish
the S3 prefix. Upload only artifacts that exist, including partial workspaces.

- `BOT_RADIO_S3_BUCKET` enables S3 mode; `BOT_RADIO_S3_PREFIX` is optional and requires a bucket.
- `--data-dir` / `BOT_RADIO_DATA_DIR` retains static inputs and local-mode storage.
- `BOT_RADIO_RUN_DIR` controls S3-mode local staging; default to a writable runs directory.
- Personas, input descriptions, and the source catalog stay local. Archive the generated
  workspace as evidence; preserve the existing selected-persona prompt behavior.
- Keep local mode for development and offline tests.

## Storage interfaces

Add typed local/S3 adapters. Keep playlist validation in generation code.

Python's `Protocol` acts like an interface: a class satisfies it by providing matching
methods, without explicitly inheriting from it; type checkers verify compatibility.

```python
from datetime import date
from typing import Protocol


class DailyRecordStore(Protocol):
    async def read(self, run_date: date) -> bytes | None:
        """Read a day; return None only when it does not exist."""
        ...

    async def create(self, run_date: date, content: bytes) -> str:
        """Create without replacement; return its location or raise RecordExists."""
        ...


class RunArtifactStore(Protocol):
    async def write(
        self,
        run_date: date,
        run_id: str,
        relative_name: str,
        content: bytes,
    ) -> str:
        """Replace one complete artifact snapshot and return its location."""
        ...
```

| Operation | Local implementation | S3 implementation |
| --- | --- | --- |
| Read day | Read `days/<date>.json` | `GetObject` |
| Create day | Atomic creation without replacement | Conditional `PutObject` |
| Write artifact | Atomic file replacement | `PutObject` |
| Returned location | Absolute file path | `s3://bucket/prefix/key` |

Select `LocalDailyRecordStore` / `LocalRunArtifactStore` or their S3 counterparts once
at startup based on `BOT_RADIO_S3_BUCKET`. Inject them into generation and preparation;
use in-memory implementations in tests. Storage failures raise; `RecordExists` explicitly
identifies a create conflict. Validate artifact names as relative paths within the run.

Keep the agent workspace as a local `Path`. A `RunRecorder` owns local diagnostics and
transcript files and checkpoints their bytes through `RunArtifactStore`. It handles
redaction, checkpoint timing, upload ordering, retries, and final flushing. Storage
adapters only handle persistence. Add listing as a separate capability for the website;
generation needs only date-based reads.

**Daily repository:** read bytes by date and create records without replacement.
Return a URI or location type for S3 output, never an S3 URI wrapped in `Path`.
Use it for same-day checks and the preceding 30 historical keys, fetched with bounded
concurrency and returned chronologically. Preserve published-only history, schema/date
validation, and missing/excluded coverage. Only confirmed object absence counts as missing;
permissions, network failures, and malformed published records fail setup.

For each included history record, validate the downloaded bytes and check that its date
matches the requested key, then write those same bytes to `workspace/history/<date>.json`.
Reuse the parsed model for track exclusions and DJ selection. This avoids serializing
the model back to JSON while preserving the original validated file; deserialization
and validation are still required. Apply this behavior to both local and S3 storage.

Create canonical records with `PutObject(IfNoneMatch="*")`. On a precondition failure,
read and validate the existing record. Reconcile ambiguous upload outcomes by reading
and comparing the destination with the intended record; never retry unconditionally.

**Artifact writer:** keep local atomic JSON writes and redacted transcript appends,
then upload snapshots. Upload diagnostics on changes and transcripts every 15 seconds
(configurable), at milestones, and on exit. Use one ordered upload worker, snapshot bytes
before enqueueing, and coalesce superseded snapshots to bound memory and prevent stale
uploads overwriting newer ones. General-purpose S3 does not support local-style append.

Use bounded retries and provide upload-only recovery from an existing local run directory,
preserving its date and run ID. If using boto3, add it with `uv add`, follow the AWS Python
SDK skill, inject the client for tests, and keep blocking calls off the async event loop.
The agent receives no S3 tools or credentials and retains its workspace access boundary.

## Invocation lifecycle

1. Resolve configuration, date, timestamp, and a new run ID. Create the local run directory,
   append `started`, and write diagnostics with `status: running`, date, run ID, model,
   and canonical daily location.
2. Upload initial artifacts before paid setup. If S3 is unavailable, stop and report
   local artifacts; a remote prefix cannot be guaranteed during an outage.
3. Read the canonical daily record. A valid record produces `status: skipped` with
   `reason: existing_daily_record`; flush artifacts and return success. An invalid record
   fails without replacement. A missing record proceeds.
4. Load history, build the local workspace, collect readings, score mood, and run one
   agent task with the existing validator. Checkpoint artifacts throughout.
5. Save the proposal and search evidence, then upload available artifacts and workspace.
   Assemble and validate the final daily record at the host boundary and conditionally
   save it with `publication_status: pending`.
6. Mark diagnostics `accepted`, append the terminal event, and flush. Print the canonical
   S3 URI, or the local path in local mode. Record the committed ETag when available.

## Failure handling

The canonical daily object determines acceptance. S3 writes are atomic per object;
there is no transaction across the daily record and artifacts.

- Before commit, exhausted artifact-upload retries prevent acceptance. Retain local files.
- After commit, a final upload failure returns nonzero and reports both the committed URI
  and local artifacts. Recover with upload-only retries; do not regenerate the day.
- A rerun creates its own prefix and records a skip; it does not repair earlier runs.
- Preserve the primary exception if failure-path uploads also fail; report both safely.
- Use one finalization path for skips, setup errors, agent errors, cancellation, and commit
  failures, with bounded upload cleanup time.
- Hard termination may lose events since the last checkpoint. Diagnostics left `running`
  indicate an unfinished run, not acceptance. Ephemeral staging may disappear with the worker.

## Consumers

`web/store.py` supports local filename/mtime reloads and an injected S3 daily snapshot.
The S3 cache paginates only `days/`, fetches changed objects, and refreshes periodically.
Store methods read memory without making S3 requests. Replacements at the same key,
including pending-to-published updates, are detected. Infrastructure failures retain the
entire last successful snapshot and are logged.

Keep existing display rules: hide pending records, skip invalid files, and support local
legacy fixtures. Generation history remains stricter. Adapt standalone
`DjRollService.fetch()` if it needs S3 history; generation already calls `roll()` with
validated history. A future publisher must update the canonical key using an ETag
precondition. Publishing, scheduling, and compute deployment are outside this plan.

## Security and cost

Use the standard AWS credential/region chain and execution roles on AWS. Keep the bucket
private with Block Public Access, disabled ACLs, encryption at rest, and HTTPS. Scope
generation access to required prefixes without delete permissions; deny website access
to run artifacts. Preserve transcript redaction.

Start with S3 Standard. Checkpoints add PUT requests; workspace copies and object versions
add storage. Set artifact retention separately from canonical days, including noncurrent
versions if versioning is enabled.

## Development phases and parallel work

Phases 1–4 deliver a working local pipeline behind the storage interfaces. No bucket is
required. Phase 5 adds S3 with stubbed clients; phase 7 connects the separately provisioned
bucket. Keep each phase reviewable with its own tests.

All seven phases are complete within their stated scope. Completion was rechecked against
implementation and tests: local storage contracts, history preparation, run recording, daily
orchestration, S3 adapters/configuration, consumers, and isolated staging qualification.
The verification results and retained staging locations are recorded under Phase 7.
Production deployment, publishing, scheduling, migration, IAM provisioning, and retention
changes remain outside these phases.

### 1. Contracts and local adapters

status: complete

Define the protocols, location/error contracts, and local implementations in
`generation/storage.py`. Test missing vs. failed reads, atomic no-overwrite creation,
artifact replacement, and path containment. Agree on these APIs before parallel work.

### 2. History reads

status: complete

Inject `DailyRecordStore` into `preparation.py`, defaulting to local storage. Fetch the
preceding 30 dates with bounded concurrency; validate once and copy original bytes into
the workspace. Expose a diagnostics callback for the recorder. Test incomplete/empty
history, published-only filtering, ordering, date/schema errors, and byte preservation.

### 3. Run recorder

status: complete

Implement `RunRecorder` and artifact checkpoints in `runtime.py` using
`RunArtifactStore`. Own diagnostics updates, transcript redaction, ordered uploads,
bounded retries, final flushing, and upload-only recovery. Test stale-upload prevention,
partial workspace archival, cancellation, and upload failures independently of generation.

### 4. Daily orchestration

status: complete

After phases 2 and 3, wire both stores and the recorder into `daily.py`. Create runs before
existence checks, remove locking, and preserve validation and no-overwrite semantics.
Handle skipped runs, create conflicts, ambiguous commits, and post-commit artifact failures.
Print locations without assuming `Path`; update callers and tests accordingly. Verify two
sequential invocations create distinct run folders while only one calls the agent.

This is the first milestone: local generation works end to end through injected stores,
including an in-memory store that proves generation no longer depends on daily file paths.

### 5. S3 adapters and configuration

status: complete

Implement the same protocols in a separate S3 module with an injected SDK client.
Add bucket/prefix configuration and select adapters at startup. Test key construction,
missing vs. service errors, conditional writes, ambiguous outcomes, and nonblocking SDK
calls using stubs. Keep local mode as the default. No live bucket is needed for this phase.

The injected S3 adapters, shared configuration,
conditional canonical creation, commit reconciliation, recorder recovery, and drained
worker/client ownership are implemented. Offline SDK tests use Stubber with explicit dummy
credentials and region. The full offline suite and Ruff checks below include Phase 5.

### 6. Consumers

status: complete

Verified offline and against isolated staging data.

#### Listing and cached daily records

Keep `DailyRecordStore` and `RunArtifactStore` unchanged. The separate typed `DailyListing`
capability returns `DailyObjectDescriptor` values with date, key, ETag, last-modified time,
and size. `S3DailyListing` uses the SDK `ListObjectsV2` paginator and restricts requests to
`<prefix>/days/`. Accept only direct, calendar-valid ASCII `YYYY-MM-DD.json` keys; ignore
nested paths, directory markers, invalid dates, and unrelated files.

`S3DailyCache` injects the listing and date-based reader. Each refresh lists all pages,
compares every descriptor with the previous successful snapshot, and downloads only new
or changed objects with at most four concurrent reads. Retain descriptor state for hidden
pending and invalid objects so unchanged objects are not repeatedly fetched or logged.
Changed metadata triggers revalidation. Validate using the website `Day` model, require
payload dates to match key dates, hide pending records, require playlist IDs for explicitly
published records, and support legacy records. Generation history keeps its stricter rules.

Assemble separately, then atomically swap the entire completed snapshot. Successful listings
remove deleted keys; `NoSuchKey` between listing and download removes that entry. Invalid
payloads are logged and excluded, including invalid replacements of formerly visible records.
Any other listing, access, transport, or body-read error rejects the entire refresh and retains
the previous successful state. Drain every download on failure or cancellation before releasing
client ownership. Refreshes serialize, including manually triggered refreshes.

#### Website lifecycle and configuration

Preserve the synchronous `Store` API. An optional `DailySnapshotSource` supplies memory-only
daily views; no store method or route performs S3 requests. Personas and input descriptions
retain their local filename/mtime loading behavior.

`create_app()` owns FastAPI lifespan, with `app` preserved at `web.main:app`. Resolve bucket,
prefix, and local data configuration after dotenv loading, sharing
`GenerationStorageConfig.from_environment()` and `create_s3_client()` with generation while
preserving generation's public entry points. Local mode is the default and initializes no AWS
resources. S3 startup constructs one SDK client off-loop and loads the first complete snapshot
before starting one background task. Initial infrastructure failure fails startup and closes
the client. An empty prefix, or only hidden/invalid records, is a successful empty load.

Wait **300 seconds after each preceding attempt completes**, with no overlap. Tests can inject
the interval without another environment variable. Log failures and subsequent recovery.
`/healthz` retains its existing response contract and reports the latest cached visible day,
even when stale. Shutdown stops the refresh task, drains SDK workers, then closes the client
off-loop. Never close while a request is still running. Resolve the store through application
state; tests explicitly inject local configuration or fake S3 dependencies.

#### Standalone DJ history

`DjRollService(daily_reader=...)` optionally reads yesterday remotely. The caller owns the
reader's client and lifetime. Existing callers keep local history. Preserve standalone rules:
yesterday's string `dj` supplies the exclusion; missing or malformed payloads mean no exclusion.
Remote access/transport failures propagate so the input runner marks the reading skipped;
never silently fall back to local history. Generation's published-only validated history and
direct `roll()` invocation remain unchanged.

#### Acceptance and verification

Offline coverage includes pagination, empty listings, direct-key filtering, all descriptor
change fields, unchanged hidden/invalid reuse, four-download concurrency, deletion and download
races, invalid replacements, publication transitions, atomic visibility, complete retention on
failed refresh, and successful recovery. Lifecycle tests cover initial failure, successful
empty startup, post-attempt timing, nonoverlap, responsive event loop, cancellation draining,
and client ownership including cancelled construction. Consumer tests cover all store views,
routes, local hot reload/static inputs, DJ injection/error handling, and no artifact requests.
Ruff check, format check, and the full offline suite are required before acceptance.

### 7. Integration and bucket connection

status: complete

Isolated staging qualification passed. Production deployment,
publishing, scheduling, migration, IAM provisioning, and retention changes remain outside
Phases 6 and 7. No website deployment or production configuration switch was performed.

#### Opt-in qualification

The dedicated `s3_live` marker is separate from provider `live` tests. Default collection
remains entirely offline and skips both unless `RUN_LIVE=1`. Run only storage qualification:

```bash
export BOT_RADIO_S3_BUCKET=the-bot-radio-staging-data
export AWS_DEFAULT_REGION=us-east-1
RUN_LIVE=1 uv run pytest -m s3_live -s
```

Require an explicitly configured bucket and normal AWS credential/region resolution.
`the-bot-radio-staging-data` / `us-east-1` is the intended staging configuration, never an
application default. An optional configured prefix precedes `qualification/<uuid>/`.
Each test receives a fresh UUID namespace; every test write stays underneath it and never
operates on existing canonical records. Print the unique prefix and retained S3 location
before client construction and in teardown, including on failure. Retain every object and
version; do not request or use deletion permissions. Existing bucket lifecycle policies are
not changed by qualification.

Reuse deterministic catalog, personas, readings, mood, and playlist-agent fixtures. Exercise
real S3 adapters through generation, proving exact committed bytes, validated published history
copied unchanged into the workspace and artifacts, pending-history exclusion, accepted output,
audited skip on rerun, and distinct run prefixes without another agent/provider call.

Exercise the real cache and application locally against that namespace. Pending records remain
hidden. Replace a test record with a published fixture, explicitly refresh, and verify routes
and health show it. This is fixture setup, not a publisher. Inject precommit and postcommit
artifact failures around real writes, then recover retained artifacts under the original date
and run ID; verify canonical bytes/absence remain unchanged and providers are not called again.
Make no live Anthropic, Spotify, or neighborhood-input calls.

#### Configuration and permissions

Website credentials require `s3:GetObject` on `<prefix>/days/*` and `s3:ListBucket` constrained
with `s3:prefix` to `<prefix>/days/`. Website credentials must have no artifact access or writes.
Generation separately requires daily reads/conditional writes and artifact writes, with listing
access needed to distinguish missing from denied daily objects. Qualification requires daily
and artifact read/write access and necessary listing only inside its unique namespace; no
deletion is needed. These are documented permissions, not IAM provisioning by these phases.

Use existing authorized credentials. If access is unavailable, record qualification as blocked
with the failing operation, without provisioning credentials, modifying IAM, or claiming a pass.
SDK support for existing AWS login profiles is installed through `boto3[crt]`. Keep credentials
and provider payloads out of evidence. README, `.env.example`, module documentation, project
conventions, and deployment guidance cover optional S3 reads, startup failure, stale snapshots,
local static inputs, and retained qualification data.

#### Qualification evidence — 2026-10-28

- Revision: `9e30732f4d582e65088ebd4b0a9fc10bf680c85b` plus the uncommitted Phase 5–7
  implementation in this working tree (no commit was created by qualification).
- UTC evidence recorded after qualification: `2026-10-28T12:29:42Z`.
- Command: `env RUN_LIVE=1 BOT_RADIO_S3_BUCKET=the-bot-radio-staging-data AWS_DEFAULT_REGION=us-east-1 uv run pytest -m s3_live -s --tb=short`.
- Bucket: `the-bot-radio-staging-data`; resolved region: `us-east-1`; configured prefix: empty.
- Result: **3 passed, 662 deselected in 7.94 seconds**. No provider calls, deletion, IAM changes,
  publishing, deployment, or production configuration changes.

| Scenario | Retained staging prefix under `s3://the-bot-radio-staging-data/` | Result |
| --- | --- | --- |
| Generation bytes, history, audited rerun, website/cache transitions | `qualification/8cc90f7ddfc64fa4a92750be1782594b/` | Passed |
| Precommit artifact failure and upload-only recovery | `qualification/192c646df53f4a54b1bf071047f8a85f/` | Passed |
| Postcommit artifact failure and upload-only recovery | `qualification/bc03754f3ed940918c29682090f8e497/` | Passed |

The first attempt failed during client construction with `MissingDependencyException` for
existing login-profile CRT support, before any S3 request. Installing the SDK CRT extra fixed
that dependency; no credentials were provisioned or changed. Its printed but unused prefixes
were `qualification/b75da0bfcb73437d9b0170b499237622/`,
`qualification/658c685776554418b8b134a12d9811a6/`, and
`qualification/0421fa0ed8b945249495056c5250755f/`.

Final offline verification recorded at `2026-10-28T12:33:23+00:00`:
`uv run ruff check .` passed, `uv run ruff format --check .` passed (80 files), and
`uv run pytest -q` passed with **653 passed, 13 skipped in 18.12 seconds**.
`git diff --check` also passed. The live result above predates only the final client protocol
annotation and startup-error preservation refinement, both covered by the final offline suite.

Remaining limitations: this qualifies storage and local application behavior with fake
providers, not live provider quality, publisher correctness, scheduling, deployment, migration,
or production permissions. The suite uses existing staging credentials; a separate website-only
IAM role was not provisioned or tested. Recovery still depends on retained local run artifacts.
Objects and versions consume storage under existing bucket lifecycle rules.

### Subagent ownership and dependencies

| Workstream | Starts after | Owns |
| --- | --- | --- |
| Contracts agent | Plan agreed | `generation/storage.py`, `test_storage.py` |
| History agent | Phase 1 | `preparation.py`, `test_preparation.py` |
| Recorder agent | Phase 1 | `runtime.py`, `test_runtime.py` and recorder tests |
| S3 adapter agent | Phase 1 | Separate S3 module and adapter tests; bucket-independent |
| Listing/cache agent | Phase 5; listing/snapshot interfaces fixed | `s3_listing.py`, `daily_cache.py`, cache tests |
| Standalone DJ agent | Reader interface fixed | `dj_roll.py`, standalone DJ tests and input docs |
| Qualification agent | Phase 5 and consumer APIs fixed | Isolated `s3_live` scenarios using fake providers |
| Integrating agent | Phases 2 and 3; consumer interfaces fixed | Shared configuration, store integration, application lifecycle, documentation, final verification |

Listing/cache and standalone DJ work may proceed in parallel once interfaces are fixed.
One integrating agent owns shared configuration, application lifecycle, documentation, and
final verification. Other workstreams do not independently edit shared files. The qualification
agent can prepare tests alongside consumer work once the factory and cache APIs are agreed;
the integrator runs staging qualification after offline checks. Integrate changes and run
cross-module checks before declaring either phase complete.

## References

- [ListObjectsV2 paginator](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/paginator/ListObjectsV2.html)
- [Conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Consistency and atomicity](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html)
- [Directory-bucket append support](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-objects-append.html)
- [Security best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)
- [Pricing](https://aws.amazon.com/s3/pricing/)
