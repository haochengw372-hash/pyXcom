# Public API — pyXcom 0.5

Import `XClient`, `Profile`, `Post`, `CollectionResult`, `PyXcomError`, `AuthenticationError`, `APIError`, and `RateLimitError` from `pyxcom`.

## Client

```python
XClient(*, browser="chrome", profile=None, cookie_db=None, proxy=None,
        mirror_base=None, delay=1.0, timeout=30)
```

Use as a context manager to close connections. `profile` selects the local browser profile; it is not an X username. Manually sign in first. The client never launches a browser.

## Lookup methods

```python
get_user(handle: str) -> Profile
get_post(post_id_or_url: str) -> Post
get_posts(post_ids: list[str]) -> list[Post]
get_raw_post(post_id_or_url: str) -> dict
get_raw_user_timeline(handle: str, *, timeline="posts", cursor=None) -> dict
```

`get_posts` can return fewer items when X does not return every requested ID. Raw methods return the platform response for advanced use and have no stable field schema.

## Account content

```python
iter_user_posts(handle, *, timeline="posts", since=None, until=None,
                max_pages=None, limit=None) -> Iterator[Post]
get_user_posts(handle, *, timeline="posts", since=None, until=None,
               max_pages=None, limit=None) -> list[Post]
save_user_posts(handle, output_dir, *, timeline="posts", since=None, until=None,
                max_pages=None, limit=None) -> CollectionResult

iter_user_replies(handle, *, since=None, until=None,
                  max_pages=None, limit=None) -> Iterator[Post]
get_user_replies(handle, *, since=None, until=None,
                 max_pages=None, limit=None) -> list[Post]
save_user_replies(handle, output_dir, *, since=None, until=None,
                  max_pages=None, limit=None) -> CollectionResult

save_user_activity(handle, output_dir, *, since=None, until=None,
                   max_pages=None) -> CollectionResult
save_users_activity(handles, output_dir, *, since, until, pages_per_round=5,
                    rounds=1, wait_on_rate_limit=False, progress=None) -> CollectionResult
```

`handle: str`; `handles: list[str]`; `output_dir: str | Path`. Dates are `str | None` in YYYY-MM-DD UTC. `max_pages` and `limit` are positive `int | None`. `progress` is an optional callable taking a dictionary. `get`/`iter` start a fresh traversal; `save` resumes the same dataset query. `limit` on a saved timeline/mirror search is checked after a page; it may overshoot. Account activity and multi-account saves intentionally have no total-record `limit`.

`user_posts` defaults to original/quote main posts. `user_replies` returns authored replies, including self-replies. Neither is an API to retrieve a main post's entire received comment tree. `timeline` is a compatibility option for selecting the source stream.

## Keyword search

```python
iter_search_posts(keyword, *, handle=None, since=None, until=None,
                  max_pages=None, limit=None) -> Iterator[Post]
get_search_posts(keyword, *, handle=None, since=None, until=None,
                 max_pages=None, limit=None) -> list[Post]
save_search_posts(keyword, output_dir, *, handle=None, since=None, until=None,
                  max_pages=None, limit=None) -> CollectionResult
```

`keyword: str`, `handle: str | None`. Direct search requires a handle and matches literal text without case sensitivity across that account's main posts and replies. Cross-account discovery requires constructor `mirror_base`. `max_pages` applies per stream for direct search.

## Offline dataset functions

```python
export_tables(output_dir: str | Path) -> dict
validate_tables(output_dir: str | Path) -> dict
validate_collection(output_dir: str | Path) -> dict
finalize_collection(output_dir: str | Path) -> CollectionResult
schema_summary(output_dir: str | Path) -> dict
write_schema_report(output_dir: str | Path) -> dict
apply_role_schema(output_dir: str | Path) -> dict
```

Export/finalization migrate legacy files with backups and regenerate public tables. Validation never fetches X data. `validate_collection` checks internal observations and public tables; `validate_tables` checks normalized relationships, source/file hashes and counts. Reports describe missing ancestry explicitly. `schema_summary` describes the internal standard Post/Profile fields, not only the normalized table columns.

## Backward-compatible names

| Previous API | Preferred API |
| --- | --- |
| `iter_search(..., user=...)` | `iter_search_posts(..., handle=...)` |
| `get_search(..., user=...)` | `get_search_posts(..., handle=...)` |
| `save_search(..., user=...)` | `save_search_posts(..., handle=...)` |
| `save_accounts(...)` | `save_users_activity(...)` |
| `*_user_posts(..., timeline="replies")` | `*_user_replies(...)` |

No removal is scheduled. These names call the same implementation; no separate authentication flow or duplicate scraper is introduced.
