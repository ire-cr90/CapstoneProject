"""Custom exception hierarchy for the application."""

from __future__ import annotations


class LibraryAppError(Exception):
    """Base class for every error raised by the application."""


class APIError(LibraryAppError):
    """Raised when the Open Library API returns an error or an unusable response."""

    def __init__(
        self, message: str, status_code: int | None = None, url: str | None = None
    ) -> None:
        """Store the HTTP status code and URL that produced the error."""
        super().__init__(message)
        self.status_code = status_code
        self.url = url

    def __str__(self) -> str:
        """Return the message enriched with the status code and URL when available."""
        details = []
        if self.status_code is not None:
            details.append(f"status={self.status_code}")
        if self.url:
            details.append(f"url={self.url}")
        message = super().__str__()
        return f"{message} ({', '.join(details)})" if details else message


class RateLimitError(APIError):
    """Raised when the API keeps answering HTTP 429 after all retries."""

    def __init__(
        self,
        message: str,
        status_code: int | None = 429,
        url: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the last ``Retry-After`` value suggested by the server."""
        super().__init__(message, status_code=status_code, url=url)
        self.retry_after = retry_after


class NotFoundError(APIError):
    """Raised when the requested resource does not exist (HTTP 404)."""


class DataValidationError(LibraryAppError):
    """Raised for invalid user input, bad ISBNs or malformed file rows."""


class DatabaseError(LibraryAppError):
    """Raised when a SQLite operation fails."""


class ConfigurationError(LibraryAppError):
    """Raised when the configuration is invalid or cannot be updated."""


class ReportGenerationError(LibraryAppError):
    """Raised when a report file cannot be generated."""
