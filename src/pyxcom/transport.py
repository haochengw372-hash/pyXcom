"""Cookie-authenticated, read-only requests to X's current web endpoints."""

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx

from .errors import APIError, RateLimitError
from .models import Post, Profile
from .parse import parse_post, parse_profile, single_post
from .assets import lazy_operation_assets

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
        self._homepage_html = ""
        self._main_asset_url = ""
        self._lazy_attempted: set[str] = set()

    def close(self) -> None:
        self._x.close()
        self._assets.close()

    def _request(
        self, client: httpx.Client, method: str, url: str, **kwargs
    ) -> httpx.Response:
        """Retry transient transport/5xx errors, then surface a safe message."""
        for attempt in range(3):
            try:
                response = client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise APIError(
                        f"Network request to {urlsplit(url).netloc} failed after 3 attempts"
                    ) from exc
            else:
                if response.status_code not in {502, 503, 504} or attempt == 2:
                    return response
            time.sleep(2**attempt)
        raise APIError("Network retry loop ended unexpectedly")

    def _get(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        return self._request(client, "GET", url, **kwargs)

    def _post_read(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        return self._request(client, "POST", url, **kwargs)

    def discover(self, *, refresh: bool = False) -> None:
        if self._operations and not refresh:
            return
        response = self._get(self._x, "https://x.com/")
        if response.status_code != 200:
            raise APIError(f"X homepage returned HTTP {response.status_code}")
        match = _MAIN_SCRIPT.search(response.text)
        if not match:
            raise APIError(
                "X web bundle was not found; the site layout may have changed"
            )
        js_response = self._get(self._assets, match.group(0))
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
        self._homepage_html = response.text
        self._main_asset_url = match.group(0)
        self._lazy_attempted.clear()

    def _discover_lazy_operation(self, operation: str) -> None:
        if operation in self._lazy_attempted:
            return
        self._lazy_attempted.add(operation)
        for url in lazy_operation_assets(
            self._homepage_html, self._main_asset_url, operation
        ):
            response = self._get(self._assets, url)
            if response.status_code != 200:
                continue
            self._operations.update(
                {name: query_id for query_id, name in _OPERATION.findall(response.text)}
            )
            if operation in self._operations:
                return

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
            self._discover_lazy_operation(operation)
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
        if operation in {"SearchTimeline", "Followers", "Retweeters"}:
            # Current web read queries use POST; no mutation operation is sent here.
            body = {
                "queryId": query_id,
                "variables": variables,
                "features": features or {},
                "fieldToggles": field_toggles or {},
            }
            if field_toggles is not None:
                body["fieldToggles"] = field_toggles
            response = self._post_read(self._x, url, json=body, headers=self._headers())
        else:
            response = self._get(self._x, url, params=params, headers=self._headers())
        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            raise RateLimitError(
                f"X rate-limited {operation}",
                int(reset) if reset and reset.isdigit() else None,
            )
        if response.status_code == 404 and not response.content:
            # A 404 is not evidence of an exhausted quota when requests remain.
            reset = response.headers.get("x-rate-limit-reset")
            remaining = response.headers.get("x-rate-limit-remaining")
            if remaining == "0":
                raise RateLimitError(
                    f"X quota exhausted for {operation}",
                    int(reset) if reset and reset.isdigit() else None,
                )
            raise APIError(
                f"X returned an empty 404 for {operation}; the route or read request may be incompatible"
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

    def conversation_page(self, post_id: str, cursor: str | None = None) -> dict:
        """Read a post's conversation, including replies by other authors.

        A focal post may itself be a reply; callers can expand such branches to
        discover nested comments that the root conversation omits.
        """
        variables = {
            "focalTweetId": post_id,
            "with_rux_injections": False,
            "includePromotedContent": False,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor
        return self.graphql(
            "TweetDetail",
            variables,
            features=TWEET_FEATURES,
            field_toggles={"withArticleRichContentState": False},
        )

    def search_page(
        self,
        query: str,
        cursor: str | None = None,
        *,
        product: str = "Latest",
        count: int = 20,
    ) -> dict:
        """Read native X search; terms/operators are interpreted by X, not locally."""
        if not query.strip() or product not in {"Latest", "Top"}:
            raise ValueError("A nonempty query and Latest/Top product are required")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 100
        ):
            raise ValueError("count must be an integer from 1 to 100")
        variables = {
            "rawQuery": query,
            "count": count,
            "querySource": "typed_query",
            "product": product,
        }
        if cursor:
            variables["cursor"] = cursor
        return self.graphql("SearchTimeline", variables, features=TWEET_FEATURES)

    def relationship_page(
        self, user_id: str, *, kind: str, cursor: str | None = None
    ) -> dict:
        """Followers and following are distinct directed-list read operations."""
        if kind not in {"followers", "following"} or not str(user_id).isdigit():
            raise ValueError("kind must be followers/following and user_id numeric")
        variables = {
            "userId": str(user_id),
            "count": 20,
            "includePromotedContent": False,
        }
        if cursor:
            variables["cursor"] = cursor
        return self.graphql(
            "Followers" if kind == "followers" else "Following",
            variables,
            features=TWEET_FEATURES,
        )

    def reposters_page(self, post_id: str, cursor: str | None = None) -> dict:
        """Return the visible reposter-user list, not timestamped repost activities."""
        if not str(post_id).isdigit():
            raise ValueError("post_id must be numeric")
        variables = {
            "tweetId": str(post_id),
            "count": 20,
            "includePromotedContent": True,
            "enableRanking": False,
        }
        if cursor:
            variables["cursor"] = cursor
        return self.graphql("Retweeters", variables, features=TWEET_FEATURES)

    def user_timeline_page(
        self, user_id: str, *, timeline: str, cursor: str | None = None
    ) -> dict:
        if timeline not in {"posts", "replies", "reposts"}:
            raise ValueError("timeline must be posts, replies, or reposts")
        operation = {
            "posts": "UserOriginalsTimeline",
            "replies": "UserRepliesTimeline",
            "reposts": "UserTweets",
        }[timeline]
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
