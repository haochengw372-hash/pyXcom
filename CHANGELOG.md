# Changelog

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
