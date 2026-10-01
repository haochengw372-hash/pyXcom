import unittest
from unittest.mock import patch

import httpx

from pyxcom.transport import XTransport


class TransportTests(unittest.TestCase):
    def test_retries_transient_connection_errors(self):
        attempts = 0

        def handle(request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("temporary", request=request)
            return httpx.Response(200, text="ok")

        transport = XTransport({"auth_token": "example", "ct0": "example"})
        transport._x.close()
        transport._x = httpx.Client(transport=httpx.MockTransport(handle))
        with patch("pyxcom.transport.time.sleep"):
            response = transport._get(transport._x, "https://x.com/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 3)
        transport.close()


if __name__ == "__main__":
    unittest.main()


class ReadProtocolTests(unittest.TestCase):
    def transport(self, handler):
        t = XTransport({"auth_token": "dummy", "ct0": "dummy"})
        t._x.close()
        t._x = httpx.Client(transport=httpx.MockTransport(handler))
        t._operations = {
            "SearchTimeline": "search-id",
            "Followers": "followers-id",
            "TweetDetail": "tweet-id",
        }
        t._bearer = "public-test-token"
        self.addCleanup(t.close)
        return t

    def test_current_search_and_followers_are_post_read_queries(self):
        import json

        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"data": {}})

        t = self.transport(handler)
        t.graphql(
            "SearchTimeline", {"rawQuery": "Codex reset"}, features={"flag": True}
        )
        t.graphql("Followers", {"userId": "123"})
        t.graphql("TweetDetail", {"focalTweetId": "1234567890"})
        self.assertEqual([r.method for r in seen], ["POST", "POST", "GET"])
        body = json.loads(seen[0].content)
        self.assertEqual(body["queryId"], "search-id")
        self.assertEqual(body["variables"], {"rawQuery": "Codex reset"})
        self.assertEqual(body["features"], {"flag": True})

    def test_lazy_reposter_route_is_discovered_from_cookie_free_public_asset(self):
        calls = []

        def data(request):
            calls.append(request)
            return httpx.Response(200, json={"data": {}})

        t = self.transport(data)
        t._homepage_html = '<script>h.u=e=>(({1:"shared~bundle.TweetActivity"})[e]||e)+"."+({1:"abcdef12"})[e]+"a.js"</script>'
        t._main_asset_url = (
            "https://abs.twimg.com/responsive-web/client-web/main.abcdef12a.js"
        )
        t._assets.close()
        assets = []

        def asset(request):
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            assets.append(request)
            return httpx.Response(
                200, text='queryId:"dynamic-id",operationName:"Retweeters"'
            )

        t._assets = httpx.Client(transport=httpx.MockTransport(asset))
        t.graphql("Retweeters", {"tweetId": "1234567890"})
        t.graphql("Retweeters", {"tweetId": "1234567890"})
        self.assertEqual(len(assets), 1)
        self.assertEqual(t._operations["Retweeters"], "dynamic-id")
        self.assertEqual([r.method for r in calls], ["POST", "POST"])

    def test_empty_404_with_remaining_quota_is_not_a_rate_limit(self):
        from pyxcom.errors import APIError, RateLimitError

        t = self.transport(
            lambda r: httpx.Response(
                404,
                content=b"",
                headers={"x-rate-limit-remaining": "47", "x-rate-limit-reset": "1000"},
            )
        )
        with self.assertRaises(APIError) as caught:
            t.graphql("SearchTimeline", {"rawQuery": "reset"})
        self.assertNotIsInstance(caught.exception, RateLimitError)

    def test_zero_quota_remains_typed_rate_limit(self):
        from pyxcom.errors import RateLimitError

        t = self.transport(
            lambda r: httpx.Response(
                404,
                content=b"",
                headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": "1000"},
            )
        )
        with self.assertRaises(RateLimitError) as caught:
            t.graphql("Followers", {"userId": "123"})
        self.assertEqual(caught.exception.reset_at, 1000)
