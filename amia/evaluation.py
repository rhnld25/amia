"""Evaluation: per-lens rank IC against SPY, Brier score of the model's confidence, attribution tags."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from . import stats
from .config import EVALUATION, LENSES
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

    def between(self, symbol: str, start: str, end: str) -> float:
        """Compounded return over trading days after `start` up to and including `end` (missing days count as 0)."""
        level = 1.0
        for d in self.calendar:
            if start < d <= end:
                level *= 1 + self.rets.get(symbol, {}).get(d, 0.0)
        return level - 1

    def date_after(self, date: str, h: int) -> str | None:
        i = self.idx.get(date)
        if i is None or i + h >= len(self.calendar):
            return None
        return self.calendar[i + h]

    def excess_vs_spy(self, symbol: str, date: str, h: int) -> float | None:
        a, m = self.forward(symbol, date, h), self.forward("SPY", date, h)
        return None if a is None or m is None else a - m


def update_ic(db: DB, panel: ReturnPanel) -> int:
    """Rank IC per lens per day: does the lens order the watchlist by later return against SPY?"""
    done = {(r["date"], r["horizon"]) for r in db.query("SELECT DISTINCT date, horizon FROM ic")}
    dates = [r["date"] for r in db.query("SELECT DISTINCT date FROM features ORDER BY date")]
    new = []
    for h in EVALUATION["horizons"]:
        for d in dates:
            if (d, h) in done or panel.date_after(d, h) is None:
                continue
            rows = db.query("SELECT * FROM features WHERE date = :d", {"d": d})
            ex = {r["symbol"]: panel.excess_vs_spy(r["symbol"], d, h) for r in rows}
            for lens in LENSES:
                pairs = [(r[lens], ex[r["symbol"]]) for r in rows if r[lens] is not None and ex.get(r["symbol"]) is not None]
                if len(pairs) < EVALUATION["min_names_for_ic"]:
                    continue
                ic = stats.spearman([a for a, _ in pairs], [b for _, b in pairs])
                if ic == ic:
                    new.append({"date": d, "horizon": h, "lens": lens, "ic": ic, "n": len(pairs)})
    db.upsert("ic", new, ["date", "horizon", "lens"])
    return len(new)


def ic_summary(db: DB) -> dict[str, dict[int, dict[str, Any]]]:
    out: dict[str, dict[int, dict[str, Any]]] = {}
    for lens in LENSES:
        out[lens] = {}
        for h in EVALUATION["horizons"]:
            vals = [r["ic"] for r in db.query("SELECT ic FROM ic WHERE horizon = :h AND lens = :l ORDER BY date", {"h": h, "l": lens})]
            if len(vals) >= 2:
                m, sd = stats.mean(vals), stats.stdev(vals)
                # overlapping windows inflate t-stats; scale by sqrt(horizon) as a rough correction
                t = m / sd * (len(vals) / h) ** 0.5 if sd else float("nan")
                out[lens][h] = {"mean_ic": m, "t_stat_overlap_adj": t, "days": len(vals)}
            else:
                out[lens][h] = {"mean_ic": None, "t_stat_overlap_adj": None, "days": len(vals)}
    return out


def decision_brier(db: DB, panel: ReturnPanel) -> dict[str, Any]:
    """Is the model's confidence (P(beat SPY over the decision horizon)) calibrated? Uses every review."""
    h = EVALUATION["decision_horizon"]
    probs, outcomes, buys = [], [], []
    for d in db.query("SELECT date, symbol, action, confidence FROM decisions WHERE confidence IS NOT NULL"):
        ex = panel.excess_vs_spy(d["symbol"], d["date"], h)
        if ex is None:
            continue
        probs.append(d["confidence"])
        outcomes.append(1 if ex > 0 else 0)
        if d["action"] == "ACCUMULATE":
            buys.append(1 if ex > 0 else 0)
    if not probs:
        return {"resolved": 0}
    base = stats.mean(outcomes)
    return {
        "resolved": len(probs),
        "brier": stats.brier(probs, outcomes),
        "brier_base_rate": stats.brier([base] * len(outcomes), outcomes),
        "beat_spy_rate": base,
        "accumulate_hit_rate": stats.mean(buys) if buys else None,
        "accumulate_resolved": len(buys),
    }


def _feature(db: DB, symbol: str, date: str, on_or_before: bool = True) -> dict[str, Any] | None:
    op = "<=" if on_or_before else "="
    rows = db.query(f"SELECT * FROM features WHERE symbol = :s AND date {op} :d ORDER BY date DESC LIMIT 1", {"s": symbol, "d": date})
    return rows[0] if rows else None


def _regime(db: DB, date: str) -> int | None:
    v = db.scalar("SELECT regime_flags FROM market_state WHERE date <= :d ORDER BY date DESC LIMIT 1", {"d": date})
    return int(v) if v is not None else None


def tag_closed_positions(db: DB, panel: ReturnPanel, sector_etf: dict[str, str]) -> int:
    """Deterministic attribution tags for closed positions that have none yet. Evidence for the post-mortem."""
    n = 0
    for p in db.query("SELECT * FROM closed_positions WHERE tags IS NULL"):
        tags = []
        spy = panel.between("SPY", p["opened_date"], p["closed_date"])
        excess = p["ret"] - spy
        etf = sector_etf.get(p["sector"] or "")
        sector_excess = None
        if etf:
            sec = panel.between(etf, p["opened_date"], p["closed_date"])
            sector_excess = p["ret"] - sec
            if excess < 0 and sec - spy < 0 and abs(sec - spy) >= abs(excess) * 0.5:
                tags.append("sector_drag")
        f_open, f_close = _feature(db, p["symbol"], p["opened_date"]), _feature(db, p["symbol"], p["closed_date"])
        if f_open and f_close:
            g0, g1 = f_open["graham_passed"] or 0, f_close["graham_passed"] or 0
            b0, b1 = f_open["buffett_passed"] or 0, f_close["buffett_passed"] or 0
            if g1 < g0 - 1 or b1 < b0 - 1:
                tags.append("fundamental_break")
            if f_close["graham_mos"] is not None and f_close["graham_mos"] > 0:
                tags.append("valuation_held")
        r0, r1 = _regime(db, p["opened_date"]), _regime(db, p["closed_date"])
        if r0 is not None and r1 is not None and r1 > r0:
            tags.append("macro_shift")
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
            "UPDATE closed_positions SET tags = :t, excess_ret = :e, sector_excess_ret = :se WHERE id = :i",
            {"t": json.dumps(tags), "e": excess, "se": sector_excess, "i": p["id"]},
        )
        n += 1
    return n


def consistency_rate(db: DB) -> dict[str, Any]:
    rows = db.query(
        "SELECT a.novelty AS n1, a.sentiment AS s1, a.relevance AS m1, b.novelty AS n2, b.sentiment AS s2, b.relevance AS m2 "
        "FROM text_scores a JOIN text_scores b ON a.item_id = b.item_id AND a.score_pass = 'primary' AND b.score_pass = 'recheck'"
    )
    if not rows:
        return {"pairs": 0, "agreement": None}
    agree = sum(1 for r in rows if abs(r["n1"] - r["n2"]) <= 1 and abs(r["s1"] - r["s2"]) <= 1 and abs(r["m1"] - r["m2"]) <= 1)
    return {"pairs": len(rows), "agreement": agree / len(rows)}


def grounding_failure_rate(db: DB) -> dict[str, Any]:
    rows = db.query("SELECT thesis_ok FROM decisions WHERE thesis_ok IS NOT NULL")
    if not rows:
        return {"decisions": 0, "failure_rate": None}
    return {"decisions": len(rows), "failure_rate": sum(1 for r in rows if not r["thesis_ok"]) / len(rows)}
