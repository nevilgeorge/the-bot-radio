"""Citi Bike availability summed over every dock in our ZIP code, from the public GBFS feeds.

Source: Lyft-hosted GBFS 2.3 for Citi Bike NYC (``bkn``), as listed by the autodiscovery feed
https://gbfs.citibikenyc.com/gbfs/gbfs.json → gbfs_versions. Two calls, made concurrently:
``station_information.json`` (name, lat/lon, capacity) and ``station_status.json``
(bikes/docks/last_reported). GBFS has no ZIP field, so stations are matched against the ZIP's
boundary polygon bundled in ``geo/<zip>.json`` (NYC MODZCTA). See docs/inputs/citibike.md.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, ClassVar

from .base import Reading, RunContext

GBFS_BASE = "https://gbfs.lyft.com/gbfs/2.3/bkn/en"
STATUS_URL = f"{GBFS_BASE}/station_status.json"
INFO_URL = f"{GBFS_BASE}/station_information.json"
GEO_DIR = Path(__file__).parent / "geo"
STALE_AFTER = timedelta(minutes=60)
TOTALED_FIELDS = ("bikes", "ebikes", "docks", "capacity")

Ring = list[list[float]]  # [[lon, lat], ...]


@cache
def zip_polygons(zip_code: str) -> list[list[Ring]]:
    """MultiPolygon coordinates (polygons → rings, first ring is the outer boundary)."""
    boundary_path = GEO_DIR / f"{zip_code}.json"
    if not boundary_path.exists():
        raise ValueError(f"no boundary for ZIP {zip_code} (expected {boundary_path.name} in geo/)")
    return json.loads(boundary_path.read_text())["coordinates"]


def _is_point_in_ring(lon: float, lat: float, ring: Ring) -> bool:
    """Ray-casting point-in-polygon test: count edge crossings of a ray cast east of the point."""
    is_inside = False
    previous_vertices = ring[-1:] + ring[:-1]
    for (x1, y1), (x2, y2) in zip(ring, previous_vertices, strict=True):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            is_inside = not is_inside
    return is_inside


def in_zip(lon: float, lat: float, polygons: list[list[Ring]]) -> bool:
    """True when the point is inside any polygon's outer ring and none of its holes."""
    return any(
        _is_point_in_ring(lon, lat, outer_ring)
        and not any(_is_point_in_ring(lon, lat, hole_ring) for hole_ring in hole_rings)
        for outer_ring, *hole_rings in polygons
    )


def _extract_stations(payload: Any, feed_name: str) -> list[dict[str, Any]]:
    """Return a GBFS feed's ``data.stations`` list, raising ValueError if it's malformed."""
    try:
        stations = payload["data"]["stations"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed {feed_name} payload") from exc
    if not isinstance(stations, list):
        raise ValueError(f"malformed {feed_name} payload")
    return stations


class CitiBikeService:
    """Bikes docked across every Citi Bike station in the configured ZIP."""

    key: ClassVar[str] = "citibike"
    label: ClassVar[str] = "Citi Bikes · Greenpoint"

    async def fetch(self, ctx: RunContext) -> Reading:
        """Sum bikes/docks over in-ZIP stations, preferring stations with fresh reports."""
        zip_code = ctx.settings.zip_code
        polygons = zip_polygons(zip_code)
        info_response, status_response = await asyncio.gather(
            ctx.http.get(INFO_URL), ctx.http.get(STATUS_URL)
        )
        info_response.raise_for_status()
        status_response.raise_for_status()

        # station_id -> station_information record, for stations inside the ZIP boundary.
        try:
            local_stations_by_id = {
                station["station_id"]: station
                for station in _extract_stations(info_response.json(), "station_information")
                if in_zip(float(station["lon"]), float(station["lat"]), polygons)
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed station_information record") from exc
        if not local_stations_by_id:
            raise ValueError(f"no Citi Bike stations found in ZIP {zip_code}")

        # Bucket each local station by whether it's renting and how recently it reported.
        active_rows: list[dict[str, Any]] = []
        offline_rows: list[dict[str, Any]] = []
        stale_rows: list[dict[str, Any]] = []
        for station_status in _extract_stations(status_response.json(), "station_status"):
            station_info = local_stations_by_id.get(station_status.get("station_id"))
            if station_info is None:
                continue
            try:
                station_row = {
                    "station_id": station_info["station_id"],
                    "name": station_info.get("name", ""),
                    "bikes": int(station_status["num_bikes_available"]),
                    "ebikes": int(station_status.get("num_ebikes_available", 0)),
                    "docks": int(station_status["num_docks_available"]),
                    "capacity": int(station_info["capacity"]),
                    "reported": datetime.fromtimestamp(int(station_status["last_reported"]), UTC),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"malformed station record for {station_info['station_id']}"
                ) from exc
            if not (station_status.get("is_installed") and station_status.get("is_renting")):
                offline_rows.append(station_row)
            elif ctx.now - station_row["reported"] > STALE_AFTER:
                stale_rows.append(station_row)
            else:
                active_rows.append(station_row)

        # Sum the stations reporting fresh data; if none are fresh, fall back to the stale ones.
        counted_rows = active_rows or stale_rows
        if not counted_rows:
            raise ValueError(f"no renting Citi Bike stations in ZIP {zip_code}")
        totals = {
            field_name: sum(row[field_name] for row in counted_rows)
            for field_name in TOTALED_FIELDS
        }
        newest_report = max(row["reported"] for row in counted_rows).astimezone(ctx.now.tzinfo)

        return ctx.reading(
            self,
            f"{totals['bikes']} of {totals['capacity']} docked",
            status="ok" if active_rows else "stale",
            data={
                "zip": zip_code,
                **totals,
                "station_count": len(counted_rows),
                "stale_stations": [row["name"] for row in stale_rows] if active_rows else [],
                "offline_stations": [row["name"] for row in offline_rows],
                "last_reported": newest_report.isoformat(),
                "stations": [
                    {
                        field_name: row[field_name]
                        for field_name in ("station_id", "name", "bikes", "capacity")
                    }
                    for row in sorted(counted_rows, key=lambda row: row["name"])
                ],
            },
            raw={"station_ids": sorted(local_stations_by_id)},
        )
