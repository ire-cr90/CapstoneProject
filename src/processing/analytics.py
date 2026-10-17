"""pandas analytics over the catalog: missing values, dates, joins, aggregations and KPIs."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd

from src.logger import get_logger

if TYPE_CHECKING:
    from src.db.manager import DatabaseManager

logger = get_logger(__name__)

WORK_COLUMNS: tuple[str, ...] = (
    "work_key",
    "title",
    "normalized_title",
    "first_publish_year",
    "languages",
    "isbns",
    "subjects",
    "edition_count",
    "source",
    "created_at",
    "updated_at",
)
AUTHOR_COLUMNS: tuple[str, ...] = ("author_key", "name", "normalized_name", "updated_at")
LINK_COLUMNS: tuple[str, ...] = ("work_key", "author_key")
BENCHMARK_COLUMNS: tuple[str, ...] = (
    "id",
    "subject",
    "pages",
    "sync_seconds",
    "async_seconds",
    "speedup",
    "works_count",
    "run_at",
)
LIST_COLUMNS: tuple[str, ...] = ("languages", "isbns", "subjects")
UNDETERMINED_LANGUAGE = "und"
MIN_VALID_YEAR = 1
DEFAULT_TOP_N = 10

TableData = pd.DataFrame | list[dict[str, Any]] | None


def _frame(data: TableData, columns: tuple[str, ...]) -> pd.DataFrame:
    """Build a DataFrame that always contains the expected columns."""
    frame = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data or [])
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.Series([None] * len(frame), dtype="object")
    return frame


def _as_list(value: Any) -> list[Any]:
    """Coerce a list-like cell (list, tuple, JSON text or scalar) into a Python list."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def _clean_value(value: Any) -> Any:
    """Convert pandas missing values and numpy scalars into JSON-friendly Python values."""
    if isinstance(value, list):
        return value
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


class DataProcessor:
    """Prepares catalog DataFrames and computes the aggregations used by the reports."""

    def __init__(
        self,
        works: TableData,
        authors: TableData = None,
        work_authors: TableData = None,
        benchmarks: TableData = None,
        cache_hit_ratio: float = 0.0,
    ) -> None:
        """Load the raw tables and prepare the works DataFrame."""
        self.authors = _frame(authors, AUTHOR_COLUMNS)
        self.work_authors = _frame(work_authors, LINK_COLUMNS)
        self.benchmarks = self._prepare_benchmarks(_frame(benchmarks, BENCHMARK_COLUMNS))
        self.cache_hit_ratio = float(cache_hit_ratio)
        self.works = self._prepare_works(_frame(works, WORK_COLUMNS))
        logger.info(
            "DataProcessor loaded %d works, %d authors, %d links",
            len(self.works),
            len(self.authors),
            len(self.work_authors),
        )

    @classmethod
    def from_database(cls, db: DatabaseManager) -> DataProcessor:
        """Load every table from a ``DatabaseManager``."""
        return cls(
            works=db.fetch_works(),
            authors=db.fetch_authors(),
            work_authors=db.fetch_work_authors(),
            benchmarks=db.fetch_benchmarks(),
            cache_hit_ratio=float(db.get_cache_stats()["hit_ratio"]),
        )

    @staticmethod
    def _prepare_works(frame: pd.DataFrame) -> pd.DataFrame:
        """Parse years and timestamps, derive decades and flag missing values."""
        works = frame.copy()
        for column in LIST_COLUMNS:
            works[column] = works[column].map(_as_list).astype("object")

        max_year = date.today().year + 1
        year = pd.to_numeric(works["first_publish_year"], errors="coerce")
        year = year.where((year >= MIN_VALID_YEAR) & (year <= max_year))
        works["first_publish_year"] = year.round().astype("Int64")
        works["publish_date"] = pd.to_datetime(
            works["first_publish_year"].astype("string").str.zfill(4),
            format="%Y",
            errors="coerce",
        )
        works["decade"] = (works["first_publish_year"] // 10 * 10).astype("Int64")
        works["edition_count"] = pd.to_numeric(works["edition_count"], errors="coerce").astype(
            "Int64"
        )
        for column in ("created_at", "updated_at"):
            works[column] = pd.to_datetime(
                works[column], utc=True, errors="coerce", format="ISO8601"
            )

        works["missing_year"] = works["first_publish_year"].isna()
        works["missing_languages"] = works["languages"].map(len) == 0
        works["missing_edition_count"] = works["edition_count"].isna()
        works["languages_filled"] = (
            works["languages"]
            .map(lambda languages: languages or [UNDETERMINED_LANGUAGE])
            .astype("object")
        )
        return works

    @staticmethod
    def _prepare_benchmarks(frame: pd.DataFrame) -> pd.DataFrame:
        """Convert benchmark columns to numbers and timestamps."""
        benchmarks = frame.copy()
        for column in ("pages", "sync_seconds", "async_seconds", "speedup", "works_count"):
            benchmarks[column] = pd.to_numeric(benchmarks[column], errors="coerce")
        benchmarks["run_at"] = pd.to_datetime(
            benchmarks["run_at"], utc=True, errors="coerce", format="ISO8601"
        )
        return benchmarks

    def _author_names_by_work(self) -> dict[str, list[str]]:
        """Return the author names of each work, keyed by ``work_key``."""
        joined = self.works_with_authors().dropna(subset=["author_name"])
        return joined.groupby("work_key")["author_name"].agg(list).to_dict()

    def missing_values_report(self) -> dict[str, int]:
        """Return how many works miss a year, languages, edition count or authors."""
        linked = set(self.work_authors["work_key"].dropna())
        return {
            "first_publish_year": int(self.works["missing_year"].sum()),
            "languages": int(self.works["missing_languages"].sum()),
            "edition_count": int(self.works["missing_edition_count"].sum()),
            "authors": int((~self.works["work_key"].isin(linked)).sum()),
        }

    def works_with_authors(self) -> pd.DataFrame:
        """Join works with authors through ``work_authors`` (one row per work and author)."""
        works = self.works[["work_key", "title", "first_publish_year", "decade"]]
        authors = self.authors[["author_key", "name"]].rename(columns={"name": "author_name"})
        return works.merge(
            self.work_authors[["work_key", "author_key"]], on="work_key", how="left"
        ).merge(authors, on="author_key", how="left")

    def top_publication_years(self, n: int = DEFAULT_TOP_N) -> pd.DataFrame:
        """Return the years with the most works."""
        counts = (
            self.works.dropna(subset=["first_publish_year"])
            .groupby("first_publish_year")
            .size()
            .reset_index(name="count")
            .rename(columns={"first_publish_year": "year"})
        )
        return (
            counts.sort_values(["count", "year"], ascending=[False, True])
            .head(n)
            .reset_index(drop=True)
        )

    def works_per_decade(self) -> pd.DataFrame:
        """Return the number of works per decade in chronological order."""
        return (
            self.works.dropna(subset=["decade"])
            .groupby("decade")
            .size()
            .reset_index(name="count")
            .sort_values("decade")
            .reset_index(drop=True)
        )

    def top_languages(self, n: int = DEFAULT_TOP_N) -> pd.DataFrame:
        """Return the most common languages, counting ``und`` for works without languages."""
        exploded = self.works["languages_filled"].explode().dropna().astype(str)
        return self._count_values(exploded, "language", n)

    def top_subjects(self, n: int = DEFAULT_TOP_N) -> pd.DataFrame:
        """Return the most common subjects, compared case-insensitively."""
        exploded = self.works["subjects"].explode().dropna().astype(str).str.strip().str.lower()
        return self._count_values(exploded[exploded != ""], "subject", n)

    @staticmethod
    def _count_values(values: pd.Series, label: str, n: int) -> pd.DataFrame:
        """Count values and return the top ``n`` as a ``label, count`` DataFrame."""
        if values.empty:
            return pd.DataFrame(
                {label: pd.Series(dtype="object"), "count": pd.Series(dtype="int64")}
            )
        counts = values.value_counts().rename_axis(label).reset_index(name="count")
        return (
            counts.sort_values(["count", label], ascending=[False, True])
            .head(n)
            .reset_index(drop=True)
        )

    def top_authors(self, n: int = DEFAULT_TOP_N) -> pd.DataFrame:
        """Return the authors with the most works."""
        joined = self.works_with_authors().dropna(subset=["author_key", "author_name"])
        if joined.empty:
            return pd.DataFrame(
                {
                    "author_key": pd.Series(dtype="object"),
                    "author": pd.Series(dtype="object"),
                    "works": pd.Series(dtype="int64"),
                }
            )
        counts = (
            joined.groupby(["author_key", "author_name"])["work_key"]
            .nunique()
            .reset_index(name="works")
            .rename(columns={"author_name": "author"})
        )
        return (
            counts.sort_values(["works", "author"], ascending=[False, True])
            .head(n)
            .reset_index(drop=True)
        )

    def average_edition_count(self) -> float | None:
        """Return the mean edition count, ignoring missing values."""
        editions = self.works["edition_count"].dropna()
        return round(float(editions.mean()), 2) if not editions.empty else None

    def benchmark_summary(self) -> dict[str, Any]:
        """Summarize the sync versus async benchmark runs."""
        runs = self.benchmarks
        speedups = runs["speedup"].dropna()

        def mean_or_none(series: pd.Series) -> float | None:
            clean = series.dropna()
            return round(float(clean.mean()), 4) if not clean.empty else None

        return {
            "runs": len(runs),
            "mean_sync_seconds": mean_or_none(runs["sync_seconds"]),
            "mean_async_seconds": mean_or_none(runs["async_seconds"]),
            "mean_speedup": mean_or_none(speedups),
            "best_speedup": round(float(speedups.max()), 4) if not speedups.empty else None,
        }

    def data_completeness(self) -> float:
        """Return the percentage of filled key fields across all works."""
        if self.works.empty:
            return 0.0
        linked = set(self.work_authors["work_key"].dropna())
        checks = pd.DataFrame(
            {
                "title": self.works["title"].fillna("").astype(str).str.strip() != "",
                "year": ~self.works["missing_year"],
                "languages": ~self.works["missing_languages"],
                "isbns": self.works["isbns"].map(len) > 0,
                "edition_count": ~self.works["missing_edition_count"],
                "authors": self.works["work_key"].isin(linked),
            }
        )
        return round(float(checks.to_numpy().mean() * 100), 2)

    def kpis(self) -> dict[str, Any]:
        """Return the headline metrics shown in the report summary."""
        years = self.works["first_publish_year"].dropna()
        languages = set(self.works["languages"].explode().dropna().astype(str))
        return {
            "total_works": len(self.works),
            "total_authors": len(self.authors),
            "unique_languages": len(languages),
            "oldest_year": int(years.min()) if not years.empty else None,
            "newest_year": int(years.max()) if not years.empty else None,
            "average_edition_count": self.average_edition_count(),
            "cache_hit_ratio": round(self.cache_hit_ratio, 4),
            "average_async_speedup": self.benchmark_summary()["mean_speedup"],
            "data_completeness_pct": self.data_completeness(),
        }

    def works_table(self) -> pd.DataFrame:
        """Return a flat, report-friendly view of the works with authors and flags."""
        names = self._author_names_by_work()
        table = pd.DataFrame(
            {
                "work_key": self.works["work_key"],
                "title": self.works["title"],
                "authors": self.works["work_key"].map(lambda key: ", ".join(names.get(key, []))),
                "first_publish_year": self.works["first_publish_year"],
                "decade": self.works["decade"],
                "languages": self.works["languages"].map(", ".join),
                "isbn": self.works["isbns"].map(lambda isbns: isbns[0] if isbns else ""),
                "edition_count": self.works["edition_count"],
                "source": self.works["source"],
                "missing_year": self.works["missing_year"],
                "missing_languages": self.works["missing_languages"],
            }
        )
        return table.sort_values("title", key=lambda column: column.str.casefold()).reset_index(
            drop=True
        )

    def export_records(self) -> list[dict[str, Any]]:
        """Return the cleaned works as JSON-friendly dictionaries."""
        names = self._author_names_by_work()
        records: list[dict[str, Any]] = []
        for row in self.works.itertuples(index=False):
            records.append(
                {
                    "work_key": row.work_key,
                    "title": row.title,
                    "authors": names.get(row.work_key, []),
                    "first_publish_year": _clean_value(row.first_publish_year),
                    "decade": _clean_value(row.decade),
                    "languages": list(row.languages),
                    "isbns": list(row.isbns),
                    "subjects": list(row.subjects),
                    "edition_count": _clean_value(row.edition_count),
                    "source": _clean_value(row.source),
                }
            )
        return records
