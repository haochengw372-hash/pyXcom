# Concurrent collection with manually authenticated accounts

`XClientPool` runs bounded collection slices across independently verified X
accounts. Each account has at most one active slice. Increasing `max_workers`
beyond the number of distinct usable accounts does not increase concurrency.
There are no new runtime dependencies.

## Start a queue

Sign in to X yourself in each browser profile before running Python. pyXcom reads
the existing session; it does not launch a browser or automate login. Two browser
profiles signed in to the same X account count as one observer.

```python
from pyxcom import CollectionJob, LoginConfig, XClientPool

logins = [
    LoginConfig("primary", profile="Default"),
    LoginConfig("secondary", profile="Profile 3"),
]
with XClientPool(logins, state_dir="queue", client_options={"delay": 1.0}) as pool:
    pool.add_jobs([
        CollectionJob("openai", "user_posts", "OpenAI", "data/openai",
                      options={"since": "2025-10-12", "until": "2026-10-12"}),
        CollectionJob("anthropic", "user_posts", "AnthropicAI", "data/anthropic",
                      options={"since": "2025-10-12", "until": "2026-10-12"}),
    ])
    status = pool.run(max_workers=2, page_budget=1, max_steps=20)
    print(status)
```

`LoginConfig` accepts `login_id`, `browser`, `profile`, `cookie_db`, and optional
`expected_observer_id`. This last value is a public numeric X account ID, never a
token. Identity is checked with the authenticated Viewer operation; the legacy
settings route is a fallback when Viewer is unavailable. Unknown, changed, or
duplicate identities are excluded. Credentials remain in the clients' memory.

Each job has a stable `job_id`, `kind`, `target`, and separate `output_dir`.
Optional `login_id` pins it before its first request. Otherwise the coordinator
assigns a ready account at dispatch, then keeps that observer for every resume.
Jobs never change accounts to escape a rate limit. New request logs record
`observer_id` and `observer_login_id`; historical rows are not relabeled.

| Kind | Target | Extra options |
| --- | --- | --- |
| `search_query` | Native search expression | `since`, `until`, `limit` |
| `post_quotes` | Post ID or URL | `since`, `until`, `limit` |
| `user_posts`, `user_replies`, `user_reposts` | Account handle | `since`, `until`, `limit` |
| `post_comments` | Post ID or URL | `since`, `until`, `max_depth`, `max_comments` |
| `followers`, `following` | Numeric user ID | `limit`, `snapshot_id` |
| `post_reposters` | Post ID or URL | `limit`, `snapshot_id` |

Limits are explicit: omitted record limits do not introduce a hidden top-N cap.
Dates use inclusive `since` and exclusive `until` in UTC. Compound activity jobs
can be expressed as separate posts/replies jobs with separate outputs.

## Scheduling and stopping

The coordinator alone persists queue state and events. Worker threads call the
existing synchronous savers and return outcomes. Larger numeric `priority` runs
first; equal-priority pending jobs take turns. A continuing high-priority workload
can delay lower-priority jobs.

- `page_budget` bounds pages per slice; it does not include identity, discovery,
  or root/profile hydration requests. Transport events count actual attempts.
- `max_steps` bounds dispatched slices; `None` continues until no job is ready.
- `max_seconds` stops **new dispatch** after a deadline. Already running slices
  finish under the configured request timeout before clients and locks close.
- Request pacing spans slices, retries, and identity checks. It is separate from
  endpoint cooldowns and does not promise an X quota.
- Authentication failure isolates that account. A 403 alone does not establish
  that its login is invalid. Failed and paused jobs remain visible in status.
- `wait_on_rate_limit=False` returns when only cooled jobs remain; `True` waits
  while respecting the dispatch deadline.

`rate_limit_scope="per_account"` tracks observer/operation cooldowns. `"shared"`
also pauses new dispatch using that endpoint across accounts. Default `"auto"`
applies the shared pause after two distinct observers encounter a limit on the
same operation. This is a conservative scheduling rule, not a conclusion about
X's quota implementation. Requests already in flight cannot be recalled.

## Resume and output ownership

Reopen the same `state_dir` with the same login IDs and call `run` again. Re-adding
an identical job is harmless; changing its query, scope, or observer is rejected.
Use `resume_job(job_id)` for a paused job. Explicit discovery-stall retries use
`resume_job(job_id, retry_stalled=True)` where the underlying saver supports it.

Adopting existing output requires both a pinned `login_id` and
`existing_observer_id` matching the verified login. That is the caller's
attestation of past collection identity, not retrospective verification.

OS locks exclude overlapping output directories, including ancestor/descendant
and symlink aliases, across pools on one host. Public `save_*`, table export, and
recovery application use the same lock protocol. Pool-owned collection jobs must
resume through their pool. Locks and credentials are not stored in research
exports. These are cooperative locks: older pyXcom versions, external writers,
and sync services do not participate. Distributed coordination is unsupported.

Queue snapshots use atomic replacement; source checkpoints retain the existing
resumable collection format. This protects tested process-interruption paths;
power-loss durability and exactly-once network delivery are not claimed.

## Command line

Save a credential-free JSON config with `logins`, `jobs`, and optional
`client_options` / `rate_limit_scope`, using the same fields as above:

```sh
pyxcom pool --config jobs.json --state-dir queue \
  --max-workers 2 --page-budget 1 --max-steps 20 --max-seconds 120
```

Unknown and duplicate config fields are rejected. Never put passwords or Cookie
values in config. Read the returned job/login statuses even when the command
successfully writes a report: completion is per job, not implied by a command
exit or a worker finishing its slice.

For matched live and replay comparison protocols, see
[the benchmark tooling](../benchmarks/README.md).
