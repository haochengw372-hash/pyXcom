# Account timeline pagination — 1.0.1

Account timelines can return changing cursors while cycling through posts they
have already returned. `repeated_cursor` alone does not detect that situation.
Version 1.0.1 uses the shared source-progress recorder with a separate timeline
namespace; search and quotation counters are independent.

## Inspect progress

`save_user_posts()` and `save_user_replies()` retain source IDs before sampling
filters. Five consecutive pages without a new primary source ID pause the
collection with `reason="no_progress_limit"` and `complete=False`. The raw
responses, metric observations, sampled records and latest cursor are retained.
You can inspect the counters in `.pyxcom/state.json` and the summary in
`manifest.json.timeline_pagination`.

Increasing `max_pages` or calling a paused save again does not clear the pause or
request more timeline pages. `get_user_posts()` and related memory iterators
raise `APIError` on the same no-progress limit. No collection method gains a new
argument in this fix. An independently planned traversal requires its own output
directory; the existing cursor and observer evidence should not be edited to
force a retry.

Fresh source IDs reset the streak even when date or author filtering excludes
them from your sampled records. Repeated pinned items contribute their ID once;
they do not make duplicate body pages appear novel.

## Understand the date boundary

Only explicit platform pin metadata (`TimelinePinEntry` or `contextType="Pin"`)
excludes an item from the date-boundary calculation. A recurring ordinary post
is not inferred to be pinned. Pinned posts remain collected when within scope.
Two consecutive pages of fresh ordinary authored items older than `since` can
produce the existing `passed_since` result. The counter persists across page
budgets. A repeated old body page, or a page containing only a pin, cannot count
as a second independently traversed page beyond the boundary.

For a pre-counter checkpoint, the package can bootstrap progress from a complete
saved response history after verifying page count, user and timeline scope, raw
SHA, request cursor chain and final cursor. It preserves the current cursor,
query, observer metadata and raw bytes. Incomplete histories begin tracking from
the next returned page. Bootstrap never changes an already cycling task to
complete based on an earlier date-boundary observation.

## Verification

The fix passed 278 regression tests, Ruff, mypy and an independent review. Tests
cover nonempty duplicate pages with changing cursors, counters spanning calls,
explicit pins, fresh filtered/context IDs, partial pause latching and archived
history replay. Existing public get/iter/save method signatures are unchanged.

A frozen OpenAI timeline incident was checked offline using the package and
mocked transport. Its original full snapshot of 277 files stayed byte-identical:

- Saved incident: 217 pages, 1,234 distinct platform primary IDs, then 167
  consecutive nonempty pages without new IDs. On an external copy, bootstrap
  returns `no_progress_limit`, remains partial, preserves 267 sampled posts and
  the original cursor, and requests zero further timeline pages.
- Replay from the beginning, with five-page save budgets: stops at page 16 with
  `passed_since` and the same 267 sampled posts. The recent pinned post is kept;
  the two ordinary old pages at 15 and 16 establish the date boundary.

These checks do not retrieve new X data or upgrade a running collector. They
verify the termination behavior of retained evidence. A no-progress pause may
reflect a temporary source anomaly; it is not proof that all historical posts
were discoverable. Deleted, hidden, protected or omitted content remains outside
the observed coverage.
