"""Pause genuinely stalled source pagination without confusing local filters."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pyxcom.errors import APIError
from pyxcom.layout import source_path
from pyxcom.parse import timeline_primary_posts
from test_discovery import Client, TARGET, page, tweet


def instructions(payload):
    return payload["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
        "instructions"
    ]


def cursor_page(value):
    payload = page()
    instructions(payload)[:] = [
        {
            "type": "TimelineReplaceEntry",
            "entry_id_to_replace": f"cursor-{kind.lower()}",
            "entry": {
                "entryId": f"cursor-{kind.lower()}",
                "content": {
                    "entryType": "TimelineTimelineCursor",
                    "cursorType": kind,
                    "value": value if kind == "Bottom" else "top",
                },
            },
        }
        for kind in ("Top", "Bottom")
    ]
    return payload


class DiscoveryPaginationTests(unittest.TestCase):
    def test_empty_streak_crosses_page_budgets_and_pauses_without_more_requests(self):
        client = Client()
        client._x.search_page.side_effect = [
            *[page(tweet(str(1234567900 + i)), cursor=f"active-{i}") for i in range(4)],
            cursor_page("empty-1"),
            cursor_page("empty-2"),
            cursor_page("empty-3"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            first = client.save_search_query("reset", tmp, max_pages=5)
            self.assertEqual(first.reason, "page_limit")
            state = json.loads(source_path(tmp, "state.json").read_text())
            self.assertEqual(
                state["discovery_pagination"]["consecutive_source_empty_pages"], 1
            )
            second = client.save_search_query("reset", tmp, max_pages=5)
            self.assertEqual(second.reason, "empty_page_limit")
            self.assertFalse(second.complete)
            self.assertEqual(second.pages_fetched, 7)
            state = json.loads(source_path(tmp, "state.json").read_text())
            self.assertEqual(state["cursor"], "empty-3")
            self.assertEqual(state["discovery_pagination"]["status"], "paused")
            for _ in range(3):
                paused = client.save_search_query("reset", tmp, max_pages=5)
                self.assertEqual(paused.reason, "empty_page_limit")
            self.assertEqual(client._x.search_page.call_count, 7)
            self.assertEqual(len(list(source_path(tmp, "raw").glob("*.json"))), 7)
            public = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(
                public["discovery_pagination"]["consecutive_source_empty_pages"], 3
            )

    def test_explicit_retry_keeps_cursor_raw_and_seen_ids(self):
        client = Client()
        client._x.search_page.side_effect = [
            cursor_page(f"empty-{i}") for i in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            client.save_search_query("reset", tmp, max_pages=5)
            raw = source_path(tmp, "raw")
            before = {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in raw.glob("*.json")
            }
            client._x.search_page.side_effect = None
            client._x.search_page.return_value = page(tweet("1234567900"), end=True)
            result = client.save_search_query(
                "reset", tmp, max_pages=5, retry_stalled=True
            )
            self.assertTrue(result.complete)
            self.assertEqual(result.post_count, 1)
            self.assertEqual(
                client._x.search_page.call_args.kwargs["cursor"], "empty-2"
            )
            for name, digest in before.items():
                self.assertEqual(
                    hashlib.sha256((raw / name).read_bytes()).hexdigest(), digest
                )
            state = json.loads(source_path(tmp, "state.json").read_text())
            self.assertIn("1234567900", state["discovery_source_ids"])
            self.assertEqual(
                state["discovery_pagination"]["consecutive_source_empty_pages"], 0
            )

    def test_fresh_content_pagination_continues_across_calls(self):
        client = Client()
        client._x.search_page.side_effect = [
            page(
                tweet(str(1234567900 + i)),
                cursor=None if i == 6 else f"next-{i}",
                end=i == 6,
            )
            for i in range(7)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            first = client.save_search_query("reset", tmp, max_pages=5)
            self.assertEqual(first.reason, "page_limit")
            result = client.save_search_query("reset", tmp, max_pages=5)
            self.assertTrue(result.complete)
            self.assertEqual(result.post_count, 7)
            audit = json.loads((Path(tmp) / "manifest.json").read_text())[
                "discovery_pagination"
            ]
            self.assertEqual(audit["consecutive_no_progress_pages"], 0)

    def test_fresh_tweets_filtered_by_quote_target_are_not_empty(self):
        client = Client()
        client._x.search_page.side_effect = [
            *[
                page(tweet(str(1234567900 + i), quote="1111111111"), cursor=f"next-{i}")
                for i in range(6)
            ],
            page(tweet("1234568000", quote=TARGET), end=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            first = client.save_post_quotes(TARGET, tmp, max_pages=5)
            self.assertEqual(first.reason, "page_limit")
            self.assertEqual(first.post_count, 0)
            result = client.save_post_quotes(TARGET, tmp, max_pages=5)
            self.assertEqual(result.post_count, 1)
            self.assertTrue(result.complete)
            audit = json.loads((Path(tmp) / "manifest.json").read_text())[
                "discovery_pagination"
            ]
            self.assertEqual(audit["consecutive_source_empty_pages"], 0)
            self.assertEqual(audit["source_ids_seen"], 7)

    def test_duplicate_source_pages_pause_and_remain_latched(self):
        client = Client()
        client._x.search_page.side_effect = [
            page(tweet("1234567900"), cursor=f"next-{i}") for i in range(6)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            first = client.save_search_query("reset", tmp, max_pages=5)
            self.assertEqual(first.reason, "page_limit")
            result = client.save_search_query("reset", tmp, max_pages=5)
            self.assertEqual(result.reason, "no_progress_limit")
            self.assertFalse(result.complete)
            self.assertEqual(result.post_count, 1)
            client.save_search_query("reset", tmp, max_pages=5)
            self.assertEqual(client._x.search_page.call_count, 6)

    def test_partially_consumed_page_replays_do_not_trigger_stall(self):
        client = Client()
        client._x.search_page.return_value = page(
            *[tweet(str(1234567900 + i)) for i in range(8)], cursor="next"
        )
        with tempfile.TemporaryDirectory() as tmp:
            for cap in range(1, 8):
                result = client.save_search_query("reset", tmp, limit=cap)
                self.assertEqual(result.reason, "item_limit")
                self.assertEqual(result.post_count, cap)
                audit = json.loads((Path(tmp) / "manifest.json").read_text())[
                    "discovery_pagination"
                ]
                self.assertEqual(audit["consecutive_no_progress_pages"], 0)
            self.assertEqual(client._x.search_page.call_count, 7)

    def test_content_resets_a_preceding_empty_streak(self):
        client = Client()
        client._x.search_page.side_effect = [
            cursor_page("a"),
            cursor_page("b"),
            page(tweet("1234567900"), cursor="c"),
            cursor_page("d"),
            cursor_page("e"),
            page(tweet("1234567901"), end=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = client.save_search_query("reset", tmp, max_pages=10)
            self.assertTrue(result.complete)
            self.assertEqual(result.post_count, 2)

    def test_memory_iterator_raises_for_cursor_only_stall(self):
        client = Client()
        client._x.search_page.side_effect = [cursor_page(str(i)) for i in range(3)]
        with self.assertRaisesRegex(APIError, "empty_page_limit"):
            client.get_search_query("reset")
        self.assertEqual(client._x.search_page.call_count, 3)

    def test_replacement_and_module_tweets_are_primary_content(self):
        for kind in ("replacement", "module"):
            with self.subTest(kind=kind):
                post = tweet("1234567900", quote="1111111111")
                content = {"itemContent": {"tweet_results": {"result": post}}}
                payload = cursor_page("next")
                if kind == "replacement":
                    instructions(payload).insert(
                        0,
                        {
                            "type": "TimelineReplaceEntry",
                            "entry": {
                                "entryId": "tweet-1234567900",
                                "content": content,
                            },
                        },
                    )
                else:
                    instructions(payload).insert(
                        0,
                        {
                            "type": "TimelineAddToModule",
                            "moduleItems": [{"item": content}],
                        },
                    )
                self.assertEqual(
                    [p.id for p in timeline_primary_posts(payload)], ["1234567900"]
                )
                client = Client()
                client._x.search_page.return_value = payload
                with tempfile.TemporaryDirectory() as tmp:
                    result = client.save_search_query("reset", tmp, max_pages=1)
                    self.assertEqual(result.reason, "page_limit")
                    self.assertEqual(result.post_count, 1)

    def test_unavailable_or_unknown_items_are_not_cursor_only_empty_pages(self):
        unavailable = tweet("1234567900")
        unavailable["__typename"] = "TweetTombstone"
        unknown = cursor_page("next")
        instructions(unknown).insert(
            0,
            {
                "type": "TimelineAddEntries",
                "entries": [
                    {
                        "entryId": "unknown",
                        "content": {"itemContent": {"itemType": "Unknown"}},
                    }
                ],
            },
        )
        for payload in (page(unavailable, end=True), unknown):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                client = Client()
                client._x.search_page.return_value = payload
                result = client.save_search_query("reset", tmp, max_pages=5)
                self.assertEqual(result.reason, "partial_source_content")
                self.assertFalse(result.complete)
                self.assertIn(
                    "discovery_pagination",
                    json.loads(source_path(tmp, "state.json").read_text()),
                )
                self.assertEqual(len(list(source_path(tmp, "raw").glob("*.json"))), 1)
