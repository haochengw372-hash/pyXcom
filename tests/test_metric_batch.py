"""Page metric writes preserve observation order and checkpoint failure boundaries."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pyxcom._persistence import append_jsonl, read_jsonl
from pyxcom.models import Post
from pyxcom.storage import PostStore


def post(identifier="100", likes=3):
    return Post(
        id=identifier,
        author_id="42",
        author_handle="fixture",
        created_at_utc="2026-01-01T00:00:00Z",
        text="synthetic",
        url="https://example.invalid/post",
        like_count=likes,
        captured_at_utc="2026-10-12T00:00:00Z",
    )


class MetricBatchTests(unittest.TestCase):
    def test_one_durable_metric_append_per_page_with_new_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={"kind": "fixture"})
            with patch("pyxcom.storage.append_jsonl", wraps=append_jsonl) as writer:
                store.append_page([post(str(i)) for i in range(10)], "next")
            metric_calls = [
                call
                for call in writer.call_args_list
                if call.args[0].name == "metric_snapshots.jsonl"
            ]
            self.assertEqual(len(metric_calls), 1)
            self.assertEqual(
                len(list(read_jsonl(Path(tmp) / ".pyxcom/metric_snapshots.jsonl"))), 10
            )

    def test_digest_order_duplicates_and_raw_observations_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={"kind": "fixture"})
            first = post()
            updated = replace(first, like_count=9)
            missing = post("102", likes=None)
            observations = [first, first, updated, post("101", likes=0), missing]
            self.assertEqual(store.append_page(observations, "a"), 3)
            with patch("pyxcom.storage.append_jsonl", wraps=append_jsonl) as writer:
                store.append_page(observations, "b")
            self.assertFalse(
                any(
                    c.args[0].name == "metric_snapshots.jsonl"
                    for c in writer.call_args_list
                )
            )
            internal = Path(tmp) / ".pyxcom"
            metrics = list(read_jsonl(internal / "metric_snapshots.jsonl"))
            self.assertEqual([row["like_count"] for row in metrics], [3, 9, 0])
            for row in metrics:
                snapshot_id = row.pop("snapshot_id")
                self.assertEqual(
                    snapshot_id,
                    hashlib.sha256(
                        json.dumps(row, sort_keys=True).encode()
                    ).hexdigest(),
                )
            self.assertEqual(
                list(read_jsonl(internal / "observations.jsonl")),
                [item.to_dict() for item in observations] * 2,
            )
            self.assertEqual(
                list(read_jsonl(internal / "posts.jsonl"))[0]["like_count"], 3
            )
            store.finish(complete=False, reason="page_limit")
            restored = PostStore(tmp, query={"kind": "fixture"})
            with patch("pyxcom.storage.append_jsonl", wraps=append_jsonl) as writer:
                restored.append_page(observations, "c")
            self.assertFalse(
                any(
                    c.args[0].name == "metric_snapshots.jsonl"
                    for c in writer.call_args_list
                )
            )

    def test_failed_metric_write_does_not_advance_checkpoint_or_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={"kind": "fixture"})

            def fail_metrics(path, records, **kwargs):
                if path.name == "metric_snapshots.jsonl":
                    raise OSError("simulated disk error")
                return append_jsonl(path, records, **kwargs)

            with patch("pyxcom.storage.append_jsonl", side_effect=fail_metrics):
                with self.assertRaises(OSError):
                    store.append_page([post(), post("101")], "next")
            self.assertEqual(store.count, 0)
            self.assertIsNone(store.cursor)
            self.assertEqual(store.state["pages_fetched"], 0)
            self.assertFalse(store._metrics)
            self.assertFalse((Path(tmp) / ".pyxcom/observations.jsonl").exists())
            self.assertEqual(store.append_page([post(), post("101")], "next"), 2)
            self.assertEqual(
                len(list(read_jsonl(Path(tmp) / ".pyxcom/metric_snapshots.jsonl"))), 2
            )

    def test_metrics_are_durable_before_post_writes_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query={"kind": "fixture"})
            events = []

            def write(path, records, **kwargs):
                events.append(path.name)
                return append_jsonl(path, records, **kwargs)

            with patch("pyxcom.storage.append_jsonl", side_effect=write):
                store.append_page([post(), post("101")], "next")
            self.assertEqual(
                events, ["metric_snapshots.jsonl", "observations.jsonl", "posts.jsonl"]
            )


if __name__ == "__main__":
    unittest.main()
