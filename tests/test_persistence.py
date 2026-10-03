import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyxcom._persistence import append_jsonl, atomic_json, jsonl_lines, read_jsonl


class PersistenceTests(unittest.TestCase):
    def test_only_lf_separates_records_and_physical_line_numbers_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            record = {
                "text": "a\u2028b\u2029c\u0085d",
                "nested": json.dumps({"description": "x\u2028y"}, ensure_ascii=False),
            }
            path.write_bytes(
                (
                    "\r\n"
                    + json.dumps(record, ensure_ascii=False)
                    + "\r\n\n"
                    + json.dumps(record, ensure_ascii=False)
                ).encode()
            )
            before = path.read_bytes()
            self.assertEqual([number for number, _ in jsonl_lines(path)], [2, 4])
            self.assertEqual(list(read_jsonl(path)), [record, record])
            self.assertEqual(path.read_bytes(), before)

    def test_malformed_object_reports_filename_and_physical_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text('{}\n\n{"text": "broken\n')
            with self.assertRaisesRegex(
                json.JSONDecodeError, "history.jsonl, record 3"
            ):
                list(read_jsonl(path))

    def test_nonobject_record_is_rejected_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            for record in ([], "text", None, 5):
                with self.subTest(record=record):
                    path.write_text(json.dumps(record) + "\n")
                    with self.assertRaisesRegex(ValueError, "expected an object"):
                        list(read_jsonl(path))

    def test_unicode_only_record_is_not_silently_skipped_as_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text("{}\n\u2028\n")
            with self.assertRaisesRegex(json.JSONDecodeError, "record 2"):
                list(read_jsonl(path))

    def test_atomic_json_failures_keep_previous_file_and_remove_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_json(path, {"cursor": "original", "text": "a\u2028b"})
            before = path.read_bytes()
            for target in ("json.dump", "os.fsync", "os.replace"):
                with (
                    self.subTest(target=target),
                    patch(
                        "pyxcom._persistence." + target,
                        side_effect=OSError("injected failure"),
                    ),
                ):
                    with self.assertRaises(OSError):
                        atomic_json(path, {"cursor": "next"})
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_profile_resume_verifies_ledger_once(self):
        from pyxcom.profiles import save_profiles
        from pyxcom._persistence import read_jsonl as original_reader

        with tempfile.TemporaryDirectory() as tmp:
            save_profiles(tmp, {"alice": {"id": "1", "handle": "alice"}})
            with patch("pyxcom.profiles.read_jsonl", wraps=original_reader) as reader:
                save_profiles(tmp, {"alice": {"id": "1", "handle": "alice"}})
            self.assertEqual(reader.call_count, 1)

    def test_append_preserves_valid_last_record_without_terminal_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            before = json.dumps(
                {"text": "original\u2028content"}, ensure_ascii=False
            ).encode()
            path.write_bytes(before)
            append_jsonl(path, [{"text": "next"}])
            self.assertTrue(path.read_bytes().startswith(before + b"\n"))
            self.assertEqual(
                list(read_jsonl(path)),
                [{"text": "original\u2028content"}, {"text": "next"}],
            )

    def test_append_encoding_failure_does_not_change_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_bytes(b'{"id": "original"}')
            before = path.read_bytes()
            with self.assertRaises(TypeError):
                append_jsonl(path, [{"id": "new"}, {"unserializable": object()}])
            self.assertEqual(path.read_bytes(), before)

    def test_profile_history_without_final_lf_resumes_with_original_ids(self):
        from pyxcom.profiles import profile_views, save_profiles

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            save_profiles(
                directory,
                {
                    "alice": {
                        "id": "1",
                        "handle": "alice",
                        "captured_at_utc": "2026-10-01T00:00:00+00:00",
                    }
                },
            )
            ledger = directory / ".pyxcom/profile_observations.jsonl"
            ledger.write_bytes(ledger.read_bytes().rstrip(b"\n"))
            before = ledger.read_bytes()
            identifiers = {r["snapshot_id"] for r in profile_views(directory)[1]}
            save_profiles(
                directory,
                {
                    "alice": {
                        "id": "1",
                        "handle": "alice",
                        "captured_at_utc": "2026-10-02T00:00:00+00:00",
                    }
                },
            )
            self.assertTrue(ledger.read_bytes().startswith(before))
            snapshots = profile_views(directory)[1]
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(identifiers <= {r["snapshot_id"] for r in snapshots})

    def test_all_post_store_streams_resume_without_terminal_lf(self):
        from pyxcom.models import Post
        from pyxcom.storage import PostStore
        from pyxcom import validate_collection

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = PostStore(directory, query={"kind": "post_ids"})

            def post(identifier):
                return Post(
                    id=identifier,
                    author_id="1",
                    author_handle="alice",
                    created_at_utc="2026-01-01T00:00:00+00:00",
                    text="text\u2028content",
                    url="https://x.com/alice/status/" + identifier,
                    like_count=1,
                    captured_at_utc="2026-10-02T00:00:00+00:00",
                )

            store.append_page([post("1")], "cursor")
            store.log(operation="test_checkpoint")
            paths = [
                directory / ".pyxcom" / name
                for name in (
                    "posts.jsonl",
                    "observations.jsonl",
                    "metric_snapshots.jsonl",
                    "collection_log.jsonl",
                )
            ]
            before = {}
            for path in paths:
                path.write_bytes(path.read_bytes().rstrip(b"\n"))
                before[path] = path.read_bytes()
            # Anchor the valid no-final-LF source bytes before any resume.
            # Altering already anchored sources must instead trigger integrity gates.
            store.finish(complete=False, reason="page_limit")
            resumed = PostStore(directory, query=store.state["query"])
            resumed.append_page([post("2")], "next")
            resumed.finish(complete=False, reason="page_limit")
            for path in paths:
                self.assertTrue(path.read_bytes().startswith(before[path]))
                self.assertTrue(list(read_jsonl(path)))
            self.assertTrue(validate_collection(directory)["valid"])

    def test_csv_replacement_failure_cleans_temporary_and_keeps_old_csv(self):
        from pyxcom.tables import _write_csv

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.csv"
            _write_csv(path, ["user_id"], [{"user_id": "1"}])
            before = path.read_bytes()
            with patch(
                "pyxcom._persistence.os.replace", side_effect=OSError("injected")
            ):
                with self.assertRaises(OSError):
                    _write_csv(path, ["user_id"], [{"user_id": "2"}])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(tmp).iterdir()), [path])
