"""Stable-ID profile views with retained, source-labelled time observations."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from .layout import internal_dir, source_path

SNAPSHOT_FIELDS = [
    "snapshot_id",
    "user_id",
    "username",
    "captured_at_utc",
    "time_status",
    "source_file",
    "source_key",
    "profile_json",
]
PROFILE_POLICY = "latest_valid_capture_then_completeness_then_canonical_json"


def _time(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _snapshot(entry: dict, source_file: str, source_key: str) -> dict:
    identifier = entry.get("id")
    if (
        identifier is None
        or isinstance(identifier, bool)
        or not str(identifier).strip()
    ):
        raise ValueError("Profile ID must be nonempty")
    encoded = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    identity = json.dumps(
        [source_file, source_key, entry], sort_keys=True, ensure_ascii=False
    )
    return {
        "snapshot_id": hashlib.sha256(identity.encode()).hexdigest(),
        "user_id": str(identifier),
        "username": entry.get("handle"),
        "captured_at_utc": entry.get("captured_at_utc"),
        "time_status": "observed" if _time(entry.get("captured_at_utc")) else "unknown",
        "source_file": source_file,
        "source_key": source_key,
        "profile_json": encoded,
    }


def _entries(payload, source_file: str) -> list[dict]:
    if isinstance(payload, list):
        pairs = [(str(i), entry) for i, entry in enumerate(payload)]
    elif isinstance(payload, dict):
        pairs = [("$", payload)] if "id" in payload else list(payload.items())
    else:
        raise ValueError("Invalid profile snapshot container")
    if any(not isinstance(entry, dict) for _, entry in pairs):
        raise ValueError("Invalid profile snapshot record")
    return [_snapshot(entry, source_file, str(key)) for key, entry in pairs]


def _read_snapshots(directory: Path) -> list[dict]:
    snapshots = {}
    ledger = source_path(directory, "profile_observations.jsonl")
    if ledger.exists():
        # JSONL records end at LF. Unicode separators belong to JSON strings.
        for line in ledger.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                row = json.loads(line)
                verified = _snapshot(
                    json.loads(row["profile_json"]),
                    row["source_file"],
                    row["source_key"],
                )
                if row != verified:
                    raise ValueError("Invalid profile observation metadata")
                snapshots[row["snapshot_id"]] = row
    path = source_path(directory, "profiles.json")
    if path.exists():
        for row in _entries(
            json.loads(path.read_text(encoding="utf-8")),
            str(path.relative_to(directory)),
        ):
            snapshots[row["snapshot_id"]] = row
    return sorted(snapshots.values(), key=lambda row: row["snapshot_id"])


def _views(snapshots: list[dict]) -> tuple[dict[str, dict], dict]:
    grouped: dict[str, list[dict]] = {}
    for row in snapshots:
        grouped.setdefault(row["user_id"], []).append(json.loads(row["profile_json"]))
    selected = {}
    tied = []
    for identifier, entries in sorted(grouped.items()):
        creation_times = set()
        for entry in entries:
            value = entry.get("created_at_utc")
            if value:
                created = _time(value)
                if created is None:
                    raise ValueError(
                        f"Invalid profile creation timestamp: {identifier}"
                    )
                creation_times.add(created)
        if len(creation_times) > 1:
            raise ValueError(
                f"Conflicting profile identity: {identifier} (created_at_utc)"
            )

        def rank(entry):
            return (
                _time(entry.get("captured_at_utc"))
                or datetime.min.replace(tzinfo=timezone.utc),
                sum(value is not None and value != "" for value in entry.values()),
                json.dumps(entry, sort_keys=True, ensure_ascii=False),
            )

        latest = max(entries, key=rank)
        latest_time = _time(latest.get("captured_at_utc"))
        variants = {
            json.dumps(e, sort_keys=True, ensure_ascii=False)
            for e in entries
            if _time(e.get("captured_at_utc")) == latest_time
        }
        if len(variants) > 1:
            tied.append(identifier)
        selected[identifier] = {**latest, "id": identifier}
    return selected, {
        "count": len(snapshots),
        "user_count": len(grouped),
        "policy": PROFILE_POLICY,
        "unknown_time_observations": sum(
            row["time_status"] == "unknown" for row in snapshots
        ),
        "tied_latest_user_ids": tied,
    }


def profile_views(output_dir: str | Path) -> tuple[dict[str, dict], list[dict], dict]:
    snapshots = _read_snapshots(Path(output_dir).expanduser())
    selected, report = _views(snapshots)
    return selected, snapshots, report


def save_profiles(output_dir: str | Path, payload: dict) -> None:
    """Archive old and incoming snapshots before replacing a handle-keyed view."""
    directory = Path(output_dir).expanduser()
    private = internal_dir(directory)
    private.mkdir(parents=True, exist_ok=True)
    path = source_path(directory, "profiles.json")
    snapshots = {row["snapshot_id"]: row for row in _read_snapshots(directory)}
    for row in _entries(payload, str(path.relative_to(directory))):
        snapshots[row["snapshot_id"]] = row
    _views(list(snapshots.values()))  # Fail on actual identity conflicts before writes.
    ledger = source_path(directory, "profile_observations.jsonl")
    saved = set()
    if ledger.exists():
        saved = {
            json.loads(line)["snapshot_id"]
            for line in ledger.read_text(encoding="utf-8").split("\n")
            if line.strip()
        }
    with ledger.open("a", encoding="utf-8") as stream:
        for key, row in sorted(snapshots.items()):
            if key not in saved:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)
