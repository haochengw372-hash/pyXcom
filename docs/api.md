# Public API — pyXcom 0.7.1

The native discovery and network methods have passed offline tests and bounded authenticated live endpoint tests. These tests verify the exercised acquisition paths, not complete historical or network coverage.

Import `XClient`, `Profile`, `Post`, `CollectionResult`, `PyXcomError`, `AuthenticationError`, `APIError`, and `RateLimitError` from `pyxcom`.

## Client

```python
XClient(*, browser="chrome", profile=None, cookie_db=None, proxy=None,
        mirror_base=None, delay=1.0, timeout=30)
```

Use as a context manager to close connections. `profile` selects the local browser profile; it is not an X username. Manually sign in first. The client never launches a browser. Supported choices are `chrome`, `edge`, and `safari` (macOS only). For Safari omit `profile`; optionally select an accessible `Cookies.binarycookies` file with `cookie_db`. Permission denial is reported as `AuthenticationError` with macOS Full Disk Access guidance, without changing system permissions. Chrome/Edge profile selection is unchanged.

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

## Comments received by a post or reply

```python
iter_post_comments(post_id_or_url: str, *, max_depth: int = 2,
                   max_comments: int | None = 100,
                   max_pages: int | None = 20,
                   since: str | None = None, until: str | None = None) -> Iterator[Post]
get_post_comments(post_id_or_url: str, *, max_depth: int = 2,
                  max_comments: int | None = 100,
                  max_pages: int | None = 20,
                  since: str | None = None, until: str | None = None) -> list[Post]
save_post_comments(post_id_or_url: str, output_dir: str | Path, *,
                   max_depth: int = 2, max_comments: int | None = 100,
                   max_pages: int | None = 20,
                   since: str | None = None, until: str | None = None) -> CollectionResult
```

These methods expand descendants of the seed from all visible authors, including when the seed itself is a reply. They differ from `*_user_replies`, which fetch only one account's authored replies. `get` and `iter` return descendants only; `save` preserves the seed in its proper main/comment table, profiles in `users.csv`, comments in `comments.csv`, and private traversal state for resume. Ancestor context is retained in `context_posts.csv` and is not counted as descendants.

Traversal depth is relative to the seed (direct replies=1), recorded as `seed_post_id` and `seed_relative_depth`. Actual platform `root_post_id`, `parent_post_id` and `depth` remain separate; missing ancestors can leave actual `depth` unresolved. UTC `since`/`until` filter returned descendants while preserving traversal context. Comment cap is exact and cumulative per dataset; page cap applies to each call. A rate-limited save preserves its queue with `complete=false`; memory/iterator methods raise `RateLimitError`. Network/parse failures save a partial checkpoint and raise. Repeating a save with larger comment/page budgets resumes it; changing seed/depth/date scope requires a separate dataset. Completion only means visible traversal within the requested depth ended, not an exhaustive historical thread. CLI comment/page caps accept `all` for Python `None`; depth must remain a positive integer.

For offline validation of `post_comments`, UTC date scope applies to analysis descendants, while the seed and explicitly marked context can lie outside it. These retained records still participate in identifier, relationship and artifact-integrity checks. The exemption does not apply to other collection types; a descendant missing `observation_role` is treated as analysis for this date check.

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

## Native discovery

```python
iter_search_query(query, *, since=None, until=None, max_pages=None, limit=None) -> Iterator[Post]
get_search_query(query, *, since=None, until=None, max_pages=None, limit=None) -> list[Post]
save_search_query(query, output_dir, *, since=None, until=None,
                  max_pages=None, limit=None, retry_stalled=False) -> CollectionResult

iter_post_quotes(post_id_or_url, *, since=None, until=None,
                 max_pages=None, limit=None) -> Iterator[Post]
get_post_quotes(post_id_or_url, *, since=None, until=None,
                max_pages=None, limit=None) -> list[Post]
save_post_quotes(post_id_or_url, output_dir, *, since=None, until=None,
                 max_pages=None, limit=None, retry_stalled=False) -> CollectionResult

iter_user_reposts(handle, *, since=None, until=None,
                  max_pages=None, limit=None) -> Iterator[Post]
get_user_reposts(handle, *, since=None, until=None,
                 max_pages=None, limit=None) -> list[Post]
save_user_reposts(handle, output_dir, *, since=None, until=None,
                  max_pages=None, limit=None, retry_stalled=False) -> CollectionResult
```

All options are explicit and keyword-only. Dates are inclusive/exclusive UTC dates; budgets are positive integers or `None`. Native search uses X Latest, preserves query operators, and does not use the mirror. `*_post_quotes` discovers candidates using `quoted_tweet_id:` and verifies the returned target ID. Its result is a visible search sample, not the complete quote population. `*_user_reposts` parses visible repost wrappers in the account timeline, preserving the action ID/time and original target ID/time; it cannot infer missing reposts. If X returns flattened originals instead of wrappers, action times cannot be recovered; the save reports incomplete `repost_activity_unavailable` rather than substituting original publication times.

Saved discovery queries retain original/effective query provenance, raw response pages and resumable cursors. Save scopes cannot be changed within the same directory. Use `complete` and `reason` to report caps, source end, rate limits or stalled pagination. A visible source end does not establish historical exhaustiveness. For bounded native searches, inspect `manifest.json.date_scope` alongside `complete` and `reason`: the source may return out-of-window posts even when the exact query includes `since:` and `until:`. Local filtering uses UTC `[since, until)`, and all-out-of-window pages can still be followed by valid results. A filtered zero with `source_returned_posts_outside_requested_dates` is not evidence of an empty historical population. The package does not infer a source timezone or modify research query terms to compensate.

Native discovery pauses with `complete=false` and `reason="empty_page_limit"` after 3 consecutive source-empty pages, or `reason="no_progress_limit"` after 5 consecutive pages without new primary source IDs. The counters persist across `max_pages` calls in `.pyxcom/state.json` and are summarized in `manifest.json.discovery_pagination`. Source emptiness is checked before date/quote/author filtering; fresh tweets filtered to zero do not count as empty or stalled. Replacement entries and module additions are parsed as primary content. Readable tweets are retained alongside unavailable or unparsed slots, and `manifest.json.source_content` records the missingness counts and any available IDs; these slots are not source-empty pages. Known unavailable slots allow pagination to continue. Unknown item shapes or unsupported instructions pause with `partial_source_content`, preserving the request cursor and readable records. Reaching source end with a content warning also yields `complete=false`; a different stop reason, such as `repeated_cursor`, can coexist with the warning. The collector does not infer whether an unavailable slot was deleted, protected or hidden.

A pause is a heuristic coverage warning, not a source-end or historical-completeness claim. Repeating a paused save does not fetch more pages by default, even if its budget increases. To probe again, explicitly use `retry_stalled=True` on `save_search_query`, `save_post_quotes` or `save_user_reposts` (CLI `--retry-stalled`). This clears the consecutive counters while preserving the saved cursor, raw pages, existing records and seen source IDs. A fresh source post resets the streak; another stalled run pauses again. Partly consumed pages replayed after an item cap do not count as stalled. Memory/iterator discovery raises `APIError` on empty-page and no-progress pauses; content warnings return the readable prefix and set `last_discovery_collection` with `complete=false`, the stop reason and `source_content`. Existing checkpoints without the counters begin tracking from their next request.

## Network lists and snapshots

```python
iter_followers(user_id, *, max_pages=None, limit=None) -> Iterator[Profile]
get_followers(user_id, *, max_pages=None, limit=None) -> list[Profile]
save_followers(user_id, output_dir, *, max_pages=None, limit=None,
               snapshot_id=None) -> CollectionResult

iter_following(user_id, *, max_pages=None, limit=None) -> Iterator[Profile]
get_following(user_id, *, max_pages=None, limit=None) -> list[Profile]
save_following(user_id, output_dir, *, max_pages=None, limit=None,
               snapshot_id=None) -> CollectionResult

iter_post_reposters(post_id_or_url, *, max_pages=None, limit=None) -> Iterator[Profile]
get_post_reposters(post_id_or_url, *, max_pages=None, limit=None) -> list[Profile]
save_post_reposters(post_id_or_url, output_dir, *, max_pages=None, limit=None,
                    snapshot_id=None) -> CollectionResult
```

`user_id` is a numeric ID string, resolved with `get_user(handle).id`. Network methods have no historical date filter. Saves write `follow_edges.csv` or `reposters.csv`, `users.csv`, `user_snapshots.csv`, and `network_manifest.json`. Network and post collections must use separate output directories to protect their different table layouts. `CollectionResult.post_count` is the observed user count for these methods. Follow edges point follower → followed account. Reposter edges point reposter → original author with `target_post_id`; `action_time_utc` remains unknown because the user list does not return repost activity times. Discovery/network CLI budgets accept `all` for Python `None`.

`snapshot_id` accepts 1–128 letters, digits, underscores or hyphens. Without it, a save resumes a recoverable unfinished snapshot for that source, or creates a new snapshot after completion or a terminal bad cursor. Reusing an explicit completed ID returns that observation. History and raw pages remain under `.pyxcom/networks/`; the network manifest records completion and stop reasons per snapshot. `get`/`iter` raise `APIError`, `ParseError` or `RateLimitError` on source failures and set `last_network_collection`; budget stops return the prefix. Saves return incomplete status with resumable checkpoints for recoverable failures. Snapshot time is the time of observation, not relationship creation time. Present-day follower/following lists cannot backfill historical follow networks.

## Observation archives

Saved post collections retain page responses under `.pyxcom/raw/` and collection provenance under `.pyxcom/collection_log.jsonl`; network responses are kept under each snapshot state. Re-observing a post preserves returned engagement-count history in `metric_snapshots.csv` and `.pyxcom/metric_snapshots.jsonl`, with observations retained in `.pyxcom/observations.jsonl`, while public post tables remain deduplicated by ID. Unknown counts remain unavailable, not zero. A repeated observation does not reconstruct an earlier historical metric. Raw archives contain platform payloads, not request cookies or authorization headers.

Post datasets also export `profile_snapshots.csv` with `snapshot_id`, `user_id`, `username`, `captured_at_utc`, `time_status`, `source_file`, `source_key` and `profile_json`. The source history is retained in `.pyxcom/profile_observations.jsonl`; legacy handle-keyed `profiles.json` files remain readable. `users.csv` selects one profile per stable ID using the latest valid UTC capture, then populated-field count, then canonical JSON; missing or invalid capture times are marked unknown. `manifest.json.profile_observations` reports the policy, observation/user counts, unknown-time observations and tied latest user IDs. All differing source-labelled snapshots remain available, including renamed handles; conflicting valid account creation times or invalid nonempty creation timestamps fail. Export and validation use the same selection policy.

JSONL records are separated by LF, so U+2028, U+2029 and U+0085 inside text or nested `profile_json` strings are preserved. Appending to an existing valid record without a final LF inserts the record separator without rewriting the preceding bytes. Malformed JSON, non-object records and invalid nested profile JSON still fail strict reading; validators report invalid records. JSON checkpoints, profile views and table manifests share flushed atomic replacement, CSV replacement failures remove temporary files, and profile history is flushed before the current profile view changes. Public methods, saved query scopes, cursors and existing snapshot IDs are unchanged. See [saved-data rules](stability.md).

`post_edges.csv` exports observed reply, quote and repost edges when present. Columns are `source_post_id`, `target_post_id`, `source_user_id`, `target_user_id`, `edge_type`, `action_time_utc`, `observed_at_utc`, `target_post_available`, `target_author_resolved` and `observation_role`. Edges point from the acting user/post to the target. Unavailable targets remain in the table; unresolved target user IDs stay empty with `target_author_resolved=false`. Known but otherwise unobserved target authors have stub user rows (`profile_available=false`). Context-only records do not generate source edges. Relationship, context and metric exports are conditional on corresponding observations being present.

## Offline dataset functions

```python
export_tables(output_dir: str | Path) -> dict
validate_tables(output_dir: str | Path) -> dict
validate_collection(output_dir: str | Path) -> dict
validate_network_collection(output_dir: str | Path) -> dict
finalize_collection(output_dir: str | Path) -> CollectionResult
schema_summary(output_dir: str | Path) -> dict
write_schema_report(output_dir: str | Path) -> dict
apply_role_schema(output_dir: str | Path) -> dict
```

Export/finalization migrate legacy files with backups and regenerate public tables. Validation never fetches X data. `validate_collection` checks internal observations and public tables; `validate_tables` checks normalized relationships, source/file hashes and counts. Reports describe missing ancestry explicitly. `schema_summary` describes the internal standard Post/Profile fields, not only the normalized table columns.

Post export/finalization/schema helpers operate on post datasets. `validate_collection` automatically recognizes network datasets; `validate_network_collection` explicitly verifies network snapshot hashes, directions and provenance. To resume or regenerate network exports, repeat the corresponding network `save_*` call.

## Backward-compatible names

| Previous API | Preferred API |
| --- | --- |
| `iter_search(..., user=...)` | `iter_search_posts(..., handle=...)` |
| `get_search(..., user=...)` | `get_search_posts(..., handle=...)` |
| `save_search(..., user=...)` | `save_search_posts(..., handle=...)` |
| `save_accounts(...)` | `save_users_activity(...)` |
| `*_user_posts(..., timeline="replies")` | `*_user_replies(...)` |

No removal is scheduled. These names call the same implementation; no separate authentication flow or duplicate scraper is introduced.
