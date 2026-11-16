"""Paper portfolio: next-open fills with slippage, daily total-return marking, and performance metrics."""

from __future__ import annotations

import json
import math
from typing import Any

from . import stats
from .config import CORE, EVALUATION, PORTFOLIO, PORTFOLIOS, SHORT
from .db import DB


def sync_portfolios(db: DB) -> None:
    """Write the configured portfolios to the database, so every book has a row describing it. A portfolio with no
    configured start date keeps the date of its first run."""
    started = {r["portfolio_id"]: r["started"] for r in db.query("SELECT portfolio_id, started FROM portfolios")}
    rows = [
        {
            "portfolio_id": pid,
            "name": p["name"],
            "universe": p["universe"],
            "strategy": p["strategy"],
            "hedge": p.get("hedge"),
            "params": json.dumps(p.get("params") or {}),
            "started": p["started"] or started.get(pid),
            "status": "active",
        }
        for pid, p in PORTFOLIOS.items()
    ]
    db.upsert("portfolios", rows, ["portfolio_id"])


def held_symbols(db: DB) -> set[str]:
    """Every stock any portfolio holds, long or short. Hedge ETFs are left out: they are benchmarks, not watchlist names."""
    return {r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM positions WHERE kind = 'stock'")}


def cash(db: DB, pid: str = CORE) -> float:
    return float(db.get_kv(f"cash:{pid}", PORTFOLIO["starting_nav"]))


def positions(db: DB, pid: str = CORE) -> dict[str, dict[str, Any]]:
    return {p["symbol"]: p for p in db.query("SELECT * FROM positions WHERE portfolio_id = :p", {"p": pid})}


def last_nav(db: DB, pid: str = CORE) -> float:
    v = db.scalar("SELECT nav FROM nav WHERE portfolio_id = :p ORDER BY date DESC LIMIT 1", {"p": pid})
    return float(v) if v is not None else PORTFOLIO["starting_nav"]


def _slippage(size: str | None) -> float:
    return PORTFOLIO["slippage_large"] if size == "large" else PORTFOLIO["slippage_small"]


def _close(db: DB, pid: str, p: dict[str, Any], date: str, exit_price: float, proceeds: float, ret: float | None, exit_decision_id: str | None) -> None:
    sym = p["symbol"]
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
                "exit_decision_id": exit_decision_id,
                "tags": None,
                "side": p.get("side") or "long",
                "kind": p.get("kind") or "stock",
            }
        ],
        ["id"],
    )


def _fill(db: DB, o: dict[str, Any], date: str, price: float, amount: float) -> None:
    db.execute(
        "UPDATE orders SET status = 'filled', fill_date = :d, fill_price = :p, amount = :a WHERE order_id = :i",
        {"d": date, "p": price, "a": amount, "i": o["order_id"]},
    )


def _cancel(db: DB, o: dict[str, Any]) -> None:
    db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = :i", {"i": o["order_id"]})


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
    """Fill one portfolio's pending orders at today's open, mark its book to today's close, append a NAV row.

    Longs are worth `value`. Shorts (stocks and hedges) are liabilities of `value`: opening one adds its proceeds
    to cash, and NAV = cash + longs - shorts. Every position is marked with the day's total return, so a short pays
    the dividends it owes; shorts also pay a daily borrow fee. Cash, short proceeds included, earns the T-bill rate.

    Order sides: BUY and SELL open and close longs, SHORT and COVER open and close shorts, and HEDGE sets an ETF
    short to `target_weight` of NAV (0 closes it)."""
    if db.scalar("SELECT 1 FROM nav WHERE portfolio_id = :p AND date = :d", {"p": pid, "d": date}):
        return {"skipped": "already processed"}
    nav_prev = last_nav(db, pid)
    c = cash(db, pid)
    pos = positions(db, pid)
    traded = 0.0
    filled_today: set[str] = set()
    events = []

    def new_position(sym: str, o: dict[str, Any], fill: float, amount: float, close: float, side: str, kind: str) -> None:
        pos[sym] = {
            "portfolio_id": pid,
            "symbol": sym,
            "sector": sector_of.get(sym),
            "opened_date": date,
            "entry_price": fill,
            "value": amount * close / fill,
            "cost": amount,
            "decision_id": o["decision_id"],
            "last_close": close,
            "side": side,
            "kind": kind,
        }
        filled_today.add(sym)

    for o in db.query(
        "SELECT * FROM orders WHERE portfolio_id = :p AND status = 'pending' AND created_date < :d ORDER BY created_date",
        {"p": pid, "d": date},
    ):
        sym, side = o["symbol"], o["side"]
        bar = bars.get(sym)
        if not bar or not bar.get("open"):
            continue  # no trade today (halt or missing data); retry tomorrow
        slip = _slippage(size_of.get(sym))
        held = pos.get(sym)
        if side == "BUY" and not held:
            amount = min(o["target_weight"] * nav_prev, c)
            if amount <= 0:
                _cancel(db, o)
                continue
            fill = bar["open"] * (1 + slip)
            c -= amount
            traded += amount
            new_position(sym, o, fill, amount, bar["close"], "long", "stock")
            _fill(db, o, date, fill, amount)
            events.append(f"BUY {sym} {amount:,.0f} @ {fill:.2f}")
        elif side == "SHORT" and not held:
            amount = o["target_weight"] * nav_prev
            fill = bar["open"] * (1 - slip)
            c += amount
            traded += amount
            new_position(sym, o, fill, amount, bar["close"], "short", "stock")
            _fill(db, o, date, fill, amount)
            events.append(f"SHORT {sym} {amount:,.0f} @ {fill:.2f}")
        elif (side == "SELL" and held and held.get("side", "long") == "long") or (side == "COVER" and held and held.get("side") == "short"):
            p = pos.pop(sym)
            pc = prev_close.get(sym) or p["last_close"]
            if side == "SELL":
                exit_price = bar["open"] * (1 - slip)
                amount = p["value"] * exit_price / pc
                c += amount
                ret = amount / p["cost"] - 1
            else:
                exit_price = bar["open"] * (1 + slip)
                amount = p["value"] * exit_price / pc
                c -= amount
                ret = 1 - amount / p["cost"]
            traded += amount
            _close(db, pid, p, date, exit_price, amount, ret, o["decision_id"])
            db.execute("DELETE FROM positions WHERE portfolio_id = :p AND symbol = :s", {"p": pid, "s": sym})
            _fill(db, o, date, exit_price, amount)
            events.append(f"{side} {sym} {amount:,.0f} @ {exit_price:.2f} ({ret:+.1%})")
        elif side == "HEDGE" and (not held or held.get("kind") == "hedge"):
            pc = prev_close.get(sym) or (held or {}).get("last_close") or bar["open"]
            current = held["value"] * bar["open"] / pc if held else 0.0  # liability at today's open
            target = max(o["target_weight"], 0.0) * nav_prev
            delta = target - current  # > 0 shorts more, < 0 buys back
            c += delta - abs(delta) * slip
            traded += abs(delta)
            if target <= 0 and held:
                p = pos.pop(sym)
                # a hedge is resized often, so a return on it means little; its effect shows in the portfolio's NAV
                _close(db, pid, p, date, bar["open"] * (1 + slip), current, None, o["decision_id"])
                db.execute("DELETE FROM positions WHERE portfolio_id = :p AND symbol = :s", {"p": pid, "s": sym})
            elif held:
                held.update(value=target * bar["close"] / bar["open"], cost=held["cost"] + delta, last_close=bar["close"])
                filled_today.add(sym)
            elif target > 0:
                new_position(sym, o, bar["open"], target, bar["close"], "short", "hedge")
            _fill(db, o, date, bar["open"], delta)
            events.append(f"HEDGE {sym} to {o['target_weight']:.1%} ({delta:+,.0f})")
        else:
            _cancel(db, o)

    fee_daily = SHORT["borrow_fee_annual"] / 252
    for sym, p in pos.items():
        bar = bars.get(sym)
        if sym not in filled_today and bar and bar.get("ret") is not None:
            p["value"] *= 1 + bar["ret"]
            p["last_close"] = bar["close"]
        if p.get("side") == "short":
            c -= p["value"] * fee_daily
    rf_daily = (rf_annual_pct or 0) / 100 / 252
    c *= 1 + rf_daily

    db.execute("DELETE FROM positions WHERE portfolio_id = :p", {"p": pid})
    db.upsert("positions", [{**p, "portfolio_id": pid} for p in pos.values()], ["portfolio_id", "symbol"])
    db.set_kv(f"cash:{pid}", c)
    invested = sum(p["value"] if p.get("side", "long") == "long" else -p["value"] for p in pos.values())
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
    if not db.scalar("SELECT started FROM portfolios WHERE portfolio_id = :p", {"p": pid}):
        db.execute("UPDATE portfolios SET started = :d WHERE portfolio_id = :p", {"d": date, "p": pid})
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
