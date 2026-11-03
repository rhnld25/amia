"""Evaluation: rank IC, calibration, Brier score, deterministic attribution tags, learning gate."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from . import stats
from .config import EVALUATION, FRAMEWORK
from .db import DB


class ReturnPanel:
    """Daily total returns by symbol over a trading calendar (SPY's dates)."""

    def __init__(self, db: DB, since: str):
        self.calendar = [r["date"] for r in db.query("SELECT date FROM prices WHERE symbol = 'SPY' AND date >= :s ORDER BY date", {"s": since})]
        self.idx = {d: i for i, d in enumerate(self.calendar)}
        self.rets: dict[str, dict[str, float]] = defaultdict(dict)
        for r in db.query("SELECT symbol, date, ret FROM prices WHERE date >= :s", {"s": since}):
            if r["ret"] is not None:
                self.rets[r["symbol"]][r["date"]] = r["ret"]

    def forward(self, symbol: str, date: str, h: int) -> float | None:
        i = self.idx.get(date)
        if i is None or i + h >= len(self.calendar):
            return None
        series = self.rets.get(symbol, {})
        level = 1.0
        for d in self.calendar[i + 1 : i + h + 1]:
            r = series.get(d)
            if r is None:
                return None
            level *= 1 + r
        return level - 1

    def date_after(self, date: str, h: int) -> str | None:
        i = self.idx.get(date)
        if i is None or i + h >= len(self.calendar):
            return None
        return self.calendar[i + h]


def _excess_by_symbol(panel: ReturnPanel, date: str, symbols: list[str], h: int) -> dict[str, float]:
    fwd = {s: panel.forward(s, date, h) for s in symbols}
    fwd = {s: v for s, v in fwd.items() if v is not None}
    if len(fwd) < 5:
        return {}
    m = stats.mean(list(fwd.values()))
    return {s: v - m for s, v in fwd.items()}


def update_ic(db: DB, panel: ReturnPanel) -> int:
    done = {(r["date"], r["horizon"]) for r in db.query("SELECT date, horizon FROM ic")}
    score_dates = [r["date"] for r in db.query("SELECT DISTINCT date FROM scores ORDER BY date")]
    new = []
    for h in EVALUATION["horizons"]:
        for d in score_dates:
            if (d, h) in done or panel.date_after(d, h) is None:
                continue
            rows = db.query("SELECT symbol, composite FROM scores WHERE date = :d", {"d": d})
            ex = _excess_by_symbol(panel, d, [r["symbol"] for r in rows], h)
            pairs = [(r["composite"], ex[r["symbol"]]) for r in rows if r["symbol"] in ex]
            if len(pairs) < 10:
                continue
            ic = stats.spearman([a for a, _ in pairs], [b for _, b in pairs])
            if ic == ic:
                new.append({"date": d, "horizon": h, "ic": ic, "n": len(pairs)})
    db.upsert("ic", new, ["date", "horizon"])
    return len(new)


def ic_summary(db: DB) -> dict[int, dict[str, Any]]:
    out = {}
    for h in EVALUATION["horizons"]:
        vals = [r["ic"] for r in db.query("SELECT ic FROM ic WHERE horizon = :h ORDER BY date", {"h": h})]
        if len(vals) >= 2:
            m, sd = stats.mean(vals), stats.stdev(vals)
            # overlapping windows inflate t-stats; scale by sqrt(horizon) as a rough correction
            t = m / sd * (len(vals) / h) ** 0.5 if sd else float("nan")
            out[h] = {"mean_ic": m, "t_stat_overlap_adj": t, "days": len(vals)}
        else:
            out[h] = {"mean_ic": None, "t_stat_overlap_adj": None, "days": len(vals)}
    return out


def calibration(db: DB, panel: ReturnPanel) -> tuple[list[tuple[float, float]] | None, int]:
    """Isotonic map from composite score to P(beat equal-weight universe over the decision horizon).

    Uses one score date per week to limit overlap. Returns (steps or None, resolved observations)."""
    h = EVALUATION["decision_horizon"]
    dates = [r["date"] for r in db.query("SELECT DISTINCT date FROM scores ORDER BY date")]
    weekly = dates[::5]
    xs, ys = [], []
    for d in weekly:
        if panel.date_after(d, h) is None:
            continue
        rows = db.query("SELECT symbol, composite FROM scores WHERE date = :d", {"d": d})
        ex = _excess_by_symbol(panel, d, [r["symbol"] for r in rows], h)
        for r in rows:
            if r["symbol"] in ex:
                xs.append(r["composite"])
                ys.append(1 if ex[r["symbol"]] > 0 else 0)
    if len(xs) < EVALUATION["min_resolved_for_calibration"]:
        return None, len(xs)
    return stats.isotonic_fit(xs, ys), len(xs)


def decision_brier(db: DB, panel: ReturnPanel) -> dict[str, Any]:
    h = EVALUATION["decision_horizon"]
    probs, outcomes = [], []
    for d in db.query("SELECT date, symbol, confidence FROM decisions WHERE action = 'ACCUMULATE'"):
        universe = [r["symbol"] for r in db.query("SELECT symbol FROM scores WHERE date = :d", {"d": d["date"]})]
        ex = _excess_by_symbol(panel, d["date"], universe, h)
        if d["symbol"] in ex:
            probs.append(d["confidence"])
            outcomes.append(1 if ex[d["symbol"]] > 0 else 0)
    if not probs:
        return {"resolved": 0}
    base = stats.mean(outcomes)
    return {
        "resolved": len(probs),
        "brier": stats.brier(probs, outcomes),
        "brier_base_rate": stats.brier([base] * len(outcomes), outcomes),
        "hit_rate": base,
    }


def tag_closed_positions(db: DB, panel: ReturnPanel, sector_etf: dict[str, str]) -> int:
    """Deterministic attribution tags for closed positions that have none yet."""
    n = 0
    for p in db.query("SELECT * FROM closed_positions WHERE tags IS NULL"):
        tags = []
        days = [d for d in panel.calendar if p["opened_date"] < d <= p["closed_date"]]
        uni = [r["symbol"] for r in db.query("SELECT symbol FROM scores WHERE date = :d", {"d": p["closed_date"]})]
        uni_ret = []
        for d in days:
            vals = [panel.rets[s][d] for s in uni if d in panel.rets.get(s, {})]
            if vals:
                uni_ret.append(stats.mean(vals))
        level = 1.0
        for r in uni_ret:
            level *= 1 + r
        excess = p["ret"] - (level - 1)
        etf = sector_etf.get(p["sector"] or "")
        if etf:
            sec_level = 1.0
            for d in days:
                sec_level *= 1 + panel.rets.get(etf, {}).get(d, 0.0)
            sec_ret = sec_level - 1
            if excess < 0 and sec_ret - (level - 1) < 0 and abs(sec_ret - (level - 1)) >= abs(excess) * 0.5:
                tags.append("sector_drag")
        open_score = db.query("SELECT mos_low, criteria_passed FROM scores WHERE symbol = :s AND date = :d", {"s": p["symbol"], "d": p["opened_date"]})
        close_score = db.query(
            "SELECT mos_low, criteria_passed FROM scores WHERE symbol = :s AND date <= :d ORDER BY date DESC LIMIT 1",
            {"s": p["symbol"], "d": p["closed_date"]},
        )
        if close_score and close_score[0]["mos_low"] is not None and close_score[0]["mos_low"] > 0:
            tags.append("valuation_held")
        if open_score and close_score and (close_score[0]["criteria_passed"] or 0) < (open_score[0]["criteria_passed"] or 0) - 1:
            tags.append("fundamental_break")
        texts = db.query(
            "SELECT AVG(sentiment) AS s FROM text_scores WHERE score_pass = 'primary' AND symbols LIKE :sym AND published_at >= :a AND published_at <= :b",
            {"sym": f"%|{p['symbol']}|%", "a": p["opened_date"], "b": p["closed_date"]},
        )
        avg_sent = texts[0]["s"] if texts else None
        if avg_sent is not None and ((avg_sent > 2.5 and excess < 0) or (avg_sent < 1.5 and excess > 0)):
            tags.append("text_signal_wrong")
        if not tags:
            tags.append("unexplained")
        db.execute(
            "UPDATE closed_positions SET tags = :t, excess_ret = :e WHERE id = :i",
            {"t": json.dumps(tags), "e": excess, "i": p["id"]},
        )
        n += 1
    return n


def learning_gate(db: DB) -> dict[str, Any]:
    h = EVALUATION["decision_horizon"]
    resolved_days = db.scalar("SELECT COUNT(*) FROM ic WHERE horizon = :h", {"h": h}) or 0
    need = EVALUATION["learning_unlock_resolved_days"]
    return {
        "unlocked": resolved_days >= need,
        "resolved_days": resolved_days,
        "required_days": need,
        "framework": FRAMEWORK,
        "note": (
            "Parameter updates stay off until enough 126-day predictions have resolved; proposals then need 30+ cases, "
            "a chronological holdout improvement, and a one-month shadow run. Automated proposal generation is not built "
            "yet (Phase 5): once unlocked, changes are made by hand as a new framework version."
        ),
    }


def consistency_rate(db: DB) -> dict[str, Any]:
    rows = db.query(
        "SELECT a.novelty AS n1, a.sentiment AS s1, a.materiality AS m1, b.novelty AS n2, b.sentiment AS s2, b.materiality AS m2 "
        "FROM text_scores a JOIN text_scores b ON a.item_id = b.item_id AND a.score_pass = 'primary' AND b.score_pass = 'recheck'"
    )
    if not rows:
        return {"pairs": 0, "agreement": None}
    agree = sum(1 for r in rows if abs(r["n1"] - r["n2"]) <= 1 and abs(r["s1"] - r["s2"]) <= 1 and abs(r["m1"] - r["m2"]) <= 1)
    return {"pairs": len(rows), "agreement": agree / len(rows)}


def grounding_failure_rate(db: DB) -> dict[str, Any]:
    rows = db.query("SELECT thesis_ok FROM decisions WHERE action IN ('ACCUMULATE', 'EXIT')")
    if not rows:
        return {"decisions": 0, "failure_rate": None}
    return {"decisions": len(rows), "failure_rate": sum(1 for r in rows if not r["thesis_ok"]) / len(rows)}
