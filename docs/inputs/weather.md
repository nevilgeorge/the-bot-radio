# Weather · Greenpoint

- **Source:** National Weather Service API, `https://api.weather.gov`. No key. NWS requires a
  `User-Agent` (`ctx.http` sends one), and we also send `Accept: application/geo+json`.
- **Calls (3 per run):**
  1. `GET /points/{lat},{lon}` with `settings.lat/lon` rounded to 4 decimals. For Greenpoint
     (40.7295, -73.954) this resolves to grid `OKX/35,43`. We call it every run instead of
     hard-coding the grid, since there is only one run a day and it has been reliable.
  2. `GET properties.forecastHourly` (`/gridpoints/OKX/35,43/forecast/hourly`) and
  3. `GET properties.forecast` (`/gridpoints/OKX/35,43/forecast`, the 12-hour day/night periods).
     Calls 2 and 3 run concurrently.
- **Observations not used:** `observationStations` lists KNYC (Central Park) first, then KLGA,
  KTEB, KJFK and KEWR. Reading the latest observation would take a fourth call (over the budget).
  Observations also arrive in metric and lag up to about an hour; KNYC's was 40 minutes old when
  checked. The first hourly forecast period gives a good enough "now" for a 06:00 run.
- **Rate limits:** not published. NWS just asks for a reasonable rate, and one run a day is well
  within that.
- **Cadence:** forecasts are regenerated about hourly (`updateTime`). The hourly list starts at
  the current hour.
- **Retries:** a 5xx response is retried once after `RETRY_DELAY` (0.5 s), because gridpoint
  endpoints sometimes return 500/503 briefly. If the retry also fails, `raise_for_status()`
  raises. A 4xx is never retried.

## value / data
- `value`: `"{temp}°F, {conditions}"`, e.g. `"68°F, sunny"` or `"52°F, chance of showers"`.
- **Current period:** the first hourly period whose `endTime` is after `ctx.now`. If none
  qualifies, the last period is used.
- `data`:
  - `temp_f`: from the current period.
  - `conditions`: the shortened condition (see below). `short_forecast` keeps the original text.
  - `precip_chance`: the highest `probabilityOfPrecipitation` (int, 0–100; null counts as 0) over
    the hourly periods that start within 12 h of the current period.
  - `wind_mph`: parsed from `windSpeed`. A range like `"10 to 15 mph"` uses the **upper** bound
    (15), because for a vibe signal it is better to overstate wind than understate it. Text with
    no digits counts as 0.
  - `wind_dir`: the compass string, e.g. `"W"`.
  - `high_f` / `low_f`: the first daytime / nighttime period in the daily forecast whose start
    falls on `ctx.run_date` (ET), i.e. "Today" and "Tonight". Either is `null` if no such period
    exists. For example, after about 18:00 NWS drops "Today", so `high_f` is null.
  - `forecast_period_start`: the current period's start time (ISO).
  - `forecast_updated`: the hourly forecast's `updateTime`.
- `raw`: the grid ids, the 12 trimmed hourly periods used, and the first 2 daily periods.

## Condition shortening (`shorten_condition`)
Steps:
1. Keep only the text before `" then "`.
2. Lowercase it.
3. Apply these phrase rewrites:

   | NWS phrase | becomes |
   |---|---|
   | `Slight Chance X` / `Chance X` | `chance of x` |
   | `(Showers And) Thunderstorms` | `t-storms` |
   | `Rain Showers` | `showers` |
   | `Snow Showers` | `snow` |
   | `Areas Of` / `Patchy` / `Isolated` / `Scattered` | dropped |

4. If the result is still longer than 22 characters, drop qualifiers one at a time until it fits,
   in this order: `likely`, `chance of`, `light`, `heavy`, `very`, `mostly`, `partly`.
5. If it still doesn't fit, keep the last two words.

Examples: `Chance Light Rain` → `chance of light rain`, `Slight Chance Rain Showers` →
`chance of showers`, `Patchy Fog then Mostly Sunny` → `fog`, `Chance Rain And Snow Showers` →
`rain and snow`. More are in `tests/services/test_weather.py::test_shorten_condition`.

## Status rules
- `stale`: the first hourly period's `startTime` is more than 3 h before `ctx.now`, which means
  NWS is serving an old forecast. Data is still returned.
- `skipped` (the service raises) in these cases:
  - HTTP errors after the 5xx retry.
  - `/points` is missing the forecast URLs.
  - A forecast has no periods.
  - A period is missing fields or has an unparseable timestamp (raised as `ValueError`).
