"""Typed failures surfaced by pyXcom."""


class PyXcomError(Exception):
    """Base package exception."""


class AuthenticationError(PyXcomError):
    """No usable browser login was found."""


class CaptureError(PyXcomError):
    """The browser could not capture the requested X read endpoint."""


class APIError(PyXcomError):
    """X returned an unusable response."""


class RateLimitError(APIError):
    """X limited the requested endpoint."""

    def __init__(self, message: str, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class ParseError(PyXcomError):
    """The response shape changed or omitted expected data."""


class IntegrityError(PyXcomError, ValueError):
    """Saved sources or a recovery generation cannot be safely accepted."""
