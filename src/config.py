"""Application settings loaded from environment variables and the ``.env`` file."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_LOG_LEVELS: frozenset[str] = frozenset(logging.getLevelNamesMapping())


class Settings(BaseSettings):
    """Typed configuration with safe defaults so tests and CI run without a ``.env`` file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_base_url: str = "https://openlibrary.org"
    request_timeout: float = Field(default=10.0, gt=0)
    max_retries: int = 3
    backoff_factor: float = Field(default=0.5, ge=0)
    max_concurrency: int = Field(default=5, ge=1, le=50)
    requests_per_second: float = Field(default=3.0, gt=0)
    cache_ttl_hours: int = Field(default=24, ge=0)
    db_path: str = "data/library.db"
    output_dir: str = "data/outputs"
    log_level: str = "INFO"
    log_file: str = "library.log"
    user_agent: str = "OpenLibraryCapstone/1.0 (contact: your-email@example.com)"

    @field_validator("max_retries")
    @classmethod
    def _validate_max_retries(cls, value: int) -> int:
        """Ensure the retry count is never negative."""
        if value < 0:
            raise ValueError("max_retries must be >= 0")
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Normalize the log level and ensure it is a level known by ``logging``."""
        level = value.strip().upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(VALID_LOG_LEVELS)}")
        return level

    @field_validator("api_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        """Require an HTTP(S) URL and drop any trailing slash."""
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("api_base_url must start with http:// or https://")
        return url

    @field_validator("user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str) -> str:
        """Reject an empty User-Agent, since Open Library asks clients to identify themselves."""
        agent = value.strip()
        if not agent:
            raise ValueError("user_agent must not be empty")
        return agent


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cached settings and load them again from the environment and ``.env``."""
    get_settings.cache_clear()
    return get_settings()
