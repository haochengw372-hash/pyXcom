"""Copy-only, bounded verification of missing comment ancestors."""

import csv
import json
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ._persistence import append_jsonl, atomic_json, read_jsonl
from ._pool_locks import ScopeLock
from .errors import IntegrityError, ParseError, PyXcomError, RateLimitError
from .integrity import assess_recovery
from .models import Post
from .parse import single_post
from .recovery import _files
from .transport import now_utc

if TYPE_CHECKING:
    from .client import XClient


def _directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise ValueError("Ancestry directory must not be a symbolic link")
    return path.resolve()


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _protected(payload: dict) -> bool:
    result = _mapping(
        _mapping(_mapping(payload.get("data")).get("tweetResult")).get("result")
    )
    tweet = _mapping(result.get("tweet", result))
    user = _mapping(
        _mapping(_mapping(tweet.get("core")).get("user_results")).get("result")
    )
    return any(
        _mapping(user.get(key)).get("protected") is True
        for key in ("privacy", "legacy")
    )


def save_comment_ancestry(
    client: "XClient",
    source_dir: str | Path,
    output_dir: str | Path,
    *,
    lookup_limit: int = 20,
) -> dict:
    """Verify exact parent chains without admitting records to the original sample.

    The cap counts single-post lookup calls, not transport retry attempts. A new
    supplement is required for every run; source files are read-only throughout.
    """
    if (
        isinstance(lookup_limit, bool)
        or not isinstance(lookup_limit, int)
        or lookup_limit < 0
    ):
        raise ValueError("lookup_limit must be a nonnegative integer")
    source, target = _directory(source_dir), _directory(output_dir)
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Source and supplement scopes must not overlap")
    with ExitStack() as stack:
        for scope in sorted((source, target)):
            stack.enter_context(ScopeLock(scope))
        if target.exists():
            raise FileExistsError("Ancestry supplement already exists")
        assessment = assess_recovery(source)
        if not assessment["allowed"] or assessment["record_integrity"] != "verified":
            raise IntegrityError(
                "Source collection did not pass integrity verification"
            )
        query = assessment["query"]
        if query.get("kind") != "post_comments":
            raise ValueError(
                "Ancestry verification requires a post_comments collection"
            )
        state = json.loads((source / ".pyxcom/state.json").read_text(encoding="utf-8"))
        conversation = state["conversation"]
        pending = conversation.get("pending", {})
        posts = {p["id"]: Post(**p) for p in read_jsonl(source / ".pyxcom/posts.jsonl")}
        posts.update({key: Post(**value) for key, value in pending.items()})
        root = str(query["root_post_id"])
        conversation_id = str(conversation.get("conversation_id", root))
        owner_path = source / ".pyxcom/pool-owner.json"
        owner = (
            json.loads(owner_path.read_text(encoding="utf-8"))
            if owner_path.exists()
            else {}
        )
        expected_observer = owner.get("observer_id")
        verified_observer = None
        fetched: dict[str, Post] = {}
        failures: dict[str, str] = {}
        raw_responses: dict[str, dict] = {}
        calls = 0
        stop_reason = None

        def lookup(identifier: str) -> tuple[Post | None, str | None]:
            nonlocal calls, stop_reason, verified_observer
            if identifier in failures:
                return None, failures[identifier]
            if stop_reason:
                return None, stop_reason
            if calls >= lookup_limit:
                return None, "lookup_budget"
            if verified_observer is None:
                verified_observer = str(client.check_login_identity())
                if expected_observer is not None and verified_observer != str(
                    expected_observer
                ):
                    raise ValueError(
                        "Ancestry lookup observer differs from source observer"
                    )
            if calls:
                time.sleep(client.delay)
            calls += 1
            payload = None
            try:
                payload = client.get_raw_post(identifier)
                if _protected(payload):
                    failures[identifier] = "protected_parent"
                    return None, failures[identifier]
                raw_responses[identifier] = payload
                try:
                    post = single_post(payload, captured_at_utc=now_utc())
                except (ParseError, ValueError, TypeError, KeyError, AttributeError):
                    failures[identifier] = "parent_not_returned"
                    return None, failures[identifier]
                if post.id != identifier:
                    failures[identifier] = "identity_conflict"
                elif post.conversation_id != conversation_id or post.reposted_post_id:
                    failures[identifier] = "conversation_conflict"
                else:
                    post = replace(
                        post, observation_role="context", discovery_source="x_ancestor"
                    )
                    posts[identifier] = fetched[identifier] = post
                    return post, None
            except ParseError:
                stop_reason = failures[identifier] = "request_error"
            except RateLimitError:
                stop_reason = failures[identifier] = "rate_limited"
            except PyXcomError:
                stop_reason = failures[identifier] = "request_error"
            return None, failures[identifier]

        def platform_depth(seed_depth: int | None) -> int | None:
            if seed_depth is None:
                return None
            current = posts[root]
            seen = {root}
            offset = 0
            while current.id != conversation_id:
                parent = posts.get(current.in_reply_to_id or "")
                if (
                    parent is None
                    or parent.id in seen
                    or parent.conversation_id != conversation_id
                ):
                    return None
                seen.add(parent.id)
                offset += 1
                current = parent
            return seed_depth + offset

        rows = []
        for identifier, value in pending.items():
            post = posts[identifier]
            chain = [identifier]
            current = post
            status = "resolved"
            while current.id != root:
                if (
                    current.conversation_id != conversation_id
                    or current.reposted_post_id
                ):
                    status = "conversation_conflict"
                    break
                parent_id = current.in_reply_to_id
                if parent_id is None or (
                    parent_id == conversation_id and conversation_id != root
                ):
                    status = "outside_seed_branch"
                    break
                if parent_id in chain:
                    status = "cycle"
                    break
                parent = posts.get(parent_id)
                if parent is None:
                    parent, failure = lookup(parent_id)
                    if parent is None:
                        status = failure or "parent_not_returned"
                        break
                if parent.conversation_id != conversation_id or parent.reposted_post_id:
                    status = "conversation_conflict"
                    break
                chain.append(parent_id)
                current = parent
            seed_depth = len(chain) - 1 if status == "resolved" else None
            within = None
            if seed_depth is not None:
                day = post.created_at_utc[:10]
                within = (
                    seed_depth <= query["max_depth"]
                    and (not query.get("since") or day >= query["since"])
                    and (not query.get("until") or day < query["until"])
                )
            rows.append(
                {
                    "post_id": identifier,
                    "root_post_id": conversation_id,
                    "seed_post_id": root,
                    "parent_post_id": value.get("in_reply_to_id"),
                    "ancestry_status": status,
                    "depth": platform_depth(seed_depth),
                    "seed_relative_depth": seed_depth,
                    "chain_ids": json.dumps(chain),
                    "within_requested_bounds": within,
                }
            )
        if _files(source) != assessment["input_sha256"]:
            raise IntegrityError("Source changed during ancestry verification")
        target.mkdir(parents=True, exist_ok=False)
        (target / "raw").mkdir()
        for name, records in (
            ("ancestor_posts.jsonl", (p.to_dict() for p in fetched.values())),
            ("pending_posts.jsonl", pending.values()),
        ):
            append_jsonl(target / name, records)
        with (target / "resolution.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "post_id",
                    "root_post_id",
                    "seed_post_id",
                    "parent_post_id",
                    "ancestry_status",
                    "depth",
                    "seed_relative_depth",
                    "chain_ids",
                    "within_requested_bounds",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        for identifier, payload in raw_responses.items():
            atomic_json(target / "raw" / f"{identifier}.json", payload)
        counts = {
            status: sum(r["ancestry_status"] == status for r in rows)
            for status in sorted({r["ancestry_status"] for r in rows})
        }
        from . import __version__

        report = {
            "schema": "pyxcom.comment-ancestry/1",
            "package_version": __version__,
            "captured_at_utc": now_utc(),
            "query": query,
            "source_sha256": assessment["input_sha256"],
            "lookup_limit": lookup_limit,
            "lookup_calls": calls,
            "stop_reason": stop_reason,
            "identity_verified": verified_observer is not None,
            "source_observer_bound": expected_observer is not None
            and verified_observer is not None,
            "status_counts": counts,
            "resolved_records": counts.get("resolved", 0),
            "unresolved_records": len(rows) - counts.get("resolved", 0),
            "context_records": len(fetched),
            "collection_complete": assessment["complete"],
            "source_collection_reason": assessment["reason"],
            "files_sha256": _files(target),
        }
        atomic_json(target / "manifest.json", report)
        return report
