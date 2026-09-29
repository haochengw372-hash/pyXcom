"""Load only X authentication cookies from an existing local browser profile."""

import os
import sys
from pathlib import Path

import browser_cookie3

from .errors import AuthenticationError


def profile_cookie_db(browser: str, profile: str) -> Path:
    """Resolve a Chrome/Edge profile cookie DB on supported desktop systems."""
    if browser not in {"chrome", "edge"}:
        raise AuthenticationError("Supported browsers are chrome and edge")
    if sys.platform == "darwin":
        folder = "Google/Chrome" if browser == "chrome" else "Microsoft Edge"
        return (
            Path.home() / "Library/Application Support" / folder / profile / "Cookies"
        )
    if sys.platform.startswith("linux"):
        folder = "google-chrome" if browser == "chrome" else "microsoft-edge"
        return Path.home() / ".config" / folder / profile / "Cookies"
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        folder = "Google/Chrome" if browser == "chrome" else "Microsoft/Edge"
        return local / folder / "User Data" / profile / "Network" / "Cookies"
    raise AuthenticationError("Unsupported operating system; pass cookie_db explicitly")


def load_x_cookies(
    browser: str = "chrome",
    *,
    profile: str | None = None,
    cookie_db: str | Path | None = None,
) -> dict[str, str]:
    """Return auth_token and ct0 in memory; never save or log their values."""
    if browser not in {"chrome", "edge"}:
        raise AuthenticationError("Supported browsers are chrome and edge")
    path = Path(cookie_db).expanduser() if cookie_db else None
    if path is None and profile:
        path = profile_cookie_db(browser, profile)
    if path is not None and not path.exists():
        raise AuthenticationError(
            "Browser cookie database not found at the selected path"
        )
    try:
        reader = browser_cookie3.chrome if browser == "chrome" else browser_cookie3.edge
        kwargs = {"domain_name": ".x.com"}
        if path is not None:
            kwargs["cookie_file"] = str(path)
        jar = reader(**kwargs)
    except Exception as exc:
        raise AuthenticationError(
            "Could not read the selected browser profile"
        ) from exc
    cookies = {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.name in {"auth_token", "ct0"} and cookie.domain.lstrip(".") == "x.com"
    }
    if set(cookies) != {"auth_token", "ct0"}:
        raise AuthenticationError(
            "No active X login in this browser profile; sign in to x.com or select another profile"
        )
    return cookies
