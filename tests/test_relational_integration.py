import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyxcom import Post, validate_tables
from pyxcom.cli import main
from pyxcom.storage import PostStore
from pyxcom.validate import validate_collection


class RelationalIntegrationTests(unittest.TestCase):
    def test_finish_exports_and_offline_cli_rebuilds(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            store = PostStore(output, query={})
            root = Post(
                "1",
                "9",
                "sample",
                "2026-01-01T00:00:00+00:00",
                "root",
                "https://x.com/i/status/1",
            )
            reply = Post(
                "2",
                "8",
                "other",
                "2026-01-01T01:00:00+00:00",
                "reply",
                "https://x.com/i/status/2",
                conversation_id="1",
                in_reply_to_id="1",
            )
            store.append_page([root, reply], None)
            store.finish(complete=True, reason="test")
            self.assertTrue(validate_tables(output)["valid"])
            self.assertTrue(validate_collection(output)["valid"])
            original = (output / ".pyxcom" / "posts.jsonl").read_bytes()
            with patch(
                "pyxcom.cli.XClient", side_effect=AssertionError("must stay offline")
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["export", "--output", folder]), 0)
            self.assertEqual(
                original, (output / ".pyxcom" / "posts.jsonl").read_bytes()
            )
            csv_path = output / "comments.csv"
            csv_path.write_bytes(csv_path.read_bytes().replace(b"reply", b"tampered"))
            self.assertFalse(validate_tables(output)["valid"])
            self.assertFalse(validate_collection(output)["valid"])

    def test_missing_source_cli_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["export", "--output", folder]), 2)
