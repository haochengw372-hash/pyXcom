import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pyxcom.discovery import DiscoveryMixin
from pyxcom.errors import APIError, ParseError, RateLimitError
from pyxcom.layout import source_path
from pyxcom.models import Profile


TARGET = "1234567890"


def tweet(
    post_id,
    *,
    author="1",
    created="Tue Sep 01 12:00:00 +0000 2026",
    quote=None,
    repost=None,
):
    result = {
        "rest_id": post_id,
        "core": {
            "user_results": {
                "result": {
                    "rest_id": author,
                    "legacy": {"screen_name": "alice"},
                }
            }
        },
        "legacy": {
            "user_id_str": author,
            "created_at": created,
            "full_text": "The returned text does not literally match the OR query",
        },
    }
    if quote:
        result["legacy"]["quoted_status_id_str"] = quote
        result["quoted_status_result"] = {"result": tweet(quote, author="9")}
    if repost:
        result["legacy"]["retweeted_status_result"] = {
            "result": tweet(
                repost, author="9", created="Mon Aug 31 00:00:00 +0000 2026"
            )
        }
    return result


def page(*posts, cursor=None, end=False):
    entries = [
        {
            "entryId": f"tweet-{post['rest_id']}",
            "content": {
                "itemContent": {"tweet_results": {"result": post}},
            },
        }
        for post in posts
    ]
    if cursor:
        entries.append({"entryId": "cursor-bottom-0", "content": {"value": cursor}})
    instructions = [{"type": "TimelineAddEntries", "entries": entries}]
    if end:
        instructions.append(
            {"type": "TimelineTerminateTimeline", "direction": "Bottom"}
        )
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {"instructions": instructions},
                }
            }
        }
    }


class Client(DiscoveryMixin):
    def __init__(self):
        self._x = Mock()
        self.delay = 0
        self.get_user = Mock(return_value=Profile("1", "alice", "Alice"))


class DiscoveryTests(unittest.TestCase):
    def test_date_scope_mismatch_is_recorded_and_never_exported(self):
        c = Client()
        c._x.search_page.return_value = page(tweet("1234567891"), end=True)
        with tempfile.TemporaryDirectory() as tmp:
            r = c.save_search_query(
                "reset", tmp, since="2026-09-02", until="2026-09-03"
            )
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(r.post_count, 0)
            self.assertEqual(manifest["date_scope"]["out_of_window_observations"], 1)
            self.assertTrue(manifest["date_scope"]["local_filter_applied"])

    def test_native_complex_query_preserved_and_dates_filter_locally(self):
        client = Client()
        client._x.search_page.return_value = page(
            tweet("1234567891"),
            tweet("1234567892", created="Wed Sep 02 00:00:00 +0000 2026"),
            end=True,
        )
        result = client.get_search_query(
            '(reset OR "rate limit") (Codex OR OpenAI)',
            since="2026-09-01",
            until="2026-09-02",
        )
        self.assertEqual([post.id for post in result], ["1234567891"])
        client._x.search_page.assert_called_once_with(
            '(reset OR "rate limit") (Codex OR OpenAI) since:2026-09-01 until:2026-09-02',
            cursor=None,
            product="Latest",
            count=20,
        )
        self.assertEqual(result[0].discovery_source, "x_search")

    def test_explicit_dates_reject_conflicting_query_operators_before_request(self):
        client = Client()
        for query, options in (
            ("reset since:2026-08-01", {"since": "2026-09-01"}),
            ("reset (until:2026-10-01)", {"until": "2026-09-02"}),
        ):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                client.get_search_query(query, **options)
        client._x.search_page.assert_not_called()
        client._x.search_page.return_value = page(end=True)
        self.assertEqual(
            client.get_search_query("reset since:2026-09-01", since="2026-09-01"), []
        )
        self.assertEqual(
            client.get_search_query('"since:2026-08-01" reset', since="2026-09-01"), []
        )

    def test_quotes_validate_exact_target_and_ignore_embedded_post(self):
        client = Client()
        client._x.search_page.return_value = page(
            tweet("1234567891", quote=TARGET),
            tweet("1234567892", quote="1234567893"),
            end=True,
        )
        result = client.get_post_quotes(f"https://x.com/example/status/{TARGET}")
        self.assertEqual([p.id for p in result], ["1234567891"])
        self.assertEqual(
            client._x.search_page.call_args.args[0], f"quoted_tweet_id:{TARGET}"
        )

    def test_zero_verified_quotes_are_partial_not_false_empty_success(self):
        client = Client()
        client._x.search_page.return_value = page(tweet("1234567891"), end=True)
        with self.assertRaises(APIError):
            client.get_post_quotes(TARGET)
        with tempfile.TemporaryDirectory() as output:
            result = client.save_post_quotes(TARGET, output)
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "query_unverified")
            self.assertEqual(result.post_count, 0)

    def test_reposts_filter_outer_author_role_and_action_date(self):
        client = Client()
        client._x.user_timeline_page.return_value = page(
            tweet("1234567891", repost=TARGET),
            tweet("1234567892", author="2", repost=TARGET),
            tweet("1234567893"),
            end=True,
        )
        result = client.get_user_reposts(
            "alice", since="2026-09-01", until="2026-09-02"
        )
        self.assertEqual([p.id for p in result], ["1234567891"])
        self.assertEqual(result[0].reposted_post_id, TARGET)
        self.assertTrue(result[0].created_at_utc.startswith("2026-09-01"))
        self.assertEqual(result[0].reposted_author_id, "9")
        self.assertTrue(result[0].reposted_created_at_utc.startswith("2026-08-31"))
        client._x.user_timeline_page.assert_called_once_with(
            "1", timeline="reposts", cursor=None
        )

    def test_flattened_native_repost_is_not_false_empty_success(self):
        client = Client()
        payload = page(tweet(TARGET, author="9"), end=True)
        content = payload["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
            "instructions"
        ][0]["entries"][0]["content"]["itemContent"]
        content["socialContext"] = {"contextType": "Retweet", "text": "Alice reposted"}
        client._x.user_timeline_page.return_value = payload
        with self.assertRaisesRegex(APIError, "Repost activity unavailable"):
            client.get_user_reposts("alice", limit=1)
        with tempfile.TemporaryDirectory() as output:
            result = client.save_user_reposts("alice", output)
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "repost_activity_unavailable")
            self.assertEqual(result.post_count, 0)
            self.assertTrue(list(source_path(output, "raw").glob("*")))
            self.assertIsNone(
                json.loads(source_path(output, "state.json").read_text())["cursor"]
            )

    def test_mixed_flattened_and_valid_reposts_keep_valid_activity_but_partial(self):
        client = Client()
        payload = page(
            tweet(TARGET, author="9"),
            tweet("1234567891", repost=TARGET),
            cursor="next",
            end=True,
        )
        instructions = payload["data"]["search_by_raw_query"]["search_timeline"][
            "timeline"
        ]["instructions"]
        instructions[0]["entries"][0]["content"]["itemContent"]["socialContext"] = {
            "contextType": "Retweet"
        }
        client._x.user_timeline_page.return_value = payload
        with self.assertRaisesRegex(APIError, "Repost activity unavailable"):
            client.get_user_reposts("alice")
        with tempfile.TemporaryDirectory() as output:
            result = client.save_user_reposts("alice", output, limit=1)
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "repost_activity_unavailable")
            self.assertEqual(result.post_count, 1)
            rows = [
                json.loads(row)
                for row in source_path(output, "posts.jsonl").read_text().splitlines()
            ]
            self.assertEqual(rows[0]["id"], "1234567891")
            self.assertTrue(rows[0]["created_at_utc"].startswith("2026-09-01"))
            # Resume retries the unavailable page instead of falsely finishing a later page.
            again = client.save_user_reposts("alice", output)
            self.assertFalse(again.complete)
            self.assertEqual(again.reason, "repost_activity_unavailable")
            self.assertIsNone(client._x.user_timeline_page.call_args.kwargs["cursor"])

    def test_repost_context_with_outer_wrapper_still_supported_in_module(self):
        client = Client()
        payload = page(tweet("1234567891", repost=TARGET), end=True)
        instructions = payload["data"]["search_by_raw_query"]["search_timeline"][
            "timeline"
        ]["instructions"]
        content = instructions[0]["entries"][0]["content"]
        content["itemContent"]["socialContext"] = {"contextType": "Retweet"}
        instructions[0]["entries"][0]["content"] = {"items": [{"item": content}]}
        client._x.user_timeline_page.return_value = payload
        self.assertEqual(
            [p.id for p in client.get_user_reposts("alice")], ["1234567891"]
        )

    def test_missing_and_repeated_cursors_are_partial(self):
        for payloads, reason in (
            ([page(tweet("1234567891"))], "missing_cursor"),
            (
                [page(tweet("1234567891"), cursor="same"), page(cursor="same")],
                "repeated_cursor",
            ),
        ):
            client = Client()
            client._x.search_page.side_effect = payloads
            with tempfile.TemporaryDirectory() as output:
                result = client.save_search_query("reset", output)
                self.assertFalse(result.complete)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.post_count, 1)

    def test_missing_timeline_shape_is_parse_error_and_raw_response_retained(self):
        client = Client()
        client._x.search_page.return_value = {"data": {"unexpected": "schema"}}
        with self.assertRaises(ParseError):
            client.get_search_query("reset")
        with tempfile.TemporaryDirectory() as output:
            result = client.save_search_query("reset", output)
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "parse_error")
            self.assertTrue(list(source_path(output, "raw").glob("*")))

    def test_item_limit_resume_preserves_partly_consumed_page(self):
        client = Client()
        first = page(tweet("1234567891"), tweet("1234567892"), cursor="next")
        client._x.search_page.side_effect = [
            first,
            first,
            page(tweet("1234567893"), end=True),
        ]
        with tempfile.TemporaryDirectory() as output:
            limited = client.save_search_query("reset", output, limit=1)
            self.assertEqual(limited.reason, "item_limit")
            self.assertEqual(limited.post_count, 1)
            state = json.loads(source_path(output, "state.json").read_text())
            self.assertIsNone(state["cursor"])
            resumed = client.save_search_query("reset", output)
            self.assertTrue(resumed.complete)
            self.assertEqual(resumed.post_count, 3)
            calls_before_skip = client._x.search_page.call_count
            again = client.save_search_query("reset", output)
            self.assertTrue(again.complete)
            self.assertEqual(client._x.search_page.call_count, calls_before_skip)

    def test_bottom_termination_with_cursor_still_reads_the_next_page(self):
        first = page(tweet("1234567891"), cursor="next", end=True)
        instructions = first["data"]["search_by_raw_query"]["search_timeline"][
            "timeline"
        ]["instructions"]
        instructions.insert(0, {"type": "TimelineClearCache"})
        second = page(tweet("1234567892"), end=True)
        client = Client()
        client._x.search_page.side_effect = [first, second]
        self.assertEqual(
            [post.id for post in client.get_search_query("reset")],
            ["1234567891", "1234567892"],
        )
        self.assertEqual(client._x.search_page.call_count, 2)
        self.assertEqual(client._x.search_page.call_args.kwargs["cursor"], "next")
        client._x.search_page.reset_mock(side_effect=True)
        client._x.search_page.side_effect = [first, second]
        with tempfile.TemporaryDirectory() as output:
            result = client.save_search_query("reset", output)
            self.assertTrue(result.complete)
            self.assertEqual(result.reason, "source_end")
            self.assertEqual(result.post_count, 2)
            self.assertEqual(result.pages_fetched, 2)
            self.assertEqual(client._x.search_page.call_count, 2)
            self.assertEqual(client._x.search_page.call_args.kwargs["cursor"], "next")

    def test_page_limit_resumes_cursor_and_deduplicates(self):
        client = Client()
        client._x.search_page.side_effect = [
            page(tweet("1234567891"), cursor="next"),
            page(tweet("1234567891"), tweet("1234567892"), end=True),
        ]
        with tempfile.TemporaryDirectory() as output:
            result = client.save_search_query("reset", output, max_pages=1)
            self.assertEqual(result.reason, "page_limit")
            result = client.save_search_query("reset", output)
            self.assertEqual(result.post_count, 2)
            self.assertTrue(result.complete)
            self.assertEqual(client._x.search_page.call_args.kwargs["cursor"], "next")
            with self.assertRaises(ValueError):
                client.save_search_query("another query", output)

    def test_rate_limit_is_resumable_and_bad_inputs_do_not_call_x(self):
        client = Client()
        client._x.search_page.side_effect = RateLimitError("limited", reset_at=123)
        with tempfile.TemporaryDirectory() as output:
            result = client.save_search_query("reset", output)
            self.assertEqual(result.reason, "rate_limited")
            self.assertEqual(
                json.loads(source_path(output, "state.json").read_text())[
                    "rate_reset_at"
                ],
                123,
            )
        client._x.search_page.reset_mock()
        for options in (
            {"limit": 0},
            {"max_pages": True},
            {"since": "2026-09-02", "until": "2026-09-01"},
        ):
            with self.assertRaises(ValueError):
                client.get_search_query("reset", **options)
        client._x.search_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
