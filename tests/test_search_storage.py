import csv
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from pyxcom.models import Post
from pyxcom.search import MirrorSearch
from pyxcom.storage import PostStore
from pyxcom.validate import finalize_collection, validate_collection


class SearchStorageTests(unittest.TestCase):
    def test_keyword_dates_user_and_mirror_cursor(self):
        html = """<html><title>Search</title><div class="timeline-item" data-username="sample">
        <span class="tweet-date"><a href="/sample/status/1234567890">date</a></span></div>
        <div class="show-more"><a href="?cursor=abc">Load more</a></div></html>"""
        seen = []

        def handle(request):
            seen.append(request)
            return httpx.Response(200, text=html)

        mirror = MirrorSearch(base_url="https://mirror.example")
        mirror._http.close()
        mirror._http = httpx.Client(transport=httpx.MockTransport(handle))
        url = mirror.initial_url(
            "Codex", user="sample", since="2025-09-29", until="2026-09-29"
        )
        page = mirror.page(url)
        self.assertEqual(page.post_ids, ["1234567890"])
        self.assertIn("cursor=abc", page.next_url)
        self.assertIn("from%3Asample", url)
        self.assertIn("since%3A2025-09-29", url)
        self.assertFalse(seen[0].headers.get("cookie"))
        mirror.close()

    def test_resume_deduplicates_and_exports_hashes(self):
        post = Post(
            id="1234567890",
            author_id="1",
            author_handle="sample",
            created_at_utc="2025-09-29T07:39:00+00:00",
            text="hello\u2028world\u2029done",
            url="https://x.com/sample/status/1234567890",
            like_count=7,
        )
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            store = PostStore(folder, query={"kind": "test"})
            self.assertEqual(store.append_page([post], "cursor-2"), 1)
            resumed = PostStore(folder, query={"kind": "test"})
            self.assertEqual(resumed.cursor, "cursor-2")
            self.assertEqual(resumed.append_page([post], None), 0)
            summary = resumed.finish(complete=True, reason="source_end")
            self.assertEqual(summary.post_count, 1)
            self.assertEqual(
                len((folder / "posts.jsonl").read_text().split("\n")) - 1, 1
            )
            with (folder / "posts.csv").open(encoding="utf-8-sig") as file:
                self.assertEqual(len(list(csv.DictReader(file))), 1)
            manifest = json.loads((folder / "manifest.json").read_text())
            self.assertEqual(manifest["post_count"], 1)
            self.assertEqual(len(manifest["sha256"]["posts.csv"]), 64)

    def test_reads_existing_jsonl_with_unicode_line_separator(self):
        post = Post(
            id="1234567890",
            author_id="1",
            author_handle="sample",
            created_at_utc="2025-09-29T07:39:00+00:00",
            text="first\u2028second\u2029third",
            url="https://x.com/sample/status/1234567890",
        )
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "posts.jsonl").write_text(
                json.dumps(post.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
            )
            store = PostStore(folder, query={"kind": "legacy"})
            self.assertEqual(store.count, 1)
            store.finish(complete=True, reason="source_end")

    def test_finalize_recovers_interrupted_export(self):
        post = Post(
            id="1234567890",
            author_id="1",
            author_handle="sample",
            created_at_utc="2025-09-29T07:39:00+00:00",
            text="hello",
            url="https://x.com/sample/status/1234567890",
        )
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            store = PostStore(folder, query={"kind": "test", "since": "2025-09-29"})
            store.append_page([post], "next")
            result = finalize_collection(folder)
            self.assertEqual(result.post_count, 1)
            self.assertTrue(validate_collection(folder)["valid"])


if __name__ == "__main__":
    unittest.main()
