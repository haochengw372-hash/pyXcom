"""Portable public-data records returned by pyXcom."""

from dataclasses import asdict, dataclass, field
from pathlib import Path


def classify_post(
    in_reply_to_id: str | None,
    quoted_post_id: str | None,
    reposted_post_id: str | None,
) -> tuple[str, str]:
    """Return (role, type) from X's explicit post relationships."""
    if reposted_post_id:
        return "repost", "repost"
    if in_reply_to_id:
        return "comment", "reply"
    if quoted_post_id:
        return "main", "quote"
    return "main", "original"


@dataclass(frozen=True)
class Profile:
    id: str
    handle: str
    name: str
    description: str | None = None
    created_at_utc: str | None = None
    followers_count: int | None = None
    following_count: int | None = None
    posts_count: int | None = None
    media_count: int | None = None
    verified: bool | None = None
    url: str | None = None
    captured_at_utc: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Post:
    id: str
    author_id: str
    author_handle: str
    created_at_utc: str
    text: str
    url: str
    post_role: str = ""
    post_type: str = ""
    language: str | None = None
    conversation_id: str | None = None
    in_reply_to_id: str | None = None
    quoted_post_id: str | None = None
    reposted_post_id: str | None = None
    reply_count: int | None = None
    repost_count: int | None = None
    like_count: int | None = None
    view_count: int | None = None
    quote_count: int | None = None
    bookmark_count: int | None = None
    media_urls: list[str] = field(default_factory=list)
    outbound_urls: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    discovery_source: str | None = None
    discovery_url: str | None = None
    captured_at_utc: str | None = None

    def __post_init__(self) -> None:
        role, kind = classify_post(
            self.in_reply_to_id, self.quoted_post_id, self.reposted_post_id
        )
        object.__setattr__(self, "post_role", role)
        object.__setattr__(self, "post_type", kind)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CollectionResult:
    output_dir: Path
    post_count: int
    pages_fetched: int
    complete: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "output_dir": str(self.output_dir),
            "post_count": self.post_count,
            "pages_fetched": self.pages_fetched,
            "complete": self.complete,
            "reason": self.reason,
        }
