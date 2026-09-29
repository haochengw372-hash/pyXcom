"""User-facing collection API for public X data."""

import json
import re
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterator

from .auth import load_x_cookies
from .errors import RateLimitError
from .models import CollectionResult, Post, Profile
from .parse import bottom_cursor, timeline_posts
from .search import MirrorSearch
from .storage import PostStore
from .transport import TWEET_FEATURES, XTransport

_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_POST_ID = re.compile(r"(?:/status/)?(\d{10,25})(?:\D.*)?$")


def _handle(value: str) -> str:
    handle = value.removeprefix("@").strip()
    if not _HANDLE.fullmatch(handle):
        raise ValueError("X handle must contain 1-15 letters, numbers, or underscores")
    return handle


def _date(value: str | None) -> str | None:
    if value is None:
        return None
    return date.fromisoformat(value).isoformat()


def _dates(since: str | None, until: str | None) -> tuple[str | None, str | None]:
    since, until = _date(since), _date(until)
    if since and until and since >= until:
        raise ValueError("since must be earlier than until; until is exclusive")
    return since, until


def _post_id(value: str) -> str:
    match = _POST_ID.search(value.strip())
    if not match:
        raise ValueError("Expected a numeric post ID or an X post URL")
    return match.group(1)


def _within(post: Post, since: str | None, until: str | None) -> bool:
    day = post.created_at_utc[:10]
    return (since is None or day >= since) and (until is None or day < until)


class XClient:
    """Read public X data using a manually logged-in Chrome or Edge session.

    No browser is launched or controlled. Cookies are read into memory and sent
    only to x.com. The search mirror uses a separate cookie-free HTTP client.
    """

    def __init__(
        self,
        *,
        browser: str = "chrome",
        profile: str | None = None,
        cookie_db: str | Path | None = None,
        proxy: str | None = None,
        mirror_base: str = "https://x.noodl3.net",
        delay: float = 1.0,
        timeout: float = 30,
    ) -> None:
        cookies = load_x_cookies(browser, profile=profile, cookie_db=cookie_db)
        self._x = XTransport(cookies, proxy=proxy, timeout=timeout)
        self._mirror = MirrorSearch(base_url=mirror_base, proxy=proxy, timeout=timeout)
        self.delay = max(0.0, delay)

    def close(self) -> None:
        self._x.close()
        self._mirror.close()

    def __enter__(self) -> "XClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def get_user(self, handle: str) -> Profile:
        return self._x.get_profile(_handle(handle))

    def get_post(self, post_id_or_url: str) -> Post:
        post = self._x.get_post(_post_id(post_id_or_url))
        return replace(post, discovery_source="x_detail", discovery_url=post.url)

    def get_posts(self, post_ids: list[str]) -> list[Post]:
        posts: list[Post] = []
        for offset in range(0, len(post_ids), 50):
            ids = [_post_id(value) for value in post_ids[offset : offset + 50]]
            posts.extend(self._x.get_posts(ids))
            if offset + 50 < len(post_ids):
                time.sleep(self.delay)
        return posts

    def get_raw_post(self, post_id_or_url: str) -> dict:
        """Return the current raw public X object for a single post."""
        return self._x.graphql(
            "TweetResultByRestId",
            {
                "tweetId": _post_id(post_id_or_url),
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            },
            features=TWEET_FEATURES,
            field_toggles={},
        )

    def iter_user_posts(
        self,
        handle: str,
        *,
        timeline: str = "posts",
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        since, until = _dates(since, until)
        profile = self.get_user(handle)
        cursor = None
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        pages = 0
        yielded = 0
        old_pages = 0
        while True:
            if cursor and cursor in seen_cursors:
                return
            if cursor:
                seen_cursors.add(cursor)
            payload = self._x.user_timeline_page(
                profile.id, timeline=timeline, cursor=cursor
            )
            authored = [
                post for post in timeline_posts(payload) if post.author_id == profile.id
            ]
            for post in authored:
                if post.id in seen_ids or not _within(post, since, until):
                    continue
                seen_ids.add(post.id)
                yield replace(
                    post,
                    discovery_source="x_timeline",
                    discovery_url=f"https://x.com/{profile.handle}",
                )
                yielded += 1
                if limit and yielded >= limit:
                    return
            pages += 1
            if (
                since
                and authored
                and max(post.created_at_utc[:10] for post in authored) < since
            ):
                old_pages += 1
            else:
                old_pages = 0
            cursor = bottom_cursor(payload)
            if not cursor or old_pages >= 2 or (max_pages and pages >= max_pages):
                return
            time.sleep(self.delay)

    def get_user_posts(self, handle: str, **kwargs) -> list[Post]:
        return list(self.iter_user_posts(handle, **kwargs))

    def save_user_posts(
        self,
        handle: str,
        output_dir: str | Path,
        *,
        timeline: str = "posts",
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        since, until = _dates(since, until)
        profile = self.get_user(handle)
        query = {
            "kind": "user_timeline",
            "handle": profile.handle,
            "timeline": timeline,
            "since": since,
            "until": until,
        }
        store = PostStore(output_dir, query=query)
        if store.complete:
            return store.finish(complete=True, reason=store.state["reason"])
        cursor = store.cursor
        seen_cursors: set[str] = set()
        pages_this_run = 0
        old_pages = 0
        while True:
            if cursor and cursor in seen_cursors:
                return store.finish(complete=False, reason="repeated_cursor")
            if cursor:
                seen_cursors.add(cursor)
            try:
                payload = self._x.user_timeline_page(
                    profile.id, timeline=timeline, cursor=cursor
                )
            except RateLimitError:
                return store.finish(complete=False, reason="rate_limited")
            authored = [
                post for post in timeline_posts(payload) if post.author_id == profile.id
            ]
            filtered = [
                replace(
                    post,
                    discovery_source="x_timeline",
                    discovery_url=f"https://x.com/{profile.handle}",
                )
                for post in authored
                if _within(post, since, until)
            ]
            next_cursor = bottom_cursor(payload)
            store.append_page(filtered, next_cursor)
            pages_this_run += 1
            if (
                since
                and authored
                and max(post.created_at_utc[:10] for post in authored) < since
            ):
                old_pages += 1
            else:
                old_pages = 0
            if not next_cursor:
                return store.finish(complete=True, reason="source_end")
            if old_pages >= 2:
                return store.finish(complete=True, reason="passed_since")
            if limit and store.count >= limit:
                return store.finish(complete=False, reason="item_limit")
            if max_pages and pages_this_run >= max_pages:
                return store.finish(complete=False, reason="page_limit")
            cursor = next_cursor
            time.sleep(self.delay)

    def save_user_activity(
        self,
        handle: str,
        output_dir: str | Path,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
    ) -> CollectionResult:
        """Collect originals and authored replies into one deduplicated table."""
        since, until = _dates(since, until)
        output = Path(output_dir)
        original = self.save_user_posts(
            handle,
            output / "originals",
            timeline="posts",
            since=since,
            until=until,
            max_pages=max_pages,
        )
        replies = self.save_user_posts(
            handle,
            output / "replies",
            timeline="replies",
            since=since,
            until=until,
            max_pages=max_pages,
        )
        store = PostStore(
            output,
            query={
                "kind": "user_activity",
                "handle": _handle(handle),
                "since": since,
                "until": until,
            },
        )
        if store.complete and original.complete and replies.complete:
            return store.finish(complete=True, reason="both_timelines_complete")
        combined: dict[str, Post] = {}
        for child in (output / "originals", output / "replies"):
            for line in (
                (child / "posts.jsonl").read_text(encoding="utf-8").splitlines()
            ):
                if line.strip():
                    post = Post(**json.loads(line))
                    combined[post.id] = post
        store.append_page(list(combined.values()), None)
        store.state["pages_fetched"] = original.pages_fetched + replies.pages_fetched
        return store.finish(
            complete=original.complete and replies.complete,
            reason="both_timelines_complete"
            if original.complete and replies.complete
            else "partial_timelines",
        )

    def iter_search(
        self,
        keyword: str,
        *,
        since: str | None = None,
        until: str | None = None,
        user: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        since, until = _dates(since, until)
        user = _handle(user) if user else None
        cursor: str | None = self._mirror.initial_url(
            keyword, since=since, until=until, user=user
        )
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        pages = 0
        yielded = 0
        while cursor and cursor not in seen_cursors:
            seen_cursors.add(cursor)
            page = self._mirror.page(cursor)
            for offset in range(0, len(page.post_ids), 50):
                posts = self._x.get_posts(page.post_ids[offset : offset + 50])
                for post in posts:
                    if post.id in seen_ids or not _within(post, since, until):
                        continue
                    if user and post.author_handle.lower() != user.lower():
                        continue
                    seen_ids.add(post.id)
                    yield replace(
                        post,
                        discovery_source="nitter_search",
                        discovery_url=page.source_url,
                    )
                    yielded += 1
                    if limit and yielded >= limit:
                        return
            pages += 1
            cursor = page.next_url
            if max_pages and pages >= max_pages:
                return
            time.sleep(self.delay)

    def get_search(self, keyword: str, **kwargs) -> list[Post]:
        return list(self.iter_search(keyword, **kwargs))

    def save_search(
        self,
        keyword: str,
        output_dir: str | Path,
        *,
        since: str | None = None,
        until: str | None = None,
        user: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        since, until = _dates(since, until)
        user = _handle(user) if user else None
        query = {
            "kind": "search",
            "keyword": keyword,
            "since": since,
            "until": until,
            "user": user,
            "mirror": self._mirror.base_url,
        }
        store = PostStore(output_dir, query=query)
        if store.complete:
            return store.finish(complete=True, reason=store.state["reason"])
        cursor = store.cursor or self._mirror.initial_url(
            keyword, since=since, until=until, user=user
        )
        seen_cursors: set[str] = set()
        pages_this_run = 0
        while cursor:
            if cursor in seen_cursors:
                return store.finish(complete=False, reason="repeated_cursor")
            seen_cursors.add(cursor)
            try:
                page = self._mirror.page(cursor)
                posts = self._x.get_posts(page.post_ids)
            except RateLimitError:
                return store.finish(complete=False, reason="rate_limited")
            missing = set(page.post_ids) - {post.id for post in posts}
            if missing:
                store.add_missing_ids(missing)
            filtered = [
                replace(
                    post,
                    discovery_source="nitter_search",
                    discovery_url=page.source_url,
                )
                for post in posts
                if _within(post, since, until)
                and (not user or post.author_handle.lower() == user.lower())
            ]
            store.append_page(filtered, page.next_url)
            pages_this_run += 1
            if not page.next_url:
                return store.finish(complete=True, reason="source_end")
            if limit and store.count >= limit:
                return store.finish(complete=False, reason="item_limit")
            if max_pages and pages_this_run >= max_pages:
                return store.finish(complete=False, reason="page_limit")
            cursor = page.next_url
            time.sleep(self.delay)
        return store.finish(complete=True, reason="source_end")
