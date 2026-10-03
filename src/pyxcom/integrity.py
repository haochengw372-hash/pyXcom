"""Read-only recovery gates for retained post collections.

A valid derived table is not proof that a collection checkpoint is current.
The assessment keeps those two questions separate and never writes a file.
"""

import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .client import XClient

from .comments import CommentTraversal, parse_conversation
from .errors import PyXcomError
from .models import Post
from .profiles import _entries, _snapshot, _views
from .parse import bottom_cursor, timeline_primary_posts
from .tables import _depths


def _inventory(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink():
        raise ValueError("symlink_input")
    result = {}
    if not directory.is_dir():
        raise ValueError("missing_directory")
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink_input")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = path.read_bytes()
    return result


def _hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def _object(files: dict[str, bytes], name: str) -> dict:
    value = json.loads(files[name]) if name in files else {}
    if not isinstance(value, dict):
        raise ValueError(f"invalid_object:{name}")
    return value


def _rows(files: dict[str, bytes], name: str) -> list[dict]:
    # JSONL records are separated by LF, not Unicode paragraph separators.
    result = []
    for line in files.get(name, b"").decode("utf-8").split("\n"):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"invalid_record:{name}")
            result.append(row)
    return result


def _source_name(files: dict[str, bytes], name: str) -> str:
    private = f".pyxcom/{name}"
    return private if private in files else name


def _pending(items: dict) -> dict:
    names = (
        "id",
        "author_id",
        "conversation_id",
        "in_reply_to_id",
        "created_at_utc",
        "observation_role",
    )
    return {key: {name: row.get(name) for name in names} for key, row in items.items()}


def _replay_comments(
    files: dict[str, bytes],
    hashes: dict[str, str],
    state: dict,
    internal: dict,
    records: dict[str, Post],
) -> bool:
    """Prove an advanced state against all retained requests, not an old prefix."""
    query = state.get("query", {})
    if query.get("kind") != "post_comments":
        return False
    logs = _rows(files, _source_name(files, "collection_log.jsonl"))
    responses = [
        row
        for row in logs
        if row.get("operation") == "TweetDetail" and row.get("raw_path")
    ]
    finishes = [row for row in logs if row.get("operation") == "finish"]
    if not responses or not finishes:
        return False
    if any(
        state.get(key) != finishes[-1].get(key)
        for key in ("query", "complete", "reason", "pages_fetched")
    ):
        return False
    root = str(query.get("root_post_id"))
    if root not in records:
        return False
    seed = records[root]
    index = 0

    class ReplayTransport:
        def conversation_page(self, focal, cursor=None):
            nonlocal index
            if index >= len(responses):
                raise ValueError("missing_response")
            row = responses[index]
            variables = row.get("variables", {})
            path = row.get("raw_path", "")
            if (
                str(focal) != str(variables.get("focalTweetId"))
                or cursor != variables.get("cursor")
                or not path.startswith(".pyxcom/raw/")
                or ".." in Path(path).parts
                or hashes.get(path) != row.get("raw_sha256")
            ):
                raise ValueError("request_evidence_mismatch")
            index += 1
            return json.loads(files[path])

    traversal = CommentTraversal(
        cast("XClient", SimpleNamespace(_x=ReplayTransport(), delay=0)),
        root,
        max_depth=query["max_depth"],
        max_comments=None,
        max_pages=len(responses),
        records={root: seed},
        state={},
        since=query.get("since"),
        until=query.get("until"),
    )
    list(traversal.pages())
    actual, replayed = state.get("conversation", {}), traversal.state
    fields = (
        "queue",
        "visited",
        "expanded",
        "depths",
        "accepted_ids",
        "excluded_ids",
        "pagination_warnings",
        "empty_pages",
        "conversation_id",
        "unresolved_count",
    )
    if any(actual.get(key) != replayed.get(key) for key in fields):
        return False
    if _pending(actual.get("pending", {})) != _pending(replayed.get("pending", {})):
        return False
    if set(records) != set(traversal.records):
        return False
    names = (
        "author_id",
        "in_reply_to_id",
        "conversation_id",
        "created_at_utc",
        "observation_role",
        "post_role",
        "quoted_post_id",
        "reposted_post_id",
    )
    if any(
        any(
            getattr(post, key) != getattr(traversal.records[identifier], key)
            for key in names
        )
        for identifier, post in records.items()
    ):
        return False
    raw_seeds = [
        post
        for row in responses
        for post in parse_conversation(json.loads(files[row["raw_path"]]))[0]
        if post.id == root
    ]
    identity = (
        "author_id",
        "created_at_utc",
        "conversation_id",
        "in_reply_to_id",
        "quoted_post_id",
        "reposted_post_id",
    )
    return (
        bool(raw_seeds)
        and all(
            all(getattr(post, key) == getattr(seed, key) for key in identity)
            for post in raw_seeds
        )
        and state.get("pages_fetched") == index == len(responses)
        and internal.get("post_count") == len(records)
        and state.get("complete") == traversal.complete
        and state.get("reason") == traversal.reason
    )


def _has_append_prefix(data: bytes, expected: str | None) -> bool:
    if not expected:
        return False
    digest = hashlib.sha256()
    if digest.hexdigest() == expected:
        return True
    for line in data.splitlines(keepends=True):
        # Bytes.splitlines only recognises byte CR/LF, not Unicode separators.
        digest.update(line)
        if digest.hexdigest() == expected:
            return True
    return False


def _timeline_unpublished_tail(files, hashes, state, internal, public, records) -> bool:
    """Corroborate an unpublished UserTimeline append without endorsing resume."""
    query = state.get("query", {})
    if (
        query.get("kind") != "user_timeline"
        or state.get("reason") != "in_progress"
        or state.get("complete") is not False
        or query != internal.get("query")
    ):
        return False
    pages = state.get("pages_fetched", 0)
    prior_pages = internal.get("pages_fetched", 0)
    if (
        not isinstance(pages, int)
        or not isinstance(prior_pages, int)
        or pages <= prior_pages
    ):
        return False
    source = _source_name(files, "posts.jsonl")
    old_post_hash = internal.get("sha256", {}).get("posts.jsonl")
    if (
        not _has_append_prefix(files[source], old_post_hash)
        or public.get("source_sha256", {}).get(source) != old_post_hash
    ):
        return False
    for name, expected in public.get("source_sha256", {}).items():
        if name == _source_name(files, "state.json"):
            continue
        if hashes.get(name) != expected and (
            not name.endswith(("posts.jsonl", "metric_snapshots.jsonl"))
            or not _has_append_prefix(files.get(name, b""), expected)
        ):
            return False
    logs = _rows(files, _source_name(files, "collection_log.jsonl"))
    responses = [
        row
        for row in logs
        if row.get("operation") == "user_timeline" and row.get("raw_path")
    ]
    finishes = [row for row in logs if row.get("operation") == "finish"]
    if (
        len(responses) != pages
        or not finishes
        or any(
            finishes[-1].get(key) != internal.get(key)
            for key in ("query", "pages_fetched", "reason", "complete")
        )
    ):
        return False

    # Compare explicit raw content and author/relationship fields while keeping
    # original observation times in the retained ledger, never inventing them.
    def content(post):
        return {
            key: value
            for key, value in post.to_dict().items()
            if key not in {"captured_at_utc", "discovery_source", "discovery_url"}
        }

    raw_posts: dict[str, dict] = {}
    raw_observations: list[dict] = []
    cursor = None
    user_id = None
    prefix_ids = set()
    for index, row in enumerate(responses):
        variables = row.get("variables", {})
        path = row["raw_path"]
        if (
            not path.startswith(".pyxcom/raw/")
            or ".." in Path(path).parts
            or hashes.get(path) != row.get("raw_sha256")
            or variables.get("cursor") != cursor
            or variables.get("timeline") != query.get("timeline")
        ):
            return False
        if user_id is None:
            user_id = str(variables.get("userId"))
        if str(variables.get("userId")) != user_id:
            return False
        payload = json.loads(files[path])
        for post in timeline_primary_posts(
            payload, captured_at_utc="2000-01-01T00:00:00+00:00"
        ):
            if (
                post.author_id == user_id
                and post.post_role
                == ("comment" if query["timeline"] == "replies" else "main")
                and (
                    not query.get("since") or post.created_at_utc[:10] >= query["since"]
                )
                and (
                    not query.get("until") or post.created_at_utc[:10] < query["until"]
                )
            ):
                parsed = content(post)
                raw_posts.setdefault(post.id, parsed)
                raw_observations.append(parsed)
        if index + 1 == prior_pages:
            prefix_ids = set(raw_posts)
        cursor = bottom_cursor(payload)
    if (
        cursor != state.get("cursor")
        or set(raw_posts) != set(records)
        or len(prefix_ids) != internal.get("post_count")
    ):
        return False
    if any(
        content(post) != raw_posts[identifier] for identifier, post in records.items()
    ):
        return False
    observations = [
        Post(**row) for row in _rows(files, _source_name(files, "observations.jsonl"))
    ]
    if not observations or set(post.id for post in observations) != set(records):
        return False
    if [content(post) for post in observations] != raw_observations:
        return False
    observed_records = {
        json.dumps(post.to_dict(), sort_keys=True) for post in observations
    }
    if any(
        json.dumps(post.to_dict(), sort_keys=True) not in observed_records
        for post in records.values()
    ):
        return False
    metric_values = (
        "like_count",
        "reply_count",
        "repost_count",
        "quote_count",
        "view_count",
        "bookmark_count",
    )
    observation_metrics = {}
    for post in observations:
        row = {
            "post_id": post.id,
            "retrieved_at_utc": post.captured_at_utc,
            "time_status": "observed" if post.captured_at_utc else "unknown",
            **{name: getattr(post, name) for name in metric_values},
        }
        if all(row[name] is None for name in metric_values):
            continue
        identifier = hashlib.sha256(
            json.dumps(row, sort_keys=True).encode()
        ).hexdigest()
        observation_metrics[identifier] = {**row, "snapshot_id": identifier}
    actual_metrics = _rows(files, _source_name(files, "metric_snapshots.jsonl"))
    return {row.get("snapshot_id") for row in actual_metrics} == set(
        observation_metrics
    ) and all(
        row == observation_metrics.get(row["snapshot_id"]) for row in actual_metrics
    )


def assess_recovery(
    output_dir: str | Path, *, expected_query=None, binding=None, previous_receipt=None
) -> dict:
    """Assess a stable saved collection without export, migration or acquisition.

    ``binding`` records caller-supplied provenance; it does not authenticate a
    historical login. A prior independently retained receipt is a generation
    anchor and is required to detect an entirely self-consistent old generation.
    """
    from . import __version__

    report = {
        "schema_version": "1",
        "allowed": False,
        "resume_allowed": False,
        "record_integrity": "unknown",
        "latest_checkpoint": "unknown",
        "coverage_complete": None,
        "errors": [],
        "input_sha256": {},
        "source_sha256": {},
        "query": {},
        "binding": binding,
        "binding_provenance": "caller_supplied" if binding is not None else "unknown",
        "complete": None,
        "reason": None,
        "package_version": __version__,
    }
    directory = Path(output_dir).expanduser()
    errors = report["errors"]
    tail_verified = False
    try:
        files = _inventory(directory)
        hashes = _hashes(files)
        report["input_sha256"] = hashes
        report["source_sha256"] = {
            key: value for key, value in hashes.items() if key.startswith(".pyxcom/")
        }
        source = _source_name(files, "posts.jsonl")
        if source not in files:
            raise ValueError("missing_canonical_posts")
        records: dict[str, Post] = {}
        for row in _rows(files, source):
            post = Post(**row)
            if not post.id or not post.author_id:
                raise ValueError("invalid_post_identity")
            if post.id in records and records[post.id] != post:
                raise ValueError("conflicting_post_identity")
            records[post.id] = post
        _depths(records)  # Missing target/author stubs are permitted; cycles are not.
        snapshots = {}
        for row in _rows(files, _source_name(files, "profile_observations.jsonl")):
            expected = _snapshot(
                json.loads(row["profile_json"]), row["source_file"], row["source_key"]
            )
            if row != expected:
                raise ValueError("invalid_profile_observation")
            snapshots[row["snapshot_id"]] = row
        profile_name = _source_name(files, "profiles.json")
        if profile_name in files:
            for row in _entries(json.loads(files[profile_name]), profile_name):
                snapshots[row["snapshot_id"]] = row
        _views(list(snapshots.values()))
        metrics = _rows(files, _source_name(files, "metric_snapshots.jsonl"))
        if any(row.get("post_id") not in records for row in metrics):
            raise ValueError("metric_source_missing")
        state_name = _source_name(files, "state.json")
        state = _object(files, state_name)
        internal = _object(files, ".pyxcom/manifest.json")
        public = _object(files, "manifest.json")
        query = state.get("query", internal.get("query", public.get("query", {})))
        report.update(
            query=query,
            complete=state.get("complete", internal.get("complete")),
            reason=state.get("reason", internal.get("reason")),
        )
        # Successful traversal is still not a claim of historical representativeness.
        report["coverage_complete"] = report["complete"]
        if expected_query is not None and query != expected_query:
            errors.append("query_mismatch")
        for anchor in (internal, public):
            if anchor.get("query") is not None and anchor["query"] != query:
                errors.append("query_anchor_mismatch")
        source_bad = []
        for name, expected in public.get("source_sha256", {}).items():
            if hashes.get(name) != expected:
                source_bad.append(name)
        for name, expected in internal.get("sha256", {}).items():
            path = f".pyxcom/{name}"
            # Internal CSV is derivable; the canonical JSONL is not.
            if name.endswith(".jsonl") and hashes.get(path) != expected:
                source_bad.append(path)
        if (
            state
            and public
            and public.get("source_sha256", {}).get(source) is None
            and internal.get("sha256", {}).get("posts.jsonl") is None
        ):
            errors.append("canonical_anchor_missing")
        for name in source_bad:
            if name != state_name:
                errors.append(f"source_hash_mismatch:{name}")
        if internal.get("post_count", len(records)) != len(records):
            errors.append("canonical_count_mismatch")
        if public.get("counts", {}).get("source_records", len(records)) > len(records):
            errors.append("canonical_behind_public")
        if public.get("profile_observations", {}).get("count", len(snapshots)) > len(
            snapshots
        ):
            errors.append("profile_history_behind_public")
        # Newer public rows must not be discarded by re-exporting older private data.
        for table, identifier in (
            ("posts.csv", "post_id"),
            ("comments.csv", "comment_id"),
            ("interactions.csv", "interaction_id"),
            ("context_posts.csv", "post_id"),
        ):
            if table in files and public.get("sha256", {}).get(table) == hashes[table]:
                rows = csv.DictReader(io.StringIO(files[table].decode("utf-8-sig")))
                if any(row.get(identifier) not in records for row in rows):
                    errors.append(f"canonical_behind_table:{table}")
        logs = _rows(files, _source_name(files, "collection_log.jsonl"))
        for row in logs:
            if row.get("raw_path") and (
                hashes.get(row["raw_path"]) != row.get("raw_sha256")
                or not row["raw_path"].startswith(".pyxcom/raw/")
                or ".." in Path(row["raw_path"]).parts
            ):
                errors.append("raw_response_evidence_mismatch")
        finishes = [row for row in logs if row.get("operation") == "finish"]
        if finishes and any(
            state.get(key) != finishes[-1].get(key)
            for key in ("query", "complete", "reason", "pages_fetched")
        ):
            errors.append("checkpoint_finish_mismatch")
        checkpoint_mismatch = any(
            key in internal and state.get(key) != internal[key]
            for key in ("query", "pages_fetched", "complete", "reason")
        )
        public_checkpoint_mismatch = any(
            key in public and state.get(key) != public[key]
            for key in ("query", "pages_fetched", "complete", "reason")
        )
        if (
            public.get("captured_at_utc")
            and internal.get("captured_at_utc")
            and public["captured_at_utc"] > internal["captured_at_utc"]
        ):
            errors.append("checkpoint_behind_public_generation")
        for anchor in (public, internal):
            if anchor.get("pages_fetched", 0) > state.get("pages_fetched", 0):
                errors.append("checkpoint_behind_manifest")
        conversation = state.get("conversation", {})
        if any(
            str(identifier) not in records
            for identifier in conversation.get("accepted_ids", [])
        ):
            errors.append("checkpoint_records_missing")
        receipt_content_changed = False
        if previous_receipt:
            if previous_receipt.get("query", query) != query:
                errors.append("receipt_query_mismatch")
            if previous_receipt.get("binding", binding) != binding:
                errors.append("receipt_binding_mismatch")
            prior_hashes = previous_receipt.get("source_sha256", {})
            changed = {
                name
                for name, value in prior_hashes.items()
                if hashes.get(name) != value
            }
            if changed:
                errors.append("receipt_source_changed")
                receipt_content_changed = any(
                    name != state_name and not name.endswith((".csv", "manifest.json"))
                    for name in changed
                )
            if previous_receipt.get("pages_fetched", 0) > state.get("pages_fetched", 0):
                errors.append("checkpoint_behind_receipt")
        tail_candidate = (
            state.get("reason") == "in_progress"
            and state.get("complete") is False
            and isinstance(state.get("pages_fetched"), int)
            and isinstance(internal.get("pages_fetched"), int)
            and state["pages_fetched"] > internal["pages_fetched"]
            and not any(
                error.startswith(("canonical_behind_table:", "checkpoint_behind_"))
                or error in {"canonical_behind_public", "profile_history_behind_public"}
                for error in errors
            )
        )
        tail_verified = False
        if tail_candidate:
            try:
                tail_verified = _timeline_unpublished_tail(
                    files, hashes, state, internal, public, records
                )
            except (ValueError, TypeError, KeyError, AttributeError, PyXcomError):
                pass
        content_conflict = receipt_content_changed or any(
            error.startswith(("source_hash_mismatch:", "canonical_behind_table:"))
            or error
            in {
                "canonical_count_mismatch",
                "canonical_behind_public",
                "profile_history_behind_public",
                "raw_response_evidence_mismatch",
            }
            for error in errors
        )
        content_anchored = public.get("source_sha256", {}).get(source) == hashes.get(
            source
        ) or internal.get("sha256", {}).get("posts.jsonl") == hashes.get(source)
        report["record_integrity"] = (
            "conflicting"
            if content_conflict
            else "verified"
            if content_anchored
            else "unanchored_valid"
        )
        if tail_candidate:
            # An interrupted producer can publish committed source rows before
            # finish creates a coherent public generation. Never rehash it away.
            report["record_integrity"] = "verified" if tail_verified else "unknown"
            report["latest_checkpoint"] = (
                "incomplete_publication" if tail_verified else "unknown"
            )
            errors.append(
                "publication_pending" if tail_verified else "publication_tail_unproven"
            )
        elif errors:
            if any(error.startswith("checkpoint_behind_") for error in errors):
                report["latest_checkpoint"] = "regressed"
            elif (
                not any(error.startswith("checkpoint_") for error in errors)
                and not checkpoint_mismatch
                and not public_checkpoint_mismatch
                and state_name not in source_bad
                and internal
                and not content_conflict
                and public.get("source_sha256", {}).get(state_name)
                == hashes.get(state_name)
            ):
                # A caller's query/binding disagreement does not erase the
                # independently retained manifest's record/checkpoint evidence.
                report["latest_checkpoint"] = "verified_manifest_anchor"
        elif not state and not internal and not public:
            report.update(
                record_integrity="unanchored_valid",
                latest_checkpoint="not_present",
                allowed=True,
            )
        elif checkpoint_mismatch:
            errors.append("checkpoint_manifest_mismatch")
        elif state_name in source_bad or public_checkpoint_mismatch:
            try:
                replay_verified = _replay_comments(
                    files, hashes, state, internal, records
                )
            except (ValueError, TypeError, KeyError, AttributeError, PyXcomError):
                replay_verified = False
            if replay_verified:
                report.update(
                    record_integrity="verified",
                    latest_checkpoint="verified_response_replay",
                    allowed=True,
                )
            else:
                errors.append("checkpoint_replay_unproven")
        elif (
            public.get("source_sha256", {}).get(state_name) == hashes.get(state_name)
            and internal
        ):
            report.update(
                record_integrity="verified",
                latest_checkpoint="verified_manifest_anchor",
                allowed=True,
            )
        else:
            errors.append("checkpoint_anchor_missing")
            report["record_integrity"] = "unanchored_valid"
        report["pages_fetched"] = state.get("pages_fetched")
        report["record_count"] = len(records)
        report["profile_observation_count"] = len(snapshots)
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        AttributeError,
        PyXcomError,
    ) as error:
        # Never include raw record text or exception values in safe reports.
        code = (
            str(error)
            if isinstance(error, ValueError)
            and str(error).split(":")[0]
            in {
                "symlink_input",
                "missing_directory",
                "missing_canonical_posts",
                "invalid_post_identity",
                "conflicting_post_identity",
                "invalid_profile_observation",
                "metric_source_missing",
            }
            else f"invalid_source:{type(error).__name__}"
        )
        errors.append(code)
        report["record_integrity"] = "blocked"
    try:
        if _hashes(_inventory(directory)) != report["input_sha256"]:
            errors.append("input_changed")
    except (ValueError, OSError):
        errors.append("input_changed")
    if errors:
        report["allowed"] = False
    if "input_changed" in errors:
        report["record_integrity"] = "unknown"
        report["latest_checkpoint"] = "unknown"
    report["errors"] = sorted(set(errors))
    report["resume_allowed"] = report["allowed"]
    if tail_verified:
        tail_resume_errors = {
            "canonical_count_mismatch",
            "checkpoint_finish_mismatch",
            "publication_pending",
            f"source_hash_mismatch:{_source_name(files, 'posts.jsonl')}",
            f"source_hash_mismatch:{_source_name(files, 'metric_snapshots.jsonl')}",
        }
        report["resume_allowed"] = report[
            "latest_checkpoint"
        ] == "incomplete_publication" and set(report["errors"]).issubset(
            tail_resume_errors
        )
    return report
