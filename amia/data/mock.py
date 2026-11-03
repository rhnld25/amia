"""Deterministic synthetic market for local runs and tests (AMIA_MOCK=1). Never used in production."""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from ..config import BENCHMARKS, SECTOR_ETFS

SECTORS = list(SECTOR_ETFS.keys())
BASE_DATE = date(2023, 1, 2)


def _rng(*parts: Any) -> random.Random:
    seed = int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")
    return random.Random(seed)


def trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


COMPANIES = [
    {"symbol": f"MK{i:02d}", "cik": str(9_000_000 + i).zfill(10), "name": f"Mock Corp {i:02d}", "sector": SECTORS[i % len(SECTORS)]}
    for i in range(40)
]
_BY_SYMBOL = {c["symbol"]: c for c in COMPANIES}
_BY_CIK = {c["cik"]: c for c in COMPANIES}


def _profile(symbol: str) -> dict[str, float]:
    r = _rng("profile", symbol)
    return {
        "beta": r.uniform(0.5, 1.5),
        "vol": r.uniform(0.012, 0.03),
        "pe": r.uniform(7, 30),
        "eps0": r.uniform(1, 8),
        "growth": r.uniform(-0.02, 0.12),
        "quality": r.random(),  # high = strong balance sheet, steady dividends
    }


@lru_cache(maxsize=None)
def _eps(symbol: str, year: int) -> float:
    p = _profile(symbol)
    r = _rng("eps", symbol, year)
    shock = r.gauss(0, 0.15 if p["quality"] > 0.4 else 0.5)
    v = p["eps0"] * (1 + p["growth"]) ** (year - 2010) * (1 + shock)
    if p["quality"] < 0.25 and r.random() < 0.2:
        v = -abs(v) * 0.3
    return round(v, 2)


@lru_cache(maxsize=None)
def _market_ret(d: date) -> float:
    return _rng("mkt", d).gauss(0.0004, 0.01)


@lru_cache(maxsize=None)
def _path(symbol: str) -> dict[date, tuple[float, float, float]]:
    """(open, close, ret) by day from BASE_DATE to 2030."""
    p = _profile(symbol) if symbol in _BY_SYMBOL else {"beta": 1.0, "vol": 0.004, "pe": 20, "eps0": 20}
    price = p["eps0"] * p["pe"] if symbol in _BY_SYMBOL else 100.0 + 10 * BENCHMARKS.index(symbol)
    out = {}
    for d in trading_days(BASE_DATE, date(2030, 12, 31)):
        r = _rng("ret", symbol, d)
        ret = p["beta"] * _market_ret(d) + r.gauss(0, p["vol"])
        open_ = price * (1 + r.gauss(0, p["vol"] / 3))
        price = price * (1 + ret)
        out[d] = (round(open_, 4), round(price, 4), ret)
    return out


class MockProviders:
    def universe(self, month: str) -> list[dict[str, Any]]:
        return [{**c, "index_name": "MOCK"} for c in COMPANIES]

    def daily_bars(self, symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
        s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        out = {}
        for sym in symbols:
            path = _path(sym)
            out[sym] = [
                {"date": d.isoformat(), "open": o, "close": c, "ret": r}
                for d, (o, c, r) in path.items()
                if s < d <= e
            ]
        return out

    def macro(self, series: str, start: str, end: str) -> list[tuple[str, float]]:
        base = {"DAAA": 5.5, "AAA": 5.5, "BAMLH0A0HYM2": 3.5, "T10Y2Y": 0.3, "DFF": 4.0, "DTB3": 4.0}[series]
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        out = []
        for d in trading_days(max(s, BASE_DATE - timedelta(days=400)), e):
            out.append((d.isoformat(), round(base + 0.3 * math.sin(d.toordinal() / 90), 3)))
        return out

    def recent_filings(self, cik: str, since: str) -> list[dict[str, Any]]:
        c = _BY_CIK[cik]
        s = date.fromisoformat(since)
        out = []
        for year in range(s.year - 1, s.year + 2):
            for month, form in [(3, "10-K"), (5, "10-Q"), (8, "10-Q"), (11, "10-Q")]:
                d = date(year, month, 1 + int(cik) % 20)
                out.append({"form": form, "filed": d})
        r = _rng("8k", cik, s)
        if r.random() < 0.05:
            out.append({"form": "8-K", "filed": s})
        res = []
        for f in out:
            if s <= f["filed"]:
                accn = f"{cik[-6:]}-{f['filed']:%y}-{f['filed'].timetuple().tm_yday:06d}"
                res.append(
                    {
                        "accn": accn,
                        "form": f["form"],
                        "filed": f["filed"].isoformat(),
                        "accepted": f"{f['filed'].isoformat()}T20:05:00.000Z",
                        "primary_doc": f"{c['symbol'].lower()}.htm",
                    }
                )
        return res

    def company_facts(self, cik: str) -> list[dict[str, Any]]:
        c = _BY_CIK[cik]
        sym, p = c["symbol"], _profile(c["symbol"])
        rows = []
        for year in range(2010, 2031):
            filed = date(year + 1, 3, 1 + int(cik) % 20).isoformat()
            accn = f"{cik[-6:]}-{(year + 1) % 100:02d}-K{year}"
            eps = _eps(sym, year)
            shares = 100e6
            div = round(max(0.0, eps * 0.4), 2) if p["quality"] > 0.3 else 0.0
            ca = 2e9 * (1 + p["quality"])
            cl = ca / (1.2 + 2 * p["quality"])
            for concept, value, start in [
                ("EarningsPerShareDiluted", eps, f"{year}-01-01"),
                ("Revenues", 1e9 * (1 + p["growth"]) ** (year - 2010), f"{year}-01-01"),
                ("CommonStockDividendsPerShareDeclared", div, f"{year}-01-01"),
                ("AssetsCurrent", ca, None),
                ("LiabilitiesCurrent", cl, None),
                ("LongTermDebtNoncurrent", (ca - cl) * (1.6 - p["quality"]), None),
                ("StockholdersEquity", eps * shares * 8, None),
                ("EntityCommonStockSharesOutstanding", shares, None),
            ]:
                rows.append(
                    {
                        "cik": cik,
                        "concept": concept,
                        "period_start": start,
                        "period_end": f"{year}-12-31",
                        "value": float(value),
                        "filed": filed,
                        "form": "10-K",
                        "accn": accn,
                    }
                )
        return rows

    def filing_text(self, cik: str, accn: str, primary_doc: str, form: str) -> str:
        r = _rng("text", accn)
        tone = r.choice(["record demand and expanding margins", "stable results in line with guidance", "impairment charges and weaker orders"])
        return f"{_BY_CIK[cik]['name']} {form}. Management's discussion and analysis: the quarter showed {tone}."

    def news(self, start: str, end: str) -> list[dict[str, Any]]:
        s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        out = []
        for d in trading_days(s, e):
            r = _rng("news", d)
            for k in range(r.randint(0, 3)):
                sym = r.choice(COMPANIES)["symbol"]
                tone = r.choice(["beats estimates and raises guidance", "announces CEO departure", "wins long-term supply contract", "faces product recall"])
                out.append(
                    {
                        "id": f"{d:%Y%m%d}{k}",
                        "symbols": [sym],
                        "created_at": f"{d.isoformat()}T14:30:00Z",
                        "headline": f"{sym} {tone}",
                        "summary": f"{_BY_SYMBOL[sym]['name']} {tone}.",
                        "url": "",
                    }
                )
        return out
