"""Exercise the benchmark's assertions through the package's public saver."""

import sys
import tempfile
import unittest
from pathlib import Path

benchmark_dir = Path(__file__).resolve().parents[1] / "benchmarks"
if not benchmark_dir.exists():
    raise unittest.SkipTest("Benchmark tooling is outside the installed wheel")
sys.path.insert(0, str(benchmark_dir))
from comment_branches import one_run  # noqa: E402


class CommentBranchBenchmarkTests(unittest.TestCase):
    def test_paired_workers_retain_identical_ids_edges_order_and_workload(self):
        with tempfile.TemporaryDirectory() as temporary:
            runs = [
                one_run(
                    Path(temporary) / str(workers),
                    workers=workers,
                    branches=4,
                    latency=0.005,
                )
                for workers in (1, 2, 4)
            ]
        for key in (
            "ids_sha256",
            "edges_and_depth_sha256",
            "accepted_order_sha256",
            "request_workload_sha256",
        ):
            self.assertEqual(len({row[key] for row in runs}), 1, key)
        self.assertEqual([row["actual_page_calls"] for row in runs], [10, 10, 10])
        for run in runs:
            self.assertEqual(run["unique_comments"], 12)
            self.assertEqual(run["depth_counts"], {"1": 4, "2": 8})
            self.assertTrue(run["validation_valid"])
            self.assertTrue(run["recovery_allowed"])
            self.assertFalse(run["same_focal_overlap"])
            self.assertLessEqual(run["peak_active_requests"], run["comment_workers"])
        self.assertEqual(runs[0]["peak_active_requests"], 1)
        self.assertGreaterEqual(runs[1]["peak_active_requests"], 2)
        self.assertGreaterEqual(runs[2]["peak_active_requests"], 2)
