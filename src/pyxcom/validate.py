"""Validate pyXcom's saved public-data artifacts without fetching anything."""

import csv
import hashlib
import json
from pathlib import Path


def validate_collection(output_dir: str | Path) -> dict:
    output = Path(output_dir).expanduser()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    errors: list[str] = []
    for number, line in enumerate(
        (output / "posts.jsonl").read_text(encoding="utf-8").split("\n"), 1
    ):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            errors.append(f"invalid_jsonl_line:{number}")
    with (output / "posts.csv").open(encoding="utf-8-sig", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    if len(rows) != manifest["post_count"] or len(csv_rows) != len(rows):
        errors.append("row_count_mismatch")
    if len({row["id"] for row in rows}) != len(rows):
        errors.append("duplicate_post_ids")
    query = manifest.get("query", {})
    since, until = query.get("since"), query.get("until")
    if any(
        (since and row["created_at_utc"][:10] < since)
        or (until and row["created_at_utc"][:10] >= until)
        for row in rows
    ):
        errors.append("post_outside_date_range")
    for filename, digest in manifest.get("sha256", {}).items():
        if hashlib.sha256((output / filename).read_bytes()).hexdigest() != digest:
            errors.append(f"hash_mismatch:{filename}")
    profiles_path = output / "profiles.json"
    if profiles_path.exists():
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        user_ids = {profile["id"] for profile in profiles.values()}
        if any(row["author_id"] not in user_ids for row in rows):
            errors.append("unexpected_author_id")
        account_manifest = json.loads(
            (output / "account_manifest.json").read_text(encoding="utf-8")
        )
        if len(account_manifest["accounts"]) != len(profiles):
            errors.append("account_manifest_mismatch")
    return {
        "output_dir": str(output),
        "valid": not errors,
        "post_count": len(rows),
        "complete": manifest["complete"],
        "errors": errors,
    }
