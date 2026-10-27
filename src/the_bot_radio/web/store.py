"""File-backed store for daily playlists, personas and input descriptions.

Reads ``data/days/*.json``, ``data/personas.json`` and ``data/inputs.json``. Parsed data is
cached and re-read only when the files' (name, mtime) signature changes, so the pipeline can
drop a new day file without a restart. Invalid files are logged and skipped, never raised.
"""

import json
import logging
import threading
from datetime import date
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from .models import Day, InputDescription, Persona

log = logging.getLogger(__name__)

_personas_adapter = TypeAdapter(list[Persona])
_inputs_adapter = TypeAdapter(list[InputDescription])

Signature = tuple[tuple[str, int], ...]
_UNLOADED = object()  # sentinel: file signature never computed


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._days_sig: Signature | None = None
        self._personas_sig: object = _UNLOADED
        self._inputs_sig: object = _UNLOADED
        self._days: list[Day] = []  # newest first
        self._by_date: dict[date, Day] = {}
        self._personas: list[Persona] = []
        self._inputs: list[InputDescription] = []

    # ---- loading -------------------------------------------------------------------

    @property
    def _days_dir(self) -> Path:
        return self.data_dir / "days"

    def _days_signature(self) -> Signature:
        sig: list[tuple[str, int]] = []
        try:
            paths = sorted(self._days_dir.glob("*.json"))
        except OSError:
            return ()
        for p in paths:
            m = _mtime(p)
            if m is not None:
                sig.append((p.name, m))
        return tuple(sig)

    def _refresh(self) -> None:
        with self._lock:
            sig = self._days_signature()
            if sig != self._days_sig:
                self._load_days()
                self._days_sig = sig

            psig = _mtime(self.data_dir / "personas.json")
            if psig != self._personas_sig:
                self._personas = self._load_list(self.data_dir / "personas.json", _personas_adapter)
                self._personas_sig = psig

            isig = _mtime(self.data_dir / "inputs.json")
            if isig != self._inputs_sig:
                self._inputs = self._load_list(self.data_dir / "inputs.json", _inputs_adapter)
                self._inputs_sig = isig

    def _load_days(self) -> None:
        by_date: dict[date, Day] = {}
        try:
            paths = sorted(self._days_dir.glob("*.json"))
        except OSError:
            paths = []
        for path in paths:
            try:
                day = Day.model_validate_json(path.read_bytes())
            except (OSError, ValidationError, ValueError) as exc:
                log.warning("skipping invalid day file %s: %s", path, exc)
                continue
            if day.publication_status == "pending":
                continue
            if day.publication_status == "published" and not day.spotify_playlist_id:
                log.warning("skipping published day without playlist ID: %s", path)
                continue
            if day.date in by_date:
                log.warning("skipping %s: duplicate date %s", path, day.date)
                continue
            by_date[day.date] = day
        self._by_date = by_date
        self._days = sorted(by_date.values(), key=lambda d: d.date, reverse=True)

    @staticmethod
    def _load_list[T: BaseModel](path: Path, adapter: TypeAdapter[list[T]]) -> list[T]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            log.warning("skipping invalid file %s: %s", path, exc)
            return []
        if not isinstance(raw, list):
            log.warning("skipping %s: expected a JSON list", path)
            return []
        items: list[T] = []
        for i, entry in enumerate(raw):
            try:
                items.extend(adapter.validate_python([entry]))
            except ValidationError as exc:
                log.warning("skipping entry %d in %s: %s", i, path, exc)
        return items

    # ---- days ----------------------------------------------------------------------

    def latest_day(self) -> Day | None:
        self._refresh()
        return self._days[0] if self._days else None

    def get_day(self, d: date) -> Day | None:
        self._refresh()
        return self._by_date.get(d)

    def list_days(self, dj: str | None = None, month: str | None = None) -> list[Day]:
        self._refresh()
        return [
            day
            for day in self._days
            if (dj is None or day.dj == dj)
            and (month is None or day.date.strftime("%Y-%m") == month)
        ]

    def months(self, dj: str | None = None) -> list[str]:
        seen: list[str] = []
        for day in self.list_days(dj=dj):
            m = day.date.strftime("%Y-%m")
            if m not in seen:
                seen.append(m)
        return seen

    def prev_next(self, d: date) -> tuple[Day | None, Day | None]:
        """(previous/older day, next/newer day) relative to ``d`` among all days."""
        self._refresh()
        older = next((day for day in self._days if day.date < d), None)
        newer = next((day for day in reversed(self._days) if day.date > d), None)
        return older, newer

    # ---- personas & inputs -----------------------------------------------------------

    def personas(self) -> list[Persona]:
        self._refresh()
        return list(self._personas)

    def persona(self, persona_id: str) -> Persona | None:
        return next((p for p in self.personas() if p.id == persona_id), None)

    def input_descriptions(self) -> list[InputDescription]:
        self._refresh()
        return list(self._inputs)

    def resident_stats(self, persona_id: str) -> tuple[int, date | None]:
        days = self.list_days(dj=persona_id)
        return len(days), (days[0].date if days else None)
