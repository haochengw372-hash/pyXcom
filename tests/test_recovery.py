"""Recovery of stable snapshots, source refusal, and explicit apply failures."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyxcom.errors import IntegrityError
from pyxcom.models import Post
from pyxcom.recovery import (
    _files,
    _private,
    _writer_lock,
    apply_recovery,
    prepare_recovery,
    verify_generation,
)
from pyxcom.storage import PostStore
from pyxcom.validate import validate_collection


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.generations = self.root / "generations"
        self.receipts = self.root / "receipts"
        self.query = {"kind": "test"}
        store = PostStore(self.source, query=self.query)
        root = Post(
            "1",
            "9",
            "alice",
            "2026-01-01T00:00:00+00:00",
            "hello\u2028world\u2029again",
            "https://x.com/i/status/1",
        )
        reply = Post(
            "2",
            "8",
            "bob",
            "2026-01-01T01:00:00+00:00",
            "reply",
            "https://x.com/i/status/2",
            conversation_id="1",
            in_reply_to_id="1",
        )
        store.append_page([root, reply], None)
        store.finish(complete=False, reason="page_limit")

    def prepare(self, **kwargs):
        return prepare_recovery(self.source, generation_dir=self.generations, **kwargs)

    def corrupt_derived(self):
        path = self.source / "users.csv"
        path.write_bytes(path.read_bytes().replace(b"alice", b"older"))

    def test_preparation_has_no_source_mutations_and_keeps_partial(self):
        self.corrupt_derived()
        initial = _files(self.source)
        report = self.prepare(binding={"observer_id": "9"})
        self.assertTrue(report["ready"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["reason"], "page_limit")
        self.assertEqual(report["pages_fetched"], 1)
        self.assertEqual(_files(self.source), initial)
        generation = Path(report["generation_dir"])
        self.assertEqual(_files(generation / "original"), initial)
        self.assertTrue(verify_generation(generation)["valid"])
        self.assertNotEqual(report["output_sha256"]["users.csv"], initial["users.csv"])
        self.assertEqual(_private(report["output_sha256"]), _private(initial))

    def test_derived_apply_only_and_external_receipt(self):
        self.corrupt_derived()
        private = _private(_files(self.source))
        report = self.prepare(binding={"observer_id": "9", "login_id": "manual"})
        result = apply_recovery(
            self.source,
            generation_dir=report["generation_dir"],
            receipt_dir=self.receipts,
        )
        self.assertTrue(result["applied"])
        self.assertFalse(result["complete"])
        self.assertEqual(_private(_files(self.source)), private)
        self.assertTrue(validate_collection(self.source)["valid"])
        self.assertTrue(Path(result["receipt_path"]).is_file())
        self.assertEqual(
            _files(Path(result["application_dir"]) / "original"), report["input_sha256"]
        )
        self.assertEqual(
            json.loads(Path(result["receipt_path"]).read_text())["status"], "committed"
        )

    def test_changed_input_rejects_application(self):
        report = self.prepare()
        state_path = self.source / ".pyxcom" / "state.json"
        state = json.loads(state_path.read_text())
        state["cursor"] = "advanced"
        state_path.write_text(json.dumps(state))
        changed = _files(self.source)
        with self.assertRaises(IntegrityError):
            apply_recovery(
                self.source,
                generation_dir=report["generation_dir"],
                receipt_dir=self.receipts,
            )
        self.assertEqual(_files(self.source), changed)
        self.assertFalse(list(self.receipts.glob("*-current.json")))

    def test_blocked_private_records_cannot_be_erased_by_export(self):
        path = self.source / ".pyxcom" / "posts.jsonl"
        records = path.read_text().split("\n")
        path.write_text(records[0] + "\n")
        before = _files(self.source)
        with self.assertRaises(IntegrityError):
            self.prepare()
        self.assertEqual(before, _files(self.source))
        self.assertFalse(self.generations.exists())

    def test_stale_plan_and_wrong_query_refused(self):
        from pyxcom.integrity import assess_recovery

        plan = assess_recovery(self.source)
        self.corrupt_derived()
        with self.assertRaises(IntegrityError):
            self.prepare(plan=plan)
        with self.assertRaises(IntegrityError):
            self.prepare(expected_query={"kind": "different"})

    def test_generation_output_and_original_tampering_are_detected(self):
        for section in ("data", "original"):
            with self.subTest(section=section):
                report = self.prepare()
                generation = Path(report["generation_dir"])
                target = generation / section / "posts.csv"
                target.write_bytes(target.read_bytes() + b"tamper\n")
                self.assertFalse(verify_generation(generation)["valid"])
                with self.assertRaises(IntegrityError):
                    apply_recovery(
                        self.source,
                        generation_dir=generation,
                        receipt_dir=self.receipts,
                    )

    def test_failure_during_export_has_no_ready_receipt(self):
        initial = _files(self.source)
        with patch(
            "pyxcom.tables._export_tables_unchecked", side_effect=OSError("disk")
        ):
            with self.assertRaises(OSError):
                self.prepare()
        generation = next(self.generations.iterdir())
        self.assertFalse((generation / "receipt.json").exists())
        self.assertEqual(
            json.loads((generation / "status.json").read_text())["status"], "failed"
        )
        self.assertEqual(_files(self.source), initial)

    def test_source_change_during_copy_blocks_ready(self):
        import shutil

        original_copy = shutil.copytree

        def changing_copy(src, dst, *args, **kwargs):
            result = original_copy(src, dst, *args, **kwargs)
            if Path(src).resolve() == self.source.resolve():
                (self.source / ".pyxcom" / "state.json").write_text("{}")
            return result

        with patch("pyxcom.recovery.shutil.copytree", side_effect=changing_copy):
            with self.assertRaises(IntegrityError):
                self.prepare()
        generation = next(self.generations.iterdir())
        self.assertFalse((generation / "receipt.json").exists())

    def test_apply_failure_emits_no_committed_receipt(self):
        self.corrupt_derived()
        report = self.prepare()
        private = _private(_files(self.source))
        with patch("pyxcom.recovery._atomic_bytes", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                apply_recovery(
                    self.source,
                    generation_dir=report["generation_dir"],
                    receipt_dir=self.receipts,
                )
        self.assertEqual(_private(_files(self.source)), private)
        self.assertFalse(list(self.receipts.rglob("receipt.json")))
        self.assertFalse(list(self.receipts.glob("*-current.json")))

    def test_repeated_preparation_is_unique_and_never_implicitly_applies(self):
        self.corrupt_derived()
        first = self.prepare()
        second = self.prepare()
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(_files(self.source), first["input_sha256"])

    def test_evidence_paths_cannot_overlap_source(self):
        for path in (self.source, self.source / "generations", self.source.parent):
            with self.subTest(path=path):
                with self.assertRaises(IntegrityError):
                    prepare_recovery(self.source, generation_dir=path)
        report = self.prepare()
        with self.assertRaises(IntegrityError):
            apply_recovery(
                self.source,
                generation_dir=report["generation_dir"],
                receipt_dir=self.source / "receipts",
            )

    def test_missing_or_malformed_receipt_is_invalid(self):
        generation = self.root / "missing"
        self.assertFalse(verify_generation(generation)["valid"])
        generation.mkdir()
        (generation / "receipt.json").write_text("[]")
        self.assertFalse(verify_generation(generation)["valid"])

    def test_source_advances_during_apply_without_commit_receipt(self):
        from pyxcom.recovery import _atomic_bytes

        report = self.prepare()
        state_path = self.source / ".pyxcom" / "state.json"

        def changed_write(path, value):
            _atomic_bytes(path, value)
            state_path.write_text("{}")

        with patch("pyxcom.recovery._atomic_bytes", side_effect=changed_write):
            with self.assertRaises(IntegrityError):
                apply_recovery(
                    self.source,
                    generation_dir=report["generation_dir"],
                    receipt_dir=self.receipts,
                )
        self.assertFalse(list(self.receipts.rglob("receipt.json")))
        self.assertFalse(list(self.receipts.glob("*-current.json")))

    def test_stale_optional_derived_file_is_removed_without_source_change(self):
        (self.source / "interactions.csv").write_text("stale")
        private = _private(_files(self.source))
        report = self.prepare()
        self.assertNotIn("interactions.csv", report["output_sha256"])
        apply_recovery(
            self.source,
            generation_dir=report["generation_dir"],
            receipt_dir=self.receipts,
        )
        self.assertFalse((self.source / "interactions.csv").exists())
        self.assertEqual(_private(_files(self.source)), private)

    def test_false_completed_receipt_does_not_change_partial_status(self):
        report = self.prepare()
        receipt_path = Path(report["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        receipt["complete"] = True
        receipt_path.write_text(json.dumps(receipt))
        self.assertFalse(verify_generation(report["generation_dir"])["valid"])

    def test_profile_history_recovery_preserves_all_observations(self):
        import csv
        from pyxcom.profiles import save_profiles

        store = PostStore(self.source, query=self.query)
        base = {"id": "9", "created_at_utc": "2009-01-01T00:00:00+00:00"}
        save_profiles(
            self.source,
            {
                "old": {
                    **base,
                    "handle": "old",
                    "description": "first\u2028second",
                    "captured_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
        )
        save_profiles(
            self.source,
            {
                "alice": {
                    **base,
                    "handle": "alice",
                    "description": "next\u2029last\u0085line",
                    "captured_at_utc": "2026-01-02T00:00:00+00:00",
                }
            },
        )
        save_profiles(
            self.source,
            {
                "bob": {
                    **base,
                    "id": "8",
                    "handle": "bob",
                    "captured_at_utc": "2026-01-02T00:00:00+00:00",
                }
            },
        )
        store.finish(complete=False, reason="page_limit")
        expected = (self.source / "profile_snapshots.csv").read_bytes()
        (self.source / "profile_snapshots.csv").write_bytes(
            expected.split(b"\n")[0] + b"\n"
        )
        private = _private(_files(self.source))
        report = self.prepare()
        recovered = Path(report["generation_dir"]) / "data" / "profile_snapshots.csv"
        self.assertEqual(recovered.read_bytes(), expected)
        with recovered.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 3)
        self.assertEqual(report["source_sha256"], private)
        self.assertEqual(_private(_files(self.source)), private)

    def test_lock_refuses_second_writer(self):
        if __import__("os").name == "nt":
            self.skipTest("POSIX lock test")
        with _writer_lock(self.root / "test.lock"):
            with self.assertRaises(IntegrityError):
                with _writer_lock(self.root / "test.lock"):
                    self.fail("second writer acquired busy scope")
