"""Bounded research collection recipe; importing this module makes no requests.

Requires this local 0.7 development checkout and a manually logged-in browser.
New endpoints have offline fixture coverage; live results must be checked.
"""

import argparse
from pathlib import Path

from pyxcom import XClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "handle", help="Account whose repost activity/network to observe"
    )
    parser.add_argument("post_id", help="Announcement post or reply ID")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile", default="Default")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument(
        "--query", default='("usage reset" OR "rate limit reset") Codex'
    )
    args = parser.parse_args()
    with XClient(profile=args.profile) as client:
        user_id = client.get_user(args.handle).id
        results = [
            client.save_search_query(
                args.query,
                args.output_dir / "search",
                since=args.since,
                until=args.until,
                max_pages=2,
                limit=100,
            ),
            client.save_post_quotes(
                args.post_id,
                args.output_dir / "quotes",
                since=args.since,
                until=args.until,
                max_pages=2,
                limit=100,
            ),
            client.save_user_reposts(
                args.handle,
                args.output_dir / "repost_activity",
                since=args.since,
                until=args.until,
                max_pages=2,
                limit=100,
            ),
            client.save_post_comments(
                args.post_id,
                args.output_dir / "comments",
                since=args.since,
                until=args.until,
                max_depth=2,
                max_comments=100,
                max_pages=2,
            ),
            client.save_followers(
                user_id, args.output_dir / "network", max_pages=2, limit=100
            ),
            client.save_following(
                user_id, args.output_dir / "network", max_pages=2, limit=100
            ),
            client.save_post_reposters(
                args.post_id, args.output_dir / "reposters", max_pages=2, limit=100
            ),
        ]
        for result in results:
            print(result.to_dict())


if __name__ == "__main__":
    main()
