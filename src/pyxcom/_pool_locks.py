"""Process locks outside datasets, including ancestor/descendant exclusion."""

import hashlib
import os
from pathlib import Path
import tempfile
from threading import Lock
from typing import Any

from .errors import PyXcomError


class ScopeBusyError(PyXcomError, RuntimeError):
    """Another writer currently owns this output scope."""


class FileLock:
    """Hold an OS lock until close; never remove an acquired lock inode."""

    def __init__(self, path: Path, *, shared: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class Overlapped(ctypes.Structure):
                    _fields_ = [
                        ("Internal", ctypes.c_size_t),
                        ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD),
                        ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE),
                    ]

                windows: Any = msvcrt
                self._overlapped = Overlapped()
                ctypes_api: Any = ctypes
                kernel = ctypes_api.WinDLL("kernel32", use_last_error=True)
                acquire = kernel.LockFileEx
                acquire.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(Overlapped),
                ]
                acquire.restype = wintypes.BOOL
                if not acquire(
                    windows.get_osfhandle(self.file.fileno()),
                    1 if shared else 3,
                    0,
                    1,
                    0,
                    ctypes.byref(self._overlapped),
                ):
                    raise OSError("Lock unavailable")
            else:
                import fcntl

                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(self.file.fileno(), mode | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ScopeBusyError(
                "Collection scope already has an active writer"
            ) from None

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# Only shared ancestors are cached. Exclusive leaves always use a fresh OS lock,
# preserving conflicts even when two callers run in the same Python process.
_shared_guard = Lock()
_shared_locks: dict[Path, tuple[FileLock, int]] = {}


class _SharedLease:
    def __init__(self, path: Path):
        self.path = path
        self.pid = os.getpid()
        self.closed = False
        with _shared_guard:
            entry = _shared_locks.get(path)
            if entry is None:
                _shared_locks[path] = (FileLock(path, shared=True), 1)
            else:
                lock, references = entry
                _shared_locks[path] = (lock, references + 1)

    def close(self):
        with _shared_guard:
            if self.closed or self.pid != os.getpid():
                return
            self.closed = True
            lock, references = _shared_locks[self.path]
            if references == 1:
                lock.close()
                del _shared_locks[self.path]
            else:
                _shared_locks[self.path] = (lock, references - 1)


def _after_fork():
    # The child needs its own guard and shared lock descriptions. Closing its
    # inherited descriptors does not release the parent's open descriptions.
    global _shared_guard
    _shared_guard = Lock()
    for lock, _ in _shared_locks.values():
        lock.close()
    _shared_locks.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class ScopeLock:
    """Exclusive leaf and shared ancestors allow siblings but reject overlaps.

    Canonical paths make symlink aliases conflict. Lock files live outside the
    dataset; process exit releases locks automatically, including after a crash.
    Shared ancestors reuse one file descriptor per process with reference counts.
    """

    def __init__(self, output_path: str | Path):
        self.path = Path(output_path).expanduser().resolve()
        self._locks: list[FileLock | _SharedLease] = []
        identity = (
            str(os.getuid())
            if hasattr(os, "getuid")
            else os.environ.get("USERNAME", "user")
        )
        registry = Path(tempfile.gettempdir()) / f"pyxcom-scope-locks-{identity}"
        if registry.is_symlink():
            raise ValueError("Scope lock registry must not be a symlink")
        registry.mkdir(mode=0o700, parents=True, exist_ok=True)
        if hasattr(os, "getuid") and registry.stat().st_uid != os.getuid():
            raise ValueError("Scope lock registry has another owner")
        try:
            for path in [*reversed(self.path.parents), self.path]:
                key = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()
                lock_path = registry / f"{key}.lock"
                self._locks.append(
                    FileLock(lock_path)
                    if path == self.path
                    else _SharedLease(lock_path)
                )
        except BaseException:
            self.close()
            raise

    def close(self):
        for lock in reversed(self._locks):
            lock.close()
        self._locks.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
