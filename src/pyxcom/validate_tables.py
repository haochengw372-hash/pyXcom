"""Offline integrity checks for relational exports and their source snapshots."""

import csv
import hashlib
import json
from pathlib import Path


def validate_tables(output_dir: str | Path) -> dict:
    output = Path(output_dir).expanduser()
    tables = output if (output / "comments.csv").exists() else output / "tables"
    errors: list[str] = []
    try:
        manifest = json.loads((tables / "manifest.json").read_text(encoding="utf-8"))
        counts = manifest["counts"]
        records: dict[str, list[dict]] = {}
        for name in ("users", "posts", "comments", "interactions"):
            path = tables / f"{name}.csv"
            if name == "interactions" and not counts[name] and not path.exists():
                records[name] = []
                continue
            with path.open(encoding="utf-8-sig", newline="") as file:
                records[name] = list(csv.DictReader(file))
            if len(records[name]) != counts[name]:
                errors.append(f"row_count_mismatch:{name}")
        records["context_posts"] = []
        context_path = tables / "context_posts.csv"
        if context_path.exists():
            with context_path.open(encoding="utf-8-sig", newline="") as file:
                records["context_posts"] = list(csv.DictReader(file))
        if len(records["context_posts"]) != counts.get("context_posts", 0):
            errors.append("row_count_mismatch:context_posts")
        for base, key in ((tables, "sha256"), (output, "source_sha256")):
            for name, digest in manifest[key].items():
                if hashlib.sha256((base / name).read_bytes()).hexdigest() != digest:
                    errors.append(f"hash_mismatch:{key}:{name}")
        identifiers: dict[str, set[str]] = {}
        for name, key in (
            ("users", "user_id"),
            ("posts", "post_id"),
            ("comments", "comment_id"),
            ("interactions", "interaction_id"),
            ("context_posts", "post_id"),
        ):
            ids = {row[key] for row in records[name]}
            identifiers[name] = ids
            if "" in ids or len(ids) != len(records[name]):
                errors.append(f"invalid_or_duplicate_id:{name}")
        context_ids = identifiers["context_posts"]
        content_ids = identifiers["posts"] | identifiers["comments"] | context_ids
        main_ids = identifiers["posts"] | {
            r["post_id"] for r in records["context_posts"] if r["post_role"] == "main"
        }
        if identifiers["posts"] & identifiers["comments"]:
            errors.append("post_comment_overlap")
        if identifiers["interactions"] & content_ids:
            errors.append("interaction_content_overlap")
        if (
            sum(
                len(records[k])
                for k in ("posts", "comments", "interactions", "context_posts")
            )
            != counts["source_records"]
        ):
            errors.append("source_partition_mismatch")
        for name in ("posts", "comments", "interactions", "context_posts"):
            if any(
                row["author_id"] not in identifiers["users"] for row in records[name]
            ):
                errors.append(f"missing_user:{name}")
        comments = {row["comment_id"]: row for row in records["comments"]}
        ancestry = {
            **comments,
            **{
                r["post_id"]: r
                for r in records["context_posts"]
                if r["post_role"] == "comment"
            },
        }
        missing_roots = missing_parents = unknown_depth = 0
        for row in comments.values():
            parent, root = row["parent_post_id"], row["root_post_id"]
            parent_present, root_present = (
                parent in content_ids,
                root in main_ids,
            )
            missing_roots += not root_present
            missing_parents += not parent_present
            unknown_depth += not row["depth"]
            if (
                row["parent_in_dataset"].lower() != str(parent_present).lower()
                or row["root_in_dataset"].lower() != str(root_present).lower()
            ):
                errors.append(f"relationship_flag_mismatch:{row['comment_id']}")
            if row["depth"]:
                depth = int(row["depth"])
                if row["depth_status"] != "known" or depth < 1:
                    errors.append(f"invalid_depth:{row['comment_id']}")
                elif depth == 1 and parent != root:
                    errors.append(f"invalid_direct_reply:{row['comment_id']}")
                elif depth > 1:
                    ancestor = ancestry.get(parent)
                    if (
                        not ancestor
                        or not ancestor["depth"]
                        or int(ancestor["depth"]) != depth - 1
                        or ancestor["root_post_id"] != root
                    ):
                        errors.append(f"invalid_ancestry:{row['comment_id']}")
            elif row["depth_status"] == "known":
                errors.append(f"missing_known_depth:{row['comment_id']}")
        for key, actual in (
            ("missing_root_count", missing_roots),
            ("missing_parent_count", missing_parents),
            ("unknown_depth_count", unknown_depth),
        ):
            if manifest[key] != actual:
                errors.append(f"coverage_mismatch:{key}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"invalid_tables:{exc}")
    return {"valid": not errors, "errors": errors}
