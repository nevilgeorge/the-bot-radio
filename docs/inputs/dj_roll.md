# dj_roll — The DJ roll

Picks the day's resident DJ. Uses local history by default; runs once per daily run.

## Sources
- `src/the_bot_radio/resources/personas.json` — packaged list of personas, validated as
  `the_bot_radio.web.models.Persona`; tests may inject `RunContext.resources_dir`.
- `data/days/<run_date - 1>.json` — yesterday's record; only its `"dj"` field (a persona id) is read.
- Standalone callers may instead pass `DjRollService(daily_reader=...)` with an async
  `read(date) -> bytes | None` reader, such as `S3DailyRecordStore`. The caller owns its
  client and lifetime. Personas always remain local.

## Rules
- Seed: `run_date.toordinal()` fed to `random.Random(seed)`. Deterministic across runs and
  processes (Python's `hash()` of a str is randomized per process, so it is not used).
  Re-running a day re-rolls the same DJ as long as personas and yesterday's record are unchanged.
- Yesterday's DJ is excluded when there are ≥ 2 personas. With one persona, they play every day.
- Missing, unparseable, or `dj`-less yesterday file → no exclusion.
- Injected history follows the same string-`dj` rule, including pending or legacy payloads.
  Missing or malformed remote bytes mean no exclusion. Remote access and transport errors
  propagate so the runner records `skipped`; they never trigger a local history fallback.
- Generation continues to call `roll()` directly with its validated, published-only history.
- Candidates keep `personas.json` order; reordering personas can change a day's pick.
- No personas (or invalid `personas.json`) → `ValueError` → runner marks the input `skipped`.
  Missing `personas.json` raises `FileNotFoundError` (also `skipped`). Never `stale`.

## Reading
- `value`: persona `name`, e.g. `[Resident 03]`.
- `data`: `persona_id`, `excluded` (list of ids), `candidates` (list of ids), `seed` (int).
- `raw`: empty.
