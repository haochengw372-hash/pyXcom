# Concurrent reads inside one comment tree

```python
from pyxcom import XClient

with XClient(profile="Default", delay=1.0) as client:
    result = client.save_post_comments(
        post_id, "data/conversation", max_depth=2,
        max_comments=None, max_pages=20, comment_workers=4,
    )
```

Sign in manually before running. `comment_workers` accepts integers 1–8 and
defaults to 1; the same option works with `iter_post_comments`, `get_post_comments`,
pool `post_comments` job options, and CLI `post-comments --comment-workers 4`.

## Traversal and sampling

Independent focal posts can be fetched concurrently after the first page request
has warmed the route. A focal has at most one in-flight request; subsequent cursors
must already have been returned by a previous page. The coordinator commits in
the original FIFO order, including when a later branch responds faster. Only that
coordinator assigns depths, deduplicates posts, applies caps and writes files.
Canonical records keep the first FIFO-committed observation, rather than whichever
network response finishes first. Buffered records retain their original fetch time.

An exact `max_comments` cap can stop processing before all started requests have
been committed. Successful uncommitted responses are archived as
`TweetDetailPrefetch`, with hash-checked references in
`.pyxcom/state.json.conversation.prefetched_pages`. Resume uses those responses
before making replacement requests; switching back to `comment_workers=1` works.
Use pyXcom 1.4.0 or newer to resume collections containing these references.

`max_pages` limits committed pages per call and bounds newly dispatched page
calls within that budget. Cached pages consume the processing budget too.
`pages_fetched` is the cumulative committed-page count; it does not include
uncommitted read-ahead, identity checks, route discovery or retry attempts. A call
can fetch up to `comment_workers-1` extra pages before reaching its comment cap,
but never dispatches page calls beyond its page budget. All retained successful
read-ahead remains available for later processing rather than automatic admission
to the analysis sample. This does not establish exhaustive platform coverage.

## Pacing and failures

`delay` remains a shared minimum interval between actual HTTP attempt starts
within a concurrent traversal, including route discovery and retries. With the
pool, its existing per-observer attempt gate also applies. Only one active comment
traversal per `XClient` is supported. No observer rotation is introduced.

An observed request error stops new dispatch; already-started requests can finish.
The writer joins workers and preserves successful uncommitted pages before a
normal cap, error, generator close or handled interruption. Invalid buffered
responses are archived as `TweetDetailPrefetchUnparsed` and left for a future
request rather than caching a permanent parse failure. Rate limits keep the
collection partial and retain its frontier. A hard process kill can lose responses
not yet archived; network delivery is not exactly once.

Concurrency helps when request latency exceeds the pacing interval and independent
branches are available. A single cursor chain, a shallow tree or a one-page pool
slice offers little overlap. Begin with 2 or 4 workers and measure; adding workers
does not increase an account's endpoint quota.

## Verification

The offline benchmark `benchmarks/comment_branches.py` uses the public saver with
the same response fixture, 16 independent branches and artificial 30–50 ms latency.
At 1/2/4 workers, each run requests 34 pages and retains 48 comments. ID, edge,
depth, FIFO acceptance-order and request-workload hashes must match, with no
same-focal overlap; saved collections pass validation and recovery assessment.
These timings describe a controlled fixture, not a platform throughput guarantee.

A bounded live pre-release check on October 14, 2026 used one verified original
observer, one post, a 1-second pacing interval and eight committed pages per run.
Serial runs took 13.73 and 15.30 seconds; four workers took 8.90 seconds. All three
retained 64 comments, including 21 at depth two, and passed source assessment.
The runs ended at the page budget and remained partial. This is one sequential
snapshot comparison; platform results and timings can change.
The serial path preserves its legacy post-page sleep; the concurrent path paces
attempt starts. The live timing therefore reflects both scheduling and pacing,
not an isolated estimate of the effect of concurrency alone.
