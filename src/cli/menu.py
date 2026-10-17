"""Loop-driven interactive menu that ties the clients, database, cleaner and reports together."""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TextIO

from dotenv import set_key
from pydantic import ValidationError

from src.api.async_client import AsyncLibraryClient
from src.api.sync_client import PageResult, SyncLibraryClient
from src.config import Settings, get_settings, reload_settings
from src.db.manager import DatabaseManager
from src.exceptions import (
    APIError,
    ConfigurationError,
    DatabaseError,
    DataValidationError,
    LibraryAppError,
    NotFoundError,
    RateLimitError,
    ReportGenerationError,
)
from src.logger import get_logger, set_level
from src.processing.analytics import DataProcessor
from src.processing.cleaner import (
    SUPPORTED_EXTENSIONS,
    CatalogStats,
    catalog_record_to_work,
    clean_isbn,
    clean_user_input,
    deduplicate,
    detect_query_type,
    is_valid_isbn,
    isbn_variants,
    stream_catalog,
    to_subject_slug,
    work_from_search_doc,
    work_from_subject_work,
)
from src.reporting.generator import ReportGenerator

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LINE_WIDTH = 70
LINE = "=" * LINE_WIDTH
TITLE = "OPEN LIBRARY INTELLIGENCE & CATALOG AUTOMATION SYSTEM"
MAIN_MENU: tuple[tuple[str, str], ...] = (
    ("1", "Ingest Works by Subject"),
    ("2", "Search Books by Author/Title/ISBN"),
    ("3", "Process & Deduplicate Local Catalog File"),
    ("4", "Generate Analytical Reports"),
    ("5", "System Health, Cache Maintenance & Database Administration"),
    ("6", "Run Quality Checks & Test Suite Diagnostic"),
    ("0", "Exit"),
)
SEARCH_MENU: tuple[tuple[str, str], ...] = (
    ("1", "Search by author"),
    ("2", "Search by title"),
    ("3", "Search by ISBN"),
    ("4", "Auto-detect query type"),
    ("0", "Back"),
)
ADMIN_MENU: tuple[tuple[str, str], ...] = (
    ("1", "Show database metrics"),
    ("2", "Purge expired cache entries"),
    ("3", "Show current configuration"),
    ("4", "Change a .env value"),
    ("5", "Back"),
)
MIN_PAGES = 1
MAX_PAGES = 10
DEFAULT_PAGES = 3
SUBJECT_PAGE_LIMIT = 50
SEARCH_RESULT_LIMIT = 10
CATALOG_BATCH_SIZE = 200
DEFAULT_CATALOG = "data/sample_catalog.csv"
MAX_ATTEMPTS = 3
MAX_CELL_WIDTH = 40
COMMAND_TIMEOUT_SECONDS = 900
EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
ERROR_LABELS: tuple[tuple[type[LibraryAppError], str], ...] = (
    (NotFoundError, "Not found on Open Library"),
    (RateLimitError, "Open Library rate limit reached"),
    (APIError, "Open Library API problem"),
    (DataValidationError, "Invalid input"),
    (DatabaseError, "Database problem"),
    (ConfigurationError, "Configuration problem"),
    (ReportGenerationError, "Report generation failed"),
    (LibraryAppError, "Application error"),
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one quality check command."""

    name: str
    returncode: int
    output: str

    @property
    def passed(self) -> bool:
        """Return ``True`` when the command exited with code 0."""
        return self.returncode == 0


def friendly_error(exc: LibraryAppError) -> str:
    """Return a user-facing message for any custom exception."""
    label = next(label for error_type, label in ERROR_LABELS if isinstance(exc, error_type))
    message = f"{label}: {exc}"
    if isinstance(exc, RateLimitError) and exc.retry_after:
        message += f" Try again in about {exc.retry_after:.0f} seconds."
    return message


def mask_email(text: str) -> str:
    """Hide most of any email address, keeping the first character and the domain."""
    return EMAIL_PATTERN.sub(lambda match: f"{match.group(1)}***{match.group(2)}", text)


def _shorten(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` characters with an ellipsis."""
    return text if len(text) <= width else f"{text[: width - 3]}..."


def format_table(
    headers: Sequence[str], rows: Iterable[Sequence[Any]], max_width: int = MAX_CELL_WIDTH
) -> str:
    """Render rows as an aligned text table; numbers are right-aligned."""
    raw_rows = [list(row) for row in rows]
    cells = [
        [_shorten("" if value is None else str(value), max_width) for value in row]
        for row in raw_rows
    ]
    widths = [
        max([len(header), *(len(row[index]) for row in cells)])
        for index, header in enumerate(headers)
    ]
    lines = [
        " | ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)),
        "-+-".join("-" * width for width in widths),
    ]
    for raw, row in zip(raw_rows, cells, strict=True):
        lines.append(
            " | ".join(
                cell.rjust(width)
                if isinstance(value, int | float) and not isinstance(value, bool)
                else cell.ljust(width)
                for value, cell, width in zip(raw, row, widths, strict=True)
            )
        )
    return "\n".join(line.rstrip() for line in lines)


def human_size(size: float) -> str:
    """Format a byte count with binary units."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


class ProgressBar:
    """Minimal text progress bar that redraws itself on one line."""

    def __init__(self, total: int, label: str, stream: TextIO, width: int = 30) -> None:
        """Prepare a bar for ``total`` steps."""
        self.total = max(total, 1)
        self.label = label
        self.stream = stream
        self.width = width
        self.current = 0
        self._lock = threading.Lock()

    def advance(self, step: int = 1, note: str = "") -> None:
        """Move the bar forward and redraw it."""
        with self._lock:
            self.current = min(self.current + step, self.total)
            filled = int(self.width * self.current / self.total)
            bar = "#" * filled + "-" * (self.width - filled)
            self.stream.write(f"\r{self.label} [{bar}] {self.current}/{self.total} {note}".rstrip())
            self.stream.flush()

    def finish(self) -> None:
        """End the progress line."""
        self.stream.write("\n")
        self.stream.flush()


class Spinner:
    """Context manager that shows a spinner on a background thread while work runs."""

    FRAMES = "|/-\\"

    def __init__(self, label: str, stream: TextIO, interval: float = 0.1) -> None:
        """Prepare the spinner without starting it."""
        self.label = label
        self.stream = stream
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def __enter__(self) -> Self:
        """Start spinning."""
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop spinning and print whether the work finished or failed."""
        self._stop.set()
        self._thread.join()
        status = "done" if exc_type is None else "failed"
        self.stream.write(f"\r{self.label}... {status}\n")
        self.stream.flush()

    def _spin(self) -> None:
        """Redraw the next frame until stopped."""
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                return
            self.stream.write(f"\r{frame} {self.label}...")
            self.stream.flush()
            self._stop.wait(self.interval)


class CLIApp:
    """Interactive menu for the Open Library Intelligence & Catalog Automation System."""

    def __init__(
        self,
        settings: Settings | None = None,
        input_func: Callable[[str], str] = input,
        stream: TextIO | None = None,
        env_file: str | Path = ".env",
        runner: CommandRunner = subprocess.run,
    ) -> None:
        """Wire the dependencies; the database is opened on first use."""
        self.settings = settings or get_settings()
        self.stream = stream or sys.stdout
        self.env_file = Path(env_file)
        self._input = input_func
        self._runner = runner
        self._db: DatabaseManager | None = None
        self._running = False
        self._actions: dict[str, Callable[[], None]] = {
            "1": self.ingest_by_subject,
            "2": self.search_books,
            "3": self.process_catalog,
            "4": self.generate_reports,
            "5": self.admin_menu,
            "6": self.run_quality_checks,
            "0": self.exit,
        }

    @property
    def db(self) -> DatabaseManager:
        """Return the open database, reopening it if ``DB_PATH`` changed."""
        if self._db is None or self._db.db_path != str(self.settings.db_path):
            self.close()
            database = DatabaseManager(self.settings.db_path)
            database.initialize()
            self._db = database
        return self._db

    def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def say(self, text: str = "") -> None:
        """Print a line to the output stream."""
        print(text, file=self.stream, flush=True)

    def section(self, title: str) -> None:
        """Print a section heading."""
        self.say(f"\n--- {title} ---")

    def show_menu(self) -> None:
        """Print the main menu."""
        self.say(LINE)
        self.say(TITLE.center(LINE_WIDTH).rstrip())
        self.say(LINE)
        for key, label in MAIN_MENU:
            self.say(f"[{key}] {label}")
        self.say(LINE)

    def run(self) -> None:
        """Show the menu in a loop until the user exits."""
        if hasattr(self.stream, "reconfigure"):
            self.stream.reconfigure(errors="replace")
        self._running = True
        logger.info("CLI started")
        try:
            while self._running:
                self.show_menu()
                try:
                    choice = self._input("Select an option: ").strip()
                except (KeyboardInterrupt, EOFError):
                    self.say("\nGoodbye!")
                    break
                self.dispatch(choice)
        finally:
            self.close()
            logger.info("CLI stopped")

    def dispatch(self, choice: str) -> None:
        """Run the action for a menu choice, keeping the loop alive on any error."""
        action = self._actions.get(choice)
        if action is None:
            self.say(f"'{choice}' is not a valid option. Please choose a number from 0 to 6.")
            return
        self._guarded(action)

    def _guarded(self, action: Callable[[], None]) -> None:
        """Run an action and turn interruptions and errors into friendly messages."""
        try:
            action()
        except KeyboardInterrupt:
            self.say("\nOperation cancelled. Returning to the menu.")
        except LibraryAppError as exc:
            logger.error("%s failed: %s", getattr(action, "__name__", "action"), exc)
            self.say(f"[ERROR] {friendly_error(exc)}")
        except Exception:
            logger.exception("Unexpected error in %s", getattr(action, "__name__", "action"))
            self.say(
                f"[ERROR] Something unexpected happened. Details were written to "
                f"{self.settings.log_file}."
            )

    def exit(self) -> None:
        """Stop the main loop."""
        self._running = False
        self.say("Goodbye!")

    def ask(self, message: str, default: str | None = None) -> str:
        """Prompt for raw text, returning ``default`` when the answer is empty."""
        suffix = f" [{default}]" if default else ""
        answer = self._input(f"{message}{suffix}: ").strip()
        return answer or (default or "")

    def ask_int(self, message: str, minimum: int, maximum: int, default: int) -> int:
        """Prompt for an integer in a range, re-prompting on invalid answers."""
        for _ in range(MAX_ATTEMPTS):
            answer = self.ask(f"{message} ({minimum}-{maximum})", str(default))
            if answer.isdigit() and minimum <= int(answer) <= maximum:
                return int(answer)
            self.say(f"  Please enter a whole number between {minimum} and {maximum}.")
        raise DataValidationError("Too many invalid attempts")

    def ask_text(self, message: str) -> str:
        """Prompt for free text cleaned with a regex, re-prompting when nothing is left."""
        for _ in range(MAX_ATTEMPTS):
            try:
                return clean_user_input(self.ask(message))
            except DataValidationError as exc:
                self.say(f"  {exc}. Please try again.")
        raise DataValidationError("Too many invalid attempts")

    def ask_isbn(self) -> str:
        """Prompt for an ISBN-10 or ISBN-13 and reject invalid checksums."""
        for _ in range(MAX_ATTEMPTS):
            isbn = clean_isbn(self.ask("ISBN-10 or ISBN-13 (hyphens and spaces allowed)"))
            if is_valid_isbn(isbn):
                return isbn
            self.say("  That ISBN is not valid (wrong length or checksum). Please try again.")
        raise DataValidationError("Too many invalid ISBN attempts")

    def ask_choice(self, options: Sequence[tuple[str, str]]) -> str:
        """Print a submenu and return a valid choice."""
        for key, label in options:
            self.say(f"  [{key}] {label}")
        valid = {key for key, _ in options}
        for _ in range(MAX_ATTEMPTS):
            choice = self.ask("Choose an option").strip()
            if choice in valid:
                return choice
            self.say(f"  Please choose one of: {', '.join(sorted(valid))}.")
        raise DataValidationError("Too many invalid attempts")

    def ingest_by_subject(self) -> None:
        """Option 1: fetch a subject with both clients, compare timings and store the works."""
        self.section("Ingest Works by Subject")
        subject = to_subject_slug(self.ask_text("Subject (for example: science fiction)"))
        pages = self.ask_int("Number of pages to fetch", MIN_PAGES, MAX_PAGES, DEFAULT_PAGES)
        self.say(f"Subject slug: {subject} | {pages} page(s) of up to {SUBJECT_PAGE_LIMIT} works")

        sync_results, sync_seconds = self._fetch_subject_sync(subject, pages)
        async_results, async_seconds = self._fetch_subject_async(subject, pages)
        works = self._collect_subject_works(sync_results, async_results)

        self._report_failed_pages("sync", sync_results)
        self._report_failed_pages("async", async_results)
        if not works:
            self.say(f"No works were found for subject '{subject}'. Nothing was saved.")
            return

        saved = self.db.upsert_works_batch(works)
        speedup = sync_seconds / async_seconds if async_seconds > 0 else 0.0
        self.say()
        self.say(
            format_table(
                ["Mode", "Seconds", "Pages OK", "Works fetched"],
                [
                    [
                        "Sync",
                        round(sync_seconds, 3),
                        self._pages_ok(sync_results),
                        self._count_works(sync_results),
                    ],
                    [
                        "Async",
                        round(async_seconds, 3),
                        self._pages_ok(async_results),
                        self._count_works(async_results),
                    ],
                ],
            )
        )
        self.say(f"Speedup: async was {speedup:.2f}x the speed of sync.")
        self.db.record_benchmark(subject, pages, sync_seconds, async_seconds, len(works))
        self.say(f"Saved {saved} unique works with their authors. Responses were cached.")

    def _fetch_subject_sync(self, subject: str, pages: int) -> tuple[list[PageResult], float]:
        """Fetch the pages sequentially and time the run."""
        bar = ProgressBar(pages, "Sync  fetch", self.stream)
        with SyncLibraryClient(settings=self.settings, cache=self.db) as client:
            start = time.perf_counter()
            results = client.fetch_subject_pages(
                subject, pages, SUBJECT_PAGE_LIMIT, refresh=True, progress=lambda _: bar.advance()
            )
            elapsed = time.perf_counter() - start
        bar.finish()
        return results, elapsed

    def _fetch_subject_async(self, subject: str, pages: int) -> tuple[list[PageResult], float]:
        """Fetch the pages concurrently and time the run."""
        bar = ProgressBar(pages, "Async fetch", self.stream)

        async def fetch() -> tuple[list[PageResult], float]:
            async with AsyncLibraryClient(settings=self.settings, cache=self.db) as client:
                start = time.perf_counter()
                results = await client.fetch_subject_pages(
                    subject,
                    pages,
                    SUBJECT_PAGE_LIMIT,
                    refresh=True,
                    progress=lambda _: bar.advance(),
                )
                return results, time.perf_counter() - start

        results, elapsed = asyncio.run(fetch())
        bar.finish()
        return results, elapsed

    @staticmethod
    def _collect_subject_works(*page_lists: list[PageResult]) -> list[dict[str, Any]]:
        """Merge the works from every successful page, keeping one entry per work key."""
        works: dict[str, dict[str, Any]] = {}
        for results in page_lists:
            for result in results:
                if not result.ok or result.data is None:
                    continue
                for raw in result.data.get("works") or []:
                    work = work_from_subject_work(raw)
                    if work is not None:
                        works.setdefault(work["work_key"], work)
        return list(works.values())

    @staticmethod
    def _pages_ok(results: list[PageResult]) -> str:
        """Return ``ok/total`` for a list of page results."""
        return f"{sum(result.ok for result in results)}/{len(results)}"

    @staticmethod
    def _count_works(results: list[PageResult]) -> int:
        """Count the works returned across successful pages."""
        return sum(len((result.data or {}).get("works") or []) for result in results if result.ok)

    def _report_failed_pages(self, mode: str, results: list[PageResult]) -> None:
        """Print the pages that failed in one mode."""
        for result in results:
            if result.error is not None:
                self.say(f"  [{mode}] page {result.page} failed: {friendly_error(result.error)}")

    def search_books(self) -> None:
        """Option 2: search SQLite first, then the cache, then the Search API."""
        self.section("Search Books by Author/Title/ISBN")
        choice = self.ask_choice(SEARCH_MENU)
        if choice == "0":
            return
        query_type = {"1": "author", "2": "title", "3": "isbn"}.get(choice)
        if query_type == "isbn":
            query = self.ask_isbn()
        else:
            query = self.ask_text("Search text")
            if query_type is None:
                query_type = detect_query_type(query)
                self.say(f"Detected query type: {query_type}")
                if query_type == "isbn" and not is_valid_isbn(query):
                    raise DataValidationError("That ISBN is not valid (wrong checksum)")
        rows, source = self._search(query_type, query)
        if not rows:
            self.say(f"No results found for {query_type} '{query}'.")
            return
        self.say(f"\nSource: {source} | {len(rows)} result(s)")
        self.say(format_table(["Title", "Authors", "Year", "Languages", "ISBN"], rows))

    def _search(self, query_type: str, query: str) -> tuple[list[list[Any]], str]:
        """Return display rows and where they came from (database, cache or API)."""
        variants = isbn_variants(query) if query_type == "isbn" else []
        if query_type == "isbn":
            db_rows = self.db.search_by_isbn(variants, limit=SEARCH_RESULT_LIMIT)
        elif query_type == "author":
            db_rows = self.db.search_works_by_author(query, limit=SEARCH_RESULT_LIMIT)
        else:
            db_rows = self.db.search_works_by_title(query, limit=SEARCH_RESULT_LIMIT)
        if db_rows:
            return [
                self._display_row(row, row.get("authors") or "", variants) for row in db_rows
            ], ("database")

        api_query = variants[0] if variants else query
        with (
            Spinner("Searching Open Library", self.stream),
            SyncLibraryClient(settings=self.settings, cache=self.db) as client,
        ):
            data = client.search(api_query, by=query_type, limit=SEARCH_RESULT_LIMIT)
            source = "cache" if client.last_source == "cache" else "API"
        works = [work for work in map(work_from_search_doc, data.get("docs") or []) if work]
        if works:
            self.db.upsert_works_batch(works)
        rows = [
            self._display_row(work, ", ".join(a["name"] for a in work["authors"]), variants)
            for work in works
        ]
        return rows, source

    @staticmethod
    def _display_row(work: dict[str, Any], authors: str, preferred_isbns: list[str]) -> list[Any]:
        """Build a table row, showing the searched ISBN when the work lists it."""
        isbns = list(work.get("isbns") or [])
        isbn = next((value for value in preferred_isbns if value in isbns), None)
        return [
            work.get("title"),
            authors,
            work.get("first_publish_year"),
            ", ".join((work.get("languages") or [])[:3]),
            isbn or (isbns[0] if isbns else ""),
        ]

    def ask_catalog_path(self) -> Path:
        """Prompt for an existing CSV or JSON file, re-prompting on bad paths."""
        for _ in range(MAX_ATTEMPTS):
            path = Path(self.ask("Catalog file path (.csv, .json or .jsonl)", DEFAULT_CATALOG))
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                return path
            self.say(f"  '{path}' is not an existing .csv, .json or .jsonl file. Please try again.")
        raise DataValidationError("Too many invalid file paths")

    def process_catalog(self) -> None:
        """Option 3: stream, normalize, deduplicate and batch upsert a local catalog file."""
        self.section("Process & Deduplicate Local Catalog File")
        path = self.ask_catalog_path()
        stats = CatalogStats()
        inserted = updated = 0
        batch: list[dict[str, Any]] = []
        for record in deduplicate(stream_catalog(path, stats), stats):
            batch.append(catalog_record_to_work(record))
            self.stream.write(f"\rProcessing rows... {stats.rows_read}")
            self.stream.flush()
            if len(batch) >= CATALOG_BATCH_SIZE:
                new, existing = self._save_catalog_batch(batch)
                inserted, updated, batch = inserted + new, updated + existing, []
        if batch:
            new, existing = self._save_catalog_batch(batch)
            inserted, updated = inserted + new, updated + existing
        self.say(f"\rProcessing rows... {stats.rows_read} done")
        self.say()
        self.say(
            format_table(
                ["Metric", "Value"],
                [
                    ["Rows read", stats.rows_read],
                    ["Invalid rows skipped", stats.invalid_rows],
                    ["Invalid ISBNs dropped", stats.invalid_isbns],
                    ["Duplicates removed", stats.duplicates],
                    ["Unique records", stats.unique_records],
                    ["Rows inserted", inserted],
                    ["Rows updated", updated],
                ],
            )
        )

    def _save_catalog_batch(self, batch: list[dict[str, Any]]) -> tuple[int, int]:
        """Upsert a batch and return how many rows were new and how many already existed."""
        existing = self.db.existing_work_keys(work["work_key"] for work in batch)
        self.db.upsert_works_batch(batch)
        return len(batch) - len(existing), len(existing)

    def generate_reports(self) -> None:
        """Option 4: run the analytics and write the Excel, CSV and JSON reports."""
        self.section("Generate Analytical Reports")
        with Spinner("Analyzing data and writing reports", self.stream):
            processor = DataProcessor.from_database(self.db)
            paths = ReportGenerator(self.settings.output_dir).generate_all(processor)
        kpis = processor.kpis()
        if kpis["total_works"] == 0:
            self.say("Note: the database is empty, so the reports have no rows yet.")
        self.say(format_table(["KPI", "Value"], [[key, value] for key, value in kpis.items()]))
        self.say("\nFiles created:")
        for kind, path in paths.items():
            self.say(f"  {kind.upper():<5} {path}")

    def admin_menu(self) -> None:
        """Option 5: health, cache maintenance and configuration submenu."""
        handlers: dict[str, Callable[[], None]] = {
            "1": self.show_metrics,
            "2": self.purge_cache,
            "3": self.show_configuration,
            "4": self.change_env_value,
        }
        while True:
            self.section("System Health, Cache Maintenance & Database Administration")
            choice = self.ask_choice(ADMIN_MENU)
            if choice == "5":
                return
            self._guarded(handlers[choice])

    def show_metrics(self) -> None:
        """Print row counts, storage size, cache hit ratio and expired entries."""
        metrics = self.db.get_metrics()
        cache = metrics["cache"]
        self.say(format_table(["Table", "Rows"], list(metrics["row_counts"].items())))
        self.say()
        self.say(
            format_table(
                ["Metric", "Value"],
                [
                    ["Database file", metrics["db_path"]],
                    ["Database file size", human_size(metrics["file_size_bytes"])],
                    ["WAL file size", human_size(metrics["wal_size_bytes"])],
                    ["Total size on disk", human_size(metrics["total_size_bytes"])],
                    ["Page-based size", human_size(metrics["page_size_bytes"])],
                    ["Cache hits", cache["hits"]],
                    ["Cache misses", cache["misses"]],
                    ["Cache hit ratio", f"{cache['hit_ratio']:.1%}"],
                    ["Expired cache entries", metrics["expired_cache_entries"]],
                ],
            )
        )

    def purge_cache(self) -> None:
        """Delete expired cache entries."""
        removed = self.db.purge_expired()
        self.say(f"Purged {removed} expired cache entr{'y' if removed == 1 else 'ies'}.")

    def show_configuration(self) -> None:
        """Print the current settings with email addresses partially hidden."""
        rows = [
            [name.upper(), mask_email(str(value))]
            for name, value in self.settings.model_dump().items()
        ]
        self.say(format_table(["Setting", "Value"], rows, max_width=70))
        self.say(f"\n.env file: {self.env_file.resolve()}")

    def change_env_value(self) -> None:
        """Ask for a setting and a new value, then persist it in ``.env``."""
        self.say(f"Available settings: {', '.join(name.upper() for name in Settings.model_fields)}")
        key = self.ask("Setting to change").strip()
        if not key:
            raise DataValidationError("No setting name was entered")
        current = getattr(self.settings, key.lower(), None)
        value = self.ask(f"New value for {key.upper()}", None if current is None else str(current))
        self.update_setting(key, value)

    def update_setting(self, key: str, value: str) -> None:
        """Validate a new value, write it to ``.env`` with ``set_key`` and reload settings."""
        name = key.strip().lower()
        if name not in Settings.model_fields:
            raise ConfigurationError(f"Unknown setting {key.strip().upper()!r}")
        try:
            Settings(_env_file=None, **{**self.settings.model_dump(), name: value})
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"]
            raise ConfigurationError(f"Invalid value for {name.upper()}: {reason}") from exc
        try:
            self.env_file.touch(exist_ok=True)
            set_key(str(self.env_file), name.upper(), value)
        except OSError as exc:
            raise ConfigurationError(f"Could not write {self.env_file}: {exc}") from exc
        reload_settings()
        self.settings = Settings(_env_file=self.env_file)
        if name == "log_level":
            set_level(self.settings.log_level)
        if name.upper() in os.environ:
            self.say(f"Warning: the environment variable {name.upper()} is set and overrides .env.")
        self.say(f"{name.upper()} updated to {mask_email(value)} and settings reloaded.")

    def _find_uv(self) -> str:
        """Locate the ``uv`` executable on PATH or in its default install folder."""
        found = shutil.which("uv")
        if found:
            return found
        for candidate in (
            Path.home() / ".local" / "bin" / "uv.exe",
            Path.home() / ".local" / "bin" / "uv",
        ):
            if candidate.is_file():
                return str(candidate)
        raise ConfigurationError("The uv executable was not found on PATH")

    def run_quality_checks(self) -> None:
        """Option 6: run Ruff lint, the format check and pytest, then summarize."""
        self.section("Run Quality Checks & Test Suite Diagnostic")
        uv = self._find_uv()
        results = [self._run_command("Ruff lint", [uv, "run", "ruff", "check", "."])]
        format_result = self._run_command("Format check", [uv, "format", "--check"])
        if format_result.returncode == 2 and "unrecognized subcommand" in format_result.output:
            self.say("'uv format' is not supported by this uv version; using Ruff directly.")
            format_result = self._run_command(
                "Format check (ruff fallback)", [uv, "run", "ruff", "format", "--check", "."]
            )
        results.append(format_result)
        results.append(self._run_command("Tests", [uv, "run", "pytest", "-q"]))

        self.say()
        self.say(
            format_table(
                ["Check", "Exit code", "Result"],
                [[r.name, r.returncode, "PASS" if r.passed else "FAIL"] for r in results],
            )
        )
        passed = sum(result.passed for result in results)
        verdict = "ALL CHECKS PASSED" if passed == len(results) else "SOME CHECKS FAILED"
        self.say(f"{verdict} ({passed}/{len(results)})")

    def _run_command(self, name: str, command: list[str]) -> CommandResult:
        """Run one command without a shell and print its output and exit code."""
        self.say(f"\n$ {' '.join(['uv', *command[1:]])}")
        try:
            with Spinner(f"Running {name}", self.stream):
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=PROJECT_ROOT,
                    timeout=COMMAND_TIMEOUT_SECONDS,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.say(f"Could not run {name}: {exc}")
            return CommandResult(name, -1, str(exc))
        output = f"{completed.stdout or ''}{completed.stderr or ''}".rstrip()
        if output:
            self.say(output)
        self.say(f"Exit code: {completed.returncode}")
        return CommandResult(name, completed.returncode, output)
