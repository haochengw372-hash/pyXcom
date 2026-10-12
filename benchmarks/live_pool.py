"""Bounded real pool experiment; run only after competing live SDK arms finish."""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import time
from pathlib import Path

from common import BenchmarkStop, WireAudit, arm_order, write_results
from live_arm import _deadline
from pool_summary import retained_records


def one_arm(args, config, output, workers):
    from pyxcom import validate_collection
    from pyxcom.pool import CollectionJob, LoginConfig, XClientPool

    output.mkdir(parents=True, exist_ok=False)
    audit = WireAudit(
        output / "raw",
        max_attempts=4,
        seconds=60,
        data_operations={"SearchTimeline"},
        per_observer_limit=2,
    )
    logins = [
        LoginConfig(
            f"observer_{i}",
            browser=s.get("browser", "chrome"),
            profile=s["profile"],
            expected_observer_id=str(s["observer_id"]),
        )
        for i, s in enumerate(config["sessions"])
    ]
    if len(logins) != 2:
        raise ValueError("This experiment requires exactly two verified observers")
    query = config["queries"][0]
    report = None
    reason = "returned"
    start = time.perf_counter()
    previous = signal.signal(signal.SIGALRM, _deadline)
    signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        with audit.intercept():
            with XClientPool(
                logins,
                state_dir=output / "queue",
                client_options={"delay": 1, "timeout": 12, "proxy": args.proxy},
            ) as pool:
                # Observer identities have just been independently verified by
                # the package; bind only in memory to identify wire attempts.
                for client in pool._clients.values():
                    for cookie in client._x._x.cookies.jar:
                        if cookie.name == "auth_token":
                            audit.observer_by_token[cookie.value] = client.observer_id
                        if cookie.value:
                            audit.secrets += (cookie.value,)
                jobs = [
                    CollectionJob(
                        f"job_{i}",
                        "search_query",
                        query["query"],
                        output / f"job_{i}",
                        options={"since": query["since"], "until": query["until"]},
                        login_id=login.login_id,
                    )
                    for i, login in enumerate(logins)
                ]
                pool.add_jobs(jobs)
                report = pool.run(
                    page_budget=1,
                    max_steps=4,
                    max_workers=workers,
                    wait_on_rate_limit=False,
                    max_seconds=60,
                )
    except BenchmarkStop as exc:
        reason = str(exc)
    except Exception as exc:
        reason = "error:" + type(exc).__name__
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    elapsed = time.perf_counter() - start
    ids, table_counts = retained_records(output)
    validations = {}
    for index in range(2):
        directory = output / f"job_{index}"
        if (directory / "manifest.json").exists():
            validations[str(index)] = validate_collection(directory)["valid"]
    result = {
        "mode": "live_pool",
        "workers": workers,
        "elapsed_seconds": elapsed,
        "stop_reason": reason,
        "data_attempts": audit.data_attempts,
        "setup_attempts": audit.setup_attempts,
        "observer_attempts": audit.observer_attempts,
        "events": audit.events,
        "ids_by_job": ids,
        "ids_unit": "canonical retained primary search records including replies",
        "table_counts_by_job": table_counts,
        "validations": validations,
        "pool_report": report,
    }
    write_results(
        output,
        {
            "workers": workers,
            "max_steps": 4,
            "page_budget": 1,
            "delay": 1,
            "timeout": 12,
            "max_seconds": 60,
            "query": query,
        },
        [result],
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source).resolve()))
    config = json.loads(Path(args.config).read_text())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    code_files = {
        str(p.relative_to(Path(args.source))): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in sorted((Path(args.source) / "pyxcom").rglob("*.py"))
    }
    config["candidate_source_hashes"] = code_files
    config["experiment"] = "matched_live_pool_serial_concurrent"
    rows = []
    blocked = False
    for repeat in range(2):
        for arm in arm_order(["serial", "concurrent"], 42, repeat):
            if blocked:
                result = {
                    "arm": arm,
                    "round": repeat,
                    "stop_reason": "not_run_after_auth_or_rate_limit",
                }
            else:
                result = one_arm(
                    args,
                    config,
                    output / f"round_{repeat}_{arm}",
                    1 if arm == "serial" else 2,
                )
                result.update({"arm": arm, "round": repeat})
                blocked = result["stop_reason"] in {"http_401", "http_403", "http_429"}
            rows.append(result)
            write_results(output, config, rows)
            print(
                json.dumps(
                    {
                        k: result.get(k)
                        for k in [
                            "arm",
                            "round",
                            "stop_reason",
                            "data_attempts",
                            "elapsed_seconds",
                            "validations",
                        ]
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
