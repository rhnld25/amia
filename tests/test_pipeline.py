import json
import os
from datetime import date

from amia.config import Settings
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

    # a ticker with no SEC filings still gets reviewed, told its fundamentals are missing
    nosec = db.query("SELECT record FROM decisions WHERE symbol = :s LIMIT 1", {"s": NO_SEC})
    assert nosec and json.loads(nosec[0]["record"])["computed"]["graham"]["available"] is False

    decisions = db.query("SELECT * FROM decisions")
    assert decisions
    assert any(d["action"] == "ACCUMULATE" and d["executed"] for d in decisions)
    rec = json.loads(decisions[0]["record"])
    assert set(rec["framework_views"]) == {"graham", "buffett", "marks"}
    assert rec["checks"]["number_grounding"] == "pass"
    snap = load_snapshot(db, rec["snapshot_hash"])
    assert snap and rec["ticker"] in snap["inputs"] and "dossier" in snap["inputs"][rec["ticker"]]

    # only executed buys and exits become orders, and they fill on a later day at the open
    executed = {d["decision_id"] for d in decisions if d["executed"] and d["action"] in ("ACCUMULATE", "EXIT")}
    assert {o["decision_id"] for o in db.query("SELECT decision_id FROM orders")} == executed
    for o in db.query("SELECT * FROM orders WHERE status = 'filled'"):
        assert o["fill_date"] > o["created_date"]

    m = metrics(db)
    assert m["days"] == len(days) - 1
    for r in db.query("SELECT nav, cash, invested FROM nav"):
        assert abs(r["nav"] - r["cash"] - r["invested"]) < 1e-6

    # rerunning a processed day changes nothing (and makes no new model calls)
    before = db.scalar("SELECT COUNT(*) FROM decisions")
    pipe.daily(days[-1].isoformat(), facts_limit=1000, live_clock=False)
    assert db.scalar("SELECT COUNT(*) FROM decisions") == before

    # monthly post-mortems cover every position closed in the window
    report = pipe.monthly_report("2024-05-01")
    closed = db.scalar("SELECT COUNT(*) FROM closed_positions WHERE closed_date >= '2024-03-01'")  # this month and last
    assert db.scalar("SELECT COUNT(*) FROM postmortems WHERE case_type = 'closed_position'") == closed
    assert "Which inputs predict returns vs SPY" in report

    build_site(db, str(tmp_path / "site"))
    page = (tmp_path / "site" / "index.html").read_text()
    assert "AMIA paper portfolio" in page and "watchlist (from news traction)" in page and "T-bills" in page


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
