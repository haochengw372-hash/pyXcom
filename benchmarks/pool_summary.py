"""Read retained canonical records and table counts from a finished pool run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from common import write_results


def retained_records(output):
    ids, counts = {}, {}
    for directory in sorted(Path(output).glob("job_*")):
        key = directory.name.removeprefix("job_")
        canonical = directory / ".pyxcom" / "posts.jsonl"
        if canonical.exists():
            ids[key] = sorted(
                {
                    str(json.loads(line)["id"])
                    for line in canonical.read_text().split("\n")
                    if line.strip()
                }
            )
        counts[key] = {}
        for table in sorted(directory.glob("*.csv")):
            with table.open(encoding="utf-8-sig", newline="") as stream:
                counts[key][table.name] = sum(1 for _ in csv.DictReader(stream))
    return ids, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.input).resolve()
    original_hashes = json.loads((source / "sha256.json").read_text())
    for name, expected in original_hashes.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Original evidence hash mismatch")
    rows = []
    for result in json.loads((source / "results.json").read_text()):
        directory = source / f"round_{result['round']}_{result['arm']}"
        ids, counts = retained_records(directory)
        rows.append(
            {
                **result,
                "canonical_ids_by_job": ids,
                "canonical_counts_by_job": {k: len(v) for k, v in ids.items()},
                "table_counts_by_job": counts,
                "original_ids_by_job_unit": "posts.csv main-post rows only",
                "canonical_unit": "retained primary search records including replies",
            }
        )
    write_results(
        args.output,
        {
            "mode": "offline_pool_canonical_summary",
            "original_evidence": str(source),
            "original_sha256_inventory": original_hashes,
        },
        rows,
    )
    print(
        json.dumps(
            [
                {
                    k: row[k]
                    for k in (
                        "arm",
                        "round",
                        "canonical_counts_by_job",
                        "table_counts_by_job",
                    )
                }
                for row in rows
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
