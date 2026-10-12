"""Worker-local observer provenance and cooperative collection ownership."""

import json
import inspect
from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .errors import IntegrityError

_write_scopes: ContextVar[tuple] = ContextVar("pyxcom_write_scopes", default=())

_observer: ContextVar[dict | None] = ContextVar("pyxcom_observer", default=None)


@contextmanager
def collection_observer(observer_id, login_id, *, pool_state_dir=None, job_id=None):
    value = {
        "observer_id": str(observer_id),
        "login_id": str(login_id),
        "pool_state_dir": str(Path(pool_state_dir).expanduser().resolve())
        if pool_state_dir is not None
        else None,
        "job_id": job_id,
    }
    token = _observer.set(value)
    try:
        yield
    finally:
        _observer.reset(token)


def observer_metadata() -> dict:
    value = _observer.get()
    return (
        {"observer_id": value["observer_id"], "observer_login_id": value["login_id"]}
        if value
        else {}
    )


def _owner(output_dir):
    directory = Path(output_dir).expanduser().resolve()
    for scope in (directory, *directory.parents):
        marker = scope / ".pyxcom" / "pool-owner.json"
        if marker.exists() or marker.is_symlink() or marker.parent.is_symlink():
            break
    else:
        return None
    if marker.is_symlink() or marker.parent.is_symlink():
        raise IntegrityError(
            "Collection ownership storage must remain inside output_dir; symlinks are unsupported"
        )
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        required = {"pool_state_dir", "job_id", "login_id", "observer_id"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError
        if any(
            not isinstance(value[key], str) or not value[key]
            for key in ("pool_state_dir", "job_id")
        ) or any(
            value[key] is not None and not isinstance(value[key], str)
            for key in ("login_id", "observer_id")
        ):
            raise ValueError
        value["pool_state_dir"] = str(
            Path(value["pool_state_dir"]).expanduser().resolve()
        )
        return value
    except (ValueError, OSError) as exc:
        raise IntegrityError("Invalid collection ownership marker") from exc


def owns_collection(output_dir) -> bool:
    owner, context = _owner(output_dir), _observer.get()
    return bool(
        owner
        and context
        and all(
            owner[key] == context.get(key)
            for key in ("pool_state_dir", "job_id", "login_id", "observer_id")
        )
    )


def assert_collection_owner(output_dir) -> None:
    if _owner(output_dir) is not None and not owns_collection(output_dir):
        raise IntegrityError(
            "Collection is owned by a pool; resume through its bound job"
        )


@contextmanager
def collection_write_guard(output_dir):
    # A worker already owns the pool's lifetime scope lock. Maintenance callers
    # acquire that same lock, including when a persisted pool is currently closed.
    directory = Path(output_dir).expanduser().resolve()
    if owns_collection(directory) or any(
        directory.is_relative_to(scope) for scope in _write_scopes.get()
    ):
        yield
    else:
        from ._pool_locks import ScopeLock

        with ScopeLock(directory):
            token = _write_scopes.set((*_write_scopes.get(), directory))
            try:
                yield
            finally:
                _write_scopes.reset(token)


def guarded_collection(method):
    """Hold the same scope lock across a public saver and all nested writes."""
    signature = inspect.signature(method)

    @wraps(method)
    def wrapped(*args, **kwargs):
        output = signature.bind(*args, **kwargs).arguments["output_dir"]
        assert_collection_owner(output)
        with collection_write_guard(output):
            assert_collection_owner(output)
            return method(*args, **kwargs)

    return wrapped
