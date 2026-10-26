import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from the_bot_radio.services import claude_service
from the_bot_radio.services.claude_service import ClaudeService, build_mood_prompt
from the_bot_radio.services.inputs import runner
from the_bot_radio.services.inputs.base import TZ, Reading, RunContext, make_http_client
from the_bot_radio.web.models import Mood

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=TZ)
MOOD = Mood(tempo_bpm=84, energy=4, brightness=3, obscurity=6)


def _reading(key: str, status: str = "ok", **fields) -> Reading:
    return Reading(
        key=key, label=key.title(), value=f"{key} value", status=status, fetched_at=NOW, **fields
    )


def _fake_client(response: SimpleNamespace) -> SimpleNamespace:
    """Stand-in for AsyncAnthropic exposing only `beta.messages.parse`."""
    parse = AsyncMock(return_value=response)
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))


def _response(parsed_output=MOOD, stop_reason="end_turn", stop_details=None) -> SimpleNamespace:
    return SimpleNamespace(
        parsed_output=parsed_output, stop_reason=stop_reason, stop_details=stop_details
    )


def test_prompt_drops_skipped_keeps_stale_and_omits_raw():
    prompt = build_mood_prompt(
        [
            _reading("weather", data={"temp_f": 52}, raw={"secret": "payload"}),
            _reading("tide", status="stale"),
            _reading("g_train", status="skipped", error="TimeoutError"),
        ]
    )
    sent = json.loads(prompt.split("\n\n", 1)[1])
    assert [r["key"] for r in sent] == ["weather", "tide"]
    assert sent[0]["data"] == {"temp_f": 52} and "raw" not in sent[0]
    assert sent[1]["status"] == "stale"


def test_prompt_is_stable_for_identical_readings():
    readings = [_reading("weather", data={"b": 1, "a": 2})]
    assert build_mood_prompt(readings) == build_mood_prompt(list(readings))


async def test_score_mood_returns_parsed_mood():
    client = _fake_client(_response())
    mood = await ClaudeService(client).score_mood([_reading("weather")])
    assert mood == MOOD
    kwargs = client.beta.messages.parse.await_args.kwargs
    assert kwargs["model"] == claude_service.MODEL
    assert kwargs["system"] == claude_service.MOOD_SYSTEM_PROMPT
    assert kwargs["output_format"] is Mood
    assert kwargs["messages"][0]["content"] == build_mood_prompt([_reading("weather")])


async def test_refusal_raises():
    details = SimpleNamespace(category="cyber")
    client = _fake_client(
        _response(parsed_output=None, stop_reason="refusal", stop_details=details)
    )
    with pytest.raises(ValueError, match="refused.*cyber"):
        await ClaudeService(client).score_mood([_reading("weather")])


async def test_missing_parsed_output_raises():
    client = _fake_client(_response(parsed_output=None, stop_reason="max_tokens"))
    with pytest.raises(ValueError, match="no parsed output.*max_tokens"):
        await ClaudeService(client).score_mood([_reading("weather")])


@pytest.mark.live
async def test_live_score_mood():
    """RUN_LIVE=1 uv run pytest -m live -s  → prints the mood Claude picks for today."""
    now = datetime.now(TZ)
    async with make_http_client() as http:
        readings = await runner.read_all(RunContext(run_date=now.date(), now=now, http=http))
    mood = await ClaudeService().score_mood(readings)
    print(mood.model_dump())
    assert 40 <= mood.tempo_bpm <= 200
