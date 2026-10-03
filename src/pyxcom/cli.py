"""Command-line entry point for pyXcom."""

import argparse
import json
import sys
from pathlib import Path

from .client import XClient
from .layout import internal_dir
from .errors import PyXcomError
from .integrity import assess_recovery
from .recovery import apply_recovery, prepare_recovery, verify_generation
from .schema import apply_role_schema, write_schema_report
from .tables import export_tables
from .validate import finalize_collection, validate_collection


def _budget(value: str) -> int | None:
    if value.lower() == "all":
        return None
    try:
        budget = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a positive integer or 'all'") from exc
    if budget <= 0:
        raise argparse.ArgumentTypeError("use a positive integer or 'all'")
    return budget


def _depth(value: str) -> int:
    budget = _budget(value)
    if budget is None:
        raise argparse.ArgumentTypeError("depth must be a positive integer")
    return budget


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--browser", choices=("chrome", "edge", "safari"), default="chrome"
    )
    common.add_argument(
        "--profile",
        help="Chrome/Edge profile folder, e.g. 'Profile 3'; omit for Safari",
    )
    common.add_argument(
        "--cookie-db",
        type=Path,
        help="Chrome/Edge Cookies DB or Safari Cookies.binarycookies path",
    )
    common.add_argument(
        "--proxy", help="HTTP or SOCKS proxy URL for X and mirror requests"
    )
    common.add_argument(
        "--mirror-base",
        help="Explicit HTTPS mirror for cross-user search; sends search terms to that mirror",
    )
    common.add_argument(
        "--delay", type=float, default=1.0, help="Seconds between pages"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyxcom", description="Collect public X data")
    commands = parser.add_subparsers(dest="command", required=True)
    # Recovery commands are offline and never construct a browser client.
    for name, help_text in (
        ("assess", "Read-only assessment of a saved dataset's recovery safety"),
        (
            "prepare-recovery",
            "Prepare a verified generation without changing the dataset",
        ),
        (
            "apply-recovery",
            "Explicitly apply a prepared generation after source checks",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--output-dir", dest="output", type=Path, required=True)
        if name in ("assess", "prepare-recovery"):
            command.add_argument(
                "--expected-query",
                type=Path,
                help="JSON object with the expected saved query",
            )
            command.add_argument(
                "--binding",
                type=Path,
                help="JSON object with caller-known public observer binding",
            )
            command.add_argument(
                "--previous-receipt",
                type=Path,
                help="Previously verified generation receipt JSON",
            )
        if name == "prepare-recovery":
            command.add_argument(
                "--plan", type=Path, help="Previously saved assessment JSON"
            )
        if name in ("prepare-recovery", "apply-recovery"):
            command.add_argument("--generation-dir", type=Path, required=True)
        if name == "apply-recovery":
            command.add_argument("--receipt-dir", type=Path, required=True)
    generation = commands.add_parser(
        "verify-generation", help="Verify a prepared generation offline"
    )
    generation.add_argument("--generation-dir", type=Path, required=True)
    common = _common_parser()
    profile = commands.add_parser(
        "user", aliases=["profile"], parents=[common], help="Get public user profile"
    )
    profile.set_defaults(command="profile")
    profile.add_argument("handle")
    profile.add_argument("--output", type=Path)
    post = commands.add_parser("post", parents=[common], help="Get one public post")
    post.add_argument("post_id_or_url")
    post.add_argument("--output", type=Path)
    raw = commands.add_parser(
        "raw-post", parents=[common], help="Get raw public X JSON"
    )
    raw.add_argument("post_id_or_url")
    raw.add_argument("--output", type=Path)
    posts = commands.add_parser(
        "user-posts",
        aliases=["posts"],
        parents=[common],
        help="Save authored main posts",
    )
    posts.set_defaults(command="posts")
    posts.add_argument("handle")
    posts.add_argument("--timeline", choices=("posts", "replies"), default="posts")
    posts.add_argument("--since", help="Inclusive UTC date YYYY-MM-DD")
    posts.add_argument("--until", help="Exclusive UTC date YYYY-MM-DD")
    posts.add_argument("--max-pages", type=int)
    posts.add_argument("--limit", type=int)
    posts.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    activity = commands.add_parser(
        "user-activity",
        aliases=["activity"],
        parents=[common],
        help="Save posts and authored replies",
    )
    activity.set_defaults(command="activity")
    activity.add_argument("handle")
    activity.add_argument("--since")
    activity.add_argument("--until")
    activity.add_argument("--max-pages", type=int)
    activity.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    search = commands.add_parser(
        "search-posts",
        aliases=["search"],
        parents=[common],
        help="Search keyword, dates, and user",
    )
    search.set_defaults(command="search")
    search.add_argument("keyword", help="Literal word or phrase for direct user search")
    search.add_argument(
        "--handle", "--user", dest="user", help="Author handle for direct X search"
    )
    search.add_argument("--since", help="Inclusive UTC date YYYY-MM-DD")
    search.add_argument("--until", help="Exclusive UTC date YYYY-MM-DD")
    search.add_argument("--max-pages", type=int)
    search.add_argument("--limit", type=int)
    search.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    batch = commands.add_parser(
        "users-activity",
        aliases=["batch"],
        parents=[common],
        help="Collect several accounts and write a combined report",
    )
    batch.set_defaults(command="batch")
    batch.add_argument("--handles", nargs="+", required=True)
    batch.add_argument("--since", required=True)
    batch.add_argument("--until", required=True)
    batch.add_argument("--pages-per-round", type=int, default=5)
    batch.add_argument(
        "--rounds", type=int, default=1, help="0 keeps resuming until complete"
    )
    batch.add_argument("--wait-on-rate-limit", action="store_true")
    batch.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    validate = commands.add_parser(
        "validate", help="Check saved rows, scope, and SHA-256 hashes"
    )
    validate.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    finalize = commands.add_parser(
        "finalize", help="Rebuild CSV and report after an interrupted run"
    )
    finalize.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    schema = commands.add_parser(
        "schema", help="Report field coverage and optionally add main/comment columns"
    )
    schema.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    schema.add_argument(
        "--apply", action="store_true", help="Backfill role/type in saved CSV and JSONL"
    )
    tables = commands.add_parser(
        "export", help="Export saved records as users/posts/comments relational tables"
    )
    tables.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    replies = commands.add_parser(
        "user-replies", parents=[common], help="Save replies authored by an account"
    )
    replies.set_defaults(command="posts", timeline="replies", limit=None)
    replies.add_argument("handle")
    replies.add_argument("--since")
    replies.add_argument("--until")
    replies.add_argument("--max-pages", type=int)
    replies.add_argument("--limit", type=int)
    replies.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    comments = commands.add_parser(
        "post-comments",
        parents=[common],
        help="Collect replies below a post or reply, including nested replies",
    )
    comments.add_argument("post_id_or_url")
    comments.add_argument("--max-depth", type=_depth, default=2)
    comments.add_argument("--max-comments", type=_budget, default=100)
    comments.add_argument("--max-pages", type=_budget, default=20)
    comments.add_argument("--since", help="Inclusive UTC date YYYY-MM-DD")
    comments.add_argument("--until", help="Exclusive UTC date YYYY-MM-DD")
    comments.add_argument(
        "--output-dir", "--output", dest="output", type=Path, required=True
    )
    for name, argument, help_text in (
        ("search-query", "query", "Save native X Latest search results"),
        ("post-quotes", "post_id_or_url", "Save visible posts quoting one post"),
        ("user-reposts", "handle", "Save native repost activities by one account"),
    ):
        command = commands.add_parser(name, parents=[common], help=help_text)
        command.add_argument(argument)
        command.add_argument("--since", help="Inclusive UTC date YYYY-MM-DD")
        command.add_argument("--until", help="Exclusive UTC date YYYY-MM-DD")
        command.add_argument("--max-pages", type=_budget)
        command.add_argument("--limit", type=_budget)
        command.add_argument(
            "--retry-stalled",
            action="store_true",
            help="Explicitly retry discovery paused by source empty/no-progress pages",
        )
        command.add_argument(
            "--output-dir", "--output", dest="output", type=Path, required=True
        )
    for name, argument, help_text in (
        ("user-followers", "user_id", "Save observed followers of a user ID"),
        ("user-following", "user_id", "Save observed accounts followed by a user ID"),
        (
            "post-reposters",
            "post_id_or_url",
            "Save visible reposters; action times are unknown",
        ),
    ):
        command = commands.add_parser(name, parents=[common], help=help_text)
        command.add_argument(argument)
        command.add_argument("--max-pages", type=_budget)
        command.add_argument("--limit", type=_budget)
        command.add_argument("--snapshot-id")
        command.add_argument(
            "--output-dir", "--output", dest="output", type=Path, required=True
        )
    return parser


def _write_or_print(value: dict, output: Path | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if output is None:
        print(text)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(str(output))


def _recovery_json(path: Path | None, label: str) -> dict | None:
    """Read caller evidence without echoing file content or credential values."""
    if path is None:
        return None

    def unique_object(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = child
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
    except (OSError, UnicodeError, ValueError):
        raise ValueError(f"{label} must be a readable JSON object") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    forbidden = {
        "auth_token",
        "ct0",
        "cookie",
        "cookies",
        "cookie_db",
        "password",
        "token",
        "access_token",
        "api_key",
        "secret",
        "authorization",
        "headers",
    }

    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in forbidden:
                    raise ValueError(f"{label} must not contain credentials")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in (
            "assess",
            "prepare-recovery",
            "verify-generation",
            "apply-recovery",
        ):
            if args.command in ("assess", "prepare-recovery"):
                options = {
                    "expected_query": _recovery_json(
                        args.expected_query, "expected query"
                    ),
                    "binding": _recovery_json(args.binding, "binding"),
                    "previous_receipt": _recovery_json(
                        args.previous_receipt, "previous receipt"
                    ),
                }
                if args.command == "assess":
                    report = assess_recovery(args.output, **options)
                else:
                    report = prepare_recovery(
                        args.output,
                        plan=_recovery_json(args.plan, "plan"),
                        generation_dir=args.generation_dir,
                        **options,
                    )
            elif args.command == "verify-generation":
                report = verify_generation(args.generation_dir)
            else:
                report = apply_recovery(
                    args.output,
                    generation_dir=args.generation_dir,
                    receipt_dir=args.receipt_dir,
                )
            print(json.dumps(report, ensure_ascii=False))
            success_key = {
                "assess": "allowed",
                "prepare-recovery": "ready",
                "verify-generation": "valid",
                "apply-recovery": "applied",
            }[args.command]
            return 0 if report.get(success_key) is True else 3
        if args.command == "export":
            print(json.dumps(export_tables(args.output), ensure_ascii=False))
            return 0
        if args.command == "validate":
            validation = validate_collection(args.output)
            print(json.dumps(validation, ensure_ascii=False))
            return 0 if validation["valid"] else 3
        if args.command == "finalize":
            result = finalize_collection(args.output)
            print(json.dumps(result.to_dict(), ensure_ascii=False))
            return 0
        if args.command == "schema":
            summary = (
                apply_role_schema(args.output)
                if args.apply
                else write_schema_report(args.output)
            )
            print(
                json.dumps(
                    {
                        "post_count": summary["post_count"],
                        "post_field_count": summary["post_field_count"],
                        "role_counts": summary["role_counts"],
                        "type_counts": summary["type_counts"],
                        "schema_report": str(
                            internal_dir(args.output) / "schema_report.md"
                        ),
                        "changed_directories": len(
                            summary.get("changed_directories", [])
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        with XClient(
            browser=args.browser,
            profile=args.profile,
            cookie_db=args.cookie_db,
            proxy=args.proxy,
            mirror_base=args.mirror_base,
            delay=args.delay,
        ) as client:
            if args.command == "post-comments":
                result = client.save_post_comments(
                    args.post_id_or_url,
                    args.output,
                    max_depth=args.max_depth,
                    max_comments=args.max_comments,
                    max_pages=args.max_pages,
                    since=args.since,
                    until=args.until,
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command in ("search-query", "post-quotes", "user-reposts"):
                method, value = {
                    "search-query": ("save_search_query", getattr(args, "query", None)),
                    "post-quotes": (
                        "save_post_quotes",
                        getattr(args, "post_id_or_url", None),
                    ),
                    "user-reposts": (
                        "save_user_reposts",
                        getattr(args, "handle", None),
                    ),
                }[args.command]
                result = getattr(client, method)(
                    value,
                    args.output,
                    since=args.since,
                    until=args.until,
                    max_pages=args.max_pages,
                    limit=args.limit,
                    **({"retry_stalled": True} if args.retry_stalled else {}),
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command in ("user-followers", "user-following", "post-reposters"):
                method, value = {
                    "user-followers": (
                        "save_followers",
                        getattr(args, "user_id", None),
                    ),
                    "user-following": (
                        "save_following",
                        getattr(args, "user_id", None),
                    ),
                    "post-reposters": (
                        "save_post_reposters",
                        getattr(args, "post_id_or_url", None),
                    ),
                }[args.command]
                result = getattr(client, method)(
                    value,
                    args.output,
                    max_pages=args.max_pages,
                    limit=args.limit,
                    snapshot_id=args.snapshot_id,
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command == "profile":
                _write_or_print(client.get_user(args.handle).to_dict(), args.output)
            elif args.command == "post":
                _write_or_print(
                    client.get_post(args.post_id_or_url).to_dict(), args.output
                )
            elif args.command == "raw-post":
                _write_or_print(client.get_raw_post(args.post_id_or_url), args.output)
            elif args.command == "posts":
                result = client.save_user_posts(
                    args.handle,
                    args.output,
                    timeline=args.timeline,
                    since=args.since,
                    until=args.until,
                    max_pages=args.max_pages,
                    limit=args.limit,
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command == "activity":
                result = client.save_user_activity(
                    args.handle,
                    args.output,
                    since=args.since,
                    until=args.until,
                    max_pages=args.max_pages,
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command == "search":
                result = client.save_search_posts(
                    args.keyword,
                    args.output,
                    handle=args.user,
                    since=args.since,
                    until=args.until,
                    max_pages=args.max_pages,
                    limit=args.limit,
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command == "batch":
                result = client.save_users_activity(
                    args.handles,
                    args.output,
                    since=args.since,
                    until=args.until,
                    pages_per_round=args.pages_per_round,
                    rounds=args.rounds,
                    wait_on_rate_limit=args.wait_on_rate_limit,
                    progress=lambda item: print(
                        json.dumps(item, ensure_ascii=False), flush=True
                    ),
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
    except (PyXcomError, ValueError, OSError) as exc:
        print(f"pyxcom: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
