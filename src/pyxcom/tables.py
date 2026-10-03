"""Relational views of saved observations; never infer missing conversations."""

import csv
import hashlib
import json
from dataclasses import fields
from pathlib import Path

from ._persistence import atomic_json, atomic_text, read_jsonl
from .layout import migrate_collection, source_path
from .models import Post, Profile
from .profiles import SNAPSHOT_FIELDS, profile_views

_CONTENT = [
    item.name
    for item in fields(Post)
    if item.name
    not in {
        "id",
        "author_id",
        "author_handle",
        "post_role",
        "post_type",
        "conversation_id",
        "in_reply_to_id",
        "reposted_post_id",
    }
]
_USER = (
    ["user_id", "username", "display_name"]
    + [
        item.name
        for item in fields(Profile)
        if item.name not in {"id", "handle", "name"}
    ]
    + ["profile_available"]
)
_POST = ["post_id", "author_id", "post_type"] + _CONTENT
_COMMENT = [
    "comment_id",
    "author_id",
    "root_post_id",
    "parent_post_id",
    "depth",
    "depth_status",
    "parent_in_dataset",
    "root_in_dataset",
    "seed_post_id",
    "seed_relative_depth",
] + _CONTENT
_CONTEXT = [
    "post_id",
    "author_id",
    "post_role",
    "post_type",
    "root_post_id",
    "parent_post_id",
    "depth",
    "depth_status",
    "target_post_id",
] + _CONTENT
_INTERACTION = [
    "interaction_id",
    "author_id",
    "interaction_type",
    "target_post_id",
] + _CONTENT
_EDGE = [
    "source_post_id",
    "target_post_id",
    "source_user_id",
    "target_user_id",
    "edge_type",
    "action_time_utc",
    "observed_at_utc",
    "target_post_available",
    "target_author_resolved",
    "observation_role",
]


def _post_edges(records: dict[str, Post]) -> list[dict]:
    edges = []
    for post in records.values():
        if post.observation_role == "context":
            continue
        relations: tuple[tuple[str, str | None, str | None], ...] = (
            ("reply", post.in_reply_to_id, post.in_reply_to_user_id),
            ("quote", post.quoted_post_id, post.quoted_author_id),
            ("repost", post.reposted_post_id, post.reposted_author_id),
        )
        if post.post_role == "repost":
            relations = (relations[-1],)
        for kind, target_id, target_author in relations:
            if not target_id:
                continue
            target = records.get(target_id)
            target_author = target.author_id if target else target_author
            edges.append(
                dict(
                    source_post_id=post.id,
                    target_post_id=target_id,
                    source_user_id=post.author_id,
                    target_user_id=target_author,
                    edge_type=kind,
                    action_time_utc=post.created_at_utc,
                    observed_at_utc=post.captured_at_utc,
                    target_post_available=target is not None,
                    target_author_resolved=bool(target_author),
                    observation_role=post.observation_role,
                )
            )
    return edges


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with atomic_text(path, encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )


def _depths(records: dict[str, Post]) -> dict[str, tuple[int | None, str]]:
    """Iterative traversal avoids stack limits, including very long reply threads."""
    resolved: dict[str, tuple[int | None, str]] = {}
    for origin in records.values():
        if origin.post_role != "comment" or origin.id in resolved:
            continue
        trail: list[str] = []
        seen: set[str] = set()
        current = origin
        root = origin.conversation_id
        while True:
            if current.id in seen:
                raise ValueError(f"Reply ancestry cycle at post {current.id}")
            if current.id in resolved:
                base, status = resolved[current.id]
                break
            seen.add(current.id)
            trail.append(current.id)
            if not root:
                base, status = None, "missing_root_id"
                break
            if current.conversation_id != root:
                base, status = None, "inconsistent_root"
                break
            parent = current.in_reply_to_id
            if parent == current.id:
                raise ValueError(f"Reply ancestry cycle at post {current.id}")
            if parent == root:
                if parent in records and records[parent].post_role != "main":
                    raise ValueError(f"Conversation root {root} is not a main post")
                base, status = 0, "known"
                break
            if parent not in records:
                base, status = None, "missing_parent"
                break
            ancestor = records[parent]
            if ancestor.post_role != "comment":
                base, status = None, "inconsistent_root"
                break
            if ancestor.conversation_id != root:
                base, status = None, "inconsistent_root"
                break
            current = ancestor
        for post_id in reversed(trail):
            base = base + 1 if base is not None else None
            resolved[post_id] = (base, status)
    return resolved


def export_tables(output_dir: str | Path) -> dict:
    """Rebuild public tables only when saved sources pass integrity assessment."""
    directory = Path(output_dir).expanduser()
    # Existing modern public manifests are integrity anchors. Fresh/legacy
    # inputs retain their original migration path and remain explicitly unanchored.
    manifest_path = directory / "manifest.json"
    if manifest_path.exists() and (directory / ".pyxcom" / "posts.jsonl").exists():
        from .errors import IntegrityError
        from .integrity import assess_recovery

        report = assess_recovery(directory)
        if not report["allowed"]:
            raise IntegrityError(
                "Source integrity blocks re-export: " + "; ".join(report["errors"])
            )
    return _export_tables_unchecked(directory)


def _export_tables_unchecked(output_dir: str | Path) -> dict:
    """Write public relational CSV tables at the collection root."""
    directory = Path(output_dir).expanduser()
    if (directory / "network_manifest.json").exists():
        raise ValueError(
            "Post table export cannot overwrite a network snapshot dataset"
        )
    migrate_collection(directory)
    source = source_path(directory, "posts.jsonl")
    records: dict[str, Post] = {}
    for data in read_jsonl(source):
        for key in (
            "id",
            "author_id",
            "conversation_id",
            "in_reply_to_id",
            "quoted_post_id",
            "reposted_post_id",
        ):
            if data.get(key) is not None:
                data[key] = str(data[key])
        post = Post(**data)
        if not post.id or not post.author_id:
            raise ValueError("Post and author IDs must be nonempty")
        if post.id in records and records[post.id] != post:
            raise ValueError(f"Conflicting duplicate post ID: {post.id}")
        records[post.id] = post
    depths = _depths(records)
    profiles_path = source_path(directory, "profiles.json")
    profiles, profile_snapshots, profile_report = profile_views(directory)
    users: dict[str, dict] = {
        user_id: {
            "user_id": user_id,
            "username": profile.get("handle"),
            "display_name": profile.get("name"),
            **{key: profile.get(key) for key in _USER[3:-1]},
            "profile_available": True,
        }
        for user_id, profile in profiles.items()
    }
    posts, comments, interactions, contexts = [], [], [], []
    state_file = source_path(directory, "state.json")
    collection_state = json.loads(state_file.read_text()) if state_file.exists() else {}
    seed_id = (
        collection_state.get("query", {}).get("root_post_id")
        if collection_state.get("query", {}).get("kind") == "post_comments"
        else None
    )
    seed_depths = collection_state.get("conversation", {}).get("depths", {})
    main_ids = {p.id for p in records.values() if p.post_role == "main"}
    content_ids = {p.id for p in records.values() if p.post_role != "repost"}
    for post in sorted(records.values(), key=lambda p: (p.created_at_utc, p.id)):
        profile = profiles.get(post.author_id)
        users[post.author_id] = {
            "user_id": post.author_id,
            "username": profile.get("handle") if profile else post.author_handle,
            "display_name": profile.get("name") if profile else None,
            **{key: profile.get(key) if profile else None for key in _USER[3:-1]},
            "profile_available": profile is not None,
        }
        data = post.to_dict()
        payload = {key: data[key] for key in _CONTENT}
        if post.observation_role == "context":
            depth, status = depths.get(post.id, (None, "not_reply"))
            contexts.append(
                {
                    "post_id": post.id,
                    "author_id": post.author_id,
                    "post_role": post.post_role,
                    "post_type": post.post_type,
                    "root_post_id": post.conversation_id,
                    "parent_post_id": post.in_reply_to_id,
                    "depth": depth,
                    "depth_status": status,
                    "target_post_id": post.reposted_post_id,
                    **payload,
                }
            )
            continue
        if post.post_role == "comment":
            depth, status = depths[post.id]
            comments.append(
                {
                    "comment_id": post.id,
                    "author_id": post.author_id,
                    "root_post_id": post.conversation_id,
                    "parent_post_id": post.in_reply_to_id,
                    "depth": depth,
                    "depth_status": status,
                    "parent_in_dataset": post.in_reply_to_id in content_ids,
                    "root_in_dataset": post.conversation_id in main_ids,
                    "seed_post_id": seed_id,
                    "seed_relative_depth": seed_depths.get(post.id)
                    if seed_id
                    else None,
                    **payload,
                }
            )
        elif post.post_role == "repost":
            interactions.append(
                {
                    "interaction_id": post.id,
                    "author_id": post.author_id,
                    "interaction_type": "repost",
                    "target_post_id": post.reposted_post_id,
                    **payload,
                }
            )
        else:
            posts.append(
                {
                    "post_id": post.id,
                    "author_id": post.author_id,
                    "post_type": post.post_type,
                    **payload,
                }
            )
    target = directory
    target.mkdir(exist_ok=True)
    edges = _post_edges(records)
    for edge in edges:
        user_id = edge["target_user_id"]
        if user_id and user_id not in users:
            users[user_id] = {
                **dict.fromkeys(_USER),
                "user_id": user_id,
                "profile_available": False,
            }
    tables = {
        "users.csv": (_USER, list(users.values())),
        "posts.csv": (_POST, posts),
        "comments.csv": (_COMMENT, comments),
    }
    if profile_snapshots:
        tables["profile_snapshots.csv"] = (SNAPSHOT_FIELDS, profile_snapshots)
    else:
        (target / "profile_snapshots.csv").unlink(missing_ok=True)
    if interactions:
        tables["interactions.csv"] = (_INTERACTION, interactions)
    if contexts:
        tables["context_posts.csv"] = (_CONTEXT, contexts)
    if edges:
        tables["post_edges.csv"] = (_EDGE, edges)
    metric_source = source_path(directory, "metric_snapshots.jsonl")
    if metric_source.exists():
        metrics = list(read_jsonl(metric_source))
        if metrics:
            metric_fields = [
                "snapshot_id",
                "post_id",
                "retrieved_at_utc",
                "time_status",
                "like_count",
                "reply_count",
                "repost_count",
                "quote_count",
                "view_count",
                "bookmark_count",
            ]
            tables["metric_snapshots.csv"] = (metric_fields, metrics)
    for name, (columns, rows) in tables.items():
        _write_csv(target / name, columns, rows)
    if not interactions:
        (target / "interactions.csv").unlink(missing_ok=True)
    if not contexts:
        (target / "context_posts.csv").unlink(missing_ok=True)
    if not edges:
        (target / "post_edges.csv").unlink(missing_ok=True)
    manifest: dict = {
        "schema_version": "1.2",
        "layout_version": "2.0",
        "source_kind": "saved_post_observations",
        "source_sha256": {
            str(source.relative_to(directory)): _hash(source),
            **(
                {str(state_file.relative_to(directory)): _hash(state_file)}
                if state_file.exists()
                else {}
            ),
            **(
                {str(metric_source.relative_to(directory)): _hash(metric_source)}
                if metric_source.exists()
                else {}
            ),
            **(
                {str(profiles_path.relative_to(directory)): _hash(profiles_path)}
                if profiles_path.exists()
                else {}
            ),
            **(
                {
                    str(
                        source_path(
                            directory, "profile_observations.jsonl"
                        ).relative_to(directory)
                    ): _hash(source_path(directory, "profile_observations.jsonl"))
                }
                if source_path(directory, "profile_observations.jsonl").exists()
                else {}
            ),
        },
        "counts": {
            "users": len(users),
            "posts": len(posts),
            "comments": len(comments),
            "interactions": len(interactions),
            "source_records": len(records),
            **({"context_posts": len(contexts)} if contexts else {}),
        },
        "missing_root_count": sum(not row["root_in_dataset"] for row in comments),
        "missing_parent_count": sum(not row["parent_in_dataset"] for row in comments),
        "unknown_depth_count": sum(row["depth"] is None for row in comments),
        "conversation_coverage": "observed_records_only",
        "canonical_record_policy": "first_saved_observation_per_id",
        "metric_policy": "returned_counts_at_observation_time",
        "profile_observations": profile_report,
        "edge_count": len(edges),
        "sha256": {name: _hash(target / name) for name in tables},
    }
    collection_path = source_path(directory, "manifest.json")
    if collection_path.exists() and collection_path != target / "manifest.json":
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        for key in (
            "query",
            "pages_fetched",
            "complete",
            "reason",
            "captured_at_utc",
            "oldest_post_utc",
            "newest_post_utc",
            "missing_ids",
            "date_scope",
            "discovery_pagination",
            "source_content",
        ):
            if key in collection:
                manifest[key] = collection[key]
    state_path = source_path(directory, "state.json")
    if manifest.get("query", {}).get("kind") == "post_comments" and state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8")).get(
            "conversation", {}
        )
        manifest["comment_collection"] = {
            "max_depth": manifest["query"]["max_depth"],
            "pending_requests": len(state.get("queue", [])),
            "unresolved_or_deferred_records": len(state.get("pending", {})),
            "pagination_warnings": sorted(set(state.get("pagination_warnings", []))),
            "depth_counts": {
                str(depth): sum(row["depth"] == depth for row in comments)
                for depth in sorted(
                    {row["depth"] for row in comments if row["depth"] is not None}
                )
            },
        }
    atomic_json(target / "manifest.json", manifest)
    return manifest
