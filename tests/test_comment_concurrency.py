import csv
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from concurrent.futures import Future

from pyxcom import XClient, assess_recovery, RateLimitError
from pyxcom.parse import parse_post
from test_comments import ROOT, A, B, C, page, tweet


D = "1974042999999999999"
E = "1974043999999999999"


class BranchTransport:
    def __init__(self, responses, delays=None):
        self.responses, self.delays = responses, delays or {}
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = []
        self.focals = set()
        self.same_focal_overlap = False

    def conversation_page(self, focal, cursor=None):
        key = (focal, cursor)
        with self.lock:
            self.same_focal_overlap |= focal in self.focals
            self.focals.add(focal)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(key)
        try:
            time.sleep(self.delays.get(focal, 0.01))
            result = self.responses[key]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            with self.lock:
                self.active -= 1
                self.focals.discard(focal)


def make_client(responses, delays=None):
    c = XClient.__new__(XClient)
    c.delay = 0
    c._x = BranchTransport(responses, delays)
    c.get_post = Mock(return_value=parse_post(tweet(ROOT)))
    return c


def fixtures():
    return {
        (ROOT, None): page(tweet(A, ROOT, replies=1), tweet(B, ROOT, replies=1)),
        (A, None): page(tweet(C, A)),
        (B, None): page(tweet(D, B)),
    }


class CommentConcurrencyTests(unittest.TestCase):
    def rows(self, output):
        with (output / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_parallel_requests_commit_in_original_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            for workers in (1, 2, 4):
                c = make_client(fixtures(), {A: 0.06, B: 0.01})
                target = Path(tmp) / str(workers)
                result = c.save_post_comments(
                    ROOT,
                    target,
                    max_depth=2,
                    max_comments=None,
                    comment_workers=workers,
                )
                self.assertTrue(result.complete)
                state = json.loads((target / ".pyxcom/state.json").read_text())[
                    "conversation"
                ]
                self.assertEqual(state["accepted_ids"], [A, B, C, D])
                self.assertEqual(state["depths"], {ROOT: 0, A: 1, B: 1, C: 2, D: 2})
                self.assertGreaterEqual(c._x.peak, min(workers, 2))
                self.assertTrue(assess_recovery(target)["allowed"])

    def test_cap_preserves_prefetched_page_for_serial_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data"
            c = make_client(fixtures(), {A: 0.03, B: 0.005})
            first = c.save_post_comments(
                ROOT, target, max_depth=2, max_comments=3, comment_workers=2
            )
            self.assertEqual(first.reason, "comment_limit")
            self.assertEqual({r["comment_id"] for r in self.rows(target)}, {A, B, C})
            self.assertEqual(len(c._x.calls), 3)
            state = json.loads((target / ".pyxcom/state.json").read_text())[
                "conversation"
            ]
            self.assertEqual(len(state["prefetched_pages"]), 1)
            self.assertTrue(assess_recovery(target)["allowed"])
            second = c.save_post_comments(
                ROOT, target, max_depth=2, max_comments=10, comment_workers=1
            )
            self.assertTrue(second.complete)
            self.assertEqual(len(c._x.calls), 3)
            self.assertEqual({r["comment_id"] for r in self.rows(target)}, {A, B, C, D})

    def test_same_focal_cursors_stay_sequential(self):
        f = fixtures()
        f[(ROOT, None)] = page(
            tweet(A, ROOT, replies=1), tweet(B, ROOT, replies=1), cursor="root_next"
        )
        f[(ROOT, "root_next")] = page()
        f[(A, None)] = page(tweet(C, A), cursor="branch_next")
        f[(A, "branch_next")] = page(tweet(E, A))
        c = make_client(f)
        c.get_post_comments(ROOT, max_depth=2, max_comments=None, comment_workers=4)
        self.assertFalse(c._x.same_focal_overlap)
        self.assertLess(
            c._x.calls.index((A, None)), c._x.calls.index((A, "branch_next"))
        )

    def test_page_budget_limits_dispatched_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = make_client(fixtures())
            result = c.save_post_comments(
                ROOT, tmp, max_comments=None, max_pages=2, comment_workers=4
            )
            self.assertEqual(result.reason, "page_limit")
            self.assertEqual(len(c._x.calls), 2)
            self.assertEqual(result.pages_fetched, 2)

    def test_rate_limit_preserves_uncommitted_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = fixtures()
            f[(B, None)] = RateLimitError("limited", 9999999999)
            c = make_client(f, {A: 0.03, B: 0.005})
            result = c.save_post_comments(
                ROOT, tmp, max_comments=None, comment_workers=2
            )
            self.assertFalse(result.complete)
            self.assertEqual(result.reason, "rate_limited")
            state = json.loads((Path(tmp) / ".pyxcom/state.json").read_text())[
                "conversation"
            ]
            self.assertIn([B, None], state["queue"])
            self.assertEqual(c._x.active, 0)
            self.assertTrue(assess_recovery(tmp)["allowed"])

    def test_invalid_workers_reject_before_output_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (0, 9, True, 1.5):
                c = make_client(fixtures())
                with self.assertRaises(ValueError):
                    c.save_post_comments(
                        ROOT, Path(tmp) / "invalid", comment_workers=value
                    )
                self.assertEqual(c._x.calls, [])
            self.assertFalse((Path(tmp) / "invalid").exists())

    def test_generic_failure_joins_workers_and_retains_successful_buffer(self):
        from pyxcom.errors import APIError

        with tempfile.TemporaryDirectory() as tmp:
            f = fixtures()
            f[(A, None)] = APIError("temporary failure")
            c = make_client(f, {A: 0.03, B: 0.005})
            with self.assertRaises(APIError):
                c.save_post_comments(ROOT, tmp, max_comments=None, comment_workers=2)
            self.assertEqual(c._x.active, 0)
            state = json.loads((Path(tmp) / ".pyxcom/state.json").read_text())[
                "conversation"
            ]
            self.assertEqual(state["queue"], [[A, None], [B, None]])
            self.assertEqual(len(state["prefetched_pages"]), 1)
            self.assertTrue(assess_recovery(tmp)["allowed"])
            c._x.responses[(A, None)] = page(tweet(C, A))
            calls = len(c._x.calls)
            result = c.save_post_comments(
                ROOT, tmp, max_comments=None, comment_workers=1
            )
            self.assertTrue(result.complete)
            self.assertEqual(len(c._x.calls) - calls, 1)

    def test_prefetched_archive_tampering_blocks_resume(self):
        from pyxcom import IntegrityError

        with tempfile.TemporaryDirectory() as tmp:
            c = make_client(fixtures(), {A: 0.03, B: 0.005})
            c.save_post_comments(ROOT, tmp, max_comments=3, comment_workers=2)
            state = json.loads((Path(tmp) / ".pyxcom/state.json").read_text())[
                "conversation"
            ]
            (Path(tmp) / state["prefetched_pages"][0]["raw_path"]).write_text("{}")
            before = len(c._x.calls)
            with self.assertRaises(IntegrityError):
                c.save_post_comments(ROOT, tmp, max_comments=10, comment_workers=1)
            self.assertEqual(len(c._x.calls), before)

    def test_request_start_spacing_is_shared_by_branches(self):
        c = make_client(fixtures())
        c.delay = 0.03
        starts = []
        original = c._x.conversation_page

        def measured(focal, cursor=None):
            starts.append(time.monotonic())
            return original(focal, cursor)

        c._x.conversation_page = measured
        c.get_post_comments(ROOT, max_comments=None, comment_workers=4)
        self.assertTrue(all(b - a >= 0.025 for a, b in zip(starts, starts[1:])))

    def test_generator_close_joins_started_reads(self):
        c = make_client(fixtures(), {A: 0.01, B: 0.03})
        iterator = c.iter_post_comments(ROOT, max_comments=None, comment_workers=2)
        self.assertEqual(next(iterator).id, A)
        self.assertEqual(next(iterator).id, B)
        self.assertEqual(next(iterator).id, C)
        iterator.close()
        self.assertEqual(c._x.active, 0)

    def test_malformed_head_response_remains_partial_and_resume_refetches(self):
        from pyxcom.errors import ParseError

        with tempfile.TemporaryDirectory() as tmp:
            f = fixtures()
            f[(A, None)] = {"data": {}}
            c = make_client(f, {A: 0.03, B: 0.005})
            with self.assertRaises(ParseError):
                c.save_post_comments(ROOT, tmp, max_comments=None, comment_workers=2)
            self.assertEqual(c._x.active, 0)
            c._x.responses[(A, None)] = page(tweet(C, A))
            result = c.save_post_comments(
                ROOT, tmp, max_comments=None, comment_workers=1
            )
            self.assertTrue(result.complete)

    def test_interrupt_while_waiting_retains_all_started_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = make_client(fixtures(), {A: 0.06, B: 0.06})
            original = Future.result
            interrupted = False

            def interrupt_once(future, *args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    deadline = time.monotonic() + 1
                    while c._x.active < 2 and time.monotonic() < deadline:
                        time.sleep(0.001)
                    raise KeyboardInterrupt()
                return original(future, *args, **kwargs)

            with (
                patch.object(Future, "result", interrupt_once),
                self.assertRaises(KeyboardInterrupt),
            ):
                c.save_post_comments(ROOT, tmp, max_comments=None, comment_workers=2)
            state = json.loads((Path(tmp) / ".pyxcom/state.json").read_text())[
                "conversation"
            ]
            self.assertEqual(len(state["prefetched_pages"]), 2)
            self.assertTrue(assess_recovery(tmp)["allowed"])
            calls = len(c._x.calls)
            result = c.save_post_comments(
                ROOT, tmp, max_comments=None, comment_workers=1
            )
            self.assertTrue(result.complete)
            self.assertEqual(len(c._x.calls), calls)

    def test_real_transport_paces_retry_attempts_and_restores_gate(self):
        import httpx
        from pyxcom.transport import XTransport

        starts = []
        attempts = {}
        lock = threading.Lock()

        def handle(request):
            focal = json.loads(request.url.params["variables"])["focalTweetId"]
            with lock:
                starts.append(time.monotonic())
                attempts[focal] = attempts.get(focal, 0) + 1
                retry = focal == A and attempts[focal] == 1
            threading.Event().wait(0.005)
            return httpx.Response(
                503 if retry else 200, json={} if retry else fixtures()[(focal, None)]
            )

        transport = XTransport({"auth_token": "fixture", "ct0": "fixture"})
        transport._x.close()
        transport._x = httpx.Client(transport=httpx.MockTransport(handle))
        transport._operations = {"TweetDetail": "fixture"}
        old_gate = Mock()
        transport._request_gate = old_gate
        c = make_client(fixtures())
        c._x = transport
        c.delay = 0.025
        try:
            with patch("pyxcom.transport.time.sleep", return_value=None):
                comments = c.get_post_comments(
                    ROOT, max_comments=None, comment_workers=4
                )
            self.assertEqual({p.id for p in comments}, {A, B, C, D})
            self.assertEqual(len(starts), 4)
            self.assertTrue(all(b - a >= 0.02 for a, b in zip(starts, starts[1:])))
            self.assertIs(transport._request_gate, old_gate)
            self.assertIsNone(transport._comment_requests)
            self.assertEqual(old_gate.call_count, 4)
        finally:
            transport.close()

    def test_cli_exposes_comment_workers(self):
        from pyxcom.cli import build_parser

        args = build_parser().parse_args(
            ["post-comments", ROOT, "--output-dir", "data", "--comment-workers", "4"]
        )
        self.assertEqual(args.comment_workers, 4)

    def test_pool_accepts_branch_worker_option(self):
        from test_pool import FakeClient
        from pyxcom import XClientPool, CollectionJob, LoginConfig

        c = FakeClient("100")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("pyxcom.pool.XClient.from_login", return_value=c),
        ):
            with XClientPool(
                [LoginConfig("one", profile="Default")], state_dir=Path(tmp) / "pool"
            ) as pool:
                pool.add_jobs(
                    [
                        CollectionJob(
                            "comments",
                            "post_comments",
                            ROOT,
                            Path(tmp) / "data",
                            options={"comment_workers": 4, "max_depth": 2},
                        )
                    ]
                )
                pool.run(max_steps=1, page_budget=8)
            self.assertEqual(c.calls[0][2]["comment_workers"], 4)

    def test_pool_keeps_rate_limit_seen_by_another_branch_on_parse_error(self):
        from types import SimpleNamespace
        from test_pool import FakeClient
        from pyxcom import XClientPool, CollectionJob, LoginConfig
        from pyxcom.errors import ParseError

        c = FakeClient("100")
        c._x = SimpleNamespace(
            _request_gate=None, _request_observer=None, last_error=None
        )

        def mixed_failure(*args, **kwargs):
            c._x.last_error = {
                "status_code": 200,
                "operation": "TweetDetail",
                "error_codes": [88],
                "reset_at": 9999999999,
            }
            raise ParseError("earlier branch response changed")

        c.save_post_comments = mixed_failure
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("pyxcom.pool.XClient.from_login", return_value=c),
        ):
            with XClientPool(
                [LoginConfig("one", profile="Default")], state_dir=Path(tmp) / "pool"
            ) as pool:
                pool.add_jobs(
                    [
                        CollectionJob(
                            "comments",
                            "post_comments",
                            ROOT,
                            Path(tmp) / "data",
                            options={"comment_workers": 4},
                        )
                    ]
                )
                report = pool.run(max_steps=1, page_budget=8)
            self.assertEqual(report["jobs"]["comments"]["reason"], "rate_limited")
