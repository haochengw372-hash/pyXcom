"""Stable public tables and private, resumable collection artifacts."""

import csv
import json
import shutil
from pathlib import Path

_INTERNAL_FILES = (
    "posts.jsonl",
    "state.json",
    "profiles.json",
    "account_manifest.json",
    "report.md",
    "schema.json",
    "schema_report.md",
    "posts.jsonl.before-role-schema.gz",
)


def internal_dir(output: str | Path) -> Path:
    return Path(output).expanduser() / ".pyxcom"


def source_path(output: str | Path, name: str) -> Path:
    private = internal_dir(output) / name
    legacy = Path(output).expanduser() / name
    return private if private.exists() or not legacy.exists() else legacy


def child_dir(output: str | Path, name: str) -> Path:
    return internal_dir(output) / name


def _same_contents(left: Path, right: Path) -> bool:
    """Compare every directory entry and file byte before reusing a backup."""
    if left.is_symlink() or right.is_symlink():
        return False
    if left.is_dir() and right.is_dir():
        left_entries = {entry.name: entry for entry in left.iterdir()}
        right_entries = {entry.name: entry for entry in right.iterdir()}
        return left_entries.keys() == right_entries.keys() and all(
            _same_contents(entry, right_entries[name])
            for name, entry in left_entries.items()
        )
    return (
        left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes()
    )


def _move_preserving(source: Path, target: Path, backup: Path) -> None:
    if not source.exists():
        return
    if target.exists():
        if (
            source.is_dir()
            or target.is_dir()
            or source.read_bytes() != target.read_bytes()
        ):
            raise ValueError(f"Conflicting migration paths: {source} and {target}")
    if backup.exists():
        if not _same_contents(source, backup):
            raise ValueError(f"Conflicting legacy backup: {backup}")
    else:
        backup.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, backup)
        else:
            shutil.copy2(source, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        source.unlink()
    else:
        source.rename(target)


def migrate_collection(output: str | Path) -> Path:
    """Move recognized legacy artifacts, preserving backups and unrelated files.

    Repeated calls are safe. Conflicting destinations raise rather than overwrite.
    Normalized root tables are never mistaken for legacy mixed observations.
    """
    directory = Path(output).expanduser()
    private = internal_dir(directory)
    private.mkdir(parents=True, exist_ok=True)
    backup = private / "legacy"
    for name in _INTERNAL_FILES:
        _move_preserving(directory / name, private / name, backup / name)
    root_manifest = directory / "manifest.json"
    if root_manifest.exists():
        data = json.loads(root_manifest.read_text(encoding="utf-8"))
        if "post_count" in data and "counts" not in data:
            _move_preserving(
                root_manifest, private / "manifest.json", backup / "manifest.json"
            )
    root_csv = directory / "posts.csv"
    if root_csv.exists():
        with root_csv.open(encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), [])
        if "id" in header and ("post_role" in header or "author_handle" in header):
            _move_preserving(root_csv, private / "posts.csv", backup / "posts.csv")
    # Only folders with a collection checkpoint are owned collection scopes.
    for folder in list(directory.iterdir()):
        if (
            folder.is_dir()
            and folder.name not in {".pyxcom", "tables"}
            and (folder / "state.json").exists()
        ):
            _move_preserving(folder, private / folder.name, backup / folder.name)
    old_tables = directory / "tables"
    if (old_tables / "manifest.json").exists():
        data = json.loads((old_tables / "manifest.json").read_text(encoding="utf-8"))
        if data.get("source_kind") == "saved_post_observations":
            _move_preserving(old_tables, private / "previous_tables", backup / "tables")
    for folder in list(private.iterdir()):
        if (
            folder.is_dir()
            and folder.name not in {"legacy", "previous_tables"}
            and (
                (folder / "state.json").exists()
                or (internal_dir(folder) / "state.json").exists()
            )
        ):
            migrate_collection(folder)
    return directory
