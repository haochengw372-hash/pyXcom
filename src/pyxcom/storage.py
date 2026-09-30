"""Resumable public-post exports with auditable state and hashes."""

import csv
import gzip
import hashlib
import json
import os
from dataclasses import fields
from pathlib import Path

from .layout import internal_dir, migrate_collection
from .models import CollectionResult, Post
from .transport import now_utc
from .tables import export_tables


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


class PostStore:
    def __init__(self, output_dir: str | Path, *, query: dict) -> None:
        self.output_dir = Path(output_dir).expanduser()
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
            for line in self._jsonl.read_text(encoding="utf-8").split("\n"):
                if line.strip():
                    post = Post(**json.loads(line))
                    self._posts[post.id] = post

    @property
    def count(self) -> int:
        return len(self._posts)

    @property
    def cursor(self) -> str | None:
        return self.state["cursor"]

    @property
    def complete(self) -> bool:
        return self.state["complete"]

    def append_page(self, posts: list[Post], next_cursor: str | None) -> int:
        added = 0
        with self._jsonl.open("a", encoding="utf-8") as file:
            for post in posts:
                if post.id in self._posts:
                    continue
                file.write(json.dumps(post.to_dict(), ensure_ascii=True) + "\n")
                self._posts[post.id] = post
                added += 1
            file.flush()
            os.fsync(file.fileno())
        self.state.update(
            cursor=next_cursor,
            pages_fetched=self.state["pages_fetched"] + 1,
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
                for key in ("media_urls", "outbound_urls", "hashtags", "mentions"):
                    row[key] = json.dumps(row[key], ensure_ascii=False)
                writer.writerow(row)
        manifest = {
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
