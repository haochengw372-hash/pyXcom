# 1.0.0 release contract

Preserve published collection signatures, aliases, record types, UTC boundaries,
source-query/cursor compatibility and relational CSV schema. Keep all production
research files, processes and credentials outside this checkout. Unfinished
multi-account drafts are not part of this stable release.

Scope:
- Read-only integrity assessment distinguishes verified records, verified latest
  checkpoint and incomplete coverage. A matching older prefix is not latest proof.
- Block blind public re-export of regressed private sources; keep a trusted
  internal exporter for producer finish and preverified staged recovery.
- Add explicit staged-generation recovery and verification, default no live apply;
  derived-only apply never rewrites source/checkpoint/query/observer metadata.
- Preserve original evidence, require unchanged source membership/hashes, validate
  generation before publication and reject stale plans / reverted generations.
- Source reconstruction and unidentified latest cursors stay blocked. Do not infer
  unknown target/author data or historical observer identity.
- Standard-library regressions cover rolled-back JSONL/profile/state/history,
  partial capture, stale plans, partial artifacts and complete-generation checks.
- Add offline CLI assess/prepare/verify/apply commands and workflow docs; freeze
  public1.0 contract and document upgrade limitations. No runtime dependency adds.

Acceptance: existing207 tests before edits; meaningful new safety regressions;
original SHA unchanged on protected real cases; clean installed wheel tests;
Python3.10 and3.13 CI; metadata/content checks; trusted GitHub/PyPI release and
fresh public-index installation verification. Approximately130k retained records
are field-use evidence, not a benchmark or completeness/representativeness claim.
