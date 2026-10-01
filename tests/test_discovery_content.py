"""Readable rows survive unavailable slots; unknown rows retain partial coverage."""

import json
import tempfile
import unittest
from pathlib import Path

from pyxcom.discovery import _source_page
from pyxcom.layout import source_path
from test_discovery import Client, page, tweet


def empty_result_page(*readable, cursor=None, end=False):
    payload = page(*readable, tweet("1234567999"), cursor=cursor, end=end)
    entries = payload["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
        "instructions"
    ][0]["entries"]
    entries[len(readable)]["content"]["itemContent"]["tweet_results"] = {"result": {}}
    return payload


class DiscoveryContentTests(unittest.TestCase):
    def test_empty_tweet_result_is_unavailable_not_a_source_empty_page(self):
        audit = {}
        posts, empty = _source_page(empty_result_page(tweet("1234567900")), audit=audit)
        self.assertEqual([p.id for p in posts], ["1234567900"])
        self.assertFalse(empty)
        self.assertEqual(audit["primary_item_count"], 2)
        self.assertEqual(audit["unavailable_items"], 1)
        self.assertEqual(audit["unavailable_post_ids"], ["1234567999"])

    def test_save_continues_and_retains_partial_coverage_at_source_end(self):
        client = Client()
        client._x.search_page.side_effect = [
            empty_result_page(tweet("1234567900"), cursor="next"),
            page(tweet("1234567901"), end=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_search_query("quota", tmp, max_pages=5)
            self.assertEqual(result.post_count, 2)
            self.assertEqual(result.reason, "partial_source_content")
            self.assertFalse(result.complete)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(
                manifest["source_content"]["unavailable_primary_observations"], 1
            )
            self.assertEqual(len(list(source_path(tmp, "raw").glob("*.json"))), 2)
            client.save_search_query("quota", tmp, max_pages=5)
            self.assertEqual(client._x.search_page.call_count, 2)

    def test_unknown_tweet_saves_known_rows_and_keeps_original_cursor(self):
        malformed = tweet("1234567999")
        malformed["legacy"].pop("created_at")
        client = Client()
        client._x.search_page.return_value = page(
            tweet("1234567900"), malformed, cursor="next"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_search_query("quota", tmp, max_pages=5)
            self.assertEqual(result.post_count, 1)
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "partial_source_content")
            state = json.loads(source_path(tmp, "state.json").read_text())
            self.assertIsNone(state["cursor"])
            self.assertEqual(
                state["source_content"]["unparsed_post_ids"], ["1234567999"]
            )
            self.assertEqual(
                state["discovery_pagination"]["consecutive_source_empty_pages"], 0
            )

    def test_memory_results_expose_partial_status_without_dropping_readable_rows(self):
        client = Client()
        client._x.search_page.return_value = empty_result_page(
            tweet("1234567900"), end=True
        )
        posts = client.get_search_query("quota")
        self.assertEqual([p.id for p in posts], ["1234567900"])
        self.assertFalse(client.last_discovery_collection["complete"])
        self.assertEqual(
            client.last_discovery_collection["reason"], "partial_source_content"
        )

    def test_only_unavailable_slots_do_not_increment_source_empty_streak(self):
        client = Client()
        client._x.search_page.side_effect = [
            empty_result_page(cursor=f"next-{i}") for i in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_search_query("quota", tmp, max_pages=3)
            self.assertEqual(result.reason, "page_limit")
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(
                manifest["discovery_pagination"]["consecutive_source_empty_pages"], 0
            )
            self.assertEqual(
                manifest["source_content"]["unavailable_primary_observations"], 3
            )

    def test_repeated_cursor_retains_readable_rows_and_unavailable_warning(self):
        client = Client()
        client._x.search_page.return_value = empty_result_page(
            tweet("1234567900"), cursor="next"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_search_query("quota", tmp, max_pages=5)
            self.assertEqual(result.post_count, 1)
            self.assertEqual(result.reason, "repeated_cursor")
            self.assertFalse(result.complete)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertTrue(manifest["source_content"]["coverage_warning"])
            self.assertEqual(
                manifest["source_content"]["unavailable_primary_observations"], 2
            )
            self.assertEqual(
                manifest["discovery_pagination"]["consecutive_source_empty_pages"], 0
            )
