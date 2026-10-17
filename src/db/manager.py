"""SQLite persistence layer: catalog tables, TTL response cache, benchmarks and metrics."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from src.config import get_settings
from src.exceptions import DatabaseError, DataValidationError
from src.logger import get_logger
from src.processing.cleaner import fold_text

logger = get_logger(__name__)

MEMORY_DB = ":memory:"
WORK_SOURCES: frozenset[str] = frozenset({"api", "catalog"})
TABLES: tuple[str, ...] = (
    "works",
    "authors",
    "work_authors",
    "api_cache",
    "cache_stats",
    "benchmarks",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    work_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    first_publish_year INTEGER,
    languages TEXT NOT NULL DEFAULT '[]',
    isbns TEXT NOT NULL DEFAULT '[]',
    subjects TEXT NOT NULL DEFAULT '[]',
    edition_count INTEGER,
    source TEXT NOT NULL CHECK (source IN ('api', 'catalog')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_normalized_title ON works (normalized_title);

CREATE TABLE IF NOT EXISTS authors (
    author_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authors_normalized_name ON authors (normalized_name);

CREATE TABLE IF NOT EXISTS work_authors (
    work_key TEXT NOT NULL REFERENCES works (work_key) ON DELETE CASCADE,
    author_key TEXT NOT NULL REFERENCES authors (author_key) ON DELETE CASCADE,
    PRIMARY KEY (work_key, author_key)
);
CREATE INDEX IF NOT EXISTS idx_work_authors_author ON work_authors (author_key);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_cache_expires_at ON api_cache (expires_at);

CREATE TABLE IF NOT EXISTS cache_stats (
    stat TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO cache_stats (stat, value) VALUES ('hits', 0), ('misses', 0);

CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    pages INTEGER NOT NULL,
    sync_seconds REAL NOT NULL,
    async_seconds REAL NOT NULL,
    speedup REAL,
    works_count INTEGER NOT NULL,
    run_at TEXT NOT NULL
);
"""

UPSERT_WORK_SQL = """
INSERT INTO works (
    work_key, title, normalized_title, first_publish_year, languages, isbns, subjects,
    edition_count, source, created_at, updated_at
) VALUES (
    :work_key, :title, :normalized_title, :first_publish_year, :languages, :isbns, :subjects,
    :edition_count, :source, :now, :now
)
ON CONFLICT (work_key) DO UPDATE SET
    title = excluded.title,
    normalized_title = excluded.normalized_title,
    first_publish_year = COALESCE(excluded.first_publish_year, works.first_publish_year),
    languages = CASE WHEN excluded.languages = '[]' THEN works.languages
        ELSE excluded.languages END,
    isbns = CASE WHEN excluded.isbns = '[]' THEN works.isbns ELSE excluded.isbns END,
    subjects = CASE WHEN excluded.subjects = '[]' THEN works.subjects
        ELSE excluded.subjects END,
    edition_count = COALESCE(excluded.edition_count, works.edition_count),
    source = excluded.source,
    updated_at = excluded.updated_at
"""

UPSERT_AUTHOR_SQL = """
INSERT INTO authors (author_key, name, normalized_name, updated_at)
VALUES (:author_key, :name, :normalized_name, :now)
ON CONFLICT (author_key) DO UPDATE SET
    name = excluded.name,
    normalized_name = excluded.normalized_name,
    updated_at = excluded.updated_at
"""

LINK_WORK_AUTHOR_SQL = """
INSERT INTO work_authors (work_key, author_key) VALUES (:work_key, :author_key)
ON CONFLICT (work_key, author_key) DO NOTHING
"""

UPSERT_CACHE_SQL = """
INSERT INTO api_cache (cache_key, url, response_json, created_at, expires_at, hit_count)
VALUES (?, ?, ?, ?, ?, 0)
ON CONFLICT (cache_key) DO UPDATE SET
    url = excluded.url,
    response_json = excluded.response_json,
    created_at = excluded.created_at,
    expires_at = excluded.expires_at,
    hit_count = 0
"""

WORK_SELECT_SQL = """
SELECT
    w.work_key, w.title, w.normalized_title, w.first_publish_year, w.languages, w.isbns,
    w.subjects, w.edition_count, w.source, w.created_at, w.updated_at,
    GROUP_CONCAT(a.name, ', ') AS authors
FROM works AS w
LEFT JOIN work_authors AS wa ON wa.work_key = w.work_key
LEFT JOIN authors AS a ON a.author_key = wa.author_key
"""

WORK_ORDER_SQL = """
GROUP BY w.work_key
ORDER BY w.edition_count DESC NULLS LAST, w.title
LIMIT ?
"""

JSON_LIST_COLUMNS: tuple[str, ...] = ("languages", "isbns", "subjects")


def _escape_like(value: str) -> str:
    """Escape the LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class DatabaseManager:
    """Owns the SQLite connection and every SQL statement used by the application."""

    def __init__(
        self, db_path: str | Path | None = None, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        """Remember where the database lives; the connection is opened lazily."""
        self.db_path = str(db_path if db_path is not None else get_settings().db_path)
        self._clock = clock
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        """Open the connection, create the schema and return the manager."""
        self.initialize()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection."""
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the open connection, connecting on first use."""
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _connect(self) -> sqlite3.Connection:
        """Open the database with foreign keys enabled and WAL journaling."""
        try:
            if self.db_path != MEMORY_DB:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(f"Could not open database {self.db_path!r}: {exc}") from exc
        logger.info("Connected to SQLite database %s", self.db_path)
        return connection

    def close(self) -> None:
        """Close the connection if it is open."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block in a single transaction, rolling back and raising ``DatabaseError``."""
        connection = self.connection
        try:
            with connection:
                yield connection
        except sqlite3.Error as exc:
            logger.error("Transaction rolled back: %s", exc)
            raise DatabaseError(f"Database operation failed: {exc}") from exc

    def initialize(self) -> None:
        """Create every table and index if they do not exist yet."""
        try:
            self.connection.executescript(SCHEMA)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not initialize schema: {exc}") from exc

    def _now(self) -> str:
        """Return the current time as an ISO 8601 UTC string."""
        return self._clock().astimezone(UTC).isoformat(timespec="seconds")

    def _work_params(self, work: dict[str, Any], now: str) -> dict[str, Any]:
        """Validate a work dict and convert it into named SQL parameters."""
        work_key = str(work.get("work_key") or "").strip()
        title = str(work.get("title") or "").strip()
        if not work_key or not title:
            raise DataValidationError("A work needs a non-empty work_key and title")
        source = work.get("source", "api")
        if source not in WORK_SOURCES:
            raise DataValidationError(f"Work source must be one of {sorted(WORK_SOURCES)}")
        params: dict[str, Any] = {
            "work_key": work_key,
            "title": title,
            "normalized_title": work.get("normalized_title") or fold_text(title),
            "first_publish_year": work.get("first_publish_year"),
            "edition_count": work.get("edition_count"),
            "source": source,
            "now": now,
        }
        for column in JSON_LIST_COLUMNS:
            params[column] = json.dumps(list(work.get(column) or []), ensure_ascii=False)
        return params

    def _author_params(self, author: dict[str, Any], now: str) -> dict[str, Any]:
        """Validate an author dict and convert it into named SQL parameters."""
        author_key = str(author.get("author_key") or "").strip()
        name = str(author.get("name") or "").strip()
        if not author_key or not name:
            raise DataValidationError("An author needs a non-empty author_key and name")
        return {
            "author_key": author_key,
            "name": name,
            "normalized_name": author.get("normalized_name") or fold_text(name),
            "now": now,
        }

    def upsert_work(self, work: dict[str, Any]) -> None:
        """Insert a work or update the existing row with the same ``work_key``."""
        params = self._work_params(work, self._now())
        with self.transaction() as connection:
            connection.execute(UPSERT_WORK_SQL, params)

    def upsert_author(self, author: dict[str, Any]) -> None:
        """Insert an author or update the existing row with the same ``author_key``."""
        params = self._author_params(author, self._now())
        with self.transaction() as connection:
            connection.execute(UPSERT_AUTHOR_SQL, params)

    def link_work_author(self, work_key: str, author_key: str) -> None:
        """Link an existing work with an existing author, ignoring duplicates."""
        with self.transaction() as connection:
            connection.execute(
                LINK_WORK_AUTHOR_SQL, {"work_key": work_key, "author_key": author_key}
            )

    def upsert_works_batch(self, works: Iterable[dict[str, Any]]) -> int:
        """Upsert works, their ``authors`` and the links in one transaction; return the count."""
        now = self._now()
        work_rows: list[dict[str, Any]] = []
        author_rows: dict[str, dict[str, Any]] = {}
        link_rows: list[dict[str, str]] = []
        for work in works:
            params = self._work_params(work, now)
            work_rows.append(params)
            for author in work.get("authors") or []:
                author_params = self._author_params(author, now)
                author_rows[author_params["author_key"]] = author_params
                link_rows.append(
                    {"work_key": params["work_key"], "author_key": author_params["author_key"]}
                )
        if not work_rows:
            return 0
        with self.transaction() as connection:
            connection.executemany(UPSERT_WORK_SQL, work_rows)
            connection.executemany(UPSERT_AUTHOR_SQL, list(author_rows.values()))
            connection.executemany(LINK_WORK_AUTHOR_SQL, link_rows)
        logger.info("Batch upserted %d works and %d authors", len(work_rows), len(author_rows))
        return len(work_rows)

    def _bump_stat(self, connection: sqlite3.Connection, stat: str) -> None:
        """Increment one of the persistent cache counters."""
        connection.execute("UPDATE cache_stats SET value = value + 1 WHERE stat = ?", (stat,))

    def get_cached(self, key: str) -> Any | None:
        """Return the cached payload for ``key``, or ``None`` if missing or expired."""
        now = self._now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT response_json FROM api_cache WHERE cache_key = ? AND expires_at > ?",
                (key, now),
            ).fetchone()
            if row is None:
                self._bump_stat(connection, "misses")
                return None
            try:
                payload = json.loads(row["response_json"])
            except json.JSONDecodeError:
                logger.warning("Discarding corrupted cache entry %s", key)
                connection.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
                self._bump_stat(connection, "misses")
                return None
            connection.execute(
                "UPDATE api_cache SET hit_count = hit_count + 1 WHERE cache_key = ?", (key,)
            )
            self._bump_stat(connection, "hits")
        return payload

    def set_cache(self, key: str, url: str, data: Any, ttl_hours: int) -> None:
        """Store ``data`` under ``key`` until ``ttl_hours`` from now."""
        if ttl_hours < 0:
            raise DataValidationError("ttl_hours must not be negative")
        created = self._clock().astimezone(UTC)
        expires = created + timedelta(hours=ttl_hours)
        try:
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise DataValidationError(f"Cache payload is not JSON serializable: {exc}") from exc
        with self.transaction() as connection:
            connection.execute(
                UPSERT_CACHE_SQL,
                (
                    key,
                    url,
                    payload,
                    created.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                ),
            )

    def purge_expired(self) -> int:
        """Delete expired cache entries and return how many were removed."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM api_cache WHERE expires_at <= ?", (self._now(),)
            )
        logger.info("Purged %d expired cache entries", cursor.rowcount)
        return cursor.rowcount

    def get_cache_stats(self) -> dict[str, float | int]:
        """Return the persistent hit and miss counters and the hit ratio."""
        rows = self._fetch_all("SELECT stat, value FROM cache_stats")
        stats = {row["stat"]: row["value"] for row in rows}
        hits = int(stats.get("hits", 0))
        misses = int(stats.get("misses", 0))
        total = hits + misses
        return {"hits": hits, "misses": misses, "hit_ratio": hits / total if total else 0.0}

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a read query and return the rows as dictionaries."""
        try:
            cursor = self.connection.execute(sql, params)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Query failed: {exc}") from exc
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _decode_work(row: dict[str, Any]) -> dict[str, Any]:
        """Turn the JSON list columns of a work row back into Python lists."""
        for column in JSON_LIST_COLUMNS:
            if column in row:
                row[column] = json.loads(row[column] or "[]")
        return row

    def search_works_by_title(self, title: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return works whose title contains ``title`` (accent and case insensitive)."""
        pattern = f"%{_escape_like(fold_text(title))}%"
        rows = self._fetch_all(
            f"{WORK_SELECT_SQL} WHERE w.normalized_title LIKE ? ESCAPE '\\' {WORK_ORDER_SQL}",
            (pattern, limit),
        )
        return [self._decode_work(row) for row in rows]

    def search_works_by_author(self, author: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return works by authors whose name contains ``author``."""
        pattern = f"%{_escape_like(fold_text(author))}%"
        rows = self._fetch_all(
            f"""{WORK_SELECT_SQL}
            WHERE w.work_key IN (
                SELECT wa2.work_key FROM work_authors AS wa2
                JOIN authors AS a2 ON a2.author_key = wa2.author_key
                WHERE a2.normalized_name LIKE ? ESCAPE '\\'
            )
            {WORK_ORDER_SQL}""",
            (pattern, limit),
        )
        return [self._decode_work(row) for row in rows]

    def search_by_isbn(self, isbns: str | Sequence[str], limit: int = 20) -> list[dict[str, Any]]:
        """Return works that list any of the given ISBNs."""
        values = [isbns] if isinstance(isbns, str) else list(isbns)
        values = [value.strip() for value in values if value and value.strip()]
        if not values:
            return []
        placeholders = ", ".join("?" for _ in values)
        rows = self._fetch_all(
            f"""{WORK_SELECT_SQL}
            WHERE EXISTS (
                SELECT 1 FROM json_each(w.isbns) WHERE json_each.value IN ({placeholders})
            )
            {WORK_ORDER_SQL}""",
            (*values, limit),
        )
        return [self._decode_work(row) for row in rows]

    def record_benchmark(
        self,
        subject: str,
        pages: int,
        sync_seconds: float,
        async_seconds: float,
        works_count: int,
    ) -> int:
        """Store a sync versus async timing run and return its ID."""
        speedup = sync_seconds / async_seconds if async_seconds > 0 else None
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO benchmarks
                (subject, pages, sync_seconds, async_seconds, speedup, works_count, run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (subject, pages, sync_seconds, async_seconds, speedup, works_count, self._now()),
            )
        return int(cursor.lastrowid or 0)

    def existing_work_keys(self, work_keys: Iterable[str]) -> set[str]:
        """Return the subset of ``work_keys`` that already exist in ``works``."""
        keys = sorted({key for key in work_keys if key})
        if not keys:
            return set()
        rows = self._fetch_all(
            "SELECT work_key FROM works WHERE work_key IN (SELECT value FROM json_each(?))",
            (json.dumps(keys),),
        )
        return {row["work_key"] for row in rows}

    def count_rows(self, table: str) -> int:
        """Return the number of rows in one of the known tables."""
        if table not in TABLES:
            raise DataValidationError(f"Unknown table: {table!r}")
        rows = self._fetch_all(f"SELECT COUNT(*) AS total FROM {table}")
        return int(rows[0]["total"])

    def get_metrics(self) -> dict[str, Any]:
        """Return row counts, storage size, cache hit ratio and expired cache entries."""
        page_count = self._fetch_all("PRAGMA page_count")[0]["page_count"]
        page_size = self._fetch_all("PRAGMA page_size")[0]["page_size"]
        expired = self._fetch_all(
            "SELECT COUNT(*) AS total FROM api_cache WHERE expires_at <= ?", (self._now(),)
        )[0]["total"]
        file_size = 0
        if self.db_path != MEMORY_DB and os.path.exists(self.db_path):
            file_size = os.path.getsize(self.db_path)
        return {
            "db_path": self.db_path,
            "row_counts": {table: self.count_rows(table) for table in TABLES},
            "file_size_bytes": file_size,
            "page_size_bytes": page_count * page_size,
            "page_count": page_count,
            "cache": self.get_cache_stats(),
            "expired_cache_entries": int(expired),
        }

    def fetch_works(self) -> list[dict[str, Any]]:
        """Return every work with its JSON list columns decoded."""
        rows = self._fetch_all("SELECT * FROM works ORDER BY work_key")
        return [self._decode_work(row) for row in rows]

    def fetch_authors(self) -> list[dict[str, Any]]:
        """Return every author."""
        return self._fetch_all("SELECT * FROM authors ORDER BY author_key")

    def fetch_work_authors(self) -> list[dict[str, Any]]:
        """Return every work-author link."""
        return self._fetch_all("SELECT * FROM work_authors ORDER BY work_key, author_key")

    def fetch_benchmarks(self) -> list[dict[str, Any]]:
        """Return every benchmark run, newest first."""
        return self._fetch_all("SELECT * FROM benchmarks ORDER BY run_at DESC, id DESC")
