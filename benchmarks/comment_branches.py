"""Matched offline experiment for within-conversation branch concurrency.

Only transport responses and request latency are simulated. The public
XClient.save_post_comments method performs traversal, persistence, and export.
These timings measure the controlled fixture, not real-world X throughput.

PYTHONPATH=src python benchmarks/comment_branches.py --output-dir /tmp/branches
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import statistics
import threading
import time
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

from pyxcom import Post, XClient, __version__, assess_recovery, validate_collection

ROOT = "1973931546550894681"


def tweet(identifier: str, parent: str | None, *, replies: int = 0) -> dict:
    return {
        "rest_id": identifier,
        "core": {
            "user_results": {
                "result": {
                    "rest_id": "123",
                    "core": {"screen_name": "fixture", "name": "Offline fixture"},
                }
            }
        },
        "legacy": {
            "user_id_str": "123",
            "created_at": "Mon Sep 29 07:39:00 +0000 2025",
            "full_text": "Synthetic comment branch benchmark fixture",
            "conversation_id_str": ROOT,
            "in_reply_to_status_id_str": parent,
            "reply_count": replies,
        },
    }


def page(*posts: dict, cursor: str | None = None) -> dict:
    entries = [
        {"content": {"itemContent": {"tweet_results": {"result": post}}}}
        for post in posts
    ]
    if cursor:
        entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {
                "instructions": [{"entries": entries}]
            }
        }
    }


def fixtures(branches: int) -> dict:
    first_level = []
    responses = {}
    for number in range(branches):
        parent = str(1974000000000000000 + number * 10)
        first_level.append(tweet(parent, ROOT, replies=2))
        responses[(parent, None)] = page(
            tweet(str(int(parent) + 1), parent), cursor="branch-next"
        )
        responses[(parent, "branch-next")] = page(tweet(str(int(parent) + 2), parent))
    midpoint = max(1, branches // 2)
    responses[(ROOT, None)] = page(*first_level[:midpoint], cursor="root-next")
    responses[(ROOT, "root-next")] = page(*first_level[midpoint:])
    return responses


class SimulatedTransport:
    """Thread-safe fixed payloads with varying, deterministic request delays."""

    def __init__(self, responses: dict, latency: float):
        self.responses = responses
        self.latency = latency
        self.lock = threading.Lock()
        self.active_focals: set[str] = set()
        self.active = 0
        self.peak_active = 0
        self.same_focal_overlap = False
        self.calls: list[tuple[str, str | None]] = []

    def conversation_page(self, focal: str, cursor: str | None = None):
        key = (focal, cursor)
        with self.lock:
            self.same_focal_overlap |= focal in self.active_focals
            self.active_focals.add(focal)
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.calls.append(key)
        try:
            # 30--50 ms at the default 40 ms: vary completion order across
            # branches without varying the paired workload or using randomness.
            factor = (0.75, 1.0, 1.25)[int(focal) % 3]
            time.sleep(self.latency * factor)
            return self.responses[key]
        finally:
            with self.lock:
                self.active -= 1
                self.active_focals.remove(focal)


def digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def one_run(output: Path, *, workers: int, branches: int, latency: float) -> dict:
    responses = fixtures(branches)
    transport = SimulatedTransport(responses, latency)
    client = XClient.__new__(XClient)
    client.delay = 0
    client._x = transport
    client.get_post = Mock(
        return_value=Post(
            id=ROOT,
            author_id="123",
            author_handle="fixture",
            created_at_utc="2025-09-29T07:39:00Z",
            text="Synthetic root",
            url=f"https://x.com/fixture/status/{ROOT}",
            conversation_id=ROOT,
            reply_count=branches,
        )
    )
    started = time.perf_counter()
    result = client.save_post_comments(
        ROOT,
        output,
        max_depth=2,
        max_comments=None,
        max_pages=None,
        comment_workers=workers,
    )
    wall_seconds = time.perf_counter() - started
    validation = validate_collection(output)
    recovery = assess_recovery(output)
    with (output / "comments.csv").open(encoding="utf-8-sig", newline="") as stream:
        comments = list(csv.DictReader(stream))
    ids = sorted(row["comment_id"] for row in comments)
    edges = sorted(
        (row["comment_id"], row["parent_post_id"], int(row["depth"]))
        for row in comments
    )
    state = json.loads((output / ".pyxcom/state.json").read_text())["conversation"]
    if not validation["valid"] or not recovery["allowed"]:
        raise AssertionError({"validation": validation, "recovery": recovery})
    if not result.complete or len(ids) != branches * 3:
        raise AssertionError("Fixture is incomplete or has the wrong comment count")
    if len(transport.calls) != len(responses) or set(transport.calls) != set(responses):
        raise AssertionError("Different request workload or duplicate page calls")
    if transport.same_focal_overlap or transport.active:
        raise AssertionError("Same-focal overlap or requests still active after save")
    for focal, cursor in responses:
        if cursor is not None and transport.calls.index(
            (focal, None)
        ) >= transport.calls.index((focal, cursor)):
            raise AssertionError("A continuation was requested before its first page")
    return {
        "comment_workers": workers,
        "wall_seconds": wall_seconds,
        "actual_page_calls": len(transport.calls),
        "peak_active_requests": transport.peak_active,
        "same_focal_overlap": transport.same_focal_overlap,
        "unique_comments": len(ids),
        "depth_counts": dict(sorted(Counter(row["depth"] for row in comments).items())),
        "ids_sha256": digest(ids),
        "edges_and_depth_sha256": digest(edges),
        "accepted_order_sha256": digest(state["accepted_ids"]),
        "request_workload_sha256": digest(
            sorted((focal, cursor or "") for focal, cursor in transport.calls)
        ),
        "validation_valid": validation["valid"],
        "recovery_allowed": recovery["allowed"],
        "record_integrity": recovery["record_integrity"],
        "complete": result.complete,
        "reason": result.reason,
        "collection_dir": str(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--branches", type=int, default=16)
    parser.add_argument("--latency-ms", type=float, default=40)
    args = parser.parse_args()
    if args.repeats < 3 or args.branches < 4 or args.latency_ms < 0:
        parser.error("Use repeats >= 3, branches >= 4, and latency-ms >= 0")
    if args.output_dir.exists():
        parser.error("Use a new output directory to avoid resuming an old benchmark")
    args.output_dir.mkdir(parents=True)
    runs = []
    for repeat in range(args.repeats):
        order = (1, 2, 4) if repeat % 2 == 0 else (4, 2, 1)
        for workers in order:
            run = one_run(
                args.output_dir / f"repeat-{repeat + 1}-workers-{workers}",
                workers=workers,
                branches=args.branches,
                latency=args.latency_ms / 1000,
            )
            run["repeat"] = repeat + 1
            runs.append(run)
            print(json.dumps(run), flush=True)
    equivalence_keys = (
        "ids_sha256",
        "edges_and_depth_sha256",
        "accepted_order_sha256",
        "request_workload_sha256",
    )
    equivalent = all(len({row[key] for row in runs}) == 1 for key in equivalence_keys)
    if not equivalent:
        raise AssertionError(
            "Worker counts/repeats produced different retained records or request workloads"
        )
    summary = {}
    for workers in (1, 2, 4):
        selected = [row for row in runs if row["comment_workers"] == workers]
        summary[str(workers)] = {
            "median_wall_seconds": statistics.median(
                row["wall_seconds"] for row in selected
            ),
            "page_calls_per_run": sorted(
                {row["actual_page_calls"] for row in selected}
            ),
            "peak_active_requests": sorted(
                {row["peak_active_requests"] for row in selected}
            ),
        }
    for arm in summary.values():
        arm["simulated_speedup_vs_serial"] = (
            summary["1"]["median_wall_seconds"] / arm["median_wall_seconds"]
        )
    report = {
        "schema": "pyxcom.comment-branch-benchmark/1",
        "mode": "simulated_latency",
        "scope": "Offline matched synthetic payloads; not live X throughput or a limit/safety benchmark.",
        "package_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "branches": args.branches,
        "per_request_latency_ms_range": [
            args.latency_ms * 0.75,
            args.latency_ms * 1.25,
        ],
        "client_delay_seconds": 0,
        "repeats": args.repeats,
        "all_arms_ids_edges_depths_order_and_request_workload_equivalent": equivalent,
        "summary": summary,
        "runs": runs,
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"summary": summary, "equivalent": equivalent}, indent=2))


if __name__ == "__main__":
    main()
