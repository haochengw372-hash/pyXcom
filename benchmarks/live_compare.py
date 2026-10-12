"""Coordinate matched SDK arms, alternating order and keeping failed arms."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import arm_order, write_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="Credential-free JSON: sessions [{profile,observer_id}], queries [{query,since,until}], rounds, attempts, seconds, seed, arms",
    )
    parser.add_argument("--source", required=True, help="Candidate src path")
    parser.add_argument("--output", required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    from pyxcom.auth import load_x_cookies

    rows = []
    arms = config.get(
        "arms", ["pyxcom-baseline", "pyxcom-current", "twikit", "twscrape"]
    )
    plan = [
        (session_index, query_index, round_index, position, arm)
        for session_index in range(len(config["sessions"]))
        for query_index in range(len(config["queries"]))
        for round_index in range(config.get("rounds", 2))
        for position, arm in enumerate(
            arm_order(arms, config.get("seed", 42), round_index)
        )
    ]
    credentials = {}
    blocked = False
    for session_index, query_index, round_index, position, arm in plan:
        session = config["sessions"][session_index]
        query = config["queries"][query_index]
        destination = (
            output / f"s{session_index}_q{query_index}_r{round_index}_{position}_{arm}"
        )
        if blocked:
            result = {
                "arm": arm,
                "stop_reason": "not_run_after_auth_or_rate_limit",
                "data_attempts": 0,
            }
        else:
            if session_index not in credentials:
                credentials[session_index] = load_x_cookies(
                    session.get("browser", "chrome"), profile=session["profile"]
                )
            command = [
                sys.executable,
                str(Path(__file__).with_name("live_arm.py")),
                "--arm",
                arm,
                "--query",
                query["query"],
                "--since",
                query["since"],
                "--until",
                query["until"],
                "--observer-id",
                str(session["observer_id"]),
                "--cookies-stdin",
                "--attempts",
                str(config.get("attempts", 2)),
                "--seconds",
                str(config.get("seconds", 90)),
                "--timeout",
                str(config.get("timeout", 15)),
                "--output",
                str(destination),
            ]
            if arm == "pyxcom-current":
                command += ["--source", args.source]
            if args.proxy:
                command += ["--proxy", args.proxy]
            try:
                completed = subprocess.run(
                    command,
                    input=json.dumps(credentials[session_index]),
                    text=True,
                    capture_output=True,
                    timeout=config.get("seconds", 90) + 10,
                )
                report = destination / "results.json"
                result = (
                    json.loads(report.read_text())[0]
                    if report.exists()
                    else {
                        "arm": arm,
                        "stop_reason": "process_failed",
                        "returncode": completed.returncode,
                    }
                )
            except subprocess.TimeoutExpired:
                result = {"arm": arm, "stop_reason": "process_timeout"}
            blocked = result["stop_reason"] in {"http_401", "http_403", "http_429"}
        result.update(
            {
                "session_index": session_index,
                "query_index": query_index,
                "round": round_index,
                "position": position,
                "observer_id": str(session["observer_id"]),
            }
        )
        rows.append(result)
        write_results(output, config, rows)
        print(
            json.dumps(
                {
                    k: result.get(k)
                    for k in [
                        "arm",
                        "stop_reason",
                        "usable_unique_count",
                        "data_attempts",
                    ]
                }
            ),
            flush=True,
        )
    write_results(output, config, rows)


if __name__ == "__main__":
    main()
