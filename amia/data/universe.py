"""S&P Composite 1500 membership from Wikipedia's S&P 500/400/600 constituent tables.

Fetched through the MediaWiki API with a descriptive User-Agent, as Wikipedia asks of bots.
CIKs come from the SEC ticker map, which is authoritative.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .http import HttpClient

PAGES = {
    "SP500": "List_of_S&P_500_companies",
    "SP400": "List_of_S&P_400_companies",
    "SP600": "List_of_S&P_600_companies",
}


class _TableParser(HTMLParser):
    """Collects rows of the table with id="constituents"."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0  # nesting depth inside the target table
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            if self.depth or dict(attrs).get("id") == "constituents":
                self.depth += 1
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.depth:
            self.depth -= 1
            return
        if self.depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_constituents(html: str) -> list[dict[str, str]]:
    p = _TableParser()
    p.feed(html)
    if not p.rows:
        return []
    header = [h.lower() for h in p.rows[0]]

    def col(*names: str) -> int | None:
        for i, h in enumerate(header):
            if any(n in h for n in names):
                return i
        return None

    i_sym, i_name, i_sector = col("symbol", "ticker"), col("security", "company"), col("gics sector", "sector")
    if i_sym is None or i_sector is None:
        raise ValueError(f"Unexpected constituent table header: {p.rows[0]}")
    out = []
    for row in p.rows[1:]:
        if len(row) <= max(i_sym, i_sector):
            continue
        out.append({"symbol": row[i_sym], "name": row[i_name] if i_name is not None else "", "sector": row[i_sector]})
    return out


def normalize(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ticker.upper())


def fetch_universe(contact_email: str, sec_ticker_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    http = HttpClient(headers={"User-Agent": f"AMIA-research/0.1 ({contact_email})"}, min_interval=1.0)
    members: dict[str, dict[str, Any]] = {}
    for index_name, page in PAGES.items():
        data = http.json(
            "https://en.wikipedia.org/w/api.php",
            {"action": "parse", "page": page, "prop": "text", "format": "json", "formatversion": "2"},
        )
        rows = parse_constituents(data["parse"]["text"])
        if len(rows) < 300:
            raise ValueError(f"{index_name}: only {len(rows)} constituents parsed; page layout may have changed")
        for r in rows:
            sec = sec_ticker_map.get(normalize(r["symbol"]))
            if not sec:
                continue
            symbol = sec["ticker"].replace("-", ".")  # Alpaca uses BRK.B
            members.setdefault(
                symbol,
                {"symbol": symbol, "cik": sec["cik"], "name": r["name"] or sec["title"], "sector": r["sector"], "index_name": index_name},
            )
    return list(members.values())
