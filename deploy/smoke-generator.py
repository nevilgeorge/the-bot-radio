"""Check an installed generator image without credentials or paid API requests."""

import os
import platform
import subprocess
from pathlib import Path

import claude_agent_sdk
from pydantic import TypeAdapter

import the_bot_radio
from the_bot_radio.catalog import load_catalog
from the_bot_radio.generation.models import normalize_artist
from the_bot_radio.resources import RESOURCE_DIR
from the_bot_radio.web.models import Persona


def validate_packaged_resources(resources_directory: Path) -> None:
    """Validate the installed package resources independently of writable runtime storage."""
    required_files = {"personas.json", "shared_catalog.jsonl"}
    assert not any((resources_directory / name).exists() for name in ("days", "runs"))
    for filename in required_files:
        input_path = resources_directory / filename
        assert input_path.is_file() and not input_path.is_symlink(), input_path
        assert input_path.stat().st_size > 0, input_path
    catalog = load_catalog(resources_directory / "shared_catalog.jsonl")
    eligible_artists = {
        normalize_artist(track.artists[0])
        for track in catalog.values()
        if track.artists and track.artists[0].strip() and track.duration_ms > 0
    }
    assert len(eligible_artists) >= 5, "Catalog needs five distinct eligible primary artists"
    personas = TypeAdapter(list[Persona]).validate_json(
        (resources_directory / "personas.json").read_text()
    )
    assert personas and len({persona.id for persona in personas}) == len(personas)
    for persona in personas:
        for field_name in ("name", "bio", "agent_instructions", "speaking_voice"):
            field_value = getattr(persona, field_name).strip()
            assert field_value and not field_value.startswith("["), (persona.id, field_name)


def main() -> None:
    """Verify runtime identity, writable storage, packaged code, and bundled CLI."""
    assert platform.machine() == "x86_64", platform.machine()
    assert os.getuid() == 10001, os.getuid()
    assert os.environ["TZ"] == "America/New_York"
    assert os.environ["BOT_RADIO_DATA_DIR"] == "/work/data"
    for directory in (Path.home(), Path("/work"), Path("/work/data")):
        probe_path = directory / ".generator-smoke-test"
        probe_path.write_text("writable")
        probe_path.unlink()
    assert not list(Path("/work/data").iterdir())
    validate_packaged_resources(RESOURCE_DIR)
    application_path = Path(the_bot_radio.__file__).resolve().parent
    assert "site-packages" in application_path.parts, application_path
    assert RESOURCE_DIR == application_path / "resources"
    for root_path in (Path("/work"), Path.home(), application_path):
        for candidate_path in root_path.rglob("*"):
            assert candidate_path.name not in {".git", ".aws", ".claude", "credentials"}
            assert not candidate_path.name.startswith(".env"), candidate_path
    assert not Path("/build").exists()
    bundled_cli_path = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    assert bundled_cli_path.is_file() and os.access(bundled_cli_path, os.X_OK)
    subprocess.run([str(bundled_cli_path), "--version"], check=True, timeout=60)
    print("Generator image smoke checks passed.")


if __name__ == "__main__":
    main()
