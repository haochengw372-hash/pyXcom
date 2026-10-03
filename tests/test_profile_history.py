import csv
import hashlib
import json
import tempfile
import shutil
import unittest
from pathlib import Path

from pyxcom import export_tables, validate_collection, validate_tables
from pyxcom.models import Post
from pyxcom.storage import PostStore


def profile(handle, captured, **changes):
    return {
        "id": "99204810",
        "handle": handle,
        "name": handle,
        "created_at_utc": "2009-12-25T02:30:23+00:00",
        "captured_at_utc": captured,
        "followers_count": 1,
        **changes,
    }


def rows(directory, name):
    with (directory / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class ProfileHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = PostStore(
            self.directory,
            query={"kind": "post_comments", "root_post_id": "1", "max_depth": 2},
        )
        self.store.append_page(
            [
                Post(
                    id="1",
                    author_id="99204810",
                    author_handle="old",
                    created_at_utc="2026-07-03T00:00:00+00:00",
                    text="x",
                    url="https://x.com/old/status/1",
                )
            ],
            "saved-cursor",
        )
        self.path = self.directory / ".pyxcom/profiles.json"
        self.old = profile("quarkyplasma", "2026-10-01T21:06:37.942690+00:00")
        self.new = profile(
            "clicktobuynow",
            "2026-10-02T09:42:31.413071+00:00",
            followers_count=7,
            verified=True,
        )

    def write(self, payload):
        self.path.write_text(json.dumps(payload))

    def test_rename_and_metrics_export_latest_and_preserve_sources(self):
        self.write({"quarkyplasma": self.old, "clicktobuynow": self.new})
        original = self.path.read_bytes()
        self.store.finish(complete=False, reason="page_limit")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.cursor, "saved-cursor")
        self.assertEqual(
            rows(self.directory, "users.csv")[0]["username"], "clicktobuynow"
        )
        self.assertEqual(rows(self.directory, "users.csv")[0]["followers_count"], "7")
        history = rows(self.directory, "profile_snapshots.csv")
        self.assertEqual(
            {r["username"] for r in history}, {"quarkyplasma", "clicktobuynow"}
        )
        self.assertEqual(len(history), 2)
        self.assertTrue(validate_collection(self.directory)["valid"])

    def test_out_of_order_input_is_deterministic(self):
        self.write({"new": self.new, "old": self.old})
        export_tables(self.directory)
        before = (self.directory / "users.csv").read_bytes()
        self.write({"old": self.old, "new": self.new})
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp)
            shutil.copytree(self.directory / ".pyxcom", other / ".pyxcom")
            export_tables(other)
            self.assertEqual((other / "users.csv").read_bytes(), before)

    def test_same_timestamp_is_audited_and_deterministic(self):
        self.new["captured_at_utc"] = self.old["captured_at_utc"]
        self.write([self.new, self.old])
        report = export_tables(self.directory)
        before = (self.directory / "users.csv").read_bytes()
        self.write([self.old, self.new])
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp)
            shutil.copytree(self.directory / ".pyxcom", other / ".pyxcom")
            export_tables(other)
            self.assertEqual((other / "users.csv").read_bytes(), before)
        self.assertEqual(
            report["profile_observations"]["tied_latest_user_ids"], ["99204810"]
        )

    def test_unknown_time_does_not_override_dated_snapshot(self):
        self.write([self.new, profile("unknown", None, followers_count=999)])
        report = export_tables(self.directory)
        self.assertEqual(
            rows(self.directory, "users.csv")[0]["username"], "clicktobuynow"
        )
        self.assertEqual(report["profile_observations"]["unknown_time_observations"], 1)

    def test_creation_identity_conflict_fails_before_table_writes(self):
        self.new["created_at_utc"] = "2010-01-01T00:00:00+00:00"
        self.write([self.old, self.new])
        with self.assertRaisesRegex(ValueError, "Conflicting profile identity"):
            export_tables(self.directory)
        self.assertFalse((self.directory / "users.csv").exists())

    def test_equivalent_creation_times_and_missing_creation_are_compatible(self):
        self.new["created_at_utc"] = "2009-12-25T03:30:23+01:00"
        self.write([self.old, self.new, profile("unknown", None, created_at_utc=None)])
        export_tables(self.directory)
        self.assertTrue(validate_tables(self.directory)["valid"])

    def test_same_handle_updates_are_archived_before_overwrite(self):
        from pyxcom.profiles import save_profiles

        save_profiles(self.directory, {"same": self.old})
        save_profiles(self.directory, {"same": self.new})
        save_profiles(self.directory, {"same": self.new})
        export_tables(self.directory)
        self.assertEqual(len(rows(self.directory, "profile_snapshots.csv")), 2)
        self.assertEqual(
            rows(self.directory, "users.csv")[0]["username"], "clicktobuynow"
        )

    def test_validator_checks_latest_view_even_with_rehashed_csv(self):
        self.write([self.old, self.new])
        export_tables(self.directory)
        p = self.directory / "users.csv"
        p.write_bytes(p.read_bytes().replace(b"clicktobuynow", b"quarkyplasma"))
        m = self.directory / "manifest.json"
        manifest = json.loads(m.read_text())
        manifest["sha256"]["users.csv"] = hashlib.sha256(p.read_bytes()).hexdigest()
        m.write_text(json.dumps(manifest))
        self.assertFalse(validate_tables(self.directory)["valid"])

    def test_conflict_does_not_overwrite_profile_or_history(self):
        from pyxcom.profiles import save_profiles

        save_profiles(self.directory, {"same": self.old})
        ledger = self.directory / ".pyxcom/profile_observations.jsonl"
        before = self.path.read_bytes(), ledger.read_bytes()
        self.new["created_at_utc"] = "2010-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "Conflicting profile identity"):
            save_profiles(self.directory, {"same": self.new})
        self.assertEqual((self.path.read_bytes(), ledger.read_bytes()), before)

    def test_identical_list_entries_remain_distinct_source_observations(self):
        self.write([self.old, self.old])
        export_tables(self.directory)
        self.assertEqual(len(rows(self.directory, "users.csv")), 1)
        snapshots = rows(self.directory, "profile_snapshots.csv")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual({row["source_key"] for row in snapshots}, {"0", "1"})

    def test_latest_sparse_snapshot_does_not_backfill_stale_metrics(self):
        self.new["followers_count"] = None
        self.write([self.old, self.new])
        export_tables(self.directory)
        self.assertEqual(rows(self.directory, "users.csv")[0]["followers_count"], "")

    def test_timezone_order_uses_instants_instead_of_timestamp_strings(self):
        self.old["captured_at_utc"] = "2026-10-02T10:00:00+02:00"
        self.new["captured_at_utc"] = "2026-10-02T09:00:00+00:00"
        self.write([self.old, self.new])
        export_tables(self.directory)
        self.assertEqual(
            rows(self.directory, "users.csv")[0]["username"], "clicktobuynow"
        )

    def test_unicode_separators_survive_history_append_resume_and_export(self):
        from pyxcom.profiles import profile_views, save_profiles

        for separator in ("\u2028", "\u2029", "\u0085"):
            with (
                self.subTest(separator=repr(separator)),
                tempfile.TemporaryDirectory() as tmp,
            ):
                directory = Path(tmp)
                store = PostStore(
                    directory,
                    query={
                        "kind": "post_comments",
                        "root_post_id": "1",
                        "max_depth": 2,
                    },
                )
                post = Post(
                    id="1",
                    author_id="99204810",
                    author_handle="same",
                    created_at_utc="2026-07-05T00:00:00+00:00",
                    text="before" + separator + "after",
                    url="https://x.com/same/status/1",
                )
                store.append_page([post], "original-cursor")
                old = {**self.old, "description": "old" + separator + "description"}
                new = {**self.new, "description": "new" + separator + "description"}
                save_profiles(directory, {"same": old})
                ledger = directory / ".pyxcom/profile_observations.jsonl"
                before = ledger.read_bytes()
                before_ids = {r["snapshot_id"] for r in profile_views(directory)[1]}
                save_profiles(directory, {"same": new})
                save_profiles(directory, {"same": new})
                selected, snapshots, _ = profile_views(directory)
                self.assertEqual(
                    selected["99204810"]["description"], new["description"]
                )
                self.assertEqual(len(snapshots), 2)
                self.assertTrue(before_ids <= {r["snapshot_id"] for r in snapshots})
                self.assertTrue(ledger.read_bytes().startswith(before))
                self.assertIn(separator.encode(), ledger.read_bytes())
                self.assertEqual(
                    len(
                        [
                            line
                            for line in ledger.read_text().split("\n")
                            if line.strip()
                        ]
                    ),
                    2,
                )
                resumed = PostStore(directory, query=store.state["query"])
                self.assertEqual(resumed.cursor, "original-cursor")
                self.assertEqual(resumed._posts["1"].text, post.text)
                resumed.finish(complete=False, reason="page_limit")
                self.assertEqual(
                    rows(directory, "users.csv")[0]["description"], new["description"]
                )
                exported = rows(directory, "profile_snapshots.csv")
                self.assertEqual(
                    {r["snapshot_id"] for r in exported},
                    {r["snapshot_id"] for r in snapshots},
                )
                self.assertTrue(
                    all(
                        separator in json.loads(r["profile_json"])["description"]
                        for r in exported
                    )
                )
                self.assertTrue(validate_collection(directory)["valid"])

    def test_crlf_history_is_read_without_changing_existing_bytes(self):
        from pyxcom.profiles import profile_views, save_profiles

        save_profiles(self.directory, {"same": self.old})
        ledger = self.directory / ".pyxcom/profile_observations.jsonl"
        ledger.write_bytes(ledger.read_bytes().replace(b"\n", b"\r\n"))
        before = ledger.read_bytes()
        save_profiles(self.directory, {"same": self.new})
        self.assertTrue(ledger.read_bytes().startswith(before))
        self.assertEqual(len(profile_views(self.directory)[1]), 2)

    def test_genuinely_broken_history_fails_and_is_not_rewritten(self):
        from pyxcom.profiles import profile_views, save_profiles

        save_profiles(self.directory, {"same": self.old})
        self.store.finish(complete=False, reason="page_limit")
        ledger = self.directory / ".pyxcom/profile_observations.jsonl"
        with ledger.open("ab") as stream:
            stream.write(b'{"profile_json": "unterminated\n')
        before = ledger.read_bytes(), self.path.read_bytes()
        with self.assertRaises(json.JSONDecodeError):
            profile_views(self.directory)
        with self.assertRaises(json.JSONDecodeError):
            save_profiles(self.directory, {"same": self.new})
        from pyxcom import IntegrityError

        with self.assertRaises(IntegrityError):
            export_tables(self.directory)
        self.assertFalse(validate_collection(self.directory)["valid"])
        self.assertEqual((ledger.read_bytes(), self.path.read_bytes()), before)
