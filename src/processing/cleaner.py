"""Pure text cleaning, ISBN validation, catalog streaming and deduplication helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from src.exceptions import DataValidationError
from src.logger import get_logger

logger = get_logger(__name__)

QueryType = Literal["isbn", "author", "title"]

BOM = "﻿"
DEFAULT_MAX_INPUT_LENGTH = 120
MAX_SUBJECTS_PER_WORK = 25
MIN_YEAR = 0
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".json", ".jsonl"})
LIST_SEPARATOR_PATTERN = re.compile(r"\s*[;|]\s*")
UNSAFE_INPUT_PATTERN = re.compile(r"[^\w\s'.,:;&()!?\-]|_")
SLUG_INVALID_PATTERN = re.compile(r"[^a-z0-9]+")
ISBN_SEPARATOR_PATTERN = re.compile(r"[\s-]")
ISBN_CANDIDATE_PATTERN = re.compile(
    r"(?<![0-9Xx])(?:97[89](?:[\s-]?\d){10}|\d(?:[\s-]?\d){8}[\s-]?[\dXx])(?![0-9Xx])"
)
ISBN10_PATTERN = re.compile(r"\d{9}[\dX]")
ISBN13_PATTERN = re.compile(r"97[89]\d{10}")
TITLE_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "el", "la", "los", "las", "y"}
)


@dataclass
class CatalogStats:
    """Counters collected while streaming and deduplicating a catalog file."""

    rows_read: int = 0
    invalid_rows: int = 0
    invalid_isbns: int = 0
    duplicates: int = 0
    unique_records: int = 0


def normalize_text(value: Any) -> str:
    """Apply NFC normalization, drop the BOM and control characters, and collapse whitespace."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).replace(BOM, "")
    text = "".join(
        " " if char in "\t\n\r" else char
        for char in text
        if char in "\t\n\r" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return " ".join(text.split())


def fold_text(value: Any) -> str:
    """Return an accent-insensitive, case-insensitive form of ``value`` for matching."""
    decomposed = unicodedata.normalize("NFKD", normalize_text(value))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.split()).casefold()


def clean_user_input(value: str, max_length: int = DEFAULT_MAX_INPUT_LENGTH) -> str:
    """Keep letters, digits, spaces and safe punctuation; reject empty or overly long input."""
    cleaned = " ".join(UNSAFE_INPUT_PATTERN.sub(" ", normalize_text(value)).split())
    if not cleaned:
        raise DataValidationError("Input is empty after removing unsupported characters")
    if len(cleaned) > max_length:
        raise DataValidationError(f"Input is longer than {max_length} characters")
    return cleaned


def to_subject_slug(value: str) -> str:
    """Convert a subject into the Open Library slug format, such as ``science_fiction``."""
    slug = SLUG_INVALID_PATTERN.sub("_", fold_text(value)).strip("_")
    if not slug:
        raise DataValidationError(f"Cannot build a subject slug from {value!r}")
    return slug


def clean_isbn(value: str) -> str:
    """Remove spaces and hyphens from an ISBN and uppercase a trailing ``x``."""
    return ISBN_SEPARATOR_PATTERN.sub("", normalize_text(value)).upper()


def extract_isbns(text: Any) -> list[str]:
    """Find ISBN-10 and ISBN-13 candidates in free text, returned without separators."""
    found: list[str] = []
    for match in ISBN_CANDIDATE_PATTERN.finditer(normalize_text(text)):
        candidate = clean_isbn(match.group())
        if candidate not in found:
            found.append(candidate)
    return found


def is_valid_isbn10(isbn: str) -> bool:
    """Validate an ISBN-10 checksum: weights 10 to 1, ``X`` means 10, sum mod 11 equals 0."""
    value = clean_isbn(isbn)
    if not ISBN10_PATTERN.fullmatch(value):
        return False
    total = sum(
        (10 - index) * (10 if char == "X" else int(char)) for index, char in enumerate(value)
    )
    return total % 11 == 0


def is_valid_isbn13(isbn: str) -> bool:
    """Validate an ISBN-13 checksum: alternating weights 1 and 3, sum mod 10 equals 0."""
    value = clean_isbn(isbn)
    if not value.isdigit() or len(value) != 13:
        return False
    total = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(value))
    return total % 10 == 0


def is_valid_isbn(isbn: str) -> bool:
    """Return ``True`` for a valid ISBN-10 or ISBN-13."""
    return is_valid_isbn10(isbn) or is_valid_isbn13(isbn)


def isbn10_to_isbn13(isbn: str) -> str:
    """Convert a valid ISBN-10 into its ISBN-13 form with the ``978`` prefix."""
    value = clean_isbn(isbn)
    if not is_valid_isbn10(value):
        raise DataValidationError(f"Invalid ISBN-10: {isbn!r}")
    core = f"978{value[:9]}"
    total = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(core))
    return f"{core}{(10 - total % 10) % 10}"


def isbn13_to_isbn10(isbn: str) -> str | None:
    """Convert a valid ``978`` ISBN-13 into ISBN-10, or return ``None`` when impossible."""
    value = clean_isbn(isbn)
    if not is_valid_isbn13(value) or not value.startswith("978"):
        return None
    core = value[3:12]
    remainder = (11 - sum((10 - index) * int(char) for index, char in enumerate(core)) % 11) % 11
    return f"{core}{'X' if remainder == 10 else remainder}"


def to_isbn13(isbn: str) -> str | None:
    """Return the ISBN-13 form of a valid ISBN, or ``None`` when it is invalid."""
    value = clean_isbn(isbn)
    if is_valid_isbn13(value):
        return value
    if is_valid_isbn10(value):
        return isbn10_to_isbn13(value)
    return None


def isbn_variants(isbn: str) -> list[str]:
    """Return every equivalent form (ISBN-13 and ISBN-10) of a valid ISBN."""
    isbn13 = to_isbn13(isbn)
    if isbn13 is None:
        raise DataValidationError(f"Invalid ISBN checksum: {isbn!r}")
    isbn10 = isbn13_to_isbn10(isbn13)
    return [isbn13, isbn10] if isbn10 else [isbn13]


def detect_query_type(value: str) -> QueryType:
    """Guess whether a search string is an ISBN, an author name or a title."""
    text = normalize_text(value)
    compact = clean_isbn(text)
    if ISBN10_PATTERN.fullmatch(compact) or ISBN13_PATTERN.fullmatch(compact):
        return "isbn"
    words = text.split()
    looks_like_name = (
        2 <= len(words) <= 4
        and not any(char.isdigit() for char in text)
        and not any(word.casefold() in TITLE_STOPWORDS for word in words)
        and all(
            word[0].isupper() or word.casefold() in {"de", "del", "van", "von"} for word in words
        )
    )
    return "author" if looks_like_name else "title"


def _record_authors(record: dict[str, Any]) -> list[str]:
    """Return the author names of a record that has ``author`` or ``authors``."""
    names: list[str] = []
    if record.get("author"):
        names.append(str(record["author"]))
    for author in record.get("authors") or []:
        names.append(str(author.get("name", "")) if isinstance(author, dict) else str(author))
    return [name for name in names if name.strip()]


def make_dedup_key(record: dict[str, Any]) -> str:
    """Return the ISBN-13 if any, otherwise the folded title plus the folded first author."""
    raw_isbns = record.get("isbns") or record.get("isbn") or []
    candidates = extract_isbns(raw_isbns) if isinstance(raw_isbns, str) else list(raw_isbns)
    for candidate in candidates:
        isbn13 = to_isbn13(candidate)
        if isbn13:
            return f"isbn:{isbn13}"
    authors = _record_authors(record)
    first_author = fold_text(authors[0]) if authors else ""
    return f"title:{fold_text(record.get('title'))}|author:{first_author}"


def _split_list(value: Any) -> list[str]:
    """Split a ``;`` or ``|`` separated field (or a list) into clean, unique strings."""
    items = (
        value if isinstance(value, list) else LIST_SEPARATOR_PATTERN.split(normalize_text(value))
    )
    result: list[str] = []
    for item in items:
        text = normalize_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _parse_year(value: Any) -> int | None:
    """Parse a publication year, returning ``None`` when it is missing or implausible."""
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(r"-?\d{1,4}", text)
    if match is None:
        return None
    year = int(match.group())
    return year if MIN_YEAR <= year <= date.today().year + 1 else None


def normalize_catalog_row(row: dict[str, Any], stats: CatalogStats | None = None) -> dict[str, Any]:
    """Normalize one raw catalog row; raise ``DataValidationError`` when it is unusable."""
    fields = {
        normalize_text(key).casefold(): value for key, value in row.items() if key is not None
    }
    if not any(normalize_text(value) for value in fields.values() if not isinstance(value, list)):
        raise DataValidationError("Row is empty")
    title = normalize_text(fields.get("title"))
    if not title:
        raise DataValidationError("Row has no title")

    isbns: list[str] = []
    for candidate in extract_isbns(fields.get("isbn") or fields.get("isbns")):
        isbn13 = to_isbn13(candidate)
        if isbn13 is None:
            logger.warning("Dropping invalid ISBN %s for %r", candidate, title)
            if stats is not None:
                stats.invalid_isbns += 1
            continue
        for variant in (candidate, isbn13):
            if variant not in isbns:
                isbns.append(variant)

    languages = [language.casefold() for language in _split_list(fields.get("language"))]
    return {
        "title": title,
        "author": normalize_text(fields.get("author")) or None,
        "isbns": isbns,
        "first_publish_year": _parse_year(fields.get("first_publish_year") or fields.get("year")),
        "languages": languages,
        "subjects": _split_list(fields.get("subjects"))[:MAX_SUBJECTS_PER_WORK],
    }


def _iter_raw_rows(path: Path) -> Iterator[tuple[int, Any]]:
    """Yield ``(line_number, raw_row)`` pairs from a CSV, JSON array or JSON Lines file."""
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                yield reader.line_num, row
        return

    with path.open(encoding="utf-8-sig") as handle:
        head = handle.read(1)
        while head and head.isspace():
            head = handle.read(1)
        handle.seek(0)
        if head == "[":
            try:
                items = json.load(handle)
            except json.JSONDecodeError as exc:
                raise DataValidationError(f"Invalid JSON array in {path}: {exc}") from exc
            yield from enumerate(items, start=1)
            return
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError:
                yield line_number, None


def stream_catalog(path: str | Path, stats: CatalogStats | None = None) -> Iterator[dict[str, Any]]:
    """Lazily yield normalized catalog rows; invalid rows are logged, counted and skipped."""
    file_path = Path(path)
    if not file_path.is_file():
        raise DataValidationError(f"Catalog file not found: {file_path}")
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DataValidationError(
            f"Unsupported file type {file_path.suffix!r}; use {sorted(SUPPORTED_EXTENSIONS)}"
        )
    for line_number, raw in _iter_raw_rows(file_path):
        if stats is not None:
            stats.rows_read += 1
        try:
            if not isinstance(raw, dict):
                raise DataValidationError("Row is not an object")
            record = normalize_catalog_row(raw, stats)
        except DataValidationError as exc:
            logger.warning("Skipping %s line %d: %s", file_path.name, line_number, exc)
            if stats is not None:
                stats.invalid_rows += 1
            continue
        record["line"] = line_number
        yield record


def deduplicate(
    records: Iterable[dict[str, Any]], stats: CatalogStats | None = None
) -> Iterator[dict[str, Any]]:
    """Yield only the first record for each dedup key, tracked with a set."""
    seen: set[str] = set()
    for record in records:
        key = make_dedup_key(record)
        if key in seen:
            if stats is not None:
                stats.duplicates += 1
            logger.info("Duplicate removed: %s", key)
            continue
        seen.add(key)
        if stats is not None:
            stats.unique_records += 1
        yield record


def _short_hash(value: str) -> str:
    """Return a short, stable hexadecimal hash of ``value``."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16].upper()


def catalog_record_to_work(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a normalized catalog record into a work dict for ``DatabaseManager``."""
    authors = [
        {"author_key": f"CAT-A-{_short_hash(fold_text(name))}", "name": name}
        for name in _record_authors(record)
    ]
    return {
        "work_key": f"CAT-{_short_hash(make_dedup_key(record))}",
        "title": record["title"],
        "normalized_title": fold_text(record["title"]),
        "first_publish_year": record.get("first_publish_year"),
        "languages": record.get("languages") or [],
        "isbns": record.get("isbns") or [],
        "subjects": record.get("subjects") or [],
        "edition_count": record.get("edition_count"),
        "source": "catalog",
        "authors": authors,
    }


def _key_suffix(key: Any) -> str:
    """Return the last segment of an Open Library key such as ``/works/OL1W``."""
    return normalize_text(key).rstrip("/").rsplit("/", 1)[-1]


def _api_isbns(values: Iterable[Any]) -> list[str]:
    """Keep only valid ISBNs from an API list."""
    result: list[str] = []
    for value in values:
        isbn = clean_isbn(str(value))
        if is_valid_isbn(isbn) and isbn not in result:
            result.append(isbn)
    return result


def _int_or_none(value: Any) -> int | None:
    """Convert a value to ``int`` or return ``None``."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def work_from_search_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Map a ``/search.json`` document to a work dict, or ``None`` if it lacks key or title."""
    work_key = _key_suffix(doc.get("key"))
    title = normalize_text(doc.get("title"))
    if not work_key or not title:
        return None
    authors = [
        {"author_key": _key_suffix(key), "name": normalize_text(name)}
        for key, name in zip(
            doc.get("author_key") or [], doc.get("author_name") or [], strict=False
        )
        if _key_suffix(key) and normalize_text(name)
    ]
    return {
        "work_key": work_key,
        "title": title,
        "normalized_title": fold_text(title),
        "first_publish_year": _int_or_none(doc.get("first_publish_year")),
        "languages": [normalize_text(code) for code in doc.get("language") or []],
        "isbns": _api_isbns(doc.get("isbn") or []),
        "subjects": _split_list(doc.get("subject") or [])[:MAX_SUBJECTS_PER_WORK],
        "edition_count": _int_or_none(doc.get("edition_count")),
        "source": "api",
        "authors": authors,
    }


def work_from_subject_work(work: dict[str, Any]) -> dict[str, Any] | None:
    """Map a work from ``/subjects/{subject}.json`` to a work dict, or ``None`` if unusable."""
    work_key = _key_suffix(work.get("key"))
    title = normalize_text(work.get("title"))
    if not work_key or not title:
        return None
    authors = [
        {"author_key": _key_suffix(author.get("key")), "name": normalize_text(author.get("name"))}
        for author in work.get("authors") or []
        if _key_suffix(author.get("key")) and normalize_text(author.get("name"))
    ]
    availability = work.get("availability") or {}
    isbn = availability.get("isbn") if isinstance(availability, dict) else None
    return {
        "work_key": work_key,
        "title": title,
        "normalized_title": fold_text(title),
        "first_publish_year": _int_or_none(work.get("first_publish_year")),
        "languages": [],
        "isbns": _api_isbns([isbn] if isbn else []),
        "subjects": _split_list(work.get("subject") or [])[:MAX_SUBJECTS_PER_WORK],
        "edition_count": _int_or_none(work.get("edition_count")),
        "source": "api",
        "authors": authors,
    }
