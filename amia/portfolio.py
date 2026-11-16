"""Paper portfolio: next-open fills with slippage, daily total-return marking, and performance metrics."""

from __future__ import annotations

import json
import math
from typing import Any

from . import stats
from .config import CORE, EVALUATION, PORTFOLIO, PORTFOLIOS
from .db import DB


def sync_portfolios(db: DB) -> None:
    """Write the configured portfolios to the database, so every book has a row describing it."""
    rows = [
        {
            "portfolio_id": pid,
            "name": p["name"],
            "universe": p["universe"],
            "strategy": p["strategy"],
            "hedge": p.get("hedge"),
            "params": json.dumps(p.get("params") or {}),
            "started": p["started"],
            "status": "active",
        }
        for pid, p in PORTFOLIOS.items()
    ]
    db.upsert("portfolios", rows, ["portfolio_id"])


def cash(db: DB, pid: str = CORE) -> float:
    return float(db.get_kv(f"cash:{pid}", PORTFOLIO["starting_nav"]))


def positions(db: DB, pid: str = CORE) -> dict[str, dict[str, Any]]:
    return {p["symbol"]: p for p in db.query("SELECT * FROM positions WHERE portfolio_id = :p", {"p": pid})}


def last_nav(db: DB, pid: str = CORE) -> float:
    v = db.scalar("SELECT nav FROM nav WHERE portfolio_id = :p ORDER BY date DESC LIMIT 1", {"p": pid})
    return float(v) if v is not None else PORTFOLIO["starting_nav"]


def _slippage(size: str | None) -> float:
    return PORTFOLIO["slippage_large"] if size == "large" else PORTFOLIO["slippage_small"]


def run_day(
    db: DB,
    date: str,
    bars: dict[str, dict[str, Any]],
    prev_close: dict[str, float],
    size_of: dict[str, str],
    sector_of: dict[str, str],
    spy_ret: float | None,
    rf_annual_pct: float | None,
    pid: str = CORE,
) -> dict[str, Any]:
    """Fill one portfolio's pending orders at today's open, mark its book to today's close, append a NAV row."""
    if db.scalar("SELECT 1 FROM nav WHERE portfolio_id = :p AND date = :d", {"p": pid, "d": date}):
        return {"skipped": "already processed"}
    nav_prev = last_nav(db, pid)
    c = cash(db, pid)
    pos = positions(db, pid)
    traded = 0.0
    filled_today: set[str] = set()
    events = []

    for o in db.query(
        "SELECT * FROM orders WHERE portfolio_id = :p AND status = 'pending' AND created_date < :d ORDER BY created_date",
        {"p": pid, "d": date},
    ):
        sym = o["symbol"]
        bar = bars.get(sym)
        if not bar or not bar.get("open"):
            continue  # no trade today (halt or missing data); retry tomorrow
        slip = _slippage(size_of.get(sym))
        if o["side"] == "BUY" and sym not in pos:
            amount = min(o["target_weight"] * nav_prev, c)
            if amount <= 0:
                db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = :i", {"i": o["order_id"]})
                continue
            fill = bar["open"] * (1 + slip)
            value = amount * bar["close"] / fill
            c -= amount
            traded += amount
            pos[sym] = {
                "portfolio_id": pid,
                "symbol": sym,
                "sector": sector_of.get(sym),
                "opened_date": date,
                "entry_price": fill,
                "value": value,
                "cost": amount,
                "decision_id": o["decision_id"],
                "last_close": bar["close"],
            }
            filled_today.add(sym)
            db.execute(
                "UPDATE orders SET status = 'filled', fill_date = :d, fill_price = :p, amount = :a WHERE order_id = :i",
                {"d": date, "p": fill, "a": amount, "i": o["order_id"]},
            )
            events.append(f"BUY {sym} {amount:,.0f} @ {fill:.2f}")
        elif o["side"] == "SELL" and sym in pos:
            p = pos.pop(sym)
            pc = prev_close.get(sym) or p["last_close"]
            exit_price = bar["open"] * (1 - slip)
            proceeds = p["value"] * exit_price / pc
            c += proceeds
            traded += proceeds
            ret = proceeds / p["cost"] - 1
            db.upsert(
                "closed_positions",
                [
                    {
                        "id": f"{sym}-{p['opened_date']}-{date}" if pid == CORE else f"{pid}:{sym}-{p['opened_date']}-{date}",
                        "portfolio_id": pid,
                        "symbol": sym,
                        "sector": p["sector"],
                        "opened_date": p["opened_date"],
                        "closed_date": date,
                        "entry_price": p["entry_price"],
                        "exit_price": exit_price,
                        "ret": ret,
                        "excess_ret": None,
                        "sector_excess_ret": None,
                        "decision_id": p["decision_id"],
                        "exit_decision_id": o["decision_id"],
                        "tags": None,
                    }
                ],
                ["id"],
            )
            db.execute("DELETE FROM positions WHERE portfolio_id = :p AND symbol = :s", {"p": pid, "s": sym})
            db.execute(
                "UPDATE orders SET status = 'filled', fill_date = :d, fill_price = :p, amount = :a WHERE order_id = :i",
                {"d": date, "p": exit_price, "a": proceeds, "i": o["order_id"]},
            )
            events.append(f"SELL {sym} {proceeds:,.0f} @ {exit_price:.2f} ({ret:+.1%})")
        else:
            db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = :i", {"i": o["order_id"]})

    for sym, p in pos.items():
        bar = bars.get(sym)
        if sym not in filled_today and bar and bar.get("ret") is not None:
            p["value"] *= 1 + bar["ret"]
            p["last_close"] = bar["close"]
    rf_daily = (rf_annual_pct or 0) / 100 / 252
    c *= 1 + rf_daily

    db.execute("DELETE FROM positions WHERE portfolio_id = :p", {"p": pid})
    db.upsert("positions", [{**p, "portfolio_id": pid} for p in pos.values()], ["portfolio_id", "symbol"])
    db.set_kv(f"cash:{pid}", c)
    invested = sum(p["value"] for p in pos.values())
    nav = c + invested
    db.upsert(
        "nav",
        [
            {
                "portfolio_id": pid,
                "date": date,
                "nav": nav,
                "cash": c,
                "invested": invested,
                "ret": nav / nav_prev - 1,
                "spy_ret": spy_ret,
                "rf_ret": rf_daily,
                "turnover": traded / nav_prev if nav_prev else 0,
            }
        ],
        ["portfolio_id", "date"],
    )
    return {"nav": nav, "cash": c, "invested": invested, "events": events}


def metrics(db: DB, pid: str = CORE) -> dict[str, Any]:
    """Absolute return, beta-adjusted alpha vs SPY, drawdown and Sharpe, with intervals that show how
    little a short record proves."""
    rows = db.query("SELECT * FROM nav WHERE portfolio_id = :p ORDER BY date", {"p": pid})
    if len(rows) < 2:
        return {"days": len(rows)}
    rets = [r["ret"] for r in rows[1:]]
    rf = [r["rf_ret"] or 0 for r in rows[1:]]
    spy = [r["spy_ret"] for r in rows[1:]]
    n = len(rets)
    years = n / 252
    excess = [a - b for a, b in zip(rets, rf)]
    sd = stats.stdev(excess)
    sharpe = stats.mean(excess) / sd * math.sqrt(252) if sd and sd == sd else float("nan")
    downside = [min(0.0, e) for e in excess]
    dsd = math.sqrt(sum(d * d for d in downside) / n) if n else 0
    sortino = stats.mean(excess) / dsd * math.sqrt(252) if dsd else float("nan")
    levels = [r["nav"] for r in rows]
    out: dict[str, Any] = {
        "days": n,
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "nav": levels[-1],
        "total_return": levels[-1] / PORTFOLIO["starting_nav"] - 1,
        "ann_vol": stats.stdev(rets) * math.sqrt(252) if n > 1 else float("nan"),
        "sharpe": sharpe,
        "sharpe_ci95": 1.96 * math.sqrt((1 + 0.5 * sharpe**2) / years) if years > 0 and sharpe == sharpe else float("nan"),
        "sortino": sortino,
        "max_drawdown": stats.max_drawdown(levels),
        "turnover_ann": sum(r["turnover"] or 0 for r in rows) / years if years else float("nan"),
        "invested_frac": rows[-1]["invested"] / rows[-1]["nav"] if rows[-1]["nav"] else 0,
    }
    paired = [(x, y, f) for x, y, f in zip(spy, rets, rf) if x is not None]
    if len(paired) >= 20:
        xs = [x - f for x, _, f in paired]
        ys = [y - f for _, y, f in paired]
        a, b, a_se, _ = stats.ols(xs, ys)
        te = stats.stdev([y - x for x, y, _ in paired]) * math.sqrt(252)
        out.update(
            {
                "alpha_ann": a * 252,
                "alpha_ci95": 1.96 * a_se * 252,
                "beta_spy": b,
                "tracking_error": te,
                # years of data needed for a 3%/yr alpha to reach t = 2 at the current tracking error
                "years_needed_3pct_alpha": (2 * te / 0.03) ** 2 if te == te else float("nan"),
            }
        )
    cum = {"spy": 1.0, "cash": 1.0}
    for r in rows[1:]:
        cum["spy"] *= 1 + (r["spy_ret"] or 0)
        cum["cash"] *= 1 + (r["rf_ret"] or 0)
    out["spy_total_return"] = cum["spy"] - 1
    out["cash_total_return"] = cum["cash"] - 1
    out["horizon_note"] = f"{years:.2f} years of history; decisions are judged over {EVALUATION['decision_horizon']} trading days"
    return out
