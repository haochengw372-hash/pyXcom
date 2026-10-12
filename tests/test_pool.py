"""Offline queue, identity, quota and writer isolation regression cases."""

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pyxcom.pool import XClient
import json
from pathlib import Path
from pyxcom.errors import AuthenticationError, ParseError, RateLimitError
from pyxcom.models import CollectionResult
from pyxcom.pool import CollectionJob, LoginConfig, XClientPool


class FakeClient:
    def __init__(self, observer):
        self.observer_id = observer
        self.identity = observer
        self.calls = []
        self.outcomes = []
        self.pages = 0
        self.closed = False

    def check_login_identity(self):
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def close(self):
        self.closed = True

    def save(self, target, output, **options):
        self.calls.append((target, output, options))
        self.pages += options["max_pages"]
        outcome = self.outcomes.pop(0) if self.outcomes else "done"
        if isinstance(outcome, Exception):
            raise outcome
        return CollectionResult(
            Path(output), self.pages * 2, self.pages, outcome == "done", outcome
        )

    save_search_query = save
    save_post_quotes = save
    save_user_posts = save
    save_user_replies = save
    save_user_reposts = save
    save_post_comments = save
    save_followers = save
    save_following = save
    save_post_reposters = save


def logins():
    return [LoginConfig("a", profile="A"), LoginConfig("b", profile="B")]


def job(tmp_path, name="one", login="a", **kwargs):
    return CollectionJob(
        name, "search_query", "topic", tmp_path / name, login_id=login, **kwargs
    )


class PoolTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)
        self.clients = {"A": FakeClient("100"), "B": FakeClient("200")}

        def create(**options):
            client = self.clients[options["profile"]]
            if isinstance(client, Exception):
                raise client
            return client

        patcher = patch.object(XClient, "from_login", side_effect=create, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_distinct_observers_run_simultaneously(self):
        from threading import Barrier

        barrier = Barrier(2)
        for client in self.clients.values():
            original = client.save_search_query

            def save(*args, _original=original, **kwargs):
                barrier.wait(timeout=2)
                return _original(*args, **kwargs)

            client.save_search_query = save
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs([job(self.tmp_path), job(self.tmp_path, "two", "b")])
            report = pool.run(max_workers=2)
        self.assertTrue(all(j["status"] == "complete" for j in report["jobs"].values()))

    def test_nested_outputs_conflict_across_pool_state_directories(self):
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as first:
            first.add_job(job(self.tmp_path))
            with XClientPool(logins(), state_dir=self.tmp_path / "other") as second:
                with self.assertRaisesRegex(RuntimeError, "writer"):
                    second.add_job(
                        CollectionJob(
                            "nested", "search_query", "q", self.tmp_path / "one/nested"
                        )
                    )
        self.assertFalse((self.tmp_path / "one/nested").exists())

    def test_unbound_jobs_fill_free_distinct_observers(self):
        from threading import Barrier

        rendezvous = Barrier(2)
        for client in self.clients.values():
            original = client.save_search_query

            def save(*args, _original=original, **kwargs):
                rendezvous.wait(2)
                return _original(*args, **kwargs)

            client.save_search_query = save
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs(
                [job(self.tmp_path, "one", None), job(self.tmp_path, "two", None)]
            )
            result = pool.run(max_workers=4)
        self.assertEqual(result["peak_workers"], 2)
        self.assertEqual({j["owner"] for j in result["jobs"].values()}, {"a", "b"})
        self.assertTrue(all(j["status"] == "complete" for j in result["jobs"].values()))

    def test_bound_jobs_never_overlap_and_only_coordinator_persists(self):
        from threading import get_ident
        import time

        client = self.clients["A"]
        original = client.save_search_query
        active = []
        worker_threads = []
        coordinator = get_ident()

        def save(*args, **kwargs):
            self.assertFalse(active)
            active.append(True)
            worker_threads.append(get_ident())
            state = json.loads((self.tmp_path / "queue/state.json").read_text())
            self.assertEqual(state["jobs"][Path(args[1]).name]["status"], "running")
            time.sleep(0.005)
            result = original(*args, **kwargs)
            active.pop()
            return result

        client.save_search_query = save
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs([job(self.tmp_path, "one"), job(self.tmp_path, "two")])
            real_persist = pool._persist

            def persist():
                self.assertEqual(get_ident(), coordinator)
                real_persist()

            pool._persist = persist
            result = pool.run(max_workers=4)
        self.assertEqual(result["peak_workers"], 1)
        self.assertTrue(all(worker != coordinator for worker in worker_threads))

    def test_deadline_joins_active_worker_and_stops_dispatch(self):
        import time

        client = self.clients["A"]
        original = client.save_search_query

        def save(*args, **kwargs):
            time.sleep(0.04)
            return original(*args, **kwargs)

        client.save_search_query = save
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs([job(self.tmp_path, "one"), job(self.tmp_path, "two")])
            result = pool.run(max_workers=1, max_seconds=0.02)
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["jobs"]["one"]["status"], "complete")
        self.assertEqual(result["jobs"]["two"]["status"], "pending")

    def test_close_waits_for_workers_before_closing_client_and_scope(self):
        from threading import Event, Thread

        entered, release, closed = Event(), Event(), Event()
        client = self.clients["A"]
        original = client.save_search_query

        def save(*args, **kwargs):
            entered.set()
            release.wait(2)
            self.assertFalse(client.closed)
            return original(*args, **kwargs)

        client.save_search_query = save
        pool = XClientPool(logins(), state_dir=self.tmp_path / "queue")
        self.addCleanup(pool.close)
        pool.add_job(job(self.tmp_path))
        runner = Thread(target=pool.run)
        runner.start()
        self.assertTrue(entered.wait(2))

        def close():
            pool.close()
            closed.set()

        closer = Thread(target=close)
        closer.start()
        self.assertFalse(closed.wait(0.03))
        release.set()
        runner.join(2)
        closer.join(2)
        self.assertTrue(closed.is_set())
        self.assertTrue(client.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            pool.run()

    def test_forbidden_target_does_not_disable_account(self):
        from pyxcom.errors import APIError

        self.clients["A"].outcomes = [APIError("secret", status_code=403), "done"]
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs([job(self.tmp_path, "a"), job(self.tmp_path, "b")])
            result = pool.run()
        self.assertEqual(result["jobs"]["a"]["reason"], "forbidden")
        self.assertEqual(result["jobs"]["b"]["status"], "complete")
        self.assertEqual(result["logins"]["a"]["status"], "active")

    def test_request_pacing_and_metrics_cross_dispatch_boundaries(self):
        from types import SimpleNamespace
        import time

        client = self.clients["A"]
        client._x = SimpleNamespace(
            _request_gate=None, _request_observer=None, last_error={"status_code": 401}
        )
        timestamps = []
        original = client.save_search_query

        def save(*args, **kwargs):
            self.assertIsNone(client._x.last_error)
            client._x._request_gate("SearchTimeline")
            timestamps.append(time.monotonic())
            client._x._request_observer(
                {
                    "operation": "SearchTimeline",
                    "status_code": 200,
                    "attempt": 1,
                    "elapsed_seconds": 0.001,
                    "response_bytes": 5,
                    "cookie": "secret",
                }
            )
            return original(*args, **kwargs)

        client.save_search_query = save
        client.outcomes = ["page_limit", "done"]
        with XClientPool(
            logins(), state_dir=self.tmp_path / "queue", client_options={"delay": 0.03}
        ) as pool:
            pool.add_job(job(self.tmp_path))
            result = pool.run()
        self.assertGreaterEqual(timestamps[1] - timestamps[0], 0.025)
        self.assertEqual(result["jobs"]["one"]["requests"], 2)
        self.assertIsNone(client._x._request_gate)
        self.assertNotIn("secret", (self.tmp_path / "queue/events.jsonl").read_text())

    def test_swallowed_api_error_uses_fresh_transport_metadata(self):
        from types import SimpleNamespace

        client = self.clients["A"]
        client._x = SimpleNamespace(
            _request_gate=None, _request_observer=None, last_error=None
        )

        def save(target, output, **options):
            client._x.last_error = {
                "status_code": 429,
                "operation": "SearchTimeline",
                "reset_at": 9999999999,
            }
            return CollectionResult(Path(output), 0, 0, False, "source_error")

        client.save_search_query = save
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_job(job(self.tmp_path))
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "rate_limited")
        self.assertEqual(result["cooldowns"]["a:SearchTimeline"], 10000000001)

    def test_shared_limit_does_not_redispatch_finished_inflight_slice(self):
        from threading import Barrier, Event
        from pyxcom.errors import RateLimitError

        barrier, accepted = Barrier(2), Event()

        def first(target, output, **options):
            barrier.wait(2)
            raise RateLimitError("limited", 9999999999)

        def second(target, output, **options):
            barrier.wait(2)
            self.assertTrue(accepted.wait(2))
            return CollectionResult(Path(output), 1, 1, False, "page_limit")

        self.clients["A"].save_search_query = first
        self.clients["B"].save_search_query = second
        with XClientPool(
            logins(), state_dir=self.tmp_path / "queue", rate_limit_scope="shared"
        ) as pool:
            original_event = pool._event

            def event(*args, **kwargs):
                original_event(*args, **kwargs)
                if args[1] == "rate_limited":
                    accepted.set()

            pool._event = event
            pool.add_jobs([job(self.tmp_path), job(self.tmp_path, "two", "b")])
            result = pool.run(max_workers=2)
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["jobs"]["two"]["attempts"], 1)
        self.assertEqual(result["jobs"]["two"]["status"], "pending")

    def test_scope_aliases_parent_child_and_siblings(self):
        from pyxcom._pool_locks import ScopeLock

        parent = self.tmp_path / "output"
        parent.mkdir()
        alias = self.tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        with ScopeLock(parent / "first"):
            with ScopeLock(parent / "second"):
                with self.assertRaises(RuntimeError):
                    ScopeLock(parent)
                with self.assertRaises(RuntimeError):
                    ScopeLock(alias / "first")
                with self.assertRaises(RuntimeError):
                    ScopeLock(parent / "first/nested")
        with ScopeLock(parent):
            pass

    def test_reopened_pool_rejects_modified_owner_marker(self):
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_job(job(self.tmp_path))
            pool.run()
        path = self.tmp_path / "one/.pyxcom/pool-owner.json"
        marker_data = json.loads(path.read_text())
        marker_data["observer_id"] = "999"
        path.write_text(json.dumps(marker_data))
        with self.assertRaisesRegex(ValueError, "ownership marker"):
            XClientPool(logins(), state_dir=self.tmp_path / "queue")

    def test_positive_finite_run_limits(self):
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            for options in (
                {"max_workers": 0},
                {"max_workers": True},
                {"max_steps": 0},
                {"page_budget": None},
                {"max_seconds": float("nan")},
                {"max_seconds": float("inf")},
                {"max_seconds": False},
            ):
                with self.subTest(options=options):
                    with self.assertRaises(ValueError):
                        pool.run(**options)

    def test_dispatch_intent_recovers_when_marker_write_was_interrupted(self):
        import pyxcom.pool as module

        pool = XClientPool(logins(), state_dir=self.tmp_path / "queue")
        pool.add_job(job(self.tmp_path))
        original = module._atomic_json

        def fail_marker(path, value):
            if path.name == "pool-owner.json":
                raise OSError("simulated interruption")
            original(path, value)

        with patch.object(module, "_atomic_json", side_effect=fail_marker):
            with self.assertRaises(OSError):
                pool.run()
        pool.close()
        self.assertFalse(self.clients["A"].calls)
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as resumed:
            self.assertEqual(resumed.state["jobs"]["one"]["status"], "pending")
            self.assertNotIn("dispatch_preparing", resumed.state["jobs"]["one"])
            self.assertEqual(resumed.run()["jobs"]["one"]["status"], "complete")

    def test_dispatch_intent_recovers_after_marker_before_final_state(self):
        pool = XClientPool(logins(), state_dir=self.tmp_path / "queue")
        pool.add_job(job(self.tmp_path))
        original = pool._persist
        calls = []

        def fail_final_state():
            calls.append(True)
            if len(calls) == 2:
                raise OSError("simulated interruption")
            original()

        pool._persist = fail_final_state
        with self.assertRaises(OSError):
            pool.run()
        pool.close()
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as resumed:
            self.assertEqual(resumed.run()["jobs"]["one"]["status"], "complete")

    def test_identity_rate_limit_persists_and_blocks_all_identity_dependent_jobs(self):
        from pyxcom.errors import RateLimitError

        client = self.clients["A"]
        calls = []

        def identify():
            calls.append(True)
            raise RateLimitError("limited", 9999999999, operation="Viewer")

        client.check_login_identity = identify
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs([job(self.tmp_path, "one"), job(self.tmp_path, "two")])
            result = pool.run()
        self.assertEqual(result["steps"], 1)
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as resumed:
            self.assertEqual(resumed.run()["steps"], 0)
        self.assertEqual(len(calls), 1)

    def test_network_request_error_disables_only_its_observer(self):
        from types import SimpleNamespace

        client = self.clients["A"]
        client._x = SimpleNamespace(
            _request_gate=None, _request_observer=None, last_error=None
        )

        def followers(target, output, **options):
            client._x.last_error = {
                "status_code": 401,
                "operation": "Followers",
                "error_codes": [32],
            }
            return CollectionResult(Path(output), 0, 0, False, "request_error")

        client.save_followers = followers
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_jobs(
                [
                    CollectionJob(
                        "one", "followers", "42", self.tmp_path / "one", login_id="a"
                    ),
                    job(self.tmp_path, "two", "b"),
                ]
            )
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "authentication_failed")
        self.assertEqual(result["jobs"]["two"]["status"], "complete")

    def test_timeline_retry_stalled_rejected_and_mirror_option_rejected(self):
        with self.assertRaises(ValueError):
            XClientPool(
                logins(),
                state_dir=self.tmp_path / "bad",
                client_options={"mirror_base": "https://example.org"},
            )
        with XClientPool(logins(), state_dir=self.tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", "user_posts", "actor", self.tmp_path / "one")
            )
            with self.assertRaisesRegex(ValueError, "does not support"):
                pool.resume_job("one", retry_stalled=True)

    def test_default_caps_are_unbounded_and_page_dispatch_bounded(self):
        tmp_path = self.tmp_path
        clients = self.clients
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            result = pool.run(page_budget=3)
        self.assertEqual(clients["A"].calls[0][2], {"limit": None, "max_pages": 3})
        self.assertEqual(result["jobs"]["one"]["status"], "complete")

    def test_all_atomic_kinds_search_query(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "search_query"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_post_quotes(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "post_quotes"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_user_posts(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "user_posts"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_user_replies(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "user_replies"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_user_reposts(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "user_reposts"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_post_comments(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "post_comments"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_followers(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "followers"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_following(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "following"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_all_atomic_kinds_post_reposters(self):
        tmp_path = self.tmp_path
        clients = self.clients
        kind = "post_reposters"
        target = "1234567890123456789" if kind.startswith("post_") else "actor"
        if kind in {"followers", "following"}:
            target = "42"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(
                CollectionJob("one", kind, target, tmp_path / "one", login_id="a")
            )
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")
        options = clients["A"].calls[0][2]
        self.assertIs(
            options["max_comments" if kind == "post_comments" else "limit"], None
        )

    def test_round_robin_priority(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["page_limit"] * 8
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_jobs(
                [
                    job(tmp_path, "z", priority=1),
                    job(tmp_path, "b", priority=1),
                    job(tmp_path, "low", priority=0),
                ]
            )
            result = pool.run(max_steps=4)
        self.assertEqual(
            [Path(call[1]).name for call in clients["A"].calls], ["b", "z", "b", "z"]
        )
        self.assertEqual(result["jobs"]["low"]["attempts"], 0)

    def test_sticky_owner_restored_after_adding_login_and_running_recovery(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["page_limit", "done"]
        state_dir = tmp_path / "queue"
        with XClientPool([logins()[0]], state_dir=state_dir) as pool:
            pool.add_job(job(tmp_path, login=None))
            pool.run(max_steps=1)
            pool.state["jobs"]["one"]["status"] = "running"
            pool._persist()
        with XClientPool(logins(), state_dir=state_dir) as pool:
            self.assertEqual(pool.state["jobs"]["one"]["status"], "pending")
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["owner"], "a")
        self.assertEqual(len(clients["B"].calls), 0)

    def test_identity_change_during_dispatch_pauses_only_affected_login(self):
        tmp_path = self.tmp_path
        clients = self.clients
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_jobs([job(tmp_path), job(tmp_path, "two", "b")])
            clients["A"].identity = "999"
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "identity_unavailable")
        self.assertEqual(result["jobs"]["two"]["status"], "complete")
        self.assertFalse(clients["A"].calls)

    def test_identity_change_on_restart_never_reassigns_started_task(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["page_limit"]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            pool.run(max_steps=1)
        clients["A"].observer_id = "999"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "identity_changed")
        self.assertEqual(result["jobs"]["one"]["observer_id"], "100")
        self.assertEqual(result["jobs"]["one"]["owner"], "a")

    def test_duplicate_profiles_do_not_count_as_extra_account(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["B"].observer_id = "100"
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path, login="b"))
            result = pool.run()
        self.assertEqual(result["logins"]["b"]["status"], "duplicate_observer")
        self.assertEqual(result["jobs"]["one"]["status"], "paused")

    def test_unavailable_safari_does_not_block_other_login(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["B"] = AuthenticationError("secret-cookie-value")
        with XClientPool(
            [logins()[0], LoginConfig("b", browser="safari", profile="B")],
            state_dir=tmp_path / "queue",
        ) as pool:
            pool.add_jobs([job(tmp_path), job(tmp_path, "two", "b")])
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["status"], "complete")
        self.assertEqual(result["jobs"]["two"]["status"], "paused")
        self.assertNotIn("secret-cookie-value", json.dumps(result))

    def test_independent_and_shared_endpoint_limits_per_account(self):
        tmp_path = self.tmp_path
        clients = self.clients
        scope = "per_account"
        expected = "complete"
        clients["A"].outcomes = [RateLimitError("secret", 9999999999)]
        with XClientPool(
            logins(), state_dir=tmp_path / "queue", rate_limit_scope=scope
        ) as pool:
            pool.add_jobs([job(tmp_path), job(tmp_path, "two", "b")])
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "rate_limited")
        self.assertEqual(result["jobs"]["two"]["status"], expected)

    def test_independent_and_shared_endpoint_limits_shared(self):
        tmp_path = self.tmp_path
        clients = self.clients
        scope = "shared"
        expected = "pending"
        clients["A"].outcomes = [RateLimitError("secret", 9999999999)]
        with XClientPool(
            logins(), state_dir=tmp_path / "queue", rate_limit_scope=scope
        ) as pool:
            pool.add_jobs([job(tmp_path), job(tmp_path, "two", "b")])
            result = pool.run(max_workers=1)
        self.assertEqual(result["jobs"]["one"]["reason"], "rate_limited")
        self.assertEqual(result["jobs"]["two"]["status"], expected)

    def test_auto_limits_are_conservative_after_two_observers(self):
        tmp_path = self.tmp_path
        clients = self.clients
        for client in clients.values():
            client.outcomes = [RateLimitError("limited", 9999999999)]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_jobs([job(tmp_path), job(tmp_path, "two", "b")])
            result = pool.run()
        self.assertIn("*:SearchTimeline", result["cooldowns"])

    def test_lookup_limit_blocks_other_job_depending_on_same_endpoint(self):
        tmp_path = self.tmp_path
        clients = self.clients
        exc = RateLimitError("limited", 9999999999)
        exc.operation = "UserByScreenName"
        clients["A"].outcomes = [exc]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_jobs(
                [
                    CollectionJob(
                        "a", "user_posts", "actor", tmp_path / "one", login_id="a"
                    ),
                    CollectionJob(
                        "b", "user_replies", "actor", tmp_path / "two", login_id="a"
                    ),
                ]
            )
            result = pool.run()
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["jobs"]["b"]["attempts"], 0)

    def test_saved_rate_limit_uses_persisted_reset(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["rate_limited"]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            (tmp_path / "one/.pyxcom/state.json").write_text(
                json.dumps(
                    {
                        "conversation": {
                            "rate_reset_at": 9999999999,
                            "error_operation": "TweetDetail",
                        }
                    }
                )
            )
            result = pool.run(max_steps=1)
        self.assertEqual(result["cooldowns"]["a:TweetDetail"], 10000000001)

    def test_stall_is_paused_until_explicit_retry(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["empty_page_limit"]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "paused")
            with self.assertRaises(ValueError):
                pool.resume_job("one")
            pool.resume_job("one", retry_stalled=True)
            self.assertEqual(pool.run()["jobs"]["one"]["status"], "complete")

    def test_parse_failure_preserves_error_boundary_without_secret(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = [ParseError("Cookie=secret-password")]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "source_error")
        self.assertNotIn(
            "secret-password", (tmp_path / "queue/events.jsonl").read_text()
        )

    def test_existing_output_requires_matching_explicit_identity_and_preserves_hash(
        self,
    ):
        tmp_path = self.tmp_path
        directory = tmp_path / "one"
        directory.mkdir()
        records = directory / "posts.csv"
        records.write_bytes(b"original-bytes\n")
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            with self.assertRaisesRegex(ValueError, "Adopting"):
                pool.add_job(job(tmp_path))
            with self.assertRaisesRegex(ValueError, "match"):
                pool.add_job(job(tmp_path, existing_observer_id="200"))
            pool.add_job(job(tmp_path, existing_observer_id="100"))
            pool.run()
        self.assertEqual(records.read_bytes(), b"original-bytes\n")

    def test_pool_and_output_os_locks(self):
        tmp_path = self.tmp_path
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            with self.assertRaisesRegex(RuntimeError, "writer"):
                XClientPool(logins(), state_dir=tmp_path / "queue")
            with XClientPool(logins(), state_dir=tmp_path / "other") as other:
                with self.assertRaisesRegex(RuntimeError, "writer"):
                    other.add_job(job(tmp_path))
        with XClientPool(logins(), state_dir=tmp_path / "queue"):
            pass

    def test_output_owner_survives_os_lock_release(self):
        tmp_path = self.tmp_path
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            pool.run()
        with XClientPool(logins(), state_dir=tmp_path / "other") as pool:
            with self.assertRaisesRegex(ValueError, "owned"):
                pool.add_job(job(tmp_path, existing_observer_id="100"))

    def test_job_specs_idempotent_and_outputs_not_reusable(self):
        tmp_path = self.tmp_path
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            pool.add_job(job(tmp_path))
            with self.assertRaisesRegex(ValueError, "specification"):
                pool.add_job(job(tmp_path, options={"limit": 20}))
            with self.assertRaisesRegex(ValueError, "overlap"):
                pool.add_job(
                    CollectionJob("two", "search_query", "q", tmp_path / "one/nested")
                )

    def test_invalid_options_do_not_create_output_1(self):
        tmp_path = self.tmp_path
        options = {"cookies": "secret"}
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            with self.assertRaises(ValueError):
                pool.add_job(job(tmp_path, options=options))
        self.assertFalse((tmp_path / "one").exists())

    def test_invalid_options_do_not_create_output_2(self):
        tmp_path = self.tmp_path
        options = {"max_pages": 50}
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            with self.assertRaises(ValueError):
                pool.add_job(job(tmp_path, options=options))
        self.assertFalse((tmp_path / "one").exists())

    def test_invalid_options_do_not_create_output_3(self):
        tmp_path = self.tmp_path
        options = {"limit": True}
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            with self.assertRaises(ValueError):
                pool.add_job(job(tmp_path, options=options))
        self.assertFalse((tmp_path / "one").exists())

    def test_invalid_options_do_not_create_output_4(self):
        tmp_path = self.tmp_path
        options = {"since": "invalid"}
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            with self.assertRaises(ValueError):
                pool.add_job(job(tmp_path, options=options))
        self.assertFalse((tmp_path / "one").exists())

    def test_session_options_never_written_to_state(self):
        tmp_path = self.tmp_path
        with XClientPool(
            logins(),
            state_dir=tmp_path / "queue",
            client_options={"proxy": "http://user:secret@proxy", "timeout": 3},
        ) as pool:
            pool.add_job(job(tmp_path))
            pool.run()
        text = "".join(
            (path.read_text() for path in (tmp_path / "queue").glob("*.json*"))
        )
        self.assertNotIn("secret", text)
        self.assertNotIn("cookie_db", text)

    def test_date_filter_zero_is_not_a_source_end(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["page_limit", "done"]
        original = clients["A"].save_search_query

        def filtered(*args, **kwargs):
            result = original(*args, **kwargs)
            return CollectionResult(
                result.output_dir,
                0,
                result.pages_fetched,
                result.complete,
                result.reason,
            )

        clients["A"].save_search_query = filtered
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            result = pool.run()
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["jobs"]["one"]["status"], "complete")

    def test_cooldown_survives_process_reconstruction(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = [RateLimitError("limited", 9999999999)]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            pool.run()
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            self.assertEqual(pool.run()["steps"], 0)
        self.assertEqual(len(clients["A"].calls), 1)

    def test_wait_uses_short_chunks_then_resumes(self):
        tmp_path = self.tmp_path
        clients = self.clients
        import pyxcom.pool as module

        clock = [1000.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        self._patch(module.time, "time", lambda: clock[0])
        self._patch(module.time, "sleep", sleep)
        clients["A"].outcomes = [RateLimitError("limited", 1070), "done"]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            result = pool.run(wait_on_rate_limit=True)
        self.assertEqual(result["steps"], 2)
        self.assertEqual(sleeps, [30, 30, 12])

    def test_auth_failure_disables_all_bound_jobs_only(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = [AuthenticationError("secret-cookie")]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_jobs(
                [job(tmp_path, "a"), job(tmp_path, "b"), job(tmp_path, "c", "b")]
            )
            result = pool.run()
        self.assertEqual(result["jobs"]["a"]["reason"], "authentication_failed")
        self.assertEqual(result["jobs"]["b"]["status"], "paused")
        self.assertEqual(result["jobs"]["c"]["status"], "complete")

    def test_saved_auth_error_pauses_without_marking_complete(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = ["source_error"]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            (tmp_path / "one/.pyxcom/state.json").write_text(
                json.dumps({"error_status_code": 401, "error_codes": [32]})
            )
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "authentication_failed")
        self.assertIs(result["jobs"]["one"]["result"]["complete"], False)

    def test_no_dispatch_progress_pauses_a_repeated_checkpoint(self):
        tmp_path = self.tmp_path
        clients = self.clients

        def fixed(target, output, **options):
            return CollectionResult(Path(output), 0, 1, False, "page_limit")

        clients["A"].save_search_query = fixed
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            result = pool.run()
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["jobs"]["one"]["reason"], "no_dispatch_progress")

    def test_expected_identity_is_checked_before_collection(self):
        tmp_path = self.tmp_path
        clients = self.clients
        with XClientPool(
            [LoginConfig("a", profile="A", expected_observer_id="999")],
            state_dir=tmp_path / "queue",
        ) as pool:
            pool.add_job(job(tmp_path))
            result = pool.run()
        self.assertEqual(result["jobs"]["one"]["reason"], "identity_changed")
        self.assertFalse(clients["A"].calls)

    def test_auto_global_uses_longest_known_endpoint_reset(self):
        tmp_path = self.tmp_path
        clients = self.clients
        clients["A"].outcomes = [RateLimitError("limited", 9999999999)]
        clients["B"].outcomes = [RateLimitError("limited", 8888888888)]
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_jobs([job(tmp_path), job(tmp_path, "two", "b")])
            result = pool.run()
        self.assertEqual(result["cooldowns"]["*:SearchTimeline"], 10000000001)

    def test_retry_flag_reaches_discovery_method(self):
        tmp_path = self.tmp_path
        clients = self.clients
        flags = []

        def discovery(
            target, output, *, limit=None, max_pages=None, retry_stalled=False
        ):
            flags.append(retry_stalled)
            return CollectionResult(Path(output), 0, 1, False, "empty_page_limit")

        clients["A"].save_search_query = discovery
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            pool.run()
            pool.resume_job("one", retry_stalled=True)
            pool.run()
        self.assertEqual(flags, [False, True])

    def test_provenance_context_applies_to_each_dispatch(self):
        tmp_path = self.tmp_path
        clients = self.clients
        from pyxcom.provenance import observer_metadata

        observed = []
        original = clients["A"].save_search_query

        def save(*args, **kwargs):
            observed.append(observer_metadata())
            return original(*args, **kwargs)

        clients["A"].save_search_query = save
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            pool.run()
        self.assertEqual(observed, [{"observer_id": "100", "observer_login_id": "a"}])

    def test_one_pool_rejects_concurrent_dispatch(self):
        tmp_path = self.tmp_path
        clients = self.clients
        from threading import Event, Thread

        entered, release = (Event(), Event())
        original = clients["A"].save_search_query

        def save(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            return original(*args, **kwargs)

        clients["A"].save_search_query = save
        with XClientPool(logins(), state_dir=tmp_path / "queue") as pool:
            pool.add_job(job(tmp_path))
            runner = Thread(target=pool.run)
            runner.start()
            self.assertTrue(entered.wait(timeout=3))
            try:
                with self.assertRaisesRegex(RuntimeError, "active run"):
                    pool.run()
                with self.assertRaisesRegex(RuntimeError, "queue"):
                    pool.add_job(job(tmp_path, "two"))
            finally:
                release.set()
                runner.join(timeout=3)
            self.assertFalse(runner.is_alive())
        self.assertEqual(len(clients["A"].calls), 1)


if __name__ == "__main__":
    unittest.main()
