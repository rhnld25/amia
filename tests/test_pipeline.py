import json
import os

from amia.config import Settings
from amia.data.mock import MockProviders, trading_days
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


def test_mock_run_end_to_end(tmp_path):
    from datetime import date

    pipe = _pipeline(tmp_path)
    days = trading_days(date(2024, 3, 1), date(2024, 4, 30))
    for d in days:
        res = pipe.daily(d.isoformat(), facts_limit=1000, live_clock=False)
        assert res["status"] == "ok"
    db = pipe.db

    decisions = db.query("SELECT * FROM decisions")
    assert decisions, "expected the mock universe to produce decisions"
    rec = json.loads(decisions[0]["record"])
    assert rec["checks"]["number_grounding"] == "pass"
    snap = load_snapshot(db, rec["snapshot_hash"])
    assert snap and rec["ticker"] in snap["inputs"]

    # every buy fills on the next trading day at the open, never the decision day
    for o in db.query("SELECT * FROM orders WHERE status = 'filled'"):
        assert o["fill_date"] > o["created_date"]

    m = metrics(db)
    assert m["days"] == len(days) - 1
    nav_rows = db.query("SELECT nav, cash, invested FROM nav")
    for r in nav_rows:
        assert abs(r["nav"] - r["cash"] - r["invested"]) < 1e-6

    # rerunning a processed day changes nothing
    before = db.scalar("SELECT COUNT(*) FROM decisions")
    pipe.daily(days[-1].isoformat(), facts_limit=1000, live_clock=False)
    assert db.scalar("SELECT COUNT(*) FROM decisions") == before

    build_site(db, str(tmp_path / "site"))
    page = (tmp_path / "site" / "index.html").read_text()
    assert "AMIA paper portfolio" in page and "Growth of 100" in page


def test_market_closed_day_makes_no_decisions(tmp_path):
    pipe = _pipeline(tmp_path)
    res = pipe.daily("2024-03-02", live_clock=False)  # Saturday
    assert res["status"] == "market_closed"
    assert pipe.db.scalar("SELECT COUNT(*) FROM decisions") == 0


def test_point_in_time_scores_do_not_see_future_filings(tmp_path):
    pipe = _pipeline(tmp_path)
    pipe.daily("2024-03-01", facts_limit=1000, live_clock=False)
    future = pipe.db.scalar("SELECT COUNT(*) FROM facts WHERE filed > '2024-03-01'")
    assert future > 0  # the mock serves facts filed later...
    # ...but scoring used only facts filed on or before the run date
    snap_rows = pipe.db.query("SELECT hash FROM snapshots")
    for r in snap_rows:
        snap = load_snapshot(pipe.db, r["hash"])
        for sym, inp in snap["inputs"].items():
            for concept, periods in inp["facts"].items():
                assert all(end <= "2024-03-01" for end in periods), (sym, concept)
