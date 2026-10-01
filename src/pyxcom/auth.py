"""Load only X authentication cookies from an existing local browser profile."""

import errno
import os
import stat
import sys
from pathlib import Path

import browser_cookie3

from .errors import AuthenticationError

_SUPPORTED_BROWSERS = {"chrome", "edge", "safari"}
_SAFARI_COOKIE_FILES = (
    "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
    "~/Library/Cookies/Cookies.binarycookies",
)
_SAFARI_PERMISSION_MESSAGE = (
    "macOS denied access to Safari cookies. In System Settings > Privacy & Security > "
    "Full Disk Access, authorize the app running pyXcom (for example your terminal or "
    "Codex), then retry."
)


def _safari_cookie_db() -> Path:
    """Resolve the upstream Safari locations without probing beyond denied access."""
    for filename in _SAFARI_COOKIE_FILES:
        path = Path(filename).expanduser()
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM} or isinstance(
                exc, PermissionError
            ):
                raise AuthenticationError(_SAFARI_PERMISSION_MESSAGE) from None
            raise AuthenticationError(
                "Could not inspect the Safari cookie file"
            ) from None
        if stat.S_ISREG(mode):
            return path
    raise AuthenticationError(
        "Safari cookie file not found in the locations supported by browser-cookie3. "
        "Sign in to x.com in Safari manually; if its cookie storage is elsewhere, "
        "pass cookie_db pointing to an accessible Cookies.binarycookies file."
    )


def profile_cookie_db(browser: str, profile: str) -> Path:
    """Resolve a Chrome/Edge profile cookie DB on supported desktop systems."""
    if browser == "safari":
        raise AuthenticationError(
            "Safari profile selection is not supported; omit profile and use its "
            "default cookie file, or pass cookie_db explicitly."
        )
    if browser not in {"chrome", "edge"}:
        raise AuthenticationError("Supported browsers are chrome, edge and safari")
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
    if browser not in _SUPPORTED_BROWSERS:
        raise AuthenticationError("Supported browsers are chrome, edge and safari")
    path = Path(cookie_db).expanduser() if cookie_db else None
    if browser == "safari":
        if sys.platform != "darwin":
            raise AuthenticationError("Safari login reading is supported only on macOS")
        if profile:
            raise AuthenticationError(
                "Safari profile selection is not supported; omit profile and use its "
                "default cookie file, or pass cookie_db explicitly."
            )
        if path is None:
            path = _safari_cookie_db()
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            raise AuthenticationError(
                "Safari cookie file not found at the selected path"
            ) from None
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM} or isinstance(
                exc, PermissionError
            ):
                raise AuthenticationError(_SAFARI_PERMISSION_MESSAGE) from None
            raise AuthenticationError(
                "Could not inspect the selected Safari cookie file"
            ) from None
        if not stat.S_ISREG(mode):
            raise AuthenticationError(
                "Safari cookie_file must be a regular Cookies.binarycookies file"
            )
    else:
        if path is None and profile:
            path = profile_cookie_db(browser, profile)
        if path is not None and not path.exists():
            raise AuthenticationError(
                "Browser cookie database not found at the selected path"
            )
    try:
        reader = getattr(browser_cookie3, browser)
        kwargs = {"domain_name": ".x.com"}
        if path is not None:
            kwargs["cookie_file"] = str(path)
        jar = reader(**kwargs)
    except Exception as exc:
        if browser == "safari":
            if isinstance(exc, OSError) and (
                isinstance(exc, PermissionError)
                or exc.errno in {errno.EACCES, errno.EPERM}
            ):
                raise AuthenticationError(_SAFARI_PERMISSION_MESSAGE) from None
            raise AuthenticationError(
                "Could not parse the selected Safari cookie file with browser-cookie3; "
                "its format or selected path may be unsupported."
            ) from None
        raise AuthenticationError(
            "Could not read the selected browser profile"
        ) from exc
    cookies = {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.name in {"auth_token", "ct0"} and cookie.domain in {"x.com", ".x.com"}
    }
    if set(cookies) != {"auth_token", "ct0"}:
        raise AuthenticationError(
            "No active X login in this browser profile; sign in to x.com or select another profile"
        )
    return cookies
