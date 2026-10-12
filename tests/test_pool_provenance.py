import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pyxcom import IntegrityError
from pyxcom.models import Post
from pyxcom.provenance import collection_observer, observer_metadata
from pyxcom.storage import PostStore
from pyxcom.networks import NetworkStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def owned(self, name, observer, login):
        directory = self.root / name
        private = directory / ".pyxcom"
        private.mkdir(parents=True)
        marker = {
            "pool_state_dir": str(self.root / "pool"),
            "job_id": name,
            "observer_id": observer,
            "login_id": login,
        }
        (private / "pool-owner.json").write_text(json.dumps(marker))
        return directory

    def test_direct_post_or_network_writer_is_rejected_before_mutation(self):
        for kind in ("post", "network"):
            directory = self.owned(kind, "101", "a")
            before = {
                str(p.relative_to(directory)): p.read_bytes()
                for p in directory.rglob("*")
                if p.is_file()
            }
            with self.assertRaises(IntegrityError):
                if kind == "post":
                    PostStore(directory, query={"kind": "test"})
                else:
                    NetworkStore(directory, kind="followers", object_id="1")
            self.assertEqual(
                before,
                {
                    str(p.relative_to(directory)): p.read_bytes()
                    for p in directory.rglob("*")
                    if p.is_file()
                },
            )

    def test_wrong_observer_or_job_cannot_write_owned_scope(self):
        directory = self.owned("one", "101", "a")
        for observer, job in [("202", "one"), ("101", "other")]:
            with collection_observer(
                observer, "a", pool_state_dir=self.root / "pool", job_id=job
            ):
                with self.assertRaises(IntegrityError):
                    PostStore(directory, query={"kind": "test"})

    def test_threads_keep_distinct_observers_and_context_resets(self):
        barrier = threading.Barrier(2)

        def work(name, observer, login):
            directory = self.owned(name, observer, login)
            with collection_observer(
                observer, login, pool_state_dir=self.root / "pool", job_id=name
            ):
                barrier.wait(timeout=3)
                store = PostStore(directory, query={"kind": "test"})
                store.archive_response({"data": {}}, operation="fixture", variables={})
                store.append_page(
                    [
                        Post(
                            name,
                            observer,
                            "actor",
                            "2026-01-01T00:00:00+00:00",
                            "text",
                            "https://x.com/i/status/" + name,
                        )
                    ],
                    None,
                )
                store.finish(complete=False, reason="page_limit")
            self.assertEqual(observer_metadata(), {})
            rows = [
                json.loads(line)
                for line in (directory / ".pyxcom/collection_log.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["observer_id"], observer)
                self.assertEqual(row["observer_login_id"], login)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(work, "1", "101", "a"),
                executor.submit(work, "2", "202", "b"),
            ]
            for future in futures:
                future.result()
        self.assertEqual(observer_metadata(), {})

    def test_plain_collection_does_not_gain_guessed_observer(self):
        store = PostStore(self.root / "plain", query={"kind": "test"})
        store.log(operation="fixture")
        row = json.loads((self.root / "plain/.pyxcom/collection_log.jsonl").read_text())
        self.assertNotIn("observer_id", row)


class MaintenanceLockTests(unittest.TestCase):
    def test_export_and_recovery_cannot_race_a_pool_scope(self):
        from pyxcom import export_tables, apply_recovery
        from pyxcom._pool_locks import ScopeLock

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "dataset"
            store = PostStore(directory, query={"kind": "test"})
            store.finish(complete=False, reason="page_limit")
            before = {
                str(p.relative_to(directory)): p.read_bytes()
                for p in directory.rglob("*")
                if p.is_file()
            }
            with ScopeLock(directory):
                for operation in (
                    lambda: export_tables(directory),
                    lambda: apply_recovery(
                        directory,
                        generation_dir=Path(folder) / "generation",
                        receipt_dir=Path(folder) / "receipts",
                    ),
                ):
                    with self.assertRaises((RuntimeError, ValueError)):
                        operation()
            after = {
                str(p.relative_to(directory)): p.read_bytes()
                for p in directory.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
            export_tables(directory)


class PublicSaverGuardTests(ProvenanceTests):
    def test_public_savers_refuse_owned_root_or_child_before_any_mutation(self):
        from pyxcom import XClient

        directory = self.owned("saver", "101", "a")
        client = object.__new__(XClient)
        before = {
            str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*")
            if p.is_file()
        }
        for output in (directory, directory / "nested"):
            for method in (
                client.save_user_activity,
                client.save_search_posts,
                client.save_search_query,
                client.save_followers,
            ):
                with self.assertRaises(IntegrityError):
                    method("1234567890", output)
        self.assertEqual(
            before,
            {
                str(p.relative_to(directory)): p.read_bytes()
                for p in directory.rglob("*")
                if p.is_file()
            },
        )

    def test_public_saver_participates_in_hierarchical_lock(self):
        from pyxcom import XClient
        from pyxcom._pool_locks import ScopeLock

        directory = self.root / "open"
        client = object.__new__(XClient)
        with ScopeLock(directory):
            with self.assertRaises((RuntimeError, ValueError)):
                client.save_user_activity("OpenAI", directory / "nested")
        self.assertFalse(directory.exists())
