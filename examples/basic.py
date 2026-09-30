"""Sign in to X in Chrome first, then set optional environment variables."""

import os

from pyxcom import XClient


with XClient(
    profile=os.environ.get("PYXCOM_PROFILE", "Default"),
    proxy=os.environ.get("PYXCOM_PROXY"),
) as client:
    profile = client.get_user("thsottiaux")
    print(profile.name, profile.followers_count)
    result = client.save_search_posts(
        "Codex",
        "output/example-search",
        handle="thsottiaux",
        since="2026-09-01",
        until="2026-09-30",
        max_pages=1,
    )
    print(result.to_dict())
