"""Asynchronous Open Library client with bounded concurrency, retries and rate limit handling."""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx

from src.api.sync_client import (
    DEFAULT_PAGE_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    PageProgress,
    PageResult,
    ResponseCache,
    ResponseSource,
    RetryPolicy,
    build_cache_key,
    build_search_params,
    normalize_olid,
    subject_path,
    validate_pagination,
)
from src.config import Settings, get_settings
from src.exceptions import APIError, DatabaseError, DataValidationError, LibraryAppError
from src.logger import get_logger

logger = get_logger(__name__)


class AsyncLibraryClient:
    """Non-blocking client for the Open Library API built on ``httpx.AsyncClient``."""

    def __init__(
        self,
        settings: Settings | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the HTTP client, the concurrency semaphore and the throttle lock."""
        self.settings = settings or get_settings()
        self.cache = cache
        self.retry_policy = RetryPolicy(self.settings.max_retries, self.settings.backoff_factor)
        self.last_source: ResponseSource | None = None
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._throttle_lock = asyncio.Lock()
        self._min_interval = 1.0 / self.settings.requests_per_second
        self._last_request_at: float | None = None
        self._client = httpx.AsyncClient(
            base_url=self.settings.api_base_url,
            timeout=self.settings.request_timeout,
            headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        """Return the client for use in an ``async with`` block."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP client."""
        await self.close()

    async def close(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()

    async def search(
        self, query: str, page: int = 1, by: str = "q", limit: int = DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        """Search works by free text (``q``), ``title``, ``author`` or ``isbn``."""
        return await self._request("/search.json", build_search_params(query, page, by, limit))

    async def search_isbn(self, isbn: str) -> dict[str, Any]:
        """Search the work that owns an edition with the given ISBN."""
        return await self.search(isbn, by="isbn", limit=5)

    async def get_work(self, work_id: str) -> dict[str, Any]:
        """Return the work record for an ID such as ``OL893414W``."""
        return await self._request(f"/works/{normalize_olid(work_id)}.json")

    async def get_author_works(
        self, author_id: str, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        """Return the works written by an author such as ``OL79034A``."""
        validate_pagination(limit, offset)
        return await self._request(
            f"/authors/{normalize_olid(author_id)}/works.json",
            {"limit": limit, "offset": offset},
        )

    async def get_subject(
        self,
        subject: str,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Return one page of works for a subject slug such as ``science_fiction``."""
        validate_pagination(limit, offset)
        return await self._request(
            subject_path(subject), {"limit": limit, "offset": offset}, refresh=refresh
        )

    async def fetch_subject_pages(
        self,
        subject: str,
        pages: int,
        limit: int = DEFAULT_PAGE_LIMIT,
        refresh: bool = False,
        progress: PageProgress | None = None,
    ) -> list[PageResult]:
        """Fetch all pages concurrently with ``asyncio.gather``, preserving page order."""
        if pages < 1:
            raise DataValidationError("Pages must be a positive integer")
        return list(
            await asyncio.gather(
                *(
                    self._fetch_page(subject, page, limit, refresh, progress)
                    for page in range(1, pages + 1)
                )
            )
        )

    async def _fetch_page(
        self,
        subject: str,
        page: int,
        limit: int,
        refresh: bool,
        progress: PageProgress | None,
    ) -> PageResult:
        """Fetch one subject page and turn any failure into a ``PageResult`` error."""
        try:
            data = await self.get_subject(
                subject, limit=limit, offset=(page - 1) * limit, refresh=refresh
            )
            result = PageResult(page=page, data=data)
        except LibraryAppError as exc:
            logger.error("Page %d of subject %r failed: %s", page, subject, exc)
            result = PageResult(page=page, error=exc)
        except Exception as exc:
            logger.exception("Unexpected error on page %d of subject %r", page, subject)
            result = PageResult(page=page, error=APIError(str(exc)))
        if progress is not None:
            progress(result)
        return result

    async def _throttle(self) -> None:
        """Space out request start times so the rate stays under ``requests_per_second``."""
        async with self._throttle_lock:
            loop = asyncio.get_running_loop()
            if self._last_request_at is not None:
                wait = self._min_interval - (loop.time() - self._last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = loop.time()

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

    async def _request(
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
        async with self._semaphore:
            for attempt in range(1, max_attempts + 1):
                await self._throttle()
                logger.info("GET %s params=%s (attempt %d/%d)", url, params, attempt, max_attempts)
                try:
                    response = await self._client.get(path, params=params)
                except httpx.TransportError as exc:
                    delay = self.retry_policy.delay_for_transport_error(exc, url, attempt)
                    logger.warning("%s on %s, retrying in %.2fs", type(exc).__name__, url, delay)
                    await asyncio.sleep(delay)
                    continue

                delay = self.retry_policy.delay_for_response(response, url, attempt)
                if delay is not None:
                    logger.warning(
                        "HTTP %d on %s, retrying in %.2fs", response.status_code, url, delay
                    )
                    await asyncio.sleep(delay)
                    continue

                data = self.retry_policy.decode_json(response, url)
                self._write_cache(cache_key, str(httpx.URL(url, params=params or {})), data)
                self.last_source = "api"
                return data

        raise APIError("Request failed without a response", url=url)
