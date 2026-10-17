"""Tests for the synchronous and asynchronous Open Library clients."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from src.api.async_client import AsyncLibraryClient
from src.api.sync_client import (
    SEARCH_FIELDS,
    SyncLibraryClient,
    build_cache_key,
    build_search_params,
    normalize_olid,
    parse_retry_after,
)
from src.config import Settings
from src.exceptions import (
    APIError,
    DatabaseError,
    DataValidationError,
    NotFoundError,
    RateLimitError,
)

REAL_SLEEP = asyncio.sleep


class FakeCache:
    """In-memory stand-in for ``DatabaseManager``'s cache methods."""

    def __init__(self) -> None:
        """Start with an empty store."""
        self.store: dict[str, Any] = {}
        self.urls: dict[str, str] = {}

    def get_cached(self, key: str) -> Any | None:
        """Return the stored payload, if any."""
        return self.store.get(key)

    def set_cache(self, key: str, url: str, data: Any, ttl_hours: int) -> None:
        """Store the payload and remember its URL."""
        self.store[key] = data
        self.urls[key] = url


def make_response(status: int, payload: Any = None, headers: dict[str, str] | None = None):
    """Build an ``httpx.Response`` with a JSON body."""
    return httpx.Response(status, json=payload if payload is not None else {}, headers=headers)


@pytest.fixture
def settings() -> Settings:
    """Fast settings: two retries, tiny backoff and no practical throttle."""
    return Settings(
        _env_file=None,
        max_retries=2,
        backoff_factor=0.5,
        requests_per_second=1000,
        max_concurrency=3,
        user_agent="TestAgent/1.0 (contact: test@example.com)",
    )


@pytest.fixture
def sleep_mock() -> Iterator[MagicMock]:
    """Patch ``time.sleep`` so retries do not slow down the tests."""
    with patch("src.api.sync_client.time.sleep") as mocked:
        yield mocked


@pytest.fixture
def client(settings: Settings) -> Iterator[SyncLibraryClient]:
    """Yield a sync client and close it afterwards."""
    with SyncLibraryClient(settings=settings) as sync_client:
        yield sync_client


def test_search_success_sends_expected_params(client: SyncLibraryClient, sleep_mock) -> None:
    """A successful search returns the decoded JSON and sends the query parameters."""
    payload = {"numFound": 1, "docs": [{"key": "/works/OL1W", "title": "Dune"}]}
    with patch.object(client._client, "get", return_value=make_response(200, payload)) as get:
        assert client.search("dune", page=2) == payload
    get.assert_called_once_with(
        "/search.json", params={"q": "dune", "page": 2, "limit": 20, "fields": SEARCH_FIELDS}
    )
    assert client.last_source == "api"


def test_user_agent_header_is_sent(settings: Settings) -> None:
    """Every request carries the configured User-Agent."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={"key": "/works/OL1W"})

    with SyncLibraryClient(settings=settings, transport=httpx.MockTransport(handler)) as client:
        client.get_work("/works/OL1W")
    assert seen == ["TestAgent/1.0 (contact: test@example.com)"]


def test_search_isbn_uses_isbn_parameter(client: SyncLibraryClient, sleep_mock) -> None:
    """ISBN searches use the ``isbn`` query parameter."""
    with patch.object(client._client, "get", return_value=make_response(200, {})) as get:
        client.search_isbn("9780441172719")
    assert get.call_args.kwargs["params"]["isbn"] == "9780441172719"


def test_not_found_raises(client: SyncLibraryClient, sleep_mock) -> None:
    """HTTP 404 maps to ``NotFoundError`` without retrying."""
    with (
        patch.object(client._client, "get", return_value=make_response(404)) as get,
        pytest.raises(NotFoundError) as info,
    ):
        client.get_work("OL1W")
    assert info.value.status_code == 404
    assert get.call_count == 1


def test_server_error_is_retried_then_succeeds(client: SyncLibraryClient, sleep_mock) -> None:
    """A 5xx response is retried with exponential backoff."""
    responses = [make_response(503), make_response(500), make_response(200, {"ok": True})]
    with patch.object(client._client, "get", side_effect=responses) as get:
        assert client.get_subject("science_fiction") == {"ok": True}
    assert get.call_count == 3
    assert call(0.5) in sleep_mock.call_args_list
    assert call(1.0) in sleep_mock.call_args_list


def test_rate_limit_waits_for_retry_after(client: SyncLibraryClient, sleep_mock) -> None:
    """HTTP 429 waits for the ``Retry-After`` seconds before retrying."""
    responses = [make_response(429, headers={"Retry-After": "7"}), make_response(200, {"a": 1})]
    with patch.object(client._client, "get", side_effect=responses):
        assert client.search("dune") == {"a": 1}
    assert call(7.0) in sleep_mock.call_args_list


def test_rate_limit_exhausted_raises(client: SyncLibraryClient, sleep_mock) -> None:
    """Persistent 429 responses raise ``RateLimitError`` carrying ``retry_after``."""
    limited = make_response(429, headers={"Retry-After": "3"})
    with (
        patch.object(client._client, "get", return_value=limited) as get,
        pytest.raises(RateLimitError) as info,
    ):
        client.search("dune")
    assert info.value.retry_after == 3.0
    assert get.call_count == 3


def test_server_errors_exhausted_raise_api_error(client: SyncLibraryClient, sleep_mock) -> None:
    """When every attempt fails with 5xx the client raises ``APIError``."""
    with (
        patch.object(client._client, "get", return_value=make_response(502)) as get,
        pytest.raises(APIError) as info,
    ):
        client.search("dune")
    assert info.value.status_code == 502
    assert get.call_count == 3


def test_timeouts_are_retried_then_raise(client: SyncLibraryClient, sleep_mock) -> None:
    """Network timeouts are retried and finally raised as ``APIError``."""
    error = httpx.ReadTimeout("timed out")
    with (
        patch.object(client._client, "get", side_effect=error) as get,
        pytest.raises(APIError, match="Network error"),
    ):
        client.search("dune")
    assert get.call_count == 3


def test_other_client_error_raises_api_error(client: SyncLibraryClient, sleep_mock) -> None:
    """A 4xx other than 404 and 429 raises ``APIError`` immediately."""
    with (
        patch.object(client._client, "get", return_value=make_response(400)),
        pytest.raises(APIError) as info,
    ):
        client.search("dune")
    assert not isinstance(info.value, NotFoundError)
    assert info.value.status_code == 400


def test_invalid_json_raises_api_error(client: SyncLibraryClient, sleep_mock) -> None:
    """A body that is not JSON raises ``APIError``."""
    response = httpx.Response(200, content=b"<html>oops</html>")
    with (
        patch.object(client._client, "get", return_value=response),
        pytest.raises(APIError, match="Invalid JSON"),
    ):
        client.search("dune")


def test_cache_hit_skips_network(settings: Settings, sleep_mock) -> None:
    """A second identical request is served from the cache."""
    cache = FakeCache()
    with SyncLibraryClient(settings=settings, cache=cache) as client:
        with patch.object(client._client, "get", return_value=make_response(200, {"n": 1})) as get:
            client.search("dune")
            assert client.search("dune") == {"n": 1}
        assert get.call_count == 1
        assert client.last_source == "cache"
    assert any("q=dune" in url for url in cache.urls.values())


def test_refresh_bypasses_cache_but_stores_response(settings: Settings, sleep_mock) -> None:
    """``refresh=True`` always calls the network and updates the cache."""
    cache = FakeCache()
    with SyncLibraryClient(settings=settings, cache=cache) as client:
        with patch.object(client._client, "get", return_value=make_response(200, {"n": 2})) as get:
            client.get_subject("art", refresh=True)
            client.get_subject("art", refresh=True)
        assert get.call_count == 2
    assert len(cache.store) == 1


def test_cache_errors_do_not_break_requests(settings: Settings, sleep_mock) -> None:
    """Cache failures are logged and the request still reaches the API."""
    cache = MagicMock()
    cache.get_cached.side_effect = DatabaseError("locked")
    cache.set_cache.side_effect = DatabaseError("locked")
    with (
        SyncLibraryClient(settings=settings, cache=cache) as client,
        patch.object(client._client, "get", return_value=make_response(200, {"n": 3})),
    ):
        assert client.search("dune") == {"n": 3}


def test_fetch_subject_pages_collects_errors(client: SyncLibraryClient, sleep_mock) -> None:
    """A failing page is reported without stopping the remaining pages."""
    responses = [make_response(200, {"p": 1}), make_response(404), make_response(200, {"p": 3})]
    with patch.object(client._client, "get", side_effect=responses) as get:
        results = client.fetch_subject_pages("art", pages=3, limit=10)
    assert [result.page for result in results] == [1, 2, 3]
    assert [result.ok for result in results] == [True, False, True]
    assert isinstance(results[1].error, NotFoundError)
    offsets = [c.kwargs["params"]["offset"] for c in get.call_args_list]
    assert offsets == [0, 10, 20]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("OL893414W", "OL893414W"), ("/works/OL893414W", "OL893414W"), ("/authors/OL1A.json", "OL1A")],
)
def test_normalize_olid(value: str, expected: str) -> None:
    """Open Library identifiers are extracted from keys and paths."""
    assert normalize_olid(value) == expected


def test_input_validation_errors() -> None:
    """Bad identifiers and search parameters raise ``DataValidationError``."""
    with pytest.raises(DataValidationError):
        normalize_olid("../etc/passwd")
    with pytest.raises(DataValidationError):
        build_search_params("   ")
    with pytest.raises(DataValidationError):
        build_search_params("dune", by="publisher")


def test_parse_retry_after_handles_formats() -> None:
    """``Retry-After`` accepts seconds, HTTP dates and falls back on garbage."""
    assert parse_retry_after("5", default=1.0) == 5.0
    assert parse_retry_after(None, default=1.5) == 1.5
    assert parse_retry_after("soon", default=2.0) == 2.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", default=1.0) == 0.0
    assert parse_retry_after("9999", default=1.0) == 60.0


def test_cache_key_is_stable_and_order_independent() -> None:
    """The cache key ignores parameter order but changes with the values."""
    first = build_cache_key("get", "https://x/search.json", {"q": "a", "page": 1})
    second = build_cache_key("GET", "https://x/search.json", {"page": 1, "q": "a"})
    assert first == second
    assert first != build_cache_key("GET", "https://x/search.json", {"q": "b", "page": 1})


def test_api_error_string_includes_details() -> None:
    """``APIError`` renders its status code and URL."""
    assert (
        str(APIError("Boom", status_code=500, url="https://x"))
        == "Boom (status=500, url=https://x)"
    )
    assert str(APIError("Boom")) == "Boom"


async def test_async_search_success(settings: Settings) -> None:
    """The async client returns decoded JSON."""
    async with AsyncLibraryClient(settings=settings) as client:
        with patch.object(
            client._client, "get", new=AsyncMock(return_value=make_response(200, {"x": 1}))
        ):
            assert await client.search("dune") == {"x": 1}
        assert client.last_source == "api"


async def test_async_not_found(settings: Settings) -> None:
    """HTTP 404 maps to ``NotFoundError`` in the async client."""
    async with AsyncLibraryClient(settings=settings) as client:
        with (
            patch.object(client._client, "get", new=AsyncMock(return_value=make_response(404))),
            pytest.raises(NotFoundError),
        ):
            await client.get_work("OL1W")


async def test_async_retry_after_and_server_errors(settings: Settings) -> None:
    """The async client honours ``Retry-After`` and retries 5xx responses."""
    responses = [
        make_response(429, headers={"Retry-After": "4"}),
        make_response(500),
        make_response(200, {"done": True}),
    ]
    with patch("src.api.async_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        async with AsyncLibraryClient(settings=settings) as client:
            with patch.object(client._client, "get", new=AsyncMock(side_effect=responses)):
                assert await client.get_subject("art") == {"done": True}
    assert call(4.0) in sleep.await_args_list
    assert call(1.0) in sleep.await_args_list


async def test_async_retries_exhausted(settings: Settings) -> None:
    """Persistent 429 responses raise ``RateLimitError`` in the async client."""
    limited = make_response(429, headers={"Retry-After": "1"})
    with patch("src.api.async_client.asyncio.sleep", new_callable=AsyncMock):
        async with AsyncLibraryClient(settings=settings) as client:
            with (
                patch.object(client._client, "get", new=AsyncMock(return_value=limited)),
                pytest.raises(RateLimitError),
            ):
                await client.search("dune")


async def test_async_pages_run_concurrently_and_keep_order(settings: Settings) -> None:
    """Pages run concurrently up to the semaphore limit and come back in page order."""
    active = 0
    peak = 0

    async def fake_get(path: str, params: dict[str, Any]) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        page = params["offset"] // 10 + 1
        await REAL_SLEEP(0.01 * (6 - page))
        active -= 1
        if page == 4:
            return make_response(404)
        return make_response(200, {"page": page})

    async with AsyncLibraryClient(settings=settings) as client:
        with patch.object(client._client, "get", new=AsyncMock(side_effect=fake_get)):
            results = await client.fetch_subject_pages("art", pages=5, limit=10)

    assert [result.page for result in results] == [1, 2, 3, 4, 5]
    assert [result.data["page"] for result in results if result.ok] == [1, 2, 3, 5]
    assert isinstance(results[3].error, NotFoundError)
    assert 1 < peak <= settings.max_concurrency


async def test_async_cache_hit(settings: Settings) -> None:
    """The async client serves repeated requests from the cache."""
    cache = FakeCache()
    async with AsyncLibraryClient(settings=settings, cache=cache) as client:
        mocked = AsyncMock(return_value=make_response(200, {"n": 1}))
        with patch.object(client._client, "get", new=mocked):
            await client.search("dune")
            await client.search("dune")
        assert mocked.await_count == 1
        assert client.last_source == "cache"
