"""Parse X's public GraphQL response objects without depending on twikit."""

from datetime import datetime, timezone
from typing import Any, Iterator

from .errors import ParseError
from .models import Post, Profile


def _walk(value: Any) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _count(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return (
            datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
            .astimezone(timezone.utc)
            .isoformat()
        )
    except ValueError:
        return None


def parse_profile(data: dict, *, captured_at_utc: str | None = None) -> Profile:
    """Parse a UserByScreenName result or its inner user result."""
    result = data.get("data", {}).get("user", {}).get("result", data)
    legacy = result.get("legacy") or {}
    core = result.get("core") or {}
    relationships = result.get("relationship_counts") or {}
    tweet_counts = result.get("tweet_counts") or {}
    user_id = result.get("rest_id")
    handle = legacy.get("screen_name") or core.get("screen_name")
    if not user_id or not handle:
        raise ParseError("X user response has no user ID or screen name")
    return Profile(
        id=str(user_id),
        handle=handle,
        name=legacy.get("name") or core.get("name") or handle,
        description=legacy.get("description")
        or (result.get("profile_bio") or {}).get("description"),
        created_at_utc=_utc(legacy.get("created_at") or core.get("created_at")),
        followers_count=_count(
            legacy.get("followers_count", relationships.get("followers"))
        ),
        following_count=_count(
            legacy.get("friends_count", relationships.get("following"))
        ),
        posts_count=_count(legacy.get("statuses_count", tweet_counts.get("tweets"))),
        media_count=_count(legacy.get("media_count", tweet_counts.get("media_tweets"))),
        verified=bool(result.get("is_blue_verified") or legacy.get("verified"))
        if "is_blue_verified" in result or "verified" in legacy
        else None,
        url=f"https://x.com/{handle}",
        captured_at_utc=captured_at_utc,
    )


def parse_post(data: dict, *, captured_at_utc: str | None = None) -> Post | None:
    """Parse a Tweet or TweetWithVisibilityResults result."""
    if data.get("__typename") == "TweetTombstone":
        return None
    if "tweet" in data:
        data = data["tweet"]
    legacy = data.get("legacy") or {}
    post_id = data.get("rest_id") or legacy.get("id_str")
    user_result = data.get("core", {}).get("user_results", {}).get("result") or {}
    author = user_result.get("legacy") or user_result.get("core") or {}
    author_id = legacy.get("user_id_str") or user_result.get("rest_id")
    created = _utc(legacy.get("created_at"))
    if not post_id or not author_id or not created:
        return None
    handle = author.get("screen_name") or ""
    note = (
        (data.get("note_tweet") or {}).get("note_tweet_results", {}).get("result", {})
    )
    text = note.get("text") or legacy.get("full_text") or ""
    entities = legacy.get("entities") or {}
    extended = legacy.get("extended_entities") or {}
    media_urls: list[str] = []
    for media in extended.get("media", entities.get("media", [])):
        if media.get("media_url_https"):
            media_urls.append(media["media_url_https"])
        variants = (media.get("video_info") or {}).get("variants") or []
        mp4 = [
            variant
            for variant in variants
            if variant.get("content_type") == "video/mp4"
        ]
        if mp4:
            best = max(mp4, key=lambda variant: variant.get("bitrate") or 0)
            if best.get("url"):
                media_urls.append(best["url"])
    outbound = [
        item["expanded_url"]
        for item in entities.get("urls", [])
        if item.get("expanded_url")
    ]
    hashtags = [
        item["text"] for item in entities.get("hashtags", []) if item.get("text")
    ]
    mentions = [
        item["screen_name"]
        for item in entities.get("user_mentions", [])
        if item.get("screen_name")
    ]
    repost_result = (
        legacy.get("retweeted_status_result")
        or data.get("retweeted_status_result")
        or {}
    )
    repost = repost_result.get("result") or {}
    repost = repost.get("tweet", repost)
    quoted = data.get("quoted_status_result") or {}
    quoted = (
        quoted.get("result") or (quoted.get("tweet_results") or {}).get("result") or {}
    )
    quoted = quoted.get("tweet", quoted)

    def referenced_author(result: dict) -> str | None:
        author = (result.get("core") or {}).get("user_results", {}).get("result") or {}
        identifier = (result.get("legacy") or {}).get("user_id_str") or author.get(
            "rest_id"
        )
        return str(identifier) if identifier else None

    return Post(
        id=str(post_id),
        author_id=str(author_id),
        author_handle=handle,
        created_at_utc=created,
        text=text,
        url=f"https://x.com/{handle}/status/{post_id}"
        if handle
        else f"https://x.com/i/status/{post_id}",
        language=legacy.get("lang"),
        conversation_id=legacy.get("conversation_id_str"),
        in_reply_to_id=legacy.get("in_reply_to_status_id_str"),
        quoted_post_id=legacy.get("quoted_status_id_str") or quoted.get("rest_id"),
        reposted_post_id=repost.get("rest_id"),
        reply_count=_count(legacy.get("reply_count")),
        repost_count=_count(legacy.get("retweet_count")),
        like_count=_count(legacy.get("favorite_count")),
        view_count=_count((data.get("views") or {}).get("count")),
        quote_count=_count(legacy.get("quote_count")),
        bookmark_count=_count(legacy.get("bookmark_count")),
        media_urls=list(dict.fromkeys(media_urls)),
        outbound_urls=list(dict.fromkeys(outbound)),
        hashtags=list(dict.fromkeys(hashtags)),
        mentions=list(dict.fromkeys(mentions)),
        captured_at_utc=captured_at_utc,
        in_reply_to_user_id=str(legacy["in_reply_to_user_id_str"])
        if legacy.get("in_reply_to_user_id_str")
        else None,
        quoted_author_id=referenced_author(quoted),
        reposted_author_id=referenced_author(repost),
        reposted_created_at_utc=_utc((repost.get("legacy") or {}).get("created_at")),
        raw_json=data,
        text_source="note_tweet" if note.get("text") else "legacy_full_text",
        text_complete=True
        if note.get("text")
        else (False if legacy.get("truncated") else None),
    )


def timeline_posts(data: dict, *, captured_at_utc: str | None = None) -> list[Post]:
    """Read tweet_results in user, search, or conversation timelines."""
    posts: dict[str, Post] = {}
    for node in _walk(data.get("data", data)):
        tweet_results = node.get("tweet_results")
        if not isinstance(tweet_results, dict):
            continue
        result = tweet_results.get("result")
        if not isinstance(result, dict):
            continue
        post = parse_post(result, captured_at_utc=captured_at_utc)
        if post is not None:
            posts[post.id] = post
    return list(posts.values())


def _timeline_item_contents(data: dict) -> Iterator[dict]:
    """Yield primary timeline content, never embedded quoted post objects."""
    instructions: list[dict] = next(
        (
            node["instructions"]
            for node in _walk(data.get("data", data))
            if isinstance(node.get("instructions"), list)
        ),
        [],
    )
    for instruction in instructions:
        entries = list(instruction.get("entries", []))
        if isinstance(instruction.get("entry"), dict):
            entries.append(instruction["entry"])
        for entry in entries:
            content = entry.get("content") or {}
            if isinstance(content.get("items"), list):
                if not content["items"]:
                    yield content
                for module_item in content["items"]:
                    item = module_item.get("item") or module_item
                    content_item = item.get("itemContent") or item
                    yield {
                        **content_item,
                        "_timeline_entry_id": module_item.get(
                            "entryId", entry.get("entryId")
                        ),
                    }
            else:
                item = content.get("itemContent") or content
                if (
                    str(entry.get("entryId", "")).startswith("cursor-")
                    and item.get("value")
                    and not item.get("entryType")
                ):
                    item = {**item, "entryType": "TimelineTimelineCursor"}
                yield {**item, "_timeline_entry_id": entry.get("entryId")}
        for module_item in instruction.get("moduleItems", []):
            item = module_item.get("item") or module_item
            yield {
                **(item.get("itemContent") or item),
                "_timeline_entry_id": module_item.get("entryId"),
            }


def timeline_primary_posts(
    data: dict, *, captured_at_utc: str | None = None
) -> list[Post]:
    """Read primary entries, replacements and modules without counting quotes."""
    posts: dict[str, Post] = {}
    for content in _timeline_item_contents(data):
        result = (content.get("tweet_results") or {}).get("result")
        if isinstance(result, dict):
            post = parse_post(result, captured_at_utc=captured_at_utc)
            if post is not None:
                posts[post.id] = post
    return list(posts.values())


def bottom_cursor(data: dict) -> str | None:
    """Find the bottom pagination cursor without relying on one timeline shape."""
    for node in _walk(data.get("data", data)):
        entry_id = node.get("entryId", "")
        content = node.get("content") or {}
        if isinstance(entry_id, str) and entry_id.startswith("cursor-bottom"):
            return content.get("value")
        if content.get("cursorType") == "Bottom":
            return content.get("value")
    return None


def single_post(data: dict, *, captured_at_utc: str | None = None) -> Post:
    result = data.get("data", {}).get("tweetResult", {}).get("result")
    if not isinstance(result, dict):
        raise ParseError("X single-post response has no tweet result")
    post = parse_post(result, captured_at_utc=captured_at_utc)
    if post is None:
        raise ParseError("X post is unavailable or its response changed")
    return post
