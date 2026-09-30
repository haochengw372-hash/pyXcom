"""Relational views of saved observations; never infer missing conversations."""

import csv
import hashlib
import json
import os
from dataclasses import fields
from pathlib import Path
from tempfile import NamedTemporaryFile

from .layout import migrate_collection, source_path
from .models import Post, Profile

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
] + _CONTENT
_INTERACTION = [
    "interaction_id",
    "author_id",
    "interaction_type",
    "target_post_id",
] + _CONTENT


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
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
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


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
    """Write public relational CSV tables at the collection root."""
    directory = Path(output_dir).expanduser()
    migrate_collection(directory)
    source = source_path(directory, "posts.jsonl")
    records: dict[str, Post] = {}
    for line in source.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        data = json.loads(line)
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
    profiles: dict[str, dict] = {}
    if profiles_path.exists():
        raw = json.loads(profiles_path.read_text(encoding="utf-8"))
        entries = (
            raw if isinstance(raw, list) else ([raw] if "id" in raw else raw.values())
        )
        for entry in entries:
            user_id = str(entry["id"])
            if user_id in profiles and profiles[user_id] != entry:
                raise ValueError(f"Conflicting duplicate profile ID: {user_id}")
            profiles[user_id] = entry
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
    posts, comments, interactions = [], [], []
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
    tables = {
        "users.csv": (_USER, list(users.values())),
        "posts.csv": (_POST, posts),
        "comments.csv": (_COMMENT, comments),
    }
    if interactions:
        tables["interactions.csv"] = (_INTERACTION, interactions)
    for name, (columns, rows) in tables.items():
        _write_csv(target / name, columns, rows)
    if not interactions:
        (target / "interactions.csv").unlink(missing_ok=True)
    manifest = {
        "schema_version": "1.1",
        "layout_version": "2.0",
        "source_kind": "saved_post_observations",
        "source_sha256": {
            str(source.relative_to(directory)): _hash(source),
            **(
                {str(profiles_path.relative_to(directory)): _hash(profiles_path)}
                if profiles_path.exists()
                else {}
            ),
        },
        "counts": {
            "users": len(users),
            "posts": len(posts),
            "comments": len(comments),
            "interactions": len(interactions),
            "source_records": len(records),
        },
        "missing_root_count": sum(not row["root_in_dataset"] for row in comments),
        "missing_parent_count": sum(not row["parent_in_dataset"] for row in comments),
        "unknown_depth_count": sum(row["depth"] is None for row in comments),
        "conversation_coverage": "observed_records_only",
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
        ):
            if key in collection:
                manifest[key] = collection[key]
    with NamedTemporaryFile("w", encoding="utf-8", dir=target, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target / "manifest.json")
    return manifest
