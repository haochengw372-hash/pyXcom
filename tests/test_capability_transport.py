import unittest
from unittest.mock import patch
from pyxcom.transport import XTransport


class CapabilityTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = XTransport({"auth_token": "dummy", "ct0": "dummy"})
        self.addCleanup(self.transport.close)

    def test_native_search_keeps_operator_query_and_cursor(self):
        query = "(Codex OR ChatGPT) reset -is:retweet since:2026-01-01"
        with patch.object(self.transport, "graphql", return_value={}) as call:
            self.transport.search_page(query, "cursor")
        operation, variables = call.call_args.args
        self.assertEqual(operation, "SearchTimeline")
        self.assertEqual(variables["rawQuery"], query)
        self.assertEqual(variables["product"], "Latest")
        self.assertEqual(variables["cursor"], "cursor")

    def test_directed_lists_and_native_repost_timeline_use_distinct_operations(self):
        with patch.object(self.transport, "graphql", return_value={}) as call:
            self.transport.relationship_page("123", kind="followers", cursor="a")
            self.transport.relationship_page("123", kind="following", cursor="b")
            self.transport.reposters_page("1234567890", "c")
            self.transport.user_timeline_page("123", timeline="reposts", cursor="d")
        self.assertEqual(
            [c.args[0] for c in call.call_args_list],
            ["Followers", "Following", "Retweeters", "UserTweets"],
        )
        self.assertEqual(
            [c.args[1]["cursor"] for c in call.call_args_list], list("abcd")
        )

    def test_invalid_source_options_fail_before_network(self):
        with patch.object(self.transport, "graphql") as call:
            for kwargs in [{"kind": "friends"}, {"kind": "followers"}]:
                with self.assertRaises(ValueError):
                    self.transport.relationship_page("not-an-id", **kwargs)
            with self.assertRaises(ValueError):
                self.transport.search_page("reset", count=True)
            with self.assertRaises(ValueError):
                self.transport.search_page("", product="Latest")
        call.assert_not_called()
