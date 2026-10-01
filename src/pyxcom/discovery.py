"""Native X discovery with explicit pagination and collection provenance."""

import re
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import quote

from .errors import APIError, ParseError, RateLimitError
from .models import CollectionResult, Post, Profile
from .parse import (
    _timeline_item_contents,
    _walk,
    bottom_cursor,
    parse_post,
    timeline_primary_posts,
)
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


_EMPTY_PAGE_LIMIT = 3
_NO_PROGRESS_LIMIT = 5
_PAUSE_REASONS = {"empty_page_limit", "no_progress_limit", "partial_source_content"}


def _source_page(
    payload: dict, *, audit: dict | None = None
) -> tuple[list[Post], bool]:
    """Keep readable tweets and explicitly account for unavailable/unknown slots."""
    instructions = _instructions(payload)
    known_instructions = {
        "TimelineAddEntries",
        "TimelineReplaceEntry",
        "TimelineAddToModule",
        "TimelinePinEntry",
        "TimelineTerminateTimeline",
        "TimelineClearCache",
    }
    details: dict = {
        "primary_item_count": 0,
        "unavailable_items": 0,
        "unparsed_items": 0,
        "unsupported_instructions": sum(
            i.get("type") not in known_instructions for i in instructions
        ),
        "unavailable_post_ids": [],
        "unparsed_post_ids": [],
        "source_post_ids": [],
    }
    posts: dict[str, Post] = {}
    source_ids: set[str] = set()
    captured = now_utc()
    for content in _timeline_item_contents(payload):
        identifier = re.search(
            r"(?:tweet|tombstone)-(\d+)(?:$|[-_])",
            str(content.get("_timeline_entry_id", "")),
        )
        entry_id = identifier.group(1) if identifier else None
        if "tweet_results" in content:
            details["primary_item_count"] += 1
            results = content.get("tweet_results")
            result = results.get("result") if isinstance(results, dict) else None
            empty_result = results is None or (
                isinstance(results, dict) and result in (None, {})
            )
            known_unavailable = isinstance(result, dict) and result.get(
                "__typename"
            ) in {"TweetTombstone", "TweetUnavailable"}
            post = None
            if not empty_result and not known_unavailable and isinstance(result, dict):
                try:
                    post = parse_post(result, captured_at_utc=captured)
                except (AttributeError, TypeError, ValueError, OverflowError):
                    pass
            if post is not None:
                posts[post.id] = post
                source_ids.add(post.id)
            else:
                category = (
                    "unavailable" if empty_result or known_unavailable else "unparsed"
                )
                details[category + "_items"] += 1
                if entry_id:
                    details[category + "_post_ids"].append(entry_id)
                    source_ids.add(entry_id)
        elif not (
            content.get("cursorType")
            or content.get("entryType") == "TimelineTimelineCursor"
            or content.get("itemType") == "TimelineTimelineCursor"
        ):
            details["unparsed_items"] += 1
            if entry_id:
                details["unparsed_post_ids"].append(entry_id)
                source_ids.add(entry_id)
    details["source_post_ids"] = sorted(source_ids)
    if audit is not None:
        audit.update(details)
    empty = (
        not posts
        and not details["primary_item_count"]
        and not details["unparsed_items"]
        and not details["unsupported_instructions"]
    )
    return list(posts.values()), empty


def _record_content_warnings(state: dict, details: dict) -> bool:
    """Counts describe returned-page observations, not an asserted deletion cause."""
    warning = any(
        details.get(key)
        for key in ("unavailable_items", "unparsed_items", "unsupported_instructions")
    )
    if warning:
        audit = state.setdefault(
            "source_content",
            {
                "unavailable_primary_observations": 0,
                "unparsed_primary_observations": 0,
                "unsupported_instruction_observations": 0,
                "unavailable_post_ids": [],
                "unparsed_post_ids": [],
                "coverage_warning": True,
            },
        )
        for field, key in (
            ("unavailable_primary_observations", "unavailable_items"),
            ("unparsed_primary_observations", "unparsed_items"),
            ("unsupported_instruction_observations", "unsupported_instructions"),
        ):
            audit[field] += details.get(key, 0)
        for field in ("unavailable_post_ids", "unparsed_post_ids"):
            audit[field] = sorted(set(audit[field]) | set(details.get(field, [])))
    return bool(
        details.get("unparsed_items") or details.get("unsupported_instructions")
    )


def _pause_content(state: dict) -> None:
    state["discovery_pagination"].update(
        status="paused", stop_reason="partial_source_content"
    )


def _discovery_summary(state: dict, reason: str, complete: bool) -> dict:
    return {
        "complete": complete,
        "reason": reason,
        **(
            {"source_content": state["source_content"].copy()}
            if "source_content" in state
            else {}
        ),
    }


def _record_source_page(
    state: dict,
    primary: list[Post],
    source_empty: bool,
    *,
    truncated: bool = False,
    ended: bool = False,
    source_ids: set[str] | None = None,
) -> str | None:
    audit = state.setdefault(
        "discovery_pagination",
        {
            "consecutive_source_empty_pages": 0,
            "consecutive_no_progress_pages": 0,
            "empty_page_limit": _EMPTY_PAGE_LIMIT,
            "no_progress_limit": _NO_PROGRESS_LIMIT,
            "status": "active",
        },
    )
    seen = set(state.get("discovery_source_ids", []))
    source_ids = {p.id for p in primary} if source_ids is None else source_ids
    audit["last_page_primary_count"] = len(primary)
    audit["last_page_new_source_ids"] = len(source_ids - seen)
    audit["consecutive_source_empty_pages"] = (
        audit["consecutive_source_empty_pages"] + 1 if source_empty else 0
    )
    # Item caps intentionally replay a partly consumed page. Do not classify that
    # page as no progress or remember its unread source IDs as already consumed.
    audit["consecutive_no_progress_pages"] = (
        0
        if truncated or source_ids - seen
        else audit["consecutive_no_progress_pages"] + 1
    )
    if not truncated:
        seen.update(source_ids)
        state["discovery_source_ids"] = sorted(seen)
    audit["source_ids_seen"] = len(seen)
    reason = None
    if not ended and not truncated:
        if audit["consecutive_source_empty_pages"] >= _EMPTY_PAGE_LIMIT:
            reason = "empty_page_limit"
        elif audit["consecutive_no_progress_pages"] >= _NO_PROGRESS_LIMIT:
            reason = "no_progress_limit"
    audit["status"] = "paused" if reason else ("source_end" if ended else "active")
    if reason:
        audit["stop_reason"] = reason
    else:
        audit.pop("stop_reason", None)
    return reason


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

    def _discovery_posts(
        self, payload: dict, query: dict, *, primary: list[Post] | None = None
    ) -> list[Post]:
        from .client import _within

        _instructions(payload)
        posts = (
            timeline_primary_posts(payload, captured_at_utc=now_utc())
            if primary is None
            else primary
        )
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
        pagination_state: dict = {}
        self.last_discovery_collection = _discovery_summary(
            pagination_state, "running", False
        )
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
            content_audit: dict = {}
            primary, source_empty = _source_page(payload, audit=content_audit)
            content_pause = _record_content_warnings(pagination_state, content_audit)
            pause_reason = _record_source_page(
                pagination_state,
                primary,
                source_empty,
                ended=_source_end(payload),
                source_ids=set(content_audit["source_post_ids"]),
            )
            self.last_discovery_collection = _discovery_summary(
                pagination_state, "running", False
            )
            for post in self._discovery_posts(payload, query, primary=primary):
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                yield post
                if limit and len(seen_ids) >= limit:
                    self.last_discovery_collection = _discovery_summary(
                        pagination_state, "item_limit", False
                    )
                    return
            pages += 1
            if content_pause:
                self.last_discovery_collection = _discovery_summary(
                    pagination_state, "partial_source_content", False
                )
                return
            if _source_end(payload):
                if "source_content" in pagination_state:
                    self.last_discovery_collection = _discovery_summary(
                        pagination_state, "partial_source_content", False
                    )
                    return
                if query["kind"] == "post_quotes" and not seen_ids:
                    raise APIError(
                        "Quote query is unverified: no matching quoted post IDs"
                    )
                self.last_discovery_collection = _discovery_summary(
                    pagination_state, "source_end", True
                )
                return
            if pause_reason:
                self.last_discovery_collection = _discovery_summary(
                    pagination_state, pause_reason, False
                )
                raise APIError(
                    f"Discovery pagination paused: {pause_reason}; coverage remains incomplete"
                )
            cursor = bottom_cursor(payload)
            if not cursor:
                raise APIError("Discovery pagination omitted its bottom cursor")
            if max_pages and pages >= max_pages:
                self.last_discovery_collection = _discovery_summary(
                    pagination_state, "page_limit", False
                )
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
        *,
        retry_stalled: bool = False,
    ) -> CollectionResult:
        if not isinstance(retry_stalled, bool):
            raise ValueError("retry_stalled must be a boolean")
        store = PostStore(output_dir, query=query)
        if store.complete:
            return store.finish(complete=True, reason=store.state["reason"])
        audit = store.state.get("discovery_pagination", {})
        if (
            audit.get("status") == "paused"
            and audit.get("stop_reason") in _PAUSE_REASONS
        ):
            if not retry_stalled:
                return store.finish(complete=False, reason=audit["stop_reason"])
            audit.update(
                consecutive_source_empty_pages=0,
                consecutive_no_progress_pages=0,
                status="active",
            )
            audit.pop("stop_reason", None)
            store.log(operation="retry_stalled_discovery", cursor=store.cursor)
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
            raw_path = store.archive_response(
                payload, operation=operation, variables=variables
            )
            try:
                content_audit: dict = {}
                primary, source_empty = _source_page(payload, audit=content_audit)
                content_pause = _record_content_warnings(store.state, content_audit)
                if any(
                    content_audit.get(k)
                    for k in (
                        "unavailable_items",
                        "unparsed_items",
                        "unsupported_instructions",
                    )
                ):
                    store.log(
                        operation="discovery_content_warning",
                        source_raw_path=str(raw_path.relative_to(store.output_dir)),
                        unavailable_count=content_audit["unavailable_items"],
                        unparsed_count=content_audit["unparsed_items"],
                        unsupported_instruction_count=content_audit[
                            "unsupported_instructions"
                        ],
                        unavailable_post_ids=content_audit["unavailable_post_ids"],
                        unparsed_post_ids=content_audit["unparsed_post_ids"],
                    )
                posts = self._discovery_posts(payload, query, primary=primary)
                if query["kind"] == "search_query" and (
                    query["since"] or query["until"]
                ):
                    from .client import _within

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
            pause_reason = _record_source_page(
                store.state,
                primary,
                source_empty,
                truncated=truncated or content_pause,
                ended=ended,
                source_ids=set(content_audit["source_post_ids"]),
            )
            if content_pause:
                _pause_content(store.state)
            store.state.pop("rate_reset_at", None)
            store.append_page(
                posts,
                cursor if truncated or unavailable or content_pause else next_cursor,
            )
            pages += 1
            if unavailable:
                return store.finish(
                    complete=False, reason="repost_activity_unavailable"
                )
            if content_pause:
                return store.finish(complete=False, reason="partial_source_content")
            if truncated:
                return store.finish(complete=False, reason="item_limit")
            if ended:
                if "source_content" in store.state:
                    _pause_content(store.state)
                    return store.finish(complete=False, reason="partial_source_content")
                if query["kind"] == "post_quotes" and not store.count:
                    return store.finish(complete=False, reason="query_unverified")
                return store.finish(complete=True, reason="source_end")
            if pause_reason:
                return store.finish(complete=False, reason=pause_reason)
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
        retry_stalled: bool = False,
    ) -> CollectionResult:
        effective, since, until = _query_options(query, since, until, max_pages, limit)
        spec = {
            "kind": "search_query",
            "query": query.strip(),
            "effective_query": effective,
            "since": since,
            "until": until,
        }
        return self._save_discovery(
            spec, output_dir, max_pages, limit, retry_stalled=retry_stalled
        )

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
        retry_stalled: bool = False,
    ) -> CollectionResult:
        spec = self._quote_query(post_id_or_url, since, until, max_pages, limit)
        return self._save_discovery(
            spec, output_dir, max_pages, limit, retry_stalled=retry_stalled
        )

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
        retry_stalled: bool = False,
    ) -> CollectionResult:
        spec = self._repost_query(handle, since, until, max_pages, limit)
        return self._save_discovery(
            spec, output_dir, max_pages, limit, retry_stalled=retry_stalled
        )
