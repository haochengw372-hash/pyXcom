# 1.0.0 release validation

The 1.0 changes were checked against the existing 207-test suite before edits,
then against 267 standard-library regression tests after integration. Ruff passed
for `src` and `tests`; mypy reported no issues in 24 package modules with
`--ignore-missing-imports`. An independent read-only review checked the recovery
and interruption paths and that every module parses with Python 3.10 syntax.

## Frozen field cases

Six previously retained failure snapshots were assessed offline. Only external
copies were changed. The hash inventory of every original snapshot was compared
before and after the run.

| Retained case | Check | Result |
| --- | --- | --- |
| Three conversation snapshots with 4, 15, and 5 source records | Assess, prepare, verify, apply to a copy | Passed; private source bytes unchanged and partial status preserved |
| Search snapshot: 665 private records versus 1,550 published records | Assessment and ordinary export refusal | Blocked without writes |
| Conversation snapshot: 55 private profile observations versus 66 published observations, checkpoint 20 versus 50 pages | Assessment and ordinary export refusal | Blocked without writes |
| Internally consistent older conversation bundle: 35 pages versus independent prior evidence of 80 pages | Assessment with a previous receipt | Blocked as behind the independent receipt |

These are retained incidents used to exercise integrity behavior, not a sampling
or performance benchmark. A coherent older bundle requires an independently
retained anchor to detect that it is old. Without that anchor, consistency checks
alone cannot establish recency.

## API interruption

Two deterministic tests exercise `save_user_posts()` when a timeline request
raises `APIError`: after already saved pages and before the first successful
page. The package publishes an incomplete generation with `reason="api_error"`,
retains its cursor and metric observations, and re-raises the original exception.
A retry resumes from the committed cursor. The tests also check that the error
message is not written to the request log.

This addresses the saving contract for subsequent runs of this version. It does
not retroactively repair an unfinished private source anchor from an older
running collector. An unpublished UserTimeline tail can resume only when the
complete raw response/cursor chain, append prefix, ordered observations including
duplicates, and every metric snapshot corroborate the retained checkpoint. Such
a tail has `resume_allowed=True` while derived recovery stays `allowed=False`.
Tests reject metrics changed without changing their IDs, missing metric rows,
and missing newer observations for a repeated post. Source reconstruction and
an unproven latest checkpoint stay outside derived-file recovery.

## Installation and publication gates

The release workflow builds the source distribution and wheel, checks their
metadata and contents, and runs the tests against the installed wheel outside
the repository on Python 3.10 and 3.13 before PyPI publication. A fresh public
index installation is checked after publication. TestPyPI requires a separately
configured publisher and is not part of this project's current publishing setup.

Approximately 130,000 records is the author's reported field use. This release
does not certify their unique-post count, historical completeness, or analysis
coverage. Publishing the package does not upgrade a running research collector
or apply staged recoveries to production data.
