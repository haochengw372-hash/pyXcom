import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pyxcom.layout import child_dir, internal_dir, migrate_collection, source_path
from pyxcom.models import Post
from pyxcom.storage import PostStore
from pyxcom.tables import export_tables
from pyxcom.validate import finalize_collection, validate_collection
from pyxcom.batch import _account_status


def sample(post_id="1"):
    return Post(
        post_id,
        "9",
        "alice",
        "2026-01-01T00:00:00+00:00",
        "hello",
        "https://x.com/i/status/1",
    )


def legacy(folder, query=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "posts.jsonl").write_text(json.dumps(sample().to_dict()) + "\n")
    (folder / "state.json").write_text(
        json.dumps(
            {
                "query": query or {},
                "cursor": "next",
                "pages_fetched": 1,
                "complete": False,
                "reason": "page_limit",
            }
        )
    )
    (folder / "posts.csv").write_text("id,author_handle,post_role\n1,alice,main\n")
    (folder / "manifest.json").write_text(json.dumps({"post_count": 1}))


class LayoutTests(unittest.TestCase):
    def test_new_root_is_only_public_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = PostStore(root, query={})
            store.append_page([sample()], "next")
            store.finish(complete=False, reason="page_limit")
            self.assertEqual(
                {p.name for p in root.iterdir()},
                {"users.csv", "posts.csv", "comments.csv", "manifest.json", ".pyxcom"},
            )
            self.assertTrue(validate_collection(root)["valid"])
            resumed = PostStore(root, query={})
            self.assertEqual(resumed.cursor, "next")
            self.assertEqual(resumed.append_page([sample()], None), 0)

    def test_legacy_resume_backup_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy(root)
            original = (root / "posts.jsonl").read_bytes()
            mixed_csv = (root / "posts.csv").read_bytes()
            (root / "research_notes.txt").write_text("keep")
            resumed = PostStore(root, query={})
            self.assertEqual(resumed.cursor, "next")
            self.assertEqual(resumed.count, 1)
            resumed.finish(complete=True, reason="source_end")
            migrate_collection(root)
            export_tables(root)
            self.assertEqual(
                (internal_dir(root) / "legacy/posts.jsonl").read_bytes(), original
            )
            self.assertEqual(
                (internal_dir(root) / "legacy/posts.csv").read_bytes(), mixed_csv
            )
            self.assertEqual((root / "research_notes.txt").read_text(), "keep")
            with (root / "posts.csv").open(encoding="utf-8-sig") as file:
                self.assertIn("post_id", next(csv.reader(file)))
            self.assertTrue(validate_collection(root)["valid"])

    def test_conflict_preserves_both_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            internal_dir(root).mkdir()
            (root / "posts.jsonl").write_text("legacy")
            (internal_dir(root) / "posts.jsonl").write_text("new")
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                migrate_collection(root)
            self.assertEqual((root / "posts.jsonl").read_text(), "legacy")
            self.assertEqual((internal_dir(root) / "posts.jsonl").read_text(), "new")

    def test_batch_children_migrate_and_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy(root, {"kind": "account_batch"})
            profile = {"id": "9", "handle": "alice", "name": "Alice"}
            (root / "profiles.json").write_text(json.dumps({"alice": profile}))
            legacy(root / "alice")
            legacy(root / "alice/originals")
            legacy(root / "alice/replies")
            result = finalize_collection(root)
            self.assertEqual(result.post_count, 1)
            self.assertTrue(
                source_path(
                    child_dir(child_dir(root, "alice"), "replies"), "state.json"
                ).exists()
            )
            self.assertEqual(
                _account_status(root, "alice", profile)["replies"]["count"], 1
            )
            self.assertTrue(validate_collection(root)["valid"])
            self.assertFalse((root / "alice").exists())

    def test_single_user_profile_is_not_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = PostStore(root, query={"kind": "user_posts"})
            store.append_page([sample()], None)
            source_path(root, "profiles.json").write_text(
                json.dumps({"alice": {"id": "9"}})
            )
            finalize_collection(root)
            self.assertTrue(validate_collection(root)["valid"])
            self.assertFalse(source_path(root, "account_manifest.json").exists())

    def test_resume_after_directory_backup_before_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy(root / "alice")
            (root / "alice" / "empty").mkdir()
            backup = internal_dir(root) / "legacy" / "alice"
            shutil.copytree(root / "alice", backup)
            expected = (backup / "posts.jsonl").read_bytes()
            migrate_collection(root)
            migrate_collection(root)
            self.assertFalse((root / "alice").exists())
            self.assertEqual(
                source_path(child_dir(root, "alice"), "posts.jsonl").read_bytes(),
                expected,
            )
            self.assertEqual((backup / "posts.jsonl").read_bytes(), expected)

    def test_mismatched_directory_backup_remains_untouched(self):
        for change in ("content", "missing", "extra", "type"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                legacy(root / "alice")
                backup = internal_dir(root) / "legacy" / "alice"
                shutil.copytree(root / "alice", backup)
                original = (root / "alice" / "posts.jsonl").read_bytes()
                if change == "content":
                    (backup / "posts.jsonl").write_text("changed")
                elif change == "missing":
                    (backup / "posts.jsonl").unlink()
                elif change == "extra":
                    (backup / "extra").mkdir()
                else:
                    (backup / "posts.jsonl").unlink()
                    (backup / "posts.jsonl").mkdir()
                with self.assertRaisesRegex(ValueError, "Conflicting legacy backup"):
                    migrate_collection(root)
                self.assertEqual(
                    (root / "alice" / "posts.jsonl").read_bytes(), original
                )
                self.assertTrue(backup.exists())
                self.assertFalse(child_dir(root, "alice").exists())
