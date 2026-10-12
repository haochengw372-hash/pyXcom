"""Deterministic scheduler experiment using real XClient savers and pool state.

Only the network transport is replaced. Timings describe a controlled workload,
not live X throughput. Healthy, rate-limited and failed arms use identical fixtures.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from common import arm_order, write_results


def payload(observer: str, index: int, pages: int):
    identifier = str(int(observer) * 1000 + index)
    item = {
        "rest_id": identifier,
        "core": {
            "user_results": {
                "result": {"rest_id": observer, "legacy": {"screen_name": "fixture"}}
            }
        },
        "legacy": {
            "user_id_str": observer,
            "created_at": "Tue Sep 01 12:00:00 +0000 2026",
            "full_text": "fixture",
        },
    }
    entries = [
        {
            "entryId": "tweet-" + identifier,
            "content": {"itemContent": {"tweet_results": {"result": item}}},
        }
    ]
    instructions = [{"type": "TimelineAddEntries", "entries": entries}]
    if index + 1 < pages:
        entries.append(
            {"entryId": "cursor-bottom-0", "content": {"value": str(index + 1)}}
        )
    else:
        instructions.append(
            {"type": "TimelineTerminateTimeline", "direction": "Bottom"}
        )
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {"timeline": {"instructions": instructions}}
            }
        }
    }


class ReplayWorld:
    def __init__(self, *, pages, latency, scenario):
        self.pages = pages
        self.latency = latency
        self.scenario = scenario
        self.lock = threading.Lock()
        self.active = set()
        self.peak = 0
        self.overlap_violation = False
        self.events = []
        self.faulted = set()

    def request(self, observer, index):
        from pyxcom.errors import APIError, RateLimitError

        with self.lock:
            if observer in self.active:
                self.overlap_violation = True
            self.active.add(observer)
            self.peak = max(self.peak, len(self.active))
        try:
            time.sleep(self.latency)
            status = 200
            with self.lock:
                if (
                    observer == "100"
                    and index == 1
                    and self.scenario != "healthy"
                    and observer not in self.faulted
                ):
                    self.faulted.add(observer)
                    status = 429 if self.scenario == "rate_limit" else 503
                self.events.append(
                    {"observer_id": observer, "page_index": index, "status": status}
                )
            if status == 429:
                raise RateLimitError(
                    "Replay rate limit",
                    reset_at=int(time.time()) + 60,
                    status_code=429,
                    operation="SearchTimeline",
                )
            if status == 503:
                raise APIError(
                    "Replay endpoint failure",
                    status_code=503,
                    operation="SearchTimeline",
                )
            return payload(observer, index, self.pages)
        finally:
            with self.lock:
                self.active.remove(observer)


class ReplayTransport:
    def __init__(self, observer, world):
        self.observer = observer
        self.world = world
        self._request_gate = None
        self._request_observer = None

    def get_login_identity(self):
        return self.observer

    def search_page(self, query, cursor=None, **kwargs):
        if self._request_gate:
            self._request_gate("SearchTimeline")
        result = self.world.request(self.observer, int(cursor or 0))
        return result

    def close(self):
        pass


def one_run(destination, *, workers, pages=3, latency=0.05, scenario="healthy"):
    from pyxcom import XClient, validate_collection
    from pyxcom.pool import CollectionJob, LoginConfig, XClientPool

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    world = ReplayWorld(pages=pages, latency=latency, scenario=scenario)

    def factory(**kwargs):
        observer = {"A": "100", "B": "200"}[kwargs["profile"]]
        client = object.__new__(XClient)
        client._x = ReplayTransport(observer, world)
        client._observer_id = observer
        client._mirror = None
        client._profile_cache = {}
        client.delay = 0
        return client

    start = time.perf_counter()
    with patch.object(XClient, "from_login", side_effect=factory):
        with XClientPool(
            [
                LoginConfig("a", profile="A", expected_observer_id="100"),
                LoginConfig("b", profile="B", expected_observer_id="200"),
            ],
            state_dir=destination / "queue",
            client_options={"delay": 0},
        ) as pool:
            pool.add_jobs(
                [
                    CollectionJob(
                        login,
                        "search_query",
                        "fixture",
                        destination / login,
                        options={"since": "2026-09-01", "until": "2026-09-02"},
                        login_id=login,
                    )
                    for login in ("a", "b")
                ]
            )
            report = pool.run(
                page_budget=1,
                max_steps=pages * 2 + 2,
                max_workers=workers,
                wait_on_rate_limit=False,
                max_seconds=30,
            )
    elapsed = time.perf_counter() - start
    ids = []
    validations = {}
    for login in ("a", "b"):
        file = destination / login / "posts.csv"
        if file.exists():
            with file.open(newline="", encoding="utf-8-sig") as handle:
                ids.extend(row["post_id"] for row in csv.DictReader(handle))
            validations[login] = validate_collection(destination / login)["valid"]
    result = {
        "mode": "deterministic_transport_replay",
        "workers": workers,
        "scenario": scenario,
        "pages_per_observer": pages,
        "latency_per_request_seconds": latency,
        "elapsed_seconds": elapsed,
        "endpoint_attempts": len(world.events),
        "max_simultaneous_observers": world.peak,
        "same_observer_overlap": world.overlap_violation,
        "ids": sorted(ids),
        "validations": validations,
        "events": world.events,
        "pool_report": report,
    }
    write_results(
        destination,
        {"workers": workers, "pages": pages, "latency": latency, "scenario": scenario},
        [result],
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--latency", type=float, default=0.1)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source).resolve()))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for scenario in ("healthy", "rate_limit", "failure"):
        for repeat in range(args.rounds):
            for arm in arm_order(["serial", "concurrent"], 42, repeat):
                result = one_run(
                    output / f"{scenario}_{repeat}_{arm}",
                    workers=1 if arm == "serial" else 2,
                    pages=args.pages,
                    latency=args.latency,
                    scenario=scenario,
                )
                result.update({"arm": arm, "round": repeat})
                results.append(result)
    write_results(
        output,
        {
            "mode": "deterministic_transport_replay",
            "latency": args.latency,
            "pages": args.pages,
            "rounds": args.rounds,
        },
        results,
    )
    print(
        json.dumps(
            [
                {
                    k: r[k]
                    for k in (
                        "scenario",
                        "arm",
                        "elapsed_seconds",
                        "endpoint_attempts",
                        "max_simultaneous_observers",
                    )
                }
                for r in results
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
