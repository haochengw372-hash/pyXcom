"""Visible follower and reposter lists, with resumable observation snapshots.

A reposter lookup identifies current visible reposters, never when they reposted.
A follow edge is observed at collection time, not at relationship creation time.
"""

import csv
import json
import re
import time
from dataclasses import fields
from pathlib import Path

from ._persistence import append_jsonl
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from .errors import APIError, ParseError, RateLimitError
from .models import CollectionResult, Post, Profile, Relationship
from .parse import _walk, parse_profile
from .storage import _atomic_json
from .tables import _USER, _hash, _write_csv
from .transport import now_utc


def parse_network_page(
    data: dict, *, captured_at_utc: str
) -> tuple[list[Profile], str | None]:
    """Read only primary timeline users; exclude embedded posts/recommendations."""
    if not isinstance(data, dict):
        raise ParseError("X network response is not an object")
    body = data.get("data", {})
    instructions = next(
        (
            node["instructions"]
            for node in _walk(body)
            if isinstance(node.get("instructions"), list)
        ),
        None,
    )
    users: dict[str, Profile] = {}
    cursors: list[str] = []

    def accept(result):
        if not isinstance(result, dict):
            raise ParseError("X network user result is malformed")
        try:
            profile = parse_profile(result, captured_at_utc=captured_at_utc)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ParseError("X network user fields are malformed") from exc
        users[profile.id] = profile

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if node.get("promotedMetadata"):
                return
            if "user_results" in node:
                accept((node.get("user_results") or {}).get("result"))
                return
            if node.get("cursorType") == "Bottom" and node.get("value"):
                cursors.append(str(node["value"]))
            # These are structural containers, not arbitrary embedded account data.
            for key in (
                "entries",
                "entry",
                "content",
                "items",
                "item",
                "itemContent",
                "moduleItems",
            ):
                if key in node:
                    visit(node[key])

    if instructions is not None:
        for instruction in instructions:
            if not isinstance(instruction, dict):
                raise ParseError("X network timeline instruction is malformed")
            if "entries" in instruction and not isinstance(
                instruction["entries"], list
            ):
                raise ParseError("X network timeline entries are malformed")
            if not any(
                key in instruction for key in ("entries", "entry", "moduleItems")
            ) and instruction.get("type") not in {
                "TimelineTerminateTimeline",
                "TimelineClearCache",
                "TimelineShowAlert",
            }:
                raise ParseError(
                    "X network timeline instruction has no recognized entries"
                )
        visit(instructions)
        if not any(
            any(key in item for key in ("entries", "entry", "moduleItems"))
            or (
                item.get("type") == "TimelineTerminateTimeline"
                and item.get("direction") == "Bottom"
            )
            for item in instructions
        ):
            raise ParseError("X network response has no list entries or explicit end")
        if (
            not users
            and not cursors
            and any(
                instruction.get("entries")
                or instruction.get("entry")
                or instruction.get("moduleItems")
                for instruction in instructions
            )
        ):
            raise ParseError("X network entries contain no recognized users or cursors")
    else:
        direct = body if isinstance(body, list) else None
        if isinstance(body, dict):
            for key in ("users", "reposters", "followers", "following"):
                if isinstance(body.get(key), list):
                    direct = body[key]
                    break
        if direct is None:
            raise ParseError("X network response has no recognized user list")
        for result in direct:
            accept(result.get("result", result) if isinstance(result, dict) else result)
        if isinstance(body, dict) and body.get("next_cursor"):
            cursors.append(str(body["next_cursor"]))
    if len(set(cursors)) > 1:
        raise ParseError("X network response contains multiple bottom cursors")
    return list(users.values()), cursors[0] if cursors else None


def _options(user_id: str, max_pages: int | None, limit: int | None) -> str:
    from .client import _page_options

    _page_options(max_pages, limit)
    user_id = str(user_id)
    if not re.fullmatch(r"\d+", user_id):
        raise ValueError(
            "user_id must be a numeric X user ID; resolve handles with get_user"
        )
    return user_id


class NetworkTraversal:
    """Pagination shared by list-returning and persistent APIs."""

    def __init__(
        self,
        client,
        kind,
        object_id,
        *,
        max_pages=None,
        limit=None,
        state=None,
        persist=None,
    ):
        self.client, self.kind, self.object_id = client, kind, object_id
        self.max_pages, self.limit = max_pages, limit
        self.state: dict[str, Any] = state if state is not None else {}
        defaults: dict[str, Any] = {
            "cursor": None,
            "records": {},
            "pending": [],
            "seen_cursors": [],
            "pages_fetched": 0,
            "exhausted": False,
            "complete": False,
            "reason": "not_started",
            "warnings": [],
        }
        for key, value in defaults.items():
            self.state.setdefault(key, value)
        self.persist = persist or (lambda payload=None: None)
        self.pages_fetched = 0

    def pages(self) -> Iterator[list[Profile]]:
        state = self.state
        if state["complete"]:
            return
        while True:
            room = None if self.limit is None else self.limit - len(state["records"])
            added: list[Profile] = []
            while state["pending"] and (room is None or len(added) < room):
                record = state["pending"].pop(0)
                if record["id"] not in state["records"]:
                    state["records"][record["id"]] = record
                    added.append(Profile(**record))
            if added:
                self.persist()
                yield added
            if state["exhausted"] and not state["pending"]:
                state["complete"] = not state["warnings"]
                state["reason"] = (
                    "visible_source_end" if state["complete"] else "partial_network"
                )
                self.persist()
                return
            if self.limit is not None and len(state["records"]) >= self.limit:
                state["reason"] = "user_limit"
                self.persist()
                return
            if self.max_pages is not None and self.pages_fetched >= self.max_pages:
                state["reason"] = "page_limit"
                self.persist()
                return
            cursor = state["cursor"]
            try:
                if self.kind == "reposters":
                    payload = self.client._x.reposters_page(
                        self.object_id, cursor=cursor
                    )
                else:
                    payload = self.client._x.relationship_page(
                        self.object_id, kind=self.kind, cursor=cursor
                    )
                observed = now_utc()
                # Archive even structurally invalid responses for diagnosis.
                self.persist(
                    {
                        "retrieved_at_utc": observed,
                        "response": payload,
                        "cursor": cursor,
                    }
                )
                profiles, next_cursor = parse_network_page(
                    payload, captured_at_utc=observed
                )
            except (APIError, ParseError) as exc:
                state["reason"] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitError)
                    else "parse_error"
                    if isinstance(exc, ParseError)
                    else "request_error"
                )
                state["error_type"] = type(exc).__name__
                if isinstance(exc, RateLimitError):
                    state["rate_reset_at"] = exc.reset_at
                self.persist()
                return
            self.pages_fetched += 1
            state["pages_fetched"] += 1
            state.pop("error_type", None)
            state.pop("rate_reset_at", None)
            state["seen_cursors"].append(cursor)
            state["cursor"] = next_cursor
            state["pending"] = [
                p.to_dict() for p in profiles if p.id not in state["records"]
            ]
            state["exhausted"] = next_cursor is None
            if next_cursor is not None and next_cursor in state["seen_cursors"]:
                state["warnings"].append("repeated_cursor")
                state["exhausted"] = True
            elif next_cursor is not None and not state["pending"]:
                state["warnings"].append("no_new_users")
                state["exhausted"] = True
            state["reason"] = "in_progress"
            self.persist()
            if not state["exhausted"] and self.client.delay:
                time.sleep(self.client.delay)


def _csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _merge_csv(
    path: Path, columns: list[str], rows: list[dict], keys: tuple[str, ...]
) -> None:
    old = _csv_rows(path)
    if old:
        columns = list(dict.fromkeys(columns + list(old[0])))
    records = {tuple(str(row.get(key) or "") for key in keys): row for row in old}
    for row in rows:
        key = tuple(str(row.get(name) or "") for name in keys)
        previous = records.get(key, {})
        records[key] = {
            **previous,
            **{name: value for name, value in row.items() if value is not None},
        }
    _write_csv(path, columns, list(records.values()))


class NetworkStore:
    def __init__(self, directory, *, kind, object_id, snapshot_id=None):
        self.output = Path(directory).expanduser()
        # Network snapshots are a separate dataset from post/comment exports.
        # Reject before creating directories or modifying any public tables.
        if any(
            (self.output / name).exists()
            for name in (
                ".pyxcom/state.json",
                "state.json",
                "posts.csv",
                "comments.csv",
                "interactions.csv",
            )
        ):
            raise ValueError(
                "Network output must be separate from a post/comment collection"
            )
        for manifest_file in (
            self.output / "manifest.json",
            self.output / ".pyxcom/manifest.json",
        ):
            if manifest_file.exists():
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                if (
                    manifest.get("source_kind") == "saved_post_observations"
                    or "post_count" in manifest
                    or {"posts", "comments"}.intersection(manifest.get("counts", {}))
                ):
                    raise ValueError(
                        "Network output must be separate from a post/comment collection"
                    )
        base = self.output / ".pyxcom" / "networks" / f"{kind}_{object_id}"
        if not base.resolve().is_relative_to(self.output.resolve()):
            raise ValueError("Network storage path must remain inside output_dir")
        # An explicit invalid snapshot must also fail before any writes.
        if snapshot_id is not None and (
            not isinstance(snapshot_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", snapshot_id)
        ):
            raise ValueError(
                "snapshot_id must contain 1-128 letters, digits, underscores or hyphens"
            )
        base.mkdir(parents=True, exist_ok=True)
        if snapshot_id is None:
            unfinished = []
            for path in base.glob("*/state.json"):
                if not path.resolve().is_relative_to(self.output.resolve()):
                    raise ValueError("Snapshot state must remain inside output_dir")
                value = json.loads(path.read_text(encoding="utf-8"))
                terminal_partial = (
                    value.get("exhausted")
                    and not value.get("pending")
                    and value.get("warnings")
                )
                if not value.get("complete") and not terminal_partial:
                    unfinished.append(
                        (value.get("updated_at_utc", ""), path.parent.name)
                    )
            snapshot_id = max(unfinished)[1] if unfinished else uuid4().hex
        if not isinstance(snapshot_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", snapshot_id
        ):
            raise ValueError(
                "snapshot_id must contain 1-128 letters, digits, underscores or hyphens"
            )
        self.scope = base / snapshot_id
        if not self.scope.resolve().is_relative_to(self.output.resolve()):
            raise ValueError("Snapshot storage path must remain inside output_dir")
        self.scope.mkdir(exist_ok=True)
        self.path = self.scope / "state.json"
        if not self.path.resolve().is_relative_to(self.output.resolve()):
            raise ValueError("Snapshot state must remain inside output_dir")
        self.state: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "query": {"kind": kind, "object_id": object_id},
                "snapshot_id": snapshot_id,
                "started_at_utc": now_utc(),
                "raw_pages": 0,
            }
        )

    def persist(self, payload=None):
        if payload is not None:
            self.state["end_at_utc"] = payload["retrieved_at_utc"]
            folder = self.scope / "raw"
            if not folder.resolve().is_relative_to(self.output.resolve()):
                raise ValueError("Raw page storage must remain inside output_dir")
            folder.mkdir(exist_ok=True)
            self.state["raw_pages"] += 1
            _atomic_json(folder / f"{self.state['raw_pages']:06d}.json", payload)
        self.state["updated_at_utc"] = now_utc()
        _atomic_json(self.path, self.state)

    def finish(self, *, target_author_id=None):
        state = self.state
        kind, focal = state["query"]["kind"], state["query"]["object_id"]
        edges, users, snapshots = [], [], []
        for record in state.get("records", {}).values():
            user = {
                "user_id": record["id"],
                "username": record["handle"],
                "display_name": record["name"],
                **{key: record.get(key) for key in _USER[3:-1]},
                "profile_available": True,
            }
            users.append(user)
            snapshots.append(
                {
                    **user,
                    "snapshot_id": state["snapshot_id"],
                    "observed_at_utc": record["captured_at_utc"],
                }
            )
            edge = Relationship(
                source_user_id=record["id"]
                if kind in {"followers", "reposters"}
                else focal,
                target_user_id=target_author_id
                if kind == "reposters"
                else focal
                if kind == "followers"
                else record["id"],
                relationship_type="repost" if kind == "reposters" else "follow",
                observed_at_utc=record["captured_at_utc"],
                snapshot_id=state["snapshot_id"],
                target_post_id=focal if kind == "reposters" else None,
                action_time_utc=None,
            )
            edges.append(edge.to_dict())
        listed_user_count = len(users)
        focal_user = target_author_id if kind == "reposters" else focal
        if focal_user and focal_user not in {user["user_id"] for user in users}:
            stub: dict[str, Any] = {key: None for key in _USER}
            stub.update(user_id=focal_user, profile_available=False)
            existing = {
                row["user_id"]: row for row in _csv_rows(self.output / "users.csv")
            }
            if focal_user not in existing:
                users.append(stub)
            snapshots.append(
                {
                    **stub,
                    "snapshot_id": state["snapshot_id"],
                    "observed_at_utc": state["started_at_utc"],
                }
            )
        edge_columns = [field.name for field in fields(Relationship)]
        edge_name = "reposters.csv" if kind == "reposters" else "follow_edges.csv"
        _merge_csv(
            self.output / edge_name,
            edge_columns,
            edges,
            ("snapshot_id", "source_user_id", "target_user_id", "target_post_id"),
        )
        _merge_csv(self.output / "users.csv", _USER, users, ("user_id",))
        _merge_csv(
            self.output / "user_snapshots.csv",
            _USER + ["snapshot_id", "observed_at_utc"],
            snapshots,
            ("snapshot_id", "user_id", "observed_at_utc"),
        )
        summary = {
            "started_at_utc": state["started_at_utc"],
            "end_at_utc": state.get("end_at_utc", state["started_at_utc"]),
            "query": state["query"],
            "snapshot_id": state["snapshot_id"],
            "user_count": listed_user_count,
            "edge_count": len(edges),
            "target_author_id": target_author_id,
            "state_path": str(self.path.relative_to(self.output)),
            "source_sha256": {
                str(path.relative_to(self.output)): _hash(
                    _network_source_path(
                        self.output, str(path.relative_to(self.output))
                    )
                )
                for path in [self.path, *sorted((self.scope / "raw").glob("*.json"))]
            },
            "pages_fetched": state.get("pages_fetched", 0),
            "complete": state.get("complete", False),
            "reason": state.get("reason", "not_started"),
            "coverage": "visible_source_only",
            "action_time_available": False,
            "updated_at_utc": state["updated_at_utc"],
        }
        manifest_path = self.output / "network_manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {"snapshots": {}}
        )
        manifest["snapshots"][f"{kind}_{focal}_{state['snapshot_id']}"] = summary
        public_names = (
            "follow_edges.csv",
            "reposters.csv",
            "users.csv",
            "user_snapshots.csv",
        )
        manifest["sha256"] = {
            name: _hash(self.output / name)
            for name in public_names
            if (self.output / name).exists()
        }
        manifest["counts"] = {
            name: len(_csv_rows(self.output / name))
            for name in public_names
            if (self.output / name).exists()
        }
        _atomic_json(manifest_path, manifest)
        append_jsonl(self.output / ".pyxcom" / "collection_log.jsonl", [summary])
        return CollectionResult(
            self.output,
            listed_user_count,
            state.get("pages_fetched", 0),
            state.get("complete", False),
            state.get("reason", "not_started"),
        )


def _network_source_path(output: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Snapshot source path must be relative without traversal")
    target = output / path
    if not target.resolve().is_relative_to(
        output.resolve()
    ) or not target.resolve().is_relative_to(
        (output / ".pyxcom" / "networks").resolve()
    ):
        raise ValueError("Snapshot source path must remain inside network storage")
    return target


def validate_network_collection(output_dir: str | Path) -> dict:
    """Verify network observations and hashes offline; never contact X or edit files."""
    output = Path(output_dir).expanduser()
    errors: list[str] = []
    manifest_path = output / "network_manifest.json"
    if not manifest_path.exists():
        return {"valid": False, "errors": ["Missing network_manifest.json"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("snapshots"), dict)
            or not manifest["snapshots"]
        ):
            raise ValueError("No network snapshots in manifest")
        for name, expected in manifest.get("sha256", {}).items():
            path = output / name
            if (
                name
                not in {
                    "follow_edges.csv",
                    "reposters.csv",
                    "users.csv",
                    "user_snapshots.csv",
                }
                or path.resolve().parent != output.resolve()
            ):
                errors.append(f"Invalid public table path: {name}")
            elif not path.is_file() or _hash(path) != expected:
                errors.append(f"Hash mismatch: {name}")
        table_rows = {
            name: _csv_rows(output / name)
            for name in (
                "follow_edges.csv",
                "reposters.csv",
                "users.csv",
                "user_snapshots.csv",
            )
        }
        for name, count in manifest.get("counts", {}).items():
            if len(table_rows.get(name, [])) != count:
                errors.append(f"Row count mismatch: {name}")
        if "users.csv" not in manifest.get(
            "sha256", {}
        ) or "user_snapshots.csv" not in manifest.get("sha256", {}):
            errors.append("Missing profile table hashes")
        if set(manifest.get("counts", {})) != set(manifest.get("sha256", {})):
            errors.append("Public table counts/hashes do not match")
        users = {row["user_id"] for row in table_rows["users.csv"]}
        if len(users) != len(table_rows["users.csv"]):
            errors.append("Duplicate user IDs")
        observed_keys = set()
        for name in ("follow_edges.csv", "reposters.csv"):
            for row in table_rows[name]:
                key = tuple(
                    row.get(field, "")
                    for field in (
                        "snapshot_id",
                        "source_user_id",
                        "target_user_id",
                        "relationship_type",
                        "target_post_id",
                    )
                )
                if key in observed_keys:
                    errors.append(f"Duplicate network edge: {key}")
                observed_keys.add(key)
                if any(
                    not re.fullmatch(r"[0-9]+", row.get(field, ""))
                    for field in ("source_user_id", "target_user_id")
                ):
                    errors.append(f"Invalid edge user IDs: {key}")
                if any(
                    row.get(field) not in users
                    for field in ("source_user_id", "target_user_id")
                ):
                    errors.append(f"Unresolved edge endpoint: {key}")
                if not row.get("observed_at_utc") or not row.get("snapshot_id"):
                    errors.append(f"Missing edge observation provenance: {key}")
                if row.get("action_time_utc"):
                    errors.append(f"List lookup must not infer an action time: {key}")
        expected_keys: set[tuple[str, ...]] = set()
        for label, summary in manifest["snapshots"].items():
            for relative, digest in summary.get("source_sha256", {}).items():
                try:
                    path = _network_source_path(output, relative)
                except ValueError:
                    errors.append(f"Invalid snapshot source path: {relative}")
                    continue
                if not path.is_file() or _hash(path) != digest:
                    errors.append(f"Snapshot hash mismatch: {relative}")
            try:
                state_path = _network_source_path(output, summary["state_path"])
            except ValueError:
                errors.append(f"Invalid snapshot state path: {label}")
                continue
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if str(state_path.relative_to(output)) not in summary.get(
                "source_sha256", {}
            ):
                errors.append(f"Missing snapshot state hash: {label}")
            raw_paths = {
                str(path.relative_to(output))
                for path in (state_path.parent / "raw").glob("*.json")
            }
            if not raw_paths.issubset(summary.get("source_sha256", {})) or len(
                raw_paths
            ) != state.get("raw_pages", 0):
                errors.append(f"Raw page provenance mismatch: {label}")
            if state.get("query") != summary.get("query") or state.get(
                "snapshot_id"
            ) != summary.get("snapshot_id"):
                errors.append(f"Snapshot identity mismatch: {label}")
            if any(
                state.get(field) != summary.get(field)
                for field in ("complete", "reason", "pages_fetched")
            ):
                errors.append(f"Snapshot progress mismatch: {label}")
            if summary.get("started_at_utc") != state.get(
                "started_at_utc"
            ) or summary.get("end_at_utc") != state.get(
                "end_at_utc", state.get("started_at_utc")
            ):
                errors.append(f"Snapshot observation interval mismatch: {label}")
            records = state.get("records", {})
            kind, focal = summary["query"]["kind"], summary["query"]["object_id"]
            author = summary.get("target_author_id")
            name = "reposters.csv" if kind == "reposters" else "follow_edges.csv"
            if name not in manifest.get("sha256", {}):
                errors.append(f"Missing edge table hash: {name}")
            expected = {
                (
                    user_id if kind != "following" else focal,
                    author
                    if kind == "reposters"
                    else focal
                    if kind == "followers"
                    else user_id,
                    focal if kind == "reposters" else "",
                )
                for user_id in records
            }
            expected_keys.update(
                (
                    summary["snapshot_id"],
                    source,
                    target,
                    "repost" if kind == "reposters" else "follow",
                    post,
                )
                for source, target, post in expected
            )
            actual = {
                (
                    row["source_user_id"],
                    row["target_user_id"],
                    row.get("target_post_id", ""),
                )
                for row in table_rows[name]
                if row.get("snapshot_id") == summary["snapshot_id"]
                and (
                    row.get("target_post_id") == focal
                    if kind == "reposters"
                    else row.get("target_user_id") == focal
                    if kind == "followers"
                    else row.get("source_user_id") == focal
                )
            }
            if (
                actual != expected
                or len(expected) != summary.get("edge_count")
                or len(records) != summary.get("user_count")
            ):
                errors.append(f"Snapshot edge direction/count mismatch: {label}")
            if any(
                row.get("relationship_type")
                != ("repost" if kind == "reposters" else "follow")
                for row in table_rows[name]
            ):
                errors.append(f"Wrong relationship type: {name}")
        known_snapshots = {
            summary["snapshot_id"] for summary in manifest["snapshots"].values()
        }
        if any(key[0] not in known_snapshots for key in observed_keys):
            errors.append("Edge references an unknown snapshot")
        if observed_keys != expected_keys:
            errors.append("Network edges differ from snapshot observations")
        snapshot_users = table_rows["user_snapshots.csv"]
        profile_keys = [
            (row.get("snapshot_id"), row.get("user_id"), row.get("observed_at_utc"))
            for row in snapshot_users
        ]
        if len(profile_keys) != len(set(profile_keys)):
            errors.append("Duplicate user profile snapshots")
        if any(
            snapshot not in known_snapshots or user not in users or not observed
            for snapshot, user, observed in profile_keys
        ):
            errors.append("Invalid user profile snapshot provenance")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(f"Malformed network collection: {type(exc).__name__}")
    return {"valid": not errors, "errors": errors, "output_dir": str(output)}


class NetworkMixin:
    """Current visible directed network lists. Completeness is source-relative."""

    if TYPE_CHECKING:

        def get_post(self, post_id_or_url: str) -> Post: ...

    def _iter_network(self, kind, object_id, *, max_pages=None, limit=None):
        walk = NetworkTraversal(self, kind, object_id, max_pages=max_pages, limit=limit)
        for page in walk.pages():
            yield from page
        self.last_network_collection = dict(walk.state)
        reason = walk.state["reason"]
        if reason == "rate_limited":
            raise RateLimitError(
                "X network collection was rate limited", walk.state.get("rate_reset_at")
            )
        if reason == "parse_error":
            raise ParseError("X network response could not be parsed")
        if reason == "request_error":
            raise APIError("X network request failed")
        if reason == "partial_network":
            raise APIError("X network pagination stalled; collection is partial")

    def iter_followers(
        self, user_id: str, *, max_pages: int | None = None, limit: int | None = None
    ) -> Iterator[Profile]:
        """Yield visible followers of a numeric user ID; edges point follower → user."""
        yield from self._iter_network(
            "followers",
            _options(user_id, max_pages, limit),
            max_pages=max_pages,
            limit=limit,
        )

    def get_followers(
        self, user_id: str, *, max_pages: int | None = None, limit: int | None = None
    ) -> list[Profile]:
        return list(self.iter_followers(user_id, max_pages=max_pages, limit=limit))

    def iter_following(
        self, user_id: str, *, max_pages: int | None = None, limit: int | None = None
    ) -> Iterator[Profile]:
        """Yield visible accounts followed by user; edges point user → followed account."""
        yield from self._iter_network(
            "following",
            _options(user_id, max_pages, limit),
            max_pages=max_pages,
            limit=limit,
        )

    def get_following(
        self, user_id: str, *, max_pages: int | None = None, limit: int | None = None
    ) -> list[Profile]:
        return list(self.iter_following(user_id, max_pages=max_pages, limit=limit))

    def iter_post_reposters(
        self,
        post_id_or_url: str,
        *,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Profile]:
        """Yield visible reposters; no repost action IDs or action times are implied."""
        from .client import _page_options, _post_id

        _page_options(max_pages, limit)
        yield from self._iter_network(
            "reposters", _post_id(post_id_or_url), max_pages=max_pages, limit=limit
        )

    def get_post_reposters(
        self,
        post_id_or_url: str,
        *,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Profile]:
        return list(
            self.iter_post_reposters(post_id_or_url, max_pages=max_pages, limit=limit)
        )

    def _save_network(
        self, kind, object_id, output_dir, *, max_pages, limit, snapshot_id
    ):
        store = NetworkStore(
            output_dir, kind=kind, object_id=object_id, snapshot_id=snapshot_id
        )
        target_author = store.state.get("target_author_id")
        if kind == "reposters" and target_author is None:
            try:
                target_author = self.get_post(object_id).author_id
                store.state["target_author_id"] = target_author
            except (APIError, ParseError) as exc:
                store.state.update(
                    complete=False,
                    reason="rate_limited"
                    if isinstance(exc, RateLimitError)
                    else "request_error",
                    error_type=type(exc).__name__,
                )
                store.persist()
                return store.finish()
        walk = NetworkTraversal(
            self,
            kind,
            object_id,
            max_pages=max_pages,
            limit=limit,
            state=store.state,
            persist=store.persist,
        )
        for _ in walk.pages():
            pass
        store.persist()
        self.last_network_collection = dict(store.state)
        return store.finish(target_author_id=target_author)

    def save_followers(
        self,
        user_id: str,
        output_dir: str | Path,
        *,
        max_pages: int | None = None,
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> CollectionResult:
        return self._save_network(
            "followers",
            _options(user_id, max_pages, limit),
            output_dir,
            max_pages=max_pages,
            limit=limit,
            snapshot_id=snapshot_id,
        )

    def save_following(
        self,
        user_id: str,
        output_dir: str | Path,
        *,
        max_pages: int | None = None,
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> CollectionResult:
        return self._save_network(
            "following",
            _options(user_id, max_pages, limit),
            output_dir,
            max_pages=max_pages,
            limit=limit,
            snapshot_id=snapshot_id,
        )

    def save_post_reposters(
        self,
        post_id_or_url: str,
        output_dir: str | Path,
        *,
        max_pages: int | None = None,
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> CollectionResult:
        from .client import _page_options, _post_id

        _page_options(max_pages, limit)
        return self._save_network(
            "reposters",
            _post_id(post_id_or_url),
            output_dir,
            max_pages=max_pages,
            limit=limit,
            snapshot_id=snapshot_id,
        )
