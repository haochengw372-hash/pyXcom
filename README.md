# pyXcom

**使用前请自行打开 Chrome 并登录 X。pyXcom 不会打开或控制浏览器，也不会替你登录。**

Collect public X user profiles, main posts, authored replies and keyword matches using your existing Chrome or Edge session. Outputs are three linked CSV tables: `users.csv`, `posts.csv` and `comments.csv`.

The interface follows [PykTok](https://github.com/dfreelon/pyktok)'s approachable get/save convention, with explicit parameters and resumable datasets. pyXcom is an independent implementation; it does not depend on PykTok or twikit.

## Install

Python 3.10 or newer. From a cloned repository:

```bash
python -m pip install .
```

Or in a development environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

This package has not been published to PyPI. The distribution name is `pyXcom`; the import and command are `pyxcom`.

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

## Python functions

Methods belong to `XClient`. Every method lists its accepted parameters explicitly.

| Task | Get into memory | Stream records | Save a dataset |
| --- | --- | --- | --- |
| User profile | `get_user(handle)` | — | — |
| Post by ID or URL | `get_post(post_id_or_url)` | — | — |
| Multiple post IDs | `get_posts(post_ids)` | — | — |
| Authored main posts | `get_user_posts(handle, ...)` | `iter_user_posts(handle, ...)` | `save_user_posts(handle, output_dir, ...)` |
| Authored replies | `get_user_replies(handle, ...)` | `iter_user_replies(handle, ...)` | `save_user_replies(handle, output_dir, ...)` |
| Replies below a main post | `get_post_comments(post_id_or_url, ...)` | `iter_post_comments(post_id_or_url, ...)` | `save_post_comments(post_id_or_url, output_dir, ...)` |
| Keyword matches | `get_search_posts(keyword, ...)` | `iter_search_posts(keyword, ...)` | `save_search_posts(keyword, output_dir, ...)` |
| Main posts + replies | — | — | `save_user_activity(handle, output_dir, ...)` |
| Multiple accounts | — | — | `save_users_activity(handles, output_dir, ...)` |

- `get_*` returns a `Profile`, `Post`, or `list[Post]`; it does not write a dataset.
- `iter_*` returns an iterator. Requests happen as it is consumed.
- `save_*` writes the standard dataset and returns `CollectionResult` with `output_dir`, `post_count`, `pages_fetched`, `complete`, and `reason`. `post_count` counts **all collected observations**, including replies; use the output manifest for separate table counts.
- Parameters: `handle` for one account, `handles` for several, `keyword` for search, `output_dir` for saved datasets, and `since`/`until` for dates. Options are keyword-only.
- Dates are UTC: `since` inclusive, `until` exclusive. `max_pages` and `limit` must be positive integers or `None`. Saved timeline/mirror `limit` is a page-boundary stopping threshold, so a final page can exceed it; iterator limits are exact.

See [API reference](docs/api.md) for signatures, return types and compatibility names.

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

## Collect a main post and nested comments

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

The root belongs in `posts.csv`, replies from **all visible authors** in `comments.csv`, and author profiles in `users.csv`. The root must be a main post. Level 1 replies directly to it; level 2 replies to those comments. Quoted content, unrelated recommendations and comments beyond the requested depth are excluded. Profile details absent from the response remain unavailable.

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

`user_replies` means replies **authored by the selected account**. Use `*_post_comments` to collect replies below a particular main post from all visible authors, including nested replies. Neither endpoint establishes full conversation coverage.

CSV conventions: UTF-8 with BOM, snake_case columns, string IDs (import as text in Excel), JSON arrays for list fields, empty values for unavailable scalars, UTC timestamp fields. Users without a saved profile have `profile_available=false`. Engagement counts are snapshots; unknown values are not replaced with zero.

If source records include pure reposts, an additional `interactions.csv` preserves them. The current authored-account collector does not establish complete repost activity. `manifest.json` records schema/layout versions, table counts, collection scope, relationship coverage and hashes. `.pyxcom/` must be retained for resuming; the three CSV files can be shared independently for analysis.

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
```

Add `--proxy URL` if your network requires one. Use `pyxcom COMMAND --help` for task options. Individual `user`, `post`, and `raw-post` commands accept `--output FILE` for a JSON file.

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

pyXcom is released under the [MIT License](LICENSE). Copyright © 2026 Haocheng Wang.

## Citation

If you use pyXcom in a paper, thesis, dataset, or other research output, please cite the software and report the version used. GitHub's **Cite this repository** menu reads the machine-readable [CITATION.cff](CITATION.cff).

**Suggested reference**

Wang, H. (2026). *pyXcom: Structured and auditable X data collection for communication research* (Version 0.6.0) [Computer software]. https://github.com/haochengw372-hash/pyXcom

**BibTeX**

```bibtex
@software{wang2026pyxcom,
  author  = {Wang, Haocheng},
  title   = {{pyXcom}: Structured and Auditable X Data Collection for Communication Research},
  year    = {2026},
  version = {0.6.0},
  url     = {https://github.com/haochengw372-hash/pyXcom}
}
```

For reproducible reporting, also describe the collection dates, account or keyword scope, comment-depth limits, package version, and coverage/stop reasons recorded in the output manifest. No DOI or published-paper citation is currently assigned; this reference cites the software itself. Citation is appreciated and does not add a condition to the MIT license.
