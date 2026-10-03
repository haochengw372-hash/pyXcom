"""Explicit recovery of derived views, with immutable external evidence.

A recovery generation proves retained bytes and table semantics. It does not
reconstruct missing source records, infer cursors, or certify historical scope.
The recovery lock serializes this API only; legacy collectors and sync clients
must be stopped or coordinated by the caller before applying a generation.
"""

import hashlib
import importlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, gettempdir
from typing import Iterator

from ._persistence import atomic_json
from .errors import IntegrityError
from .transport import now_utc


def _files(directory: Path) -> dict[str, str]:
    """Hash a complete regular-file inventory; reject ambiguous symlink inputs."""
    if not directory.is_dir() or directory.is_symlink():
        raise IntegrityError("Recovery requires a regular collection directory")
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise IntegrityError("Recovery cannot follow symbolic links")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        elif not path.is_dir():
            raise IntegrityError("Recovery cannot snapshot special files")
    return result


def _private(inventory: dict[str, str]) -> dict[str, str]:
    return {
        name: digest
        for name, digest in inventory.items()
        if name.startswith(".pyxcom/")
    }


def _external(source: Path, destination: Path) -> None:
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise IntegrityError("Recovery evidence must be outside the collection tree")


def _assessment(output: Path, **kwargs) -> dict:
    from .integrity import assess_recovery

    plan = assess_recovery(output, **kwargs)
    if plan.get("allowed") is not True:
        raise IntegrityError(
            "Recovery assessment blocked: " + str(plan.get("errors", []))
        )
    return plan


def _modern(output: Path) -> None:
    if not (output / ".pyxcom" / "posts.jsonl").is_file():
        raise IntegrityError("Recovery requires normalized post collection sources")
    if (output / "network_manifest.json").exists() or (output / "tables").exists():
        raise IntegrityError("Recovery does not apply to network or legacy layouts")


def _validate(directory: Path) -> None:
    from .validate import validate_collection
    from .validate_tables import validate_tables

    for validator in (validate_collection, validate_tables):
        result = validator(directory)
        if result.get("valid") is not True:
            raise IntegrityError(
                "Recovery validation failed: " + str(result.get("errors", []))
            )


def _semantics(directory: Path, receipt: dict) -> None:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for key in ("query", "complete", "reason", "pages_fetched"):
        if manifest.get(key) != receipt.get(key):
            raise IntegrityError("Recovery generation semantics changed: " + key)


def _check_stable(directory: Path) -> dict[str, str]:
    first = _files(directory)
    _validate(directory)
    if _files(directory) != first:
        raise IntegrityError("Recovery files changed during validation")
    _validate(directory)
    if _files(directory) != first:
        raise IntegrityError("Recovery files changed during second validation")
    return first


def prepare_recovery(
    output_dir: str | Path,
    *,
    plan: dict | None = None,
    generation_dir: str | Path,
    expected_query: dict | None = None,
    binding: dict | None = None,
    previous_receipt: dict | None = None,
) -> dict:
    """Stage a new external generation; never modify or apply to the source."""
    source = Path(output_dir).expanduser().resolve()
    destination = Path(generation_dir).expanduser().resolve()
    _external(source, destination)
    _modern(source)
    assessed = _assessment(
        source,
        expected_query=expected_query,
        binding=binding,
        previous_receipt=previous_receipt,
    )
    initial = _files(source)
    if initial != assessed.get("input_sha256"):
        raise IntegrityError("Source changed after recovery assessment")
    if plan is not None and (
        plan.get("allowed") is not True
        or plan.get("input_sha256") != initial
        or plan.get("query") != assessed.get("query")
        or plan.get("binding") != assessed.get("binding")
    ):
        raise IntegrityError("Recovery plan is stale or incompatible")
    identifier = "generation-" + uuid.uuid4().hex
    generation = destination / identifier
    generation.mkdir(parents=True, exist_ok=False)
    status_path = generation / "status.json"
    atomic_json(status_path, {"status": "preparing", "generation_id": identifier})
    try:
        shutil.copytree(source, generation / "original")
        shutil.copytree(generation / "original", generation / "data")
        if (
            _files(generation / "original") != initial
            or _files(generation / "data") != initial
        ):
            raise IntegrityError("Source copy does not match assessment")
        if _files(source) != initial:
            raise IntegrityError("Source changed during recovery snapshot")
        from .tables import _export_tables_unchecked

        _export_tables_unchecked(generation / "data")
        output = _check_stable(generation / "data")
        if _private(output) != _private(initial):
            raise IntegrityError("Recovery export modified private source bytes")
        receipt = {
            "schema_version": 1,
            "generation_id": identifier,
            "source_dir": str(source),
            "input_sha256": initial,
            "source_sha256": _private(initial),
            "output_sha256": output,
            "query": assessed.get("query"),
            "binding": assessed.get("binding"),
            "complete": assessed.get("complete"),
            "reason": assessed.get("reason"),
            "pages_fetched": assessed.get("pages_fetched"),
            "package_version": assessed.get("package_version"),
            "prepared_at_utc": now_utc(),
        }
        _semantics(generation / "data", receipt)
        if _files(source) != initial:
            raise IntegrityError("Source advanced while preparing recovery")
        atomic_json(status_path, {"status": "ready", "generation_id": identifier})
        atomic_json(generation / "receipt.json", receipt)
        return {
            **receipt,
            "ready": True,
            "generation_dir": str(generation),
            "receipt_path": str(generation / "receipt.json"),
        }
    except BaseException:
        (generation / "receipt.json").unlink(missing_ok=True)
        atomic_json(status_path, {"status": "failed", "generation_id": identifier})
        raise


def verify_generation(generation_dir: str | Path) -> dict:
    """Verify an external generation without repairing or writing its contents."""
    generation = Path(generation_dir).expanduser().resolve()
    try:
        receipt = json.loads((generation / "receipt.json").read_text(encoding="utf-8"))
        status = json.loads((generation / "status.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not isinstance(status, dict):
            raise IntegrityError("Malformed generation receipt or status")
        required = {
            "schema_version",
            "generation_id",
            "source_dir",
            "input_sha256",
            "source_sha256",
            "output_sha256",
            "query",
            "binding",
            "complete",
            "reason",
            "pages_fetched",
            "package_version",
            "prepared_at_utc",
        }
        if not required <= receipt.keys() or not isinstance(receipt["source_dir"], str):
            raise IntegrityError("Incomplete generation receipt")
        for key in ("input_sha256", "source_sha256", "output_sha256"):
            inventory = receipt[key]
            if not isinstance(inventory, dict) or not all(
                isinstance(name, str)
                and isinstance(digest, str)
                and len(digest) == 64
                and set(digest) <= set("0123456789abcdef")
                and not Path(name).is_absolute()
                and ".." not in Path(name).parts
                for name, digest in inventory.items()
            ):
                raise IntegrityError("Malformed generation inventory: " + key)
        if (
            receipt.get("schema_version") != 1
            or receipt.get("generation_id") != generation.name
        ):
            raise IntegrityError("Unknown or mismatched generation receipt")
        if status != {"status": "ready", "generation_id": generation.name}:
            raise IntegrityError("Generation is not ready")
        original = _files(generation / "original")
        if original != receipt.get("input_sha256"):
            raise IntegrityError("Generation original evidence differs from receipt")
        actual = _check_stable(generation / "data")
        if actual != receipt.get("output_sha256"):
            raise IntegrityError("Generation output differs from receipt")
        if _private(original) != receipt.get("source_sha256") or _private(
            actual
        ) != _private(original):
            raise IntegrityError("Generation source evidence changed")
        _semantics(generation / "data", receipt)
        return {
            "valid": True,
            "errors": [],
            "receipt": receipt,
            "generation_dir": str(generation),
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "errors": [str(exc)], "generation_dir": str(generation)}


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as stream:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise IntegrityError("Recovery writer lock is busy") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise IntegrityError("Recovery writer lock is busy") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = None
    try:
        with NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_recovery(
    output_dir: str | Path, *, generation_dir: str | Path, receipt_dir: str | Path
) -> dict:
    """Explicitly replace derived public files after a fresh exact-input check.

    No private source file is rewritten. An interrupted multi-file replacement
    may leave mixed derived files; no commit receipt is emitted in that case.
    Callers must prevent other writers, including collectors and sync services.
    """
    source = Path(output_dir).expanduser().resolve()
    generation = Path(generation_dir).expanduser().resolve()
    evidence = Path(receipt_dir).expanduser().resolve()
    _external(source, generation)
    _external(source, evidence)
    _external(generation, evidence)
    _modern(source)
    verified = verify_generation(generation)
    if not verified["valid"]:
        raise IntegrityError("Generation cannot be applied: " + str(verified["errors"]))
    receipt = verified["receipt"]
    if receipt["source_dir"] != str(source):
        raise IntegrityError("Generation belongs to a different collection")
    evidence.mkdir(parents=True, exist_ok=True)
    scope = hashlib.sha256(str(source).encode()).hexdigest()
    lock_root = Path(gettempdir()) / "pyxcom-recovery-locks"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    if lock_root.is_symlink():
        raise IntegrityError("Unsafe recovery lock directory")
    with _writer_lock(lock_root / (scope + ".lock")):
        if _files(source) != receipt["input_sha256"]:
            raise IntegrityError("Current source differs from recovery input")
        assessment = _assessment(
            source, expected_query=receipt["query"], binding=receipt["binding"]
        )
        if (
            assessment.get("query") != receipt["query"]
            or assessment.get("binding") != receipt["binding"]
            or assessment.get("input_sha256") != receipt["input_sha256"]
        ):
            raise IntegrityError("Recovery scope or source changed")
        application = evidence / (
            receipt["generation_id"] + "-apply-" + uuid.uuid4().hex
        )
        application.mkdir(exist_ok=False)
        shutil.copytree(source, application / "original")
        if (
            _files(application / "original") != receipt["input_sha256"]
            or _files(source) != receipt["input_sha256"]
        ):
            raise IntegrityError("Source changed while preserving application evidence")
        data = generation / "data"
        names = {
            name for name in receipt["output_sha256"] if not name.startswith(".pyxcom/")
        }
        derived = {
            "users.csv",
            "posts.csv",
            "comments.csv",
            "profile_snapshots.csv",
            "interactions.csv",
            "context_posts.csv",
            "post_edges.csv",
            "metric_snapshots.csv",
            "manifest.json",
        }
        removed = set(receipt["input_sha256"]) - set(receipt["output_sha256"])
        if not removed <= derived:
            raise IntegrityError("Generation removes an unsupported source file")
        if _files(data) != receipt["output_sha256"]:
            raise IntegrityError("Generation changed before application")
        for name in sorted(names):
            if name not in derived:
                if receipt["input_sha256"].get(name) != receipt["output_sha256"][name]:
                    raise IntegrityError(
                        "Generation modifies an unsupported public file"
                    )
                continue
            if name != "manifest.json":
                _atomic_bytes(source / name, (data / name).read_bytes())
        for name in removed:
            (source / name).unlink(missing_ok=True)
        if _private(_files(source)) != receipt["source_sha256"]:
            raise IntegrityError("Source advanced during recovery apply")
        _atomic_bytes(source / "manifest.json", (data / "manifest.json").read_bytes())
        actual = _check_stable(source)
        if actual != receipt["output_sha256"]:
            raise IntegrityError("Applied generation differs from prepared output")
        _semantics(source, receipt)
        if _private(actual) != receipt["source_sha256"]:
            raise IntegrityError("Private source changed during recovery apply")
        applied = {
            **receipt,
            "status": "committed",
            "applied_at_utc": now_utc(),
            "application_dir": str(application),
        }
        atomic_json(application / "receipt.json", applied)
        atomic_json(evidence / (scope + "-current.json"), applied)
        return {
            **applied,
            "applied": True,
            "receipt_path": str(application / "receipt.json"),
        }
