# Sharing post-ID datasets

`export_post_ids()` makes a small, verified sharing package from a saved pyXcom
post collection. It writes **only post IDs and a restricted manifest**. Source
text, authors, observer identities, queries, browser profiles, raw responses and
local paths remain in the private collection.

```python
from pyxcom.datasets import export_post_ids, validate_id_dataset, iter_post_ids

manifest = export_post_ids(
    "private_collection",
    "new_public_id_dataset",
    roles=["main", "comment"],
)
print(manifest["count"])
print(validate_id_dataset("new_public_id_dataset")["valid"])
```

The destination must not exist and must be outside the source collection. Source
and destination cannot be ancestors of one another. Both scopes use the same
process locks as the collection pool; stop collection before exporting. Locks
coordinate current pyXcom callers, not arbitrary external programs.

## Files and selection

The resulting directory contains exactly two files:

- `post_ids.csv`: UTF-8, LF line endings, one `post_id` column, unique positive
  decimal IDs in ascending numeric order.
- `manifest.json`: schema and package version, ID-file SHA-256, canonical source
  fingerprint, source row count, output count, selected roles, exclusion counts
  and coverage status. Its fingerprint is a hash of the canonical JSONL bytes;
  the private source is required to independently check that provenance.

The `roles` option accepts `main`, `comment` and `repost`; omission selects all
three. Main posts include original and quote posts. Roles use explicit stored
post relationships. Both `analysis` observations and `seed` records (such as a
conversation root) are eligible, subject to the selected post roles. Context-only
observations are always excluded; unknown observation roles are rejected. Missing
parent or quoted target IDs are not added as if they had been collected. All
retained canonical post IDs must be valid, even for excluded records. An empty
selection is valid and produces a header-only CSV; an empty `roles` list is an
input error.

Export requires modern `.pyxcom/posts.jsonl` data with verified source integrity
and a valid checkpoint assessment. Unanchored legacy sources, corrupt records
and interrupted publication states are rejected without rewriting the source.
Identical duplicate retained rows yield one ID and are counted as exclusions.

`collection_complete` records whether the saved collection finished its configured
traversal. `coverage_complete` is always `false`: this export cannot establish
that every eligible platform post was returned. Partial collections are accepted
when their retained records and saved checkpoint pass verification.

## Validation and reuse

`validate_id_dataset(path)` raises `IntegrityError` for invalid hashes, schemas,
counts, extra files, symlink files, noncanonical IDs, duplicates or incorrect
ordering. On success it returns `{"valid": True, ...manifest}`. Do not place
README files or other material inside this two-file directory; put project
explanations alongside it. This validation establishes artifact consistency, not
sampling completeness or the authenticity of a publisher.

`iter_post_ids(path)` validates the entire package before yielding the first ID.
Rehydrate with the existing authenticated client and existing `get_posts()` API:

```python
from pyxcom import XClient
from pyxcom.datasets import iter_post_ids

# Manually open Chrome and log in to X before this step.
with XClient.from_login(browser="chrome", profile="Default") as client:
    batch = []
    for post_id in iter_post_ids("new_public_id_dataset"):
        batch.append(post_id)
        if len(batch) == 50:
            posts = client.get_posts(batch)
            # Process the returned Post records locally.
            batch = []
    if batch:
        posts = client.get_posts(batch)
```

Deleted, protected or otherwise unavailable posts may no longer be retrievable;
engagement metrics and accessible text may differ at a later observation time.
Record the rehydration date and distinguish unavailable IDs from retrieved posts.

The manifest's `content_license: "not_granted"` means the exporter does not grant
rights to underlying posts. IDs remain linkable identifiers, not anonymized data.
Decide release scope and sharing conditions for each research project separately.
A software license does not automatically license platform content.
