import unittest
from unittest.mock import patch

from pyxcom.client import XClient
from pyxcom.errors import APIError
from pyxcom.models import Post


def post(post_id: str, text: str) -> Post:
    return Post(
        id=post_id,
        author_id="1",
        author_handle="sample",
        created_at_utc="2026-09-01T00:00:00+00:00",
        text=text,
        url=f"https://x.com/sample/status/{post_id}",
    )


class DirectSearchTests(unittest.TestCase):
    def test_default_search_stays_on_x_and_requires_user(self):
        with (
            patch(
                "pyxcom.client.load_x_cookies",
                return_value={"auth_token": "a", "ct0": "b"},
            ),
            patch("pyxcom.client.XTransport"),
        ):
            client = XClient()
            self.assertIsNone(client._mirror)
            with self.assertRaises(APIError):
                list(client.iter_search("Codex"))
            client.close()

    def test_user_keyword_filters_both_x_timelines_without_mirror(self):
        with (
            patch(
                "pyxcom.client.load_x_cookies",
                return_value={"auth_token": "a", "ct0": "b"},
            ),
            patch("pyxcom.client.XTransport"),
        ):
            client = XClient()
            originals = [
                post("1234567890", "Codex update"),
                post("1234567891", "other"),
            ]
            replies = [
                post("1234567890", "Codex update"),
                post("1234567892", "codex reply"),
            ]
            client.iter_user_posts = lambda _user, *, timeline, **_kwargs: iter(
                originals if timeline == "posts" else replies
            )
            result = list(client.iter_search("CoDeX", user="sample"))
            self.assertEqual([item.id for item in result], ["1234567890", "1234567892"])
            client.close()


if __name__ == "__main__":
    unittest.main()
