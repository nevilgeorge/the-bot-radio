"""Verify merged diagnostics and crash-safe artifact replacement."""

import json
from pathlib import Path

import pytest

from the_bot_radio.generation import runtime


def test_diagnostics_retains_setup_evidence_after_failure(tmp_path: Path) -> None:
    runtime.update_diagnostics(tmp_path, status="running", date="2026-10-27")
    runtime.update_diagnostics(tmp_path, inputs=[{"key": "weather", "raw": {"temp": 52}}])
    runtime.update_diagnostics(tmp_path, mood_usage={"model": "test"})
    runtime.update_diagnostics(tmp_path, status="failed", error_type="RuntimeError")
    diagnostics = json.loads((tmp_path / "diagnostics.json").read_text())
    assert diagnostics == {
        "status": "failed",
        "date": "2026-10-27",
        "inputs": [{"key": "weather", "raw": {"temp": 52}}],
        "mood_usage": {"model": "test"},
        "error_type": "RuntimeError",
    }


def test_failed_atomic_update_preserves_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "proposal.json"
    runtime.write_json_atomic(path, {"title": "Original"})

    def fail_replace(source: Path, destination: Path) -> None:
        """Simulate failure between flushing the new data and replacing the artifact."""
        raise OSError("simulated disk failure")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        runtime.write_json_atomic(path, {"title": "Replacement"})
    assert json.loads(path.read_text()) == {"title": "Original"}
    assert sorted(item.name for item in tmp_path.iterdir()) == ["proposal.json"]
