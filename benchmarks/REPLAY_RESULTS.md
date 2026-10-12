# Controlled scheduler replay, 2026-10-12

This is a synthetic network workload using the real candidate XClientPool,
XClient search saver, state files, raw archival, CSV export and validation.
It is not a live-throughput claim. Two observer-specific jobs receive three pages
each, one post per page, with 100 ms artificial latency per request. The order is
concurrent/serial in round one and serial/concurrent in round two.

| Scenario | Workers | Round 1 seconds | Round 2 seconds | Requests per run | Retained IDs |
|---|---:|---:|---:|---:|---:|
| Healthy | 1 | 0.689 | 0.693 | 6 | 6 |
| Healthy | 2 | 0.352 | 0.380 | 6 | 6 |
| One observer receives 429 on page 2 | 1 | 0.573 | 0.597 | 5 | 4 |
| One observer receives 429 on page 2 | 2 | 0.360 | 0.357 | 5 | 4 |
| One observer receives APIError on page 2 | 1 | 0.575 | 0.577 | 5 | 4 |
| One observer receives APIError on page 2 | 2 | 0.343 | 0.354 | 5 | 4 |

Healthy paired runs retained identical six-ID sets and made identical six requests.
Measured simultaneous observers rose from one to two; no same-observer overlap
occurred. In fault cases the unaffected observer completed its three pages. The
limited observer retained its first page and stayed pending with `rate_limited`;
the API-error observer retained its first page and paused with `source_error`.
All 24 job collections passed validation. Speed values include local export and
validation setup costs and vary with machine/filesystem conditions.

The local evidence directory is `/tmp/pyxcom-pool-replay-20261012-c`, containing
configurations, JSON/CSV, raw synthetic payloads, queue states and SHA256 inventories.
The replay can be regenerated with `benchmarks/replay.py`; tests assert matching
records/budgets and real overlap rather than enforcing a fragile speed threshold.
