"""311 service requests in our ZIP, yesterday.

Source: NYC Open Data "311 Service Requests from 2020 to Present" (Socrata SODA 2.1),
https://data.cityofnewyork.us/resource/erm2-nwe9.json. `created_date` is a floating
(zone-less) New York local timestamp. The dataset loads once a day (~21:30 ET) and only
runs through the early hours of that day, so at a 06:00 run "yesterday" is usually
incomplete; we detect that from the citywide max(created_date) and fall back to the last
complete day as `stale`. See docs/inputs/nyc_311.md.
"""

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar

from .base import Reading, RunContext

ENDPOINT = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
TOP_TYPES = 10


def _midnight_timestamp(day: date) -> str:
    """Socrata floating timestamp for the start of `day`."""
    return f"{day.isoformat()}T00:00:00"


def _pluralize(count: int, word: str) -> str:
    """'1 request' / '3 requests'."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _is_noise(complaint_type: str) -> bool:
    """True for any of the 'Noise - ...' complaint types."""
    return complaint_type.casefold().startswith("noise")


def _summarize(day_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals and ranked complaint-type counts for one day's rows."""
    counts_by_type: dict[str, int] = {}
    for row in day_rows:
        complaint_type = row["complaint_type"]
        counts_by_type[complaint_type] = counts_by_type.get(complaint_type, 0) + row["n"]
    ranked_types = sorted(
        counts_by_type.items(), key=lambda type_count: type_count[1], reverse=True
    )
    noise_count = sum(count for complaint_type, count in ranked_types if _is_noise(complaint_type))
    return {
        "total": sum(counts_by_type.values()),
        "noise": noise_count,
        "by_type": dict(ranked_types[:TOP_TYPES]),
        "top_type": ranked_types[0][0] if ranked_types else None,
        # Largest single non-noise type; used by `headline` and then dropped.
        "top_other": max(
            (count for complaint_type, count in ranked_types if not _is_noise(complaint_type)),
            default=0,
        ),
    }


def headline(total: int, noise: int, top_other: int) -> str:
    """Lead with noise when all Noise* types together outnumber any other single type."""
    if noise and noise >= top_other:
        return _pluralize(noise, "noise complaint")
    return _pluralize(total, "request")


class Nyc311Service:
    """Yesterday's 311 requests in the configured ZIP, by complaint type."""

    key: ClassVar[str] = "nyc_311_yesterday"
    label: ClassVar[str] = "311, yesterday"
    timeout: ClassVar[float] = 45.0  # Socrata queries can take 20s+ when cold

    async def _query(self, ctx: RunContext, soql_params: dict[str, str]) -> list[dict[str, Any]]:
        """Run one SoQL query against the 311 dataset and return its rows."""
        app_token = ctx.settings.nyc_open_data_app_token
        headers = {"X-App-Token": app_token} if app_token else {}
        response = await ctx.http.get(ENDPOINT, params=soql_params, headers=headers, timeout=40.0)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise ValueError(f"unexpected 311 payload: {str(body)[:200]}")
        return body

    async def fetch(self, ctx: RunContext) -> Reading:
        """Count yesterday's requests, or the day before's (stale) if yesterday isn't loaded."""
        yesterday = ctx.run_date - timedelta(days=1)
        day_before_yesterday = ctx.run_date - timedelta(days=2)
        zip_code = ctx.settings.zip_code
        # Two queries: the citywide load watermark, and per-day/type counts for our ZIP.
        watermark_rows, grouped_rows = await asyncio.gather(
            self._query(
                ctx,
                {
                    "$select": "max(created_date) AS latest",
                    "$where": f"created_date >= '{_midnight_timestamp(day_before_yesterday)}'",
                },
            ),
            self._query(
                ctx,
                {
                    "$select": "date_trunc_ymd(created_date) AS day, complaint_type, count(*) AS n",
                    "$where": (
                        f"incident_zip='{zip_code}'"
                        f" AND created_date >= '{_midnight_timestamp(day_before_yesterday)}'"
                        f" AND created_date < '{_midnight_timestamp(ctx.run_date)}'"
                    ),
                    "$group": "day, complaint_type",
                    "$order": "day, n DESC",
                },
            ),
        )
        try:
            latest_raw = watermark_rows[0].get("latest") if watermark_rows else None
            latest_loaded_at = datetime.fromisoformat(latest_raw) if latest_raw else None
            parsed_rows = [
                {
                    "day": date.fromisoformat(row["day"][:10]),
                    "complaint_type": str(row["complaint_type"]),
                    "n": int(row["n"]),
                }
                for row in grouped_rows
                if row.get("complaint_type")  # untyped requests are rare; skip them
            ]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed 311 payload: {exc}") from exc

        base_data: dict[str, Any] = {
            "zip": zip_code,
            "requested_date": yesterday.isoformat(),
            "complete_through": latest_loaded_at.isoformat() if latest_loaded_at else None,
        }
        raw: dict[str, Any] = {"latest": latest_raw}
        # A day is complete once the dataset has records from the following day.
        if latest_loaded_at and latest_loaded_at >= datetime.combine(ctx.run_date, time()):
            reported_day, status = yesterday, "ok"
        elif latest_loaded_at and latest_loaded_at >= datetime.combine(yesterday, time()):
            reported_day, status = day_before_yesterday, "stale"  # yesterday hasn't fully landed
        else:
            return ctx.reading(
                self, "No data yet", status="stale", data={**base_data, "date": None}, raw=raw
            )

        day_rows = [row for row in parsed_rows if row["day"] == reported_day]
        summary = _summarize(day_rows)
        value = headline(summary["total"], summary["noise"], summary.pop("top_other"))
        if status == "stale":
            value = f"{reported_day:%a}: {value}"
        raw["rows"] = [{"complaint_type": row["complaint_type"], "n": row["n"]} for row in day_rows]
        return ctx.reading(
            self,
            value,
            status=status,
            data={**base_data, "date": reported_day.isoformat(), **summary},
            raw=raw,
        )
