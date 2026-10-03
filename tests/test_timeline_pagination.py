import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pyxcom import APIError, Profile, XClient, validate_collection
from test_discovery import page, tweet


def timeline(*identifiers, cursor="next", dates=None, pinned=False, end=False):
    items = []
    for index, identifier in enumerate(identifiers):
        item = tweet(identifier)
        if dates:
            item["legacy"]["created_at"] = dates[index]
        items.append(item)
    result = page(*items, cursor=None if end else cursor, end=end)
    if pinned:
        pin = tweet("1999999999")
        pin["legacy"]["created_at"] = "Tue Sep 29 17:19:49 +0000 2026"
        instructions = result["data"]["search_by_raw_query"]["search_timeline"][
            "timeline"
        ]["instructions"]
        instructions.insert(
            0,
            {
                "type": "TimelinePinEntry",
                "entry": {
                    "entryId": "tweet-1999999999",
                    "content": {
                        "itemContent": {
                            "tweet_results": {"result": pin},
                            "socialContext": {"contextType": "Pin", "text": "Pinned"},
                        }
                    },
                },
            },
        )
    return result


class TimelinePaginationTests(unittest.TestCase):
    def client(self, payloads):
        client = object.__new__(XClient)
        client.get_user = Mock(return_value=Profile("1", "sample", "Sample"))
        client._x = Mock()
        client._x.user_timeline_page.side_effect = payloads
        client.delay = 0
        return client

    def state(self, directory):
        return json.loads((Path(directory) / ".pyxcom/state.json").read_text())

    def test_content_cycle_crosses_budgets_and_latches_partial(self):
        client = self.client([timeline("1234567900", cursor=f"c{i}") for i in range(6)])
        with tempfile.TemporaryDirectory() as tmp:
            first = client.save_user_posts("sample", tmp, max_pages=3)
            self.assertEqual(first.reason, "page_limit")
            second = client.save_user_posts("sample", tmp, max_pages=3)
            self.assertEqual(second.reason, "no_progress_limit")
            self.assertFalse(second.complete)
            self.assertEqual(second.pages_fetched, 6)
            saved = self.state(tmp)
            self.assertEqual(saved["cursor"], "c5")
            self.assertEqual(
                saved["timeline_pagination"]["consecutive_no_progress_pages"], 5
            )
            for _ in range(2):
                self.assertEqual(
                    client.save_user_posts("sample", tmp, max_pages=3).reason,
                    "no_progress_limit",
                )
            self.assertEqual(client._x.user_timeline_page.call_count, 6)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(manifest["timeline_pagination"]["status"], "paused")
            self.assertTrue(validate_collection(tmp)["valid"])

    def test_fresh_source_ids_continue_even_when_all_date_filtered(self):
        client = self.client(
            [timeline(str(1234567900 + i), cursor=f"c{i}") for i in range(8)]
        )
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                result = client.save_user_posts(
                    "sample", tmp, until="2020-01-01", max_pages=4
                )
                self.assertEqual(result.reason, "page_limit")
                self.assertEqual(result.post_count, 0)
            self.assertEqual(
                self.state(tmp)["timeline_pagination"]["source_ids_seen"], 8
            )
            self.assertEqual(client._x.user_timeline_page.call_count, 8)

    def test_fresh_context_source_ids_are_not_an_empty_timeline(self):
        payloads = [
            page(tweet(str(1234567900 + i), author="9"), cursor=f"c{i}")
            for i in range(6)
        ]
        client = self.client(payloads)
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_user_replies("sample", tmp, max_pages=6)
            self.assertEqual(result.reason, "page_limit")
            self.assertFalse(result.complete)
            self.assertEqual(result.post_count, 0)
            self.assertEqual(client._x.user_timeline_page.call_count, 6)
            self.assertEqual(self.state(tmp)["timeline_pagination"]["source_ids_seen"], 6)

    def test_recent_pin_does_not_mask_old_pages_across_calls(self):
        client = self.client(
            [
                timeline(
                    "1234567900",
                    cursor="a",
                    dates=["Tue Sep 30 00:00:00 +0000 2025"],
                    pinned=True,
                ),
                timeline(
                    "1234567901",
                    cursor="b",
                    dates=["Wed Sep 03 00:00:00 +0000 2025"],
                    pinned=True,
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = client.save_user_posts(
                "sample", tmp, since="2025-10-01", max_pages=1
            )
            self.assertEqual(first.reason, "page_limit")
            second = client.save_user_posts(
                "sample", tmp, since="2025-10-01", max_pages=1
            )
            self.assertEqual(second.reason, "passed_since")
            self.assertTrue(second.complete)
            self.assertEqual(second.post_count, 1)
            self.assertTrue(validate_collection(tmp)["valid"])

    def test_pinned_only_pages_pause_instead_of_completing(self):
        client = self.client([timeline(cursor=f"c{i}", pinned=True) for i in range(6)])
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_user_posts(
                "sample", tmp, since="2025-10-01", max_pages=10
            )
            self.assertEqual(result.reason, "no_progress_limit")
            self.assertFalse(result.complete)
            self.assertEqual(result.post_count, 1)

    def test_fresh_body_resets_streak_with_a_repeated_pin(self):
        client = self.client(
            [
                timeline(str(1234567900 + i), cursor=f"c{i}", pinned=True)
                for i in range(8)
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                self.assertEqual(
                    client.save_user_posts("sample", tmp, max_pages=4).reason,
                    "page_limit",
                )
            self.assertEqual(
                self.state(tmp)["timeline_pagination"]["consecutive_no_progress_pages"],
                0,
            )

    def test_memory_iterator_signals_content_cycle(self):
        client = self.client([timeline("1234567900", cursor=f"c{i}") for i in range(6)])
        with self.assertRaisesRegex(APIError, "no_progress_limit"):
            client.get_user_posts("sample")
        self.assertEqual(client._x.user_timeline_page.call_count, 6)

    def test_same_recent_ordinary_post_is_not_inferred_as_pinned(self):
        recent = "Tue Sep 29 17:19:49 +0000 2026"
        old = "Tue Sep 30 00:00:00 +0000 2025"
        client = self.client(
            [
                timeline("1234567900", "1234567901", cursor="a", dates=[recent, old]),
                timeline("1234567900", "1234567902", cursor="b", dates=[recent, old]),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_user_posts(
                "sample", tmp, since="2025-10-01", max_pages=2
            )
            self.assertEqual(result.reason, "page_limit")
            self.assertFalse(result.complete)

    def test_repeated_old_body_does_not_claim_date_completion(self):
        old = "Tue Sep 30 00:00:00 +0000 2025"
        client = self.client(
            [
                timeline("1234567900", cursor=f"c{i}", dates=[old], pinned=True)
                for i in range(6)
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_user_posts(
                "sample", tmp, since="2025-10-01", max_pages=10
            )
            self.assertEqual(result.reason, "no_progress_limit")
            self.assertFalse(result.complete)

    def test_legacy_saved_history_seeds_cycle_without_request(self):
        from pyxcom.storage import PostStore
        from pyxcom.parse import timeline_primary_posts

        query = {
            "kind": "user_timeline",
            "handle": "sample",
            "timeline": "posts",
            "since": None,
            "until": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query=query)
            cursor = None
            for i in range(6):
                payload = timeline("1234567900", cursor=f"c{i}")
                store.archive_response(
                    payload,
                    operation="user_timeline",
                    variables={"userId": "1", "timeline": "posts", "cursor": cursor},
                )
                store.append_page(timeline_primary_posts(payload), f"c{i}")
                cursor = f"c{i}"
            store.finish(complete=False, reason="page_limit")
            raw = {
                p.name: p.read_bytes()
                for p in (Path(tmp) / ".pyxcom/raw").glob("*.json")
            }
            client = self.client([])
            result = client.save_user_posts("sample", tmp, max_pages=5)
            self.assertEqual(result.reason, "no_progress_limit")
            self.assertEqual(client._x.user_timeline_page.call_count, 0)
            self.assertEqual(self.state(tmp)["cursor"], "c5")
            self.assertEqual(
                raw,
                {
                    p.name: p.read_bytes()
                    for p in (Path(tmp) / ".pyxcom/raw").glob("*.json")
                },
            )
            self.assertTrue(validate_collection(tmp)["valid"])

    def test_no_cursor_source_end_remains_complete(self):
        client = self.client([timeline("1234567900", end=True)])
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_user_posts("sample", tmp)
            self.assertEqual(result.reason, "source_end")
            self.assertTrue(result.complete)
