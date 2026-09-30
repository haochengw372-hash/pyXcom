import unittest
from unittest.mock import patch

from pyxcom.transport import TWEET_FEATURES, XTransport


class CommentTransportTests(unittest.TestCase):
    def test_conversation_request_uses_dynamic_graphql_route(self):
        transport = XTransport({"auth_token": "example", "ct0": "example"})
        try:
            payload = {"data": {"threaded_conversation_with_injections_v2": {}}}
            with patch.object(transport, "graphql", return_value=payload) as request:
                result = transport.conversation_page("1973931546550894681")
            self.assertIs(result, payload)
            args, kwargs = request.call_args
            self.assertEqual(args[0], "TweetDetail")
            self.assertEqual(args[1]["focalTweetId"], "1973931546550894681")
            self.assertNotIn("cursor", args[1])
            self.assertFalse(args[1]["includePromotedContent"])
            self.assertEqual(kwargs["features"], TWEET_FEATURES)
        finally:
            transport.close()

    def test_cursor_and_reply_focal_id_are_preserved(self):
        transport = XTransport({"auth_token": "example", "ct0": "example"})
        try:
            with patch.object(transport, "graphql", return_value={}) as request:
                transport.conversation_page("1973932793278443635", "opaque-cursor")
            variables = request.call_args.args[1]
            self.assertEqual(variables["focalTweetId"], "1973932793278443635")
            self.assertEqual(variables["cursor"], "opaque-cursor")
        finally:
            transport.close()


if __name__ == "__main__":
    unittest.main()
