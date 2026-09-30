"""User-facing collection API for public X data."""

import json
import re
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Callable, Iterator

from .auth import load_x_cookies
from .errors import APIError, RateLimitError
from .layout import child_dir, migrate_collection, source_path
from .models import CollectionResult, Post, Profile
from .parse import bottom_cursor, timeline_primary_posts
from .search import MirrorSearch
from .storage import PostStore, _atomic_json
from .transport import TWEET_FEATURES, XTransport, now_utc

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


def _page_options(max_pages: int | None, limit: int | None) -> None:
    for name, value in (("max_pages", max_pages), ("limit", limit)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{name} must be a positive integer or None")


def _within(post: Post, since: str | None, until: str | None) -> bool:
    day = post.created_at_utc[:10]
    return (since is None or day >= since) and (until is None or day < until)


class XClient:
    """Read public X data using a manually logged-in Chrome or Edge session.

    No browser is launched or controlled. Cookies are read into memory and sent
    only to x.com. A search mirror is used only when explicitly configured.
    """

    def __init__(
        self,
        *,
        browser: str = "chrome",
        profile: str | None = None,
        cookie_db: str | Path | None = None,
        proxy: str | None = None,
        mirror_base: str | None = None,
        delay: float = 1.0,
        timeout: float = 30,
    ) -> None:
        cookies = load_x_cookies(browser, profile=profile, cookie_db=cookie_db)
        self._x = XTransport(cookies, proxy=proxy, timeout=timeout)
        self._mirror = (
            MirrorSearch(base_url=mirror_base, proxy=proxy, timeout=timeout)
            if mirror_base
            else None
        )
        self._profile_cache: dict[str, Profile] = {}
        self.delay = max(0.0, delay)

    def close(self) -> None:
        self._x.close()
        if self._mirror is not None:
            self._mirror.close()

    def __enter__(self) -> "XClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def get_user(self, handle: str) -> Profile:
        normalized = _handle(handle)
        key = normalized.lower()
        if key not in self._profile_cache:
            self._profile_cache[key] = self._x.get_profile(normalized)
        return self._profile_cache[key]

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

    def get_raw_user_timeline(
        self, handle: str, *, timeline: str = "posts", cursor: str | None = None
    ) -> dict:
        """Return the current raw X timeline page for diagnosis or custom fields."""
        profile = self.get_user(handle)
        return self._x.user_timeline_page(profile.id, timeline=timeline, cursor=cursor)

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
        _page_options(max_pages, limit)
        if timeline not in {"posts", "replies"}:
            raise ValueError("timeline must be posts or replies")
        since, until = _dates(since, until)
        profile = self.get_user(handle)
        cursor = None
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        pages = 0
        yielded = 0
        old_pages = 0
        empty_pages = 0
        while True:
            if cursor and cursor in seen_cursors:
                return
            if cursor:
                seen_cursors.add(cursor)
            payload = self._x.user_timeline_page(
                profile.id, timeline=timeline, cursor=cursor
            )
            authored = [
                post
                for post in timeline_primary_posts(payload, captured_at_utc=now_utc())
                if post.author_id == profile.id
            ]
            for post in authored:
                if post.post_role != ("comment" if timeline == "replies" else "main"):
                    continue
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
            empty_pages = empty_pages + 1 if not authored else 0
            cursor = bottom_cursor(payload)
            if (
                not cursor
                or old_pages >= 2
                or empty_pages >= 2
                or (max_pages and pages >= max_pages)
            ):
                return
            time.sleep(self.delay)

    def get_user_posts(
        self,
        handle: str,
        *,
        timeline: str = "posts",
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        """Return authored timeline records; prefer get_user_replies for replies."""
        return list(
            self.iter_user_posts(
                handle,
                timeline=timeline,
                since=since,
                until=until,
                max_pages=max_pages,
                limit=limit,
            )
        )

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
        _page_options(max_pages, limit)
        if timeline not in {"posts", "replies"}:
            raise ValueError("timeline must be posts or replies")
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
        store.retain_role("comment" if timeline == "replies" else "main")
        if query["kind"] == "user_timeline":
            _atomic_json(
                source_path(output_dir, "profiles.json"),
                {profile.handle: profile.to_dict()},
            )
        if store.complete:
            return store.finish(complete=True, reason=store.state["reason"])
        cursor = store.cursor
        seen_cursors: set[str] = set()
        pages_this_run = 0
        old_pages = 0
        empty_pages = 0
        while True:
            if cursor and cursor in seen_cursors:
                return store.finish(complete=False, reason="repeated_cursor")
            if cursor:
                seen_cursors.add(cursor)
            try:
                payload = self._x.user_timeline_page(
                    profile.id, timeline=timeline, cursor=cursor
                )
            except RateLimitError as exc:
                store.state["rate_reset_at"] = exc.reset_at
                return store.finish(complete=False, reason="rate_limited")
            authored = [
                post
                for post in timeline_primary_posts(payload, captured_at_utc=now_utc())
                if post.author_id == profile.id
            ]
            filtered = [
                replace(
                    post,
                    discovery_source="x_timeline",
                    discovery_url=f"https://x.com/{profile.handle}",
                )
                for post in authored
                if _within(post, since, until)
                and post.post_role == ("comment" if timeline == "replies" else "main")
            ]
            next_cursor = bottom_cursor(payload)
            store.state.pop("rate_reset_at", None)
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
            empty_pages = empty_pages + 1 if not authored else 0
            if not next_cursor:
                return store.finish(complete=True, reason="source_end")
            if old_pages >= 2:
                return store.finish(complete=True, reason="passed_since")
            if empty_pages >= 2:
                return store.finish(complete=True, reason="empty_timeline_end")
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
        migrate_collection(output)
        original = self.save_user_posts(
            handle,
            child_dir(output, "originals"),
            timeline="posts",
            since=since,
            until=until,
            max_pages=max_pages,
        )
        replies = self.save_user_posts(
            handle,
            child_dir(output, "replies"),
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
        profile = self.get_user(handle)
        _atomic_json(
            source_path(output, "profiles.json"), {profile.handle: profile.to_dict()}
        )
        if store.complete and original.complete and replies.complete:
            return store.finish(complete=True, reason="both_timelines_complete")
        combined: dict[str, Post] = {}
        for child in (child_dir(output, "originals"), child_dir(output, "replies")):
            for line in (
                source_path(child, "posts.jsonl")
                .read_text(encoding="utf-8")
                .split("\n")
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
        _page_options(max_pages, limit)
        since, until = _dates(since, until)
        user = _handle(user) if user else None
        if not keyword.strip():
            raise ValueError("keyword cannot be empty")
        if self._mirror is None:
            if user is None:
                raise APIError(
                    "Cross-user keyword search requires an explicit mirror_base; "
                    "search within one user with user=... to stay on X"
                )
            seen_ids: set[str] = set()
            yielded = 0
            needle = keyword.casefold()
            for timeline in ("posts", "replies"):
                for post in self.iter_user_posts(
                    user,
                    timeline=timeline,
                    since=since,
                    until=until,
                    max_pages=max_pages,
                ):
                    if post.id in seen_ids or needle not in post.text.casefold():
                        continue
                    seen_ids.add(post.id)
                    yield post
                    yielded += 1
                    if limit and yielded >= limit:
                        return
            return
        mirror = self._mirror
        cursor: str | None = mirror.initial_url(
            keyword, since=since, until=until, user=user
        )
        seen_cursors: set[str] = set()
        mirror_seen_ids: set[str] = set()
        pages = 0
        yielded = 0
        while cursor and cursor not in seen_cursors:
            seen_cursors.add(cursor)
            page = mirror.page(cursor)
            for offset in range(0, len(page.post_ids), 50):
                posts = self._x.get_posts(page.post_ids[offset : offset + 50])
                for post in posts:
                    if post.id in mirror_seen_ids or not _within(post, since, until):
                        continue
                    if user and post.author_handle.lower() != user.lower():
                        continue
                    mirror_seen_ids.add(post.id)
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

    def get_search(
        self,
        keyword: str,
        *,
        since: str | None = None,
        until: str | None = None,
        user: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        """Compatibility name for get_search_posts; user is the legacy handle option."""
        return list(
            self.iter_search(
                keyword,
                since=since,
                until=until,
                user=user,
                max_pages=max_pages,
                limit=limit,
            )
        )

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
        _page_options(max_pages, limit)
        since, until = _dates(since, until)
        user = _handle(user) if user else None
        if not keyword.strip():
            raise ValueError("keyword cannot be empty")
        if self._mirror is None:
            if user is None:
                raise APIError(
                    "Cross-user keyword search requires an explicit --mirror-base; "
                    "use --user for direct X search"
                )
            return self._save_direct_user_search(
                keyword,
                user,
                output_dir,
                since=since,
                until=until,
                max_pages=max_pages,
                limit=limit,
            )
        mirror = self._mirror
        query = {
            "kind": "search_mirror",
            "keyword": keyword,
            "since": since,
            "until": until,
            "user": user,
            "mirror": mirror.base_url,
        }
        store = PostStore(output_dir, query=query)
        if store.complete:
            return store.finish(complete=True, reason=store.state["reason"])
        cursor = store.cursor or mirror.initial_url(
            keyword, since=since, until=until, user=user
        )
        seen_cursors: set[str] = set()
        pages_this_run = 0
        while cursor:
            if cursor in seen_cursors:
                return store.finish(complete=False, reason="repeated_cursor")
            seen_cursors.add(cursor)
            try:
                page = mirror.page(cursor)
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

    def _save_direct_user_search(
        self,
        keyword: str,
        user: str,
        output_dir: str | Path,
        *,
        since: str | None,
        until: str | None,
        max_pages: int | None,
        limit: int | None,
    ) -> CollectionResult:
        output = Path(output_dir)
        migrate_collection(output)
        activity = self.save_user_activity(
            user,
            child_dir(output, "source"),
            since=since,
            until=until,
            max_pages=max_pages,
        )
        store = PostStore(
            output,
            query={
                "kind": "search_direct_user",
                "keyword": keyword,
                "user": user,
                "since": since,
                "until": until,
            },
        )
        profile = self.get_user(user)
        _atomic_json(
            source_path(output, "profiles.json"), {profile.handle: profile.to_dict()}
        )
        if store.complete and activity.complete:
            return store.finish(complete=True, reason="both_timelines_complete")
        matches: list[Post] = []
        needle = keyword.casefold()
        for line in (
            source_path(child_dir(output, "source"), "posts.jsonl")
            .read_text(encoding="utf-8")
            .split("\n")
        ):
            if line.strip():
                post = Post(**json.loads(line))
                if needle in post.text.casefold():
                    matches.append(post)
        if limit is not None:
            matches = matches[: max(0, limit)]
        store.append_page(matches, None)
        store.state["pages_fetched"] = activity.pages_fetched
        capped = limit is not None and len(matches) >= limit
        return store.finish(
            complete=activity.complete and not capped,
            reason="item_limit" if capped else activity.reason,
        )

    def save_accounts(
        self,
        handles: list[str],
        output_dir: str | Path,
        *,
        since: str,
        until: str,
        pages_per_round: int = 5,
        rounds: int = 1,
        wait_on_rate_limit: bool = False,
        progress: Callable[[dict], None] | None = None,
    ) -> CollectionResult:
        """Collect several verified X accounts with resumable round-robin paging."""
        from .batch import collect_accounts

        normalized_since, normalized_until = _dates(since, until)
        if normalized_since is None or normalized_until is None:
            raise ValueError("Batch collection requires since and until dates")
        canonical = [_handle(handle) for handle in handles]
        return collect_accounts(
            self,
            canonical,
            output_dir,
            since=normalized_since,
            until=normalized_until,
            pages_per_round=pages_per_round,
            rounds=rounds,
            wait_on_rate_limit=wait_on_rate_limit,
            progress=progress,
        )

    def iter_user_replies(
        self,
        handle: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        """Stream replies authored by this account, not replies received by it."""
        return self.iter_user_posts(
            handle,
            timeline="replies",
            since=since,
            until=until,
            max_pages=max_pages,
            limit=limit,
        )

    def get_user_replies(
        self,
        handle: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        """Return replies authored by this account in memory."""
        return list(
            self.iter_user_replies(
                handle, since=since, until=until, max_pages=max_pages, limit=limit
            )
        )

    def save_user_replies(
        self,
        handle: str,
        output_dir: str | Path,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        """Save this account's reply timeline to the standard dataset layout."""
        return self.save_user_posts(
            handle,
            output_dir,
            timeline="replies",
            since=since,
            until=until,
            max_pages=max_pages,
            limit=limit,
        )

    def iter_search_posts(
        self,
        keyword: str,
        *,
        handle: str | None = None,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        """Stream matched posts/replies; handle selects direct account search."""
        return self.iter_search(
            keyword,
            user=handle,
            since=since,
            until=until,
            max_pages=max_pages,
            limit=limit,
        )

    def get_search_posts(
        self,
        keyword: str,
        *,
        handle: str | None = None,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        """Return search matches in memory using the same options as iter/save."""
        return list(
            self.iter_search_posts(
                keyword,
                handle=handle,
                since=since,
                until=until,
                max_pages=max_pages,
                limit=limit,
            )
        )

    def save_search_posts(
        self,
        keyword: str,
        output_dir: str | Path,
        *,
        handle: str | None = None,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        """Save search matches and resumable source streams."""
        return self.save_search(
            keyword,
            output_dir,
            user=handle,
            since=since,
            until=until,
            max_pages=max_pages,
            limit=limit,
        )

    def save_users_activity(
        self,
        handles: list[str],
        output_dir: str | Path,
        *,
        since: str,
        until: str,
        pages_per_round: int = 5,
        rounds: int = 1,
        wait_on_rate_limit: bool = False,
        progress: Callable[[dict], None] | None = None,
    ) -> CollectionResult:
        """Save multiple accounts' authored main posts and replies with checkpoints."""
        return self.save_accounts(
            handles,
            output_dir,
            since=since,
            until=until,
            pages_per_round=pages_per_round,
            rounds=rounds,
            wait_on_rate_limit=wait_on_rate_limit,
            progress=progress,
        )
