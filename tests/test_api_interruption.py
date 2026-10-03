import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pyxcom import APIError, XClient, assess_recovery, validate_collection
from pyxcom.models import Profile
from test_discovery import page, tweet


class APIInterruptionTests(unittest.TestCase):
    def client(self):
        client = object.__new__(XClient)
        client.get_user = Mock(return_value=Profile("1", "sample", "Sample"))
        client._x = Mock()
        client.delay = 0
        return client

    def test_api_error_preserves_committed_cursor_metrics_and_incomplete_export(self):
        client = self.client()
        first = tweet("1234567900")
        first["legacy"]["favorite_count"] = 1
        second = tweet("1234567900")
        second["legacy"]["favorite_count"] = 5
        client._x.user_timeline_page.side_effect = [page(first, cursor="one")]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            client.save_user_posts("sample", directory, max_pages=1)
            failure = APIError("sensitive diagnostic should not be persisted")
            client._x.user_timeline_page.side_effect = [
                page(second, cursor="two"),
                failure,
            ]
            with self.assertRaises(APIError) as error:
                client.save_user_posts("sample", directory, max_pages=5)
            self.assertIs(error.exception, failure)
            state = json.loads((directory / ".pyxcom/state.json").read_text())
            public = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(state["cursor"], "two")
            self.assertEqual(state["pages_fetched"], 2)
            self.assertEqual(state["reason"], "api_error")
            self.assertFalse(state["complete"])
            self.assertEqual(public["pages_fetched"], 2)
            self.assertEqual(public["reason"], "api_error")
            metrics = [
                json.loads(line)
                for line in (directory / ".pyxcom/metric_snapshots.jsonl")
                .read_text()
                .split("\n")
                if line.strip()
            ]
            self.assertEqual([r["like_count"] for r in metrics], [1, 5])
            self.assertTrue(validate_collection(directory)["valid"])
            self.assertTrue(assess_recovery(directory)["allowed"])
            logs = (directory / ".pyxcom/collection_log.jsonl").read_text()
            self.assertNotIn("sensitive diagnostic", logs)
            self.assertEqual(
                client._x.user_timeline_page.call_args.kwargs["cursor"], "two"
            )
            client._x.user_timeline_page.side_effect = [
                page(tweet("1234567901"), end=True)
            ]
            result = client.save_user_posts("sample", directory, max_pages=1)
            self.assertTrue(result.complete)
            self.assertEqual(
                client._x.user_timeline_page.call_args.kwargs["cursor"], "two"
            )
            self.assertTrue(validate_collection(directory)["valid"])

    def test_error_before_first_page_keeps_original_exception_and_empty_scope(self):
        client = self.client()
        client._x.user_timeline_page.side_effect = APIError("endpoint unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(APIError):
                client.save_user_replies("sample", tmp, max_pages=2)
            state = json.loads((Path(tmp) / ".pyxcom/state.json").read_text())
            self.assertEqual(state["pages_fetched"], 0)
            self.assertIsNone(state["cursor"])
            self.assertEqual(state["reason"], "api_error")
            self.assertTrue(validate_collection(tmp)["valid"])
