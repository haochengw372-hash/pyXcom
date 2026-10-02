import json
import tempfile
import unittest
from pathlib import Path

from pyxcom.models import Post
from pyxcom.profiles import save_profiles
from pyxcom.schema import schema_summary
from pyxcom.storage import PostStore
from pyxcom.validate import validate_collection
from pyxcom.validate_tables import validate_tables


class ValidationShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.post = Post(
            id="1",
            author_id="9",
            author_handle="old",
            created_at_utc="2026-01-01T00:00:00+00:00",
            text="before\u2028after",
            url="https://x.com/old/status/1",
        )
        store = PostStore(
            self.directory,
            query={"kind": "post_comments", "root_post_id": "1", "max_depth": 2},
        )
        store.append_page([self.post], "saved-cursor")
        store.finish(complete=False, reason="page_limit")
        self.internal = self.directory / ".pyxcom"

    def test_nonobject_post_records_report_invalid_without_crashing(self):
        for record in ([], None, "text", 7, True):
            with self.subTest(record=record):
                (self.internal / "posts.jsonl").write_text(
                    "\n" + json.dumps(record) + "\n", encoding="utf-8"
                )
                result = validate_collection(self.directory)
                self.assertFalse(result["valid"])
                self.assertIn("invalid_post_record:2", result["errors"])

    def test_missing_required_post_fields_report_invalid(self):
        for key in (
            "id",
            "author_id",
            "author_handle",
            "created_at_utc",
            "text",
            "url",
        ):
            with self.subTest(key=key):
                record = self.post.to_dict()
                del record[key]
                (self.internal / "posts.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8"
                )
                result = validate_collection(self.directory)
                self.assertIn("invalid_post_record:1", result["errors"])

    def test_invalid_field_shapes_report_invalid(self):
        for key, value in (
            ("id", []),
            ("author_id", None),
            ("created_at_utc", None),
            ("text", {}),
        ):
            with self.subTest(key=key):
                record = {**self.post.to_dict(), key: value}
                (self.internal / "posts.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8"
                )
                result = validate_collection(self.directory)
                self.assertIn("invalid_post_record:1", result["errors"])

    def test_malformed_json_reports_physical_lf_line_after_unicode_text(self):
        (self.internal / "posts.jsonl").write_text(
            json.dumps(self.post.to_dict(), ensure_ascii=False)
            + '\n\n{"text": "unterminated\n',
            encoding="utf-8",
        )
        result = validate_collection(self.directory)
        self.assertIn("invalid_jsonl_line:3", result["errors"])
        self.assertEqual(result["post_count"], 1)

    def test_nonobject_collection_logs_report_invalid_without_crashing(self):
        for record in ([], None, "text", 7, True):
            with self.subTest(record=record):
                (self.internal / "collection_log.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8"
                )
                result = validate_collection(self.directory)
                self.assertIn("invalid_collection_log_record", result["errors"])

    def test_nested_nonobject_profile_json_reports_invalid(self):
        ledger = self.internal / "profile_observations.jsonl"
        for record in ([], None, "text", 7):
            with self.subTest(record=record):
                ledger.write_text(
                    json.dumps(
                        {
                            "profile_json": json.dumps(record),
                            "source_file": ".pyxcom/profiles.json",
                            "source_key": "old",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                result = validate_collection(self.directory)
                self.assertIn("invalid_profiles", result["errors"])
                self.assertFalse(validate_tables(self.directory)["valid"])

    def test_schema_counts_stable_profile_ids_and_ledger_only_history(self):
        old = {
            "id": "9",
            "handle": "old",
            "name": "Old",
            "captured_at_utc": "2026-10-01T00:00:00+00:00",
            "followers_count": 10,
        }
        new = {
            **old,
            "handle": "new",
            "name": "New",
            "captured_at_utc": "2026-10-02T00:00:00+00:00",
            "followers_count": None,
        }
        save_profiles(self.directory, {"old": old, "new": new})
        before = {
            path: path.read_bytes()
            for path in self.internal.iterdir()
            if path.is_file()
        }
        summary = schema_summary(self.directory)
        self.assertEqual(summary["profile_count"], 1)
        coverage = {row["name"]: row for row in summary["profile_fields"]}
        self.assertEqual(coverage["followers_count"]["available"], 0)
        self.assertEqual(coverage["followers_count"]["missing"], 1)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        (self.internal / "profiles.json").unlink()
        self.assertEqual(schema_summary(self.directory)["profile_count"], 1)
