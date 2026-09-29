"""Cookie-authenticated, read-only requests to X's current web endpoints."""

import json
import re
from datetime import datetime, timezone
import httpx

from .errors import APIError, RateLimitError
from .models import Post, Profile
from .parse import parse_post, parse_profile, single_post

_MAIN_SCRIPT = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web/main\.[^\"\s<>]+\.js"
)
_OPERATION = re.compile(r'queryId:"([^\"]+)",operationName:"([^\"]+)"')
_BEARER = re.compile(r"Bearer (A[A-Za-z0-9%]+)")
TWEET_FEATURES = {
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class XTransport:
    """Discover query IDs from X's web bundle instead of pinning twikit routes."""

    def __init__(
        self, cookies: dict[str, str], *, proxy: str | None = None, timeout: float = 30
    ) -> None:
        cookie_jar = httpx.Cookies()
        for name, value in cookies.items():
            cookie_jar.set(name, value, domain=".x.com", path="/")
        self._csrf = cookies["ct0"]
        self._x = httpx.Client(
            proxy=proxy, cookies=cookie_jar, timeout=timeout, follow_redirects=True
        )
        self._assets = httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True)
        self._operations: dict[str, str] = {}
        self._bearer: str | None = None

    def close(self) -> None:
        self._x.close()
        self._assets.close()

    def discover(self, *, refresh: bool = False) -> None:
        if self._operations and not refresh:
            return
        response = self._x.get("https://x.com/")
        if response.status_code != 200:
            raise APIError(f"X homepage returned HTTP {response.status_code}")
        match = _MAIN_SCRIPT.search(response.text)
        if not match:
            raise APIError(
                "X web bundle was not found; the site layout may have changed"
            )
        js_response = self._assets.get(match.group(0))
        if js_response.status_code != 200:
            raise APIError(f"X web bundle returned HTTP {js_response.status_code}")
        operations = {
            name: query_id for query_id, name in _OPERATION.findall(js_response.text)
        }
        bearer = _BEARER.search(js_response.text)
        if not operations or not bearer:
            raise APIError("X web bundle no longer exposes the expected read routes")
        self._operations = operations
        self._bearer = bearer.group(1)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._bearer}",
            "x-csrf-token": self._csrf,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "referer": "https://x.com/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        }

    def graphql(
        self,
        operation: str,
        variables: dict,
        *,
        features: dict | None = None,
        field_toggles: dict | None = None,
    ) -> dict:
        self.discover()
        if operation not in self._operations:
            raise APIError(f"X web bundle has no {operation} operation")
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features or {}, separators=(",", ":")),
        }
        if field_toggles is not None:
            params["fieldToggles"] = json.dumps(field_toggles, separators=(",", ":"))
        query_id = self._operations[operation]
        url = f"https://x.com/i/api/graphql/{query_id}/{operation}"
        response = self._x.get(url, params=params, headers=self._headers())
        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            raise RateLimitError(
                f"X rate-limited {operation}",
                int(reset) if reset and reset.isdigit() else None,
            )
        if response.status_code == 404 and not response.content:
            # X also uses an empty 404 when a read route is temporarily limited.
            reset = response.headers.get("x-rate-limit-reset")
            raise RateLimitError(
                f"X returned an empty 404 for {operation}; retry after the rate window",
                int(reset) if reset and reset.isdigit() else None,
            )
        if response.status_code != 200:
            raise APIError(f"X returned HTTP {response.status_code} for {operation}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError(f"X returned non-JSON data for {operation}") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise APIError(f"X returned an error for {operation}")
        return payload

    def get_profile(self, handle: str) -> Profile:
        payload = self.graphql(
            "UserByScreenName",
            {"screen_name": handle, "withSafetyModeUserFields": True},
        )
        return parse_profile(payload, captured_at_utc=now_utc())

    def get_post(self, post_id: str) -> Post:
        payload = self.graphql(
            "TweetResultByRestId",
            {
                "tweetId": post_id,
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            },
            features=TWEET_FEATURES,
            field_toggles={},
        )
        return single_post(payload, captured_at_utc=now_utc())

    def get_posts(self, post_ids: list[str]) -> list[Post]:
        if len(post_ids) > 50:
            raise ValueError("get_posts accepts at most 50 IDs per request")
        if not post_ids:
            return []
        payload = self.graphql(
            "TweetResultsByRestIds",
            {
                "tweetIds": post_ids,
                "includePromotedContent": True,
                "withBirdwatchNotes": True,
                "withVoice": True,
                "withCommunity": True,
            },
            features=TWEET_FEATURES,
            field_toggles={},
        )
        found: list[Post] = []
        for item in payload.get("data", {}).get("tweetResult", []):
            result = item.get("result")
            if isinstance(result, dict):
                post = parse_post(result, captured_at_utc=now_utc())
                if post is not None:
                    found.append(post)
        return found

    def user_timeline_page(
        self, user_id: str, *, timeline: str, cursor: str | None = None
    ) -> dict:
        if timeline not in {"posts", "replies"}:
            raise ValueError("timeline must be posts or replies")
        operation = (
            "UserOriginalsTimeline" if timeline == "posts" else "UserRepliesTimeline"
        )
        variables = {
            "userId": user_id,
            "count": 20,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor
        return self.graphql(operation, variables, features=TWEET_FEATURES)
