# Saved-data rules

The 0.7.1 stability work groups recent failures around three shared boundaries: returned content versus coverage, user identity versus observations, and text versus file records. These rules apply to collection, continuation, offline export and validation.

## Returned content and coverage

A discovery page can contain readable posts and unavailable tweet slots. Readable posts are retained, and `manifest.json.source_content` records unavailable or unparsed observations and IDs when the response provides them. The collector does not infer the reason for unavailable content.

Known unavailable slots allow paging to continue. Unknown content shapes pause with `partial_source_content`, preserving readable records and the request cursor. A visible end reached after a content warning remains incomplete. A different stop reason can take precedence; inspect both `reason` and `source_content`.

Source-empty and no-progress counters are evaluated before date, author and quote filters. Three consecutive source-empty pages or five pages without new primary source IDs pause discovery. These counters survive resume calls; repeating a paused save does not request more pages until `retry_stalled=True` is supplied. Retrying preserves the query, cursor, raw responses, records and previously seen source IDs.

## User identity and profile observations

`user_id` identifies a user. Handles, names, biographies, verification flags and counts can change between captures. Post dataset exports group profiles by ID and display one selected observation in `users.csv`.

Selection is deterministic: latest valid UTC capture, then number of populated fields, then canonical JSON. Missing or invalid capture times remain unknown. Equal latest times are reported in `manifest.json.profile_observations.tied_latest_user_ids`; the tie-break does not establish the real order of those captures.

`profile_snapshots.csv` preserves source-labelled observations with capture time, old handle, complete `profile_json` and a stable snapshot ID. Existing handle-keyed `profiles.json` files remain readable. When saving profiles, old and incoming observations enter `.pyxcom/profile_observations.jsonl` before the current view is replaced; an unchanged snapshot is not appended again. Export and validation consume the same identity and selection rules. Differing valid account creation timestamps, or invalid nonempty creation timestamps, still fail.

## Text and persistence

JSONL uses LF to separate records. Unicode separators U+2028, U+2029 and U+0085 inside descriptions or nested JSON text stay inside their record. Reading does not normalize or rewrite that text. Appending to a valid last record that lacks its final LF adds the separator before new records, preserving the existing bytes as a prefix. Malformed JSON, non-object records and invalid nested profile JSON remain errors; validators report these failures instead of treating them as valid observations.

JSON checkpoints, profile views and table manifests share one writer that fully writes and flushes a temporary file before replacement. JSON and CSV replacement failures keep the previous destination and remove the temporary file. The profile history reader verifies the ledger and gathers saved snapshot IDs in one pass.

Keep `.pyxcom/` for resume and offline reconstruction. Public calls, saved queries, cursors and snapshot identities remain compatible; this release adds no multi-login scheduler. For exact API options and output fields, see the [API reference](api.md).
