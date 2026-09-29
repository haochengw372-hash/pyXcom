# pyXcom

> **使用前请自行打开 Chrome 并登录 X。pyXcom 不会自动打开浏览器或替你登录。**

pyXcom collects public X profiles, posts, authored replies, individual post metadata, and keyword search results. It uses your existing Chrome or Edge login and does **not** automate a browser or log in for you. The Python API follows [PykTok](https://github.com/dfreelon/pyktok)'s useful distinction: `get_*` returns data to memory; `save_*` writes resumable files.

Major features: browser-cookie authentication, user timelines and replies, keyword/date/user search, single-post and raw JSON access, and resumable CSV/JSONL export.

## First, sign in manually

1. **Open Chrome yourself and sign in to [x.com](https://x.com).** Leave the account signed in.
2. Find that Chrome profile's folder name at `chrome://version` → **Profile Path**. For example, a path ending in `Profile 3` means `--profile 'Profile 3'`.
3. Install this local package:

```bash
cd pyxcom
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

No password or exported cookie file is needed. pyXcom reads only `auth_token` and `ct0` from the selected local Chrome profile at runtime, keeps them in memory, and sends them only to `x.com`. If the system needs a network proxy, pass `--proxy`; the examples below use `http://127.0.0.1:7897` only as an example.

## Command line

Get the public profile:

```bash
.venv/bin/pyxcom profile thsottiaux --profile 'Profile 3' --proxy http://127.0.0.1:7897
```

Collect an account's original posts and authored replies for a date range:

```bash
.venv/bin/pyxcom activity thsottiaux \
  --since 2025-09-29 --until 2026-09-30 \
  --profile 'Profile 3' --proxy http://127.0.0.1:7897 \
  --output output/tibo-year
```

Collect multiple accounts with the same date window. The package verifies each X handle, saves each account's original and reply timelines, and writes a combined CSV and report:

```bash
.venv/bin/pyxcom batch \
  --handles OpenAI AnthropicAI thsottiaux sama alexalbert__ bcherny \
  --since 2025-09-29 --until 2026-09-30 \
  --profile 'Profile 3' --proxy http://127.0.0.1:7897 \
  --pages-per-round 5 --rounds 1 \
  --output output/ai-accounts-year

.venv/bin/pyxcom validate --output output/ai-accounts-year
```

Repeat the same `batch` command to resume. `--rounds 0 --wait-on-rate-limit` keeps paging until all six accounts pass the date boundary, waiting for X's rate-limit window when needed. The output root includes `profiles.json`, `account_manifest.json`, `report.md`, combined `posts.csv`/`posts.jsonl`, and one resumable folder per account. `validate` checks row counts, dates, authors, and file hashes using only saved artifacts.

Search **keyword + duration + specified user** directly from X. pyXcom reads that user's originals and replies and filters their text locally:

```bash
.venv/bin/pyxcom search 'Codex' \
  --user thsottiaux --since 2026-09-01 --until 2026-09-30 \
  --profile 'Profile 3' --proxy http://127.0.0.1:7897 \
  --output output/tibo-codex-september
```

`--since` is inclusive and `--until` is exclusive, both in UTC. A direct user search uses case-insensitive text matching; enter a literal word or phrase. `--max-pages 2` is useful for a small trial run (two pages from each timeline for a direct user search); run the same command again with the same output directory to continue from its saved cursors. `--limit` caps the number of saved matches. `posts --timeline replies` saves only authored replies; `posts` defaults to original posts.

Cross-user keyword search is a separate **explicit opt-in** because the Chrome-cookie-only client cannot reliably use X's all-account search endpoint. It sends the search terms to the selected third-party mirror to discover public post IDs, then gets their text and metrics from X. To use it:

```bash
.venv/bin/pyxcom search 'Codex' \
  --since 2026-09-01 --until 2026-09-30 \
  --mirror-base https://x.noodl3.net \
  --profile 'Profile 3' --proxy http://127.0.0.1:7897 \
  --output output/all-users-codex-september
```

With neither `--user` nor `--mirror-base`, pyXcom stops with an explanation. It never silently sends a search query to a mirror.

Get a single post or its raw public JSON:

```bash
.venv/bin/pyxcom post https://x.com/thsottiaux/status/2104838506363408740 \
  --profile 'Profile 3' --proxy http://127.0.0.1:7897
.venv/bin/pyxcom raw-post 2104838506363408740 \
  --profile 'Profile 3' --proxy http://127.0.0.1:7897 --output output/post-raw.json
```

Use `--cookie-db /absolute/path/to/Cookies` if you prefer an explicit Chrome database path. Edge is also supported with `--browser edge --profile ...`. Run `.venv/bin/pyxcom --help` for all options.

## Python API

```python
from pyxcom import XClient

with XClient(profile="Profile 3", proxy="http://127.0.0.1:7897") as client:
    profile = client.get_user("thsottiaux")
    post = client.get_post("2104838506363408740")
    results = client.get_search(
        "Codex", user="thsottiaux", since="2026-09-01",
        until="2026-09-30", max_pages=1,
    )
    run = client.save_user_activity(
        "thsottiaux", "output/tibo-year",
        since="2025-09-29", until="2026-09-30",
    )
    print(profile.followers_count, post.view_count, len(results), run.post_count)
```

`get_*` methods return `Profile`, `Post`, or a list of `Post` objects. `iter_user_posts` and `iter_search` stream records. `save_*` methods return a `CollectionResult` with count, page count, completion flag, and stop reason.

## Output and resuming

Each collection directory contains:

- `posts.jsonl`: one public post per line, with text, UTC timestamp, link, author, reply/quote relations, views, likes, reposts, replies, quotes, bookmarks, media URLs, hashtags, and mentions.
- `posts.csv`: the same deduplicated records, sorted by time. List fields are JSON arrays.
- `state.json`: cursor, page count, query parameters, and completion status; **no cookies**.
- `manifest.json`: count, date bounds, completion status, missing IDs, and SHA-256 hashes.

`activity` also creates `originals/` and `replies/` subdirectories. A direct user search keeps its scanned user timelines under `source/`, so its matching decisions can be audited. Re-run an identical command with the same `--output` after `page_limit` or `rate_limited` to continue. A different query in the same directory is rejected to protect the existing dataset. Files under `output/` are excluded from Git by default.

## Data sources and limits

Account profiles, timelines, post details, and **specified-user keyword searches** come directly from X. pyXcom reads X's current web bundle to discover query IDs, so it does not depend on twikit's pinned endpoint IDs. Only cross-user search uses a Nitter-compatible mirror, and only when you explicitly provide `--mirror-base`; it then fetches each discovered post's text and metrics directly from X. The mirror never receives X login cookies.

X and mirrors can change response formats or impose rate limits. A completed pagination run means it reached the source's end or the requested date boundary; it cannot recover deleted, protected, withheld, or unindexed posts. Engagement metrics are snapshots at collection time. pyXcom does not access DMs, follow private accounts, solve CAPTCHAs, or download media files.

The local distribution name is `pyXcom`, the import is `pyxcom`, and the command is `pyxcom`. This repository is installable locally; it has not been published to PyPI.
