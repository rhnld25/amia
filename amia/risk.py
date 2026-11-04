"""Component 3: macro & risk overlay. Deterministic hard limits, and the confidence the reasoning model
must reach before a new position, raised in crowded sectors and stressed regimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import stats
from .config import DECISION, RISK


def regime_flags(macro: dict[str, list[tuple[str, float]]]) -> list[str]:
    """Each flag raises the required confidence for new positions by one step."""
    flags = []
    curve = macro.get("T10Y2Y") or []
    if curve and curve[-1][1] < 0:
        flags.append(f"yield curve inverted (2s10s {curve[-1][1]:.2f})")
    n = RISK["regime_lookback_obs"]
    hy = macro.get("BAMLH0A0HYM2") or []
    if len(hy) > n and hy[-1][1] - hy[-1 - n][1] > RISK["regime_hy_widening"]:
        flags.append(f"high-yield spreads widened {hy[-1][1] - hy[-1 - n][1]:.2f}pp in 3 months")
    ff = macro.get("DFF") or []
    if len(ff) > n and abs(ff[-1][1] - ff[-1 - n][1]) > RISK["regime_ff_change"]:
        flags.append(f"fed funds moved {ff[-1][1] - ff[-1 - n][1]:+.2f}pp in 3 months")
    return flags


def beta(returns: dict[str, float], market: dict[str, float]) -> float | None:
    days = sorted(set(returns) & set(market))[-RISK["beta_window"]:]
    if len(days) < 60:
        return None
    _, b, _, _ = stats.ols([market[d] for d in days], [returns[d] for d in days])
    return b


def avg_pairwise_corr(symbols: list[str], rets: dict[str, dict[str, float]]) -> float | None:
    pairs = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            days = sorted(set(rets.get(a, {})) & set(rets.get(b, {})))[-RISK["corr_window"]:]
            if len(days) >= 20:
                c = stats.pearson([rets[a][d] for d in days], [rets[b][d] for d in days])
                if c == c:  # not NaN
                    pairs.append(c)
    return stats.mean(pairs) if pairs else None


def annual_vol(returns: dict[str, float], window: int = 60) -> float | None:
    vals = [returns[d] for d in sorted(returns)[-window:]]
    if len(vals) < 20:
        return None
    return stats.stdev(vals) * (252**0.5)


@dataclass
class PortfolioState:
    nav: float
    weights: dict[str, float]  # symbol -> weight of NAV
    sectors: dict[str, str]  # symbol -> sector
    betas: dict[str, float] = field(default_factory=dict)

    def sector_weight(self, sector: str) -> float:
        return sum(w for s, w in self.weights.items() if self.sectors.get(s) == sector)

    def beta(self) -> float:
        return sum(w * self.betas.get(s, 1.0) for s, w in self.weights.items())


def required_confidence(sector: str, state: PortfolioState, regime: list[str]) -> tuple[float, list[str]]:
    reasons = list(regime)
    steps = len(regime)
    if state.sector_weight(sector) > RISK["crowded_sector_weight"]:
        steps += 1
        reasons.append(f"{sector} weight {state.sector_weight(sector):.0%} above crowding level")
    return round(DECISION["base_confidence"] + steps * DECISION["confidence_step"], 3), reasons


def check_buy(
    symbol: str, sector: str, weight: float, sym_beta: float | None, state: PortfolioState, rets: dict[str, dict[str, float]]
) -> tuple[bool, dict[str, Any]]:
    """Hard limits. Returns (approved, details)."""
    d: dict[str, Any] = {}
    d["position_weight"] = weight
    d["sector_weight_after"] = round(state.sector_weight(sector) + weight, 4)
    d["beta_after"] = round(state.beta() + weight * (sym_beta if sym_beta is not None else 1.0), 3)
    held = list(state.weights)
    corr = avg_pairwise_corr(held + [symbol], rets) if len(held) >= 2 else None
    d["avg_corr_after"] = round(corr, 3) if corr is not None else None
    failures = []
    if weight > DECISION["max_position_weight"]:
        failures.append("position limit")
    if d["sector_weight_after"] > RISK["max_sector_weight"]:
        failures.append("sector limit")
    if sector == "Unknown" and d["sector_weight_after"] > RISK["max_unknown_sector_weight"]:
        failures.append("unknown-sector limit")
    if d["beta_after"] > RISK["max_portfolio_beta"]:
        failures.append("beta limit")
    if corr is not None and corr > RISK["max_avg_pairwise_corr"]:
        failures.append("correlation limit")
    d["failures"] = failures
    return not failures, d
