# Verifying missing comment ancestors

X can return a child reply while omitting its parent. A saved `post_comments`
collection keeps such records in its private pending state, rather than assigning
them a guessed depth. `XClient.save_comment_ancestry()` verifies those parent chains
with bounded single-post lookups and writes a separate supplement.

```python
from pyxcom import XClient

# Sign in manually before running. Select the original collection's observer.
with XClient(profile="Default") as client:
    report = client.save_comment_ancestry(
        "data/conversation", "data/conversation_ancestry", lookup_limit=20,
    )
print(report["resolved_records"], report["unresolved_records"])
```

The source must pass retained-record and checkpoint integrity verification and
must be a `post_comments` collection. Both scopes are locked; overlapping paths,
existing destinations and symlink source contents are rejected. Source files,
cursors, roles, counts and partial status remain unchanged.

## Lookup rules

Only a record's explicit `in_reply_to_id` is requested. Each missing ID is looked
up once per invocation, including failed lookups, and shared ancestors reuse the
result. The returned ID and conversation must agree with the source. A chain is
resolved only when it reaches the exact requested seed, without a cycle. Other
branches of a reply-seeded conversation are not admitted.

Before the first lookup, the client verifies its current identity. If the source
has a pool observer binding, that identity must match; a mismatch raises before
parent content is requested. Older unbound sources remain explicitly unbound in
the manifest. No observer is inferred retroactively and no account rotation occurs.

`lookup_limit` is a nonnegative integer limiting single-post lookup calls, not
HTTP attempts. Identity checks, route discovery and the existing transport's
retries can add HTTP attempts. Zero makes no network calls. Calls respect client
delay; a rate limit or request error stops further parent lookups in that run.

## Outputs

| File | Contents |
| --- | --- |
| `resolution.csv` | One row per original pending record, verified links, depth and scope flag |
| `ancestor_posts.jsonl` | Newly fetched parents, all `observation_role="context"` |
| `pending_posts.jsonl` | Copy of the original pending records |
| `raw/<parent_id>.json` | Credential-free successful lookup payloads, including unavailable results |
| `manifest.json` | Source and supplement hashes, caps, counts, observer checks and source completeness |

`seed_post_id` is the requested collection seed; `root_post_id` is the platform
conversation root. `chain_ids` lists verified retained IDs from the pending record
back toward the seed; an unresolved chain does not include the unreturned parent.
`seed_relative_depth` counts links to the seed and is blank until that chain is
resolved. `depth` counts links to the platform root and also requires retained
evidence between a reply seed and that root; otherwise it stays blank. This method
does not make extra requests above a reply seed just to fill platform depth.

`within_requested_bounds` checks the pending record's verified seed-relative depth
and the original UTC date bounds (inclusive `since`, exclusive `until`). It is blank
for unresolved chains. A true flag is not automatic sample admission: supplement
records never alter the original `posts.csv` or `comments.csv`.

Statuses include `resolved`, `lookup_budget`, `parent_not_returned`, `rate_limited`,
`request_error`, `identity_conflict`, `conversation_conflict`, `outside_seed_branch`,
`cycle` and `protected_parent`. A successful response without a parseable parent
is `parent_not_returned`; that does not establish deletion, protection or absence
on the platform. Explicitly protected parent content is not retained or archived.

Each invocation uses a new output directory. There is no supplement resume or
automatic merging into the source. Output is published after traversal; a process
termination before publication can lose that run's in-memory supplement. Retain
the original source and compare dated supplements explicitly. The manifest's
`collection_complete` and `source_collection_reason` copy the original collection's
status, even if every pending chain has now been resolved.

## Recorded validation

On October 13, 2026, two retained personal-agent discussion trees contained three
pending replies. With their original authenticated observers, three parent lookup
calls resolved two chains in the first tree; the second tree's parent did not
return a parseable record. Source file inventories and hashes were unchanged.

The resolved replies had depths two and four. The source depth bound was three,
so only the depth-two record passed the depth/date scope flag. This illustrates why
resolving ancestry and admitting a reply to a research sample are separate steps.
Both original collections remained partial. These are recorded results, not a
coverage benchmark; later requests may return different content.

Eighteen focused offline regression tests cover exact chains, shared parents,
reply-seed boundaries, cycles, budgets, source integrity, identity mismatch,
malformed responses and protected content. The complete suite contained 454
passing tests at this release.
