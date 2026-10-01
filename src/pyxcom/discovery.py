"""Native X discovery with explicit pagination and collection provenance."""

import re
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import quote

from .errors import APIError, ParseError, RateLimitError
from .models import CollectionResult, Post, Profile
from .parse import _walk, bottom_cursor, parse_post, timeline_primary_posts
from .storage import PostStore
from .transport import XTransport, now_utc


def _instructions(payload: dict) -> list[dict]:
    for node in _walk(payload.get("data", payload)):
        if isinstance(node.get("instructions"), list):
            return node["instructions"]
    raise ParseError("X discovery response has no timeline instructions")


def _source_end(payload: dict) -> bool:
    if bottom_cursor(payload):
        return False
    return any(
        item.get("type") == "TimelineTerminateTimeline"
        and item.get("direction") == "Bottom"
        for item in _instructions(payload)
    )


def _repost_activity_unavailable(payload: dict, user_id: str) -> bool:
    """A social-context repost has no action ID/date without an outer wrapper."""
    for instruction in _instructions(payload):
        for entry in instruction.get("entries", []):
            content = entry.get("content") or {}
            items = [content]
            items.extend(
                module.get("item") or module for module in content.get("items", [])
            )
            for item in items:
                item_content = item.get("itemContent") or {}
                social = (
                    item_content.get("socialContext") or item.get("socialContext") or {}
                )
                if social.get("contextType") != "Retweet":
                    continue
                result = (item_content.get("tweet_results") or {}).get("result")
                post = parse_post(result) if isinstance(result, dict) else None
                if not post or post.post_role != "repost" or post.author_id != user_id:
                    return True
    return False


def _query_options(
    query: str,
    since: str | None,
    until: str | None,
    max_pages: int | None,
    limit: int | None,
) -> tuple[str, str | None, str | None]:
    from .client import _dates, _page_options

    _page_options(max_pages, limit)
    since, until = _dates(since, until)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query cannot be empty")
    query = query.strip()
    # Quoted text may literally contain "since:..."; it is not a date operator.
    operators = re.sub(r'"(?:[^"\\]|\\.)*"', "", query)
    for name, explicit in (("since", since), ("until", until)):
        if explicit is None:
            continue
        embedded = re.findall(rf"(?<![\w]){name}:([^\s)]+)", operators)
        if any(value != explicit for value in embedded):
            raise ValueError(f"Explicit {name} conflicts with a date operator in query")
    if since:
        query += f" since:{since}"
    if until:
        query += f" until:{until}"
    return query, since, until


class DiscoveryMixin:
    """Native search, verified quote discovery, and authored repost activity.

    Search syntax and availability are controlled by X. Completion describes
    pagination of the observed endpoint, never exhaustive historical coverage.
    """

    _x: XTransport
    delay: float

    if TYPE_CHECKING:

        def get_user(self, handle: str) -> Profile: ...

    def _discovery_page(self, query: dict, cursor: str | None) -> dict:
        if query["kind"] == "user_reposts":
            return self._x.user_timeline_page(
                query["user_id"], timeline="reposts", cursor=cursor
            )
        return self._x.search_page(
            query["effective_query"], cursor=cursor, product="Latest", count=20
        )

    def _discovery_posts(self, payload: dict, query: dict) -> list[Post]:
        from .client import _within

        _instructions(payload)
        posts = timeline_primary_posts(payload, captured_at_utc=now_utc())
        if query["kind"] == "post_quotes":
            posts = [
                p
                for p in posts
                if p.quoted_post_id == query["post_id"] and p.post_role != "repost"
            ]
        elif query["kind"] == "user_reposts":
            posts = [
                p
                for p in posts
                if p.post_role == "repost" and p.author_id == query["user_id"]
            ]
        source = "x_repost_timeline" if query["kind"] == "user_reposts" else "x_search"
        url = (
            f"https://x.com/{query['handle']}"
            if query["kind"] == "user_reposts"
            else "https://x.com/search?q="
            + quote(query["effective_query"], safe="")
            + "&f=live"
        )
        return [
            replace(p, discovery_source=source, discovery_url=url)
            for p in posts
            if _within(p, query["since"], query["until"])
        ]

    def _iter_discovery(
        self,
        query: dict,
        max_pages: int | None,
        limit: int | None,
    ) -> Iterator[Post]:
        cursor = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        pages = 0
        while True:
            if cursor and cursor in seen_cursors:
                raise APIError("Discovery pagination repeated its cursor")
            if cursor:
                seen_cursors.add(cursor)
            payload = self._discovery_page(query, cursor)
            if query["kind"] == "user_reposts" and _repost_activity_unavailable(
                payload, query["user_id"]
            ):
                raise APIError(
                    "Repost activity unavailable: X returned a social-context repost "
                    "without an outer repost ID and action timestamp"
                )
            for post in self._discovery_posts(payload, query):
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                yield post
                if limit and len(seen_ids) >= limit:
                    return
            pages += 1
            if _source_end(payload):
                if query["kind"] == "post_quotes" and not seen_ids:
                    raise APIError(
                        "Quote query is unverified: no matching quoted post IDs"
                    )
                return
            cursor = bottom_cursor(payload)
            if not cursor:
                raise APIError("Discovery pagination omitted its bottom cursor")
            if max_pages and pages >= max_pages:
                if query["kind"] == "post_quotes" and not seen_ids:
                    raise APIError(
                        "Quote query is unverified: no matching quoted post IDs"
                    )
                return
            time.sleep(self.delay)

    def _save_discovery(
        self,
        query: dict,
        output_dir: str | Path,
        max_pages: int | None,
        limit: int | None,
    ) -> CollectionResult:
        store = PostStore(output_dir, query=query)
        if store.complete:
            return store.finish(complete=True, reason=store.state["reason"])
        if limit and store.count >= limit:
            return store.finish(complete=False, reason="item_limit")
        cursor = store.cursor
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            if cursor and cursor in seen_cursors:
                return store.finish(complete=False, reason="repeated_cursor")
            if cursor:
                seen_cursors.add(cursor)
            try:
                payload = self._discovery_page(query, cursor)
            except RateLimitError as exc:
                store.state["rate_reset_at"] = exc.reset_at
                return store.finish(complete=False, reason="rate_limited")
            except APIError as exc:
                store.state["error_type"] = type(exc).__name__
                return store.finish(complete=False, reason="source_error")
            operation = (
                "UserTweets" if query["kind"] == "user_reposts" else "SearchTimeline"
            )
            variables = (
                {"userId": query["user_id"], "cursor": cursor, "timeline": "reposts"}
                if query["kind"] == "user_reposts"
                else {
                    "rawQuery": query["effective_query"],
                    "cursor": cursor,
                    "product": "Latest",
                    "count": 20,
                }
            )
            store.archive_response(payload, operation=operation, variables=variables)
            try:
                posts = self._discovery_posts(payload, query)
                if query["kind"] == "search_query" and (
                    query["since"] or query["until"]
                ):
                    from .client import _within

                    primary = timeline_primary_posts(payload)
                    audit = store.state.setdefault(
                        "date_scope",
                        {
                            "returned_observations": 0,
                            "out_of_window_observations": 0,
                            "local_filter_applied": True,
                        },
                    )
                    audit["returned_observations"] += len(primary)
                    audit["out_of_window_observations"] += sum(
                        not _within(p, query["since"], query["until"]) for p in primary
                    )
                    if audit["out_of_window_observations"]:
                        audit["warning"] = (
                            "source_returned_posts_outside_requested_dates"
                        )
                ended = _source_end(payload)
                unavailable = query[
                    "kind"
                ] == "user_reposts" and _repost_activity_unavailable(
                    payload, query["user_id"]
                )
            except ParseError:
                return store.finish(complete=False, reason="parse_error")
            next_cursor = bottom_cursor(payload)
            # Resume a partly consumed page from its original cursor, not its end.
            existing_ids = set(store._posts)
            unique_new = [p for p in posts if p.id not in existing_ids]
            truncated = bool(limit and len(unique_new) > limit - store.count)
            if truncated:
                assert limit is not None
                keep = {p.id for p in unique_new[: limit - store.count]}
                posts = [p for p in posts if p.id in existing_ids or p.id in keep]
            store.state.pop("rate_reset_at", None)
            store.append_page(
                posts, cursor if truncated or unavailable else next_cursor
            )
            pages += 1
            if unavailable:
                return store.finish(
                    complete=False, reason="repost_activity_unavailable"
                )
            if truncated:
                return store.finish(complete=False, reason="item_limit")
            if ended:
                if query["kind"] == "post_quotes" and not store.count:
                    return store.finish(complete=False, reason="query_unverified")
                return store.finish(complete=True, reason="source_end")
            if not next_cursor:
                reason = (
                    "query_unverified"
                    if query["kind"] == "post_quotes" and not store.count
                    else "missing_cursor"
                )
                return store.finish(complete=False, reason=reason)
            if next_cursor in seen_cursors or next_cursor == cursor:
                return store.finish(complete=False, reason="repeated_cursor")
            if limit and store.count >= limit:
                return store.finish(complete=False, reason="item_limit")
            if max_pages and pages >= max_pages:
                return store.finish(complete=False, reason="page_limit")
            cursor = next_cursor
            time.sleep(self.delay)

    def iter_search_query(
        self,
        query: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        """Execute native cross-user search, preserving its operator syntax.

        Explicit UTC date options cannot conflict with since/until operators
        embedded in the query. Other operator semantics are controlled by X.
        """
        effective, since, until = _query_options(query, since, until, max_pages, limit)
        spec = {
            "kind": "search_query",
            "query": query.strip(),
            "effective_query": effective,
            "since": since,
            "until": until,
        }
        return self._iter_discovery(spec, max_pages, limit)

    def get_search_query(
        self,
        query: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        return list(
            self.iter_search_query(
                query, since=since, until=until, max_pages=max_pages, limit=limit
            )
        )

    def save_search_query(
        self,
        query: str,
        output_dir: str | Path,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        effective, since, until = _query_options(query, since, until, max_pages, limit)
        spec = {
            "kind": "search_query",
            "query": query.strip(),
            "effective_query": effective,
            "since": since,
            "until": until,
        }
        return self._save_discovery(spec, output_dir, max_pages, limit)

    def _quote_query(
        self,
        post_id_or_url: str,
        since: str | None,
        until: str | None,
        max_pages: int | None,
        limit: int | None,
    ) -> dict:
        from .client import _post_id

        post_id = _post_id(post_id_or_url)
        query, since, until = _query_options(
            f"quoted_tweet_id:{post_id}", since, until, max_pages, limit
        )
        return {
            "kind": "post_quotes",
            "post_id": post_id,
            "effective_query": query,
            "since": since,
            "until": until,
        }

    def iter_post_quotes(
        self,
        post_id_or_url: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        """Find quotes through native search and verify each quoted target ID.

        The search operator depends on X; zero matches cannot verify that no
        quotes exist. This is discovery, not a complete list of quoting users.
        """
        spec = self._quote_query(post_id_or_url, since, until, max_pages, limit)
        return self._iter_discovery(spec, max_pages, limit)

    def get_post_quotes(
        self,
        post_id_or_url: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        return list(
            self.iter_post_quotes(
                post_id_or_url,
                since=since,
                until=until,
                max_pages=max_pages,
                limit=limit,
            )
        )

    def save_post_quotes(
        self,
        post_id_or_url: str,
        output_dir: str | Path,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        spec = self._quote_query(post_id_or_url, since, until, max_pages, limit)
        return self._save_discovery(spec, output_dir, max_pages, limit)

    def _repost_query(
        self,
        handle: str,
        since: str | None,
        until: str | None,
        max_pages: int | None,
        limit: int | None,
    ) -> dict:
        from .client import _dates, _page_options

        _page_options(max_pages, limit)
        since, until = _dates(since, until)
        profile = self.get_user(handle)
        return {
            "kind": "user_reposts",
            "handle": profile.handle,
            "user_id": profile.id,
            "since": since,
            "until": until,
        }

    def iter_user_reposts(
        self,
        handle: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Post]:
        """Read repost activities authored by this account, with action timestamps.

        Only visible UserTweets records are covered; a reposter-user list does
        not establish repost activity times and is collected separately. If X
        returns originals tagged as reposts instead of timestamped repost
        wrappers, raise APIError rather than inventing action timestamps.
        """
        spec = self._repost_query(handle, since, until, max_pages, limit)
        return self._iter_discovery(spec, max_pages, limit)

    def get_user_reposts(
        self,
        handle: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> list[Post]:
        return list(
            self.iter_user_reposts(
                handle, since=since, until=until, max_pages=max_pages, limit=limit
            )
        )

    def save_user_reposts(
        self,
        handle: str,
        output_dir: str | Path,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
    ) -> CollectionResult:
        spec = self._repost_query(handle, since, until, max_pages, limit)
        return self._save_discovery(spec, output_dir, max_pages, limit)
