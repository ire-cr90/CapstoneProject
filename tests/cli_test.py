"""Tests for the interactive ``CLIApp`` with scripted input and mocked network calls."""

from __future__ import annotations

import io
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.sync_client import PageResult
from src.cli.menu import (
    LINE,
    CLIApp,
    ProgressBar,
    Spinner,
    format_table,
    friendly_error,
    human_size,
    mask_email,
)
from src.config import Settings
from src.db.manager import DatabaseManager
from src.exceptions import DataValidationError, NotFoundError, RateLimitError

SAMPLE_CATALOG = Path(__file__).resolve().parents[1] / "data" / "sample_catalog.csv"

EXPECTED_MENU = f"""{LINE}
        OPEN LIBRARY INTELLIGENCE & CATALOG AUTOMATION SYSTEM
{LINE}
[1] Ingest Works by Subject
[2] Search Books by Author/Title/ISBN
[3] Process & Deduplicate Local Catalog File
[4] Generate Analytical Reports
[5] System Health, Cache Maintenance & Database Administration
[6] Run Quality Checks & Test Suite Diagnostic
[0] Exit
{LINE}
"""


def scripted(*answers: str) -> Callable[[str], str]:
    """Return an ``input`` replacement that replays answers, then raises ``EOFError``."""
    remaining: Iterator[str] = iter(answers)

    def fake_input(prompt: str) -> str:
        try:
            return next(remaining)
        except StopIteration as exc:
            raise EOFError from exc

    return fake_input


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that keep the database and reports inside ``tmp_path``."""
    return Settings(
        _env_file=None,
        db_path=str(tmp_path / "library.db"),
        output_dir=str(tmp_path / "outputs"),
        requests_per_second=1000,
        user_agent="TestAgent/1.0 (contact: tester@example.com)",
    )


def make_app(settings: Settings, *answers: str, **kwargs: Any) -> tuple[CLIApp, io.StringIO]:
    """Build an app with scripted answers and a captured output stream."""
    stream = io.StringIO()
    app = CLIApp(settings=settings, input_func=scripted(*answers), stream=stream, **kwargs)
    return app, stream


def subject_page(page: int, count: int = 2) -> PageResult:
    """Build a successful subject page with ``count`` works."""
    works = [
        {
            "key": f"/works/OL{page}{index}W",
            "title": f"Book {page}-{index}",
            "first_publish_year": 1900 + page,
            "edition_count": index + 1,
            "authors": [{"key": f"/authors/OL{page}A", "name": f"Author {page}"}],
        }
        for index in range(count)
    ]
    return PageResult(page=page, data={"works": works})


def test_menu_layout_and_exit(settings: Settings) -> None:
    """The menu matches the required layout and option 0 exits the loop."""
    app, stream = make_app(settings, "0")
    app.run()
    output = stream.getvalue()
    assert output.startswith(EXPECTED_MENU)
    assert output.rstrip().endswith("Goodbye!")


def test_invalid_option_reprompts(settings: Settings) -> None:
    """Unknown options print a message and show the menu again."""
    app, stream = make_app(settings, "9", "abc", "0")
    app.run()
    output = stream.getvalue()
    assert "'9' is not a valid option" in output
    assert output.count("[0] Exit") == 3


def test_ctrl_c_at_prompt_exits_cleanly(settings: Settings) -> None:
    """``KeyboardInterrupt`` at the main prompt exits without a traceback."""

    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    stream = io.StringIO()
    CLIApp(settings=settings, input_func=interrupt, stream=stream).run()
    assert "Goodbye!" in stream.getvalue()


def test_errors_and_interrupts_inside_actions_return_to_menu(settings: Settings) -> None:
    """Custom errors, unexpected errors and Ctrl+C inside an option never crash the loop."""
    app, stream = make_app(settings, "1", "2", "3", "0")
    with (
        patch.object(app, "ingest_by_subject", side_effect=NotFoundError("gone", 404, "u")),
        patch.object(app, "search_books", side_effect=KeyboardInterrupt),
        patch.object(app, "process_catalog", side_effect=ZeroDivisionError),
    ):
        app._actions.update(
            {"1": app.ingest_by_subject, "2": app.search_books, "3": app.process_catalog}
        )
        app.run()
    output = stream.getvalue()
    assert "[ERROR] Not found on Open Library: gone" in output
    assert "Operation cancelled" in output
    assert "Something unexpected happened" in output
    assert output.rstrip().endswith("Goodbye!")


def test_option_1_compares_sync_and_async(settings: Settings) -> None:
    """Option 1 validates input, compares both clients and stores works and the benchmark."""
    app, stream = make_app(settings, "Science Fiction!", "42", "2")
    sync_pages = [subject_page(1), subject_page(2)]
    async_pages = [subject_page(1), PageResult(page=2, error=NotFoundError("missing"))]
    with (
        patch(
            "src.cli.menu.SyncLibraryClient.fetch_subject_pages", return_value=sync_pages
        ) as sync,
        patch(
            "src.cli.menu.AsyncLibraryClient.fetch_subject_pages",
            new=AsyncMock(return_value=async_pages),
        ),
    ):
        app.ingest_by_subject()
    output = stream.getvalue()
    assert "Subject slug: science_fiction" in output
    assert "Please enter a whole number between 1 and 10" in output
    assert sync.call_args.args[:2] == ("science_fiction", 2)
    assert sync.call_args.kwargs["refresh"] is True
    assert "[async] page 2 failed" in output
    assert "Speedup:" in output
    assert app.db.count_rows("works") == 4
    assert app.db.count_rows("authors") == 2
    benchmark = app.db.fetch_benchmarks()[0]
    assert benchmark["subject"] == "science_fiction"
    assert benchmark["works_count"] == 4


def test_option_1_with_no_works(settings: Settings) -> None:
    """Option 1 explains when a subject returns no works and saves nothing."""
    app, stream = make_app(settings, "zzzz", "1")
    empty = [PageResult(page=1, data={"works": []})]
    with (
        patch("src.cli.menu.SyncLibraryClient.fetch_subject_pages", return_value=empty),
        patch(
            "src.cli.menu.AsyncLibraryClient.fetch_subject_pages", new=AsyncMock(return_value=empty)
        ),
    ):
        app.ingest_by_subject()
    assert "No works were found" in stream.getvalue()
    assert app.db.count_rows("benchmarks") == 0


def test_option_2_rejects_invalid_isbn(settings: Settings) -> None:
    """Invalid ISBN checksums are rejected with re-prompts until attempts run out."""
    app, stream = make_app(settings, "3", "978-0-441-17271-0", "123", "abc")
    with pytest.raises(DataValidationError):
        app.search_books()
    assert stream.getvalue().count("That ISBN is not valid") == 3


def test_option_2_isbn_found_in_database(settings: Settings) -> None:
    """An ISBN-10 search finds a work stored with its ISBN-13 without calling the API."""
    app, stream = make_app(settings, "3", "0-441-17271-7")
    app.db.upsert_works_batch(
        [
            {
                "work_key": "OL893414W",
                "title": "Dune",
                "isbns": ["9780441172719"],
                "authors": [{"author_key": "OL79034A", "name": "Frank Herbert"}],
            }
        ]
    )
    with patch("src.cli.menu.SyncLibraryClient.search") as search:
        app.search_books()
    search.assert_not_called()
    output = stream.getvalue()
    assert "Source: database | 1 result(s)" in output
    assert "Frank Herbert" in output
    assert "9780441172719" in output


def test_option_2_falls_back_to_api_and_upserts(settings: Settings) -> None:
    """A database miss calls the Search API, shows the source and stores the results."""
    app, stream = make_app(settings, "2", "Cien años")
    payload = {
        "docs": [
            {
                "key": "/works/OL274505W",
                "title": "Cien años de soledad",
                "author_key": ["OL4586796A"],
                "author_name": ["Gabriel García Márquez"],
                "first_publish_year": 1967,
                "language": ["spa"],
            }
        ]
    }
    with patch("src.cli.menu.SyncLibraryClient.search", return_value=payload) as search:
        app.search_books()
    assert search.call_args.kwargs["by"] == "title"
    output = stream.getvalue()
    assert "Source: API | 1 result(s)" in output
    assert app.db.search_works_by_title("cien anos")[0]["work_key"] == "OL274505W"


def test_option_2_auto_detect_and_back(settings: Settings) -> None:
    """Auto-detect reports the detected type; option 0 goes back."""
    app, stream = make_app(settings, "4", "Frank Herbert")
    with patch("src.cli.menu.SyncLibraryClient.search", return_value={"docs": []}):
        app.search_books()
    assert "Detected query type: author" in stream.getvalue()
    assert "No results found" in stream.getvalue()
    back_app, _ = make_app(settings, "0")
    back_app.search_books()


def test_option_3_processes_sample_catalog(settings: Settings, tmp_path: Path) -> None:
    """Option 3 streams, deduplicates and upserts the sample catalog, then reports counts."""
    catalog = tmp_path / "catalog.csv"
    shutil.copy(SAMPLE_CATALOG, catalog)
    app, stream = make_app(settings, "missing.csv", str(catalog))
    app.process_catalog()
    output = stream.getvalue()
    assert "is not an existing" in output
    for label, value in [
        ("Rows read", 37),
        ("Invalid rows skipped", 3),
        ("Invalid ISBNs dropped", 2),
        ("Duplicates removed", 9),
        ("Rows inserted", 25),
        ("Rows updated", 0),
    ]:
        assert any(
            line.startswith(label) and line.endswith(str(value)) for line in output.splitlines()
        )
    assert app.db.count_rows("works") == 25

    rerun, rerun_stream = make_app(settings, str(catalog))
    rerun.process_catalog()
    assert any(
        line.startswith("Rows updated") and line.endswith("25")
        for line in rerun_stream.getvalue().splitlines()
    )


def test_option_4_generates_reports(settings: Settings, tmp_path: Path) -> None:
    """Option 4 prints KPIs and the paths of the three report files."""
    app, stream = make_app(settings)
    app.generate_reports()
    output = stream.getvalue()
    assert "database is empty" in output
    for prefix in ("library_report_", "library_summary_", "library_export_"):
        assert prefix in output
    assert len(list((tmp_path / "outputs").iterdir())) == 3


def test_option_5_admin_submenu(settings: Settings, tmp_path: Path) -> None:
    """Metrics, purge and configuration views work and the submenu returns on Back."""
    app, stream = make_app(settings, "1", "2", "3", "9", "5")
    app.admin_menu()
    output = stream.getvalue()
    assert "api_cache" in output
    assert "Cache hit ratio" in output
    assert "Purged 0 expired cache entries." in output
    assert "t***@example.com" in output
    assert "tester@example.com" not in output
    assert "Please choose one of" in output


def test_option_5_change_env_value(settings: Settings, tmp_path: Path) -> None:
    """A valid setting is written to ``.env`` and reloaded; invalid ones are rejected."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DB_PATH={settings.db_path}\nOUTPUT_DIR={settings.output_dir}\n", encoding="utf-8"
    )
    app, stream = make_app(
        settings,
        "4",
        "max_retries",
        "5",
        "4",
        "NOT_A_SETTING",
        "x",
        "4",
        "MAX_RETRIES",
        "-3",
        "5",
        env_file=env_file,
    )
    app.admin_menu()
    output = stream.getvalue()
    assert "MAX_RETRIES updated to 5" in output
    assert "MAX_RETRIES='5'" in env_file.read_text(encoding="utf-8")
    assert app.settings.max_retries == 5
    assert "Unknown setting 'NOT_A_SETTING'" in output
    assert "Invalid value for MAX_RETRIES" in output
    assert app.db.db_path == settings.db_path


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a fake ``CompletedProcess``."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_option_6_runs_checks_without_shell(settings: Settings) -> None:
    """Option 6 runs lint, format check and tests and summarizes the exit codes."""
    runner = MagicMock(
        side_effect=[completed(0, "All checks passed!"), completed(0), completed(1, "1 failed")]
    )
    app, stream = make_app(settings, runner=runner)
    with patch("src.cli.menu.shutil.which", return_value="/usr/bin/uv"):
        app.run_quality_checks()
    commands = [call.args[0] for call in runner.call_args_list]
    assert commands == [
        ["/usr/bin/uv", "run", "ruff", "check", "."],
        ["/usr/bin/uv", "format", "--check"],
        ["/usr/bin/uv", "run", "pytest", "-q"],
    ]
    assert all("shell" not in call.kwargs for call in runner.call_args_list)
    output = stream.getvalue()
    assert "SOME CHECKS FAILED (2/3)" in output
    assert "Exit code: 1" in output


def test_option_6_falls_back_when_uv_format_is_missing(settings: Settings) -> None:
    """When ``uv format`` is not available, Ruff's format check is used instead."""
    runner = MagicMock(
        side_effect=[
            completed(0),
            completed(2, stderr="error: unrecognized subcommand 'format'"),
            completed(0),
            completed(0),
        ]
    )
    app, stream = make_app(settings, runner=runner)
    with patch("src.cli.menu.shutil.which", return_value="uv"):
        app.run_quality_checks()
    assert runner.call_args_list[2].args[0] == ["uv", "run", "ruff", "format", "--check", "."]
    output = stream.getvalue()
    assert "using Ruff directly" in output
    assert "ALL CHECKS PASSED (3/3)" in output


def test_helpers() -> None:
    """Formatting helpers produce aligned tables, masked emails and readable sizes."""
    table = format_table(["Name", "Count"], [["a", 5], ["long name", 12]]).splitlines()
    assert table[0] == "Name      | Count"
    assert table[2] == "a         |     5"
    assert format_table(["X"], [["y" * 50]], max_width=10).splitlines()[2] == "yyyyyyy..."
    assert mask_email("Agent (contact: jane.doe@mail.com)") == "Agent (contact: j***@mail.com)"
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.00 KB"
    assert friendly_error(RateLimitError("slow", retry_after=30)).endswith("about 30 seconds.")


def test_progress_indicators() -> None:
    """The progress bar and spinner write to the stream and finish cleanly."""
    stream = io.StringIO()
    bar = ProgressBar(2, "Fetch", stream, width=4)
    bar.advance()
    bar.advance()
    bar.finish()
    assert "Fetch [##--] 1/2" in stream.getvalue()
    assert stream.getvalue().endswith("Fetch [####] 2/2\n")
    with Spinner("Working", stream, interval=0.01):
        pass
    assert stream.getvalue().endswith("Working... done\n")


def test_database_reopens_when_path_changes(settings: Settings, tmp_path: Path) -> None:
    """Changing ``db_path`` makes the app reopen the database at the new location."""
    app, _ = make_app(settings)
    first = app.db
    app.settings = Settings(_env_file=None, db_path=str(tmp_path / "other.db"))
    second = app.db
    assert first is not second
    assert isinstance(second, DatabaseManager)
    assert second.db_path.endswith("other.db")
    app.close()
