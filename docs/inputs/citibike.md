# citibike — Citi Bikes · Greenpoint

Bikes available summed over **every Citi Bike dock in our ZIP code** (`Settings.zip_code`, env
`BOT_RADIO_ZIP`, default `11222`).

**Source:** Citi Bike NYC GBFS, run by Lyft. Public, no key, no documented rate limit. The feeds
are static JSON, regenerated about every 30–60 s (`ttl: 60`).

- Autodiscovery: `https://gbfs.citibikenyc.com/gbfs/gbfs.json` (as of 2026-10 this is still
  **v1.1**). Its `gbfs_versions` feed lists v1.1 and **v2.3**.
- We use v2.3 directly, with no autodiscovery call each run:
  - `https://gbfs.lyft.com/gbfs/2.3/bkn/en/station_information.json` (~0.8 MB): name, lat/lon,
    capacity
  - `https://gbfs.lyft.com/gbfs/2.3/bkn/en/station_status.json` (~1.1 MB): bikes, docks,
    `is_installed`, `is_renting`, `last_reported`
  Both are fetched concurrently. If the URLs ever move, start again from the autodiscovery feed.

## Which stations count

GBFS has no ZIP field. The ZIP's boundary is bundled as
`src/the_bot_radio/services/inputs/geo/<zip>.json`: the 11222 MultiPolygon from NYC Open Data's
Modified ZIP Code Tabulation Areas (MODZCTA, dataset `pri4-ifjk`, fetched 2026-10-26;
`https://data.cityofnewyork.us/resource/pri4-ifjk.geojson?modzcta=11222`). Each run, every
station in `station_information` is tested against it with a ray-casting point-in-polygon check, so
new or removed docks are picked up automatically. Changing the ZIP needs a matching geo file (the
service raises without one).

On 2026-10-26 that matched **40 stations**: 38 renting, plus Clay St & Commercial St and West St &
Freeman St (not installed). Total capacity of the renting ones was 1283. The boundary includes a
few docks some would call East Williamsburg (e.g. Morgan Ave & Lombardy St) because they're inside
11222.

Capacity comes from `station_information`, not `bikes + docks + disabled` from status: that sum
disagreed with the published capacity for about a third of stations.

## Output

- `value`: `"{bikes} of {capacity} docked"`, summed over the counted stations, e.g.
  `"537 of 1283 docked"`. `bikes` is `num_bikes_available` (classic + e-bikes).
- `data`: `zip`, `bikes`, `ebikes`, `docks`, `capacity`, `station_count`, `stale_stations`,
  `offline_stations` (names), `last_reported` (newest, ISO America/New_York), and `stations`
  (`station_id`, `name`, `bikes`, `capacity` per counted station).
- `raw`: the station IDs matched inside the ZIP.

## Status rules

- Stations that are not installed or not renting are left out (listed in `offline_stations`).
- A station whose `last_reported` is more than 60 minutes before `ctx.now` is left out of the sum
  and listed in `stale_stations`. Status stays `"ok"` while any station is fresh.
- If **every** renting station is stale, the totals fall back to those stale stations and the
  status is `"stale"`.
- **Errors** (the runner then skips this input): an HTTP error, a malformed payload or station
  record, no geo file for the ZIP, no stations inside the ZIP, or no renting stations.
