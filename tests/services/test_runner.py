import asyncio
from datetime import datetime
from typing import ClassVar

import pytest

from the_bot_radio.services.inputs import runner
from the_bot_radio.services.inputs.base import TZ, Reading, RunContext, make_http_client

pytestmark = pytest.mark.anyio


class Ok:
    key: ClassVar[str] = "ok_input"
    label: ClassVar[str] = "OK input"

    async def fetch(self, ctx: RunContext) -> Reading:
        return ctx.reading(self, "fine")


class Boom:
    key: ClassVar[str] = "boom"
    label: ClassVar[str] = "Boom"

    async def fetch(self, ctx: RunContext) -> Reading:
        raise RuntimeError("source down")


class Slow:
    key: ClassVar[str] = "slow"
    label: ClassVar[str] = "Slow"

    async def fetch(self, ctx: RunContext) -> Reading:
        await asyncio.sleep(5)
        return ctx.reading(self, "late")


async def test_failures_are_skipped_and_order_is_kept(make_ctx, monkeypatch, caplog):
    monkeypatch.setattr(runner, "SERVICE_TIMEOUT", 0.05)
    readings = await runner.read_all(make_ctx(), [Boom(), Ok(), Slow()])
    assert [r.key for r in readings] == ["boom", "ok_input", "slow"]
    assert [r.status for r in readings] == ["skipped", "ok", "skipped"]
    assert readings[0].error == "RuntimeError: source down"
    assert readings[2].error == "TimeoutError"
    assert readings[1].value == "fine"
    assert "input boom skipped" in caplog.text


@pytest.mark.live
async def test_live_read_all():
    """RUN_LIVE=1 uv run pytest -m live -s  → prints today's readings from the real APIs."""
    now = datetime.now(TZ)
    async with make_http_client() as http:
        readings = await runner.read_all(RunContext(run_date=now.date(), now=now, http=http))
    for r in readings:
        print(f"{r.key:20} {r.status:8} {r.value!r:40} {r.error or ''}")
    assert len(readings) == 8
