# pyXcom

A Python package for collecting public posts, profiles, replies and metadata from X using an existing browser session.

pyXcom is designed for research that starts with an account, a keyword, a date range or a specific post. Its main features are:

- Get user profiles and individual posts.
- Collect an account's main posts, authored replies or both.
- Search one account for a phrase, or run a native X search query.
- Collect replies below a post or reply, including nested comments.
- Collect visible quote posts, repost activity and follower/following snapshots.
- Save linked CSV tables, raw response JSON, observation history and collection checkpoints.
- Inspect saved-data integrity and prepare verified recovery copies offline.

**Before using pyXcom, open Chrome, Edge or Safari yourself and sign in to X.** The package reads your existing session; it does not open a browser or log in for you.

X can change its endpoints or limit the content it returns. Saved results include completion status and stopping reasons so you can report the observed coverage.

> If you use pyXcom in a paper, dataset or other research output, please cite **Haocheng Wang** and report the version used. See [Citation](#citation) and [CITATION.cff](CITATION.cff).

## Installation

```bash
python -m pip install pyXcom
```

To install from a cloned repository:

```bash
python -m pip install .
```

For an editable development installation, use `python -m pip install -e .` in a virtual environment. The distribution is named `pyXcom`; the Python import and command-line program are named `pyxcom`.

## Requirements

- Python 3.10 or later.
- A local Chrome or Edge profile already signed in to X, or Safari on macOS.
- Access to that browser's cookie storage from the application running Python.

The runtime dependencies are installed by pip:

- [browser-cookie3](https://pypi.org/project/browser-cookie3/)
- [Beautiful Soup](https://pypi.org/project/beautifulsoup4/)
- [HTTPX](https://pypi.org/project/httpx/)

## Usage

Sign in to X manually, then select the browser profile you used. In Chrome, open `chrome://version` and look at **Profile Path**. Pass the folder name, such as `Default` or `Profile 3`, rather than your X username.

```python
from pyxcom import XClient

with XClient(profile="Default") as client:
    user = client.get_user("OpenAI")
    print(user.id, user.name, user.followers_count)
```

The context manager closes the HTTP connection when the block ends. Cookies remain in memory and are sent only to X; request credentials are excluded from the saved response archives.

The function names follow a get/save convention inspired by [PykTok](https://github.com/dfreelon/pyktok):

- `get_*` returns a `Profile`, a `Post`, or a list of records to memory.
- `iter_*` yields records as you consume the iterator.
- `save_*` writes a dataset and returns a `CollectionResult` with counts, completion status and a stopping reason.

Get methods return Python records, not DataFrames. See the [API reference](docs/api.md) for all signatures and compatibility names.

### Get a post or its raw JSON

Use a numeric post ID or an X post URL:

```python
with XClient(profile="Default") as client:
    post = client.get_post("1973931546550894681")
    print(post.id, post.text, post.created_at_utc)

    raw = client.get_raw_post("1973931546550894681")
```

`get_raw_post()` returns the platform's response dictionary. Use it when you need fields beyond the normalized `Post` record. Replace the example ID with a post from your study.

### Save posts from one account

To save authored main posts within a date range:

```python
with XClient(profile="Default") as client:
    result = client.save_user_posts(
        "OpenAI",
        output_dir="output/openai_posts",
        since="2025-09-29",
        until="2026-09-30",
        max_pages=2,
    )
    print(result.to_dict())
```

Dates use UTC: `since` is inclusive and `until` is exclusive. `max_pages=2` limits this invocation to two timeline pages. Repeat the same query and output directory to resume a task stopped by its page budget.

Use `save_user_replies()` for replies **written by that account**, including self-replies. To save main posts and authored replies together:

```python
with XClient(profile="Default") as client:
    result = client.save_user_activity(
        "OpenAI",
        output_dir="output/openai_activity",
        since="2025-09-29",
        until="2026-09-30",
        max_pages=2,
    )
```

For `save_user_activity()`, the page budget applies to each timeline separately. To inspect main posts without saving a dataset, use `get_user_posts()` or `iter_user_posts()`.

### Search one account

To match a literal phrase against an account's main posts and authored replies:

```python
with XClient(profile="Default") as client:
    matches = client.get_search_posts(
        "reset", handle="thsottiaux",
        since="2025-09-29", until="2026-09-30",
        max_pages=2, limit=20,
    )

    result = client.save_search_posts(
        "reset", output_dir="output/tibo_reset",
        handle="thsottiaux",
        since="2025-09-29", until="2026-09-30",
        max_pages=2,
    )
```

This search is case-insensitive and its page budget applies to each authored stream. A keyword match identifies a mention; several matching messages may concern the same event.

### Run a native X search

Use `save_search_query()` for X search syntax, including account conditions and keyword combinations:

```python
with XClient(profile="Default") as client:
    result = client.save_search_query(
        "from:OpenAI (reset OR restart)",
        output_dir="output/openai_search",
        since="2025-09-29", until="2026-09-30",
        max_pages=2, limit=100,
    )
    print(result.complete, result.reason)
```

`get_search_query()` and `iter_search_query()` provide the corresponding memory and iterator interfaces. These methods use the native Latest endpoint without a search mirror. Use a separate output directory for each query; changing a saved query or date scope requires a new dataset.

### Collect replies below a post

To collect direct replies and replies to those comments:

```python
with XClient(profile="Default") as client:
    result = client.save_post_comments(
        "1973931546550894681",
        output_dir="output/conversation",
        max_depth=2,
        max_comments=100,
        max_pages=20,
    )
    print(result.complete, result.reason)
```

The seed can be a main post or a reply. `max_depth=2` follows two reply levels below that seed. `max_comments` is the cumulative descendant cap and excludes the seed; `max_pages` limits requests during this call.

Repeat the same seed, depth, dates and output directory to resume. Raise the comment cap to continue a capped dataset. Pending page records are retained so a cap does not discard the rest of a partly processed page.

For a main-post seed, the root enters `posts.csv` and descendants enter `comments.csv`. A reply seed remains in `comments.csv`. `seed_post_id` and `seed_relative_depth` describe the requested branch; platform root, parent and depth fields describe the retained ancestry. Unknown ancestry stays unresolved. `get_post_comments()` and `iter_post_comments()` return descendants without writing a dataset.

When a saved tree contains pending replies with missing parents, you can verify
their exact parent chains in a separate supplement:

```python
with XClient(profile="Default") as client:
    report = client.save_comment_ancestry(
        "data/conversation", "data/conversation_ancestry", lookup_limit=20,
    )
```

This reads a verified `post_comments` collection and looks up missing parent IDs
using the same observer when the source has a pool identity binding. It writes
`resolution.csv`, context-only `ancestor_posts.jsonl`, the original pending
records, raw responses and a hash manifest into a new directory. Source files,
sampling roles and completeness remain unchanged. The limit counts lookup calls;
identity checks and transport retries are additional HTTP attempts. See
[comment ancestry](docs/comment-ancestry.md) for field definitions and unresolved cases.

### Collect several accounts

One client can collect several target accounts using the same authenticated browser session:

```python
with XClient(profile="Default") as client:
    result = client.save_users_activity(
        ["OpenAI", "AnthropicAI", "thsottiaux",
         "sama", "alexalbert__", "bcherny"],
        output_dir="output/ai_accounts",
        since="2025-09-29", until="2026-09-30",
        pages_per_round=5, rounds=1,
    )
```

Repeat to continue saved account tasks. The [API reference](docs/api.md) describes rounds, progress callbacks and rate-limit waiting.

### Collect concurrently with multiple logins

Use separate manually authenticated browser profiles for parallel account or
query collection. The pool verifies each observing account, runs one slice per
account at a time, and persists job ownership and endpoint cooldowns.

```python
from pyxcom import CollectionJob, LoginConfig, XClientPool

with XClientPool([
    LoginConfig("a", profile="Default"),
    LoginConfig("b", profile="Profile 3"),
], state_dir="queue") as pool:
    pool.add_jobs([
        CollectionJob("openai", "user_posts", "OpenAI", "output/openai"),
        CollectionJob("anthropic", "user_posts", "AnthropicAI", "output/anthropic"),
    ])
    report = pool.run(max_workers=2, page_budget=1, max_steps=10)
```

Jobs keep their original observer when resumed. Duplicate logins for the same X
account do not add workers. See [concurrent collection](docs/pool.md) for limits,
resume behavior, output ownership, and CLI configuration. Performance depends on
available sessions and platform responses; see [comparison methods](benchmarks/README.md)
and [the bounded live results](benchmarks/LIVE_RESULTS.md).

### Collect quotes, reposts and network snapshots

Quotes and repost activity have their own methods and output directories:

```python
with XClient(profile="Default") as client:
    client.save_post_quotes(
        "1973931546550894681", "output/quotes", max_pages=2,
    )
    client.save_user_reposts(
        "OpenAI", "output/repost_activity", max_pages=2,
    )
```

Quote discovery checks the returned target identifier. Repost activity preserves action IDs and times when X returns the required wrappers; flattened originals cannot supply a repost action time.

For followers and following, first resolve the account's stable numeric ID:

```python
with XClient(profile="Default") as client:
    user_id = client.get_user("OpenAI").id
    client.save_followers(
        user_id, "output/openai_network",
        max_pages=2, snapshot_id="wave-1",
    )
    client.save_following(
        user_id, "output/openai_network",
        max_pages=2, snapshot_id="wave-1",
    )
    client.save_post_reposters(
        "1973931546550894681", "output/reposters", max_pages=2,
    )
```

Network datasets use separate directories from post collections. Follow edges point from follower to followed account and describe an observed snapshot, not when the relationship began. A reposter list identifies visible users; it does not supply individual repost action times. See [the network example](examples/research_networks.py) and [snapshot rules](docs/api.md#network-lists-and-snapshots).

### Choose another browser or adjust request settings

```python
with XClient(browser="edge", profile="Default", delay=2.0) as client:
    user = client.get_user("OpenAI")

# macOS only; Safari does not use the profile argument.
with XClient(browser="safari") as client:
    user = client.get_user("OpenAI")
```

The constructor also accepts `cookie_db`, `proxy` and `timeout`. Safari uses `Cookies.binarycookies`; you can select an accessible file with `cookie_db`. If macOS denies access, follow the `AuthenticationError` guidance for the application running Python. The package does not change system permissions. Browser options are described in the [API reference](docs/api.md#client).

## Output

A saved post collection has this basic layout:

```text
output/openai_activity/
├── users.csv
├── posts.csv
├── comments.csv
├── manifest.json
└── .pyxcom/
```

| File | Contents | Main identifiers |
| --- | --- | --- |
| `users.csv` | One consolidated row per user | `user_id`, `username` |
| `posts.csv` | Original and quote main posts | `post_id`, `author_id`, `post_type` |
| `comments.csv` | Replies, including self-replies | `comment_id`, `author_id`, `root_post_id`, `parent_post_id`, `depth` |
| `manifest.json` | Query scope, counts, coverage, stop reason and hashes | Dataset metadata |
| `.pyxcom/` | Raw responses, observations and resume state | Private collection files |

Quote links inside replies stay in `comments.csv`. Missing parents do not become guessed first-level replies. For conversations, read `seed_relative_depth` separately from platform `depth` when the seed itself is a reply.

Depending on the observations, exports also include `interactions.csv`, `post_edges.csv`, `context_posts.csv`, `metric_snapshots.csv` and `profile_snapshots.csv`. Canonical message tables retain the first saved observation of each ID; subsequent observations and metrics remain in the archives. `users.csv` selects a profile by stable user ID, while profile snapshots retain earlier handles and returned details.

CSV files use UTF-8 with BOM, snake_case columns, UTC timestamps and JSON arrays for list fields. Import IDs as text in spreadsheets. Missing values remain unavailable rather than becoming zero. Engagement values are retrieval-time snapshots.

Network collections have their own `users.csv`, `user_snapshots.csv`, relationship files and `network_manifest.json`. Keep `.pyxcom/` when you need to resume or verify a collection. See [saved-data rules](docs/stability.md) for archive and export details.

## Saved progress and coverage

Inspect `result.complete` and `result.reason`, or their values in `manifest.json`. `CollectionResult.post_count` counts unique retained message records, including replies; network methods use that compatibility field for observed users.

A page or item budget can leave a resumable partial result. A source end or reached date boundary describes the endpoint traversal, not exhaustive historical coverage. Deleted, protected, hidden or otherwise unavailable content can be absent.

Account timelines pause with `no_progress_limit` after five consecutive pages without new primary source IDs. This state persists across calls; repeating a paused save does not request more timeline pages. Explicit pins remain collectable but do not mask the date-boundary check. See [timeline pagination](docs/timeline-pagination.md).

Native search, quote discovery and repost activity have separate persisted pagination counters. Their save methods support an explicit `retry_stalled=True` to probe a paused source again. That option is not part of the authored timeline API. Dates and other local filters are applied after source progress is measured; an empty filtered page is not an empty-source signal. The [API reference](docs/api.md#native-discovery) covers content warnings, pause thresholds and retry behavior.

Saved timeline limits are checked at page boundaries and may overshoot a record threshold; conversation comment caps are exact. Use the [API reference](docs/api.md) for each method's budget semantics and older compatibility names.

## Validate saved data

Validation is offline and does not require browser cookies:

```python
from pyxcom import assess_recovery, validate_collection

report = validate_collection("output/openai_activity")
print(report["valid"], report["errors"])

assessment = assess_recovery("output/openai_activity")
print(assessment["record_integrity"], assessment["latest_checkpoint"])
```

Validation and complete acquisition are separate checks. A consistent partial dataset can pass validation. Export and finalization refuse unsafe saved sources; they do not restore missing records by assigning new hashes.

If intact sources have stale derived files, [the recovery guide](docs/recovery.md) describes how to prepare an external generation, verify it and explicitly apply it. Keep independently retained receipts and coordinate other writers before application. Recovery does not reconstruct lost source records, invent a cursor or prove historical login identity.

## Share an ID dataset

Export a verified collection as sorted, deduplicated post IDs without text,
profiles, private paths, or observing-account metadata:

```python
from pyxcom import export_post_ids, validate_id_dataset, iter_post_ids

export_post_ids("output/openai", "sharing/openai_ids")
assert validate_id_dataset("sharing/openai_ids")["valid"]
ids = list(iter_post_ids("sharing/openai_ids"))
```

The destination must be new and separate from the collection. Conversation seed
roots are included; context-only records are excluded. The export does not grant
a license to underlying X content or establish complete platform coverage. See
[ID datasets and rehydration](docs/datasets.md).

For the local persistence measurements, see [storage benchmarks](benchmarks/STORAGE_RESULTS.md).

New native searches with explicit `since`/`until` use precise UTC epoch bounds
alongside the date expression, with local half-open date validation. Existing
saved queries resume with their original expression and cursor. Use a new output
directory to collect a legacy query with the corrected date encoding.

## Command line

The `pyxcom` command exposes the same collection tasks:

```bash
pyxcom user OpenAI --profile Default
pyxcom user-activity OpenAI --profile Default \
  --since 2025-09-29 --until 2026-09-30 --max-pages 2 \
  --output-dir output/openai_activity
pyxcom search-query 'from:OpenAI reset' --profile Default \
  --max-pages 2 --output-dir output/openai_search
pyxcom post-comments 1973931546550894681 --profile Default \
  --max-depth 2 --max-comments 100 --max-pages 20 \
  --output-dir output/conversation
pyxcom validate --output-dir output/openai_activity
```

Run `pyxcom --help` or `pyxcom COMMAND --help` for all options. Add `--proxy URL` when your network requires one. Python and CLI compatibility names remain listed in the [API reference](docs/api.md#backward-compatible-names).

## Documentation and development

- [Public API and return types](docs/api.md)
- [Saved-data rules](docs/stability.md)
- [Account timeline pagination](docs/timeline-pagination.md)
- [Recovery workflow](docs/recovery.md)
- [Examples](examples/)
- [Release notes](CHANGELOG.md)
- [Publishing](docs/publishing.md)

To check an editable development installation:

```bash
PYTHONPATH=src python -m unittest discover -s tests
ruff check src tests examples
```

These are offline development checks. They do not collect new X data.

## Author and acknowledgments

Created by **Haocheng Wang, Communication University of China**.

[PykTok](https://github.com/dfreelon/pyktok) inspired the approachable get/save naming and task-by-task examples. 

## Citation

Please cite the software and report the version you used. GitHub's **Cite this repository** menu reads [CITATION.cff](CITATION.cff).

Wang, H. (2026). *pyXcom: Structured and auditable X data collection for communication research* (Version 1.0.1) [Computer software]. https://github.com/haochengw372-hash/pyXcom

```bibtex
@software{wang2026pyxcom,
  author  = {Wang, Haocheng},
  title   = {{pyXcom}: Structured and Auditable X Data Collection for Communication Research},
  year    = {2026},
  version = {1.0.1},
  url     = {https://github.com/haochengw372-hash/pyXcom}
}
```

Replace the version with the one installed for your study. Report collection dates, account or keyword scope, reply-depth limits and the manifest's coverage/stop reasons alongside your analysis. Citation is appreciated and does not add a license condition.

## License

The current repository is licensed under [Apache-2.0](LICENSE). Previously published MIT versions retain their original license; see [NOTICE](NOTICE). Copyright © 2026 Haocheng Wang.
