"""Date-scope validation distinguishes conversation context from content."""

import json
import tempfile
import unittest
from pathlib import Path

from pyxcom.models import Post
from pyxcom.storage import PostStore
from pyxcom.validate import validate_collection


ROOT = "1234567890"


def post(identifier, timestamp, *, role="analysis", parent=None, conversation=ROOT):
    return Post(
        id=identifier,
        author_id="22",
        author_handle="sample",
        created_at_utc=timestamp,
        text="sample",
        url="https://x.com/sample/status/" + identifier,
        observation_role=role,
        in_reply_to_id=parent,
        conversation_id=conversation,
    )


class ValidateDateScopeTests(unittest.TestCase):
    def validate_saved(self, records, *, kind="post_comments", legacy_roles=False):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            query = {
                "kind": kind,
                "since": "2026-06-01",
                "until": "2026-06-04",
            }
            if kind == "post_comments":
                query["root_post_id"] = ROOT
                query["max_depth"] = 2
            store = PostStore(output, query=query)
            store.append_page(records, None)
            if legacy_roles:
                path = output / ".pyxcom" / "posts.jsonl"
                rows = [
                    json.loads(line) for line in path.read_text().split("\n") if line
                ]
                for row in rows:
                    row.pop("observation_role", None)
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                store = PostStore(output, query=query)
            store.finish(complete=True, reason="source_end")
            return validate_collection(output)

    def test_window_retains_seed_and_context_on_both_sides(self):
        result = self.validate_saved(
            [
                post(ROOT, "2026-05-31T23:59:59+00:00", role="seed"),
                post(
                    "1234567891",
                    "2026-05-31T23:59:59+00:00",
                    role="context",
                    parent=ROOT,
                ),
                post(
                    "1234567892",
                    "2026-06-04T00:00:00+00:00",
                    role="context",
                    parent=ROOT,
                ),
                post(
                    "1234567893",
                    "2026-06-01T00:00:00+00:00",
                    parent="1234567891",
                ),
                post("1234567894", "2026-06-03T23:59:59+00:00", parent=ROOT),
            ]
        )
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["post_count"], 5)

    def test_mandatory_seed_is_exempt_even_with_default_observation_role(self):
        result = self.validate_saved([post(ROOT, "2026-06-04T00:00:00+00:00")])
        self.assertTrue(result["valid"], result["errors"])

    def test_analysis_descendants_outside_either_boundary_fail(self):
        for timestamp in ("2026-05-31T23:59:59+00:00", "2026-06-04T00:00:00+00:00"):
            with self.subTest(timestamp=timestamp):
                result = self.validate_saved(
                    [
                        post(ROOT, "2026-05-31T00:00:00+00:00", role="seed"),
                        post("1234567891", timestamp, parent=ROOT),
                    ]
                )
                self.assertEqual(result["errors"], ["post_outside_date_range"])

    def test_nonroot_seed_role_does_not_exempt_descendant(self):
        result = self.validate_saved(
            [
                post(
                    "1234567891",
                    "2026-06-04T00:00:00+00:00",
                    role="seed",
                    parent=ROOT,
                )
            ]
        )
        self.assertEqual(result["errors"], ["post_outside_date_range"])

    def test_reply_seed_is_exempt_but_its_analysis_descendants_are_scoped(self):
        actual_root = "1234567800"
        seed = post(
            ROOT,
            "2026-05-31T23:59:59+00:00",
            role="seed",
            parent=actual_root,
            conversation=actual_root,
        )
        eligible = post(
            "1234567891",
            "2026-06-01T00:00:00+00:00",
            parent=ROOT,
            conversation=actual_root,
        )
        valid = self.validate_saved([seed, eligible])
        self.assertTrue(valid["valid"], valid["errors"])
        invalid = self.validate_saved(
            [
                seed,
                post(
                    "1234567891",
                    "2026-06-04T00:00:00+00:00",
                    parent=ROOT,
                    conversation=actual_root,
                ),
            ]
        )
        self.assertIn("post_outside_date_range", invalid["errors"])

    def test_legacy_missing_role_does_not_relax_descendant_scope(self):
        seed = post(ROOT, "2026-05-31T23:59:59+00:00")
        valid = self.validate_saved([seed], legacy_roles=True)
        self.assertTrue(valid["valid"], valid["errors"])
        invalid = self.validate_saved(
            [seed, post("1234567891", "2026-06-04T00:00:00+00:00", parent=ROOT)],
            legacy_roles=True,
        )
        self.assertIn("post_outside_date_range", invalid["errors"])

    def test_search_and_timelines_keep_strict_date_scope_for_all_roles(self):
        for kind in (
            "search",
            "user_posts",
            "search_query",
            "search_direct_user",
            "search_mirror",
            "user_timeline",
            "post_quotes",
            "user_reposts",
        ):
            for role in ("analysis", "context", "seed"):
                with self.subTest(kind=kind, role=role):
                    result = self.validate_saved(
                        [post(ROOT, "2026-06-04T00:00:00+00:00", role=role)],
                        kind=kind,
                    )
                    self.assertEqual(result["errors"], ["post_outside_date_range"])
