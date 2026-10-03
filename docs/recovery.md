# Saved-data recovery — pyXcom 1.0

Recovery addresses a specific situation: the saved source is intact and justified, but a derived CSV or publication manifest no longer represents it. It does not retrieve unavailable posts or reconstruct a latest cursor from guesswork. Every operation here is offline. Preparation/application currently support normalized post collections with `.pyxcom/posts.jsonl`; network snapshots and legacy layouts are not supported by this recovery workflow.

## Keep three states separate

1. **Retained records:** the post/profile/metric archives and their provenance can be checked.
2. **Resume checkpoint:** saved query, page history, queue, cursor, and counters have adequate consistency evidence. Caller-known observer binding is kept separately as a provenance annotation.
3. **Published generation:** the CSV tables and manifest agree with those sources.

A recovered record count does not prove the latest checkpoint. A valid partial generation does not prove complete historical collection. Preserve the original incomplete flag and stop reason in research reporting.

## Assess before rebuilding

```python
from pyxcom import assess_recovery

plan = assess_recovery("output/conversation")
print(plan["allowed"])
print(plan)
```

The assessment reads existing files, inventories their hashes, checks saved integrity evidence, and uses supported source replay to check checkpoint consistency. Unknown or mismatched source integrity blocks recovery. A complete copy whose public tables contain more records than its private archive must be isolated; calling ordinary export could otherwise publish the shorter archive and erase evidence of missing records.

Supply `expected_query` when your study has a frozen query, seed, date window, or depth. `binding` is a caller-provided provenance annotation, compared with a supplied plan or previous receipt. It does not authenticate a historical login, verify each page's observer, or add an observer to unlabelled historical records. Supply only caller-known public observer metadata, never credentials; the package does not rewrite the source binding or query. A separately kept `previous_receipt` provides an independent generation anchor; files that agree only with a manifest rolled back alongside them are not independently proven current.

The functions accept these metadata objects as dictionaries. CLI equivalents read JSON-object files:

```bash
pyxcom assess --output-dir output/conversation \
  --expected-query query.json --binding binding.json \
  --previous-receipt archive/receipt.json
```

Never put credentials, browser cookies, authorization headers, or passwords in query, binding, plan, or receipt files. Those files describe public scope and artifact provenance.

An old saved archive can have a verified source tail that was never published after an interrupted request. Assessment may report `record_integrity="verified"` and `latest_checkpoint="incomplete_publication"` while keeping `allowed=false` for derived recovery. A supported UserTimeline tail can separately receive `resume_allowed=true` when complete raw responses, request cursor, append prefix, observations, and metrics independently prove continuation, with stable inputs and no other query/receipt conflict. PostStore may resume that original collection; prepare/re-export remain blocked from replacing the old private manifest anchor. Unknown tails and source rollback keep `resume_allowed=false`. Neither assessment nor resume permission fabricates a historical finish record.

In 1.0, `save_user_posts` catches `APIError`, preserves its cursor/page/metric evidence, writes an incomplete finish with `reason="api_error"` and `error_type`, then re-raises the original exception. A successful retry clears `error_type`. The failure path attempts publication of the saved partial state before returning the original endpoint error; publication can still fail because of local I/O or integrity errors. It does not retroactively repair old archives.

## Prepare and review a generation

```python
from pyxcom import prepare_recovery, verify_generation

if plan["allowed"]:
    prepared = prepare_recovery(
        "output/conversation", plan=plan,
        generation_dir="/local/archive/conversation-generation-1",
    )
    checked = verify_generation(prepared["generation_dir"])
    print(checked["valid"], checked["errors"])
```

Preparation creates a separate full dataset generation, rebuilds only approved derived output, and retains a receipt with its identity and hashes. It does not change the active source directory. A supplied plan must still match current source evidence; source changes since assessment invalidate it. `generation_dir` selects a parent directory; preparation creates a unique `generation-ID` child and returns its exact path. Verification and application take that returned path.

Preparation preserves an `original/` snapshot and rebuilt `data/` inside the unique generation folder. Keep original failure evidence separately. For example, a stale `profile_snapshots.csv` can omit old handles and observations while the private profile history remains intact. Recovery must preserve every retained snapshot ID, capture time, profile text, and source label, not merely choose a latest user row. Conflicting source histories and unexplained missing source bytes remain blocked.

```bash
pyxcom prepare-recovery --output-dir output/conversation \
  --generation-dir /local/archive/generations
# Substitute the exact generation_dir from that JSON report:
pyxcom verify-generation --generation-dir /local/archive/generations/generation-ID
```

A valid generation is a point-in-time statement about saved files. Replacing a prepared CSV with an older version invalidates its receipt even if the older file is syntactically valid. Retain receipts somewhere independent of the active directory so they are not lost with the files being checked.

## Explicit application

```python
from pyxcom import apply_recovery

applied = apply_recovery(
    "output/conversation",
    generation_dir=prepared["generation_dir"],
    receipt_dir="/local/archive/receipts",
)
print(applied["applied"], applied["receipt_path"])
```

```bash
pyxcom apply-recovery --output-dir output/conversation \
  --generation-dir /local/archive/generations/generation-ID \
  --receipt-dir /local/archive/receipts
```

This is the command that changes active derived files. Application verifies the prepared generation and rechecks source hashes under the package's lock. It must refuse a changed source, stale plan, invalid generation, or unsafe source assessment. It never rewrites source responses, source JSONL, query scope, observer evidence, cursor, queue, or completion status to make a validator pass. Store application receipts outside the dataset.

Do not apply while a legacy collector writes the same dataset. Recovery locks coordinate cooperating recovery callers; they do not lock out collectors, external synchronization, editors, or older code that ignores those locks. Coordinate or stop every other writer before application. File replacement is atomic per file, not a guarantee of a whole-directory transaction against arbitrary writers. A synchronized directory can still be overwritten after a successful operation.

## Acceptance checks and analysis handoff

Before accepting a recovery:

- Preserve the full failed directory and hash inventory.
- Require a permitted assessment; check the frozen query and compare caller-known observer annotations with the retained plan/receipt. This is not historical login verification.
- Verify sources before and after preparation/application are byte-identical.
- Check profile history, authors, edge source records, table counts, and collection status with both `validate_collection()` and `validate_tables()`.
- Verify the generation against its independent receipt and repeat a stable read.
- Record that a partial collection stays partial. Report isolation and excluded sources in any aggregate analysis.

Use one verified generation for a merge. Do not read private posts from one generation and public edges from another. Missing target posts and unresolved target authors remain valid coverage conditions; an edge whose **source** is absent is an integrity problem. Refuse or isolate an inconsistent input and preserve the full failed analysis cohort before retrying. Rebuilding source records or reconstructing a missing latest checkpoint is outside this derived-file recovery API.

## Reducing recurrence

Use a local active path that is not participating in synchronization, keep independently verified generation receipts, and check input integrity before each analysis publication. These measures reduce risk; they do not establish who overwrote a file or guarantee protection against all external writers. Where a task has progressed normally since an earlier incident, invalidate the old recovery plan and assess its current source again.

CLI status is 0 only for `allowed`/`ready`/`valid`/`applied` respectively, 3 for a blocked or invalid report, and 2 for an input or operational error. Existing ordinary export/finalization now raise `IntegrityError` for unsafe sources, so calling them is not a way to bypass this workflow.
