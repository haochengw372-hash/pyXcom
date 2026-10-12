"""Typed failures surfaced by pyXcom."""


class PyXcomError(Exception):
    """Base package exception."""


class AuthenticationError(PyXcomError):
    """No usable browser login was found."""


class CaptureError(PyXcomError):
    """The browser could not capture the requested X read endpoint."""


class APIError(PyXcomError):
    """X returned an unusable response with optional credential-free metadata."""

    def __init__(
        self, message: str, *, status_code: int | None = None,
        operation: str | None = None, error_codes: tuple[int, ...] = (),
        reset_at: int | None = None, remaining: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation
        self.error_codes = error_codes
        self.reset_at = reset_at
        self.remaining = remaining


class RateLimitError(APIError):
    """X limited the requested endpoint."""

    def __init__(
        self, message: str, reset_at: int | None = None, *,
        status_code: int | None = None, operation: str | None = None,
        error_codes: tuple[int, ...] = (), remaining: int | None = None,
    ) -> None:
        super().__init__(
            message, status_code=status_code, operation=operation,
            error_codes=error_codes, reset_at=reset_at, remaining=remaining,
        )


class ParseError(PyXcomError):
    """The response shape changed or omitted expected data."""


class IntegrityError(PyXcomError, ValueError):
    """Saved sources or a recovery generation cannot be safely accepted."""
