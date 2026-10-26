"""G train service status from the MTA's subway alerts feed (GTFS-realtime as JSON).

Source: https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json
No API key needed (MTA dropped keys for its GTFS-rt feeds in 2023). Each entity carries the
Mercury extension `transit_realtime.mercury_alert.alert_type` ("Delays", "Suspended",
"Planned - Part Suspended", "Station Notice", ...). We keep alerts whose informed_entity names
route "G" and whose active_period contains `ctx.now`, then summarize the most severe one.
"""

import re
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal

from .base import TZ, Reading, RunContext

ALERTS_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"
ROUTE_ID = "G"
MERCURY_ALERT_KEY = "transit_realtime.mercury_alert"
# header.timestamp is the last alert *change* (not generation time), so only a long-frozen
# feed (no update to any of ~200 subway alerts in a day) counts as stale.
STALE_AFTER = timedelta(hours=24)
MAX_ALERTS = 5
MAX_SUMMARY_LEN = 28  # keeps the chip short

Category = Literal["suspended", "delays", "other", "planned", "info"]
Direction = Literal["northbound", "southbound", "both"]

# Real-time types; anything starting "Planned" is planned work.
SUSPENDED_TYPES = {"Suspended", "Part Suspended"}
DELAY_TYPES = {"Delays", "Severe Delays", "Some Delays", "Expect Delays"}
# Informational: listed in data but don't break "Good service" (MTA's app does the same).
INFO_TYPES = {"Station Notice", "Boarding Change", "Extra Service", "Special Notice"}
SEVERITY: dict[Category, int] = {"suspended": 0, "delays": 1, "other": 2, "planned": 3, "info": 4}

# GTFS direction_id on the G: 0 = Court Sq-bound (north), 1 = Church Av-bound (south).
DIRECTION_IDS: dict[int, Direction] = {0: "northbound", 1: "southbound"}
NORTH_WORDS = ("Court Sq-bound", "Queens-bound", "Long Island City-bound", "northbound")
SOUTH_WORDS = ("Church Av-bound", "Brooklyn-bound", "southbound")
BETWEEN = re.compile(r"between (.+?) and (.+?)(?=[.,;]| while| after| because| due|$)")


def category(alert_type: str) -> Category:
    """Map a Mercury alert_type to our severity category."""
    if alert_type.startswith("Planned"):
        return "planned"
    if alert_type in SUSPENDED_TYPES:
        return "suspended"
    if alert_type in DELAY_TYPES:
        return "delays"
    return "info" if alert_type in INFO_TYPES else "other"


def _english_text(translated_string: dict[str, Any] | None) -> str:
    """The English text from a GTFS-rt TranslatedString ('' if there is none)."""
    for translation in (translated_string or {}).get("translation", []):
        if translation.get("language", "en") == "en":
            return translation.get("text", "").strip()
    return ""


def _direction(header: str, route_entities: list[dict[str, Any]]) -> Direction | None:
    """Direction from the entities' direction_id, else from wording in the header."""
    direction_ids = {
        entity["direction_id"] for entity in route_entities if "direction_id" in entity
    }
    if len(direction_ids) == 1 and (direction := DIRECTION_IDS.get(direction_ids.pop())):
        return direction
    if len(direction_ids) > 1 or "both directions" in header:
        return "both"
    mentions_north, mentions_south = (
        any(phrase in header for phrase in phrases) for phrases in (NORTH_WORDS, SOUTH_WORDS)
    )
    if mentions_north and not mentions_south:
        return "northbound"
    if mentions_south and not mentions_north:
        return "southbound"
    return None


def _active_period(periods: list[dict[str, Any]], now_timestamp: float) -> dict[str, Any] | None:
    """The first period containing `now_timestamp`; a missing `end` means open-ended."""
    for period in periods:
        has_started = int(period.get("start", 0)) <= now_timestamp
        has_not_ended = "end" not in period or now_timestamp < int(period["end"])
        if has_started and has_not_ended:
            return period
    return None


def _timestamp_to_iso(unix_timestamp: Any) -> str | None:
    """Unix seconds → New York ISO string; None for a missing/zero timestamp."""
    return datetime.fromtimestamp(int(unix_timestamp), TZ).isoformat() if unix_timestamp else None


def _segment(header: str) -> str | None:
    """'between Court Sq and Hoyts-Schermerhorn Sts' -> 'Court Sq–Hoyts'."""
    if not (between_match := BETWEEN.search(header)):
        return None
    return "–".join(station.split("-")[0].strip() for station in between_match.groups())


def _when(alert: dict[str, Any]) -> str | None:
    """'weekend' or 'overnight' for planned work, from the period that's active now."""
    start = datetime.fromisoformat(alert["active_from"]) if alert["active_from"] else None
    end = datetime.fromisoformat(alert["active_until"]) if alert["active_until"] else None
    if not (start and end):
        return None
    duration = end - start
    # Fri/Sat start through Sun/Mon end.
    if duration >= timedelta(hours=24) and start.weekday() in (4, 5) and end.weekday() in (6, 0):
        return "weekend"
    if duration <= timedelta(hours=12) and (start.hour >= 20 or start.hour < 2):
        return "overnight"
    return None


def _fit(base_text: str, extra_text: str | None, limit: int = MAX_SUMMARY_LEN) -> str:
    """Append ', extra_text' only if the result stays within `limit` characters."""
    if extra_text and len(base_text) + 2 + len(extra_text) <= limit:
        return f"{base_text}, {extra_text}"
    return base_text


def summarize(alert: dict[str, Any]) -> str:
    """Chip-sized summary of one normalized alert, e.g. 'Part suspended, Court Sq–Hoyts'."""
    alert_category, alert_type, direction = alert["category"], alert["type"], alert["direction"]
    direction_text = "both directions" if direction == "both" else direction
    if alert_category == "suspended":
        base_text = "Suspended" if alert_type == "Suspended" else "Part suspended"
        return _fit(base_text, _segment(alert["header"]))
    if alert_category == "planned":
        base_text = "Planned suspension" if "Suspended" in alert_type else "Planned work"
        return _fit(base_text, _when(alert))
    base_text = "Delays" if alert_type in ("Delays", "Severe Delays") else alert_type.capitalize()
    if direction in ("northbound", "southbound"):
        if len(base_text) + len(direction) < MAX_SUMMARY_LEN:
            return f"{base_text} {direction}"
        return base_text
    return _fit(base_text, direction_text)


def _trim_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields of a feed entity worth storing in `raw`."""
    alert = entity["alert"]
    mercury_alert = alert.get(MERCURY_ALERT_KEY, {})
    return {
        "id": entity.get("id"),
        "alert_type": mercury_alert.get("alert_type"),
        "header": _english_text(alert.get("header_text")),
        "description": _english_text(alert.get("description_text"))[:500],
        "active_period": alert.get("active_period", [])[:4],
        "human_readable_active_period": (
            _english_text(mercury_alert.get("human_readable_active_period")) or None
        ),
        "informed_entity": [
            {
                key: value
                for key, value in informed.items()
                if key in ("route_id", "stop_id", "direction_id")
            }
            for informed in alert.get("informed_entity", [])
            if informed.get("route_id") == ROUTE_ID
        ],
        "updated_at": mercury_alert.get("updated_at"),
    }


class GTrainService:
    """Current G train status, led by the most severe active alert."""

    key: ClassVar[str] = "g_train"
    label: ClassVar[str] = "G train"

    async def fetch(self, ctx: RunContext) -> Reading:
        """Collect G alerts active at `ctx.now`, ranked by severity."""
        response = await ctx.http.get(ALERTS_URL, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("entity"), list):
            raise ValueError("MTA alerts feed: missing 'entity' list")
        feed_timestamp = payload.get("header", {}).get("timestamp")
        if not feed_timestamp:
            raise ValueError("MTA alerts feed: missing header.timestamp")

        now_timestamp = ctx.now.timestamp()
        # (normalized alert, source entity) pairs for alerts affecting the G right now.
        active_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entity in payload["entity"]:
            source_alert = entity.get("alert") or {}
            route_entities = [
                informed
                for informed in source_alert.get("informed_entity", [])
                if informed.get("route_id") == ROUTE_ID
            ]
            period = _active_period(source_alert.get("active_period", []), now_timestamp)
            if not route_entities or period is None:
                continue
            header = _english_text(source_alert.get("header_text"))
            alert_type = source_alert.get(MERCURY_ALERT_KEY, {}).get("alert_type") or "Alert"
            normalized_alert = {
                "type": alert_type,
                "category": category(alert_type),
                "header": header,
                "direction": _direction(header, route_entities),
                "active_from": _timestamp_to_iso(period.get("start")),
                "active_until": _timestamp_to_iso(period.get("end")),
            }
            active_pairs.append((normalized_alert, entity))
        active_pairs.sort(key=lambda pair: SEVERITY[pair[0]["category"]])

        alerts = [normalized_alert for normalized_alert, _ in active_pairs]
        categories = {alert["category"] for alert in alerts}
        # Informational alerts never become the headline.
        top_alert = next((alert for alert in alerts if alert["category"] != "info"), None)
        data = {
            "status": top_alert["category"] if top_alert else "good",
            "delayed": "delays" in categories,
            "suspended": "suspended" in categories
            or any(
                alert["category"] == "planned" and "Suspended" in alert["type"] for alert in alerts
            ),
            "planned": "planned" in categories,
            "alerts": alerts[:MAX_ALERTS],
            "feed_updated": _timestamp_to_iso(feed_timestamp),
        }
        raw = {
            "header": payload["header"],
            "entity": [_trim_entity(entity) for _, entity in active_pairs[:MAX_ALERTS]],
        }
        is_stale = ctx.now - datetime.fromtimestamp(int(feed_timestamp), TZ) > STALE_AFTER
        value = summarize(top_alert) if top_alert else "Good service"
        return ctx.reading(self, value, status="stale" if is_stale else "ok", data=data, raw=raw)
