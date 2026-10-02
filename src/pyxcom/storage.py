"""Resumable public-post exports with auditable state and hashes."""

import csv
import gzip
import hashlib
import json
import os
import uuid
from dataclasses import fields
from pathlib import Path

from ._persistence import append_jsonl, atomic_json as _atomic_json, read_jsonl
from .layout import internal_dir, migrate_collection
from .models import CollectionResult, Post
from .transport import now_utc
from .tables import export_tables


class PostStore:
    def __init__(self, output_dir: str | Path, *, query: dict) -> None:
        self.output_dir = Path(output_dir).expanduser()
        if (self.output_dir / "network_manifest.json").exists():
            raise ValueError(
                "Network snapshots require a separate output directory from post collections"
            )
        migrate_collection(self.output_dir)
        self._internal = internal_dir(self.output_dir)
        self._jsonl = self._internal / "posts.jsonl"
        self._state_path = self._internal / "state.json"
        if self._state_path.exists():
            self.state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if self.state.get("query") != query:
                raise ValueError("Output directory belongs to a different query")
        else:
            self.state = {
                "query": query,
                "cursor": None,
                "pages_fetched": 0,
                "complete": False,
                "reason": "not_started",
                "missing_ids": [],
            }
        self._posts: dict[str, Post] = {}
        if self._jsonl.exists():
            for record in read_jsonl(self._jsonl):
                post = Post(**record)
                self._posts[post.id] = post
        self._metrics_path = self._internal / "metric_snapshots.jsonl"
        self._metrics = {}
        if self._metrics_path.exists():
            for row in read_jsonl(self._metrics_path):
                self._metrics[row["snapshot_id"]] = row

    def archive_response(
        self, payload: dict, *, operation: str, variables: dict
    ) -> Path:
        """Keep the complete read response; request credentials are never archived."""
        raw_dir = self._internal / "raw"
        raw_dir.mkdir(exist_ok=True)
        path = raw_dir / (uuid.uuid4().hex + ".json")
        _atomic_json(path, payload)
        safe = {
            k: v
            for k, v in variables.items()
            if k.lower()
            not in {
                "auth_token",
                "ct0",
                "cookies",
                "authorization",
                "password",
                "secret",
                "bearer_token",
            }
        }
        self.log(
            operation=operation,
            variables=safe,
            raw_path=str(path.relative_to(self.output_dir)),
            raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            status="response_received",
        )
        return path

    def log(self, **record) -> None:
        append_jsonl(
            self._internal / "collection_log.jsonl",
            [{"timestamp": now_utc(), **record}],
        )

    def _snapshot(self, post: Post) -> None:
        row = {
            "post_id": post.id,
            "retrieved_at_utc": post.captured_at_utc,
            "time_status": "observed" if post.captured_at_utc else "unknown",
        }
        for field in (
            "like_count",
            "reply_count",
            "repost_count",
            "quote_count",
            "view_count",
            "bookmark_count",
        ):
            row[field] = getattr(post, field)
        if all(
            row[field] is None
            for field in (
                "like_count",
                "reply_count",
                "repost_count",
                "quote_count",
                "view_count",
                "bookmark_count",
            )
        ):
            return
        key = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if key not in self._metrics:
            row["snapshot_id"] = key
            append_jsonl(self._metrics_path, [row])
            self._metrics[key] = row

    @property
    def count(self) -> int:
        return len(self._posts)

    @property
    def cursor(self) -> str | None:
        return self.state["cursor"]

    @property
    def complete(self) -> bool:
        return self.state["complete"]

    def append_page(
        self, posts: list[Post], next_cursor: str | None, *, count_page: bool = True
    ) -> int:
        incoming: dict[str, Post] = {}
        for post in posts:
            self._snapshot(post)
            if post.id not in self._posts and post.id not in incoming:
                incoming[post.id] = post
        append_jsonl(
            self._internal / "observations.jsonl", (post.to_dict() for post in posts)
        )
        append_jsonl(
            self._jsonl,
            (post.to_dict() for post in incoming.values()),
            ensure_ascii=True,
        )
        self._posts.update(incoming)
        added = len(incoming)
        self.state.update(
            cursor=next_cursor,
            pages_fetched=self.state["pages_fetched"] + int(count_page),
            reason="in_progress",
        )
        _atomic_json(self._state_path, self.state)
        return added

    def retain_role(self, role: str) -> None:
        """Normalize a source timeline while preserving its original observations."""
        kept = {
            key: post for key, post in self._posts.items() if post.post_role == role
        }
        if len(kept) == len(self._posts):
            return
        backup = self._internal / "posts.before-timeline-filter.jsonl.gz"
        if not backup.exists():
            with gzip.open(backup, "wb") as stream:
                stream.write(self._jsonl.read_bytes())
        temporary = self._jsonl.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for post in kept.values():
                stream.write(json.dumps(post.to_dict(), ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._jsonl)
        self._posts = kept

    def add_missing_ids(self, ids: set[str]) -> None:
        self.state["missing_ids"] = sorted(
            set(self.state.get("missing_ids", [])) | ids,
            key=int,
        )
        _atomic_json(self._state_path, self.state)

    def finish(self, *, complete: bool, reason: str) -> CollectionResult:
        self.state.update(complete=complete, reason=reason, updated_at_utc=now_utc())
        _atomic_json(self._state_path, self.state)
        self._jsonl.touch(exist_ok=True)
        csv_path = self._internal / "posts.csv"
        rows = sorted(
            self._posts.values(), key=lambda post: (post.created_at_utc, post.id)
        )
        names = [item.name for item in fields(Post)]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=names)
            writer.writeheader()
            for post in rows:
                row = post.to_dict()
                for key, value in list(row.items()):
                    if isinstance(value, (dict, list)):
                        row[key] = json.dumps(value, ensure_ascii=False)
                writer.writerow(row)
        from .tables import _write_csv

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
        if self._metrics:
            _write_csv(
                self.output_dir / "metric_snapshots.csv",
                metric_fields,
                list(self._metrics.values()),
            )
        self.log(
            operation="finish",
            query=self.state["query"],
            complete=complete,
            reason=reason,
            pages_fetched=self.state["pages_fetched"],
        )
        manifest = {
            **(
                {"date_scope": self.state["date_scope"]}
                if "date_scope" in self.state
                else {}
            ),
            **(
                {"discovery_pagination": self.state["discovery_pagination"]}
                if "discovery_pagination" in self.state
                else {}
            ),
            **(
                {"source_content": self.state["source_content"]}
                if "source_content" in self.state
                else {}
            ),
            "query": self.state["query"],
            "post_count": len(rows),
            "pages_fetched": self.state["pages_fetched"],
            "complete": complete,
            "reason": reason,
            "missing_ids": self.state.get("missing_ids", []),
            "oldest_post_utc": rows[0].created_at_utc if rows else None,
            "newest_post_utc": rows[-1].created_at_utc if rows else None,
            "captured_at_utc": now_utc(),
            "sha256": {
                "posts.jsonl": hashlib.sha256(self._jsonl.read_bytes()).hexdigest(),
                "posts.csv": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            },
        }
        _atomic_json(self._internal / "manifest.json", manifest)
        export_tables(self.output_dir)
        return CollectionResult(
            output_dir=self.output_dir,
            post_count=len(rows),
            pages_fetched=self.state["pages_fetched"],
            complete=complete,
            reason=reason,
        )
