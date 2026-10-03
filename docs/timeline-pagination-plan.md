# UserTimeline pagination correction

Baseline: 1.0.0 retains a per-call cursor set and resets old-page counters on
every save. It includes explicitly pinned items in its date-boundary check.
There is no saved source-ID progress check on this collection path.

1. Lock the failure with regressions for changing-cursor duplicate pages,
   counters crossing save budgets, pinned-only pages, fresh out-of-window source
   IDs, and the pinned date-boundary case. Preserve get/iter/save signatures.
2. Reuse the source-progress recorder with a separate timeline namespace. Retain
   raw source IDs before local filters, persist counters in the checkpoint, and
   pause repeated content as partial `no_progress_limit`. Parse explicit pin
   metadata only; repeated ordinary items are never inferred to be pinned.
3. Bootstrap missing timeline progress from hash-verified saved response logs
   where the complete successful-page history is available. Preserve existing
   query, observer evidence, raw files and current cursor. Do not promote a
   previously cycling saved task to complete based on historical date bounds.
4. Validate on copies of the frozen incident and replay via mocked transport;
   verify originals remain byte-identical. Run the full test suite, Ruff, mypy,
   and installed-wheel checks. Update only affected API, output and release docs.

Pause is a bounded no-new-source-ID heuristic, not proof of complete historical
coverage. Repeated save calls do not silently clear it. No production source,
collector, browser login or active package version is changed by these checks.
