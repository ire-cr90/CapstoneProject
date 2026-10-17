"""Tests for the settings loader."""

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings, reload_settings


def test_defaults_without_env_file() -> None:
    """Settings fall back to safe defaults when no ``.env`` file is present."""
    settings = Settings(_env_file=None)
    assert settings.api_base_url == "https://openlibrary.org"
    assert settings.max_retries == 3
    assert settings.log_level == "INFO"


def test_log_level_is_normalized() -> None:
    """Lowercase log levels are accepted and normalized."""
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_retries": -1},
        {"log_level": "LOUD"},
        {"api_base_url": "ftp://openlibrary.org"},
        {"user_agent": "   "},
        {"max_concurrency": 0},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    """Invalid configuration values raise a validation error."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_reload_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reload_settings`` picks up environment changes that the cached instance missed."""
    monkeypatch.setenv("MAX_RETRIES", "7")
    reloaded = reload_settings()
    assert reloaded.max_retries == 7
    assert get_settings() is reloaded
    monkeypatch.delenv("MAX_RETRIES")
    reload_settings()
