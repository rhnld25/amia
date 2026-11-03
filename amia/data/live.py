"""Live providers: SEC EDGAR, FRED, Alpaca (prices and news), Wikipedia (index membership)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from .edgar import Edgar
from .http import HttpClient
from .universe import fetch_universe

ALPACA_DATA = "https://data.alpaca.markets"


def _safe_end() -> str:
    # The free plan serves full-market (SIP) data only when it is more than 15 minutes old.
    return (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiveProviders:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.edgar = Edgar(settings.sec_contact_email)
        self.alpaca = HttpClient(
            headers={"APCA-API-KEY-ID": settings.alpaca_key_id, "APCA-API-SECRET-KEY": settings.alpaca_secret},
            min_interval=0.35,  # free plan: 200 requests per minute
        )
        self.fred = HttpClient(min_interval=0.6)
        self._ticker_map: dict[str, dict[str, Any]] | None = None

    def universe(self, month: str) -> list[dict[str, Any]]:
        if self._ticker_map is None:
            self._ticker_map = self.edgar.ticker_map()
        return fetch_universe(self.settings.sec_contact_email, self._ticker_map)

    def _bars(self, symbols: list[str], start: str, adjustment: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(symbols), 100):
            params: dict[str, Any] = {
                "symbols": ",".join(symbols[i : i + 100]),
                "timeframe": "1Day",
                "start": start,
                "end": _safe_end(),
                "adjustment": adjustment,
                "feed": "sip",
                "limit": 10000,
            }
            while True:
                data = self.alpaca.json(f"{ALPACA_DATA}/v2/stocks/bars", params)
                for sym, bars in (data.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bars)
                token = data.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        return out

    def daily_bars(self, symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
        """Raw open/close plus total-return `ret` from dividend- and split-adjusted closes."""
        adjusted = self._bars(symbols, start, "all")
        raw = self._bars(symbols, start, "raw")
        out: dict[str, list[dict[str, Any]]] = {}
        for sym, bars in adjusted.items():
            raw_by_day = {b["t"][:10]: b for b in raw.get(sym, [])}
            rows, prev = [], None
            for b in sorted(bars, key=lambda b: b["t"]):
                day = b["t"][:10]
                if day > end:
                    break
                r = raw_by_day.get(day)
                if prev is not None and r is not None and prev > 0:
                    rows.append({"date": day, "open": r["o"], "close": r["c"], "ret": b["c"] / prev - 1})
                prev = b["c"]
            out[sym] = rows
        return out

    def macro(self, series: str, start: str, end: str) -> list[tuple[str, float]]:
        data = self.fred.json(
            "https://api.stlouisfed.org/fred/series/observations",
            {
                "series_id": series,
                "api_key": self.settings.fred_api_key,
                "file_type": "json",
                "observation_start": start,
                "observation_end": end,
            },
        )
        return [(o["date"], float(o["value"])) for o in data.get("observations", []) if o["value"] not in (".", "")]

    def recent_filings(self, cik: str, since: str) -> list[dict[str, Any]]:
        return self.edgar.recent_filings(cik, since)

    def company_facts(self, cik: str) -> list[dict[str, Any]]:
        return self.edgar.company_facts(cik)

    def filing_text(self, cik: str, accn: str, primary_doc: str, form: str) -> str:
        return self.edgar.filing_text(cik, accn, primary_doc, form)

    def news(self, start: str, end: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"start": start, "end": end, "limit": 50, "sort": "asc", "include_content": "false"}
        out = []
        while True:
            data = self.alpaca.json(f"{ALPACA_DATA}/v1beta1/news", params)
            for n in data.get("news", []):
                out.append(
                    {
                        "id": str(n["id"]),
                        "symbols": n.get("symbols", []),
                        "created_at": n["created_at"],
                        "headline": n.get("headline", ""),
                        "summary": n.get("summary", ""),
                        "url": n.get("url", ""),
                    }
                )
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out
