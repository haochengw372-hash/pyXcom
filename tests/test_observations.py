"""Offline tests for immutable observations, graph edges, and raw-page audit."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from pyxcom.client import XClient
from pyxcom.models import Post
from pyxcom.storage import PostStore
from pyxcom.tables import export_tables
from pyxcom.validate import validate_collection, finalize_collection
from pyxcom.layout import migrate_collection
from pyxcom.schema import schema_summary


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def post(identifier="1234567890", **kwargs):
    return Post(
        identifier,
        "22",
        "sample",
        "2026-01-01T00:00:00+00:00",
        "own expression",
        "https://x.com/sample/status/" + identifier,
        **kwargs,
    )


class ObservationTests(unittest.TestCase):
    def test_duplicate_id_preserves_two_observations_and_metric_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            store = PostStore(output, query={})
            first = post(like_count=1, captured_at_utc="2026-01-02T00:00:00+00:00")
            second = replace(
                first, like_count=7, captured_at_utc="2026-01-03T00:00:00+00:00"
            )
            self.assertEqual(store.append_page([first], None), 1)
            self.assertEqual(store.append_page([second], None), 0)
            store.finish(complete=True, reason="source_end")
            self.assertEqual(len(rows(output / "posts.csv")), 1)
            metrics = rows(output / "metric_snapshots.csv")
            self.assertEqual([r["like_count"] for r in metrics], ["1", "7"])
            self.assertEqual(
                len((output / ".pyxcom/observations.jsonl").read_text().split("\n"))
                - 1,
                2,
            )
            self.assertTrue(validate_collection(output)["valid"])

    def test_legacy_unknown_metric_time_is_not_fabricated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={})
            store.append_page([post(like_count=3)], None)
            store.finish(complete=True, reason="source_end")
            metric = rows(Path(tmp) / "metric_snapshots.csv")[0]
            self.assertEqual(metric["retrieved_at_utc"], "")
            self.assertEqual(metric["time_status"], "unknown")

    def test_context_is_not_analysis_and_edges_resolve_authors(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            store = PostStore(tmp, query={})
            parent = replace(
                post("1234567890"), author_id="77", observation_role="context"
            )
            reply = post(
                "1234567891",
                conversation_id=parent.id,
                in_reply_to_id=parent.id,
                quoted_post_id="1234567892",
                quoted_author_id="55",
                raw_json={"own": "text"},
            )
            store.append_page([parent, reply], None)
            store.finish(complete=True, reason="source_end")
            self.assertEqual(rows(output / "posts.csv"), [])
            self.assertEqual(len(rows(output / "comments.csv")), 1)
            self.assertEqual(len(rows(output / "context_posts.csv")), 1)
            edges = rows(output / "post_edges.csv")
            self.assertEqual(
                {
                    (r["edge_type"], r["source_user_id"], r["target_user_id"])
                    for r in edges
                },
                {("reply", "22", "77"), ("quote", "22", "55")},
            )
            self.assertEqual(
                json.loads(rows(output / "comments.csv")[0]["raw_json"]),
                {"own": "text"},
            )
            self.assertTrue(validate_collection(output)["valid"])

    def test_native_repost_does_not_create_inherited_reply_or_quote_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={})
            action = post(
                in_reply_to_id="1234567891",
                in_reply_to_user_id="33",
                quoted_post_id="1234567892",
                quoted_author_id="44",
                reposted_post_id="1234567893",
                reposted_author_id="55",
            )
            store.append_page([action], None)
            store.finish(complete=True, reason="source_end")
            edges = rows(Path(tmp) / "post_edges.csv")
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0]["edge_type"], "repost")
            self.assertEqual(edges[0]["target_user_id"], "55")

    def test_post_export_cannot_mutate_network_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "network_manifest.json").write_text("{}")
            (output / "users.csv").write_text("user_id\n22\n")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(ValueError):
                PostStore(tmp, query={})
            with self.assertRaises(ValueError):
                export_tables(tmp)
            for operation in (migrate_collection, finalize_collection, schema_summary):
                with self.assertRaises(ValueError):
                    operation(tmp)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_every_dynamic_reply_response_is_archived(self):
        root = "1234567890"
        c = XClient.__new__(XClient)
        c.delay = 0
        c.get_post = Mock(return_value=post(root))
        c._x = Mock()
        index = [0]

        def response(*args, **kwargs):
            index[0] += 1
            i = index[0]
            result = {
                "rest_id": str(int(root) + i),
                "legacy": {
                    "user_id_str": "22",
                    "created_at": "Mon Sep 29 07:39:00 +0000 2025",
                    "full_text": str(i),
                    "conversation_id_str": root,
                    "in_reply_to_status_id_str": root,
                    "reply_count": 0,
                },
                "core": {
                    "user_results": {
                        "result": {"rest_id": "22", "core": {"screen_name": "sample"}}
                    }
                },
            }
            entries = [
                {"content": {"itemContent": {"tweet_results": {"result": result}}}}
            ]
            if i < 30:
                entries.append({"content": {"cursorType": "Bottom", "value": str(i)}})
            return {"data": {"conversation": {"instructions": [{"entries": entries}]}}}

        c._x.conversation_page.side_effect = response
        with tempfile.TemporaryDirectory() as tmp:
            result = c.save_post_comments(root, tmp, max_comments=None, max_pages=None)
            self.assertTrue(result.complete)
            self.assertEqual(result.pages_fetched, 30)
            self.assertEqual(len(list((Path(tmp) / ".pyxcom/raw").glob("*.json"))), 30)
            self.assertTrue(validate_collection(tmp)["valid"])

    def test_raw_request_log_does_not_save_credential_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={})
            store.archive_response(
                {"data": {"id": "123"}},
                operation="read",
                variables={
                    "rawQuery": "reset",
                    "auth_token": "dummy-secret",
                    "ct0": "dummy-secret",
                },
            )
            log = (Path(tmp) / ".pyxcom/collection_log.jsonl").read_text()
            self.assertNotIn("dummy-secret", log)
            self.assertIn("reset", log)

    def test_raw_archive_tampering_fails_offline_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={})
            raw = store.archive_response(
                {"data": {"id": "123"}}, operation="read", variables={}
            )
            store.append_page([post()], None)
            store.finish(complete=True, reason="source_end")
            self.assertTrue(validate_collection(tmp)["valid"])
            raw.write_text('{"data": "changed"}')
            self.assertFalse(validate_collection(tmp)["valid"])
