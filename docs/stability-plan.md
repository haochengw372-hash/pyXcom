# Stability work for 0.7.1

## Scope and preserved behavior

This release consolidates the completed collection and persistence fixes. Existing
public method signatures, saved queries, original task directories, cursors,
source records, snapshot IDs and literal Unicode text remain compatible.
Research archives and collectors are outside this worktree. The unfinished
multi-login scheduler remains in the original development checkout and is not
part of this stability release.

## Observed failures and shared boundaries

1. A discovery page mixed readable posts with an unavailable slot; treating the
   whole page as unparseable lost its readable observations.
2. Two captures shared a stable user ID while handle and counters changed;
   dictionary equality confused observation changes with identity conflicts.
3. A profile ledger contained legal Unicode separators inside a JSON string;
   Unicode-aware splitting confused text with JSONL record boundaries.
4. Saved source state, derived tables and validation must describe the same
   selection and history policy across continuation and offline reconstruction.

## Cleanup order

- Run the existing 189 committed regression tests before editing.
- Consolidate LF record boundaries and strict object parsing in one internal
  persistence module; keep the validator's explicit malformed-line reporting.
- Consolidate durable atomic JSON replacement and failure cleanup; retain the
  existing internal import alias for compatibility.
- Remove the second profile-ledger scan: capture saved snapshot IDs during the
  verified read, then append only unseen source-labelled observations.
- Keep profile selection and identity checks in profiles.py; exporter and
  validator consume that same view instead of duplicating policy.
- Recheck saved partial coverage and unavailable records rather than introducing
  an unrelated collection feature during this release.
- Document output/history/error contracts and release changes, and remove
  unnecessary pytest installation from the publishing workflow.

## Release evidence

Run all unit regressions, lint, format and type checks. Exercise real archived
cases only in temporary copies, checking original hashes, queries, cursors,
Unicode and snapshot IDs. Build and inspect wheel/sdist, run twine metadata
checks, install in an empty environment outside the source tree, run tests and
CLI smoke checks, then publish through the existing trusted GitHub workflow.
Verify installation of the public PyPI version before reporting completion.
