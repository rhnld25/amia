"""Map SEC SIC codes to the 11 GICS-style sectors used for risk limits and sector ETF comparisons.

SIC is coarser than GICS, so a few industries land in the nearest sector. Unmapped codes are "Unknown",
which has its own exposure cap."""

from __future__ import annotations

import re

from ..config import (
    INDUSTRIES,
    INDUSTRY_ETFS,
    INDUSTRY_OVERRIDES,
    KNOWN_ETFS,
    SECTOR_ETFS,
)

# (first SIC, last SIC, sector); the first matching range wins, so specific ranges come first
_RANGES = [
    (1311, 1389, "Energy"),
    (2830, 2836, "Health Care"),
    (2840, 2844, "Consumer Staples"),
    (2900, 2999, "Energy"),
    (3570, 3579, "Information Technology"),
    (3630, 3639, "Consumer Discretionary"),
    (3651, 3652, "Consumer Discretionary"),
    (3711, 3716, "Consumer Discretionary"),
    (3720, 3729, "Industrials"),
    (3841, 3851, "Health Care"),
    (5120, 5122, "Health Care"),
    (5140, 5149, "Consumer Staples"),
    (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"),
    (6500, 6553, "Real Estate"),
    (6798, 6798, "Real Estate"),
    (7370, 7370, "Communication Services"),
    (7371, 7379, "Information Technology"),
    (7990, 7999, "Consumer Discretionary"),
    (100, 999, "Consumer Staples"),
    (1000, 1499, "Materials"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"),
    (2400, 2499, "Materials"),
    (2500, 2599, "Consumer Discretionary"),
    (2600, 2699, "Materials"),
    (2700, 2799, "Communication Services"),
    (2800, 2899, "Materials"),
    (3000, 3099, "Materials"),
    (3100, 3199, "Consumer Discretionary"),
    (3200, 3399, "Materials"),
    (3400, 3569, "Industrials"),
    (3580, 3599, "Industrials"),
    (3600, 3699, "Information Technology"),
    (3700, 3799, "Industrials"),
    (3800, 3899, "Information Technology"),
    (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"),
    (5000, 5199, "Industrials"),
    (5200, 5999, "Consumer Discretionary"),
    (6000, 6799, "Financials"),
    (7000, 7299, "Consumer Discretionary"),
    (7300, 7399, "Industrials"),
    (7800, 7999, "Communication Services"),
    (8000, 8099, "Health Care"),
    (8200, 8299, "Consumer Discretionary"),
    (8700, 8799, "Industrials"),
]


def sector_for_sic(sic: str | int | None) -> str:
    try:
        code = int(sic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "Unknown"
    for lo, hi, sector in _RANGES:
        if lo <= code <= hi:
            return sector
    return "Unknown"


def industry_for(symbol: str, sic: str | int | None) -> str | None:
    """The industry a company trades as, or None when no listed industry fits (it then uses its sector ETF)."""
    if symbol in INDUSTRY_OVERRIDES:
        return INDUSTRY_OVERRIDES[symbol]
    try:
        code = int(sic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for name, d in INDUSTRIES.items():
        if any(lo <= code <= hi for lo, hi in d["sic"]):
            return name
    return None


_FUND_FORMS = {"N-CEN", "N-PORT", "NPORT-P", "N-CSR", "N-CSRS", "485BPOS", "485APOS", "N-1A", "24F-2NT", "N-30D"}
_FOREIGN_FORMS = {"20-F", "40-F", "6-K", "20-F/A", "40-F/A"}
_DOMESTIC_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}


def asset_class_for(symbol: str, name: str | None, sic: str | int | None, forms: set[str] | None, has_cik: bool) -> str:
    """us_equity, adr, etf, crypto or other, from the symbol, the SEC profile and the forms the filer recently filed."""
    if "/" in symbol:
        return "crypto"  # Alpaca crypto pairs look like BTC/USD
    forms = forms or set()
    if symbol in KNOWN_ETFS or forms & _FUND_FORMS or str(sic) == "6221" or re.search(r"\b(ETF|FUND)\b", (name or "").upper()):
        return "etf"
    if not has_cik:
        return "other"
    if forms & _FOREIGN_FORMS and not forms & _DOMESTIC_FORMS:
        return "adr"
    return "us_equity"


def benchmark_for(asset_class: str, sector: str | None, industry: str | None) -> str:
    """The ETF an instrument is measured against: its industry ETF, else its sector ETF, else SPY."""
    if asset_class in ("etf", "crypto", "other"):
        return "SPY"
    if industry and industry in INDUSTRY_ETFS:
        return INDUSTRY_ETFS[industry]
    return SECTOR_ETFS.get(sector or "", "SPY")
