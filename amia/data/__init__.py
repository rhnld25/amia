"""Data providers. `get_providers` returns live clients, or synthetic ones when AMIA_MOCK=1."""

from __future__ import annotations

from typing import Any, Protocol

from ..config import Settings


class Providers(Protocol):
    def ticker_map(self) -> dict[str, dict[str, Any]]: ...
    def company_profile(self, cik: str) -> dict[str, Any]: ...
    def daily_bars(self, symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]: ...
    def macro(self, series: str, start: str, end: str) -> list[tuple[str, float]]: ...
    def recent_filings(self, cik: str, since: str) -> list[dict[str, Any]]: ...
    def company_facts(self, cik: str) -> list[dict[str, Any]]: ...
    def filing_text(self, cik: str, accn: str, primary_doc: str, form: str) -> str: ...
    def news(self, start: str, end: str) -> list[dict[str, Any]]: ...


def get_providers(settings: Settings) -> Providers:
    if settings.mock:
        from .mock import MockProviders

        return MockProviders()
    settings.require_live()
    from .live import LiveProviders

    return LiveProviders(settings)
