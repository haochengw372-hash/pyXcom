"""Command-line entry point for pyXcom."""

import argparse
import json
import sys
from pathlib import Path

from .client import XClient
from .errors import PyXcomError
from .schema import apply_role_schema, write_schema_report
from .validate import finalize_collection, validate_collection


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    common.add_argument("--profile", help="Browser profile folder, e.g. 'Profile 3'")
    common.add_argument(
        "--cookie-db", type=Path, help="Explicit Chrome/Edge Cookies DB path"
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
    common = _common_parser()
    profile = commands.add_parser(
        "profile", parents=[common], help="Get public user profile"
    )
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
        "posts", parents=[common], help="Save a user's timeline"
    )
    posts.add_argument("handle")
    posts.add_argument("--timeline", choices=("posts", "replies"), default="posts")
    posts.add_argument("--since", help="Inclusive UTC date YYYY-MM-DD")
    posts.add_argument("--until", help="Exclusive UTC date YYYY-MM-DD")
    posts.add_argument("--max-pages", type=int)
    posts.add_argument("--limit", type=int)
    posts.add_argument("--output", type=Path, required=True)
    activity = commands.add_parser(
        "activity", parents=[common], help="Save posts and replies"
    )
    activity.add_argument("handle")
    activity.add_argument("--since")
    activity.add_argument("--until")
    activity.add_argument("--max-pages", type=int)
    activity.add_argument("--output", type=Path, required=True)
    search = commands.add_parser(
        "search", parents=[common], help="Search keyword, dates, and user"
    )
    search.add_argument(
        "keyword", help="Keyword(s); quote a phrase using X search syntax"
    )
    search.add_argument("--user", help="Optional author handle")
    search.add_argument("--since", help="Inclusive UTC date YYYY-MM-DD")
    search.add_argument("--until", help="Exclusive UTC date YYYY-MM-DD")
    search.add_argument("--max-pages", type=int)
    search.add_argument("--limit", type=int)
    search.add_argument("--output", type=Path, required=True)
    batch = commands.add_parser(
        "batch",
        parents=[common],
        help="Collect several accounts and write a combined report",
    )
    batch.add_argument("--handles", nargs="+", required=True)
    batch.add_argument("--since", required=True)
    batch.add_argument("--until", required=True)
    batch.add_argument("--pages-per-round", type=int, default=5)
    batch.add_argument(
        "--rounds", type=int, default=1, help="0 keeps resuming until complete"
    )
    batch.add_argument("--wait-on-rate-limit", action="store_true")
    batch.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser(
        "validate", help="Check saved rows, scope, and SHA-256 hashes"
    )
    validate.add_argument("--output", type=Path, required=True)
    finalize = commands.add_parser(
        "finalize", help="Rebuild CSV and report after an interrupted run"
    )
    finalize.add_argument("--output", type=Path, required=True)
    schema = commands.add_parser(
        "schema", help="Report field coverage and optionally add main/comment columns"
    )
    schema.add_argument("--output", type=Path, required=True)
    schema.add_argument(
        "--apply", action="store_true", help="Backfill role/type in saved CSV and JSONL"
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
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
                        "schema_report": str(args.output / "schema_report.md"),
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
            if args.command == "profile":
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
                result = client.save_search(
                    args.keyword,
                    args.output,
                    user=args.user,
                    since=args.since,
                    until=args.until,
                    max_pages=args.max_pages,
                    limit=args.limit,
                )
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif args.command == "batch":
                result = client.save_accounts(
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
    except (PyXcomError, ValueError) as exc:
        print(f"pyxcom: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
