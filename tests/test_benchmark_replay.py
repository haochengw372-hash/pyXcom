"""Check real saver/pool equivalence under controlled transport replay."""

import sys
import tempfile
import unittest
from pathlib import Path

benchmark_dir = Path(__file__).resolve().parents[1] / "benchmarks"
if not benchmark_dir.exists():
    raise unittest.SkipTest(
        "Benchmark tooling is intentionally outside the installed wheel"
    )
sys.path.insert(0, str(benchmark_dir))
from replay import one_run  # noqa: E402


class BenchmarkReplayTests(unittest.TestCase):
    def run_pair(self, scenario):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        serial = one_run(
            root / "serial", workers=1, pages=3, latency=0.01, scenario=scenario
        )
        concurrent = one_run(
            root / "concurrent", workers=2, pages=3, latency=0.01, scenario=scenario
        )
        self.assertEqual(serial["ids"], concurrent["ids"])
        self.assertEqual(serial["endpoint_attempts"], concurrent["endpoint_attempts"])
        self.assertEqual(serial["max_simultaneous_observers"], 1)
        self.assertEqual(concurrent["max_simultaneous_observers"], 2)
        for result in (serial, concurrent):
            self.assertFalse(result["same_observer_overlap"])
            self.assertEqual(result["validations"], {"a": True, "b": True})
        return serial, concurrent

    def test_healthy_same_records_and_budget_actual_parallelism(self):
        for result in self.run_pair("healthy"):
            self.assertEqual(len(result["ids"]), 6)
            self.assertEqual(result["endpoint_attempts"], 6)
            self.assertTrue(
                all(
                    job["status"] == "complete"
                    for job in result["pool_report"]["jobs"].values()
                )
            )

    def test_rate_limit_retains_records_and_other_identity_finishes(self):
        for result in self.run_pair("rate_limit"):
            self.assertEqual(len(result["ids"]), 4)
            self.assertEqual(
                result["pool_report"]["jobs"]["a"]["reason"], "rate_limited"
            )
            self.assertEqual(result["pool_report"]["jobs"]["b"]["status"], "complete")

    def test_api_failure_retains_records_and_other_identity_finishes(self):
        for result in self.run_pair("failure"):
            self.assertEqual(len(result["ids"]), 4)
            self.assertEqual(
                result["pool_report"]["jobs"]["a"]["reason"], "source_error"
            )
            self.assertEqual(result["pool_report"]["jobs"]["b"]["status"], "complete")
