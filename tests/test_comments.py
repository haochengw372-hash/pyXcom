import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from pyxcom.client import XClient
from pyxcom.comments import CommentTraversal, parse_conversation
from pyxcom.errors import RateLimitError
from pyxcom.parse import parse_post

ROOT = "1973931546550894681"
A = "1973932793278443635"
B = "1973934669768146983"
C = "1974042873461903431"


def tweet(post_id, parent=None, *, root=ROOT, replies=0, handle="other"):
    return {
        "rest_id": post_id,
        "core": {
            "user_results": {
                "result": {
                    "rest_id": "123" if handle == "other" else "456",
                    "core": {
                        "screen_name": handle,
                        "name": handle,
                        "created_at": "Mon Sep 29 07:39:00 +0000 2025",
                    },
                }
            }
        },
        "legacy": {
            "user_id_str": "123" if handle == "other" else "456",
            "created_at": "Mon Sep 29 07:39:00 +0000 2025",
            "full_text": "example",
            "conversation_id_str": root,
            "in_reply_to_status_id_str": parent,
            "reply_count": replies,
        },
    }


def page(*tweets, cursor=None):
    entries = [
        {"content": {"itemContent": {"tweet_results": {"result": t}}}} for t in tweets
    ]
    if cursor:
        entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {
                "instructions": [{"entries": entries}]
            }
        }
    }


def client(*pages):
    c = XClient.__new__(XClient)
    c.delay = 0
    c._x = Mock()
    c._x.conversation_page.side_effect = list(pages)
    c.get_post = Mock(return_value=parse_post(tweet(ROOT, handle="root")))
    return c


def traversal(c, **kwargs):
    return CommentTraversal(
        c,
        ROOT,
        max_depth=kwargs.pop("max_depth", 2),
        max_comments=kwargs.pop("max_comments", None),
        max_pages=kwargs.pop("max_pages", None),
        **kwargs,
    )


class CommentTests(unittest.TestCase):
    def test_parser_ignores_embedded_quotes_and_extracts_profiles(self):
        reply = tweet(A, ROOT)
        reply["quoted_status_result"] = {"result": tweet(B, A)}
        posts, cursors, profiles = parse_conversation(page(reply, cursor="next"))
        self.assertEqual([p.id for p in posts], [A])
        self.assertEqual(cursors, ["next"])
        self.assertIn("other", profiles)

    def test_depth_one_excludes_nested_replies(self):
        c = client(page(tweet(A, ROOT, replies=1), tweet(B, A)))
        t = traversal(c, max_depth=1)
        list(t.pages())
        self.assertEqual(set(t.records), {ROOT, A})
        self.assertTrue(t.complete)
        self.assertEqual(c._x.conversation_page.call_count, 1)

    def test_depth_two_expands_other_authors_and_excludes_other_conversation(self):
        first = tweet(A, ROOT, replies=1)
        first["quoted_status_result"] = {"result": tweet(C, ROOT)}
        c = client(
            page(first, tweet(C, "elsewhere", root="elsewhere")),
            page(tweet(A, ROOT, replies=1), tweet(B, A)),
        )
        t = traversal(c)
        list(t.pages())
        self.assertEqual(set(t.records), {ROOT, A, B})
        self.assertEqual(t.state["depths"][B], 2)
        self.assertTrue(t.complete)
        self.assertEqual(c._x.conversation_page.call_args.args, (A,))

    def test_comment_limit_is_exact_and_pending_survives_resume(self):
        c = client(page(tweet(A, ROOT), tweet(B, ROOT), tweet(C, ROOT)))
        t = traversal(c, max_comments=1)
        list(t.pages())
        self.assertEqual(set(t.records), {ROOT, A})
        self.assertEqual(len(t.state["pending"]), 2)
        self.assertEqual(t.reason, "comment_limit")
        resumed = traversal(c, records=t.records, state=t.state, max_comments=10)
        list(resumed.pages())
        self.assertEqual(set(resumed.records), {ROOT, A, B, C})
        self.assertTrue(resumed.complete)
        self.assertEqual(c._x.conversation_page.call_count, 1)

    def test_page_limit_resumes_cursor_with_fresh_per_run_budget(self):
        c = client(page(tweet(A, ROOT), cursor="next"), page(tweet(B, ROOT)))
        t = traversal(c, max_pages=1)
        list(t.pages())
        self.assertEqual(t.reason, "page_limit")
        self.assertEqual(t.state["queue"], [[ROOT, "next"]])
        resumed = traversal(c, records=t.records, state=t.state, max_pages=1)
        list(resumed.pages())
        self.assertTrue(resumed.complete)
        self.assertEqual(resumed.pages_fetched, 1)
        self.assertEqual(set(resumed.records), {ROOT, A, B})
        c._x.conversation_page.assert_called_with(ROOT, cursor="next")

    def test_unknown_ancestry_is_explicit_partial(self):
        t = traversal(client(page(tweet(B, A))))
        list(t.pages())
        self.assertFalse(t.complete)
        self.assertEqual(t.reason, "partial_conversation")
        self.assertEqual(t.state["unresolved_count"], 1)
        self.assertEqual(set(t.records), {ROOT})

    def test_root_must_be_main_post(self):
        root = parse_post(tweet(ROOT))
        for invalid in (
            replace(root, in_reply_to_id=A),
            replace(root, reposted_post_id=A),
        ):
            c = client()
            c.get_post.return_value = invalid
            with self.assertRaisesRegex(ValueError, "main post"):
                list(traversal(c).pages())
            c._x.conversation_page.assert_not_called()

    def test_save_resume_preserves_pending_and_normalized_tables(self):
        c = client(page(tweet(A, ROOT), tweet(B, A)))
        with tempfile.TemporaryDirectory() as tmp:
            first = c.save_post_comments(ROOT, tmp, max_comments=1)
            self.assertFalse(first.complete)
            second = c.save_post_comments(ROOT, tmp, max_comments=10)
            self.assertTrue(second.complete)
            with (Path(tmp) / "comments.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual({r["comment_id"] for r in rows}, {A, B})
            self.assertEqual({r["depth"] for r in rows}, {"1", "2"})
            self.assertEqual(c._x.conversation_page.call_count, 1)

    def test_rate_limit_saves_queue_for_later_retry(self):
        c = client(RateLimitError("limited", 123456))
        with tempfile.TemporaryDirectory() as tmp:
            result = c.save_post_comments(ROOT, tmp)
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "rate_limited")
            state = json.loads((Path(tmp) / ".pyxcom" / "state.json").read_text())
            self.assertEqual(state["conversation"]["queue"], [[ROOT, None]])
            self.assertEqual(state["conversation"]["rate_reset_at"], 123456)
            c._x.conversation_page.side_effect = [page(tweet(A, ROOT))]
            result = c.save_post_comments(ROOT, tmp)
            self.assertTrue(result.complete)
            self.assertEqual(result.post_count, 2)

    def test_repeated_cursor_is_partial_instead_of_looping(self):
        c = client(
            page(tweet(A, ROOT), cursor="same"), page(tweet(B, ROOT), cursor="same")
        )
        t = traversal(c)
        list(t.pages())
        self.assertFalse(t.complete)
        self.assertEqual(t.reason, "partial_conversation")
        self.assertIn("repeated_cursor", t.state["pagination_warnings"])
        self.assertEqual(c._x.conversation_page.call_count, 2)
