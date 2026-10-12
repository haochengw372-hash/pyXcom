"""The public ID export cannot disclose private collection metadata."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pyxcom._pool_locks import ScopeLock, ScopeBusyError
from pyxcom.datasets import export_post_ids, iter_post_ids, validate_id_dataset
from pyxcom.errors import IntegrityError
from pyxcom.models import Post
from pyxcom.storage import PostStore


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "private-project"
        self.target = self.root / "release"

    def saved(self, complete=False):
        records = [
            Post(
                id="10",
                author_id="987654321",
                author_handle="private_author",
                created_at_utc="2026-01-01T00:00:00Z",
                text="private_original_text",
                url="https://x.com/private/status/10",
            ),
            Post(
                id="2",
                author_id="987654321",
                author_handle="private_author",
                created_at_utc="2026-01-01T00:00:00Z",
                text="private_comment",
                url="",
                in_reply_to_id="10",
                conversation_id="10",
            ),
            Post(
                id="3",
                author_id="987654321",
                author_handle="private_author",
                created_at_utc="2026-01-01T00:00:00Z",
                text="private_context",
                url="",
                observation_role="context",
            ),
        ]
        store = PostStore(
            self.source, query={"kind": "post_ids", "query": "private_query"}
        )
        store.append_page(records, None)
        store.finish(
            complete=complete, reason="exhausted" if complete else "page_limit"
        )

    def test_sorted_ids_and_private_metadata_excluded(self):
        self.saved()
        result = export_post_ids(self.source, self.target)
        self.assertEqual(list(iter_post_ids(self.target)), ["2", "10"])
        self.assertEqual(result["exclusions"]["context_records"], 1)
        self.assertFalse(result["collection_complete"])
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(validate_id_dataset(self.target)["valid"])
        text = "".join(p.read_text() for p in self.target.iterdir())
        for forbidden in (
            "private",
            "987654321",
            "author",
            "observer",
            "query",
            str(self.root),
        ):
            self.assertNotIn(forbidden, text)

    def test_roles_do_not_include_comment_or_context(self):
        self.saved(complete=True)
        result = export_post_ids(self.source, self.target, roles=["main"])
        self.assertEqual(list(iter_post_ids(self.target)), ["10"])
        self.assertEqual(result["exclusions"]["other_roles"], 1)
        self.assertTrue(result["collection_complete"])
        self.assertFalse(result["coverage_complete"])

    def test_source_corruption_and_unanchored_source_refused(self):
        self.saved()
        canonical = self.source / ".pyxcom/posts.jsonl"
        canonical.write_text(canonical.read_text() + "{}\n")
        with self.assertRaises(IntegrityError):
            export_post_ids(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_identical_duplicate_rows_are_counted_and_deduplicated(self):
        self.saved()
        canonical = self.source / ".pyxcom/posts.jsonl"
        canonical.write_text(
            canonical.read_text() + canonical.read_text().splitlines()[0] + "\n"
        )
        digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
        for name, field, key in (
            ("manifest.json", "source_sha256", ".pyxcom/posts.jsonl"),
            (".pyxcom/manifest.json", "sha256", "posts.jsonl"),
        ):
            path = self.source / name
            manifest = json.loads(path.read_text())
            manifest[field][key] = digest
            path.write_text(json.dumps(manifest))
        result = export_post_ids(self.source, self.target)
        self.assertEqual(list(iter_post_ids(self.target)), ["2", "10"])
        self.assertEqual(result["exclusions"]["duplicate_ids"], 1)
        self.assertEqual(result["source_records"], 4)

    def test_unanchored_canonical_source_refused(self):
        self.saved()
        for name in (
            "manifest.json",
            ".pyxcom/manifest.json",
            ".pyxcom/state.json",
            ".pyxcom/collection_log.jsonl",
        ):
            (self.source / name).unlink(missing_ok=True)
        with self.assertRaises(IntegrityError):
            export_post_ids(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_empty_selected_dataset_is_valid(self):
        self.saved()
        result = export_post_ids(self.source, self.target, roles=["repost"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(list(iter_post_ids(self.target)), [])
        self.assertTrue(validate_id_dataset(self.target)["valid"])

    def test_conversation_seed_analysis_comment_and_context_ancestor(self):
        store = PostStore(self.source, query={"kind": "post_ids"})
        common = {
            "author_id": "5",
            "author_handle": "sample",
            "created_at_utc": "2026-01-01T00:00:00Z",
            "text": "",
            "url": "",
        }
        store.append_page(
            [
                Post(id="10", observation_role="seed", **common),
                Post(
                    id="30",
                    in_reply_to_id="20",
                    conversation_id="10",
                    observation_role="analysis",
                    **common,
                ),
                Post(
                    id="20",
                    in_reply_to_id="10",
                    conversation_id="10",
                    observation_role="context",
                    **common,
                ),
            ],
            None,
        )
        store.finish(complete=False, reason="page_limit")
        result = export_post_ids(self.source, self.target)
        self.assertEqual(result["source_records"], 3)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["exclusions"]["context_records"], 1)
        self.assertEqual(list(iter_post_ids(self.target)), ["10", "30"])
        for role, expected in (("main", ["10"]), ("comment", ["30"])):
            selected = self.root / role
            result = export_post_ids(self.source, selected, roles=[role])
            self.assertEqual(list(iter_post_ids(selected)), expected)
            self.assertEqual(
                result["exclusions"],
                {"context_records": 1, "other_roles": 1, "duplicate_ids": 0},
            )

    def test_unknown_observation_role_is_rejected(self):
        store = PostStore(self.source, query={"kind": "post_ids"})
        store.append_page(
            [
                Post(
                    id="10",
                    author_id="5",
                    author_handle="sample",
                    created_at_utc="",
                    text="",
                    url="",
                    observation_role="unknown",
                )
            ],
            None,
        )
        store.finish(complete=False, reason="page_limit")
        with self.assertRaises(IntegrityError):
            export_post_ids(self.source, self.target)

    def test_invalid_decimal_id_rejected(self):
        store = PostStore(self.source, query={"kind": "post_ids"})
        store.append_page(
            [
                Post(
                    id="abc",
                    author_id="1",
                    author_handle="test",
                    created_at_utc="",
                    text="",
                    url="",
                )
            ],
            None,
        )
        store.finish(complete=False, reason="page_limit")
        with self.assertRaises(IntegrityError):
            export_post_ids(self.source, self.target)

    def test_source_or_destination_lock_blocks_export(self):
        self.saved()
        for path in (self.source, self.target):
            with ScopeLock(path), self.assertRaises(ScopeBusyError):
                export_post_ids(self.source, self.target)

    def test_overlapping_paths_and_existing_destination_refused(self):
        self.saved()
        for path in (self.source, self.source / "child", self.root):
            with self.assertRaises(ValueError):
                export_post_ids(self.source, path)
        self.target.mkdir()
        with self.assertRaises(FileExistsError):
            export_post_ids(self.source, self.target)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_symlink_alias_refused(self):
        self.saved()
        self.target.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(IntegrityError):
            export_post_ids(self.source, self.target)

    def test_invalid_roles(self):
        for roles in ([], ["bogus"], "main"):
            with self.assertRaises(ValueError):
                export_post_ids(self.source, self.target, roles=roles)

    def test_tamper_hash_or_manifest_rejected(self):
        self.saved()
        export_post_ids(self.source, self.target)
        path = self.target / "post_ids.csv"
        path.write_text("post_id\n2\n11\n")
        with self.assertRaises(IntegrityError):
            validate_id_dataset(self.target)
        manifest_path = self.target / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["private_query"] = "not allowed"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(IntegrityError):
            validate_id_dataset(self.target)

    def test_bad_order_duplicate_or_rows_fail_before_first_yield(self):
        self.saved()
        export_post_ids(self.source, self.target)
        path = self.target / "post_ids.csv"
        manifest_path = self.target / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for content in (
            "post_id\n10\n2\n",
            "post_id\n2\n2\n",
            "post_id\n2\n",
            "post_id\n2\n\n",
            "post_id\n02\n10\n",
            "post_id\n2,private\n10\n",
        ):
            path.write_text(content)
            manifest["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(IntegrityError):
                next(iter_post_ids(self.target))

    def test_extra_private_file_rejected(self):
        self.saved()
        export_post_ids(self.source, self.target)
        (self.target / "cookies.json").write_text("{}")
        with self.assertRaises(IntegrityError):
            validate_id_dataset(self.target)


if __name__ == "__main__":
    unittest.main()


class DatasetCLITests(unittest.TestCase):
    def test_offline_export_validate_and_corruption(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        from pyxcom.cli import main
        from pyxcom.storage import PostStore
        from pyxcom.models import Post

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            destination = Path(folder) / "ids"
            store = PostStore(source, query={"kind": "test"})
            store.append_page(
                [
                    Post(
                        "1234567890",
                        "123",
                        "actor",
                        "2026-01-01T00:00:00+00:00",
                        "text",
                        "https://x.com/i/status/1234567890",
                    )
                ],
                None,
            )
            store.finish(complete=False, reason="page_limit")
            with patch(
                "pyxcom.cli.XClient",
                side_effect=AssertionError("No browser for offline command"),
            ):
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(
                        main(
                            [
                                "export-ids",
                                "--output-dir",
                                str(source),
                                "--destination",
                                str(destination),
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(main(["validate-ids", str(destination)]), 0)
                    (destination / "post_ids.csv").write_text("post_id\n1\n")
                    self.assertEqual(main(["validate-ids", str(destination)]), 2)
