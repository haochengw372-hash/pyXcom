# Changelog

## 1.3.1 — 2026-10-13

### Fixed

- Distinguish malformed or unparseable parent responses (`response_unparsed`)
  from recognized empty results and tombstones (`parent_not_returned`).
- Honor inherited source observer bindings and reject malformed markers by reusing
  the package's ownership reader; check ownership again before publication.
- Reject supplement destinations owned by another pool before network requests,
  including inherited ownership, and check again before publication.
- Publish ancestry output as one staged generation. Ordinary write failures leave
  no partial final directory blocking retries.

### Added

- `blocked_post_id` identifies the actual point of failure along a parent chain,
  while preserving the original `parent_post_id`.

## 1.3.0 — 2026-10-13

### Added

- `XClient.save_comment_ancestry()` verifies missing comment parents through bounded,
  deduplicated exact-ID lookups, using the source observer when bound.
- Write a separate ancestry supplement with verified seed-relative/platform depths,
  explicit unresolved statuses, context-only parents and source/output hashes.
- Preserve original sampling roles, files, cursors and partial status; stop further
  lookups on rate limits or request errors without rotating accounts.

No new runtime dependencies or changes to existing comment collection defaults.

## 1.2.1 — 2026-10-13

### Fixed

- Retain comment cursors paused by the empty-page guard and support explicit
  `retry_stalled=True` through the saver, CLI and pool resume.
- Recover older discarded cursors only from hash-verified archives bound to the
  last committed focal/cursor request, including after an ordinary resume.
- Keep historical pagination warnings while reporting still-stalled and
  unrecoverable branches separately; bounded retries may remain partial.

## 1.2.0 — 2026-10-12

### Added

- Export and validate text-free post-ID datasets with `export_post_ids`,
  `validate_id_dataset`, `iter_post_ids`, and CLI `export-ids` / `validate-ids`.
- Include selected conversation roots while excluding context-only records.

### Fixed

- Use precise UTC epoch bounds for new explicitly date-filtered native searches;
  preserve legacy saved query/cursor pairs during resume.
- Batch metric snapshot writes per page while retaining hashes, ordering and
  per-page checkpoint durability.

## 1.1.0 — 2026-10-12

- Add bounded `XClientPool`, public login/job configurations, persistent observer
  ownership, endpoint cooldowns, and queue CLI.
- Verify browser identities with the current Viewer operation.
- Pace and report actual HTTP attempts across page slices; isolate failed logins.
- Exclude overlapping writers across pools, public savers, export, and recovery.
- Preserve observer provenance for new source logs without relabeling old data.
- Add matched SDK comparisons and serial/concurrent scheduler benchmarks.
- Preserve existing synchronous collection methods and runtime dependencies.

## 1.0.1 — 2026-10-04

### Fixed

- Account timelines pause as partial `no_progress_limit` after five consecutive pages without new primary source IDs, with counters retained across save calls and a separate `timeline_pagination` report.
- Explicitly pinned items remain collectable but do not mask the timeline date boundary; old-page counters persist across page budgets and repeated old pages do not establish a second date-boundary page.
- Pre-counter checkpoints can seed progress from a complete hash-verified saved response history without changing raw files, query, observer evidence or the current cursor; a proven saved cycle pauses without another timeline request.
- Source progress is measured before date, role and author filters, so fresh context or out-of-window records continue pagination.

Public collection method signatures remain unchanged; memory timeline traversal raises `APIError` when the no-progress limit is reached. A paused save remains partial on subsequent calls. See [timeline pagination](docs/timeline-pagination.md) for evidence and limitations.

## 1.0.0 — 2026-10-03

### Added

- Offline `assess_recovery`, `prepare_recovery`, `verify_generation`, and `apply_recovery` APIs and CLI commands, with explicit generations and independently retained receipts.
- Source integrity gates and checkpoint-evidence checks before derived-file recovery; changed or insufficient sources remain blocked.

### Changed

- Export and finalization refuse unsafe source integrity instead of publishing new hashes for missing records. Recovery preserves original source bytes, scope, observer evidence, cursors, incomplete status, and stop reasons.
- Documentation distinguishes record reconstruction from readiness to resume and describes generation verification, source isolation, and limits of recovery locks. `binding` annotates caller-provided provenance and compares plan/receipt consistency; it does not authenticate historical logins or backfill observers.

### Fixed

- `save_user_posts` preserves cursor, page count, and metric evidence on `APIError`, records `complete=false`, `reason="api_error"`, and `error_type`, then re-raises the original exception; successful continuation clears `error_type`.
- Assessment distinguishes derived recovery (`allowed`) from continuation (`resume_allowed`). Independently justified unpublished UserTimeline tails can resume while derived recovery stays blocked; unknown tails/source regressions cannot. The old private anchor is never rewritten by assessment.

The get/iter/save API and compatibility names remain available. No multi-login scheduler or automatic browser login is included. Approximately 130,000 records is author-reported field experience, not a benchmark or complete-population claim. Recovery does not repair missing source records or protect files from external writers.

## 0.7.1 — 2026-10-02

### Changed

- Saved JSON checkpoints, profile views and table manifests use shared atomic replacement with failure cleanup, while profile history is verified once per save and flushed before its current view changes.
- Export and validation share one stable-ID profile selection policy, with historical profiles available in `profile_snapshots.csv` and `.pyxcom/profile_observations.jsonl`.

### Fixed

- Discovery retains readable posts from mixed pages, records unavailable or unparsed content in `source_content`, and pauses stalled pagination without treating filtered results or unavailable slots as empty source pages.
- Profile renames and changing metrics no longer interrupt export, while conflicting account creation timestamps remain errors and every distinct source-labelled snapshot is retained.
- Shared JSONL reading and append rules preserve Unicode text and existing record boundaries, including files without a final LF, while validators report malformed records and invalid nested profile JSON.

Existing public calls, saved query scopes and cursors remain compatible; no multi-login scheduler is included in this release.
See [saved-data rules](docs/stability.md) and the [API reference](docs/api.md) for the output and resume contracts.
