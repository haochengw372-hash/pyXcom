# pyXcom

**使用前请自行打开 Chrome、Edge 或 Safari 并登录 X。pyXcom 不会打开或控制浏览器，也不会替你登录。**

Collect public X user profiles, posts, replies, search results and observed network relationships using your existing Chrome, Edge or Safari session. Post datasets contain three linked CSV tables: `users.csv`, `posts.csv` and `comments.csv`; network datasets have separate relationship and snapshot tables.

Version 0.7.0 adds native search, quote discovery, repost activity and follower/following snapshots. These capabilities have passed offline tests and bounded authenticated live endpoint tests. Collection remains limited to results returned by X; the tests do not establish complete historical or network coverage.

The interface follows [PykTok](https://github.com/dfreelon/pyktok)'s approachable get/save convention, with explicit parameters and resumable datasets. pyXcom is an independent implementation; it does not depend on PykTok or twikit.

## Install

Python 3.10 or newer:

```bash
python -m pip install pyXcom
```

From a cloned repository, use `python -m pip install .`.

Or in a development environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

The distribution name is `pyXcom`; the import and command are `pyxcom`.

## Quick start

1. Open Chrome yourself and sign in to X.
2. Find your profile at `chrome://version` → **Profile Path**. Use its folder name, such as `Default` or `Profile 3`.
3. Run:

```python
from pyxcom import XClient

with XClient(profile="Default") as client:
    user = client.get_user("thsottiaux")
    print(user.name, user.followers_count)

    result = client.save_user_activity(
        "thsottiaux",
        output_dir="output/tibo_year",
        since="2025-09-29",
        until="2026-09-30",
        max_pages=2,
    )
    print(result.to_dict())
```

Repeat the same call and output directory to resume. `max_pages=2` fetches at most two pages **per timeline per call**. Remove this option to continue paging until the source ends or the date boundary is reached. A saved partial result reports its stop reason.

Optional constructor settings include `browser="edge"`, `cookie_db=...`, `proxy=...`, `delay=1.0`, and `timeout=30`. Cookies are read into memory and sent only to X. No passwords or exported cookie files are needed.

### Safari on macOS

Use an existing Safari login without launching a browser:

```python
with XClient(browser="safari") as client:
    profile = client.get_user("OpenAI")
```

```bash
pyxcom user OpenAI --browser safari
```

Safari reads `Cookies.binarycookies` using the already included `browser-cookie3`. Omit `profile`: Safari profile selection is not supported by this adapter. For storage outside the supported default locations, use `cookie_db="/accessible/path/Cookies.binarycookies"` or CLI `--cookie-db`. Chrome and Edge still use their existing profile/database options.

If macOS denies file access, pyXcom reports an `AuthenticationError` identifying the permission issue. The user may authorize the application running pyXcom (such as their terminal or Codex) in **System Settings → Privacy & Security → Full Disk Access**, then retry. The package does not change permissions or fall back past denied access. Missing storage, an unsupported cookie-file format, and absence of the required X login cookies are reported separately. This does not establish that an expired session is still accepted by X.

## Python functions

Methods belong to `XClient`. Every method lists its accepted parameters explicitly.

| Task | Get into memory | Stream records | Save a dataset |
| --- | --- | --- | --- |
| User profile | `get_user(handle)` | — | — |
| Post by ID or URL | `get_post(post_id_or_url)` | — | — |
| Multiple post IDs | `get_posts(post_ids)` | — | — |
| Authored main posts | `get_user_posts(handle, ...)` | `iter_user_posts(handle, ...)` | `save_user_posts(handle, output_dir, ...)` |
| Authored replies | `get_user_replies(handle, ...)` | `iter_user_replies(handle, ...)` | `save_user_replies(handle, output_dir, ...)` |
| Replies below a post or reply | `get_post_comments(post_id_or_url, ...)` | `iter_post_comments(post_id_or_url, ...)` | `save_post_comments(post_id_or_url, output_dir, ...)` |
| Keyword matches | `get_search_posts(keyword, ...)` | `iter_search_posts(keyword, ...)` | `save_search_posts(keyword, output_dir, ...)` |
| Native cross-user query | `get_search_query(query, ...)` | `iter_search_query(query, ...)` | `save_search_query(query, output_dir, ...)` |
| Quotes of a post | `get_post_quotes(post_id_or_url, ...)` | `iter_post_quotes(post_id_or_url, ...)` | `save_post_quotes(post_id_or_url, output_dir, ...)` |
| Account repost activity | `get_user_reposts(handle, ...)` | `iter_user_reposts(handle, ...)` | `save_user_reposts(handle, output_dir, ...)` |
| Followers / following | `get_followers(user_id, ...)` / `get_following(user_id, ...)` | `iter_followers(user_id, ...)` / `iter_following(user_id, ...)` | `save_followers(user_id, output_dir, ...)` / `save_following(user_id, output_dir, ...)` |
| Reposter user list | `get_post_reposters(post_id_or_url, ...)` | `iter_post_reposters(post_id_or_url, ...)` | `save_post_reposters(post_id_or_url, output_dir, ...)` |
| Main posts + replies | — | — | `save_user_activity(handle, output_dir, ...)` |
| Multiple accounts | — | — | `save_users_activity(handles, output_dir, ...)` |

- `get_*` returns a `Profile`, `Post`, `list[Post]`, or network `list[Profile]`; it does not write a dataset.
- `iter_*` returns an iterator. Requests happen as it is consumed.
- `save_*` returns `CollectionResult` with `output_dir`, `post_count`, `pages_fetched`, `complete`, and `reason`. For post datasets `post_count` counts unique records, including replies; for network datasets this compatibility field counts observed users. Consult each manifest for table counts and coverage.
- Parameters: `handle` for one account, `handles` for several, `keyword` for search, `output_dir` for saved datasets, and `since`/`until` for dates. Options are keyword-only.
- Dates are UTC: `since` inclusive, `until` exclusive. `max_pages` and `limit` must be positive integers or `None`. Saved timeline/mirror `limit` is a page-boundary stopping threshold, so a final page can exceed it; iterator limits are exact.

See [API reference](https://github.com/haochengw372-hash/pyXcom/blob/main/docs/api.md) for signatures, return types and compatibility names.

### Search one account

```python
with XClient(profile="Default") as client:
    matches = client.get_search_posts(
        "reset", handle="thsottiaux",
        since="2025-09-29", until="2026-09-30", limit=20,
    )
    result = client.save_search_posts(
        "reset", output_dir="output/tibo_reset",
        handle="thsottiaux", since="2025-09-29", until="2026-09-30",
    )
```

Direct account search scans main posts and authored replies, then matches a literal word/phrase without case sensitivity. Matching posts do not represent deduplicated real-world events. Cross-user search requires an explicit `mirror_base`; it sends search terms to that mirror and retrieves discovered post details from X. No mirror is enabled by default.

### Native search, quotes and repost activity

`*_search_query` submits X search syntax to the native Latest search endpoint; it does not require or use a mirror. `*_search_posts` retains its account/literal behavior above. Use a separate output directory for each query or source:

```python
with XClient(profile="Default") as client:
    result = client.save_search_query(
        '("usage reset" OR "rate limit reset") (OpenAI OR Codex)',
        "output/reset_search", since="2025-09-29", until="2026-09-30",
        max_pages=2, limit=100,
    )
    print(result.complete, result.reason)
    client.save_post_quotes("1973931546550894681", "output/reset_quotes", max_pages=2)
    client.save_user_reposts("thsottiaux", "output/tibo_reposts", max_pages=2)
```

Quote discovery uses native search followed by a check of `quoted_post_id`; zero matches do not establish zero quotes. Repost activity is collected from visible account timeline wrappers, preserving the repost ID and its action timestamp separately from the original post ID/time. A flattened timeline that only returns originals cannot establish action times and reports `repost_activity_unavailable` with an incomplete result. It is not a complete historical repost archive. Search sorting, visibility and pagination remain controlled by X.

Discovery pauses when the source keeps changing cursors without useful pagination: 3 consecutive cursor-only/empty pages produce `empty_page_limit`, and 5 pages without new primary source IDs produce `no_progress_limit`. Both are incomplete results. Counters persist across save/resume calls and appear in `manifest.json.discovery_pagination`; tweets excluded by date or quote filters are still source content, so a filtered zero is not an empty-page signal.

Paused saves retain raw responses and the last cursor. Calling the same save normally preserves the pause without more requests. If you deliberately want to probe the source later, repeat it with `retry_stalled=True` (CLI `--retry-stalled`); this resets the streak without discarding records, raw archives or the cursor. These thresholds are a conservative stopping heuristic and do not prove all historical posts were collected. A fresh query with genuine new source posts continues normally. See [API reference](https://github.com/haochengw372-hash/pyXcom/blob/main/docs/api.md) for the precise stopping and retry contract.

### Follow networks and reposter lists

Resolve a handle with `get_user(handle).id`, then pass that stable numeric ID to follower/following methods:

```python
with XClient(profile="Default") as client:
    user_id = client.get_user("thsottiaux").id
    client.save_followers(user_id, "output/tibo_network", max_pages=2, snapshot_id="wave-1")
    client.save_following(user_id, "output/tibo_network", max_pages=2, snapshot_id="wave-1")
    client.save_post_reposters("1973931546550894681", "output/reset_reposters", max_pages=2)
```

Follow edges point from follower to followed account. `follow_edges.csv`, `reposters.csv`, `user_snapshots.csv` and `network_manifest.json` preserve observed relationships, user profiles and coverage per snapshot. A reposter list identifies who visibly reposted a post, but its `action_time_utc` is unknown. Use account repost activity when the wrapper supplies an action time; never substitute the original post time.

Network saves resume a recoverable unfinished snapshot. Once completed or stopped by a terminal bad cursor, calling again without `snapshot_id` creates a new observation; explicitly reusing a completed ID returns that saved snapshot. Memory/iterator methods raise endpoint, parse or rate-limit errors and retain coverage in `last_network_collection`; hitting a budget returns the observed prefix. Saves return an incomplete result and preserve checkpoints on source failures. Network and post collections require separate output directories to protect their different `users.csv` exports. Follow observations cannot establish when a relationship began or reconstruct a historical follow network. Keep collection dates in the analysis. See [examples/research_networks.py](examples/research_networks.py) for a small collection recipe; importing it does not collect data.

### Collect several accounts

```python
with XClient(profile="Default") as client:
    result = client.save_users_activity(
        ["OpenAI", "AnthropicAI", "thsottiaux", "sama", "alexalbert__", "bcherny"],
        output_dir="output/ai_year",
        since="2025-09-29", until="2026-09-30",
        pages_per_round=5, rounds=1,
    )
```

Repeat to resume. `rounds=0, wait_on_rate_limit=True` keeps running and waits for rate windows when needed. `progress=callback` receives status dictionaries.

## Collect a post or reply and nested comments

```python
with XClient(profile="Default") as client:
    result = client.save_post_comments(
        "1973931546550894681",
        output_dir="output/tibo_conversation",
        max_depth=2,
        max_comments=100,
        max_pages=20,
    )
    print(result.complete, result.reason)
```

```bash
pyxcom post-comments 1973931546550894681 --profile Default \
  --max-depth 2 --max-comments 100 --max-pages 20 \
  --output-dir output/tibo_conversation
```

The seed can be a main post or a reply. Level 1 replies directly to that seed; level 2 replies to those comments. Main seeds belong in `posts.csv`; reply seeds remain replies in `comments.csv`. `seed_post_id` and `seed_relative_depth` describe this branch; `root_post_id`, `parent_post_id` and `depth` retain actual conversation relationships and do not relabel a reply as a platform root. Ancestors returned as context are stored in `context_posts.csv`, not counted as collected descendants. `since`/`until` filter descendant timestamps; traversal retains the ancestry needed to reach in-window descendants. Quoted content, unrelated recommendations and comments beyond the requested depth are excluded. Profile details absent from the response remain unavailable.

Offline `validate_collection` checks the UTC window for analysis descendants. The requested seed and records explicitly marked `observation_role="context"` can be outside that window because they preserve relationships; their IDs, saved artifacts and hashes are still checked. This exception applies only to `post_comments`, not to searches or account timelines. Older records without an observation role remain subject to date checks unless they are the requested seed.

`max_comments` is an exact total comment cap, excluding the root. `max_pages` caps conversation requests per call. Repeat the same root/depth/output to resume saved branches and cursors; raise the comment cap to continue a capped dataset. Pending page records are retained, so stopping midway through a page does not skip them. Changing depth requires a separate dataset. Python accepts `None` for unlimited comment/page budgets; depth must be positive.

Stop reasons include `comment_limit`, `page_limit`, `rate_limited`, `partial_conversation`, and `visible_source_end`. `complete=true` means the visible queue was exhausted **within the requested depth**, not that every comment on X was recovered. Missing ancestors and stalled pagination are reported as partial. `iter_post_comments` / `get_post_comments` return reply records without writing a dataset; use `save_post_comments` when you need coverage status and resumability.

## Output

```text
output/tibo_year/
├── users.csv
├── posts.csv
├── comments.csv
├── manifest.json
└── .pyxcom/             # Checkpoints, observations, source streams and reports
```

| Table | Unit | Main keys |
| --- | --- | --- |
| `users.csv` | One user | `user_id`, `username`, `display_name` |
| `posts.csv` | One original or quote main post | `post_id`, `author_id`, `post_type`, `quoted_post_id` |
| `comments.csv` | One reply at any observed depth | `comment_id`, `author_id`, `root_post_id`, `parent_post_id`, `depth` |

Replies to oneself remain comments. A quote within a reply retains its quote link in `comments.csv`. Direct replies are depth 1; replies to those are depth 2. Missing ancestry leaves `depth` empty with a `depth_status`; `parent_in_dataset` and `root_in_dataset` indicate observed relationships. Do not interpret missing parents as first-level replies.

`user_replies` means replies **authored by the selected account**. Use `*_post_comments` to collect replies below a particular post or reply from all visible authors, including nested replies. Neither endpoint establishes full conversation coverage.

CSV conventions: UTF-8 with BOM, snake_case columns, string IDs (import as text in Excel), JSON arrays for list fields, empty values for unavailable scalars, UTC timestamp fields. Users without a saved profile have `profile_available=false`. Engagement counts are snapshots; unknown values are not replaced with zero.

If source records include pure reposts, an additional `interactions.csv` preserves them. The current authored-account collector does not establish complete repost activity. `manifest.json` records schema/layout versions, table counts, collection scope, relationship coverage and hashes. `.pyxcom/` must be retained for resuming; the three CSV files can be shared independently for analysis.

Saved collection pages also retain platform response JSON under `.pyxcom/raw/` (network pages under their snapshot state) and query provenance in `.pyxcom/collection_log.jsonl`. Repeated post observations keep returned engagement metrics in `metric_snapshots.csv`, with source observations in `.pyxcom/observations.jsonl` and metric history in `.pyxcom/metric_snapshots.jsonl`. Unreturned counts remain unavailable. These are observations at retrieval time, not historical metrics at publication. Credentials are not included in the response archive.

When source records contain reply, quote or repost relationships, `post_edges.csv` exports their directions, source/target post and user IDs, action and observation times, and target availability/resolution flags. An unavailable target is retained; its user ID stays empty if unresolved. Known target authors can appear in `users.csv` as stubs with `profile_available=false`. Context-only records do not create source edges. Relationship, context and metric exports appear when observations provide the corresponding data.

## Command line

CLI task names correspond to the Python methods:

```bash
pyxcom user thsottiaux --profile Default
pyxcom user-posts thsottiaux --profile Default --output-dir output/tibo_posts
pyxcom user-replies thsottiaux --profile Default --output-dir output/tibo_replies
pyxcom user-activity thsottiaux --profile Default \
  --since 2025-09-29 --until 2026-09-30 --output-dir output/tibo_year
pyxcom search-posts reset --handle thsottiaux --profile Default \
  --since 2025-09-29 --until 2026-09-30 --output-dir output/tibo_reset
pyxcom users-activity --handles OpenAI AnthropicAI --profile Default \
  --since 2025-09-29 --until 2026-09-30 --output-dir output/ai_year
pyxcom search-query '("usage reset" OR "rate limit reset") Codex' --profile Default \
  --since 2025-09-29 --until 2026-09-30 --max-pages 2 --output-dir output/reset_search
pyxcom post-quotes 1973931546550894681 --max-pages 2 --output-dir output/reset_quotes
pyxcom user-reposts thsottiaux --max-pages 2 --output-dir output/tibo_reposts
pyxcom user-followers USER_ID --max-pages 2 --snapshot-id wave-1 --output-dir output/network
pyxcom user-following USER_ID --max-pages 2 --snapshot-id wave-1 --output-dir output/network
pyxcom post-reposters 1973931546550894681 --max-pages 2 --output-dir output/reposters
```

Add `--proxy URL` if your network requires one. Use `pyxcom COMMAND --help` for task options. Individual `user`, `post`, and `raw-post` commands accept `--output FILE` for a JSON file.

`post-comments --max-comments all --max-pages all` removes comment/page caps; `--max-depth` still requires a positive integer (default 2). New discovery/network commands also accept `all` for `--max-pages` and `--limit`. Unlimited budgets do not remove visibility or rate limits.

## Export, migration and verification

These operations work offline, without browser credentials:

```python
from pyxcom import export_tables, validate_collection, finalize_collection

export_tables("output/ai_year")
print(validate_collection("output/ai_year"))
# Rebuild an interrupted dataset from its saved observations:
finalize_collection("output/ai_year")
```

```bash
pyxcom export --output-dir output/ai_year
pyxcom validate --output-dir output/ai_year
pyxcom finalize --output-dir output/ai_year
pyxcom schema --output-dir output/ai_year
```

Opening/exporting a legacy dataset migrates its recognized internal files into `.pyxcom/`, with originals backed up under `.pyxcom/legacy/`. It publishes the three tables at the root; the previous mixed `posts.csv` is retained internally. Unrelated user files are left in place. Conflicting migration destinations fail rather than overwrite. `schema --apply` also backfills classification fields in older records; field reports live under `.pyxcom/`.

## Compatibility and limitations

Old Python names `get_search`, `iter_search`, `save_search` (with `user=`), and `save_accounts` remain supported. Prefer `*_search_posts` (with `handle=`) and `save_users_activity` for new code. `timeline="replies"` still works, but the explicit `*_user_replies` methods are clearer. Timeline outputs exclude same-author context of the other role. Resuming an older timeline applies the same rule and preserves its original observations in an internal compressed backup. Combined activity keeps both roles.

Old CLI names `profile`, `posts`, `activity`, `search`, `batch`, `--user` and dataset `--output` remain aliases. Existing code reading root `posts.csv` as a mixed table must adapt: it now contains main posts only; replies are in `comments.csv`.

X endpoints may change or impose limits. pyXcom discovers current GraphQL query IDs from X's web bundle, while response parsers still need maintenance. A completed run indicates the requested date boundary or visible source end was reached, not proof of all historical content. Deleted, protected or unavailable posts cannot be recovered. Output migration alone does not fetch comments; call `save_post_comments` to acquire them from X.


## License

pyXcom is released under the [MIT License](https://github.com/haochengw372-hash/pyXcom/blob/main/LICENSE). Copyright © 2026 Haocheng Wang.

## Citation

**Author:** Haocheng Wang, Communication University of China.

If you use pyXcom in a paper, thesis, dataset, or other research output, please cite the software and report the version used. GitHub's **Cite this repository** menu reads the machine-readable [CITATION.cff](https://github.com/haochengw372-hash/pyXcom/blob/main/CITATION.cff).

**Suggested reference**

Wang, H. (2026). *pyXcom: Structured and auditable X data collection for communication research* (Version 0.7.0) [Computer software]. https://github.com/haochengw372-hash/pyXcom

**BibTeX**

```bibtex
@software{wang2026pyxcom,
  author  = {Wang, Haocheng},
  title   = {{pyXcom}: Structured and Auditable X Data Collection for Communication Research},
  year    = {2026},
  version = {0.7.0},
  url     = {https://github.com/haochengw372-hash/pyXcom}
}
```

For reproducible reporting, also describe the collection dates, account or keyword scope, comment-depth limits, package version, and coverage/stop reasons recorded in the output manifest. No DOI or published-paper citation is currently assigned; this reference cites the software itself. Citation is appreciated and does not add a condition to the MIT license.

For longitudinal profile comparisons use `user_snapshots.csv`; `users.csv` consolidates known profile fields. Canonical post tables keep the first saved observation for each ID; later returned text and metrics remain in observation archives and metric snapshots.

Offline development checks: `PYTHONPATH=src python -m pytest -q` and `ruff check src tests examples`. The source distribution includes tests; testing tools are development-only, not runtime dependencies.

Live validation: current X SearchTimeline/Followers use POST read queries; Retweeters is discovered from public lazy-loaded TweetActivity assets. If a search source returns posts outside explicit dates, they are excluded locally and `manifest.json.date_scope` records the mismatch. `date_scope.returned_observations` counts source-page observations and `out_of_window_observations` counts those rejected by the local UTC filter. Pages whose records are all out of window do not stop pagination by themselves. An empty bounded result with this warning does not establish historical absence; even `complete=true, reason="source_end"` describes only the visible endpoint's pagination, not historical completeness or proof that no in-window posts exist. Request dates and query operators are preserved rather than silently shifted to compensate for source behavior. Native reposts generate only repost edges; embedded quote/reply context is not attributed as another action by the reposter.
