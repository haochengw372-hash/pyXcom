"""Offline recovery command boundaries and explicit mutation dispatch."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyxcom.cli import build_parser, main


class RecoveryCLITests(unittest.TestCase):
    def run_command(self, argv, name, report):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch(f"pyxcom.cli.{name}", return_value=report) as operation,
            patch("pyxcom.cli.XClient") as client,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(argv)
        client.assert_not_called()
        return code, operation, stdout.getvalue(), stderr.getvalue()

    def test_assessment_is_offline_and_forwards_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            query = Path(directory) / "query.json"
            binding = Path(directory) / "binding.json"
            receipt = Path(directory) / "receipt.json"
            query.write_text(json.dumps({"source": "post_comments", "max_depth": 2}))
            binding.write_text(json.dumps({"observer_id": "123"}))
            receipt.write_text(json.dumps({"generation_id": "earlier"}))
            code, operation, stdout, stderr = self.run_command(
                [
                    "assess",
                    "--output-dir",
                    "out",
                    "--expected-query",
                    str(query),
                    "--binding",
                    str(binding),
                    "--previous-receipt",
                    str(receipt),
                ],
                "assess_recovery",
                {"allowed": True, "errors": []},
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(json.loads(stdout)["allowed"])
        operation.assert_called_once_with(
            Path("out"),
            expected_query={"source": "post_comments", "max_depth": 2},
            binding={"observer_id": "123"},
            previous_receipt={"generation_id": "earlier"},
        )

    def test_prepare_forwards_plan_and_never_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.json"
            plan.write_text(json.dumps({"allowed": True, "source_sha256": {"a": "b"}}))
            with patch("pyxcom.cli.apply_recovery") as apply:
                code, operation, _, _ = self.run_command(
                    [
                        "prepare-recovery",
                        "--output-dir",
                        "out",
                        "--generation-dir",
                        "generations/g1",
                        "--plan",
                        str(plan),
                    ],
                    "prepare_recovery",
                    {"ready": True},
                )
                apply.assert_not_called()
        self.assertEqual(code, 0)
        operation.assert_called_once_with(
            Path("out"),
            plan={"allowed": True, "source_sha256": {"a": "b"}},
            generation_dir=Path("generations/g1"),
            expected_query=None,
            binding=None,
            previous_receipt=None,
        )

    def test_verification_is_read_only(self):
        code, operation, _, _ = self.run_command(
            ["verify-generation", "--generation-dir", "generations/g1"],
            "verify_generation",
            {"valid": True},
        )
        self.assertEqual(code, 0)
        operation.assert_called_once_with(Path("generations/g1"))

    def test_application_requires_explicit_command_and_receipt_directory(self):
        code, operation, _, _ = self.run_command(
            [
                "apply-recovery",
                "--output-dir",
                "out",
                "--generation-dir",
                "generations/g1",
                "--receipt-dir",
                "receipts",
            ],
            "apply_recovery",
            {"applied": True},
        )
        self.assertEqual(code, 0)
        operation.assert_called_once_with(
            Path("out"),
            generation_dir=Path("generations/g1"),
            receipt_dir=Path("receipts"),
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    [
                        "apply-recovery",
                        "--output-dir",
                        "out",
                        "--generation-dir",
                        "generation",
                    ]
                )

    def test_each_negative_report_returns_nonzero(self):
        for command, method, key, extra in (
            ("assess", "assess_recovery", "allowed", ["--output-dir", "out"]),
            (
                "prepare-recovery",
                "prepare_recovery",
                "ready",
                ["--output-dir", "out", "--generation-dir", "g"],
            ),
            (
                "verify-generation",
                "verify_generation",
                "valid",
                ["--generation-dir", "g"],
            ),
            (
                "apply-recovery",
                "apply_recovery",
                "applied",
                ["--output-dir", "out", "--generation-dir", "g", "--receipt-dir", "r"],
            ),
        ):
            with self.subTest(command=command):
                code, _, stdout, _ = self.run_command(
                    [command, *extra], method, {key: False, "errors": ["blocked"]}
                )
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(stdout)["errors"], ["blocked"])

    def test_invalid_metadata_fails_before_operation_and_hides_text(self):
        for payload in (
            "not JSON secret-example",
            '"secret-example"',
            '{"cookies": {"auth_token": "secret-example"}}',
            '{"details": [{"ct0": "secret-example"}]}',
            '{"observer_id": "123", "observer_id": "secret-example"}',
        ):
            with tempfile.TemporaryDirectory() as directory:
                metadata = Path(directory) / "binding.json"
                metadata.write_text(payload)
                code, operation, stdout, stderr = self.run_command(
                    ["assess", "--output-dir", "out", "--binding", str(metadata)],
                    "assess_recovery",
                    {"allowed": True},
                )
            self.assertEqual(code, 2)
            operation.assert_not_called()
            self.assertNotIn("secret-example", stdout + stderr)

    def test_recovery_rejects_browser_options(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    ["assess", "--output-dir", "out", "--browser", "chrome"]
                )


if __name__ == "__main__":
    unittest.main()
