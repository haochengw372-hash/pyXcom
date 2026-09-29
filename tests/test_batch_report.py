import tempfile
import unittest
from pathlib import Path

from pyxcom.batch import _write_batch_report
from pyxcom.models import Post
from pyxcom.storage import PostStore


class BatchReportTests(unittest.TestCase):
    def test_report_records_account_status_and_month(self):
        post = Post(
            id="1234567890",
            author_id="1",
            author_handle="sample",
            created_at_utc="2026-09-29T00:00:00+00:00",
            text="sample",
            url="https://x.com/sample/status/1234567890",
            view_count=100,
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            store = PostStore(output, query={"kind": "batch"})
            store.append_page([post], None)
            result = store.finish(complete=True, reason="all_accounts_complete")
            account = {
                "handle": "sample",
                "user_id": "1",
                "originals": {"count": 1, "reason": "passed_since"},
                "replies": {"count": 0, "reason": "empty_timeline_end"},
            }
            _write_batch_report(output, [account], result)
            report = (output / "report.md").read_text()
            self.assertIn("@sample", report)
            self.assertIn("2026-09", report)
            self.assertIn("100", report)
            self.assertIn("empty_timeline_end", report)


if __name__ == "__main__":
    unittest.main()
