import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from the_bot_radio.services.inputs.base import TZ, RunContext, Settings, make_http_client

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live API test; set RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def load_fixture(key: str, name: str):
    """Load tests/services/fixtures/<key>/<name> (parsed JSON for .json files, else text)."""
    path = FIXTURES / key / name
    return json.loads(path.read_text()) if path.suffix == ".json" else path.read_text()


@pytest.fixture
async def make_ctx(tmp_path):
    """Factory for RunContext. Defaults: 2026-10-26 06:00 ET, tmp data dir, default settings.

    Use inside `@pytest.mark.anyio` tests; mock HTTP with respx (`@respx.mock`).
    """
    async with make_http_client() as client:

        def _make(
            run_date: date = date(2026, 10, 26),
            now: datetime | None = None,
            settings: Settings | None = None,
            data_dir: Path | None = None,
        ) -> RunContext:
            return RunContext(
                run_date=run_date,
                now=now or datetime(run_date.year, run_date.month, run_date.day, 6, 0, tzinfo=TZ),
                http=client,
                settings=settings or Settings(),
                data_dir=data_dir or tmp_path,
            )

        yield _make
