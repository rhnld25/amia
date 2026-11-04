"""Buffett-style business quality from point-in-time EDGAR facts: returns on equity, debt, margins,
reinvestment and owner earnings. Deterministic; an input to the decision, not a gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .. import stats
from ..config import FRAMEWORK
from .graham import LTD, REVENUE, SHARES, annual, latest

NET_INCOME = ["NetIncomeLoss"]
CFO = ["NetCashProvidedByUsedInOperatingActivities"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment"]
EQUITY = ["StockholdersEquity"]

CRITERIA = ["consistent_roe", "low_debt", "durable_margins", "equity_growth", "owner_earnings_yield"]


@dataclass
class BuffettResult:
    roe_avg: float | None = None
    roe_min: float | None = None
    net_margin_avg: float | None = None
    net_margin_cv: float | None = None
    debt_payoff_years: float | None = None
    equity_cagr: float | None = None
    owner_earnings: float | None = None
    owner_earnings_yield: float | None = None
    market_cap: float | None = None
    treasury_10y: float | None = None
    criteria: dict[str, bool] = field(default_factory=dict)

    @property
    def criteria_passed(self) -> int:
        return sum(1 for v in self.criteria.values() if v)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["criteria_passed"] = self.criteria_passed
        d["criteria_total"] = len(CRITERIA)
        return d


def evaluate(pit: dict[str, dict[str, float]], price: float, treasury_10y: float | None) -> BuffettResult:
    f = FRAMEWORK
    n = f["buffett_years"]
    res = BuffettResult(treasury_10y=treasury_10y)
    ni = annual(pit, NET_INCOME)
    rev = dict((end[:7], v) for end, v in annual(pit, REVENUE))
    eq = dict((end[:7], v) for end, v in annual(pit, EQUITY))

    roes = [v / eq[end[:7]] for end, v in ni[-n:] if eq.get(end[:7], 0) > 0]
    if len(roes) >= n:
        res.roe_avg, res.roe_min = stats.mean(roes), min(roes)
    margins = [v / rev[end[:7]] for end, v in ni[-n:] if rev.get(end[:7], 0) > 0]
    if len(margins) >= n:
        res.net_margin_avg = stats.mean(margins)
        if res.net_margin_avg > 0:
            res.net_margin_cv = stats.stdev(margins) / res.net_margin_avg

    ltd = latest(pit, LTD) or 0.0
    if ni and ni[-1][1] > 0:
        res.debt_payoff_years = ltd / ni[-1][1]

    eq_series = [v for _, v in annual(pit, EQUITY) if v > 0]
    eq_annual = [eq[end[:7]] for end, _ in ni if eq.get(end[:7], 0) > 0]  # fiscal-year-end equity only
    series = eq_annual if len(eq_annual) > n else eq_series
    if len(series) > n and series[-1 - n] > 0:
        res.equity_cagr = (series[-1] / series[-1 - n]) ** (1 / n) - 1

    cfo, capex = annual(pit, CFO), dict(annual(pit, CAPEX))
    shares = latest(pit, SHARES)
    if cfo:
        end, v = cfo[-1]
        res.owner_earnings = v - (capex.get(end) or 0.0)
    if shares and price > 0:
        res.market_cap = shares * price
        if res.owner_earnings is not None:
            res.owner_earnings_yield = res.owner_earnings / res.market_cap

    c = res.criteria
    c["consistent_roe"] = res.roe_avg is not None and res.roe_avg >= f["min_roe_avg"] and res.roe_min >= f["min_roe_year"]
    c["low_debt"] = res.debt_payoff_years is not None and res.debt_payoff_years <= f["max_debt_payoff_years"]
    c["durable_margins"] = (
        len(margins) >= n and min(margins) > 0 and res.net_margin_cv is not None and res.net_margin_cv <= f["max_margin_cv"]
    )
    c["equity_growth"] = res.equity_cagr is not None and res.equity_cagr >= f["min_equity_cagr"]
    c["owner_earnings_yield"] = (
        res.owner_earnings_yield is not None and treasury_10y is not None and res.owner_earnings_yield >= treasury_10y / 100
    )
    return res
