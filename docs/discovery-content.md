# Discovery content and coverage

Native discovery retains readable primary posts even when another slot on the
same page is unavailable. Empty or null tweet results and known tombstone or
unavailable results are counted as unavailable observations. These counts do not
establish why a post is missing. Embedded quotations remain outside primary
discovery results.

Unavailable slots do not make a page source-empty. Their public entry IDs, where
present, participate in pagination progress checks. Collection can continue to
the next cursor, while `source_content` in the manifest retains the coverage
warning, observation counts, and available public identifiers. Counts describe
returned-page observations and can include repeated slots; identifier lists are
deduplicated.

Unknown or malformed content saves readable rows but pauses with
`partial_source_content`, keeping the request cursor for an explicit
`retry_stalled=True` attempt after diagnosis. An observed source end with an
unavailable-content warning also remains incomplete with this reason. Other
stopping conditions, such as `page_limit` or `repeated_cursor`, retain their own
reason alongside `source_content`; they do not erase the warning.

In-memory discovery methods retain readable results and expose completion,
reason, and coverage warnings in `client.last_discovery_collection`. Saved calls
archive the original response and link warning log entries to that raw file.
Date filtering still applies to readable posts before admission to the dataset.
