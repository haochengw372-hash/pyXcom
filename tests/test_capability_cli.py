"""Offline checks for the research collection CLI's public dispatch contract."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pyxcom.cli import build_parser, main


class CapabilityCLITests(unittest.TestCase):
    def run_command(self, argv, *, error=None):
        client = MagicMock()
        result = MagicMock()
        result.to_dict.return_value = {"complete": False, "reason": "page_limit"}
        for name in (
            "save_search_query",
            "save_post_quotes",
            "save_user_reposts",
            "save_followers",
            "save_following",
            "save_post_reposters",
            "save_post_comments",
        ):
            getattr(client, name).return_value = result
            getattr(client, name).side_effect = error
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("pyxcom.cli.XClient") as constructor,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            constructor.return_value.__enter__.return_value = client
            code = main(argv)
        return code, client, stdout.getvalue(), stderr.getvalue()

    def test_post_discovery_commands_forward_query_and_window(self):
        for command, value, method in (
            ("search-query", '(reset OR "rate limit") OpenAI', "save_search_query"),
            ("post-quotes", "https://x.com/a/status/1234567890", "save_post_quotes"),
            ("user-reposts", "alice", "save_user_reposts"),
        ):
            with self.subTest(command=command):
                code, client, stdout, stderr = self.run_command(
                    [
                        command,
                        value,
                        "--since",
                        "2026-01-01",
                        "--until",
                        "2026-02-01",
                        "--max-pages",
                        "3",
                        "--limit",
                        "25",
                        "--output-dir",
                        "out",
                    ]
                )
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertFalse(json.loads(stdout)["complete"])
                getattr(client, method).assert_called_once_with(
                    value,
                    Path("out"),
                    since="2026-01-01",
                    until="2026-02-01",
                    max_pages=3,
                    limit=25,
                )

    def test_network_commands_forward_snapshot_and_stable_identifier(self):
        for command, value, method in (
            ("user-followers", "100", "save_followers"),
            ("user-following", "100", "save_following"),
            ("post-reposters", "1234567890", "save_post_reposters"),
        ):
            with self.subTest(command=command):
                code, client, _, _ = self.run_command(
                    [
                        command,
                        value,
                        "--snapshot-id",
                        "wave-1",
                        "--max-pages",
                        "2",
                        "--limit",
                        "10",
                        "--output",
                        "out",
                    ]
                )
                self.assertEqual(code, 0)
                getattr(client, method).assert_called_once_with(
                    value,
                    Path("out"),
                    max_pages=2,
                    limit=10,
                    snapshot_id="wave-1",
                )

    def test_reply_seed_comments_forward_dates_and_relative_depth(self):
        code, client, _, _ = self.run_command(
            [
                "post-comments",
                "1234567891",
                "--since",
                "2026-01-01",
                "--until",
                "2026-01-03",
                "--max-depth",
                "3",
                "--output-dir",
                "out",
            ]
        )
        self.assertEqual(code, 0)
        client.save_post_comments.assert_called_once_with(
            "1234567891",
            Path("out"),
            max_depth=3,
            max_comments=100,
            max_pages=20,
            since="2026-01-01",
            until="2026-01-03",
        )

    def test_defaults_do_not_invent_budget_or_snapshot(self):
        code, client, _, _ = self.run_command(
            [
                "user-followers",
                "100",
                "--output-dir",
                "out",
            ]
        )
        self.assertEqual(code, 0)
        client.save_followers.assert_called_once_with(
            "100",
            Path("out"),
            max_pages=None,
            limit=None,
            snapshot_id=None,
        )

    def test_client_validation_reported_as_cli_failure(self):
        code, _, _, stderr = self.run_command(
            ["search-query", "reset", "--limit", "1", "--output-dir", "out"],
            error=ValueError("invalid query scope"),
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid query scope", stderr)

    def test_all_sentinel_forwards_unlimited_comment_budgets(self):
        code, client, _, _ = self.run_command(
            [
                "post-comments",
                "1234567891",
                "--max-comments",
                "all",
                "--max-pages",
                "all",
                "--output-dir",
                "out",
            ]
        )
        self.assertEqual(code, 0)
        client.save_post_comments.assert_called_once_with(
            "1234567891",
            Path("out"),
            max_depth=2,
            max_comments=None,
            max_pages=None,
            since=None,
            until=None,
        )

    def test_all_sentinel_for_new_collection_budgets(self):
        parser = build_parser()
        for command in (
            "search-query",
            "post-quotes",
            "user-reposts",
            "user-followers",
            "user-following",
            "post-reposters",
        ):
            with self.subTest(command=command):
                args = parser.parse_args(
                    [
                        command,
                        "1234567890",
                        "--max-pages",
                        "all",
                        "--limit",
                        "all",
                        "--output-dir",
                        "out",
                    ]
                )
                self.assertIsNone(args.max_pages)
                self.assertIsNone(args.limit)

    def test_invalid_budget_rejected_before_client_creation(self):
        for value in ("0", "-1", "no"):
            with (
                self.subTest(value=value),
                patch("pyxcom.cli.XClient") as client,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main(
                        [
                            "search-query",
                            "reset",
                            "--limit",
                            value,
                            "--output-dir",
                            "out",
                        ]
                    )
                client.assert_not_called()

    def test_network_commands_reject_historical_date_options(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    [
                        "user-following",
                        "100",
                        "--since",
                        "2025-01-01",
                        "--output-dir",
                        "out",
                    ]
                )

    def test_legacy_search_still_uses_literal_keyword_and_handle(self):
        args = build_parser().parse_args(
            [
                "search-posts",
                "reset",
                "--handle",
                "alice",
                "--output-dir",
                "out",
            ]
        )
        self.assertEqual(args.command, "search")
        self.assertEqual((args.keyword, args.user), ("reset", "alice"))


if __name__ == "__main__":
    unittest.main()
