import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from test_discovery import Client, page, tweet
from pyxcom.discovery import _query_options
from pyxcom.storage import PostStore
from pyxcom.layout import source_path


class SearchScopeTests(unittest.TestCase):
    def test_explicit_days_use_precise_utc_request_bounds(self):
        query, since, until = _query_options(
            "AI lang:en", "2026-09-01", "2026-09-08", 1, None
        )
        self.assertEqual((since, until), ("2026-09-01", "2026-09-08"))
        self.assertEqual(
            query,
            "AI lang:en since:2026-09-01 until:2026-09-08 since_time:1788220800 until_time:1788825600",
        )

    def test_native_query_without_explicit_days_is_unchanged(self):
        for query in (
            "AI since:2026-09-01 until:2026-09-08",
            "AI since_time:1788220800 until_time:1788825600",
        ):
            self.assertEqual(_query_options(query, None, None, 1, None)[0], query)

    def test_conflicting_epoch_operators_fail_before_request(self):
        client = Client()
        for query in ("AI since_time:1", "AI until_time:1", "AI since_time:invalid"):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                client.get_search_query(query, since="2026-09-01", until="2026-09-08")
        client._x.search_page.assert_not_called()
        query = _query_options('"until_time:1" AI', None, "2026-09-08", 1, None)[0]
        self.assertIn("until_time:1788825600", query)

    def test_precise_query_is_archived_and_local_window_stays_half_open(self):
        client = Client()
        client._x.search_page.return_value = page(
            tweet("1234567891", created="Tue Sep 01 00:00:00 +0000 2026"),
            tweet("1234567892", created="Mon Sep 07 23:59:59 +0000 2026"),
            tweet("1234567893", created="Tue Sep 08 00:00:00 +0000 2026"),
            end=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_search_query(
                "AI", tmp, since="2026-09-01", until="2026-09-08", max_pages=1
            )
            self.assertEqual(result.post_count, 2)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            requested = client._x.search_page.call_args.args[0]
            self.assertIn("until_time:1788825600", requested)
            self.assertEqual(manifest["query"]["effective_query"], requested)
            rows = [
                json.loads(line)
                for line in source_path(Path(tmp), "collection_log.jsonl")
                .read_text()
                .splitlines()
            ]
            response = next(
                row for row in rows if row.get("status") == "response_received"
            )
            self.assertEqual(response["variables"]["rawQuery"], requested)
            self.assertEqual(manifest["date_scope"]["out_of_window_observations"], 1)

    def test_legacy_checkpoint_retains_its_original_query_and_cursor(self):
        client = Client()
        client._x.search_page.return_value = page(tweet("1234567891"), end=True)
        query = {
            "kind": "search_query",
            "query": "AI",
            "effective_query": "AI since:2026-09-01 until:2026-09-08",
            "since": "2026-09-01",
            "until": "2026-09-08",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query=query)
            store.state["cursor"] = "original-cursor"
            store.finish(complete=False, reason="page_limit")
            client.save_search_query(
                "AI", tmp, since="2026-09-01", until="2026-09-08", max_pages=1
            )
            self.assertEqual(
                client._x.search_page.call_args.args[0], query["effective_query"]
            )
            self.assertEqual(
                client._x.search_page.call_args.kwargs["cursor"], "original-cursor"
            )

    def test_timezone_independent_one_sided_epoch(self):
        expected = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
        query = _query_options("AI", "2026-09-01", None, 1, None)[0]
        self.assertEqual(query, f"AI since:2026-09-01 since_time:{expected}")

    def test_legacy_quote_checkpoint_keeps_target_query_and_cursor(self):
        client = Client()
        client._x.search_page.return_value = page(
            tweet("1234567891", quote="1234567890"), end=True
        )
        query = {
            "kind": "post_quotes",
            "post_id": "1234567890",
            "effective_query": "quoted_tweet_id:1234567890 since:2026-09-01 until:2026-09-08",
            "since": "2026-09-01",
            "until": "2026-09-08",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = PostStore(tmp, query=query)
            store.state["cursor"] = "quote-cursor"
            store.finish(complete=False, reason="page_limit")
            result = client.save_post_quotes(
                "1234567890", tmp, since="2026-09-01", until="2026-09-08", max_pages=1
            )
            self.assertEqual(result.post_count, 1)
            self.assertEqual(
                client._x.search_page.call_args.args[0], query["effective_query"]
            )
            self.assertEqual(
                client._x.search_page.call_args.kwargs["cursor"], "quote-cursor"
            )
            self.assertEqual(
                json.loads((Path(tmp) / "manifest.json").read_text())["query"], query
            )

    def test_fresh_quote_query_uses_precise_dates(self):
        client = Client()
        client._x.search_page.return_value = page(
            tweet("1234567891", quote="1234567890"), end=True
        )
        with tempfile.TemporaryDirectory() as tmp:
            client.save_post_quotes(
                "1234567890", tmp, since="2026-09-01", until="2026-09-08", max_pages=1
            )
            requested = client._x.search_page.call_args.args[0]
            self.assertEqual(
                requested,
                "quoted_tweet_id:1234567890 since:2026-09-01 until:2026-09-08 since_time:1788220800 until_time:1788825600",
            )
