"""Marks-style cycle awareness: where the market and each stock sit on the fear/greed pendulum.

Market temperature comes from credit spreads, the yield curve, policy rates and SPY's trend.
A stock's pendulum reading comes from its 52-week position, momentum, drawdown and news tone.
Deterministic; an input to the decision, not a gate."""

from __future__ import annotations

from typing import Any

from ..config import FRAMEWORK, RISK


def _closes_from_returns(rets: list[float]) -> list[float]:
    level, out = 1.0, []
    for r in rets:
        level *= 1 + r
        out.append(level)
    return out


def price_stats(rets: list[float]) -> dict[str, float | None]:
    """From daily total returns (oldest first): 52-week position and drawdown, 12-1 momentum, 3-month return."""
    out: dict[str, float | None] = {"pos_52w": None, "drawdown_52w": None, "mom_12_1": None, "ret_3m": None, "ret_12m": None}
    if len(rets) < 60:
        return out
    year = _closes_from_returns(rets[-252:])
    hi, lo, last = max(year), min(year), year[-1]
    out["pos_52w"] = (last - lo) / (hi - lo) if hi > lo else 0.5
    out["drawdown_52w"] = last / hi - 1
    out["ret_3m"] = _closes_from_returns(rets[-63:])[-1] - 1
    if len(rets) >= 252:
        out["ret_12m"] = year[-1] - 1
        out["mom_12_1"] = _closes_from_returns(rets[-252:-21])[-1] - 1
    return out


def market_cycle(macro: dict[str, list[tuple[str, float]]], spy_rets: list[float]) -> dict[str, Any]:
    hy = [v for _, v in macro.get("BAMLH0A0HYM2") or []]
    curve = macro.get("T10Y2Y") or []
    ff = macro.get("DFF") or []
    n = RISK["regime_lookback_obs"]
    spy = price_stats(spy_rets)
    out: dict[str, Any] = {
        "hy_spread": hy[-1] if hy else None,
        "hy_percentile": (sum(1 for v in hy if v <= hy[-1]) / len(hy)) if len(hy) >= 250 else None,
        "curve_2s10s": curve[-1][1] if curve else None,
        "fed_funds_change_3m": (ff[-1][1] - ff[-1 - n][1]) if len(ff) > n else None,
        "spy_ret_12m": spy["ret_12m"],
        "spy_drawdown_52w": spy["drawdown_52w"],
    }
    score, signals = 0, []
    hy_now, tight, wide = out["hy_spread"], FRAMEWORK["hy_tight_level"], FRAMEWORK["hy_wide_level"]
    if hy_now is not None:
        # the long-run level sets the reading; the ~3-year percentile is context only
        out["hy_long_run_band"] = "tight" if hy_now <= tight else "wide" if hy_now >= wide else "normal"
        recent = f"; {out['hy_percentile']:.0%} percentile of the last ~3 years" if out["hy_percentile"] is not None else ""
        if hy_now <= tight:
            score += 1
            signals.append(f"credit spreads tight by long-run standards: {hy_now:.2f} is at or below {tight}{recent} (complacency)")
        elif hy_now >= wide:
            score -= 1
            signals.append(f"credit spreads wide by long-run standards: {hy_now:.2f} is at or above {wide}{recent} (fear)")
        elif out["hy_percentile"] is not None and (out["hy_percentile"] >= 0.8 or out["hy_percentile"] <= 0.2):
            direction = "widened" if out["hy_percentile"] >= 0.8 else "narrowed"
            signals.append(f"credit spreads {direction} recently ({hy_now:.2f}{recent}) but remain in the long-run normal range")
    if spy["ret_12m"] is not None and spy["ret_12m"] >= 0.2:
        score += 1
        signals.append(f"SPY up {spy['ret_12m']:.0%} over 12 months (optimism)")
    if spy["drawdown_52w"] is not None and spy["drawdown_52w"] <= -0.15:
        score -= 1
        signals.append(f"SPY {spy['drawdown_52w']:.0%} below its 52-week high (pessimism)")
    if out["curve_2s10s"] is not None and out["curve_2s10s"] < 0:
        signals.append(f"yield curve inverted at {out['curve_2s10s']:.2f}")
    out["temperature_score"] = score
    out["temperature"] = "hot" if score >= 1 else "cold" if score <= -1 else "neutral"
    out["signals"] = signals
    return out


def stock_pendulum(ps: dict[str, float | None], avg_sentiment: float | None, attention_top_quartile: bool) -> dict[str, Any]:
    f = FRAMEWORK
    score, signals = 0, []
    if ps["pos_52w"] is not None and ps["pos_52w"] >= f["pendulum_high_pos"]:
        score += 1
        signals.append(f"price near its 52-week high ({ps['pos_52w']:.0%} of the range)")
    if ps["mom_12_1"] is not None and ps["mom_12_1"] >= f["pendulum_hot_momentum"]:
        score += 1
        signals.append(f"12-1 month momentum {ps['mom_12_1']:.0%}")
    if avg_sentiment is not None and avg_sentiment >= f["pendulum_hot_sentiment"] and attention_top_quartile:
        score += 1
        signals.append(f"heavy, upbeat coverage (average sentiment {avg_sentiment:.1f} of 4)")
    if ps["drawdown_52w"] is not None and ps["drawdown_52w"] <= f["pendulum_deep_drawdown"]:
        score -= 1
        signals.append(f"{ps['drawdown_52w']:.0%} below its 52-week high")
    if avg_sentiment is not None and avg_sentiment <= f["pendulum_cold_sentiment"]:
        score -= 1
        signals.append(f"coverage strongly negative (average sentiment {avg_sentiment:.1f} of 4)")
    score = max(-2, min(2, score))
    return {"score": score, "label": "greed" if score >= 1 else "fear" if score <= -1 else "neutral", "signals": signals}
