# Bounded live observations, 2026-10-12

The coordinating process ran these experiments with two independently verified,
idle browser sessions. The benchmark worker analyzed only retained artifacts.
The tested candidate was an uncommitted concurrent-pool development checkout;
later arms record source hashes in experiment artifacts. These small runs support
specific measured behaviors, not a state-of-the-art or full-coverage claim.

## SDK comparison

The 32 planned arms cover two observers, two queries (`from:OpenAI` and
`from:AnthropicAI`), two reversed-order rounds and four implementations. All queries
use `2026-09-01 <= UTC < 2026-10-01`, Latest search, page size 20 and at most two
data requests. Public pyXcom 1.0.1, candidate pyXcom and twscrape 0.20.1 each made
two data requests per arm. All eight matched cases returned identical normalized
primary-record ID sets: 40 in-window records for OpenAI and 20 for Anthropic.

Twscrape returned 22 in-window SDK objects for Anthropic. Independent structural
normalization of its retained response bodies identifies the additional two as
embedded quoted tweets (`quoted_status_result.result`), not additional primary
search hits. Raw responses contain 21 primary Anthropic records, including one
outside the requested interval. Therefore unqualified 22-versus-20 list length
would compare different units.

Twikit 2.3.3 failed during transaction-ID initialization in all eight initial
arms: the homepage returned HTTP 200, but no data request followed. A separate
bounded diagnostic arm confirmed `transaction_indices_missing` at
`ClientTransaction.get_indices`, `transaction.py:54`. This identifies a setup
compatibility failure in the tested environment; it does not measure Twikit's
retrieval coverage or successful-query speed.

## Live pool comparison

Both worker configurations use the actual candidate XClientPool, exactly the same
two observer-bound jobs/query, fresh outputs, one page per dispatch, four dispatches,
one-second request spacing, twelve-second request timeout and a sixty-second
budget. Concurrent then serial were run in round one; the order was reversed in
round two. Times include identity checks, asset discovery and disk export.

| Workers | Round 1 seconds | Round 2 seconds | Data requests | Setup requests |
|---|---:|---:|---:|---:|
| 1 | 11.858 | 12.689 | 4 | 10 |
| 2 | 9.173 | 8.475 | 4 | 10 |

Every arm retained the same 40 canonical records per observer job: 20 `posts.csv`
rows and 20 `comments.csv` rows, plus 40 metric observations, 20 edges and one user.
Both observers ran the same query, so 80 records across two job directories represent
40 globally unique message IDs. All eight job collections passed validation.
Mean elapsed time fell from 12.27 to 8.82 seconds (28.1%, a 1.39× ratio).
All jobs explicitly stopped with `complete=false`, `reason=page_limit`; validation
confirms artifact integrity, not complete retrieval of the date range.
The two concurrent runs were faster under these matched conditions; two rounds on
two sessions do not establish a general throughput advantage or independent quotas.

The original first-run pool report counted `posts.csv` IDs only. The correction is
a separate derived artifact; originals and their hashes remain unchanged. Future
`live_pool.py` reports count canonical records and expose each public table's rows.

## Retained evidence

- `/tmp/pyxcom-concurrent-evidence/live-01`: original 32-arm outputs.
- `/tmp/pyxcom-concurrent-evidence/live-01-primary-normalized`: common unit check.
- `/tmp/pyxcom-concurrent-evidence/twikit-diagnostic`: confirmed setup failure.
- `/tmp/pyxcom-concurrent-evidence/pool-live-01`: original live pool runs.
- `/tmp/pyxcom-concurrent-evidence/pool-live-01-canonical-summary`: corrected units.

A durable copy is retained with the local research report `reports/pyxcom-concurrent-2026-10-12`.
These paths describe the experiment location, not publicly downloadable datasets.

Each directory includes configuration, machine-readable results and SHA256
inventories. Raw public data and private session databases are not included in the
repository. Original response artifacts are sanitized before hashing. Early SDK
arms predate the added source-fingerprint report field; later arms include it.
