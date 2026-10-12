"""Compare primary timeline units from retained sanitized search responses.

Structural traversal stops at each tweet_results.result. Embedded quoted/reposted
objects are not separate primary search hits. No network or SDK parser is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from common import in_window, write_results


def primary_records(payload):
    try:
        instructions = payload["data"]["search_by_raw_query"]["search_timeline"][
            "timeline"
        ]["instructions"]
    except (KeyError, TypeError):
        return {}
    records = {}

    def walk(value):
        if isinstance(value, dict):
            if "tweet_results" in value:
                result = value.get("tweet_results", {}).get("result", {})
                if result.get("__typename") == "TweetWithVisibilityResults":
                    result = result.get("tweet", {})
                identifier = result.get("rest_id")
                created = result.get("legacy", {}).get("created_at")
                if identifier and created:
                    records[str(identifier)] = {"created_at": created}
                return
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(instructions)
    return records


def normalize(directory):
    directory = Path(directory)
    config = json.loads((directory / "config.json").read_text())
    rows = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / "results.json").exists():
            continue
        result = json.loads((child / "results.json").read_text())[0]
        arm_config = json.loads((child / "config.json").read_text())
        primary = {}
        for event in result["events"]:
            if "artifact" not in event:
                continue
            raw = child / "raw" / event["artifact"]
            if hashlib.sha256(raw.read_bytes()).hexdigest() != event["sha256"]:
                raise ValueError("Retained response hash mismatch")
            primary.update(primary_records(json.loads(raw.read_text())))
        usable = sorted(
            identifier
            for identifier, p in primary.items()
            if in_window(p["created_at"], arm_config["since"], arm_config["until"])
        )
        returned = set(result["usable_unique_ids"])
        rows.append(
            {
                "case": child.name,
                "arm": result["arm"],
                "observer_id": result["observer_id"],
                "query": result["query"],
                "stop_reason": result["stop_reason"],
                "raw_primary_count": len(primary),
                "primary_in_window_count": len(usable),
                "primary_in_window_ids": usable,
                "sdk_in_window_count": len(returned),
                "sdk_nonprimary_in_window_ids": sorted(returned - set(usable)),
                "primary_not_returned_by_sdk": sorted(set(usable) - returned),
            }
        )
    return config, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config, rows = normalize(args.input)
    config = {
        "source": str(Path(args.input).resolve()),
        "mode": "offline_primary_unit_normalization",
        "rule": "timeline itemContent tweet_results only; exclude nested quoted/reposted objects",
        "original_config": config,
    }
    write_results(args.output, config, rows)
    print(
        json.dumps(
            [
                {
                    k: row[k]
                    for k in (
                        "case",
                        "primary_in_window_count",
                        "sdk_in_window_count",
                        "sdk_nonprimary_in_window_ids",
                    )
                }
                for row in rows
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
