import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from the_bot_radio.web.main import app, get_store
from the_bot_radio.web.store import Store

REPO_DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture
def site_data_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the repo's data/ dir that tests may modify freely."""
    dest = tmp_path / "data"
    shutil.copytree(REPO_DATA, dest)
    return dest


@pytest.fixture
def client(site_data_dir: Path) -> Iterator[TestClient]:
    store = Store(site_data_dir)
    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


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
