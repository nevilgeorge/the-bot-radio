# dj_roll — The DJ roll

Picks the day's resident DJ. No external API, no auth, no rate limits; runs once per daily run.

## Sources
- `data/personas.json` — list of personas, validated as `the_bot_radio.web.models.Persona`.
- `data/days/<run_date - 1>.json` — yesterday's record; only its `"dj"` field (a persona id) is read.

## Rules
- Seed: `run_date.toordinal()` fed to `random.Random(seed)`. Deterministic across runs and
  processes (Python's `hash()` of a str is randomized per process, so it is not used).
  Re-running a day re-rolls the same DJ as long as personas and yesterday's record are unchanged.
- Yesterday's DJ is excluded when there are ≥ 2 personas. With one persona, they play every day.
- Missing, unparseable, or `dj`-less yesterday file → no exclusion.
- Candidates keep `personas.json` order; reordering personas can change a day's pick.
- No personas (or invalid `personas.json`) → `ValueError` → runner marks the input `skipped`.
  Missing `personas.json` raises `FileNotFoundError` (also `skipped`). Never `stale`.

## Reading
- `value`: persona `name`, e.g. `[Resident 03]`.
- `data`: `persona_id`, `excluded` (list of ids), `candidates` (list of ids), `seed` (int).
- `raw`: empty.
