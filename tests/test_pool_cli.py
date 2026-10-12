"""Offline pool command contract; no cookies, clients or dataset writes."""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from pyxcom.cli import build_parser, main


class PoolCLITests(unittest.TestCase):
    def invoke(self, config, extra=(), *, failure=None, literal=None):
        module = types.ModuleType("pyxcom.pool")
        login_type = Mock(side_effect=lambda **kwargs: types.SimpleNamespace(**kwargs))
        job_type = Mock(side_effect=lambda **kwargs: types.SimpleNamespace(**kwargs))
        factory = MagicMock()
        pool = factory.return_value.__enter__.return_value
        pool.run.return_value = {"complete": False, "reason": "max_steps"}
        pool.run.side_effect = failure
        module.LoginConfig = login_type
        module.CollectionJob = job_type
        module.XClientPool = factory
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(literal if literal is not None else json.dumps(config))
            with (
                patch.dict(sys.modules, {"pyxcom.pool": module}),
                patch("pyxcom.cli.XClient") as single_client,
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = main(
                    ["pool", "--config", str(path), "--state-dir", "state", *extra]
                )
            single_client.assert_not_called()
        return code, factory, pool, login_type, job_type, out.getvalue(), err.getvalue()

    def config(self):
        return {
            "logins": [
                {"login_id": "chrome-a", "profile": "Default"},
                {"login_id": "safari-b", "browser": "safari"},
            ],
            "jobs": [
                {
                    "job_id": "query-1",
                    "kind": "search_query",
                    "target": "reset",
                    "output_dir": "output/query-1",
                }
            ],
        }

    def test_default_dispatch_has_no_record_or_step_caps(self):
        code, factory, pool, login_type, job_type, stdout, stderr = self.invoke(
            self.config()
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertFalse(json.loads(stdout)["complete"])
        factory.assert_called_once_with(
            [types.SimpleNamespace(**item) for item in self.config()["logins"]],
            state_dir=Path("state"),
            client_options=None,
            rate_limit_scope="auto",
        )
        pool.run.assert_called_once_with(
            page_budget=1,
            max_steps=None,
            max_workers=4,
            max_seconds=None,
            wait_on_rate_limit=False,
        )
        self.assertNotIn("limit", job_type.call_args.kwargs)
        self.assertNotIn("options", job_type.call_args.kwargs)

    def test_options_ownership_priority_and_client_settings_forward(self):
        config = self.config()
        config["logins"][0].update(
            cookie_db="/accessible/Cookies", expected_observer_id="123"
        )
        config["client_options"] = {"proxy": "http://user:secret@proxy", "delay": 0.5}
        config["rate_limit_scope"] = "shared"
        config["jobs"][0].update(
            priority=12,
            login_id="chrome-a",
            existing_observer_id="123",
            options={"since": "2026-01-01", "until": "2026-02-01", "limit": None},
        )
        code, factory, pool, _, job_type, stdout, stderr = self.invoke(
            config, ["--page-budget", "3", "--max-steps", "all", "--wait-on-rate-limit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            factory.call_args.kwargs["client_options"], config["client_options"]
        )
        self.assertEqual(factory.call_args.kwargs["rate_limit_scope"], "shared")
        job_type.assert_called_once_with(**config["jobs"][0])
        pool.run.assert_called_once_with(
            page_budget=3,
            max_steps=None,
            max_workers=4,
            max_seconds=None,
            wait_on_rate_limit=True,
        )
        self.assertNotIn("secret", stdout + stderr)

    def test_each_atomic_kind_and_appropriate_options(self):
        options = {
            "search_query": {"limit": None},
            "post_quotes": {"until": "2026-02-01"},
            "user_posts": {"limit": None},
            "user_replies": {"since": "2026-01-01"},
            "user_reposts": {"until": "2026-02-01"},
            "post_comments": {"max_comments": None, "max_depth": 3},
            "followers": {"snapshot_id": "wave-1"},
            "following": {"limit": None},
            "post_reposters": {},
        }
        config = self.config()
        config["jobs"] = [
            dict(
                job_id=kind,
                kind=kind,
                target="1234567890",
                output_dir=kind,
                options=opts,
            )
            for kind, opts in options.items()
        ]
        code, _, pool, _, job_type, _, _ = self.invoke(config)
        self.assertEqual(code, 0)
        self.assertEqual(job_type.call_count, 9)
        self.assertEqual(len(pool.add_jobs.call_args.args[0]), 9)
        self.assertEqual(
            job_type.call_args_list[5].kwargs["options"]["max_comments"], None
        )

    def test_explicit_resume_forwards_retry_flag(self):
        code, _, pool, _, _, _, _ = self.invoke(
            self.config(), ["--resume-job", "query-1", "--retry-stalled"]
        )
        self.assertEqual(code, 0)
        pool.resume_job.assert_called_once_with("query-1", retry_stalled=True)

    def test_retry_requires_resume_before_factory(self):
        code, factory, _, _, _, _, _ = self.invoke(self.config(), ["--retry-stalled"])
        self.assertEqual(code, 2)
        factory.assert_not_called()

    def test_unknown_and_secret_fields_rejected_before_factory(self):
        for location in ("root", "login", "job", "options", "client"):
            for key in ("auth_token", "cookies", "password", "arbitrary"):
                with self.subTest(location=location, key=key):
                    config = self.config()
                    container = {
                        "root": config,
                        "login": config["logins"][0],
                        "job": config["jobs"][0],
                        "options": config["jobs"][0].setdefault("options", {}),
                        "client": config.setdefault("client_options", {}),
                    }[location]
                    container[key] = "sensitive-value"
                    code, factory, _, _, _, stdout, stderr = self.invoke(config)
                    self.assertEqual(code, 2)
                    factory.assert_not_called()
                    self.assertNotIn("sensitive-value", stdout + stderr)
                    self.assertNotIn(key, stderr)

    def test_malformed_and_duplicate_json_redacted(self):
        for literal in ("secret-invalid-json", '{"logins": [], "logins": "secret"}'):
            code, factory, _, _, _, stdout, stderr = self.invoke({}, literal=literal)
            self.assertEqual(code, 2)
            factory.assert_not_called()
            self.assertNotIn("secret", stdout + stderr)

    def test_page_caps_and_retry_belong_to_run_or_resume_not_job_options(self):
        for key, value in (
            ("max_pages", 5),
            ("retry_stalled", True),
            ("timeline", "posts"),
        ):
            config = self.config()
            config["jobs"][0]["options"] = {key: value}
            code, factory, _, _, _, _, _ = self.invoke(config)
            self.assertEqual(code, 2)
            factory.assert_not_called()

    def test_bad_nested_shapes_and_unknown_kind_fail_before_factory(self):
        for config in (
            [],
            {"logins": {}},
            {"logins": [{}]},
            {"logins": [{"login_id": "a"}], "jobs": [{}]},
            {"logins": [{"login_id": "a"}], "client_options": []},
            {"logins": [{"login_id": "a"}], "rate_limit_scope": "secret"},
        ):
            code, factory, _, _, _, _, _ = self.invoke(config)
            self.assertEqual(code, 2)
            factory.assert_not_called()
        config = self.config()
        config["jobs"][0]["kind"] = "unknown"
        self.assertEqual(self.invoke(config)[0], 2)

    def test_pool_errors_do_not_echo_secret_or_config_values(self):
        code, _, _, _, _, stdout, stderr = self.invoke(
            self.config(), failure=ValueError("secret-from-proxy-or-cookie")
        )
        self.assertEqual(code, 2)
        self.assertNotIn("secret-from-proxy-or-cookie", stdout + stderr)

    def test_pool_has_no_single_login_flags(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                ["pool", "--config", "c", "--state-dir", "s", "--profile", "Default"]
            )

    def test_page_budget_positive_and_max_steps_all(self):
        parser = build_parser()
        for value in ("0", "-1", "all"):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(
                    [
                        "pool",
                        "--config",
                        "c",
                        "--state-dir",
                        "s",
                        "--page-budget",
                        value,
                    ]
                )
        args = parser.parse_args(
            ["pool", "--config", "c", "--state-dir", "s", "--max-steps", "all"]
        )
        self.assertIsNone(args.max_steps)


if __name__ == "__main__":
    unittest.main()
