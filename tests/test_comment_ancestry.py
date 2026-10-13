import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_comments import ROOT, A, B, C, client, page, tweet
from pyxcom import IntegrityError, RateLimitError
from pyxcom.errors import ParseError


def lookup_payload(post):
    return {"data": {"tweetResult": {"result": post}}}


def owner_marker(observer_id, source):
    return {
        "observer_id": observer_id,
        "login_id": "test_login",
        "job_id": "test_job",
        "pool_state_dir": str(source.parent / "pool"),
    }


class AncestryTests(unittest.TestCase):
    def make_source(self, tmp, posts):
        source = Path(tmp) / "source"
        c = client(page(*posts))
        c.save_post_comments(ROOT, source, max_depth=3, max_pages=2)
        return c, source

    def files(self, source):
        return {
            str(p.relative_to(source)): p.read_bytes()
            for p in source.rglob("*")
            if p.is_file()
        }

    def rows(self, destination):
        with (destination / "resolution.csv").open(
            newline="", encoding="utf-8-sig"
        ) as f:
            return list(csv.DictReader(f))

    def test_exact_chain_resolves_depth_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(C, B)])
            c._observer_id = "100"
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(
                side_effect=[
                    lookup_payload(tweet(B, A)),
                    lookup_payload(tweet(A, ROOT)),
                ]
            )
            before = self.files(source)
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target, lookup_limit=2)
            self.assertEqual(self.files(source), before)
            self.assertEqual([x.args[0] for x in c.get_raw_post.call_args_list], [B, A])
            row = self.rows(target)[0]
            self.assertEqual((row["ancestry_status"], row["depth"]), ("resolved", "3"))
            self.assertEqual(report["resolved_records"], 1)
            contexts = [
                json.loads(line)
                for line in (target / "ancestor_posts.jsonl").read_text().split("\n")
                if line
            ]
            self.assertEqual({p["observation_role"] for p in contexts}, {"context"})
            self.assertFalse(report["collection_complete"])

    def test_zero_budget_does_not_lookup_or_guess_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.get_raw_post = Mock()
            c.check_login_identity = Mock()
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target, lookup_limit=0)
            c.get_raw_post.assert_not_called()
            c.check_login_identity.assert_not_called()
            row = self.rows(target)[0]
            self.assertEqual(row["ancestry_status"], "lookup_budget")
            self.assertEqual(row["depth"], "")
            self.assertEqual(report["lookup_calls"], 0)

    def test_unreturned_parent_stays_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value={"data": {"tweetResult": {}}})
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target, lookup_limit=1)
            row = self.rows(target)[0]
            self.assertEqual(row["ancestry_status"], "parent_not_returned")
            self.assertEqual(row["depth"], "")

    def test_rate_limit_stops_all_further_parent_lookups(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(
                tmp, [tweet(B, A), tweet(C, "1974042999999999999")]
            )
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(side_effect=RateLimitError("limited"))
            report = c.save_comment_ancestry(
                source, Path(tmp) / "resolved", lookup_limit=10
            )
            self.assertEqual(c.get_raw_post.call_count, 1)
            self.assertEqual(report["stop_reason"], "rate_limited")

    def test_wrong_conversation_is_not_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(
                return_value=lookup_payload(tweet(A, ROOT, root="1974042999999999999"))
            )
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target, lookup_limit=1)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "conversation_conflict"
            )
            self.assertEqual((target / "ancestor_posts.jsonl").read_text(), "")

    def test_observer_mismatch_rejects_before_parent_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            (source / ".pyxcom/pool-owner.json").write_text(
                json.dumps(owner_marker("200", source))
            )
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock()
            with self.assertRaises(ValueError):
                c.save_comment_ancestry(source, Path(tmp) / "resolved")
            c.get_raw_post.assert_not_called()
            c.check_login_identity.assert_called_once()

    def test_inherited_observer_mismatch_rejects_before_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(Path(tmp) / "owned", [tweet(B, A)])
            marker = source.parent / ".pyxcom/pool-owner.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps(owner_marker("200", source)))
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock()
            with self.assertRaises(ValueError):
                c.save_comment_ancestry(source, Path(tmp) / "resolved")
            c.get_raw_post.assert_not_called()
            self.assertFalse((Path(tmp) / "resolved").exists())
            c.check_login_identity.assert_called_once()

    def test_other_pool_owned_destination_rejects_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            other = Path(tmp) / "other_pool_output"
            marker = other / ".pyxcom/pool-owner.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps(owner_marker("200", other)))
            before = self.files(other)
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(A, ROOT)))
            with self.assertRaises(IntegrityError):
                c.save_comment_ancestry(source, other / "supplement")
            c.check_login_identity.assert_not_called()
            self.assertEqual(self.files(other), before)

    def test_malformed_owner_marker_rejects_before_identity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            (source / ".pyxcom/pool-owner.json").write_text('{"observer_id": null}')
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value={"data": {"tweetResult": {}}})
            with self.assertRaises(IntegrityError):
                c.save_comment_ancestry(source, Path(tmp) / "resolved")
            c.check_login_identity.assert_not_called()

    def test_shared_missing_parent_is_fetched_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A), tweet(C, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(A, ROOT)))
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target)
            self.assertEqual(report["resolved_records"], 2)
            self.assertEqual(c.get_raw_post.call_count, 1)
            self.assertEqual({r["depth"] for r in self.rows(target)}, {"2"})

    def test_cycle_does_not_produce_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(A, B)))
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            row = self.rows(target)[0]
            self.assertEqual((row["ancestry_status"], row["depth"]), ("cycle", ""))

    def test_chain_budget_retains_context_without_claiming_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(C, B)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(B, A)))
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target, lookup_limit=1)
            self.assertEqual(report["context_records"], 1)
            row = self.rows(target)[0]
            self.assertEqual(
                (row["ancestry_status"], row["depth"]), ("lookup_budget", "")
            )
            self.assertEqual(json.loads(row["chain_ids"]), [C, B])

    def test_wrong_id_is_not_retained_as_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(C, ROOT)))
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "identity_conflict"
            )
            self.assertEqual(report["context_records"], 0)

    def test_protected_parent_content_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            parent = tweet(A, ROOT)
            parent["core"]["user_results"]["result"]["privacy"] = {"protected": True}
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(parent))
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "protected_parent"
            )
            self.assertEqual(report["context_records"], 0)
            self.assertEqual(list((target / "raw").iterdir()), [])

    def test_lookup_parse_error_stops_without_unbound_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(side_effect=ParseError("changed interface"))
            report = c.save_comment_ancestry(source, Path(tmp) / "resolved")
            self.assertEqual(report["stop_reason"], "request_error")

    def test_protected_unparseable_parent_is_not_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            parent = tweet(A, ROOT)
            parent["core"]["user_results"]["result"]["legacy"] = {"protected": True}
            del parent["legacy"]["created_at"]
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(parent))
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "protected_parent"
            )
            self.assertEqual(list((target / "raw").iterdir()), [])

    def test_malformed_second_response_preserves_first_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(
                tmp, [tweet(B, A), tweet(C, "1974042999999999999")]
            )
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(
                side_effect=[lookup_payload(tweet(A, ROOT)), {"data": None}]
            )
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target)
            self.assertEqual(report["resolved_records"], 1)
            self.assertEqual(report["status_counts"]["response_unparsed"], 1)
            self.assertEqual(report["context_records"], 1)

    def test_invalid_returned_parent_is_parse_failure_not_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            parent = tweet(A, ROOT)
            del parent["legacy"]["created_at"]
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(parent))
            target = Path(tmp) / "resolved"
            report = c.save_comment_ancestry(source, target)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "response_unparsed"
            )
            self.assertEqual(report["context_records"], 0)
            self.assertTrue((target / "raw" / f"{A}.json").exists())

    def test_tombstone_is_unreturned_not_unparsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(
                return_value=lookup_payload({"__typename": "TweetTombstone"})
            )
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "parent_not_returned"
            )

    def test_deeper_blocked_id_is_distinct_from_original_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(C, B)])
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(
                side_effect=[lookup_payload(tweet(B, A)), {"data": {"tweetResult": {}}}]
            )
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            row = self.rows(target)[0]
            self.assertEqual(row["parent_post_id"], B)
            self.assertEqual(row["blocked_post_id"], A)
            self.assertEqual(row["ancestry_status"], "parent_not_returned")
            self.assertEqual(json.loads(row["chain_ids"]), [C, B])

    def test_publication_failure_leaves_no_partial_target_and_can_retry(self):
        from pyxcom._persistence import atomic_json as write_json

        for phase in ("jsonl", "raw", "manifest"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                c, source = self.make_source(tmp, [tweet(B, A)])
                c.check_login_identity = Mock(return_value="100")
                c.get_raw_post = Mock(return_value=lookup_payload(tweet(A, ROOT)))
                target = Path(tmp) / "resolved"
                before = self.files(source)

                def fail_write(path, value):
                    if (phase == "raw" and path.parent.name == "raw") or (
                        phase == "manifest" and path.name == "manifest.json"
                    ):
                        raise OSError("injected write failure")
                    write_json(path, value)

                failure = (
                    patch(
                        "pyxcom.ancestry.append_jsonl",
                        side_effect=OSError("injected write failure"),
                    )
                    if phase == "jsonl"
                    else patch("pyxcom.ancestry.atomic_json", side_effect=fail_write)
                )
                with failure, self.assertRaises(OSError):
                    c.save_comment_ancestry(source, target)
                self.assertFalse(target.exists())
                self.assertEqual(list(target.parent.glob(".pyxcom-ancestry-*")), [])
                self.assertEqual(self.files(source), before)
                report = c.save_comment_ancestry(source, target)
                self.assertEqual(report["resolved_records"], 1)

    def test_paths_and_corruption_reject_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, source = self.make_source(tmp, [tweet(B, A)])
            c.check_login_identity = Mock()
            for target in (source, source / "child", source.parent):
                with self.assertRaises(ValueError):
                    c.save_comment_ancestry(source, target)
            target = Path(tmp) / "existing"
            target.mkdir()
            with self.assertRaises(FileExistsError):
                c.save_comment_ancestry(source, target)
            (source / ".pyxcom/posts.jsonl").write_text("{}\n")
            with self.assertRaises(IntegrityError):
                c.save_comment_ancestry(source, Path(tmp) / "resolved")
            c.check_login_identity.assert_not_called()

    def test_depth_bounds_are_reported_without_sample_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            c = client(page(tweet(C, B)))
            c.save_post_comments(ROOT, source, max_depth=1)
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(
                side_effect=[
                    lookup_payload(tweet(B, A)),
                    lookup_payload(tweet(A, ROOT)),
                ]
            )
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            row = self.rows(target)[0]
            self.assertEqual(row["depth"], "3")
            self.assertEqual(row["within_requested_bounds"], "False")

    def test_reply_seed_does_not_admit_other_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            c = client(page(tweet(C, B)))
            from pyxcom.parse import parse_post

            c.get_post = Mock(return_value=parse_post(tweet(A, ROOT)))
            c.save_post_comments(A, source, max_depth=3)
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(B, ROOT)))
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            self.assertEqual(
                self.rows(target)[0]["ancestry_status"], "outside_seed_branch"
            )

    def test_reply_seed_depth_is_relative_to_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            c = client(page(tweet(C, B)))
            from pyxcom.parse import parse_post

            c.get_post = Mock(return_value=parse_post(tweet(A, ROOT)))
            c.save_post_comments(A, source, max_depth=3)
            c.check_login_identity = Mock(return_value="100")
            c.get_raw_post = Mock(return_value=lookup_payload(tweet(B, A)))
            target = Path(tmp) / "resolved"
            c.save_comment_ancestry(source, target)
            row = self.rows(target)[0]
            self.assertEqual(row["seed_relative_depth"], "2")
            self.assertEqual(row["root_post_id"], ROOT)
            self.assertEqual(row["seed_post_id"], A)
            self.assertEqual(row["depth"], "")
