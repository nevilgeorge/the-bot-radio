"""Check an installed generator image without credentials or paid API requests."""

import os
import platform
import subprocess
from pathlib import Path

import claude_agent_sdk

import the_bot_radio


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
    application_path = Path(the_bot_radio.__file__).resolve().parent
    assert "site-packages" in application_path.parts, application_path
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
