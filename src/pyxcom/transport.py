"""Cookie-authenticated, read-only requests to X's current web endpoints."""

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any, Callable
import httpx

from .errors import APIError, AuthenticationError, RateLimitError
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
        self._request_gate: Callable[[str], None] | None = None
        self._request_observer: Callable[[dict], None] | None = None
        self.last_error: dict | None = None
        self._comment_requests = None

    def close(self) -> None:
        self._x.close()
        self._assets.close()

    def _failure(self, error: APIError) -> APIError:
        self.last_error = {
            "operation": error.operation, "status_code": error.status_code,
            "error_codes": list(error.error_codes), "reset_at": error.reset_at,
            "remaining": error.remaining, "error_type": type(error).__name__,
        }
        return error

    @staticmethod
    def _rate_value(response: httpx.Response, name: str) -> int | None:
        value = response.headers.get(name, "")
        return int(value) if value.isascii() and value.isdigit() else None

    @staticmethod
    def _operation_name(url: str) -> str:
        """Use fixed labels or operation names, never request parameters."""
        path = urlsplit(url).path
        if path.startswith("/i/api/graphql/"):
            name = path.rsplit("/", 1)[-1]
            return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) else "GraphQL"
        if path == "/1.1/account/settings.json":
            return "AccountSettings"
        return "Homepage" if path in {"", "/"} else "WebAsset"

    def _request(
        self, client: httpx.Client, method: str, url: str, **kwargs
    ) -> httpx.Response:
        """Gate and measure every attempt, including retries and discovery calls."""
        operation = self._operation_name(url)
        for attempt in range(1, 4):
            if self._request_gate is not None:
                self._request_gate(operation)
            started = time.monotonic()
            response = None
            error_type = None
            try:
                response = client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    error_type = "HTTPError"
            except httpx.RequestError as exc:
                error_type = type(exc).__name__
                if attempt == 3:
                    raise self._failure(APIError(
                        "Network request failed after 3 attempts", operation=operation
                    )) from exc
            finally:
                if self._request_observer is not None:
                    self._request_observer({
                        "operation": operation,
                        "status_code": response.status_code if response is not None else None,
                        "attempt": attempt,
                        "elapsed_seconds": time.monotonic() - started,
                        "response_bytes": len(response.content) if response is not None else 0,
                        "reset_at": self._rate_value(response, "x-rate-limit-reset") if response is not None else None,
                        "remaining": self._rate_value(response, "x-rate-limit-remaining") if response is not None else None,
                        "error_type": error_type,
                    })
            if response is not None and (
                response.status_code not in {502, 503, 504} or attempt == 3
            ):
                return response
            time.sleep(2 ** (attempt - 1))
        raise self._failure(APIError("Network retry loop ended unexpectedly", operation=operation))

    def _payload(self, response: httpx.Response, operation: str) -> dict:
        reset = self._rate_value(response, "x-rate-limit-reset")
        remaining = self._rate_value(response, "x-rate-limit-remaining")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        codes = tuple(
            item["code"] for item in errors
            if isinstance(item, dict) and type(item.get("code")) is int
        ) if isinstance(errors, list) else ()
        error_class: type[APIError] = APIError
        message = None
        if response.status_code == 429 or (
            response.status_code == 404 and not response.content and remaining == 0
        ) or 88 in codes:
            error_class = RateLimitError
            message = f"X rate-limited {operation}"
        elif response.status_code != 200:
            message = f"X returned HTTP {response.status_code} for {operation}"
        elif not isinstance(payload, dict) or errors:
            message = f"X returned an unusable response for {operation}"
        if message is not None:
            raise self._failure(error_class(
                message, status_code=response.status_code, operation=operation,
                error_codes=codes, reset_at=reset, remaining=remaining,
            ))
        return payload

    def _get(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        return self._request(client, "GET", url, **kwargs)

    def _post_read(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        return self._request(client, "POST", url, **kwargs)

    def discover(self, *, refresh: bool = False) -> None:
        if self._operations and not refresh:
            return
        response = self._get(self._x, "https://x.com/")
        if response.status_code != 200:
            self._payload(response, "Homepage")
        match = _MAIN_SCRIPT.search(response.text)
        if not match:
            raise APIError(
                "X web bundle was not found; the site layout may have changed"
            )
        js_response = self._get(self._assets, match.group(0))
        if js_response.status_code != 200:
            self._payload(js_response, "WebAsset")
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
        self.last_error = None
        self.discover()
        if operation not in self._operations:
            self._discover_lazy_operation(operation)
        if operation not in self._operations:
            raise self._failure(APIError(f"X web bundle has no {operation} operation", operation=operation))
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
        return self._payload(response, operation)

    def get_login_identity(self) -> str:
        """Resolve the authenticated current user; never use a collection target."""
        self.last_error = None
        self.discover()
        if "Viewer" in self._operations:
            try:
                payload = self.graphql(
                    "Viewer", {"withCommunitiesMemberships": True},
                    features={
                        "responsive_web_graphql_exclude_directive_enabled": True,
                        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                        "responsive_web_graphql_timeline_navigation_enabled": True,
                        "verified_phone_label_enabled": False,
                        "rweb_tipjar_consumption_enabled": True,
                        "creator_subscriptions_tweet_preview_api_enabled": True,
                    },
                    field_toggles={"isDelegate": False, "withAuxiliaryUserLabels": False},
                )
            except APIError as exc:
                # A missing read route may use the legacy current-user endpoint.
                # Authentication failures and quota exhaustion must stop here.
                if exc.status_code != 404 or exc.error_codes or isinstance(exc, RateLimitError):
                    raise
            else:
                result: Any = payload
                for key in ("data", "viewer", "user_results", "result"):
                    result = result.get(key) if isinstance(result, dict) else None
                observer = result.get("rest_id") if isinstance(result, dict) else None
                if (
                    not isinstance(result, dict) or result.get("__typename") != "User"
                    or not isinstance(observer, str) or not observer.isascii()
                    or not observer.isdigit()
                ):
                    raise AuthenticationError("Authenticated Viewer identity unavailable")
                return observer
        self.last_error = None
        response = self._get(
            self._x, "https://api.x.com/1.1/account/settings.json",
            headers=self._headers(),
        )
        settings = self._payload(response, "AccountSettings")
        handle = settings.get("screen_name")
        if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise AuthenticationError("Authenticated account identity unavailable")
        profile = self.get_profile(handle)
        if not profile.id.isascii() or not profile.id.isdigit() or profile.handle.lower() != handle.lower():
            raise AuthenticationError("Authenticated account identity could not be verified")
        return profile.id

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
