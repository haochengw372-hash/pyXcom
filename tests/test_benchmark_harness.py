import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

benchmark_dir = Path(__file__).resolve().parents[1] / "benchmarks"
if not benchmark_dir.exists():
    raise unittest.SkipTest(
        "Benchmark tooling is intentionally outside the installed wheel"
    )
sys.path.insert(0, str(benchmark_dir))
from common import (  # noqa: E402
    BenchmarkStop,
    WireAudit,
    arm_order,
    in_window,
    sanitize,
    write_results,
)


class BenchmarkHarnessTests(unittest.TestCase):
    def test_arm_order_reverses_and_is_deterministic(self):
        arms = ["new", "old", "twikit", "twscrape"]
        first = arm_order(arms, 42, 0)
        self.assertEqual(first, arm_order(arms, 42, 2))
        self.assertEqual(first[::-1], arm_order(arms, 42, 1))
        self.assertEqual(set(first), set(arms))

    def test_sync_wire_budget_counts_retries_before_send(self):
        calls = []

        def responder(request):
            calls.append(request)
            return httpx.Response(503, json={"error": "temporary"})

        with tempfile.TemporaryDirectory() as tmp:
            audit = WireAudit(Path(tmp) / "raw", max_attempts=2, seconds=10)
            with (
                audit.intercept(),
                httpx.Client(transport=httpx.MockTransport(responder)) as client,
            ):
                for _ in range(2):
                    client.get("https://x.com/i/api/graphql/key/SearchTimeline")
                with self.assertRaises(BenchmarkStop):
                    client.get("https://x.com/i/api/graphql/key/SearchTimeline")
            self.assertEqual(len(calls), 2)
            self.assertEqual(audit.data_attempts, 2)
            self.assertEqual([e["status"] for e in audit.events], [503, 503])

    def test_async_429_stops_before_sdk_can_rotate(self):
        async def exercise(tmp):
            audit = WireAudit(Path(tmp) / "raw", max_attempts=3, seconds=10)
            with audit.intercept():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda r: httpx.Response(429, json={})
                    )
                ) as client:
                    with self.assertRaises(BenchmarkStop):
                        await client.get(
                            "https://x.com/i/api/graphql/key/SearchTimeline"
                        )
            self.assertEqual(audit.data_attempts, 1)
            self.assertEqual(audit.events[0]["status"], 429)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(exercise(tmp))

    def test_setup_separate_cap_and_raw_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = WireAudit(
                Path(tmp) / "raw",
                max_attempts=1,
                seconds=10,
                setup_cap=1,
                secrets=["cookie-secret"],
            )
            transport = httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"auth_token": "secret", "message": "cookie-secret"}
                )
            )
            with audit.intercept(), httpx.Client(transport=transport) as client:
                client.get("https://x.com/")
                client.get("https://x.com/i/api/graphql/key/SearchTimeline")
                with self.assertRaises(BenchmarkStop):
                    client.get("https://x.com/")
            raw = json.loads(
                next((Path(tmp) / "raw").glob("response_*.json")).read_text()
            )
            self.assertEqual(raw, {"auth_token": "[redacted]", "message": "[redacted]"})
            self.assertEqual(audit.setup_attempts, 1)

    def test_primary_normalizer_excludes_embedded_quotes(self):
        from normalize import primary_records
        from replay import payload

        body = payload("100", 0, 1)
        entries = body["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
            "instructions"
        ][0]["entries"]
        result = entries[0]["content"]["itemContent"]["tweet_results"]["result"]
        result["quoted_status_result"] = {
            "result": {
                "rest_id": "embedded",
                "legacy": {"created_at": "Tue Sep 01 12:00:00 +0000 2026"},
            }
        }
        self.assertEqual(set(primary_records(body)), {"100000"})

    def test_observer_budget_and_identity_setup_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = WireAudit(
                Path(tmp) / "raw",
                max_attempts=4,
                seconds=10,
                data_operations={"SearchTimeline"},
                per_observer_limit=1,
            )
            audit.observer_by_token = {"token-a": "1", "token-b": "2"}
            transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            with audit.intercept(), httpx.Client(transport=transport) as client:
                client.get(
                    "https://api.x.com/1.1/account/settings.json",
                    headers={"cookie": "auth_token=token-a"},
                )
                client.get(
                    "https://x.com/i/api/graphql/key/SearchTimeline",
                    headers={"cookie": "auth_token=token-a"},
                )
                with self.assertRaises(BenchmarkStop):
                    client.get(
                        "https://x.com/i/api/graphql/key/SearchTimeline",
                        headers={"cookie": "auth_token=token-a"},
                    )
                client.get(
                    "https://x.com/i/api/graphql/key/SearchTimeline",
                    headers={"cookie": "auth_token=token-b"},
                )
            self.assertEqual(audit.observer_attempts, {"1": 1, "2": 1})
            self.assertEqual(audit.setup_attempts, 1)
            self.assertEqual(audit.data_attempts, 2)

    def test_pool_summary_counts_canonical_replies_and_tables(self):
        from pool_summary import retained_records

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "job_0"
            (directory / ".pyxcom").mkdir(parents=True)
            (directory / ".pyxcom/posts.jsonl").write_text(
                '{"id":"1"}\n{"id":"2"}\n{"id":"3"}\n'
            )
            (directory / "posts.csv").write_text("post_id\n1\n2\n")
            (directory / "comments.csv").write_text("comment_id\n3\n")
            ids, counts = retained_records(tmp)
            self.assertEqual(ids, {"0": ["1", "2", "3"]})
            self.assertEqual(counts, {"0": {"posts.csv": 2, "comments.csv": 1}})

    def test_window_is_utc_half_open_and_artifacts_are_hashed(self):
        self.assertTrue(in_window("2026-09-01T00:00:00Z", "2026-09-01", "2026-10-01"))
        self.assertFalse(in_window("2026-10-01T00:00:00Z", "2026-09-01", "2026-10-01"))
        self.assertFalse(in_window(None, "2026-09-01", "2026-10-01"))
        with tempfile.TemporaryDirectory() as tmp:
            write_results(
                tmp, {"cookies": {"a": "secret"}}, [{"ids": ["1"], "count": 1}]
            )
            self.assertNotIn("secret", (Path(tmp) / "config.json").read_text())
            self.assertEqual(
                set(json.loads((Path(tmp) / "sha256.json").read_text())),
                {"config.json", "results.csv", "results.json"},
            )
        self.assertEqual(sanitize({"cookie": "x"}), {"cookie": "[redacted]"})
