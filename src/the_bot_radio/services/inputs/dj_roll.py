"""The DJ roll: picks today's resident DJ from packaged resources/personas.json.

The pick is a seeded random choice (seed = ``run_date.toordinal()``), so re-running a
day always rolls the same DJ, in any process. Yesterday's DJ (read from
``data/days/<yesterday>.json`` by default) is excluded whenever there are at least two
personas. An injected daily reader may supply remote history; its caller owns the
reader and any underlying client, and remote read failures propagate to the runner.
"""

import json
import random
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar, Protocol

from pydantic import TypeAdapter

from the_bot_radio.services.inputs.base import Reading, RunContext
from the_bot_radio.web.models import Persona

_persona_list_adapter = TypeAdapter(list[Persona])


class DailyHistoryReader(Protocol):
    """Read yesterday's original bytes without owning a storage client."""

    async def read(self, run_date: date) -> bytes | None:
        """Return daily bytes or None for a missing record; propagate read failures."""
        ...


def _dj_from_payload(payload: str | bytes | None) -> str | None:
    """Extract a string DJ from any standalone record, ignoring malformed payloads."""
    if payload is None:
        return None
    try:
        dj_id = json.loads(payload).get("dj")
    except (ValueError, AttributeError):
        return None
    return dj_id if isinstance(dj_id, str) else None


def _yesterdays_dj(day_record_path: Path) -> str | None:
    """The `dj` id from a day record; None if the file is missing or unreadable."""
    try:
        return _dj_from_payload(day_record_path.read_bytes())
    except OSError:
        return None


class DjRollService:
    """Rolls today's resident DJ, avoiding a repeat of yesterday's."""

    key: ClassVar[str] = "dj_roll"
    label: ClassVar[str] = "The DJ roll"

    def __init__(self, daily_reader: DailyHistoryReader | None = None) -> None:
        """Use local history by default, or an injected caller-owned daily reader."""
        self.daily_reader = daily_reader

    async def fetch(self, ctx: RunContext) -> Reading:
        """Pick a persona deterministically for `ctx.run_date`."""
        personas = _persona_list_adapter.validate_json(
            (ctx.resources_dir / "personas.json").read_text(encoding="utf-8")
        )
        yesterday = ctx.run_date - timedelta(days=1)
        if self.daily_reader is None:
            yesterdays_dj_id = _yesterdays_dj(
                ctx.data_dir / "days" / f"{yesterday.isoformat()}.json"
            )
        else:
            yesterdays_dj_id = _dj_from_payload(await self.daily_reader.read(yesterday))
        return self.roll(ctx, personas, yesterdays_dj_id)

    def roll(
        self, ctx: RunContext, personas: Sequence[Persona], previous_dj_id: str | None
    ) -> Reading:
        """Roll from already loaded personas and a caller-verified previous DJ."""
        if not personas:
            raise ValueError("personas.json has no personas")
        excluded_ids = [previous_dj_id] if previous_dj_id and len(personas) >= 2 else []
        candidates = [persona for persona in personas if persona.id not in excluded_ids]
        candidates = candidates or personas

        # date.toordinal() is stable across processes (unlike hash() of a str).
        seed = ctx.run_date.toordinal()
        picked_persona = random.Random(seed).choice(candidates)
        return ctx.reading(
            self,
            picked_persona.name,
            data={
                "persona_id": picked_persona.id,
                "excluded": excluded_ids,
                "candidates": [persona.id for persona in candidates],
                "seed": seed,
            },
        )
