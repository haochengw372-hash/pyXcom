import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pyxcom.client import XClient
from pyxcom.errors import APIError, ParseError, RateLimitError
from pyxcom.models import Post
from pyxcom.networks import (
    NetworkMixin,
    parse_network_page,
    validate_network_collection,
)

POST = "1973931546550894681"


def user(user_id, *, handle=None):
    return {
        "rest_id": str(user_id),
        "legacy": {
            "screen_name": handle or f"user{user_id}",
            "name": "Name",
            "followers_count": 10,
        },
    }


def page(*users, cursor=None):
    entries = [
        {"content": {"itemContent": {"user_results": {"result": item}}}}
        for item in users
    ]
    if cursor is not None:
        entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
    return {
        "data": {
            "user": {"result": {"timeline": {"instructions": [{"entries": entries}]}}}
        }
    }


class FakeClient(NetworkMixin):
    _iter_network = NetworkMixin._iter_network

    def __init__(self, *pages):
        self._x = Mock()
        self.delay = 0
        self._x.relationship_page.side_effect = list(pages)
        self._x.reposters_page.side_effect = list(pages)
        self.get_post = Mock(
            return_value=Post(
                id=POST,
                author_id="999",
                author_handle="author",
                created_at_utc="2025-09-29T00:00:00+00:00",
                text="original",
                url=f"https://x.com/author/status/{POST}",
            )
        )


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class NetworkTests(unittest.TestCase):
    def test_parser_reads_primary_users_and_excludes_embedded_accounts(self):
        primary = user(1)
        primary["recommendation"] = {"user_results": {"result": user(2)}}
        payload = page(primary, cursor="next")
        payload["data"]["extra"] = {"user_results": {"result": user(3)}}
        parsed, cursor = parse_network_page(payload, captured_at_utc="observation")
        self.assertEqual([p.id for p in parsed], ["1"])
        self.assertEqual(parsed[0].captured_at_utc, "observation")
        self.assertEqual(cursor, "next")

    def test_direct_user_list_and_empty_valid_source(self):
        parsed, cursor = parse_network_page(
            {"data": {"users": [user(1)], "next_cursor": "next"}}, captured_at_utc="now"
        )
        self.assertEqual([p.id for p in parsed], ["1"])
        self.assertEqual(cursor, "next")
        self.assertEqual(parse_network_page(page(), captured_at_utc="now"), ([], None))

    def test_malformed_sources_do_not_masquerade_as_empty_network(self):
        for payload in (
            {},
            {"data": {"users": "bad"}},
            {"data": {"timeline": {"instructions": [{}]}}},
            page({"rest_id": "1"}),
            page({"rest_id": "1", "legacy": "malformed"}),
            {
                "data": {
                    "timeline": {
                        "instructions": [
                            {"entries": [{"content": {"tweet_results": {}}}]}
                        ]
                    }
                }
            },
        ):
            with self.subTest(payload=payload), self.assertRaises(ParseError):
                parse_network_page(payload, captured_at_utc="now")

    def test_get_paginates_deduplicates_and_exposes_coverage(self):
        c = FakeClient(page(user(1), cursor="next"), page(user(1), user(2)))
        self.assertEqual([p.id for p in c.get_followers("999")], ["1", "2"])
        c._x.relationship_page.assert_called_with(
            "999", kind="followers", cursor="next"
        )
        self.assertTrue(c.last_network_collection["complete"])
        self.assertEqual(c.last_network_collection["reason"], "visible_source_end")

    def test_get_limit_is_exact_and_partial(self):
        c = FakeClient(page(user(1), user(2), user(3)))
        self.assertEqual([p.id for p in c.get_following("999", limit=1)], ["1"])
        self.assertFalse(c.last_network_collection["complete"])
        self.assertEqual(c.last_network_collection["reason"], "user_limit")

    def test_follow_edge_direction_and_profile_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = FakeClient(page(user(1)), page(user(2)))
            self.assertTrue(
                c.save_followers("999", tmp, snapshot_id="followers-test").complete
            )
            self.assertTrue(
                c.save_following("999", tmp, snapshot_id="following-test").complete
            )
            edges = rows(Path(tmp) / "follow_edges.csv")
            self.assertEqual(
                [(r["source_user_id"], r["target_user_id"]) for r in edges],
                [("1", "999"), ("999", "2")],
            )
            self.assertEqual({r["relationship_type"] for r in edges}, {"follow"})
            self.assertTrue(all(r["observed_at_utc"] for r in edges))
            self.assertTrue(all(r["action_time_utc"] == "" for r in edges))
            self.assertEqual(
                {r["user_id"] for r in rows(Path(tmp) / "users.csv")}, {"1", "2", "999"}
            )
            self.assertEqual(len(rows(Path(tmp) / "user_snapshots.csv")), 4)
            self.assertEqual(
                len(list((Path(tmp) / ".pyxcom").glob("networks/*/*/raw/*.json"))), 2
            )

    def test_reposters_preserve_unknown_action_time(self):
        c = FakeClient(page(user(1)))
        with tempfile.TemporaryDirectory() as tmp:
            result = c.save_post_reposters(
                f"https://x.com/author/status/{POST}", tmp, snapshot_id="test"
            )
            self.assertTrue(result.complete)
            edge = rows(Path(tmp) / "reposters.csv")[0]
            self.assertEqual(
                (edge["source_user_id"], edge["target_user_id"]), ("1", "999")
            )
            self.assertEqual(edge["target_post_id"], POST)
            self.assertEqual(edge["action_time_utc"], "")
            c.get_post.assert_called_once_with(POST)

    def test_limit_resume_preserves_pending_without_requesting_page_again(self):
        c = FakeClient(page(user(1), user(2), user(3)))
        with tempfile.TemporaryDirectory() as tmp:
            first = c.save_followers("999", tmp, limit=1)
            self.assertFalse(first.complete)
            second = c.save_followers("999", tmp, limit=3)
            self.assertTrue(second.complete)
            self.assertEqual(second.post_count, 3)
            self.assertEqual(c._x.relationship_page.call_count, 1)
            self.assertEqual(len(rows(Path(tmp) / "follow_edges.csv")), 3)
            self.assertEqual(len(rows(Path(tmp) / "user_snapshots.csv")), 4)

    def test_page_budget_resumes_cursor_with_fresh_budget(self):
        c = FakeClient(page(user(1), cursor="next"), page(user(2)))
        with tempfile.TemporaryDirectory() as tmp:
            first = c.save_following("999", tmp, max_pages=1)
            self.assertEqual(first.reason, "page_limit")
            second = c.save_following("999", tmp, max_pages=1)
            self.assertTrue(second.complete)
            self.assertEqual(second.pages_fetched, 2)
            c._x.relationship_page.assert_called_with(
                "999", kind="following", cursor="next"
            )

    def test_completed_default_starts_new_snapshot_explicit_complete_skips(self):
        c = FakeClient(page(user(1)), page(user(1)))
        with tempfile.TemporaryDirectory() as tmp:
            c.save_followers("999", tmp, snapshot_id="first")
            before_skip = json.loads(Path(tmp, "network_manifest.json").read_text())[
                "snapshots"
            ]["followers_999_first"]["end_at_utc"]
            c.save_followers("999", tmp, snapshot_id="first")
            after_skip = json.loads(Path(tmp, "network_manifest.json").read_text())[
                "snapshots"
            ]["followers_999_first"]["end_at_utc"]
            self.assertEqual(before_skip, after_skip)
            self.assertEqual(c._x.relationship_page.call_count, 1)
            self.assertEqual(len(rows(Path(tmp) / "follow_edges.csv")), 1)
            c.save_followers("999", tmp)
            self.assertEqual(c._x.relationship_page.call_count, 2)
            self.assertEqual(len(rows(Path(tmp) / "follow_edges.csv")), 2)
            self.assertEqual(len(rows(Path(tmp) / "user_snapshots.csv")), 4)

    def test_rate_limit_keeps_cursor_and_resume_is_possible(self):
        c = FakeClient(
            page(user(1), cursor="next"),
            RateLimitError("limited", 12345),
            page(user(2)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = c.save_followers("999", tmp)
            self.assertEqual(result.reason, "rate_limited")
            state_path = next((Path(tmp) / ".pyxcom").glob("networks/*/*/state.json"))
            state = json.loads(state_path.read_text())
            self.assertEqual(state["cursor"], "next")
            self.assertEqual(state["rate_reset_at"], 12345)
            self.assertTrue(c.save_followers("999", tmp).complete)
            self.assertEqual(len(rows(Path(tmp) / "follow_edges.csv")), 2)

    def test_repeated_cursor_and_duplicate_only_page_are_partial(self):
        for ending in (page(user(2), cursor="same"), page(user(1), cursor="different")):
            c = FakeClient(page(user(1), cursor="same"), ending)
            with self.assertRaises(APIError):
                c.get_followers("999")
            self.assertFalse(c.last_network_collection["complete"])
            self.assertEqual(c.last_network_collection["reason"], "partial_network")
            self.assertEqual(c._x.relationship_page.call_count, 2)

    def test_parse_failure_archives_raw_and_is_retryable(self):
        c = FakeClient({"data": {"unexpected": True}}, page(user(1)))
        with tempfile.TemporaryDirectory() as tmp:
            result = c.save_followers("999", tmp)
            self.assertEqual(result.reason, "parse_error")
            self.assertFalse(result.complete)
            raw = list((Path(tmp) / ".pyxcom").glob("networks/*/*/raw/*.json"))
            self.assertEqual(len(raw), 1)
            self.assertTrue(c.save_followers("999", tmp).complete)
            self.assertEqual(
                len(list((Path(tmp) / ".pyxcom").glob("networks/*/*/raw/*.json"))), 2
            )

    def test_api_error_reports_partial_and_does_not_archive_credentials(self):
        c = FakeClient(APIError("request contains sensitive value"))
        with tempfile.TemporaryDirectory() as tmp:
            result = c.save_followers("999", tmp)
            self.assertEqual(result.reason, "request_error")
            log = (Path(tmp) / ".pyxcom" / "collection_log.jsonl").read_text()
            self.assertNotIn("sensitive value", log)
            self.assertEqual(rows(Path(tmp) / "follow_edges.csv"), [])

    def test_invalid_ids_and_options_fail_before_requests(self):
        for kwargs in (
            {"max_pages": 0},
            {"limit": True},
            {"max_pages": -1},
            {"limit": "1"},
        ):
            c = FakeClient()
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                c.get_followers("999", **kwargs)
            c._x.relationship_page.assert_not_called()
        with self.assertRaises(ValueError):
            FakeClient().get_followers("@handle")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            FakeClient().save_followers("999", tmp, snapshot_id="../escape")

    def test_delays_between_requests(self):
        c = FakeClient(page(user(1), cursor="next"), page(user(2)))
        c.delay = 2
        with patch("pyxcom.networks.time.sleep") as sleep:
            c.get_followers("999")
        sleep.assert_called_once_with(2)

    def test_user_snapshots_retain_handle_change_and_do_not_drop_existing_users(self):
        c = FakeClient(page(user(1, handle="old")), page(user(1, handle="new")))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "users.csv").write_text(
                "user_id,username,display_name,profile_available\n2,existing,Existing,True\n"
            )
            c.save_followers("999", tmp, snapshot_id="one")
            c.save_followers("999", tmp, snapshot_id="two")
            self.assertEqual(
                {r["username"] for r in rows(Path(tmp) / "user_snapshots.csv")},
                {"old", "new", ""},
            )
            self.assertEqual(
                {r["user_id"]: r["username"] for r in rows(Path(tmp) / "users.csv")},
                {"1": "new", "2": "existing", "999": ""},
            )

    def test_focal_stubs_resolve_edges_without_fake_profile_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = FakeClient(page(user(1)))
            result = c.save_post_reposters(POST, tmp, snapshot_id="test")
            self.assertEqual(result.post_count, 1)
            profiles = {row["user_id"]: row for row in rows(Path(tmp) / "users.csv")}
            self.assertEqual(profiles["999"]["profile_available"], "False")
            self.assertEqual(profiles["999"]["followers_count"], "")
            self.assertTrue(validate_network_collection(tmp)["valid"])

    def test_stub_does_not_override_known_existing_focal_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "users.csv").write_text(
                "user_id,username,display_name,profile_available\n999,known,Known,True\n"
            )
            FakeClient(page(user(1))).save_followers("999", tmp)
            profiles = {row["user_id"]: row for row in rows(Path(tmp) / "users.csv")}
            self.assertEqual(profiles["999"]["username"], "known")
            self.assertEqual(profiles["999"]["profile_available"], "True")
            self.assertTrue(validate_network_collection(tmp)["valid"])

    def test_offline_validator_checks_hashes_counts_directions_and_missing_endpoints(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            FakeClient(page(user(1))).save_following("999", tmp, snapshot_id="test")
            self.assertTrue(validate_network_collection(tmp)["valid"])
            raw = next((Path(tmp) / ".pyxcom").glob("networks/*/*/raw/*.json"))
            original = raw.read_bytes()
            raw.write_text("{}")
            self.assertTrue(
                any(
                    "Snapshot hash mismatch" in error
                    for error in validate_network_collection(tmp)["errors"]
                )
            )
            raw.write_bytes(original)
            edges_path = Path(tmp, "follow_edges.csv")
            edges_path.write_text(
                edges_path.read_text(encoding="utf-8-sig").replace("999,1", "1,999")
            )
            result = validate_network_collection(tmp)
            self.assertFalse(result["valid"])
            self.assertTrue(
                any("direction/count" in error for error in result["errors"])
            )
            profiles = Path(tmp, "users.csv")
            profiles.write_text("user_id,username\n1,user1\n")
            self.assertTrue(
                any(
                    "Unresolved edge endpoint" in error
                    for error in validate_network_collection(tmp)["errors"]
                )
            )

    def test_partial_empty_collection_validates_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = FakeClient(RateLimitError("limited")).save_followers("999", tmp)
            self.assertFalse(result.complete)
            self.assertTrue(validate_network_collection(tmp)["valid"])

    def test_post_collection_is_rejected_before_any_writes_or_requests(self):
        markers = (
            (".pyxcom/state.json", "{}"),
            ("manifest.json", '{"source_kind":"saved_post_observations"}'),
            (".pyxcom/manifest.json", '{"post_count":3}'),
            ("manifest.json", '{"counts":{"comments":3}}'),
            ("posts.csv", "post_id,text\n1,text\n"),
        )
        for name, body in markers:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp, name)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(body)
                Path(tmp, "users.csv").write_text("user_id,username\n999,known\n")
                before = {
                    str(path.relative_to(tmp)): path.read_bytes()
                    for path in Path(tmp).rglob("*")
                    if path.is_file()
                }
                c = FakeClient(page(user(1)))
                with self.assertRaisesRegex(ValueError, "separate"):
                    c.save_followers("999", tmp)
                after = {
                    str(path.relative_to(tmp)): path.read_bytes()
                    for path in Path(tmp).rglob("*")
                    if path.is_file()
                }
                self.assertEqual(before, after)
                self.assertFalse(Path(tmp, ".pyxcom/networks").exists())
                c._x.relationship_page.assert_not_called()

    def test_terminal_partial_default_starts_fresh_but_explicit_keeps_old_snapshot(
        self,
    ):
        c = FakeClient(
            page(user(1), cursor="same"), page(user(2), cursor="same"), page(user(3))
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = c.save_followers("999", tmp, snapshot_id="old")
            self.assertFalse(first.complete)
            self.assertEqual(first.reason, "partial_network")
            explicit = c.save_followers("999", tmp, snapshot_id="old")
            self.assertFalse(explicit.complete)
            self.assertEqual(c._x.relationship_page.call_count, 2)
            fresh = c.save_followers("999", tmp)
            self.assertTrue(fresh.complete)
            self.assertEqual(fresh.post_count, 1)
            self.assertEqual(c._x.relationship_page.call_count, 3)
            manifest = json.loads(Path(tmp, "network_manifest.json").read_text())
            self.assertEqual(len(manifest["snapshots"]), 2)
            self.assertEqual(len(rows(Path(tmp, "follow_edges.csv"))), 3)
            self.assertTrue(validate_network_collection(tmp)["valid"])

    def test_manifest_observation_interval_does_not_use_account_creation(self):
        account = user(1)
        account["legacy"]["created_at"] = "Mon Sep 29 07:39:00 +0000 2025"
        with tempfile.TemporaryDirectory() as tmp:
            FakeClient(page(account)).save_followers("999", tmp)
            manifest = json.loads(Path(tmp, "network_manifest.json").read_text())
            snapshot = next(iter(manifest["snapshots"].values()))
            self.assertLessEqual(snapshot["started_at_utc"], snapshot["end_at_utc"])
            observed = rows(Path(tmp, "follow_edges.csv"))[0]["observed_at_utc"]
            self.assertNotEqual(observed, "2025-09-29T07:39:00+00:00")
            self.assertTrue(validate_network_collection(tmp)["valid"])

    def test_validator_rejects_source_path_traversal_and_storage_symlinks(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as other,
        ):
            FakeClient(page(user(1))).save_followers("999", tmp)
            manifest_path = Path(tmp, "network_manifest.json")
            manifest = json.loads(manifest_path.read_text())
            snapshot = next(iter(manifest["snapshots"].values()))
            snapshot["source_sha256"]["../outside.json"] = "bad"
            manifest_path.write_text(json.dumps(manifest))
            self.assertTrue(
                any(
                    "Invalid snapshot source path" in error
                    for error in validate_network_collection(tmp)["errors"]
                )
            )
            snapshot["state_path"] = str(Path(other, "state.json"))
            manifest_path.write_text(json.dumps(manifest))
            self.assertTrue(
                any(
                    "Invalid snapshot state path" in error
                    for error in validate_network_collection(tmp)["errors"]
                )
            )
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as other,
        ):
            Path(tmp, ".pyxcom").symlink_to(other, target_is_directory=True)
            c = FakeClient(page(user(1)))
            with self.assertRaisesRegex(ValueError, "inside output_dir"):
                c.save_followers("999", tmp)
            self.assertEqual(list(Path(other).iterdir()), [])

    def test_getters_raise_on_failure_instead_of_returning_empty_network(self):
        cases = (
            (RateLimitError("limited", 12345), RateLimitError, "rate_limited"),
            (APIError("failed"), APIError, "request_error"),
            ({"data": {"changed": True}}, ParseError, "parse_error"),
        )
        for payload, error_type, reason in cases:
            with self.subTest(reason=reason):
                c = FakeClient(payload)
                with self.assertRaises(error_type):
                    c.get_followers("999")
                self.assertFalse(c.last_network_collection["complete"])
                self.assertEqual(c.last_network_collection["reason"], reason)
                self.assertEqual(c.last_network_collection["records"], {})
        c = FakeClient(RateLimitError("limited", 12345))
        with self.assertRaises(RateLimitError) as raised:
            c.get_post_reposters(POST)
        self.assertEqual(raised.exception.reset_at, 12345)

    def test_iterator_yields_observed_prefix_then_raises_and_retains_status(self):
        c = FakeClient(page(user(1), cursor="next"), RateLimitError("limited"))
        iterator = c.iter_following("999")
        self.assertEqual(next(iterator).id, "1")
        with self.assertRaises(RateLimitError):
            next(iterator)
        self.assertEqual(set(c.last_network_collection["records"]), {"1"})
        self.assertEqual(c.last_network_collection["reason"], "rate_limited")

    def test_cache_alert_metadata_with_user_list_is_supported(self):
        payload = page(user(1), cursor="next")
        instructions = payload["data"]["user"]["result"]["timeline"]["instructions"]
        instructions[:0] = [
            {"type": "TimelineClearCache"},
            {"type": "TimelineShowAlert", "alertType": "NewUsers"},
        ]
        parsed, cursor = parse_network_page(payload, captured_at_utc="now")
        self.assertEqual([profile.id for profile in parsed], ["1"])
        self.assertEqual(cursor, "next")

    def test_metadata_only_or_unknown_metadata_is_not_an_empty_network(self):
        for metadata in (
            [{"type": "TimelineClearCache"}],
            [{"type": "TimelineShowAlert"}],
            [{"type": "UnexpectedMetadata"}],
        ):
            payload = page()
            payload["data"]["user"]["result"]["timeline"]["instructions"] = metadata
            with self.subTest(metadata=metadata), self.assertRaises(ParseError):
                parse_network_page(payload, captured_at_utc="now")
        payload = page(user(1))
        payload["data"]["user"]["result"]["timeline"]["instructions"].insert(
            0, {"type": "UnexpectedMetadata"}
        )
        with self.assertRaises(ParseError):
            parse_network_page(payload, captured_at_utc="now")

    def test_termination_metadata_does_not_override_remaining_bottom_cursor(self):
        first = page(user(1), cursor="next")
        instructions = first["data"]["user"]["result"]["timeline"]["instructions"]
        instructions[:0] = [
            {"type": "TimelineTerminateTimeline", "direction": "Top"},
            {"type": "TimelineTerminateTimeline", "direction": "Bottom"},
        ]
        ending = page()
        ending["data"]["user"]["result"]["timeline"]["instructions"] = [
            {"type": "TimelineTerminateTimeline", "direction": "Bottom"}
        ]
        bounded = FakeClient(first)
        self.assertEqual(
            [profile.id for profile in bounded.get_following("999", max_pages=1)], ["1"]
        )
        self.assertFalse(bounded.last_network_collection["complete"])
        self.assertEqual(bounded.last_network_collection["cursor"], "next")
        full = FakeClient(first, ending)
        self.assertEqual([profile.id for profile in full.get_following("999")], ["1"])
        self.assertEqual(full._x.relationship_page.call_count, 2)
        self.assertTrue(full.last_network_collection["complete"])
        full._x.relationship_page.assert_called_with(
            "999", kind="following", cursor="next"
        )

    def test_no_live_requests_in_tests_and_public_mixin_is_wired(self):
        self.assertTrue(issubclass(XClient, NetworkMixin))
