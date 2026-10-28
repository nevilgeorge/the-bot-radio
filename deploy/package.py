"""Build explicit deployment archives without secrets, environments, or repository data."""

import json
import sys
import tarfile
from datetime import date
from pathlib import Path

SOURCE_SUFFIXES = {
    ".py",
    ".html",
    ".css",
    ".js",
    ".svg",
    ".png",
    ".jpg",
    ".webp",
    ".json",
    ".jsonl",
}


def add_file(archive: tarfile.TarFile, source: Path, name: str) -> None:
    """Add a regular file only, rejecting links rather than following them."""
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Expected a regular deployment file: {source}")
    archive.add(source, arcname=name, recursive=False)


def package_application(repository_root: Path, output_directory: Path) -> None:
    """Package build metadata and allowlisted application source/assets."""
    with tarfile.open(output_directory / "application.tar.gz", "w:gz") as archive:
        for filename in ("pyproject.toml", "uv.lock", "README.md", ".python-version"):
            add_file(archive, repository_root / filename, filename)
        source_root = repository_root / "src"
        for source_path in sorted(source_root.rglob("*")):
            relative_path = source_path.relative_to(repository_root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative_path.parts):
                continue
            if source_path.is_symlink():
                raise ValueError(f"Symlink in application source: {source_path}")
            if source_path.is_file() and source_path.suffix in SOURCE_SUFFIXES:
                add_file(archive, source_path, relative_path.as_posix())


def package_seed(data_directory: Path, output_directory: Path) -> None:
    """Stage only descriptions and valid date-named daily JSON, with no private run files."""
    with tarfile.open(output_directory / "seed.tar.gz", "w:gz") as archive:
        seed_paths = [data_directory / "inputs.json"]
        days_directory = data_directory / "days"
        if data_directory.is_symlink() or days_directory.is_symlink():
            raise ValueError("Seed directories must not be symlinks")
        for daily_path in sorted(days_directory.glob("????-??-??.json")):
            if date.fromisoformat(daily_path.stem).isoformat() != daily_path.stem:
                raise ValueError(f"Invalid daily filename: {daily_path.name}")
            seed_paths.append(daily_path)
        for source_path in seed_paths:
            if source_path.is_symlink():
                raise ValueError(f"Symlink in seed: {source_path}")
            with source_path.open() as source_file:
                json.load(source_file)
            add_file(archive, source_path, source_path.relative_to(data_directory).as_posix())


if __name__ == "__main__":
    repository_root, output_directory = Path(sys.argv[1]), Path(sys.argv[2])
    package_application(repository_root, output_directory)
    if sys.argv[3] == "true":
        package_seed(Path(sys.argv[4]), output_directory)
