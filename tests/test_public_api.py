import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pyxcom import XClient
from pyxcom.cli import build_parser
from pyxcom.models import Post, Profile
from pyxcom.layout import source_path
from pyxcom.validate import validate_collection
from pyxcom.storage import PostStore


class PublicAPITests(unittest.TestCase):
    def client(self):
        with (
            patch(
                "pyxcom.client.load_x_cookies",
                return_value={"auth_token": "a", "ct0": "b"},
            ),
            patch("pyxcom.client.XTransport"),
        ):
            return XClient()

    def test_public_get_signatures_and_compatibility(self):
        client = self.client()
        self.addCleanup(client.close)
        options = dict(since="2026-01-01", until="2026-02-01", max_pages=2, limit=5)
        client.iter_user_posts = Mock(return_value=iter([]))
        self.assertEqual(client.get_user_replies("example", **options), [])
        client.iter_user_posts.assert_called_once_with(
            "example", timeline="replies", **options
        )
        client.iter_search = Mock(side_effect=lambda *a, **k: iter([]))
        self.assertEqual(
            client.get_search_posts("reset", handle="example", **options), []
        )
        self.assertEqual(client.get_search("reset", user="example", **options), [])
        self.assertEqual(
            client.iter_search.call_args_list[0], client.iter_search.call_args_list[1]
        )
        for name in (
            "get_user_posts",
            "get_user_replies",
            "get_search_posts",
            "get_search",
            "save_users_activity",
        ):
            self.assertFalse(
                any(
                    p.kind == p.VAR_KEYWORD
                    for p in inspect.signature(
                        getattr(XClient, name)
                    ).parameters.values()
                )
            )

    def test_save_aliases_forward_options_and_result(self):
        client = self.client()
        self.addCleanup(client.close)
        client.save_search = Mock(return_value="result")
        self.assertEqual(
            client.save_search_posts("reset", "out", handle="alice", max_pages=2),
            "result",
        )
        client.save_search.assert_called_once_with(
            "reset",
            "out",
            user="alice",
            since=None,
            until=None,
            max_pages=2,
            limit=None,
        )
        client.save_accounts = Mock(return_value="batch")
        self.assertEqual(
            client.save_users_activity(
                ["alice"], "out", since="2026-01-01", until="2026-02-01"
            ),
            "batch",
        )
        self.assertEqual(client.save_accounts.call_args.kwargs["rounds"], 1)

    def test_replies_exclude_context_and_save_clean_dataset(self):
        from test_discovery import page, tweet

        client = self.client()
        self.addCleanup(client.close)
        client.get_user = Mock(return_value=Profile("9", "alice", "Alice"))
        root = tweet("1234567890", author="9")
        reply = tweet("1234567891", author="9")
        reply["legacy"].update(
            conversation_id_str="1234567890", in_reply_to_status_id_str="1234567890"
        )
        client._x.user_timeline_page.return_value = page(root, reply, end=True)
        self.assertEqual([p.id for p in client.get_user_replies("alice")], ["1234567891"])
        self.assertEqual([p.id for p in client.get_user_posts("alice")], ["1234567890"])
        with tempfile.TemporaryDirectory() as temp:
            result = client.save_user_replies("alice", temp)
            self.assertEqual(result.post_count, 1)
            self.assertTrue((Path(temp) / "comments.csv").exists())
            self.assertTrue(source_path(temp, "profiles.json").exists())
            self.assertTrue(validate_collection(temp)["valid"])

    def test_completed_legacy_reply_stream_is_normalized_without_fetching(self):
        client = self.client()
        self.addCleanup(client.close)
        client.get_user = Mock(return_value=Profile("9", "alice", "Alice"))
        root = Post(
            "1234567890", "9", "alice", "2026-01-01T00:00:00+00:00", "root", "url"
        )
        reply = Post(
            "1234567891",
            "9",
            "alice",
            "2026-01-01T00:00:00+00:00",
            "reply",
            "url",
            in_reply_to_id=root.id,
            conversation_id=root.id,
        )
        with tempfile.TemporaryDirectory() as folder:
            store = PostStore(
                folder,
                query={
                    "kind": "user_timeline",
                    "handle": "alice",
                    "timeline": "replies",
                    "since": None,
                    "until": None,
                },
            )
            store.append_page([root, reply], None)
            store.finish(complete=True, reason="source_end")
            result = client.save_user_replies("alice", folder)
            self.assertEqual(result.post_count, 1)
            self.assertTrue(
                source_path(folder, "posts.before-timeline-filter.jsonl.gz").exists()
            )
            self.assertTrue(validate_collection(folder)["valid"])
        client._x.user_timeline_page.assert_not_called()

    def test_cli_task_names_and_legacy_aliases(self):
        parser = build_parser()
        old = parser.parse_args(
            ["search", "reset", "--user", "alice", "--output", "out"]
        )
        new = parser.parse_args(
            ["search-posts", "reset", "--handle", "alice", "--output-dir", "out"]
        )
        self.assertEqual(vars(old), vars(new))
        reply = parser.parse_args(["user-replies", "alice", "--output-dir", "out"])
        self.assertEqual((reply.command, reply.timeline), ("posts", "replies"))

    def test_bad_paging_inputs_fail_before_network(self):
        client = self.client()
        self.addCleanup(client.close)
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                client.get_user_replies("alice", limit=value)
            with self.assertRaises(ValueError):
                client.get_search_posts("reset", handle="alice", max_pages=value)
        client._x.user_timeline_page.assert_not_called()
