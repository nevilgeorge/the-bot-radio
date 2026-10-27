import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta

import pytest

from the_bot_radio.services.inputs.dj_roll import DjRollService

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 27)


def write_personas(data_dir, n: int) -> list[str]:
    ids = [f"resident-{i:02d}" for i in range(1, n + 1)]
    personas = [{"id": i, "name": f"[{i}]", "bio": ""} for i in ids]
    (data_dir / "personas.json").write_text(json.dumps(personas))
    return ids


def write_day(data_dir, day: date, content: str) -> None:
    days = data_dir / "days"
    days.mkdir(exist_ok=True)
    (days / f"{day.isoformat()}.json").write_text(content)


async def test_happy_path(make_ctx, tmp_path):
    ids = write_personas(tmp_path, 4)
    r = await DjRollService().fetch(make_ctx(run_date=DAY))
    assert r.key == "dj_roll" and r.label == "The DJ roll" and r.status == "ok"
    pid = r.data["persona_id"]
    assert pid in ids and r.value == f"[{pid}]"
    assert r.data["excluded"] == [] and r.data["candidates"] == ids
    assert r.data["seed"] == DAY.toordinal()


async def test_same_date_same_pick(make_ctx, tmp_path):
    write_personas(tmp_path, 4)
    picks = {(await DjRollService().fetch(make_ctx(run_date=DAY))).value for _ in range(10)}
    assert len(picks) == 1


async def test_same_pick_across_processes(make_ctx, tmp_path):
    write_personas(tmp_path, 4)
    here = (await DjRollService().fetch(make_ctx(run_date=DAY))).data["persona_id"]
    script = (
        "import asyncio, datetime, pathlib, sys\n"
        "from the_bot_radio.services.inputs.base import TZ, RunContext, make_http_client\n"
        "from the_bot_radio.services.inputs.dj_roll import DjRollService\n"
        "async def main():\n"
        "    d = datetime.date.fromisoformat(sys.argv[2])\n"
        "    async with make_http_client() as http:\n"
        "        ctx = RunContext(run_date=d, now=datetime.datetime.now(TZ), http=http,\n"
        "                         data_dir=pathlib.Path(sys.argv[1]))\n"
        "        print((await DjRollService().fetch(ctx)).data['persona_id'])\n"
        "asyncio.run(main())\n"
    )
    for hashseed in ("1", "2"):
        out = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), DAY.isoformat()],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": hashseed},
        )
        assert out.stdout.strip() == here


async def test_never_repeats_yesterday_over_a_year(make_ctx, tmp_path):
    ids = write_personas(tmp_path, 4)
    counts: Counter[str] = Counter()
    prev = None
    for i in range(365):
        day = date(2026, 1, 1) + timedelta(days=i)
        r = await DjRollService().fetch(make_ctx(run_date=day))
        pid = r.data["persona_id"]
        assert pid != prev
        if prev:
            assert r.data["excluded"] == [prev] and prev not in r.data["candidates"]
        write_day(tmp_path, day, json.dumps({"date": day.isoformat(), "dj": pid}))
        counts[pid] += 1
        prev = pid
    # Uniform would be ~91 each; loose bound catches a broken or stuck roll.
    assert set(counts) == set(ids)
    assert all(50 <= c <= 135 for c in counts.values()), counts


async def test_single_persona_plays_again(make_ctx, tmp_path):
    write_personas(tmp_path, 1)
    write_day(tmp_path, DAY - timedelta(days=1), json.dumps({"dj": "resident-01"}))
    r = await DjRollService().fetch(make_ctx(run_date=DAY))
    assert r.data["persona_id"] == "resident-01"
    assert r.data["excluded"] == []


@pytest.mark.parametrize("content", [None, "not json {", "[1, 2]", '{"dj": 7}', "{}"])
async def test_missing_or_garbage_yesterday_means_no_exclusion(make_ctx, tmp_path, content):
    ids = write_personas(tmp_path, 4)
    if content is not None:
        write_day(tmp_path, DAY - timedelta(days=1), content)
    r = await DjRollService().fetch(make_ctx(run_date=DAY))
    assert r.data["excluded"] == [] and r.data["candidates"] == ids


async def test_no_personas_raises(make_ctx, tmp_path):
    (tmp_path / "personas.json").write_text("[]")
    with pytest.raises(ValueError):
        await DjRollService().fetch(make_ctx(run_date=DAY))


async def test_invalid_personas_raises(make_ctx, tmp_path):
    (tmp_path / "personas.json").write_text('[{"name": "no id"}]')
    with pytest.raises(ValueError):
        await DjRollService().fetch(make_ctx(run_date=DAY))
