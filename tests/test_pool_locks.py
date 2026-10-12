"""Process-wide ancestor lock sharing without weakening writer exclusion."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from pyxcom import _pool_locks as locks


class ScopeLockSharingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "collection"

    def test_siblings_reuse_shared_file_descriptors_until_last_close(self):
        created = []
        real = locks.FileLock

        def create(path, *, shared=False):
            lock = real(path, shared=shared)
            created.append((lock, shared))
            return lock

        with patch.object(locks, "FileLock", side_effect=create):
            first = locks.ScopeLock(self.root / "a")
            before = len(created)
            second = locks.ScopeLock(self.root / "b")
            self.addCleanup(second.close)
            self.addCleanup(first.close)
            self.assertEqual(len(created) - before, 1)
            shared = [lock for lock, is_shared in created if is_shared]
            self.assertTrue(shared)
            first.close()
            self.assertTrue(all(not lock.file.closed for lock in shared))
            with self.assertRaises(locks.ScopeBusyError):
                locks.ScopeLock(self.root)
            second.close()
            self.assertTrue(all(lock.file.closed for lock in shared))
            with locks.ScopeLock(self.root):
                pass

    def test_same_leaf_is_never_reference_counted(self):
        with locks.ScopeLock(self.root):
            with self.assertRaises(locks.ScopeBusyError):
                locks.ScopeLock(self.root)

    def test_parallel_siblings_release_without_dropping_shared_protection(self):
        count = 16
        entered, release = Barrier(count + 1), Barrier(count + 1)
        failures = []

        def hold(index):
            try:
                with locks.ScopeLock(self.root / str(index)):
                    entered.wait(4)
                    release.wait(4)
            except Exception as exc:
                failures.append(exc)

        threads = [Thread(target=hold, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        try:
            entered.wait(4)
            with self.assertRaises(locks.ScopeBusyError):
                locks.ScopeLock(self.root)
        finally:
            release.wait(4)
            for thread in threads:
                thread.join(4)
        self.assertEqual(failures, [])
        with locks.ScopeLock(self.root):
            pass

    def test_subprocess_cannot_take_parent_after_one_sibling_closes(self):
        source = str(Path(locks.__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=source)
        script = """
import sys
from pyxcom._pool_locks import ScopeLock, ScopeBusyError
try:
    with ScopeLock(sys.argv[1]):
        pass
except ScopeBusyError:
    sys.exit(17)
"""
        first, second = (
            locks.ScopeLock(self.root / "a"),
            locks.ScopeLock(self.root / "b"),
        )
        self.addCleanup(second.close)
        self.addCleanup(first.close)
        first.close()
        rejected = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(rejected.returncode, 17, rejected.stderr)
        second.close()
        accepted = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)


if __name__ == "__main__":
    unittest.main()
