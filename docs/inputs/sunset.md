# Sunset

- **Source:** computed locally with `astral` 3.x (NOAA solar equations) for `settings.lat/lon`
  (default Greenpoint 40.7295, -73.9540) on `ctx.run_date`, timezone America/New_York.
- **Endpoint / auth / rate limits:** none. No network calls, so it can't time out or be rate-limited.
- **Cadence:** deterministic per date; computed fresh on every run.
- **Accuracy:** checked against `https://api.sunrise-sunset.org/json?lat=..&lng=..&date=..&formatted=0`
  (only in tests). For 2026-10-26 astral says sunset 17:59:20 EDT and the API says 18:00:01 EDT,
  so they differ by about 40 s. Tests allow up to 2 minutes. The reference response is recorded in
  `tests/services/fixtures/sunset/sunrise_sunset_org_2026-10-26.json`.

## value / data
- `value`: sunset time rounded to the nearest minute, 12-hour with no leading zero, e.g. `"5:59 PM"`.
- `data`: `sunrise`, `sunset`, `civil_dusk` (sun 6° below the horizon), and `golden_hour_start`
  (sun 6° above the horizon, setting). All are ISO local times with a UTC offset, to the second.
  `day_length_min` is sunset minus sunrise, rounded to whole minutes. `golden_hour_start` is left
  out if astral can't compute it.
- `raw`: the inputs used (`lat`, `lon`, `date`). The data is computed here, so there is no source payload.

## Status rules
- Never `stale`, because it is always computed for the run date.
- `skipped` (the service raises `ValueError`) only when the sun doesn't rise or set that day, as in
  polar day or polar night. That can't happen at Greenpoint.

## Quirks
- The site fixture `data/days/2026-10-26.json` shows `"5:52 PM"`, but the real 2026-10-26
  sunset is about 5:59 PM EDT, and the two sources above agree on that. 5:52/5:53 PM is the
  sunset for **Oct 31**, so the fixture's value is wrong, not the calculation.
- DST: on 2026-11-01 sunset is 4:52 PM EST, versus 5:53 PM EDT on Oct 31. The clock time drops
  by about an hour while the actual moment of sunset moves only about a minute. Times are built
  as aware datetimes, so the UTC offset in `data` changes from -04:00 to -05:00.
