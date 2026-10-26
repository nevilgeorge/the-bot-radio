# G train (`g_train`)

**Endpoint:** `GET https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json`
This is the MTA subway service-alerts feed: GTFS-realtime encoded as JSON, with Mercury extensions. It is about 1 MB and holds about 190 alerts.

**Auth:** none. Checked on 2026-10-26: both the alerts JSON and `nyct/gtfs-g` (protobuf) return 200 without an `x-api-key`.
**Rate limits:** none published. We call it once a day.
**Cadence:** alerts are pushed as they happen. `header.timestamp` is the time of the **last alert change**: it equals the newest `updated_at` plus 1 s. It is **not** the time the feed was generated.

## Filtering
We keep an entity when both of these hold:
- one of its `informed_entity` entries has `route_id == "G"`
- one of its `active_period`s contains `ctx.now`. The rule is `start <= now < end`. A missing `end` means the alert is open-ended, which is normal for real-time alerts.

Planned work lists many periods, such as "Nov 2–6, 9:45 PM–5 AM". We report the one active now.

## alert_type → category (`transit_realtime.mercury_alert.alert_type`)
| category | types | effect |
|---|---|---|
| suspended | `Suspended`, `Part Suspended` | real time |
| delays | `Delays`, `Severe Delays`, `Some Delays`, `Expect Delays` | real time |
| other | anything else that is unplanned: `Reduced Service`, `Reroute`, `Stops Skipped`, … | real time |
| planned | every type starting `Planned - ` (`Planned - Part Suspended`, `Planned - Stops Skipped`, …) | planned work |
| info | `Station Notice`, `Boarding Change`, `Extra Service`, `Special Notice` | listed in `data.alerts`, but `value` still reads "Good service" (same as the MTA app) |

Severity order is suspended > delays > other > planned > info. The most severe alert sets the value.

## Direction
- `informed_entity.direction_id` on the G: `0` is Court Sq-bound (**northbound**) and `1` is Church Av-bound (**southbound**). We confirmed this against live alerts reading "Church Av-bound [G]", which carry `direction_id: 1`.
- If there are no direction ids, we use the header text. "both directions" gives `both`. "Court Sq-/Queens-/Long Island City-bound" gives northbound. "Church Av-/Brooklyn-bound" gives southbound. Anything else gives `null`.

## value (at most 28 chars)
- No active G alerts, or info alerts only: `Good service`
- delays: `Delays northbound`, `Delays southbound`, `Delays, both directions`, or `Some delays`
- suspended: `Suspended, Court Sq–Hoyts`. The segment comes from "between X and Y" in the header, with each station cut at its first hyphen. If the result is too long we drop the segment, giving `Suspended` or `Part suspended`.
- planned: `Planned work` or `Planned suspension`, plus a timing word: `, weekend` when the active period runs Fri/Sat → Sun/Mon and lasts at least 24 h, or `, overnight` when it starts after 8 PM and lasts at most 12 h.
- other: the alert type in sentence case (`Reduced service`), plus a direction when it fits.

## data
`status` ("good" | "delays" | "suspended" | "planned" | "other"), `delayed`, `suspended`, `planned`, `alerts` (at most 5, sorted by severity), `feed_updated` (ISO).
- Each alert has `type`, `category`, `header`, `direction`, `active_from`, `active_until` (ISO ET, or null).
- `suspended` is also true while a *planned* suspension is active, because the train isn't running either way.
- `raw` holds `header` plus the active G entities, trimmed: English text only, G informed_entity only, and descriptions cut to 500 chars.

## Stale
The reading is `stale` when `header.timestamp` is more than 24 h old. The timestamp only moves when an alert changes, so a short threshold would flag quiet periods by mistake. With about 190 alerts (planned work changes every day), no change in 24 h means the feed is frozen.

## Why alerts only
Alerts are the MTA's own call on "delays", and the feed is JSON. The other way to detect delays is to infer them from trip updates (`nyct/gtfs-g`). That needs protobuf parsing (`gtfs-realtime-bindings`) plus scheduled-vs-actual logic, which is too much for a chip-sized status.
