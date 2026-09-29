import json
import tempfile
import unittest
from pathlib import Path

from pyxcom.models import Post, classify_post
from pyxcom.schema import apply_role_schema
from pyxcom.storage import PostStore
from pyxcom.validate import validate_collection


def sample(post_id: str, **relations) -> Post:
    return Post(
        id=post_id,
        author_id="1",
        author_handle="sample",
        created_at_utc="2026-09-29T00:00:00+00:00",
        text="sample",
        url=f"https://x.com/sample/status/{post_id}",
        **relations,
    )


class SchemaTests(unittest.TestCase):
    def test_role_and_type_follow_relation_ids(self):
        self.assertEqual(classify_post(None, None, None), ("main", "original"))
        self.assertEqual(classify_post(None, "quoted", None), ("main", "quote"))
        self.assertEqual(classify_post("parent", None, None), ("comment", "reply"))
        self.assertEqual(classify_post(None, None, "other"), ("repost", "repost"))
        self.assertEqual(classify_post("parent", "quoted", None), ("comment", "reply"))

    def test_backfills_old_jsonl_and_preserves_backup(self):
        posts = [
            sample("1234567890"),
            sample("1234567891", in_reply_to_id="1234567890"),
            sample("1234567892", quoted_post_id="1234567890"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            store = PostStore(output, query={"kind": "test"})
            store.append_page(posts, None)
            store.finish(complete=True, reason="source_end")
            old_records = []
            for post in posts:
                record = post.to_dict()
                record.pop("post_role")
                record.pop("post_type")
                old_records.append(record)
            (output / "posts.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in old_records),
                encoding="utf-8",
            )
            result = apply_role_schema(output)
            self.assertEqual(result["role_counts"], {"main": 2, "comment": 1})
            self.assertEqual(
                result["type_counts"], {"original": 1, "reply": 1, "quote": 1}
            )
            self.assertTrue((output / "posts.jsonl.before-role-schema.gz").exists())
            self.assertTrue(validate_collection(output)["valid"])
            self.assertEqual(apply_role_schema(output)["changed_directories"], [])


if __name__ == "__main__":
    unittest.main()
