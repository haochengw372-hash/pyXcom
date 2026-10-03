# Changelog

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
