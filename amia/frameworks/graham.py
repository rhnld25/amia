"""Graham defensive screen and intrinsic value, computed only from point-in-time EDGAR facts.

Every input is deterministic: EPS and balance-sheet items from filings known at `as_of`,
g from a capped 5-year EPS CAGR, Y from the AAA corporate yield. No estimates, no LLM.
The result is one input to the decision, not a gate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import FRAMEWORK

EPS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
DIV_PS = ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"]
DIV_PAID = ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]
LTD = ["LongTermDebtNoncurrent", "LongTermDebt"]
SHARES = ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"]

CRITERIA = ["size", "current_ratio", "debt_vs_nca", "earnings_stability", "dividend_record", "earnings_growth", "pe_3y", "pe_x_pb"]


def point_in_time(rows: list[dict[str, Any]], as_of: str) -> dict[str, dict[str, float]]:
    """{concept: {period_end: value}} using, for each period, the latest filing made on or before as_of."""
    best: dict[tuple[str, str], tuple[str, float]] = {}
    for r in rows:
        if r["filed"] > as_of:
            continue
        key = (r["concept"], r["period_end"])
        if key not in best or r["filed"] >= best[key][0]:
            best[key] = (r["filed"], r["value"])
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (concept, end), (_, value) in best.items():
        out[concept][end] = value
    return out


def annual(pit: dict[str, dict[str, float]], concepts: list[str]) -> list[tuple[str, float]]:
    """Fiscal-year series (oldest first), taking the first concept with data for each period."""
    periods: dict[str, float] = {}
    for c in reversed(concepts):  # earlier concepts in the list win
        periods.update(pit.get(c, {}))
    # collapse to one value per fiscal year end (YYYY-MM), keeping the latest period end in that month
    by_year: dict[str, tuple[str, float]] = {}
    for end, v in periods.items():
        k = end[:7]
        if k not in by_year or end > by_year[k][0]:
            by_year[k] = (end, v)
    return sorted(by_year.values())


def latest(pit: dict[str, dict[str, float]], concepts: list[str]) -> float | None:
    for c in concepts:
        vals = pit.get(c)
        if vals:
            return vals[max(vals)]
    return None


@dataclass
class GrahamResult:
    eps: float | None = None
    eps_3y_avg: float | None = None
    g_pct: float = 0.0
    aaa_yield: float | None = None
    iv_mid: float | None = None
    iv_low: float | None = None
    iv_high: float | None = None
    mos_band_low: float | None = None
    current_ratio: float | None = None
    pe_3y: float | None = None
    pb: float | None = None
    revenue: float | None = None
    years_positive_eps: int = 0
    years_dividends: int = 0
    criteria: dict[str, bool] = field(default_factory=dict)

    @property
    def criteria_passed(self) -> int:
        return sum(1 for v in self.criteria.values() if v)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["criteria_passed"] = self.criteria_passed
        d["criteria_total"] = len(CRITERIA)
        return d


def graham_value(eps: float, g_pct: float, y: float) -> float:
    return eps * (8.5 + 2 * g_pct) * 4.4 / y


def evaluate(pit: dict[str, dict[str, float]], price: float, aaa_yield: float) -> GrahamResult:
    f = FRAMEWORK
    res = GrahamResult(aaa_yield=aaa_yield)
    eps_series = annual(pit, EPS)
    eps_vals = [v for _, v in eps_series]
    rev = annual(pit, REVENUE)
    res.revenue = rev[-1][1] if rev else None

    ca, cl = latest(pit, ["AssetsCurrent"]), latest(pit, ["LiabilitiesCurrent"])
    ltd = latest(pit, LTD)
    if ltd is None and ca is not None:
        ltd = 0.0  # no long-term debt tagged at all
    equity, shares = latest(pit, ["StockholdersEquity"]), latest(pit, SHARES)

    n = f["earnings_years"]
    last_n = eps_vals[-n:]
    res.years_positive_eps = 0
    for v in reversed(eps_vals):
        if v <= 0:
            break
        res.years_positive_eps += 1

    div_ps = dict(annual(pit, DIV_PS))
    div_paid = dict(annual(pit, DIV_PAID))
    years = [end for end, _ in eps_series][-f["dividend_years"]:]
    for end in reversed(years):
        if (div_ps.get(end) or 0) > 0 or (div_paid.get(end) or 0) > 0:
            res.years_dividends += 1
        else:
            break

    if eps_vals:
        res.eps = eps_vals[-1]
        res.eps_3y_avg = sum(eps_vals[-3:]) / len(eps_vals[-3:])

    gy = f["g_years"]
    if len(eps_vals) > gy and eps_vals[-1 - gy] > 0 and eps_vals[-1] > 0:
        g = ((eps_vals[-1] / eps_vals[-1 - gy]) ** (1 / gy) - 1) * 100
        res.g_pct = max(0.0, min(f["g_cap_pct"], g))

    if ca is not None and cl:
        res.current_ratio = ca / cl
    if res.eps_3y_avg and res.eps_3y_avg > 0 and price > 0:
        res.pe_3y = price / res.eps_3y_avg
    if equity and shares and equity > 0:
        res.pb = price / (equity / shares)

    c = res.criteria
    c["size"] = res.revenue is not None and res.revenue >= f["min_revenue_usd"]
    c["current_ratio"] = res.current_ratio is not None and res.current_ratio >= f["min_current_ratio"]
    c["debt_vs_nca"] = ca is not None and cl is not None and ltd is not None and ltd <= ca - cl
    c["earnings_stability"] = len(last_n) >= n and all(v > 0 for v in last_n)
    c["dividend_record"] = res.years_dividends >= f["dividend_years"]
    c["earnings_growth"] = False
    if len(eps_vals) >= n:
        window = eps_vals[-n:]
        start_avg, end_avg = sum(window[:3]) / 3, sum(window[-3:]) / 3
        c["earnings_growth"] = start_avg > 0 and end_avg >= start_avg * (1 + f["min_eps_growth_10y"])
    c["pe_3y"] = res.pe_3y is not None and res.pe_3y <= f["max_pe_3y"]
    c["pe_x_pb"] = res.pe_3y is not None and res.pb is not None and res.pe_3y * res.pb <= f["max_pe_x_pb"]

    if res.eps and res.eps > 0 and aaa_yield and aaa_yield > 0:
        res.iv_mid = graham_value(res.eps, res.g_pct, aaa_yield)
        band = [
            graham_value(res.eps, max(0.0, res.g_pct + dg), max(0.5, aaa_yield + dy))
            for dg in (-f["g_band_pp"], f["g_band_pp"])
            for dy in (-f["y_band_pct"], f["y_band_pct"])
        ]
        res.iv_low, res.iv_high = min(band), max(band)
        res.mos_band_low = 1 - price / res.iv_low
    return res
