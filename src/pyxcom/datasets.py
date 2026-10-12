"""Verified, text-free post-ID datasets for research sharing."""

from contextlib import ExitStack
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Iterable, Iterator

from ._pool_locks import ScopeLock
from .errors import IntegrityError
from .integrity import assess_recovery
from .models import Post

_SCHEMA = "pyxcom.post-ids/1"
_ROLES = {"main", "comment", "repost"}
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {
    "schema",
    "package_version",
    "file",
    "sha256",
    "count",
    "source_fingerprint",
    "source_records",
    "roles",
    "exclusions",
    "collection_complete",
    "coverage_complete",
    "content_license",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and int(value) < 2**64


def _roles(roles: Iterable[str] | None) -> list[str]:
    values = sorted(_ROLES if roles is None else set(roles))
    if not values or any(value not in _ROLES for value in values):
        raise ValueError("roles must contain main, comment, or repost")
    return values


def _directory(path: str | Path) -> Path:
    original = Path(path).expanduser()
    if original.is_symlink():
        raise IntegrityError("Dataset directory must not be a symlink")
    return original.resolve()


def export_post_ids(
    output_dir: str | Path,
    destination: str | Path,
    *,
    roles: Iterable[str] | None = None,
) -> dict:
    """Export analysis and seed IDs from a verified collection into a new directory.

    The source is read-only. Context records are excluded; relation targets are
    never invented as retained posts. Both scopes are locked for the full export.
    This manifest grants no license to the underlying posts.
    """
    from . import __version__

    selected_roles = _roles(roles)
    source, target = _directory(output_dir), _directory(destination)
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Source and destination scopes must not overlap")
    with ExitStack() as stack:
        for scope in sorted((source, target)):
            stack.enter_context(ScopeLock(scope))
        if target.exists():
            raise FileExistsError("Dataset destination already exists")
        canonical = source / ".pyxcom" / "posts.jsonl"
        if not canonical.is_file() or canonical.is_symlink():
            raise IntegrityError("A canonical post collection is required")
        report = assess_recovery(source)
        if not report["allowed"] or report["record_integrity"] != "verified":
            raise IntegrityError(
                "Source collection did not pass integrity verification"
            )
        raw = canonical.read_bytes()
        if _digest(raw) != report["source_sha256"].get(".pyxcom/posts.jsonl"):
            raise IntegrityError("Source changed during verification")
        ids: set[str] = set()
        exclusions = {"context_records": 0, "other_roles": 0, "duplicate_ids": 0}
        source_records = 0
        try:
            for line in raw.decode("utf-8").split("\n"):
                if not line.strip():
                    continue
                post = Post(**json.loads(line))
                source_records += 1
                if not _valid_id(post.id):
                    raise ValueError("Invalid post ID")
                if post.observation_role not in {"analysis", "seed"}:
                    if post.observation_role != "context":
                        raise ValueError("Unknown observation role")
                    exclusions["context_records"] += 1
                elif post.post_role not in selected_roles:
                    exclusions["other_roles"] += 1
                elif post.id in ids:
                    exclusions["duplicate_ids"] += 1
                else:
                    ids.add(post.id)
        except (UnicodeError, ValueError, TypeError, KeyError):
            raise IntegrityError(
                "Canonical records contain an invalid ID or role"
            ) from None
        content = (
            "post_id\n" + "".join(f"{value}\n" for value in sorted(ids, key=int))
        ).encode()
        manifest = {
            "schema": _SCHEMA,
            "package_version": __version__,
            "file": "post_ids.csv",
            "sha256": _digest(content),
            "count": len(ids),
            "source_fingerprint": _digest(raw),
            "source_records": source_records,
            "roles": selected_roles,
            "exclusions": exclusions,
            "collection_complete": report["complete"] is True,
            "coverage_complete": False,
            "content_license": "not_granted",
        }
        # Exclusive mkdir refuses even an empty existing directory. An interrupted
        # export lacks a valid manifest and cannot pass validate_id_dataset().
        target.mkdir(parents=True, exist_ok=False)
        try:
            with (target / "post_ids.csv").open("xb") as handle:
                handle.write(content)
            with (target / "manifest.json").open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, sort_keys=True, indent=2)
                handle.write("\n")
        except BaseException:
            for name in ("manifest.json", "post_ids.csv"):
                (target / name).unlink(missing_ok=True)
            target.rmdir()
            raise
        return manifest


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def _read_dataset(directory: Path) -> tuple[dict, list[str]]:
    try:
        if not directory.is_dir() or {path.name for path in directory.iterdir()} != {
            "manifest.json",
            "post_ids.csv",
        }:
            raise ValueError("Unexpected dataset contents")
        if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
            raise ValueError("Invalid dataset file")
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
        if not isinstance(manifest, dict) or set(manifest) != _FIELDS:
            raise ValueError("Invalid manifest fields")
        if manifest["schema"] != _SCHEMA or manifest["file"] != "post_ids.csv":
            raise ValueError("Unsupported dataset schema")
        if not isinstance(manifest["package_version"], str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]+)?", manifest["package_version"]
        ):
            raise ValueError("Invalid package version")
        for key in ("sha256", "source_fingerprint"):
            if not isinstance(manifest[key], str) or not _HASH.fullmatch(manifest[key]):
                raise ValueError("Invalid fingerprint")
        for key in ("count", "source_records"):
            if type(manifest[key]) is not int or manifest[key] < 0:
                raise ValueError("Invalid count")
        if (
            type(manifest["collection_complete"]) is not bool
            or manifest["coverage_complete"] is not False
        ):
            raise ValueError("Invalid coverage claim")
        if manifest["content_license"] != "not_granted":
            raise ValueError("Invalid content license claim")
        if not isinstance(manifest["roles"], list) or manifest["roles"] != _roles(
            manifest["roles"]
        ):
            raise ValueError("Invalid roles")
        excluded = manifest["exclusions"]
        if (
            not isinstance(excluded, dict)
            or set(excluded)
            != {
                "context_records",
                "other_roles",
                "duplicate_ids",
            }
            or any(type(value) is not int or value < 0 for value in excluded.values())
        ):
            raise ValueError("Invalid exclusions")
        if manifest["count"] + sum(excluded.values()) != manifest["source_records"]:
            raise ValueError("Inconsistent source count")
        data = (directory / "post_ids.csv").read_bytes()
        if _digest(data) != manifest["sha256"]:
            raise ValueError("ID file hash mismatch")
        rows = list(
            csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True)
        )
        if not rows or rows[0] != ["post_id"]:
            raise ValueError("Invalid ID column")
        ids = []
        for row in rows[1:]:
            if len(row) != 1 or not _valid_id(row[0]):
                raise ValueError("Invalid ID row")
            ids.append(row[0])
        if len(ids) != manifest["count"] or ids != sorted(set(ids), key=int):
            raise ValueError("ID count, uniqueness, or ordering mismatch")
        if data != ("post_id\n" + "".join(f"{value}\n" for value in ids)).encode():
            raise ValueError("ID file is not canonical UTF-8 CSV")
        return manifest, ids
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, csv.Error):
        raise IntegrityError(
            "ID dataset failed schema or integrity validation"
        ) from None


def validate_id_dataset(path: str | Path) -> dict:
    """Validate the package's two-file ID format; raise IntegrityError on failure."""
    directory = _directory(path)
    with ScopeLock(directory):
        manifest, _ = _read_dataset(directory)
    return {"valid": True, **manifest}


def iter_post_ids(path: str | Path) -> Iterator[str]:
    """Yield verified IDs; validate the entire file before yielding any item."""
    directory = _directory(path)
    with ScopeLock(directory):
        _, ids = _read_dataset(directory)
    yield from ids
