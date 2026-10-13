import json
import tempfile
import unittest
from pathlib import Path

from test_comments import ROOT, A, B, C, client, page, tweet, traversal
from pyxcom.storage import PostStore


class CommentStallTests(unittest.TestCase):
    def stalled(self):
        c = client(
            page(tweet(A, ROOT), cursor="c1"), page(cursor="c2"), page(cursor="c3")
        )
        t = traversal(c, max_depth=3)
        list(t.pages())
        return c, t

    def test_stalled_cursor_is_retained_and_default_resume_does_not_retry(self):
        c, t = self.stalled()
        self.assertEqual(
            t.state["stalled_queue"],
            [{"focal_id": ROOT, "cursor": "c3", "reason": "empty_page_limit"}],
        )
        c._x.conversation_page.reset_mock()
        resumed = traversal(c, records=t.records, state=t.state, max_depth=3)
        list(resumed.pages())
        c._x.conversation_page.assert_not_called()
        self.assertFalse(resumed.complete)

    def test_explicit_retry_resumes_same_cursor_and_resolves_nested_records(self):
        c, t = self.stalled()
        c._x.conversation_page.reset_mock()
        c._x.conversation_page.side_effect = [page(tweet(B, A), tweet(C, B))]
        resumed = traversal(
            c, records=t.records, state=t.state, max_depth=3, retry_stalled=True
        )
        list(resumed.pages())
        c._x.conversation_page.assert_called_once_with(ROOT, cursor="c3")
        self.assertEqual(set(resumed.records), {ROOT, A, B, C})
        self.assertEqual(resumed.state["depths"][C], 3)
        self.assertTrue(resumed.complete)
        self.assertEqual(resumed.state["stalled_queue"], [])
        self.assertIn("empty_page_limit", resumed.state["pagination_warning_history"])

    def test_retry_that_stalls_again_retains_new_cursor_and_does_not_loop(self):
        c, t = self.stalled()
        c._x.conversation_page.reset_mock()
        c._x.conversation_page.side_effect = [page(cursor="c4"), page(cursor="c5")]
        resumed = traversal(
            c, records=t.records, state=t.state, max_depth=3, retry_stalled=True
        )
        list(resumed.pages())
        self.assertEqual(c._x.conversation_page.call_count, 2)
        self.assertFalse(resumed.complete)
        self.assertEqual(resumed.state["stalled_queue"][0]["cursor"], "c5")

    def test_old_saved_empty_page_stop_can_resume_from_archived_exact_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            c = client(
                page(tweet(A, ROOT), cursor="c1"), page(cursor="c2"), page(cursor="c3")
            )
            c.save_post_comments(
                ROOT, folder, max_depth=3, max_comments=100, max_pages=5
            )
            store = PostStore(
                folder,
                query={"kind": "post_comments", "root_post_id": ROOT, "max_depth": 3},
            )
            store.state["conversation"].pop("stalled_queue", None)
            store.finish(complete=False, reason="partial_conversation")
            original_bytes = (Path(folder) / ".pyxcom/posts.jsonl").read_bytes()
            c._x.conversation_page.reset_mock()
            c._x.conversation_page.side_effect = [page(tweet(B, A))]
            result = c.save_post_comments(
                ROOT, folder, max_depth=3, max_pages=1, retry_stalled=True
            )
            c._x.conversation_page.assert_called_once_with(ROOT, cursor="c3")
            self.assertEqual(result.post_count, 3)
            self.assertTrue(
                (Path(folder) / ".pyxcom/posts.jsonl")
                .read_bytes()
                .startswith(original_bytes)
            )
            manifest = json.loads((Path(folder) / "manifest.json").read_text())
            self.assertEqual(manifest["comment_collection"]["stalled_requests"], 0)

    def test_old_default_resume_does_not_prevent_later_explicit_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            c = client(
                page(tweet(A, ROOT), cursor="c1"), page(cursor="c2"), page(cursor="c3")
            )
            c.save_post_comments(ROOT, folder, max_depth=3, max_pages=5)
            store = PostStore(
                folder,
                query={"kind": "post_comments", "root_post_id": ROOT, "max_depth": 3},
            )
            store.state["conversation"].pop("stalled_queue")
            store.finish(complete=False, reason="partial_conversation")
            c._x.conversation_page.reset_mock()
            c.save_post_comments(ROOT, folder, max_depth=3, max_pages=1)
            c._x.conversation_page.assert_not_called()
            c._x.conversation_page.side_effect = [page(tweet(B, A))]
            c.save_post_comments(
                ROOT, folder, max_depth=3, max_pages=1, retry_stalled=True
            )
            c._x.conversation_page.assert_called_once_with(ROOT, cursor="c3")


class CommentStallIntegrationTests(unittest.TestCase):
    def test_legacy_missing_archive_keeps_stop_unresolved(self):
        from types import SimpleNamespace
        from pyxcom.comments import restore_archived_stalls

        with tempfile.TemporaryDirectory() as folder:
            state = {
                "queue": [],
                "visited": [],
                "empty_pages": {ROOT: 2},
                "pagination_warnings": ["empty_page_limit"],
            }
            store = SimpleNamespace(
                output_dir=Path(folder), state={"conversation": state}
            )
            restore_archived_stalls(store)
            self.assertEqual(state["stalled_queue"], [])
            self.assertEqual(state["unrecoverable_stalled_focals"], [ROOT])
            c = client()
            t = traversal(
                c, records={ROOT: c.get_post(ROOT)}, state=state, retry_stalled=True
            )
            list(t.pages())
            self.assertFalse(t.complete)
            c._x.conversation_page.assert_not_called()

    def test_rate_limit_preserves_explicit_retry_task(self):
        from pyxcom import RateLimitError

        c, t = CommentStallTests().stalled()
        c._x.conversation_page.side_effect = [RateLimitError("limited")]
        resumed = traversal(c, records=t.records, state=t.state, retry_stalled=True)
        list(resumed.pages())
        self.assertEqual(resumed.reason, "rate_limited")
        self.assertEqual(resumed.state["queue"], [[ROOT, "c3"]])
        self.assertFalse(resumed.complete)

    def test_cli_forwards_explicit_comment_retry(self):
        import contextlib
        import io
        from unittest.mock import patch, MagicMock
        from pyxcom.cli import main

        with tempfile.TemporaryDirectory() as directory:
            factory = MagicMock()
            saved = factory.return_value.__enter__.return_value.save_post_comments
            saved.return_value.to_dict.return_value = {"complete": False}
            with (
                patch("pyxcom.cli.XClient", factory),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "post-comments",
                        ROOT,
                        "--output-dir",
                        directory,
                        "--retry-stalled",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(saved.call_args.kwargs["retry_stalled"])


class CommentRetryBindingTests(unittest.TestCase):
    def test_wrong_or_older_request_cursor_is_not_recovered(self):
        from types import SimpleNamespace
        import hashlib
        from pyxcom.comments import restore_archived_stalls

        for logged_cursor, focal in [
            ("never-requested", ROOT),
            ("earlier", ROOT),
            ("last", A),
        ]:
            with (
                self.subTest(cursor=logged_cursor, focal=focal),
                tempfile.TemporaryDirectory() as folder,
            ):
                path = Path(folder)
                raw = path / ".pyxcom/raw"
                raw.mkdir(parents=True)
                payload = json.dumps(page(cursor="unbound-next")).encode()
                (raw / "page.json").write_bytes(payload)
                record = {
                    "operation": "TweetDetail",
                    "variables": {"focalTweetId": focal, "cursor": logged_cursor},
                    "raw_path": ".pyxcom/raw/page.json",
                    "raw_sha256": hashlib.sha256(payload).hexdigest(),
                }
                (path / ".pyxcom/collection_log.jsonl").write_text(
                    json.dumps(record) + "\n"
                )
                state = {
                    "pagination_warnings": ["empty_page_limit"],
                    "empty_pages": {ROOT: 2},
                    "visited": [[ROOT, "earlier"], [ROOT, "last"]],
                    "queue": [],
                }
                restore_archived_stalls(
                    SimpleNamespace(output_dir=path, state={"conversation": state})
                )
                self.assertEqual(state["stalled_queue"], [])
                self.assertEqual(state["unrecoverable_stalled_focals"], [ROOT])

    def test_pool_resume_forwards_comment_retry_and_keeps_observer(self):
        from unittest.mock import patch
        from test_pool import FakeClient
        from pyxcom import XClientPool, LoginConfig, CollectionJob
        from pyxcom.models import CollectionResult

        calls = []
        c = FakeClient("100")

        def comments(
            target,
            output,
            *,
            max_pages=None,
            max_depth=2,
            max_comments=100,
            retry_stalled=False,
        ):
            calls.append(retry_stalled)
            return CollectionResult(
                Path(output),
                3 if retry_stalled else 2,
                len(calls),
                retry_stalled,
                "visible_source_end" if retry_stalled else "partial_conversation",
            )

        c.save_post_comments = comments
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("pyxcom.pool.XClient.from_login", return_value=c),
        ):
            with XClientPool(
                [LoginConfig("a", profile="A")], state_dir=Path(tmp) / "queue"
            ) as pool:
                pool.add_job(
                    CollectionJob("root", "post_comments", ROOT, Path(tmp) / "tree")
                )
                pool.run(max_steps=1)
                pool.resume_job("root", retry_stalled=True)
                final = pool.run(max_steps=1)
                self.assertEqual(final["jobs"]["root"]["observer_id"], "100")
                self.assertEqual(final["jobs"]["root"]["status"], "complete")
        self.assertEqual(calls, [False, True])
