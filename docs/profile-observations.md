# Profile observations and renamed accounts

`users.csv` has one row per stable user ID. Handles, names, descriptions,
verification and counts can change between captures. The displayed profile is
the snapshot with the latest valid, timezone-aware `captured_at_utc` instant.
Missing or invalid capture times remain unknown and cannot replace a dated
observation. Ties use nonmissing-field count, then canonical JSON order; the
manifest lists tied user IDs. No collection order is used as a substitute for
time, and missing fields in the chosen snapshot are not filled with older counts.

`profile_snapshots.csv` preserves profile observations, including old handles,
capture times, full `profile_json`, source file and source key. Its deterministic
snapshot ID covers the record and source labels. Repeated identical copies of
the same source-labelled observation are represented once; distinct original
list entries keep their distinct source keys. Observation counts therefore need
not equal user counts. The manifest records the policy, counts, unknown times,
source hashes and table hash. Offline validation checks the selected user view
and snapshot table against the retained sources.

Export leaves source profiles untouched. New collection saves keep
`profiles.json` compatible with the existing handle-keyed format and retain both
previous and incoming records in `.pyxcom/profile_observations.jsonl` before
replacing that view. This preserves subsequent same-handle updates across calls.
Older observations already overwritten before this change cannot be recreated
from `profiles.json` alone; existing raw archives remain their evidence.

Different nonempty account creation timestamps for the same ID remain an
explicit identity conflict. Equivalent timezone representations are compatible;
a missing creation timestamp does not contradict a known one. Invalid IDs or
creation timestamps also fail validation. Stored queries, account task folders
and continuation cursors retain their original identities.
