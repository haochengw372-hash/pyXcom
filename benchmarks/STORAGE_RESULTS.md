# Offline storage performance audit — 2026-10-12

This is a synthetic local-disk experiment, not network throughput evidence or an X coverage benchmark. No browser, cookie, network request, or original research dataset was used. Baseline `storage.py` exactly matches tag `v1.1.0` (SHA256 `1f617bf752c772a1dc9a6ffc1b439da94e5d3ec413559b31b5633be1fff0df4d`). Environment is recorded in the JSON files.

## Implemented bounded optimization

Previously each new metric snapshot called append_jsonl, file flush and fsync separately. The candidate prepares metric snapshots in input order, deduplicates within the page and against retained snapshots, durably appends the batch once, then updates the in-memory cache. Metrics remain durable before observations, canonical posts and state advance. Snapshot digest rules, first-observation canonical records, observation history, CSV schemas and exported ordering are unchanged.

Three uninstrumented paired repetitions reverse arm order on alternate rounds. The table reports median phase times; append+finish is the sum of phase medians, not a separately estimated median. Fsync counts come from separate untimed runs. Both arms use identical non-storage modules.

| Records | Page size | Baseline append sec | Batched append sec | Append time decrease | Total fsync calls | Append + finish sec |
|---|---:|---:|---:|---:|---:|---:|
| 1,000 | 50 | 0.084 | 0.033 | 60.6% | 1,070 → 90 | 0.148 → 0.096 |
| 1,000 | 500 | 0.078 | 0.024 | 68.9% | 1,016 → 18 | 0.142 → 0.088 |
| 10,000 | 50 | 0.856 | 0.338 | 60.6% | 10,610 → 810 | 1.505 → 1.018 |
| 10,000 | 500 | 0.784 | 0.244 | 68.8% | 10,070 → 90 | 1.444 → 0.895 |

Every artifact SHA256 was identical across both arms and all repetitions, including metric JSONL, original observations, canonical JSONL, all CSVs, state and private/public manifests under a fixed clock. Each output also passed recovery assessment and reopened with the expected record count. Source hashes and every artifact digest are retained in `metric-batch-results.json`.

The localized improvement concerns the append phase. Finish and restore still traverse complete saved collections. This result should not be reported as equivalent end-to-end live scraping speedup.

## Larger remaining bottleneck: repeated full materialization

The separate scaling profile uses cProfile, so its wall times should not be mixed with the uninstrumented table above. It models the lifecycle invoked by one-page pool dispatches by reopening PostStore and finishing after each page; it does not run the pool or HTTP transport itself. All seven fixtures passed resume assessment.

| Records | Page size | Finish/reopen schedule | Profiled collection sec | Append sec | Finish sec | Restore sec |
|---:|---:|---|---:|---:|---:|---:|
| 1,000 | 50 | Final finish only | 0.263 | 0.120 | 0.142 | 0.000 |
| 1,000 | 500 | Final finish only | 0.252 | 0.110 | 0.141 | 0.000 |
| 10,000 | 50 | Final finish only | 2.594 | 1.216 | 1.377 | 0.000 |
| 10,000 | 500 | Final finish only | 2.526 | 1.102 | 1.421 | 0.000 |
| 1,000 | 50 | Every page | 2.229 | 0.133 | 1.580 | 0.509 |
| 10,000 | 500 | Every page | 21.130 | 1.098 | 15.119 | 4.877 |
| 10,000 | 50 | Every page | 201.697 | 1.426 | 145.327 | 54.411 |

For the last row, 200 finishes spent 89.97 cumulative seconds in relational export; 199 recovery assessments consumed 31.91 cumulative seconds. These times are nested within the finish/restore columns, so must not be added again. Existing integrity `_inventory` also reads every retained file into memory, including raw response archives; this fixture has no raw HTTP archives, so real recovery memory/read costs may be larger. No source-replay slowdown is claimed here: response replay is only used by recovery branches requiring it.

## Recommended next change and acceptance gate

For larger jobs use the existing `page_budget` to amortize full reopen/export work, with a documented tradeoff in scheduling fairness and time between dispatch boundaries. Increasing the bounded slice budget does not alter per-page durable checkpoints or skip the current integrity check. A fair benchmark must keep total pages/requests/records, observer bindings and pacing equal; compare budgets 1/5/20 and report dispatch latency alongside throughput. This can be verified through the existing real-saver replay harness before live tests. Changing the global default should await these fairness and bounded-stop tests.

A later persistent collection writer could retain one verified store per active job under its scope lock, export at explicit boundaries and always assess on process restart. That is a lifecycle change requiring crash/tamper/interrupt regression coverage; it is not implemented in this patch. Silently dropping assess_recovery or public manifest refresh would weaken existing guarantees.

Pool identity verification occurs at every slice: Viewer is one authenticated request when discoverable, settings plus profile is the fallback. A TTL policy could reduce overhead but changes how quickly account drift is detected; no identity caching was introduced.

## Regression evidence

- New tests: one metric write per page; digest/order/dedup and unchanged original observations; failed metric write cannot advance state/cache; metric durability precedes posts/checkpoint.
- All four new tests pass; the baseline fails the two batching assertions.
- Existing observations (8), API interruption (2), persistence (11), integrity (34), recovery (17) tests passed: 76 targeted tests including the four new tests.
- Ruff passes for touched code/tests/benchmark, and mypy passes for storage.py.
- No new dependencies. No change to source format, durability ordering or public method signature.

## Reproduction

Retain baseline module with `git show v1.1.0:src/pyxcom/storage.py > /tmp/storage-baseline.py`, then run `PYTHONPATH=src python benchmarks/metric_batch.py --baseline-source /tmp/storage-baseline.py --output /tmp/metric-results.json`. Use a fresh output filename. The auxiliary `profile_store.py` should run in the v1.1.0 checkout with its output path adjusted to a new disposable directory; it does not overwrite original research data.
