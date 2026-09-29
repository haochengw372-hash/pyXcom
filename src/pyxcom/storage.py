"""Resumable public-post exports with auditable state and hashes."""

import csv
import hashlib
import json
import os
from dataclasses import fields
from pathlib import Path

from .models import CollectionResult, Post
from .transport import now_utc


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


class PostStore:
    def __init__(self, output_dir: str | Path, *, query: dict) -> None:
        self.output_dir = Path(output_dir).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = self.output_dir / "posts.jsonl"
        self._state_path = self.output_dir / "state.json"
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
            for line in self._jsonl.read_text(encoding="utf-8").splitlines():
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
                file.write(json.dumps(post.to_dict(), ensure_ascii=False) + "\n")
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
        csv_path = self.output_dir / "posts.csv"
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
        _atomic_json(self.output_dir / "manifest.json", manifest)
        return CollectionResult(
            output_dir=self.output_dir,
            post_count=len(rows),
            pages_fetched=self.state["pages_fetched"],
            complete=complete,
            reason=reason,
        )
