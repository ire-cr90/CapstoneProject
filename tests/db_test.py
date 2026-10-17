"""Tests for the SQLite ``DatabaseManager``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.db.manager import TABLES, DatabaseManager
from src.exceptions import DatabaseError, DataValidationError


class FakeClock:
    """Controllable clock used to test TTL expiry."""

    def __init__(self) -> None:
        """Start at a fixed moment."""
        self.current = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.current

    def advance(self, **delta: float) -> None:
        """Move the clock forward."""
        self.current += timedelta(**delta)


def make_work(key: str = "OL1W", **overrides: Any) -> dict[str, Any]:
    """Build a work dict with sensible defaults."""
    work: dict[str, Any] = {
        "work_key": key,
        "title": "Cien años de soledad",
        "first_publish_year": 1967,
        "languages": ["spa"],
        "isbns": ["9780307474728", "0307474720"],
        "subjects": ["magic realism"],
        "edition_count": 120,
        "source": "api",
        "authors": [{"author_key": "OL1A", "name": "Gabriel García Márquez"}],
    }
    work.update(overrides)
    return work


@pytest.fixture
def clock() -> FakeClock:
    """Provide a fresh fake clock."""
    return FakeClock()


@pytest.fixture
def db(tmp_path: Path, clock: FakeClock) -> Iterator[DatabaseManager]:
    """Yield an initialized database stored in a temporary folder."""
    with DatabaseManager(tmp_path / "nested" / "library.db", clock=clock) as manager:
        yield manager


def test_initialize_creates_all_tables(db: DatabaseManager) -> None:
    """Every expected table exists after initialization."""
    rows = db.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {row["name"] for row in rows}
    assert set(TABLES) <= names


def test_pragmas_are_enabled(db: DatabaseManager) -> None:
    """Foreign keys and WAL journaling are switched on."""
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_initialize_is_idempotent(db: DatabaseManager) -> None:
    """Running ``initialize`` twice keeps data and counters intact."""
    db.upsert_work(make_work())
    db.initialize()
    assert db.count_rows("works") == 1
    assert db.count_rows("cache_stats") == 2


def test_upsert_work_inserts_then_updates(db: DatabaseManager, clock: FakeClock) -> None:
    """A second upsert updates fields but keeps ``created_at`` and non-empty lists."""
    db.upsert_work(make_work())
    clock.advance(hours=1)
    db.upsert_work(
        make_work(title="One Hundred Years of Solitude", languages=[], edition_count=None)
    )
    works = db.fetch_works()
    assert len(works) == 1
    work = works[0]
    assert work["title"] == "One Hundred Years of Solitude"
    assert work["normalized_title"] == "one hundred years of solitude"
    assert work["languages"] == ["spa"]
    assert work["edition_count"] == 120
    assert work["created_at"] < work["updated_at"]


def test_upsert_author_and_link(db: DatabaseManager) -> None:
    """Authors are upserted and linked to works without duplicate links."""
    db.upsert_work(make_work(authors=None))
    db.upsert_author({"author_key": "OL1A", "name": "Gabriel Garcia"})
    db.upsert_author({"author_key": "OL1A", "name": "Gabriel García Márquez"})
    db.link_work_author("OL1W", "OL1A")
    db.link_work_author("OL1W", "OL1A")
    authors = db.fetch_authors()
    assert authors[0]["name"] == "Gabriel García Márquez"
    assert authors[0]["normalized_name"] == "gabriel garcia marquez"
    assert db.count_rows("work_authors") == 1


def test_link_requires_existing_rows(db: DatabaseManager) -> None:
    """Foreign keys reject links to unknown works or authors."""
    with pytest.raises(DatabaseError):
        db.link_work_author("OL404W", "OL404A")


def test_invalid_work_is_rejected(db: DatabaseManager) -> None:
    """Works without a key or with an unknown source are rejected."""
    with pytest.raises(DataValidationError):
        db.upsert_work(make_work(key=""))
    with pytest.raises(DataValidationError):
        db.upsert_work(make_work(source="scraped"))


def test_batch_upsert_inserts_works_authors_and_links(db: DatabaseManager) -> None:
    """A batch writes works, deduplicated authors and links together."""
    works = [
        make_work("OL1W"),
        make_work("OL2W", title="El amor en los tiempos del cólera"),
        make_work("OL3W", title="Dune", authors=[{"author_key": "OL2A", "name": "Frank Herbert"}]),
    ]
    assert db.upsert_works_batch(works) == 3
    assert db.count_rows("works") == 3
    assert db.count_rows("authors") == 2
    assert db.count_rows("work_authors") == 3
    assert db.upsert_works_batch([]) == 0


def test_batch_rolls_back_on_failure(db: DatabaseManager) -> None:
    """A database error in the batch rolls back every row of that batch."""
    db.connection.execute(
        "CREATE TRIGGER fail_on_bad BEFORE INSERT ON works WHEN NEW.title = 'BAD' "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(DatabaseError):
        db.upsert_works_batch([make_work("OL1W"), make_work("OL2W", title="BAD")])
    assert db.count_rows("works") == 0
    assert db.count_rows("authors") == 0


def test_cache_roundtrip_and_counters(db: DatabaseManager) -> None:
    """Cache hits and misses are counted and persisted."""
    assert db.get_cached("missing") is None
    db.set_cache("k1", "https://openlibrary.org/search.json?q=dune", {"docs": ["á"]}, 24)
    assert db.get_cached("k1") == {"docs": ["á"]}
    assert db.get_cached("k1") == {"docs": ["á"]}
    stats = db.get_cache_stats()
    assert stats == {"hits": 2, "misses": 1, "hit_ratio": pytest.approx(2 / 3)}
    hit_count = db.connection.execute("SELECT hit_count FROM api_cache").fetchone()[0]
    assert hit_count == 2


def test_cache_counters_survive_restart(tmp_path: Path) -> None:
    """Counters are stored in SQLite and survive reopening the database."""
    path = tmp_path / "library.db"
    with DatabaseManager(path) as first:
        first.get_cached("nothing")
    with DatabaseManager(path) as second:
        assert second.get_cache_stats()["misses"] == 1


def test_cache_ttl_expiry_and_purge(db: DatabaseManager, clock: FakeClock) -> None:
    """Expired entries are treated as misses and removed by ``purge_expired``."""
    db.set_cache("short", "u1", {"a": 1}, ttl_hours=1)
    db.set_cache("long", "u2", {"b": 2}, ttl_hours=48)
    clock.advance(hours=2)
    assert db.get_cached("short") is None
    assert db.get_cached("long") == {"b": 2}
    assert db.get_metrics()["expired_cache_entries"] == 1
    assert db.purge_expired() == 1
    assert db.purge_expired() == 0
    assert db.count_rows("api_cache") == 1


def test_set_cache_overwrites_entry(db: DatabaseManager) -> None:
    """Setting an existing key replaces the payload and resets the hit count."""
    db.set_cache("k", "u", {"v": 1}, 1)
    db.get_cached("k")
    db.set_cache("k", "u", {"v": 2}, 1)
    assert db.connection.execute("SELECT hit_count FROM api_cache").fetchone()[0] == 0
    assert db.get_cached("k") == {"v": 2}


def test_corrupted_cache_entry_is_discarded(db: DatabaseManager) -> None:
    """A cache row with invalid JSON counts as a miss and is deleted."""
    db.set_cache("k", "u", {"v": 1}, 1)
    with db.connection:
        db.connection.execute("UPDATE api_cache SET response_json = '{broken'")
    assert db.get_cached("k") is None
    assert db.count_rows("api_cache") == 0


def test_cache_rejects_bad_input(db: DatabaseManager) -> None:
    """Negative TTLs and non-serializable payloads are rejected."""
    with pytest.raises(DataValidationError):
        db.set_cache("k", "u", {"v": 1}, -1)
    with pytest.raises(DataValidationError):
        db.set_cache("k", "u", {"v": object()}, 1)


def test_search_by_title_author_and_isbn(db: DatabaseManager) -> None:
    """Searches are accent-insensitive, case-insensitive and parameterized."""
    db.upsert_works_batch(
        [
            make_work("OL1W"),
            make_work(
                "OL2W",
                title="Dune",
                isbns=["9780441172719"],
                authors=[{"author_key": "OL2A", "name": "Frank Herbert"}],
            ),
        ]
    )
    by_title = db.search_works_by_title("  CIEN anos ")
    assert [row["work_key"] for row in by_title] == ["OL1W"]
    assert by_title[0]["authors"] == "Gabriel García Márquez"
    assert by_title[0]["languages"] == ["spa"]
    assert [row["work_key"] for row in db.search_works_by_author("garcia marquez")] == ["OL1W"]
    assert [row["work_key"] for row in db.search_by_isbn("9780441172719")] == ["OL2W"]
    assert [row["work_key"] for row in db.search_by_isbn(["0000000000", "0307474720"])] == ["OL1W"]
    assert db.search_by_isbn([]) == []


def test_search_treats_wildcards_and_quotes_literally(db: DatabaseManager) -> None:
    """LIKE wildcards and SQL quotes in user input never match everything or break SQL."""
    db.upsert_work(make_work())
    assert db.search_works_by_title("%") == []
    assert db.search_works_by_title("_") == []
    assert db.search_works_by_title("'; DROP TABLE works; --") == []
    assert db.count_rows("works") == 1


def test_record_benchmark(db: DatabaseManager) -> None:
    """Benchmarks store the computed speedup."""
    run_id = db.record_benchmark("science_fiction", 3, 6.0, 2.0, 150)
    assert run_id == 1
    benchmark = db.fetch_benchmarks()[0]
    assert benchmark["speedup"] == pytest.approx(3.0)
    assert benchmark["works_count"] == 150
    db.record_benchmark("art", 1, 1.0, 0.0, 0)
    assert db.fetch_benchmarks()[0]["speedup"] is None


def test_get_metrics(db: DatabaseManager) -> None:
    """Metrics report counts, sizes and cache information."""
    db.upsert_works_batch([make_work()])
    metrics = db.get_metrics()
    assert metrics["row_counts"]["works"] == 1
    assert metrics["row_counts"]["authors"] == 1
    assert metrics["file_size_bytes"] > 0
    assert metrics["page_size_bytes"] > 0
    assert metrics["cache"]["hit_ratio"] == 0.0


def test_count_rows_rejects_unknown_table(db: DatabaseManager) -> None:
    """Only whitelisted table names are accepted."""
    with pytest.raises(DataValidationError):
        db.count_rows("sqlite_master; DROP TABLE works")


def test_in_memory_database() -> None:
    """The manager also works with an in-memory database."""
    with DatabaseManager(":memory:") as manager:
        manager.upsert_work(make_work())
        assert manager.get_metrics()["file_size_bytes"] == 0
