# Bounded collector comparisons

These scripts compare measured behavior, not a claim of complete coverage or
state-of-the-art performance. They run outside the package and add no runtime
dependencies. No script logs in, sends posts, or rotates to another identity.

## Comparator versions and source documentation

The isolated environment pins public pyXcom 1.0.1, Twikit 2.3.3 and twscrape
0.20.1; the latter two were the PyPI latest releases checked on 2026-10-12.
`environment-lock.txt` records the resolved Python 3.13 environment. Install into
an isolated virtual environment with `pip install -r benchmarks/requirements.txt`.

The adapters use documented APIs:

- [Twikit Client](https://twikit.readthedocs.io/en/latest/twikit.html):
  `set_cookies`, `search_tweet(..., product="Latest", count=20)`, `Result.next`.
- [twscrape](https://github.com/vladkens/twscrape): cookie-based `AccountsPool`,
  `API.search_raw` and its actual `parse_tweets` parser. The official HTTPX backend
  is selected explicitly; this experiment does not evaluate optional curl mode.
- pyXcom: `iter_search_query` for SDK collection comparisons;
  `XClientPool` with actual `save_search_query` for scheduler replay.

## Real requests

The coordinator must first verify that each selected browser profile belongs to
its expected public observer ID and is idle. The same cookie dictionary is passed
by a private pipe to every arm for that observer. A separate subprocess isolates
SDK imports and permits a public installed baseline plus a source candidate.

Example credential-free config (replace observer IDs with verified values):

```json
{
  "sessions": [{"profile": "Default", "observer_id": "VERIFIED_ID"}],
  "queries": [{"query": "from:OpenAI", "since": "2026-09-01", "until": "2026-10-01"}],
  "arms": ["pyxcom-baseline", "pyxcom-current", "twikit", "twscrape"],
  "rounds": 2,
  "attempts": 2,
  "seconds": 90,
  "timeout": 15,
  "seed": 42
}
```

```sh
/tmp/pyxcom-benchmark-env/bin/python benchmarks/live_compare.py \
  --config /private/experiment.json --source "$PWD/src" \
  --output /private/benchmark-results
```

The order is seeded and reversed on alternate rounds. Every arm uses the same
query, half-open UTC interval, Latest product, and requested page size 20. Counts
are constrained by actual data endpoint attempts, not just successful pages.
HTTPX sends are instrumented below SDK retry loops; connection-layer retries are
set to zero in every arm so hidden connection retries cannot escape accounting.
Setup/assets requests have a separate cap of 30. Every request has a finite
timeout, and each arm has a hard elapsed-time limit. A 401, 403 or 429 stops the
entire comparison, preserving failed and unrun arms rather than trying another
identity. Therefore some comparisons may be incomplete.

The twscrape session database exists only in a temporary directory with mode
0700 and is deleted when its arm exits normally or raises. A process killed with
SIGKILL can leave that private temporary directory; never include it in reports.
The coordinator captures subprocess output but never writes arbitrary exception
messages, stderr, request headers, URLs with queries, or cookies into artifacts.

`raw/response_*.json` contains sanitized data response bodies, including errors.
These are research records and may contain public post text; they are not intended
for automatic publication. Exact credential values and credential-like keys are
redacted before writing. SHA256 hashes describe the retained sanitized artifact,
not the original response bytes.

`results.json` and `results.csv` retain version, observer ID, query, arm order,
endpoint attempts, setup attempts, elapsed time including setup, returned IDs,
usable in-window IDs, stop reasons and response hashes. Error arms remain visible.
An observed ID union is a descriptive overlap set, **not ground truth or recall**.
Different library text/relationship completeness needs a separate review.

## Scheduler replay

```sh
/tmp/pyxcom-benchmark-env/bin/python benchmarks/replay.py \
  --source "$PWD/src" --output /private/replay-results --rounds 2
```

This runs the actual candidate pool with one versus two workers and actual XClient
search savers. Only the network transport is replaced by fixed JSON pages with
identical artificial latency. Each observer is bound to its own job/output. Healthy,
429 and API-failure scenarios use the same page fixtures in both arms. The report
records simultaneous observers, same-observer overlap, request sequence, retained
IDs, queue status and collection validation. Fault cases test isolation and retained
artifacts; they do not measure live reliability. Replay speed is not live speed.

## Reporting template

A complete report should state:

1. Candidate commit, baseline and comparator versions, Python/OS and date.
2. Verified observer assignments, exact queries/UTC window, order and budgets.
3. Per-arm requests, time, returned/in-window unique IDs, stops and failed arms.
4. Pairwise ID overlap, any unmatched IDs and their inspection method.
5. Scheduler replay concurrency, identity/writer isolation and retained data.
6. Which results are live, which are replay, and which planned arms did not run.
7. Links to configuration, CSV/JSON and SHA256 inventories.

Do not infer an unavailable library's zero output is zero platform coverage. Do
not rank throughput when the arms did different work, stopped on different errors,
or used different observer sessions. The live SDK comparison and scheduler replay
answer different questions; report them separately.

## Live serial versus concurrent pool

After SDK arms finish, run `live_pool.py` with the same config/source/output flags.
It requires exactly two verified browser sessions and uses the first configured
query for both. The candidate package runs four page slices, one page per slice,
with one or two workers, two reversed rounds, fresh directories, a one-second
request spacing and a twelve-second request timeout. Each arm has a sixty-second
budget and at most two SearchTimeline attempts per observer. Authenticated identity
checks/assets are recorded separately from the data attempts. The source fingerprint,
per-observer IDs, queue outcome and collection validation are retained.

This tests actual package concurrency, including setup and disk export costs.
A request-time budget or error may prevent matched output; compare output sets
before interpreting elapsed-time ratios. The replay result alone does not establish
that a live task becomes faster.

## Normalize primary search hits

```sh
/tmp/pyxcom-benchmark-env/bin/python benchmarks/normalize.py \
  --input /private/benchmark-results --output /private/normalized-results
```

The normalizer reads retained responses only and verifies their hashes. Its common
structural rule selects timeline `itemContent.tweet_results.result` records and
excludes objects embedded inside a quoted/reposted result. It handles the visibility
wrapper before applying the common UTC window. It reports SDK output separately.
This matters because twscrape can return embedded quoted tweets as additional
records: more returned objects need not mean more primary search hits. Compare the
normalized ID sets rather than unqualified SDK list length.

For an existing live pool report created before canonical counting was added,
`pool_summary.py --input ORIGINAL --output NEW_DERIVED_DIRECTORY` checks the original
SHA inventory and produces a separate canonical-ID/table-count report. It never
rewrites the original evidence. Main posts and replies are both primary search
records when returned as top-level timeline entries.

The initial observed results are documented in `LIVE_RESULTS.md`; controlled
network replay results are documented separately in `REPLAY_RESULTS.md`.
