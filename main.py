"""Entry point for the Open Library Intelligence & Catalog Automation System."""

from src.config import get_settings


def main() -> None:
    """Print a short startup message with the configured API base URL."""
    settings = get_settings()
    print(f"Open Library Intelligence & Catalog Automation System ({settings.api_base_url})")


if __name__ == "__main__":
    main()
