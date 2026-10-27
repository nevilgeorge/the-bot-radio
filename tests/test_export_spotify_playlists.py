"""Offline checks for exporting current-user playlist metadata without individual tracks."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import httpx
import pytest

from the_bot_radio.services.spotify_service import SpotifyError

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_spotify_playlists.py"
MODULE_SPEC = importlib.util.spec_from_file_location("spotify_playlist_exporter", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
exporter = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = exporter
MODULE_SPEC.loader.exec_module(exporter)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on the project's asyncio backend."""
    return "asyncio"


async def test_export_keeps_metadata_and_counts_without_tracks_and_deduplicates() -> None:
    first_summary = {"id": "first", "name": "First", "owner": {"id": "owner"}}
    second_summary = {"id": "second", "name": "Second"}
    first_details = {
        **first_summary,
        "description": "Full details",
        "images": [{"url": "https://example.com/cover.jpg"}],
        "items": {"items": [{"item": {"name": "Hidden song"}}], "next": "next", "total": 150},
    }
    second_details = {**second_summary, "collaborative": True}
    spotify_service = SimpleNamespace(
        get_current_user_playlists=AsyncMock(
            return_value=[first_summary, second_summary, first_summary]
        ),
        get_playlist_details=AsyncMock(side_effect=[first_details, second_details]),
    )

    results = await exporter.fetch_playlists(spotify_service)

    assert len(results) == 2
    assert results[0]["name"] == "First"
    assert results[0]["description"] == "Full details"
    assert results[0]["owner"]["id"] == "owner"
    assert results[0]["track_count"] == 150
    assert results[1]["collaborative"] is True
    assert results[1]["track_count"] is None
    assert "Hidden song" not in json.dumps(results)
    assert all("details" not in record and "summary" not in record for record in results)
    spotify_service.get_playlist_details.assert_has_awaits([call("first"), call("second")])
    assert spotify_service.get_playlist_details.await_count == 2


@pytest.mark.parametrize(
    "detail_error",
    [SpotifyError(403, "not accessible"), httpx.ConnectError("connection unavailable")],
)
async def test_detail_failure_keeps_summary_and_continues(detail_error: Exception) -> None:
    summaries = [
        {"id": "first", "name": "Unavailable", "tracks": {"total": 42}},
        {"id": "second", "name": "Works"},
    ]
    details = {"id": "second", "description": "Readable"}
    spotify_service = SimpleNamespace(
        get_current_user_playlists=AsyncMock(return_value=summaries),
        get_playlist_details=AsyncMock(side_effect=[detail_error, details]),
    )

    results = await exporter.fetch_playlists(spotify_service)

    assert results[0]["name"] == "Unavailable"
    assert results[0]["track_count"] == 42
    assert results[0]["error"]
    assert results[1]["name"] == "Works"
    assert results[1]["description"] == "Readable"
    assert results[1]["error"] is None


@pytest.mark.parametrize("contents_key", ["items", "tracks"])
def test_track_count_handles_zero_and_falls_back_to_summary(contents_key: str) -> None:
    summary = {"id": "playlist", contents_key: {"total": 42}}
    assert exporter.playlist_metadata(summary, {}, None)["track_count"] == 42
    details = {"id": "playlist", contents_key: {"total": 0, "items": []}}
    assert exporter.playlist_metadata(summary, details, None)["track_count"] == 0


async def test_empty_listing_and_listing_failure() -> None:
    spotify_service = SimpleNamespace(
        get_current_user_playlists=AsyncMock(return_value=[]),
        get_playlist_details=AsyncMock(),
    )
    assert await exporter.fetch_playlists(spotify_service) == []
    spotify_service.get_playlist_details.assert_not_awaited()

    spotify_service.get_current_user_playlists.side_effect = SpotifyError(401, "expired")
    with pytest.raises(SpotifyError, match="expired"):
        await exporter.fetch_playlists(spotify_service)


@pytest.mark.parametrize("write_file", [False, True])
def test_cli_writes_json_to_stdout_or_requested_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    write_file: bool,
) -> None:
    results = [{"id": "first", "name": "Café", "track_count": 20, "error": None}]
    monkeypatch.setattr(exporter.anyio, "run", lambda *_args: results)
    output_path = tmp_path / "playlists.json"
    arguments = ["--output", str(output_path)] if write_file else []

    assert exporter.main(arguments) == 0

    captured_output = capsys.readouterr()
    if write_file:
        assert json.loads(output_path.read_text()) == results
        assert captured_output.out == ""
    else:
        assert json.loads(captured_output.out) == results


def test_cli_reports_partial_failure_and_preserves_output_on_listing_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    partial_results = [{"id": "first", "name": "First", "error": "unavailable"}]
    monkeypatch.setattr(exporter.anyio, "run", lambda *_args: partial_results)
    output_path = tmp_path / "playlists.json"

    assert exporter.main(["--output", str(output_path)]) == 1
    assert json.loads(output_path.read_text()) == partial_results

    def fail_listing(*_args: Any) -> None:
        """Simulate an initial API failure before an export can be constructed."""
        raise SpotifyError(401, "expired")

    previous_content = output_path.read_text()
    monkeypatch.setattr(exporter.anyio, "run", fail_listing)
    assert exporter.main(["--output", str(output_path)]) == 1
    assert output_path.read_text() == previous_content
    assert capsys.readouterr().err
