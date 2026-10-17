"""Synchronous Open Library client with retries, rate limit handling and an optional cache."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Literal, Protocol, Self
from urllib.parse import quote

import httpx

from src.config import Settings, get_settings
from src.exceptions import (
    APIError,
    DatabaseError,
    DataValidationError,
    LibraryAppError,
    NotFoundError,
    RateLimitError,
)
from src.logger import get_logger

logger = get_logger(__name__)

SEARCH_FIELDS = (
    "key,title,author_name,author_key,first_publish_year,language,isbn,edition_count,subject"
)
SEARCH_TYPES: tuple[str, ...] = ("q", "title", "author", "isbn")
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_PAGE_LIMIT = 50
MAX_RETRY_AFTER_SECONDS = 60.0
OPEN_LIBRARY_ID_PATTERN = re.compile(r"OL\d+[A-Z]")

ResponseSource = Literal["cache", "api"]


class ResponseCache(Protocol):
    """Minimal interface the clients need from a response cache such as ``DatabaseManager``."""

    def get_cached(self, key: str) -> Any | None:
        """Return the cached payload or ``None`` when missing or expired."""
        ...

    def set_cache(self, key: str, url: str, data: Any, ttl_hours: int) -> None:
        """Store a payload under ``key`` for ``ttl_hours`` hours."""
        ...


@dataclass(frozen=True)
class PageResult:
    """Outcome of fetching one page of a subject: either data or the error that occurred."""

    page: int
    data: dict[str, Any] | None = None
    error: LibraryAppError | None = None

    @property
    def ok(self) -> bool:
        """Return ``True`` when the page was fetched successfully."""
        return self.error is None


PageProgress = Callable[[PageResult], None]


def build_cache_key(method: str, url: str, params: dict[str, Any] | None = None) -> str:
    """Return a stable SHA-256 hash of the HTTP method, URL and sorted query parameters."""
    canonical = json.dumps(
        {
            "method": method.upper(),
            "url": url,
            "params": sorted((str(key), str(value)) for key, value in (params or {}).items()),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_retry_after(value: str | None, default: float) -> float:
    """Convert a ``Retry-After`` header (seconds or HTTP date) into a bounded number of seconds."""
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = (moment - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def normalize_olid(identifier: str) -> str:
    """Extract an Open Library ID such as ``OL893414W`` from a raw ID, key or URL path."""
    candidate = identifier.strip().rstrip("/").removesuffix(".json").rsplit("/", 1)[-1]
    if not OPEN_LIBRARY_ID_PATTERN.fullmatch(candidate):
        raise DataValidationError(f"Invalid Open Library identifier: {identifier!r}")
    return candidate


def build_search_params(
    query: str, page: int = 1, by: str = "q", limit: int = DEFAULT_SEARCH_LIMIT
) -> dict[str, Any]:
    """Build the query parameters for ``/search.json`` and validate them."""
    if by not in SEARCH_TYPES:
        raise DataValidationError(f"Search type must be one of {SEARCH_TYPES}, got {by!r}")
    if not query or not query.strip():
        raise DataValidationError("Search query must not be empty")
    if page < 1 or limit < 1:
        raise DataValidationError("Page and limit must be positive integers")
    return {by: query.strip(), "page": page, "limit": limit, "fields": SEARCH_FIELDS}


def subject_path(subject: str) -> str:
    """Return the API path for a subject slug."""
    slug = subject.strip().lower()
    if not slug:
        raise DataValidationError("Subject must not be empty")
    return f"/subjects/{quote(slug)}.json"


def validate_pagination(limit: int, offset: int) -> None:
    """Ensure ``limit`` is positive and ``offset`` is not negative."""
    if limit < 1 or offset < 0:
        raise DataValidationError("Limit must be positive and offset must not be negative")


@dataclass(frozen=True)
class RetryPolicy:
    """Decides whether a failed request should be retried and how long to wait."""

    max_retries: int
    backoff_factor: float

    @property
    def max_attempts(self) -> int:
        """Return the total number of attempts, including the first one."""
        return self.max_retries + 1

    def backoff_delay(self, attempt: int) -> float:
        """Return the exponential backoff delay for the given attempt number."""
        return self.backoff_factor * (2 ** (attempt - 1))

    def delay_for_transport_error(self, exc: httpx.TransportError, url: str, attempt: int) -> float:
        """Return the wait before retrying a network failure, or raise when retries ran out."""
        if attempt >= self.max_attempts:
            raise APIError(
                f"Network error after {attempt} attempt(s): {exc.__class__.__name__}", url=url
            ) from exc
        return self.backoff_delay(attempt)

    def delay_for_response(self, response: httpx.Response, url: str, attempt: int) -> float | None:
        """Return the wait before retrying, ``None`` on success, or raise for fatal responses."""
        status = response.status_code
        if status == 429:
            retry_after = parse_retry_after(
                response.headers.get("Retry-After"), self.backoff_delay(attempt)
            )
            if attempt >= self.max_attempts:
                raise RateLimitError(
                    f"Rate limit still exceeded after {attempt} attempt(s)",
                    url=url,
                    retry_after=retry_after,
                )
            return retry_after
        if status >= 500:
            if attempt >= self.max_attempts:
                raise APIError(
                    f"Server error after {attempt} attempt(s)", status_code=status, url=url
                )
            return self.backoff_delay(attempt)
        if status == 404:
            raise NotFoundError("Resource not found", status_code=status, url=url)
        if status >= 400:
            raise APIError("Request rejected by the API", status_code=status, url=url)
        return None

    @staticmethod
    def decode_json(response: httpx.Response, url: str) -> Any:
        """Parse the JSON body or raise ``APIError`` when it is invalid."""
        try:
            return response.json()
        except ValueError as exc:
            raise APIError(
                "Invalid JSON in response", status_code=response.status_code, url=url
            ) from exc


class SyncLibraryClient:
    """Blocking client for the Open Library API built on ``httpx.Client``."""

    def __init__(
        self,
        settings: Settings | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create the HTTP client using the configured base URL, timeout and User-Agent."""
        self.settings = settings or get_settings()
        self.cache = cache
        self.retry_policy = RetryPolicy(self.settings.max_retries, self.settings.backoff_factor)
        self.last_source: ResponseSource | None = None
        self._min_interval = 1.0 / self.settings.requests_per_second
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            base_url=self.settings.api_base_url,
            timeout=self.settings.request_timeout,
            headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> Self:
        """Return the client for use in a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP client."""
        self.close()

    def close(self) -> None:
        """Release the connection pool."""
        self._client.close()

    def search(
        self, query: str, page: int = 1, by: str = "q", limit: int = DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        """Search works by free text (``q``), ``title``, ``author`` or ``isbn``."""
        return self._request("/search.json", build_search_params(query, page, by, limit))

    def search_isbn(self, isbn: str) -> dict[str, Any]:
        """Search the work that owns an edition with the given ISBN."""
        return self.search(isbn, by="isbn", limit=5)

    def get_work(self, work_id: str) -> dict[str, Any]:
        """Return the work record for an ID such as ``OL893414W``."""
        return self._request(f"/works/{normalize_olid(work_id)}.json")

    def get_author_works(
        self, author_id: str, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        """Return the works written by an author such as ``OL79034A``."""
        validate_pagination(limit, offset)
        return self._request(
            f"/authors/{normalize_olid(author_id)}/works.json",
            {"limit": limit, "offset": offset},
        )

    def get_subject(
        self,
        subject: str,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Return one page of works for a subject slug such as ``science_fiction``."""
        validate_pagination(limit, offset)
        return self._request(
            subject_path(subject), {"limit": limit, "offset": offset}, refresh=refresh
        )

    def fetch_subject_pages(
        self,
        subject: str,
        pages: int,
        limit: int = DEFAULT_PAGE_LIMIT,
        refresh: bool = False,
        progress: PageProgress | None = None,
    ) -> list[PageResult]:
        """Fetch ``pages`` consecutive subject pages one after another, collecting errors."""
        if pages < 1:
            raise DataValidationError("Pages must be a positive integer")
        results: list[PageResult] = []
        for page in range(1, pages + 1):
            try:
                data = self.get_subject(
                    subject, limit=limit, offset=(page - 1) * limit, refresh=refresh
                )
                result = PageResult(page=page, data=data)
            except LibraryAppError as exc:
                logger.error("Page %d of subject %r failed: %s", page, subject, exc)
                result = PageResult(page=page, error=exc)
            results.append(result)
            if progress is not None:
                progress(result)
        return results

    def _throttle(self) -> None:
        """Sleep just enough to keep the request rate under ``requests_per_second``."""
        if self._last_request_at is not None:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _read_cache(self, key: str, url: str) -> Any | None:
        """Return a cached payload, treating cache failures as misses."""
        if self.cache is None:
            return None
        try:
            cached = self.cache.get_cached(key)
        except DatabaseError as exc:
            logger.warning("Cache read failed for %s: %s", url, exc)
            return None
        if cached is not None:
            logger.info("Cache hit for %s", url)
        return cached

    def _write_cache(self, key: str, url: str, data: Any) -> None:
        """Store a payload in the cache, logging instead of failing on cache errors."""
        if self.cache is None:
            return
        try:
            self.cache.set_cache(key, url, data, self.settings.cache_ttl_hours)
        except DatabaseError as exc:
            logger.warning("Cache write failed for %s: %s", url, exc)

    def _request(
        self, path: str, params: dict[str, Any] | None = None, refresh: bool = False
    ) -> Any:
        """Perform a GET request with caching, throttling, retries and error mapping."""
        url = f"{self.settings.api_base_url}{path}"
        cache_key = build_cache_key("GET", url, params)
        if not refresh:
            cached = self._read_cache(cache_key, url)
            if cached is not None:
                self.last_source = "cache"
                return cached

        max_attempts = self.retry_policy.max_attempts
        for attempt in range(1, max_attempts + 1):
            self._throttle()
            logger.info("GET %s params=%s (attempt %d/%d)", url, params, attempt, max_attempts)
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                delay = self.retry_policy.delay_for_transport_error(exc, url, attempt)
                logger.warning("%s on %s, retrying in %.2fs", type(exc).__name__, url, delay)
                time.sleep(delay)
                continue

            delay = self.retry_policy.delay_for_response(response, url, attempt)
            if delay is not None:
                logger.warning("HTTP %d on %s, retrying in %.2fs", response.status_code, url, delay)
                time.sleep(delay)
                continue

            data = self.retry_policy.decode_json(response, url)
            self._write_cache(cache_key, str(httpx.URL(url, params=params or {})), data)
            self.last_source = "api"
            return data

        raise APIError("Request failed without a response", url=url)
