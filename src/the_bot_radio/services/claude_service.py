"""Every request The Bot Radio makes to Claude, via the Anthropic SDK.

Prompts live here as module constants so they can be tuned in one place. The client reads
credentials from the environment (`ANTHROPIC_API_KEY`, or an `ant auth login` profile).
"""

import json
from collections.abc import Sequence
from typing import Any

import anthropic

from the_bot_radio.services.inputs.base import Reading
from the_bot_radio.web.models import Mood

MODEL = "claude-opus-5-5"
MOOD_EFFORT = "low"  # a short judgment call; raise if mood quality needs it
MAX_TOKENS = 16000
# On a safety decline, the API re-runs the request on a recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

MOOD_SYSTEM_PROMPT = """\
You set the musical mood for The Bot Radio, a daily playlist generated using different inputs
reladted to Greenpoint, Brooklyn.

You'll get today's readings of the neighborhood as JSON: things like the weather, the G train, \
sunset, the Newtown Creek tide, Citi Bike availability, yesterday's 311 complaints, the day of \
the week, and today's resident DJ.

Use these to generate a "mood" for songs for the playlist. Mood consistst of four scores:

- tempo_bpm: the playlist's average BPM, from 60 to 160. A rainy Sunday is around 75; a bright, \
busy Friday is around 120.
- energy: 0 to 10, from hushed and still to driving and intense.
- brightness: 0 to 10, from dark and moody to sunny and major-key.
- obscurity: 0 to 10, from songs everyone knows to deep cuts.

Weigh each reading by how much it would plausibly change someone's day in the neighborhood: \
weather and the day of the week matter most, and the rest nudge. Readings marked "stale" are \
out of date, so give them less weight. Some readings may be missing; work with what's there. \
Don't drift to the middle of each range by default. Commit to the mood the readings justify.\
"""


def build_mood_prompt(readings: Sequence[Reading]) -> str:
    """Render today's readings as the user message for the mood request.

    Skipped readings are dropped; stale ones are kept with their status. Only the normalized
    `data` is sent (not `raw`), with sorted keys so identical readings give identical text.
    """
    reading_summaries: list[dict[str, Any]] = [
        {
            "key": reading.key,
            "label": reading.label,
            "value": reading.value,
            "status": reading.status,
            "data": reading.data,
        }
        for reading in readings
        if reading.status != "skipped"
    ]
    readings_json = json.dumps(reading_summaries, indent=2, sort_keys=True, ensure_ascii=False)
    return f"Today's readings:\n\n{readings_json}"


class ClaudeService:
    """Wraps the Anthropic client; one method per kind of request the bot makes."""

    def __init__(self, anthropic_client: anthropic.AsyncAnthropic | None = None) -> None:
        """Use `anthropic_client` if given (tests inject a fake), else build one from the env."""
        self.anthropic_client = anthropic_client or anthropic.AsyncAnthropic()

    async def score_mood(self, readings: Sequence[Reading]) -> Mood:
        """Turn today's input readings into a `Mood` (tempo, energy, brightness, obscurity)."""
        response = await self.anthropic_client.beta.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=MOOD_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_mood_prompt(readings)}],
            output_config={"effort": MOOD_EFFORT},
            output_format=Mood,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            refusal_category = response.stop_details.category if response.stop_details else None
            raise ValueError(f"mood request refused (category: {refusal_category})")
        if response.parsed_output is None:
            raise ValueError(f"mood response had no parsed output (stop: {response.stop_reason})")
        return response.parsed_output
