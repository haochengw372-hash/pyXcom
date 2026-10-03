"""Integrity gates must reject source regressions without repairing evidence."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyxcom import validate_collection, validate_tables
from pyxcom.integrity import assess_recovery
from pyxcom.models import Post
from pyxcom.profiles import save_profiles
from pyxcom.storage import PostStore


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def post(identifier="1"):
    return Post(
        id=identifier,
        author_id="2",
        author_handle="test",
        created_at_utc="2026-01-01T00:00:00+00:00",
        text="a\u2028b\u2029c\u0085d",
        url="https://x.com/test/status/1",
    )


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.query = {"kind": "post_ids", "ids": ["1"]}

    def saved(self):
        store = PostStore(self.path, query=self.query)
        store.append_page([post()], None)
        store.finish(complete=False, reason="page_limit")
        return store

    def test_current_manifest_anchor_keeps_partial_status_and_all_hashes(self):
        self.saved()
        before = {
            p.relative_to(self.path).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in self.path.rglob("*")
            if p.is_file()
        }
        result = assess_recovery(
            self.path, expected_query=self.query, binding={"observer_id": "7"}
        )
        self.assertTrue(result["allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "verified_manifest_anchor")
        self.assertFalse(result["complete"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["reason"], "page_limit")
        self.assertEqual(result["input_sha256"], before)
        self.assertEqual(result["binding_provenance"], "caller_supplied")

    def test_broken_derived_table_can_be_reconstructed(self):
        self.saved()
        (self.path / "users.csv").write_text("older or broken derived content")
        self.assertTrue(assess_recovery(self.path)["allowed"])
        (self.path / "posts.csv").write_text("not even a correct csv")
        self.assertTrue(assess_recovery(self.path)["allowed"])

    def test_private_records_cannot_discard_later_public_rows(self):
        self.saved()
        (self.path / ".pyxcom/posts.jsonl").write_text("")
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertIn("canonical_behind_public", result["errors"])
        self.assertEqual(result["record_integrity"], "conflicting")

    def test_profile_history_regression_is_blocked(self):
        store = self.saved()
        save_profiles(
            self.path,
            {
                "old": {
                    "id": "2",
                    "handle": "old",
                    "captured_at_utc": "2026-01-01T00:00:00+00:00",
                }
            },
        )
        save_profiles(
            self.path,
            {
                "new": {
                    "id": "2",
                    "handle": "new",
                    "captured_at_utc": "2026-01-02T00:00:00+00:00",
                }
            },
        )
        store.finish(complete=False, reason="page_limit")
        (self.path / ".pyxcom/profile_observations.jsonl").write_text("")
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertIn("profile_history_behind_public", result["errors"])

    def test_checkpoint_old_prefix_rejected_even_if_hash_is_updated(self):
        self.saved()
        state = json.loads((self.path / ".pyxcom/state.json").read_text())
        state["pages_fetched"] = 0
        write(self.path / ".pyxcom/state.json", state)
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["source_sha256"][".pyxcom/state.json"] = hashlib.sha256(
            (self.path / ".pyxcom/state.json").read_bytes()
        ).hexdigest()
        write(self.path / "manifest.json", manifest)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertIn("checkpoint_behind_manifest", result["errors"])
        self.assertEqual(result["record_integrity"], "verified")
        self.assertEqual(result["latest_checkpoint"], "regressed")

    def test_receipt_detects_whole_consistent_generation_reversion(self):
        self.saved()
        receipt = assess_recovery(self.path, binding={"observer_id": "7"})
        receipt["pages_fetched"] = 50
        result = assess_recovery(
            self.path, binding={"observer_id": "7"}, previous_receipt=receipt
        )
        self.assertFalse(result["allowed"])
        self.assertIn("checkpoint_behind_receipt", result["errors"])

    def test_query_and_receipt_binding_cannot_change(self):
        self.saved()
        result = assess_recovery(
            self.path,
            expected_query={"kind": "search"},
            binding={"observer_id": "7"},
            previous_receipt={"binding": {"observer_id": "8"}},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("query_mismatch", result["errors"])
        self.assertIn("receipt_binding_mismatch", result["errors"])
        self.assertEqual(result["record_integrity"], "verified")
        self.assertEqual(result["latest_checkpoint"], "verified_manifest_anchor")

    def test_unknown_target_and_author_stubs_are_legal(self):
        store = PostStore(self.path, query=self.query)
        store.append_page(
            [
                Post(
                    **{
                        **post().to_dict(),
                        "quoted_post_id": "999",
                        "quoted_author_id": "888",
                    }
                )
            ],
            None,
        )
        store.finish(complete=False, reason="page_limit")
        self.assertTrue(assess_recovery(self.path)["allowed"])

    def test_fresh_unicode_records_allow_unanchored_offline_export(self):
        private = self.path / ".pyxcom"
        private.mkdir()
        (private / "posts.jsonl").write_text(
            json.dumps(post().to_dict(), ensure_ascii=False) + "\n"
        )
        result = assess_recovery(self.path)
        self.assertTrue(result["allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "not_present")
        self.assertIsNone(result["coverage_complete"])

    def test_unicode_profile_json_is_not_split_into_records(self):
        store = self.saved()
        save_profiles(
            self.path,
            {
                "test": {
                    "id": "2",
                    "handle": "test",
                    "description": "\u2028\u2029\u0085",
                }
            },
        )
        store.finish(complete=False, reason="page_limit")
        self.assertTrue(assess_recovery(self.path)["allowed"])

    def test_source_changing_between_reads_rejects_plan(self):
        self.saved()
        from pyxcom.integrity import _inventory

        calls = 0

        def changing(path):
            nonlocal calls
            calls += 1
            result = _inventory(path)
            if calls == 2:
                result[".pyxcom/posts.jsonl"] = b"changed"
            return result

        with patch("pyxcom.integrity._inventory", side_effect=changing):
            result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertIn("input_changed", result["errors"])

    def test_symlink_rejected_without_reading_target(self):
        self.saved()
        (self.path / "link").symlink_to(self.path / ".pyxcom/posts.jsonl")
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertIn("symlink_input", result["errors"])

    def test_state_hash_only_without_replay_must_not_be_reanchored(self):
        self.saved()
        state = json.loads((self.path / ".pyxcom/state.json").read_text())
        state["updated_at_utc"] = "2026-02-01T00:00:00+00:00"
        write(self.path / ".pyxcom/state.json", state)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertIn("checkpoint_replay_unproven", result["errors"])

    def test_malformed_jsonl_fails_without_record_text_in_report(self):
        self.saved()
        (self.path / ".pyxcom/posts.jsonl").write_text('{"secret-example"')
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"])
        self.assertNotIn("secret-example", json.dumps(result))

    def test_full_comment_request_replay_proves_state_only_stale_anchor(self):
        from test_comments import client, page, tweet, ROOT, A

        c = client(page(tweet(ROOT, handle="root"), tweet(A, ROOT)))
        c.save_post_comments(ROOT, self.path, max_depth=2, max_pages=2)
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["source_sha256"][".pyxcom/state.json"] = "0" * 64
        write(self.path / "manifest.json", manifest)
        result = assess_recovery(self.path)
        self.assertTrue(result["allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "verified_response_replay")

    def test_full_replay_rejects_changed_request_order_and_preserves_every_byte(self):
        from test_comments import client, page, tweet, ROOT, A

        c = client(page(tweet(ROOT, handle="root"), tweet(A, ROOT)))
        c.save_post_comments(ROOT, self.path, max_depth=2, max_pages=2)
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["source_sha256"][".pyxcom/state.json"] = "0" * 64
        write(self.path / "manifest.json", manifest)
        log = self.path / ".pyxcom/collection_log.jsonl"
        rows = [json.loads(line) for line in log.read_text().split("\n") if line]
        for row in rows:
            if row.get("raw_path"):
                row["variables"]["focalTweetId"] = "99"
        log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        before = {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}
        )

    def test_public_progress_metadata_cannot_silently_be_ignored(self):
        self.saved()
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["reason"] = "source_end"
        manifest["complete"] = True
        write(self.path / "manifest.json", manifest)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertIn("checkpoint_replay_unproven", result["errors"])

    def test_newer_public_generation_rejects_old_private_checkpoint(self):
        self.saved()
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["captured_at_utc"] = "2099-01-01T00:00:00+00:00"
        write(self.path / "manifest.json", manifest)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertIn("checkpoint_behind_public_generation", result["errors"])

    def test_old_checkpoint_preserves_separate_profile_content_proof(self):
        store = self.saved()
        save_profiles(
            self.path,
            {"test": {"id": "2", "handle": "test", "description": "retained history"}},
        )
        store.finish(complete=False, reason="page_limit")
        profile_bytes = (self.path / ".pyxcom/profile_observations.jsonl").read_bytes()
        state_path = self.path / ".pyxcom/state.json"
        state = json.loads(state_path.read_text())
        state["pages_fetched"] = 0
        write(state_path, state)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["record_integrity"], "verified")
        self.assertEqual(result["latest_checkpoint"], "regressed")
        self.assertEqual(
            profile_bytes,
            (self.path / ".pyxcom/profile_observations.jsonl").read_bytes(),
        )

    def test_receipt_state_change_does_not_alone_discredit_record_content(self):
        self.saved()
        receipt = assess_recovery(self.path)
        state_path = self.path / ".pyxcom/state.json"
        state = json.loads(state_path.read_text())
        state["pages_fetched"] = 0
        write(state_path, state)
        result = assess_recovery(self.path, previous_receipt=receipt)
        self.assertFalse(result["allowed"], result)
        self.assertIn("receipt_source_changed", result["errors"])
        self.assertEqual(result["record_integrity"], "verified")
        self.assertEqual(result["latest_checkpoint"], "regressed")

    def test_unsupported_replay_shape_blocks_checkpoint_without_discrediting_records(
        self,
    ):
        from test_comments import client, page, tweet, ROOT, A

        c = client(page(tweet(ROOT, handle="root"), tweet(A, ROOT)))
        c.save_post_comments(ROOT, self.path, max_depth=2, max_pages=2)
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["source_sha256"][".pyxcom/state.json"] = "0" * 64
        write(self.path / "manifest.json", manifest)
        log_path = self.path / ".pyxcom/collection_log.jsonl"
        logs = [json.loads(line) for line in log_path.read_text().split("\n") if line]
        for row in logs:
            if row.get("raw_path"):
                raw_path = self.path / row["raw_path"]
                write(raw_path, {"unsupported": "response shape"})
                row["raw_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        log_path.write_text("\n".join(json.dumps(row) for row in logs) + "\n")
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertIn("checkpoint_replay_unproven", result["errors"])
        self.assertEqual(result["record_integrity"], "verified")
        self.assertEqual(result["latest_checkpoint"], "unknown")

    def unpublished_timeline(self):
        from test_comments import page, tweet, ROOT, A
        from pyxcom.parse import timeline_primary_posts

        query = {
            "kind": "user_timeline",
            "handle": "other",
            "timeline": "posts",
            "since": None,
            "until": None,
        }
        store = PostStore(self.path, query=query)
        first = page(tweet(ROOT), cursor="second")
        store.archive_response(
            first,
            operation="user_timeline",
            variables={"userId": "123", "timeline": "posts", "cursor": None},
        )
        store.append_page(
            timeline_primary_posts(first, captured_at_utc="2026-01-01T00:00:00+00:00"),
            "second",
        )
        store.finish(complete=False, reason="page_limit")
        second = page(tweet(A), cursor="third")
        store.archive_response(
            second,
            operation="user_timeline",
            variables={"userId": "123", "timeline": "posts", "cursor": "second"},
        )
        store.append_page(
            timeline_primary_posts(second, captured_at_utc="2026-01-02T00:00:00+00:00"),
            "third",
        )
        return store

    def test_corrobated_unpublished_tail_is_distinct_from_source_regression(self):
        self.unpublished_timeline()
        before = {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["record_integrity"], "verified")
        self.assertEqual(result["latest_checkpoint"], "incomplete_publication")
        self.assertIn("publication_pending", result["errors"])
        self.assertTrue(result["resume_allowed"], result)
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.path.rglob("*") if p.is_file()}
        )

    def test_advanced_counter_alone_does_not_prove_unpublished_tail(self):
        self.saved()
        state_path = self.path / ".pyxcom/state.json"
        state = json.loads(state_path.read_text())
        state.update(pages_fetched=3, reason="in_progress")
        write(state_path, state)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["record_integrity"], "unknown")
        self.assertEqual(result["latest_checkpoint"], "unknown")
        self.assertIn("publication_tail_unproven", result["errors"])

    def test_unpublished_tail_missing_observation_cannot_claim_proof(self):
        self.unpublished_timeline()
        (self.path / ".pyxcom/observations.jsonl").write_text("")
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "unknown")
        self.assertIn("publication_tail_unproven", result["errors"])

    def test_unpublished_tail_wrong_cursor_cannot_claim_proof(self):
        self.unpublished_timeline()
        state_path = self.path / ".pyxcom/state.json"
        state = json.loads(state_path.read_text())
        state["cursor"] = "guessed"
        write(state_path, state)
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "unknown")

    def test_in_progress_advanced_counter_does_not_hide_private_record_regression(self):
        self.unpublished_timeline()
        (self.path / ".pyxcom/posts.jsonl").write_text("")
        (self.path / ".pyxcom/metric_snapshots.jsonl").write_text("")
        result = assess_recovery(self.path)
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["record_integrity"], "conflicting")
        self.assertIn("canonical_behind_public", result["errors"])

    def test_verified_unpublished_tail_can_resume_checkpoint_without_derived_only_repair(
        self,
    ):
        store = self.unpublished_timeline()
        before = (self.path / ".pyxcom/posts.jsonl").read_bytes()
        query = store.state["query"]
        report = assess_recovery(self.path)
        self.assertFalse(report["allowed"], report)
        self.assertTrue(report["resume_allowed"], report)
        resumed = PostStore(self.path, query=query)
        self.assertEqual(resumed.cursor, "third")
        self.assertEqual(resumed.state["pages_fetched"], 2)
        self.assertEqual(resumed.count, 2)
        resumed.finish(complete=False, reason="source_error")

        self.assertTrue(validate_tables(self.path)["valid"])
        self.assertTrue(validate_collection(self.path)["valid"])
        self.assertEqual(before, (self.path / ".pyxcom/posts.jsonl").read_bytes())
        self.assertTrue(assess_recovery(self.path)["allowed"])

    def test_unpublished_tail_wrong_expected_query_is_not_resumable(self):
        self.unpublished_timeline()
        result = assess_recovery(self.path, expected_query={"kind": "different"})
        self.assertFalse(result["allowed"])
        self.assertFalse(result["resume_allowed"], result)
        self.assertIn("query_mismatch", result["errors"])

    def test_unpublished_tail_receipt_or_binding_conflict_is_not_resumable(self):
        self.unpublished_timeline()
        result = assess_recovery(
            self.path,
            binding={"observer_id": "1"},
            previous_receipt={"binding": {"observer_id": "2"}},
        )
        self.assertFalse(result["resume_allowed"], result)
        self.assertIn("receipt_binding_mismatch", result["errors"])
        current = assess_recovery(self.path)
        receipt = {
            "query": current["query"],
            "source_sha256": {".pyxcom/state.json": "0" * 64},
        }
        result = assess_recovery(self.path, previous_receipt=receipt)
        self.assertFalse(result["resume_allowed"], result)
        self.assertIn("receipt_source_changed", result["errors"])

    def test_unpublished_tail_unsupported_source_conflict_is_not_resumable(self):
        self.unpublished_timeline()
        manifest = json.loads((self.path / "manifest.json").read_text())
        manifest["source_sha256"][".pyxcom/profile_observations.jsonl"] = "0" * 64
        write(self.path / "manifest.json", manifest)
        result = assess_recovery(self.path)
        self.assertFalse(result["resume_allowed"], result)
        self.assertIn(
            "source_hash_mismatch:.pyxcom/profile_observations.jsonl", result["errors"]
        )

    def test_normal_anchor_is_resumable_and_unknown_tail_is_not(self):
        self.saved()
        self.assertTrue(assess_recovery(self.path)["resume_allowed"])
        state_path = self.path / ".pyxcom/state.json"
        state = json.loads(state_path.read_text())
        state.update(pages_fetched=2, reason="in_progress", cursor="unknown")
        write(state_path, state)
        self.assertFalse(assess_recovery(self.path)["resume_allowed"])

    def test_unpublished_tail_tampered_metric_payload_is_not_resumable(self):
        self.unpublished_timeline()
        metric_path = self.path / ".pyxcom/metric_snapshots.jsonl"
        lines = metric_path.read_text().split("\n")
        rows = [json.loads(line) for line in lines if line]
        rows[-1]["like_count"] = 999999
        metric_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        result = assess_recovery(self.path)
        self.assertFalse(result["resume_allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "unknown")
        self.assertIn("publication_tail_unproven", result["errors"])

    def test_unpublished_tail_missing_metric_observation_is_not_resumable(self):
        self.unpublished_timeline()
        metric_path = self.path / ".pyxcom/metric_snapshots.jsonl"
        lines = metric_path.read_bytes().split(b"\n")
        metric_path.write_bytes(lines[0] + b"\n")
        result = assess_recovery(self.path)
        self.assertFalse(result["resume_allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "unknown")
        self.assertIn("publication_tail_unproven", result["errors"])

    def test_repeated_post_tail_observation_rollback_is_not_resumable(self):
        from test_comments import page, tweet, ROOT
        from pyxcom.parse import timeline_primary_posts

        store = self.unpublished_timeline()
        observation_path = self.path / ".pyxcom/observations.jsonl"
        metric_path = self.path / ".pyxcom/metric_snapshots.jsonl"
        before_observations = observation_path.read_bytes()
        before_metrics = metric_path.read_bytes()
        updated = tweet(ROOT)
        updated["legacy"]["favorite_count"] = 999
        third = page(updated, cursor="fourth")
        store.archive_response(
            third,
            operation="user_timeline",
            variables={"userId": "123", "timeline": "posts", "cursor": "third"},
        )
        store.append_page(
            timeline_primary_posts(third, captured_at_utc="2026-01-03T00:00:00+00:00"),
            "fourth",
        )
        complete_tail = assess_recovery(self.path)
        self.assertTrue(complete_tail["resume_allowed"], complete_tail)
        # Same unique IDs and canonical first observations survive; the third
        # metric observation and its multiplicity still must be accounted for.
        observation_path.write_bytes(before_observations)
        metric_path.write_bytes(before_metrics)
        result = assess_recovery(self.path)
        self.assertFalse(result["resume_allowed"], result)
        self.assertEqual(result["latest_checkpoint"], "unknown")
        self.assertIn("publication_tail_unproven", result["errors"])
