"""Bounded, resumable traversal of public replies to an X post or reply seed."""

import time
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, Iterator

from .errors import ParseError, RateLimitError
from .models import Post
from .parse import _walk, parse_post, parse_profile
from .transport import now_utc

if TYPE_CHECKING:
    from .client import XClient


def parse_conversation(data: dict) -> tuple[list[Post], list[str], dict]:
    """Read timeline content only, excluding embedded quotes and recommendations."""
    instructions = next(
        (
            n["instructions"]
            for n in _walk(data.get("data", {}))
            if isinstance(n.get("instructions"), list)
        ),
        None,
    )
    if instructions is None:
        raise ParseError("X conversation response contains no timeline instructions")
    posts: dict[str, Post] = {}
    cursors: list[str] = []
    profiles: dict[str, dict] = {}
    captured = now_utc()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if node.get("promotedMetadata"):
                return
            if "tweet_results" in node:
                result = (node.get("tweet_results") or {}).get("result")
                if isinstance(result, dict):
                    post = parse_post(result, captured_at_utc=captured)
                    if post:
                        posts[post.id] = post
                        tweet = result.get("tweet", result)
                        user = (
                            tweet.get("core", {})
                            .get("user_results", {})
                            .get("result", {})
                        )
                        try:
                            profile = parse_profile(user, captured_at_utc=captured)
                            profiles[profile.handle] = profile.to_dict()
                        except ParseError:
                            pass
                return  # Never recurse into a quoted tweet or an author's other data.
            kind = node.get("cursorType")
            if kind in (
                "Bottom",
                "ShowMore",
                "ShowMoreThreads",
                "ShowMoreReplies",
            ) and node.get("value"):
                cursors.append(node["value"])
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

    visit(instructions)
    return list(posts.values()), list(dict.fromkeys(cursors)), profiles


class CommentTraversal:
    def __init__(
        self,
        client: "XClient",
        root_id: str,
        *,
        max_depth: int,
        max_comments: int | None,
        max_pages: int | None,
        records: dict[str, Post] | None = None,
        state: dict | None = None,
        since: str | None = None,
        until: str | None = None,
    ):
        if max_depth is None:
            raise ValueError("max_depth must be a positive integer")
        for name, value in (
            ("max_depth", max_depth),
            ("max_comments", max_comments),
            ("max_pages", max_pages),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer")
        for bound in (since, until):
            if bound is not None:
                date.fromisoformat(bound)
        if since and until and since >= until:
            raise ValueError("since must be earlier than until; until is exclusive")
        self.since, self.until = since, until
        self.client = client
        self.root_id = root_id
        self.max_depth, self.max_comments, self.max_pages = (
            max_depth,
            max_comments,
            max_pages,
        )
        self.records = dict(records or {})
        self.state = state if state is not None else {}
        self.state.setdefault("queue", [[root_id, None]])
        self.state.setdefault("visited", [])
        self.state.setdefault("expanded", [root_id])
        self.state.setdefault("pending", {})
        self.state.setdefault("depths", {root_id: 0})
        self.state.setdefault("profiles", {})
        self.state.setdefault("pagination_warnings", [])
        self.state.setdefault("empty_pages", {})
        self.state.setdefault(
            "accepted_ids",
            [
                key
                for key, post in self.records.items()
                if key != root_id
                and getattr(post, "observation_role", "analysis") != "context"
            ],
        )
        self.state.setdefault("excluded_ids", [])
        self.latest_payload: dict | None = None
        self.latest_request: dict | None = None
        self.latest_response_number = 0
        self.complete = False
        self.reason = "not_started"
        self.pages_fetched = 0
        self.page_was_fetched = False

    @property
    def comment_count(self) -> int:
        """Eligible descendants only; the seed and context do not consume the cap."""
        return len(self.state["accepted_ids"])

    def _within_window(self, post: Post) -> bool:
        day = post.created_at_utc[:10]
        return (self.since is None or day >= self.since) and (
            self.until is None or day < self.until
        )

    def _exclude_other_branches(self) -> None:
        pending = self.state["pending"]
        excluded = set(self.state["excluded_ids"])
        conversation = self.state.get("conversation_id", self.root_id)
        if conversation != self.root_id:
            excluded.add(conversation)
        while True:
            outsiders = {
                key
                for key, value in pending.items()
                if value.get("in_reply_to_id") in excluded
            }
            if not outsiders:
                break
            excluded.update(outsiders)
            for key in outsiders:
                del pending[key]
        self.state["excluded_ids"] = sorted(excluded)

    def _accept_pending(self) -> list[Post]:
        added: list[Post] = []
        pending = self.state["pending"]
        self._exclude_other_branches()
        while True:
            progressed = False
            for key, value in list(pending.items()):
                post = Post(**value)
                parent_depth = self.state["depths"].get(post.in_reply_to_id)
                if parent_depth is None:
                    continue
                depth = parent_depth + 1
                if depth > self.max_depth or key in self.records:
                    self.state["depths"][key] = depth
                    del pending[key]
                    progressed = True
                    continue
                eligible = self._within_window(post)
                if (
                    eligible
                    and self.max_comments is not None
                    and self.comment_count >= self.max_comments
                ):
                    return added
                post = replace(
                    post, observation_role="analysis" if eligible else "context"
                )
                self.records[key] = post
                self.state["depths"][key] = depth
                del pending[key]
                if eligible:
                    self.state["accepted_ids"].append(key)
                added.append(post)
                progressed = True
                if (
                    depth < self.max_depth
                    and post.reply_count != 0
                    and key not in self.state["expanded"]
                ):
                    self.state["queue"].append([key, None])
                    self.state["expanded"].append(key)
            if not progressed:
                return added

    def pages(self) -> Iterator[list[Post]]:
        if self.root_id not in self.records:
            root = self.client.get_post(self.root_id)
            if root.reposted_post_id:
                raise ValueError(
                    "post_id_or_url must identify an original or reply, not a repost"
                )
            root = replace(root, observation_role="seed")
            self.records[root.id] = root
            self.page_was_fetched = False
            yield [root]
        seed = self.records[self.root_id]
        self.state.setdefault("conversation_id", seed.conversation_id or seed.id)
        if (
            seed.in_reply_to_id
            and seed.in_reply_to_id not in self.state["excluded_ids"]
        ):
            self.state["excluded_ids"].append(seed.in_reply_to_id)
        while True:
            self.page_was_fetched = False
            ready = self._accept_pending()
            if ready:
                yield ready
            if (
                self.max_comments is not None
                and self.comment_count >= self.max_comments
            ):
                self.reason = "comment_limit"
                return
            if not self.state["queue"]:
                unresolved = len(self.state["pending"])
                self.state["unresolved_count"] = unresolved
                self.complete = not unresolved and not self.state["pagination_warnings"]
                self.reason = (
                    "visible_source_end" if self.complete else "partial_conversation"
                )
                return
            if self.max_pages is not None and self.pages_fetched >= self.max_pages:
                self.reason = "page_limit"
                return
            task = self.state["queue"][0]
            focal, cursor = task
            try:
                payload = self.client._x.conversation_page(focal, cursor=cursor)
            except RateLimitError as exc:
                self.state["rate_reset_at"] = exc.reset_at
                self.reason = "rate_limited"
                return
            self.latest_payload = payload
            self.latest_response_number += 1
            self.latest_request = {"focalTweetId": focal}
            if cursor is not None:
                self.latest_request["cursor"] = cursor
            posts, cursors, profiles = parse_conversation(payload)
            self.state.pop("rate_reset_at", None)
            self.state["queue"].pop(0)
            self.state["visited"].append(task)
            self.state["profiles"].update(profiles)
            relevant = [
                p
                for p in posts
                if p.conversation_id == self.state["conversation_id"]
                and p.in_reply_to_id
                and p.id not in self.records
                and p.id not in self.state["excluded_ids"]
            ]
            for post in relevant:
                self.state["pending"][post.id] = replace(
                    post,
                    discovery_source="x_conversation",
                    discovery_url=f"https://x.com/i/status/{focal}",
                ).to_dict()
            empty = self.state["empty_pages"].get(focal, 0)
            self.state["empty_pages"][focal] = empty + 1 if not relevant else 0
            for next_cursor in cursors:
                follow = [focal, next_cursor]
                if follow in self.state["visited"]:
                    self.state["pagination_warnings"].append("repeated_cursor")
                elif self.state["empty_pages"][focal] >= 2:
                    self.state["pagination_warnings"].append("empty_page_limit")
                elif follow not in self.state["queue"]:
                    self.state["queue"].append(follow)
            self.pages_fetched += 1
            self.page_was_fetched = True
            yield self._accept_pending()
            if self.client.delay:
                time.sleep(self.client.delay)
