"""Discover public X post IDs through a configurable Nitter search mirror."""

import re
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .errors import APIError, RateLimitError

_POST_ID = re.compile(r"/status/(\d+)")


@dataclass(frozen=True)
class SearchPage:
    post_ids: list[str]
    next_url: str | None
    source_url: str


class MirrorSearch:
    """Searches only public mirror pages; X login cookies are never sent here."""

    def __init__(
        self,
        *,
        base_url: str = "https://x.noodl3.net",
        proxy: str | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("mirror base_url must be HTTPS")
        self._host = parts.netloc
        self._http = httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def initial_url(
        self,
        keyword: str,
        *,
        since: str | None = None,
        until: str | None = None,
        user: str | None = None,
    ) -> str:
        terms = [keyword.strip()]
        if not terms[0] and not user:
            raise ValueError("keyword or user is required")
        if user:
            terms.append(f"from:{user}")
        if since:
            terms.append(f"since:{since}")
        if until:
            terms.append(f"until:{until}")
        query = " ".join(part for part in terms if part)
        return f"{self.base_url}/search?{urlencode({'f': 'tweets', 'q': query})}"

    def page(self, url: str) -> SearchPage:
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != self._host:
            raise APIError("Search cursor changed mirror domain")
        response = self._http.get(url)
        if response.status_code == 429:
            raise RateLimitError("Search mirror is rate-limited")
        if response.status_code != 200:
            raise APIError(f"Search mirror returned HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if "Error" in title or "bot" in title.lower() or "captcha" in title.lower():
            raise APIError("Search mirror returned an error or verification page")
        post_ids: list[str] = []
        for item in soup.select(".timeline-item"):
            date_link = item.select_one(".tweet-date a[href]")
            if date_link is None:
                continue
            href = date_link.get("href")
            if not isinstance(href, str):
                continue
            match = _POST_ID.search(href)
            if match:
                post_ids.append(match.group(1))
        more = soup.select_one(".show-more a[href*='cursor=']")
        more_href = more.get("href") if more else None
        next_url = (
            urljoin(str(response.url), more_href)
            if isinstance(more_href, str)
            else None
        )
        if next_url and urlsplit(next_url).netloc != self._host:
            raise APIError("Search mirror returned a cross-domain cursor")
        return SearchPage(list(dict.fromkeys(post_ids)), next_url, str(response.url))
