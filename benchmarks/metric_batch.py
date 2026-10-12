"""Offline paired PostStore benchmark; no browser, network, or research data access.

Run with a retained baseline storage.py from the same checkout before the edit:
  PYTHONPATH=src python benchmarks/metric_batch.py \
    --baseline-source /tmp/storage-baseline.py --output /tmp/metric-results.json

Only storage.py varies. Both arms use this checkout's other modules. Timing runs
are uninstrumented; fsync counters are collected in separate runs. Fixed clocks
allow exact hashes of every retained artifact to be compared.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from pyxcom import storage
from pyxcom.integrity import assess_recovery
from pyxcom.models import Post

CLOCK = "2026-10-12T00:00:00+00:00"


def fixtures(count):
    return [
        Post(
            id=str(1000000000000 + i),
            author_id=str(100 + i % 100),
            author_handle=f"synthetic{i % 100}",
            created_at_utc="2026-01-01T00:00:00Z",
            text="Synthetic AI discourse fixture. " * 8,
            url=f"https://example.invalid/post/{i}",
            like_count=i % 99,
            captured_at_utc=CLOCK,
            in_reply_to_id=str(1000000000000 + i - 1) if i % 10 else None,
            conversation_id=str(1000000000000 + i - i % 10),
        )
        for i in range(count)
    ]


def hashes(directory):
    return {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def run(module, posts, page_size, *, count_fsync=False):
    with tempfile.TemporaryDirectory(prefix="pyxcom-metric-benchmark-") as tmp:
        directory = Path(tmp)
        with patch.object(module, "now_utc", return_value=CLOCK):
            store = module.PostStore(directory, query={"kind": "offline_fixture"})
            original_fsync = os.fsync
            counter = 0

            def counted(fd):
                nonlocal counter
                counter += 1
                return original_fsync(fd)

            # Separate counter runs never contribute to timing summaries.
            with patch("os.fsync", counted if count_fsync else original_fsync):
                start = time.perf_counter()
                for at in range(0, len(posts), page_size):
                    store.append_page(posts[at : at + page_size], str(at + page_size))
                append_seconds = time.perf_counter() - start
                start = time.perf_counter()
                store.finish(complete=False, reason="page_limit")
                finish_seconds = time.perf_counter() - start
        artifacts = hashes(directory)
        start = time.perf_counter()
        restored = module.PostStore(directory, query={"kind": "offline_fixture"})
        restore_seconds = time.perf_counter() - start
        report = assess_recovery(directory)
        if not report["resume_allowed"] or restored.count != len(posts):
            raise AssertionError("Fixture failed restoration/integrity checks")
        return {
            "append_seconds": append_seconds,
            "finish_seconds": finish_seconds,
            "restore_seconds": restore_seconds,
            "fsync_calls": counter if count_fsync else None,
            "file_sha256": artifacts,
            "count": restored.count,
            "resume_allowed": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or args.output.exists():
        parser.error("Use repeats >= 1 and a new output path")
    spec = importlib.util.spec_from_file_location(
        "pyxcom._baseline_storage", args.baseline_source
    )
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "baseline_storage_sha256": hashlib.sha256(
            args.baseline_source.read_bytes()
        ).hexdigest(),
        "candidate_storage_sha256": hashlib.sha256(
            Path(storage.__file__).read_bytes()
        ).hexdigest(),
        "design": "Offline synthetic fixtures; identical other modules; reversed arm order on alternate repeats; no timing instrumentation; separate fsync-count runs; exact hashes include all CSV and private/public manifests.",
        "cases": [],
    }
    for count in (1000, 10000):
        posts = fixtures(count)
        for page_size in (50, 500):
            runs = {"baseline": [], "candidate": []}
            reference = None
            for repeat in range(args.repeats):
                arms = [("baseline", baseline), ("candidate", storage)]
                if repeat % 2:
                    arms.reverse()
                for name, module in arms:
                    observation = run(module, posts, page_size)
                    digest = observation.pop("file_sha256")
                    if reference is None:
                        reference = digest
                    if digest != reference:
                        raise AssertionError(
                            "Retained artifacts differ between arms/repeats"
                        )
                    runs[name].append(observation)
            case = {
                "records": count,
                "page_size": page_size,
                "runs": runs,
                "all_artifact_hashes_identical": True,
                "file_sha256": reference,
            }
            for name, module in (("baseline", baseline), ("candidate", storage)):
                counted = run(module, posts, page_size, count_fsync=True)
                if counted["file_sha256"] != reference:
                    raise AssertionError("Counter run changed artifacts")
                case[name] = {
                    key: statistics.median(row[key] for row in runs[name])
                    for key in ("append_seconds", "finish_seconds", "restore_seconds")
                }
                case[name]["fsync_calls"] = counted["fsync_calls"]
            result["cases"].append(case)
            print(
                json.dumps(
                    {
                        k: case[k]
                        for k in ("records", "page_size", "baseline", "candidate")
                    }
                ),
                flush=True,
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
