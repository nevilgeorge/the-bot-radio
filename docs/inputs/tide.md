# tide — Newtown Creek tide

Module: `src/the_bot_radio/services/inputs/tide.py` (`TideService`, key `tide`).

## Source
NOAA CO-OPS Tides & Currents data API. No key, no documented rate limit (one call per run).

```
GET https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
    ?product=predictions&interval=hilo&datum=MLLW&time_zone=lst_ldt&units=english
    &format=json&application=TheBotRadio&station=<id>&begin_date=YYYYMMDD&end_date=YYYYMMDD
```

Only `run_date` is requested (begin = end). Predictions are astronomical, so they're
available years ahead and never go stale; `status` is always `ok` or the run is skipped.

## Station
Default `STATION = "8517673"` — **Hunters Point, Newtown Creek** (subordinate station,
reference 8518750 The Battery), **1.34 km** from Greenpoint (40.7295, −73.954). It is the
nearest tide-prediction station in the metadata API
(`mdapi/prod/webapi/stations.json?type=tidepredictions`) and returns hilo predictions.
Next nearest: East 41st St 8518695 (2.26 km), Williamsburg Bridge 8518699 (2.32 km),
The Battery 8518750 (6.01 km; ~1 h 20 m earlier than Hunters Point).

Override with `Settings.noaa_tide_station` (env `NOAA_TIDE_STATION`).

## value / data
- `value`: next high tide at/after `ctx.now` — `"High at 9:49 AM"`; if today's highs have
  all passed, the last one — `"High was 10:18 PM"`.
- `data`: `station_id`, `station_name`, `events` (`[{type: "H"|"L", time: ISO local, height_ft}]`,
  sorted), `next_high`, `next_low` (ISO or `None`).
- `raw`: `{station, predictions}` (four-ish rows straight from NOAA).

## Quirks
- `time_zone=lst_ldt` returns naive local wall times ("2026-10-26 09:49"); we attach
  America/New_York.
- Errors come back as `{"error": {"message": ...}}` — sometimes HTTP 200 (e.g. bad datum:
  "No Predictions data was found"), sometimes HTTP 400 (bad station / date). Both raise.
- Empty `predictions` (or no `H` event) raises `ValueError` → the runner marks it skipped.
- Heights are strings in the payload (`"v": "5.324"`), MLLW datum, feet.
