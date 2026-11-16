import json
import os
from datetime import date, timedelta

from amia.config import PORTFOLIOS, SHORT, Settings
from amia.data.mock import NO_SEC, MockProviders, trading_days
from amia.db import DB
from amia.pipeline import Pipeline
from amia.portfolio import metrics
from amia.site import build_site
from amia.snapshots import load_snapshot


def _pipeline(tmp_path):
    # CI sets AMIA_TEST_DATABASE_URL to a throwaway Postgres so the SQL is tested on both engines
    url = os.environ.get("AMIA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 't.db'}"
    if url.startswith("postgres"):
        db = DB(url)
        db.execute("DROP SCHEMA public CASCADE")
        db.execute("CREATE SCHEMA public")
    settings = Settings(database_url=url, mock=True)
    return Pipeline(settings, providers=MockProviders())


def _run(pipe, start, end):
    days = trading_days(start, end)
    for d in days:
        res = pipe.daily(d.isoformat(), facts_limit=1000, live_clock=False)
        assert res["status"] == "ok"
    return days


def test_mock_run_end_to_end(tmp_path):
    pipe = _pipeline(tmp_path)
    days = _run(pipe, date(2024, 3, 1), date(2024, 4, 30))
    db = pipe.db

    # the watchlist comes from news and changes as the stories move on
    by_day = db.query("SELECT date, COUNT(*) AS n FROM watchlist GROUP BY date")
    assert by_day and max(r["n"] for r in by_day) <= 40 + db.scalar("SELECT COUNT(*) FROM positions")
    distinct = db.scalar("SELECT COUNT(DISTINCT symbol) FROM watchlist")
    assert distinct > max(r["n"] for r in by_day)
    assert db.scalar("SELECT COUNT(*) FROM watchlist WHERE symbol = 'BTCUSD'") == 0  # not an equity ticker

    # a ticker with no SEC filings is outside every portfolio's universe (US stocks), so it gets no view
    assert db.scalar("SELECT COUNT(*) FROM views WHERE symbol = :s", {"s": NO_SEC}) == 0
    assert db.scalar("SELECT COUNT(*) FROM watchlist WHERE symbol = :s", {"s": NO_SEC}) > 0

    # one view per stock per review, shared by every portfolio, with its inputs in a content-hashed snapshot
    views = db.query("SELECT * FROM views")
    assert views
    v = json.loads(views[0]["record"])
    assert set(v["framework_views"]) == {"graham", "buffett", "marks"} and v["counter_case"]
    assert v["checks"]["number_grounding"] == "pass" and v["benchmark"] == views[0]["benchmark"]
    snap = load_snapshot(db, v["snapshot_hash"])
    assert snap and v["ticker"] in snap["inputs"] and "dossier" in snap["inputs"][v["ticker"]]

    # each portfolio acts on the views within its strategy
    acts = {(r["portfolio_id"], r["action"]) for r in db.query("SELECT DISTINCT portfolio_id, action FROM decisions WHERE executed = 1")}
    for pid in PORTFOLIOS:
        assert (pid, "ACCUMULATE") in acts
    assert ("core_ls", "SHORT") in acts and ("core_lsh", "SHORT") in acts
    assert ("core_hedged", "HEDGE") in acts and ("core_lsh", "HEDGE") in acts
    assert not {("core", "SHORT"), ("core", "HEDGE"), ("core_hedged", "SHORT"), ("core_ls", "HEDGE")} & acts
    # entries follow the view by rule: longs at p >= the required bar, shorts only at p <= 0.30
    for r in db.query("SELECT action, confidence, record FROM decisions WHERE action IN ('ACCUMULATE', 'SHORT') AND executed = 1"):
        rec = json.loads(r["record"])
        assert (r["confidence"] >= rec["required"]) if r["action"] == "ACCUMULATE" else (r["confidence"] <= SHORT["max_p"])
    # after a short is stopped out, that portfolio leaves the stock alone for the cooldown
    stops = db.query("SELECT portfolio_id, symbol, date FROM decisions WHERE action = 'COVER' AND record LIKE '%short stop%'")
    assert stops  # the mock market runs at least one short into its stop
    for c in stops:
        again = db.scalar(
            "SELECT COUNT(*) FROM decisions WHERE portfolio_id = :p AND symbol = :s AND action = 'SHORT' AND executed = 1 AND date > :d AND date <= :e",
            {"p": c["portfolio_id"], "s": c["symbol"], "d": c["date"], "e": (date.fromisoformat(c["date"]) + timedelta(days=SHORT["stop_cooldown_days"])).isoformat()},
        )
        assert again == 0, c
    # open positions are judged by the model, one call per position per portfolio
    calls = db.query("SELECT record FROM decisions WHERE action IN ('HOLD', 'EXIT', 'COVER')")
    assert calls and all(json.loads(c["record"])["decided_by"] in ("model: position call", "code: short stop", "code: share-class consolidation") for c in calls)
    for pid in PORTFOLIOS:
        shorts = db.scalar("SELECT COALESCE(SUM(value), 0) FROM positions WHERE portfolio_id = :p AND side = 'short' AND kind = 'stock'", {"p": pid})
        assert shorts <= SHORT["max_total_weight"] * metrics(db, pid)["nav"] * 1.1  # the cap holds (prices drift after entry)

    # only executed actions become orders, and they fill on a later day at the open
    decisions = db.query("SELECT * FROM decisions")
    executed = {d["decision_id"] for d in decisions if d["executed"]}
    assert {o["decision_id"] for o in db.query("SELECT decision_id FROM orders")} == executed
    for o in db.query("SELECT * FROM orders WHERE status = 'filled'"):
        assert o["fill_date"] > o["created_date"]

    for pid in PORTFOLIOS:
        assert metrics(db, pid)["days"] == len(days) - 1
    for r in db.query("SELECT nav, cash, invested FROM nav"):
        assert abs(r["nav"] - r["cash"] - r["invested"]) < 1e-6

    # rerunning a processed day changes nothing (and makes no new model calls)
    before = db.scalar("SELECT COUNT(*) FROM decisions")
    pipe.daily(days[-1].isoformat(), facts_limit=1000, live_clock=False)
    assert db.scalar("SELECT COUNT(*) FROM decisions") == before

    # monthly post-mortems cover every trade closed in the window once, however many portfolios made it
    report = pipe.monthly_report("2024-05-01")
    trades = db.scalar(
        "SELECT COUNT(*) FROM (SELECT DISTINCT symbol, opened_date, side FROM closed_positions WHERE kind = 'stock' AND closed_date >= '2024-03-01') t"
    )
    assert trades and db.scalar("SELECT COUNT(*) FROM postmortems WHERE case_type = 'closed_position'") == trades
    assert "Which inputs predict returns vs SPY" in report

    build_site(db, str(tmp_path / "site"))
    page = (tmp_path / "site" / "index.html").read_text()
    assert "AMIA paper portfolios" in page and "watchlist (from news traction)" in page and "T-bills" in page
    assert all(cfg["name"] in page for cfg in PORTFOLIOS.values())


def test_market_closed_day_makes_no_decisions(tmp_path):
    pipe = _pipeline(tmp_path)
    res = pipe.daily("2024-03-02", live_clock=False)  # Saturday
    assert res["status"] == "market_closed"
    assert pipe.db.scalar("SELECT COUNT(*) FROM decisions") == 0


def test_point_in_time_inputs_do_not_see_future_filings(tmp_path):
    pipe = _pipeline(tmp_path)
    _run(pipe, date(2024, 3, 1), date(2024, 3, 8))
    future = pipe.db.scalar("SELECT COUNT(*) FROM facts WHERE filed > '2024-03-08'")
    assert future > 0  # the mock serves facts filed later...
    # ...but every snapshot used only facts filed on or before its own run date
    for r in pipe.db.query("SELECT hash, date FROM snapshots"):
        snap = load_snapshot(pipe.db, r["hash"])
        for sym, inp in snap["inputs"].items():
            for concept, periods in inp["facts"].items():
                assert all(end <= r["date"] for end in periods), (sym, concept)
