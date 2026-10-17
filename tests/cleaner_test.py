"""Tests for the cleaning, ISBN, streaming and deduplication helpers."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from src.exceptions import DataValidationError
from src.processing.cleaner import (
    CatalogStats,
    catalog_record_to_work,
    clean_isbn,
    clean_user_input,
    deduplicate,
    detect_query_type,
    extract_isbns,
    fold_text,
    is_valid_isbn,
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_isbn13,
    isbn13_to_isbn10,
    isbn_variants,
    make_dedup_key,
    normalize_text,
    stream_catalog,
    to_subject_slug,
    work_from_search_doc,
    work_from_subject_work,
)

CSV_CONTENT = (
    "﻿title,author,isbn,first_publish_year,language,subjects\n"
    "Dune,Frank Herbert,978-0-441-17271-9,1965,eng,Science fiction; Space\n"
    "  DUNE  ,frank   herbert,0-441-17271-7,,eng,\n"
    "Cien anos,Gabriel García Márquez,,1967,spa|eng,Magic realism\n"
    ",,,,,\n"
    ",Nobody,123,1999,eng,\n"
    "Bad ISBN Book,Someone,978-0-441-17271-0,abc,,\n"
)


@pytest.mark.parametrize("isbn", ["0306406152", "0-306-40615-2", "080442957X", "080442957x"])
def test_valid_isbn10(isbn: str) -> None:
    """Valid ISBN-10 values pass, including a lowercase or uppercase ``X``."""
    assert is_valid_isbn10(isbn)


@pytest.mark.parametrize("isbn", ["0306406153", "030640615", "03064061522", "X306406152", "abc"])
def test_invalid_isbn10(isbn: str) -> None:
    """Wrong checksums, lengths and misplaced ``X`` are rejected."""
    assert not is_valid_isbn10(isbn)


@pytest.mark.parametrize("isbn", ["9780306406157", "978-0-306-40615-7", "978 0 441 17271 9"])
def test_valid_isbn13(isbn: str) -> None:
    """Valid ISBN-13 values pass with or without separators."""
    assert is_valid_isbn13(isbn)


@pytest.mark.parametrize(
    "isbn", ["9780306406158", "978030640615", "97803064061577", "978030640615X"]
)
def test_invalid_isbn13(isbn: str) -> None:
    """Wrong checksums, lengths and letters are rejected."""
    assert not is_valid_isbn13(isbn)


def test_isbn_conversions() -> None:
    """ISBN-10 converts to ISBN-13 and back, including ``X`` check digits."""
    assert isbn10_to_isbn13("0-306-40615-2") == "9780306406157"
    assert isbn10_to_isbn13("080442957X") == "9780804429573"
    assert isbn13_to_isbn10("9780804429573") == "080442957X"
    assert isbn13_to_isbn10("9791234567896") is None
    assert isbn_variants("0306406152") == ["9780306406157", "0306406152"]
    with pytest.raises(DataValidationError):
        isbn10_to_isbn13("0306406153")
    with pytest.raises(DataValidationError):
        isbn_variants("123")
    assert is_valid_isbn("9780306406157") and not is_valid_isbn("12345")


def test_extract_isbns_from_messy_text() -> None:
    """ISBN candidates are found among words, hyphens, spaces and duplicates."""
    text = "ISBN: 978-0-306-40615-7, old 0 306 40615 2; again 9780306406157 and phone 12345"
    assert extract_isbns(text) == ["9780306406157", "0306406152"]
    assert extract_isbns("ref 080442957x end") == ["080442957X"]
    assert extract_isbns(None) == []
    assert clean_isbn(" 0-8044-2957-x ") == "080442957X"


def test_normalize_text_handles_bom_nfc_and_whitespace() -> None:
    """BOM, control characters and extra whitespace are removed and text is NFC composed."""
    raw = "﻿  García\t​Márquez \x00\n "
    assert normalize_text(raw) == "García Márquez"
    assert normalize_text(None) == ""
    assert fold_text(" GARCÍA  Márquez") == "garcia marquez"


def test_clean_user_input() -> None:
    """Unsafe characters are removed while accents and safe punctuation are kept."""
    assert (
        clean_user_input("  Ñandú: <script>alert(1)</script>  ") == "Ñandú: script alert(1) script"
    )
    assert clean_user_input("O'Brien & Co.") == "O'Brien & Co."
    with pytest.raises(DataValidationError):
        clean_user_input("<<<>>>")
    with pytest.raises(DataValidationError):
        clean_user_input("a" * 10, max_length=5)


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("Science Fiction", "science_fiction"),
        ("  Ciencia   Ficción! ", "ciencia_ficcion"),
        ("Love & Romance", "love_romance"),
    ],
)
def test_to_subject_slug(raw: str, slug: str) -> None:
    """Subjects become lowercase ``[a-z0-9_]`` slugs."""
    assert to_subject_slug(raw) == slug


def test_to_subject_slug_rejects_empty() -> None:
    """A subject without letters or digits cannot become a slug."""
    with pytest.raises(DataValidationError):
        to_subject_slug("!!!")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("978-0-441-17271-9", "isbn"),
        ("080442957X", "isbn"),
        ("Frank Herbert", "author"),
        ("Miguel de Cervantes", "author"),
        ("The Lord of the Rings", "title"),
        ("dune", "title"),
        ("1984", "title"),
    ],
)
def test_detect_query_type(query: str, expected: str) -> None:
    """Queries are classified as ISBN, author or title."""
    assert detect_query_type(query) == expected


def test_make_dedup_key_prefers_isbn13() -> None:
    """ISBN-10 and ISBN-13 of the same book share a key; otherwise title and author are used."""
    assert make_dedup_key({"isbns": ["0306406152"]}) == make_dedup_key(
        {"isbn": "978-0-306-40615-7"}
    )
    first = make_dedup_key({"title": " DUNE ", "author": "Frank  Herbert"})
    second = make_dedup_key({"title": "dune", "authors": [{"name": "frank herbert"}]})
    assert first == second == "title:dune|author:frank herbert"


def test_stream_catalog_is_a_generator_and_skips_bad_rows(tmp_path: Path) -> None:
    """The CSV stream normalizes rows, skips invalid ones and counts problems."""
    path = tmp_path / "catalog.csv"
    path.write_text(CSV_CONTENT, encoding="utf-8")
    stats = CatalogStats()
    stream = stream_catalog(path, stats)
    assert inspect.isgenerator(stream)
    records = list(stream)

    assert stats.rows_read == 6
    assert stats.invalid_rows == 2
    assert stats.invalid_isbns == 1
    assert [record["title"] for record in records] == ["Dune", "DUNE", "Cien anos", "Bad ISBN Book"]
    assert records[0]["isbns"] == ["9780441172719"]
    assert records[0]["subjects"] == ["Science fiction", "Space"]
    assert records[1]["isbns"] == ["0441172717", "9780441172719"]
    assert records[1]["author"] == "frank herbert"
    assert records[1]["first_publish_year"] is None
    assert records[2]["author"] == "Gabriel García Márquez"
    assert records[2]["languages"] == ["spa", "eng"]
    assert records[3]["isbns"] == []
    assert records[3]["first_publish_year"] is None


def test_deduplicate_uses_set_and_counts(tmp_path: Path) -> None:
    """Duplicates by ISBN are removed and only first occurrences are kept."""
    path = tmp_path / "catalog.csv"
    path.write_text(CSV_CONTENT, encoding="utf-8")
    stats = CatalogStats()
    unique = list(deduplicate(stream_catalog(path, stats), stats))
    assert [record["title"] for record in unique] == ["Dune", "Cien anos", "Bad ISBN Book"]
    assert stats.duplicates == 1
    assert stats.unique_records == 3


def test_stream_json_array_and_json_lines(tmp_path: Path) -> None:
    """JSON arrays and JSON Lines are both supported; non-object rows are invalid."""
    array_path = tmp_path / "catalog.json"
    array_path.write_text(
        json.dumps([{"title": "Dune", "isbn": "9780441172719"}, "oops", {"title": ""}]),
        encoding="utf-8",
    )
    stats = CatalogStats()
    assert [record["title"] for record in stream_catalog(array_path, stats)] == ["Dune"]
    assert stats.invalid_rows == 2

    lines_path = tmp_path / "catalog.jsonl"
    lines_path.write_text('{"title": "Emma"}\n\n{broken\n{"title": "Ivanhoe"}\n', encoding="utf-8")
    stats = CatalogStats()
    assert [record["title"] for record in stream_catalog(lines_path, stats)] == ["Emma", "Ivanhoe"]
    assert stats.invalid_rows == 1


def test_stream_catalog_rejects_missing_and_unsupported_files(tmp_path: Path) -> None:
    """Missing files, unsupported extensions and broken JSON arrays raise errors."""
    with pytest.raises(DataValidationError):
        list(stream_catalog(tmp_path / "missing.csv"))
    unsupported = tmp_path / "catalog.txt"
    unsupported.write_text("title\nDune\n", encoding="utf-8")
    with pytest.raises(DataValidationError):
        list(stream_catalog(unsupported))
    broken = tmp_path / "broken.json"
    broken.write_text("[{", encoding="utf-8")
    with pytest.raises(DataValidationError):
        list(stream_catalog(broken))


def test_catalog_record_to_work_builds_stable_keys() -> None:
    """Catalog records get deterministic work and author keys."""
    record = {"title": "Dune", "author": "Frank Herbert", "isbns": ["9780441172719"]}
    work = catalog_record_to_work(record)
    assert work["source"] == "catalog"
    assert work["work_key"].startswith("CAT-")
    assert work["work_key"] == catalog_record_to_work(dict(record))["work_key"]
    assert work["authors"][0]["name"] == "Frank Herbert"
    assert work["authors"][0]["author_key"].startswith("CAT-A-")


def test_work_from_search_doc() -> None:
    """Search documents map to work dicts with valid ISBNs only."""
    doc = {
        "key": "/works/OL893414W",
        "title": "Dune",
        "author_key": ["OL79034A"],
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1965,
        "language": ["eng", "spa"],
        "isbn": ["9780441172719", "123", "0441172717"],
        "subject": ["Science fiction", "Science fiction", "Deserts"],
        "edition_count": 155,
    }
    work = work_from_search_doc(doc)
    assert work is not None
    assert work["work_key"] == "OL893414W"
    assert work["isbns"] == ["9780441172719", "0441172717"]
    assert work["subjects"] == ["Science fiction", "Deserts"]
    assert work["authors"] == [{"author_key": "OL79034A", "name": "Frank Herbert"}]
    assert work_from_search_doc({"key": "/works/OL1W"}) is None


def test_work_from_subject_work() -> None:
    """Subject works map to work dicts, using the availability ISBN when present."""
    raw = {
        "key": "/works/OL138052W",
        "title": "Alice's Adventures in Wonderland",
        "edition_count": 3548,
        "first_publish_year": 1865,
        "authors": [{"key": "/authors/OL22098A", "name": "Lewis Carroll"}],
        "subject": ["Fantasy"],
        "availability": {"isbn": "9780306406157"},
    }
    work = work_from_subject_work(raw)
    assert work is not None
    assert work["authors"][0]["author_key"] == "OL22098A"
    assert work["isbns"] == ["9780306406157"]
    assert work["languages"] == []
    assert work_from_subject_work({"title": "No key"}) is None
